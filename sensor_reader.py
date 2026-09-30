"""
Soil sensor reader for AgriRover — 7-in-1 RS485/Modbus probe (moisture,
temperature, EC, pH, N, P, K in one read).

Wiring (MAX485 TTL module between Pi and probe; Pi 3.3 V logic):
    Pi GPIO14 (pin 8,  TXD)  -> MAX485 DI
    Pi GPIO15 (pin 10, RXD)  <- MAX485 RO
    Pi GPIO24 (pin 18)       -> MAX485 DE
    Pi GPIO25 (pin 22)       -> MAX485 RE   (driven together with DE)
    Pi 3V3 + GND             -> MAX485 VCC + GND
    MAX485 A/B               -> probe A/B (yellow/blue typically)
    Probe V+ (brown, 5-30 V) and GND (black) -> separate supply; share its
    GND with the Pi. A 5th wire, if any, is usually shield: leave it off.
Pi setup: raspi-config -> Interface -> Serial: login shell NO, hardware YES.
Motors already use GPIO 5, 6, 17, 22, 23, 27 — these four don't clash.

Readings are persisted to a local JSONL log (append-only, survives
Pi restarts) as the durable source of truth, independent of whether a
browser is currently connected. See broadcaster.py for how the log is
replayed to a freshly (re)connected dashboard.

Usage:
    from sensor_reader import read_sensors, append_reading, SENSOR_LOG_PATH
    reading = read_sensors()      # dict or None on a read failure
    if reading:
        append_reading(SENSOR_LOG_PATH, reading)
"""

import json
import logging
import os
import statistics
import threading
import time
from pathlib import Path

logger = logging.getLogger("sensor-reader")

# ── Wiring / register map — edit to match your probe's datasheet ───────────
DE_PIN, RE_PIN = 24, 25  # BCM; MAX485 driver/receiver enable
SERIAL_PORT = "/dev/ttyAMA0"   # Pi 5: GPIO14/15 UART (serial0 -> ttyAMA10 is the debug port)
BAUDRATE = 4800
SLAVE_ADDRESS = 1

# (register offset, scale, signed) — raw register value * scale = real units.
# Standard generic 7-in-1 probe register map; adjust if yours differs.
REGISTERS = {
    "moisture":    (0, 0.1, False),   # %
    "temperature": (1, 0.1, True),    # degC, can go below zero
    "ec":          (2, 1.0, False),   # uS/cm
    "ph":          (3, 0.1, False),
    "n":           (4, 1.0, False),   # mg/kg
    "p":           (5, 1.0, False),   # mg/kg
    "k":           (6, 1.0, False),   # mg/kg
}
REGISTER_COUNT = 7

# Sanity limits — a value outside these means a corrupt frame or a probe
# that isn't in the soil, so the whole reading is rejected.
VALID_RANGE = {
    "moisture":    (0, 100),
    "temperature": (-40, 80),
    "ec":          (0, 20000),
    "ph":          (0, 14),
    "n":           (0, 5000),
    "p":           (0, 5000),
    "k":           (0, 5000),
}

READ_ATTEMPTS = 3
RETRY_DELAY = 0.2  # seconds between attempts
TURNAROUND_DELAY = 0.002  # let the last bit leave the UART before RX opens

SENSOR_LOG_PATH = Path(__file__).parent / "sensor_log.jsonl"


_instrument_cache = None
_de_re = None  # DE/RE pins are created once and never closed; only the port is reopened
_io_lock = threading.Lock()  # one Modbus transaction at a time on the shared bus


def _instrument():
    """Open the port once and reuse it; DE/RE flip around every write."""
    global _instrument_cache, _de_re
    if _instrument_cache is None:
        import minimalmodbus
        if _de_re is None:
            from gpiozero import DigitalOutputDevice
            _de_re = (DigitalOutputDevice(DE_PIN), DigitalOutputDevice(RE_PIN))
        de, re_ = _de_re
        inst = minimalmodbus.Instrument(SERIAL_PORT, SLAVE_ADDRESS)
        inst.serial.baudrate = BAUDRATE
        inst.serial.timeout = 1
        raw_write = inst.serial.write

        def write(data):
            de.on(); re_.on()            # transmit, receiver off
            try:
                n = raw_write(data)
                inst.serial.flush()      # block until last byte is on the wire
                time.sleep(TURNAROUND_DELAY)
            finally:
                de.off(); re_.off()      # always back to listening
            return n

        inst.serial.write = write
        _instrument_cache = inst
    return _instrument_cache


def _reset_instrument():
    """Drop the cached port so the next read reopens it from scratch."""
    global _instrument_cache
    inst, _instrument_cache = _instrument_cache, None
    if inst is not None:
        try:
            inst.serial.close()
        except Exception:
            pass


def _decode(registers):
    """Raw register list -> dict in real units, or None if implausible."""
    reading = {}
    for name, (offset, scale, signed) in REGISTERS.items():
        raw = registers[offset]
        if signed and raw >= 0x8000:
            raw -= 0x10000
        value = round(raw * scale, 2)
        low, high = VALID_RANGE[name]
        if not low <= value <= high:
            logger.warning(f"Rejecting reading: {name}={value} outside {low}..{high}")
            return None
        reading[name] = value
    return reading


class Calibrator:
    """Turns raw probe frames into trustworthy values (derived from logged
    air / plant / water runs; there are no reference solutions, so no
    offset/gain is applied).

    - Median of the last WINDOW frames: removes the 0/100 flapping and
      one-off spikes seen while the probe is moved or re-inserted.
    - Air detection: moisture and EC both 0 means no medium; EC, pH and NPK
      are meaningless then (pH wanders 5-6 in air), so they become None.
    - pH is None until the window is stable: after insertion it drifts for
      minutes (9.0 -> 4.8 in the log). `stable` flags the same for the rest.
    """
    WINDOW = 5
    STABLE_SPAN = {"moisture": 2.0, "ec": 5.0, "ph": 0.3}  # max-min over the window
    MEDIUM_KEYS = ("moisture", "ec", "ph", "n", "p", "k")

    def __init__(self):
        self._history = []

    def update(self, raw):
        self._history = (self._history + [raw])[-self.WINDOW:]
        med = {k: statistics.median(r[k] for r in self._history) for k in raw}
        out = {"temperature": round(med["temperature"], 1)}
        in_air = med["moisture"] == 0 and med["ec"] == 0
        stable = (
            not in_air
            and len(self._history) == self.WINDOW
            and all(
                max(r[k] for r in self._history) - min(r[k] for r in self._history) <= span
                for k, span in self.STABLE_SPAN.items()
            )
        )
        for k in self.MEDIUM_KEYS:
            out[k] = None if in_air else round(med[k], 2)
        if not stable:
            out["ph"] = None
        out["in_air"] = in_air
        out["stable"] = stable
        return out


_calibrator = Calibrator()


def read_sensors():
    """Read one set of soil values. Returns a dict, or None on failure."""
    if os.environ.get("SENSOR_MOCK") == "1":
        return _mock_reading()

    with _io_lock:
        for attempt in range(1, READ_ATTEMPTS + 1):
            try:
                registers = _instrument().read_registers(0, REGISTER_COUNT, functioncode=3)
                reading = _decode(registers)
                if reading is not None:
                    return _calibrator.update(reading)
            except Exception as e:
                logger.warning(f"Sensor read failed (attempt {attempt}/{READ_ATTEMPTS}): {e}")
                _reset_instrument()  # port/GPIO may be wedged; reopen next try
            time.sleep(RETRY_DELAY)
    return None


def _mock_reading():
    # ponytail: fake data for testing the pipeline without hardware —
    # SENSOR_MOCK=1 env var only, real reads otherwise always hit the probe.
    import random
    return {
        "moisture": round(random.uniform(15, 45), 1),
        "temperature": round(random.uniform(15, 35), 1),
        "ec":       round(random.uniform(200, 2000), 0),
        "ph":       round(random.uniform(5.5, 7.5), 1),
        "n":        round(random.uniform(20, 200), 0),
        "p":        round(random.uniform(10, 100), 0),
        "k":        round(random.uniform(50, 300), 0),
    }


def append_reading(path, reading):
    """Append one timestamped reading to the JSONL log (durable, append-only)."""
    entry = {"ts": time.time(), **reading}
    with open(path, "a") as f:
        f.write(json.dumps(entry) + "\n")
    return entry


def read_log(path):
    """Read every logged entry back, in order. Returns [] if no log yet."""
    if not Path(path).exists():
        return []
    with open(path) as f:
        return [json.loads(line) for line in f if line.strip()]


def demo():
    """Runnable self-check — SENSOR_MOCK=1 avoids needing real hardware."""
    import tempfile

    os.environ["SENSOR_MOCK"] = "1"
    reading = read_sensors()
    assert reading is not None
    for key in ("moisture", "temperature", "ec", "ph", "n", "p", "k"):
        assert key in reading, f"missing {key} in reading"

    with tempfile.TemporaryDirectory() as tmp:
        log_path = Path(tmp) / "sensor_log.jsonl"
        assert read_log(log_path) == []
        append_reading(log_path, reading)
        append_reading(log_path, reading)
        entries = read_log(log_path)
        assert len(entries) == 2
        assert entries[0]["moisture"] == reading["moisture"]
        assert "ts" in entries[0]

    # decode: scaling, signed temperature, range rejection
    ok = _decode([352, 0xFFF6, 1200, 65, 30, 20, 100])
    assert ok == {"moisture": 35.2, "temperature": -1.0, "ec": 1200.0,
                  "ph": 6.5, "n": 30.0, "p": 20.0, "k": 100.0}, ok
    assert _decode([352, 250, 1200, 200, 30, 20, 100]) is None  # pH 20 is garbage

    print("sensor_reader self-check OK")


if __name__ == "__main__":
    demo()
