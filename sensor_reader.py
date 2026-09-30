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
import time
from pathlib import Path

logger = logging.getLogger("sensor-reader")

# ── Wiring / register map — edit to match your probe's datasheet ───────────
DE_PIN, RE_PIN = 24, 25  # BCM; MAX485 driver/receiver enable
SERIAL_PORT = "/dev/serial0"   # Pi hardware UART (GPIO14/15)
BAUDRATE = 4800
SLAVE_ADDRESS = 1

# (register offset, scale) — raw register value * scale = real units.
# Standard generic 7-in-1 probe register map; adjust if yours differs.
REGISTERS = {
    "moisture": (0, 0.1),   # %RH
    "ec":       (2, 1.0),   # uS/cm
    "ph":       (3, 0.1),
    "n":        (4, 1.0),   # mg/kg
    "p":        (5, 1.0),   # mg/kg
    "k":        (6, 1.0),   # mg/kg
}
REGISTER_COUNT = 7  # includes temperature at offset 1, which we don't report

SENSOR_LOG_PATH = Path(__file__).parent / "sensor_log.jsonl"


_instrument_cache = None


def _instrument():
    """Open the port once and reuse it; DE/RE flip around every write."""
    global _instrument_cache
    if _instrument_cache is None:
        import minimalmodbus
        from gpiozero import DigitalOutputDevice
        de, re_ = DigitalOutputDevice(DE_PIN), DigitalOutputDevice(RE_PIN)
        inst = minimalmodbus.Instrument(SERIAL_PORT, SLAVE_ADDRESS)
        inst.serial.baudrate = BAUDRATE
        inst.serial.timeout = 1
        raw_write = inst.serial.write

        def write(data):
            de.on(); re_.on()            # transmit, receiver off
            n = raw_write(data)
            inst.serial.flush()          # block until last byte is on the wire
            de.off(); re_.off()          # back to listening
            return n

        inst.serial.write = write
        _instrument_cache = inst
    return _instrument_cache


def read_sensors():
    """Read one set of soil values. Returns a dict, or None on failure."""
    if os.environ.get("SENSOR_MOCK") == "1":
        return _mock_reading()

    try:
        instrument = _instrument()
        registers = instrument.read_registers(0, REGISTER_COUNT, functioncode=3)
        return {name: round(registers[offset] * scale, 2) for name, (offset, scale) in REGISTERS.items()}
    except Exception as e:
        logger.warning(f"Sensor read failed: {e}")
        return None


def _mock_reading():
    # ponytail: fake data for testing the pipeline without hardware —
    # SENSOR_MOCK=1 env var only, real reads otherwise always hit the probe.
    import random
    return {
        "moisture": round(random.uniform(15, 45), 1),
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
    for key in ("moisture", "ec", "ph", "n", "p", "k"):
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

    print("sensor_reader self-check OK")


if __name__ == "__main__":
    demo()
