"""
Motor driver for AgriRover — two L298N H-bridges: one drives the RC car's
two real motors (drive motor for forward/back, steering motor turning the
front wheels for left/right), the other drives the soil probe (up/down).

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

# ── Pin wiring (BCM numbering) ──────────────────────────────────────────────
# Movement L298N: only IN1-4 wired to the Pi, ENA/ENB left on the board's
# default jumpers (tied permanently on) — no enable pin needed here, gpiozero
# PWMs the IN pins directly for speed.
DRIVE_IN1, DRIVE_IN2 = 17, 27   # L298N channel A → drive motor
STEER_IN1, STEER_IN2 = 22, 23   # L298N channel B → steering motor
# ponytail: probe driver's enable pin placeholder — fill in with the real
# wiring if it differs.
PROBE_UP, PROBE_DOWN, PROBE_ENABLE = 5, 6, 19  # second L298N → probe motor

DEFAULT_SPEED = 0.8  # 0.0-1.0, applies to all three motors

drive = Motor(forward=DRIVE_IN1, backward=DRIVE_IN2, pwm=True)
steer = Motor(forward=STEER_IN1, backward=STEER_IN2, pwm=True)
probe = Motor(forward=PROBE_UP, backward=PROBE_DOWN, enable=PROBE_ENABLE, pwm=True)

# cmd -> motor + direction. forward/back drive the car; left/right turn the
# steering motor one way or the other (mechanical steering, not skid steer).
_COMMANDS = {
    "forward":    (drive, "forward"),
    "back":       (drive, "backward"),
    "left":       (steer, "forward"),
    "right":      (steer, "backward"),
    "drill_up":   (probe, "forward"),
    "drill_down": (probe, "backward"),
}


def handle_command(cmd, state):
    if cmd not in _COMMANDS:
        logger.warning(f"Unknown command: {cmd!r}")
        return

    motor, direction = _COMMANDS[cmd]
    if state == "start":
        getattr(motor, direction)(DEFAULT_SPEED)
    else:
        motor.stop()
    logger.info(f"CMD {cmd} {state}")


def stop_all():
    drive.stop()
    steer.stop()
    probe.stop()


def demo():
    """
    Runnable self-check. Needs the mock pin factory since this usually runs
    off-Pi: `GPIOZERO_PIN_FACTORY=mock python3 motor_driver.py`
    """
    for cmd, (motor, _) in _COMMANDS.items():
        handle_command(cmd, "start")
        assert motor.is_active, f"{cmd} start: motor should be active"
        handle_command(cmd, "stop")
        assert not motor.is_active, f"{cmd} stop: motor should be stopped"

    handle_command("forward", "start")
    stop_all()
    assert not drive.is_active and not steer.is_active and not probe.is_active

    handle_command("nonsense", "start")  # should warn, not raise

    print("motor_driver self-check OK")


if __name__ == "__main__":
    demo()
