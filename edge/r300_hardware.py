"""Run the Qt dashboard against a real STM32 UART for bench commissioning."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from time import sleep

from r300_application import R300Application
from r300_link import InferenceResult
from r300_receiver import open_serial_port


MAX_COMMISSIONING_DUTY_PERMILLE = 100
MAX_COMMISSIONING_MOTOR_SECONDS = 5.0


def validate_motor_request(duty_permille: int, motor_seconds: float, allowed: bool) -> None:
    if not 0 <= duty_permille <= MAX_COMMISSIONING_DUTY_PERMILLE:
        raise ValueError(f"commissioning duty must be 0..{MAX_COMMISSIONING_DUTY_PERMILLE} permille")
    if duty_permille and not allowed:
        raise ValueError("non-zero PWM requires --allow-motor-test")
    if duty_permille and not 0.0 < motor_seconds <= MAX_COMMISSIONING_MOTOR_SECONDS:
        raise ValueError(
            f"non-zero PWM requires --motor-seconds within (0, {MAX_COMMISSIONING_MOTOR_SECONDS}]"
        )
    if not duty_permille and motor_seconds:
        raise ValueError("--motor-seconds requires non-zero --duty-permille")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", required=True, help="STM32 USART2 port, for example COM5")
    parser.add_argument("--duty-permille", type=int, default=0, help="bench PWM, maximum 100 = 10%%")
    parser.add_argument("--motor-seconds", type=float, default=0.0, help="automatic PWM stop, maximum 5 s")
    parser.add_argument("--allow-motor-test", action="store_true",
                        help="allow capped PWM only after live AVR safety evidence")
    args = parser.parse_args()
    try:
        validate_motor_request(args.duty_permille, args.motor_seconds,
                               args.allow_motor_test)
    except ValueError as error:
        parser.error(str(error))

    from PyQt5.QtCore import QTimer
    from PyQt5.QtWidgets import QApplication
    from r300_dashboard import R300Dashboard

    port = open_serial_port(args.port)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    runtime = R300Application(
        port,
        lambda _vibration, _current: InferenceResult("NORMAL", 0.0),
        Path("build/hardware") / f"r300-stm32-{stamp}.jsonl",
        model_loaded=lambda: False,
        condition="commissioning",
        sensor_config="mpu6050_1000hz_acs712_1000hz",
        notes="STM32/AVR bench commissioning; model-free placeholder output",
    )
    qt = QApplication([])
    window = R300Dashboard()
    window.setWindowTitle("R300 STM32/AVR Commissioning - MODEL-FREE")
    runtime.bind_dashboard(window)
    runtime.start()
    motor_timer = QTimer()
    if args.duty_permille:
        def start_motor_when_safe() -> None:
            if not motor_ready(runtime.store.snapshot().metrics):
                return
            motor_timer.stop()
            runtime.set_motor_duty(args.duty_permille)
            QTimer.singleShot(round(args.motor_seconds * 1000), lambda: runtime.set_motor_duty(0))

        motor_timer.timeout.connect(start_motor_when_safe)
        motor_timer.start(100)
    window.show()
    try:
        return qt.exec_()
    finally:
        runtime.set_motor_duty(0)
        sleep(0.15)
        runtime.stop()
        port.close()


def motor_ready(metrics) -> bool:
    required = ("serial_connected", "telemetry_sensor_ready", "stm_avr_live_valid",
                "stm_avr_heartbeat_healthy", "stm_avr_arm_active",
                "stm_avr_internal_healthy", "stm_avr_relay_command")
    return all(metrics.get(name, 0.0) for name in required) and not metrics.get(
        "stm_avr_estop_active", 0.0
    )


if __name__ == "__main__":
    raise SystemExit(main())
