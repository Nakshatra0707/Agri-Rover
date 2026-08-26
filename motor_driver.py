"""
Motor driver for AgriRover — two L298N H-bridges on GPIO (via gpiozero):
one drives the two skid-steer wheel motors, the other drives the soil
probe (up/down).

Wiring: edit the BCM pin constants below to match your board. Each L298N
channel needs two direction pins (IN1/IN2 or IN3/IN4) plus one PWM-capable
enable pin (ENA/ENB) for speed control — gpiozero.Motor drives all three
per motor and gives us forward()/backward()/stop() for free.

Usage from broadcaster.py:
    from motor_driver import handle_command
    handle_command("forward", "start")
    handle_command("forward", "stop")
"""

import logging
import os

from gpiozero import Motor

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
LEFT_DRIVE_FORWARD, LEFT_DRIVE_BACKWARD, LEFT_DRIVE_ENABLE = 17, 27, 12
RIGHT_DRIVE_FORWARD, RIGHT_DRIVE_BACKWARD, RIGHT_DRIVE_ENABLE = 22, 23, 13
PROBE_UP, PROBE_DOWN, PROBE_ENABLE = 5, 6, 19

DEFAULT_SPEED = 0.8  # 0.0-1.0, applies to drive motors only

left_drive = Motor(forward=LEFT_DRIVE_FORWARD, backward=LEFT_DRIVE_BACKWARD, enable=LEFT_DRIVE_ENABLE, pwm=True)
right_drive = Motor(forward=RIGHT_DRIVE_FORWARD, backward=RIGHT_DRIVE_BACKWARD, enable=RIGHT_DRIVE_ENABLE, pwm=True)
probe = Motor(forward=PROBE_UP, backward=PROBE_DOWN, enable=PROBE_ENABLE, pwm=True)

# cmd -> (motor, direction) for start; stop always stops both drive motors
# or the probe motor, whichever group the cmd belongs to.
_DRIVE_CMDS = {
    # tank-turn skid steer: left/right pivot the two sides in opposite
    # directions. ponytail: turn-in-place only, add a slower-side turn if
    # wide/rolling turns are needed later.
    "forward": lambda: (left_drive.forward(DEFAULT_SPEED), right_drive.forward(DEFAULT_SPEED)),
    "back":    lambda: (left_drive.backward(DEFAULT_SPEED), right_drive.backward(DEFAULT_SPEED)),
    "left":    lambda: (left_drive.backward(DEFAULT_SPEED), right_drive.forward(DEFAULT_SPEED)),
    "right":   lambda: (left_drive.forward(DEFAULT_SPEED), right_drive.backward(DEFAULT_SPEED)),
}
_PROBE_CMDS = {
    "drill_up":   lambda: probe.forward(DEFAULT_SPEED),
    "drill_down": lambda: probe.backward(DEFAULT_SPEED),
}


def handle_command(cmd, state):
    if cmd in _DRIVE_CMDS:
        if state == "start":
            _DRIVE_CMDS[cmd]()
        else:
            left_drive.stop()
            right_drive.stop()
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
    left_drive.stop()
    right_drive.stop()
    probe.stop()


def demo():
    """
    Runnable self-check. Needs the mock pin factory since this usually runs
    off-Pi: `GPIOZERO_PIN_FACTORY=mock python3 motor_driver.py`
    """
    for cmd in ["forward", "back", "left", "right"]:
        handle_command(cmd, "start")
        assert left_drive.is_active or right_drive.is_active, f"{cmd} start: drive motors should be active"
        handle_command(cmd, "stop")
        assert not left_drive.is_active and not right_drive.is_active, f"{cmd} stop: drive motors should be stopped"

    for cmd in ["drill_up", "drill_down"]:
        handle_command(cmd, "start")
        assert probe.is_active, f"{cmd} start: probe should be active"
        handle_command(cmd, "stop")
        assert not probe.is_active, f"{cmd} stop: probe should be stopped"

    handle_command("forward", "start")
    stop_all()
    assert not left_drive.is_active and not right_drive.is_active and not probe.is_active

    handle_command("nonsense", "start")  # should warn, not raise

    print("motor_driver self-check OK")


if __name__ == "__main__":
    demo()
