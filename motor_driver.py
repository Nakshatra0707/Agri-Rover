"""
Motor driver for AgriRover — one driver board for movement (separate
forward/backward/left/right pins) and one L298N-style H-bridge for the
soil probe (up/down).

Wiring: edit the BCM pin constants below to match your board. Each drive
pin is a single PWM-capable output — driving it high/PWM moves that
direction, off stops it. The probe motor still uses two direction pins
plus an enable pin (gpiozero.Motor) since it's a plain H-bridge.

Usage from broadcaster.py:
    from motor_driver import handle_command
    handle_command("forward", "start")
    handle_command("forward", "stop")
"""

import logging
import os

from gpiozero import Motor, PWMOutputDevice

logger = logging.getLogger("motor-driver")

if os.environ.get("GPIOZERO_PIN_FACTORY") == "mock":
    # ponytail: gpiozero's mock pin factory needs a PWM-capable pin class
    # explicitly (plain GPIOZERO_PIN_FACTORY=mock doesn't support PWM) —
    # only relevant for the off-Pi self-check below.
    from gpiozero import Device
    from gpiozero.pins.mock import MockFactory, MockPWMPin
    Device.pin_factory = MockFactory(pin_class=MockPWMPin)

# ── Pin wiring (BCM numbering) — edit these to match your wiring ───────────
# ponytail: placeholder pin numbers, fill in with the real wiring before
# running on hardware. Nothing else in this file needs to change.
DRIVE_FORWARD, DRIVE_BACKWARD, DRIVE_LEFT, DRIVE_RIGHT = 17, 27, 22, 23
PROBE_UP, PROBE_DOWN, PROBE_ENABLE = 5, 6, 19

DEFAULT_SPEED = 0.8  # 0.0-1.0, applies to drive pins only

_DRIVE_PINS = {
    "forward": PWMOutputDevice(DRIVE_FORWARD),
    "back":    PWMOutputDevice(DRIVE_BACKWARD),
    "left":    PWMOutputDevice(DRIVE_LEFT),
    "right":   PWMOutputDevice(DRIVE_RIGHT),
}
probe = Motor(forward=PROBE_UP, backward=PROBE_DOWN, enable=PROBE_ENABLE, pwm=True)

_PROBE_CMDS = {
    "drill_up":   lambda: probe.forward(DEFAULT_SPEED),
    "drill_down": lambda: probe.backward(DEFAULT_SPEED),
}


def handle_command(cmd, state):
    if cmd in _DRIVE_PINS:
        _DRIVE_PINS[cmd].value = DEFAULT_SPEED if state == "start" else 0
    elif cmd in _PROBE_CMDS:
        if state == "start":
            _PROBE_CMDS[cmd]()
        else:
            probe.stop()
    else:
        logger.warning(f"Unknown command: {cmd!r}")
        return
    logger.info(f"CMD {cmd} {state}")


def stop_all():
    for pin in _DRIVE_PINS.values():
        pin.off()
    probe.stop()


def demo():
    """
    Runnable self-check. Needs the mock pin factory since this usually runs
    off-Pi: `GPIOZERO_PIN_FACTORY=mock python3 motor_driver.py`
    """
    for cmd in ["forward", "back", "left", "right"]:
        handle_command(cmd, "start")
        assert _DRIVE_PINS[cmd].is_active, f"{cmd} start: pin should be active"
        handle_command(cmd, "stop")
        assert not _DRIVE_PINS[cmd].is_active, f"{cmd} stop: pin should be off"

    for cmd in ["drill_up", "drill_down"]:
        handle_command(cmd, "start")
        assert probe.is_active, f"{cmd} start: probe should be active"
        handle_command(cmd, "stop")
        assert not probe.is_active, f"{cmd} stop: probe should be stopped"

    handle_command("forward", "start")
    stop_all()
    assert not any(p.is_active for p in _DRIVE_PINS.values()) and not probe.is_active

    handle_command("nonsense", "start")  # should warn, not raise

    print("motor_driver self-check OK")


if __name__ == "__main__":
    demo()
