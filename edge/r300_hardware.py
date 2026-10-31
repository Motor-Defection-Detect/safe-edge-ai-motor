# ==============================================================================
# edge/r300_hardware.py  (보조 파일)
# 역할: edge/r300_demo.py 의 실장비 버전. 합성 포트 대신 실제 STM32 UART(open_serial_port)에 붙어 대시보드를 띄운다.
#       모델은 없다(항상 NORMAL, 신뢰도 0.0 자리표시자이며 model_loaded=False). 선택적으로 제한된 모터 PWM 시험을 한다.
# 읽기 순서: 보조 파일 - 13/13 의 edge/r300_demo.py 와 짝(합성 대신 실제 포트)
# 다음에 볼 곳: edge/r300_demo.py — 같은 구성을 합성 입력으로 돌리는 버전과 비교
# 관련: edge/r300_application.py, edge/r300_receiver.py(open_serial_port)
# 핵심 설계: 모터 시험은 여러 겹으로 제한한다 - 듀티 최대 100 permille(10%), 최대 5초, --allow-motor-test 플래그 필수,
#            AVR 이 이미 ARMED 임을 '관찰'한 뒤에만 시작(motor_ready), 종료 시 반드시 0. Jetson 은 AVR 을 ARM 시키지 않는다.
# ==============================================================================

"""Run the Qt dashboard against a real STM32 UART for bench commissioning."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path
from time import sleep

from r300_application import R300Application
from r300_link import InferenceResult
from r300_receiver import open_serial_port


# 시운전 PWM 상한: 100 permille = 10%.
MAX_COMMISSIONING_DUTY_PERMILLE = 100
# 시운전 모터 동작 시간 상한(초). 이 시간이 지나면 자동으로 PWM 0.
MAX_COMMISSIONING_MOTOR_SECONDS = 5.0


# 인자 조합 검증: 듀티는 0..100. 듀티가 0 이 아니면 --allow-motor-test 와 0 < 초 <= 5 가 필수이고,
# 듀티가 0 인데 초가 있으면 오류다. 틀리면 ValueError(main 에서 parser.error 로 종료).
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


# [구간 지도] main()
#  1) 인자 파싱과 모터 요청 검증(잘못되면 장치를 열기 전에 종료)
#  2) 실제 포트 열기, 모델 없는 R300Application 구성(로그는 build/hardware/ 아래), Qt 창 바인딩, 시작
#  3) 모터 시험이 요청되면 100ms 타이머로 motor_ready 를 기다렸다가 PWM 시작, 지정 시간 뒤 자동 0
#  4) 종료(finally): PWM 0 요청 -> 0.15초 대기 -> 정지 -> 포트 닫기
# Qt 관련 import 는 인자 검증 뒤에(지연 import) 한다.
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
        # 100ms 마다 호출된다. motor_ready 가 처음 참이 될 때 타이머를 멈추고 PWM 을 시작하며, 지정한 시간이 지나면 자동으로 0 을 요청한다.
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
        # PWM 0 요청이 100ms 송신 주기에 한 번은 실려 나가도록 잠깐 기다린 뒤 정지한다.
        sleep(0.15)
        runtime.stop()
        port.close()


# 필수 메트릭이 모두 참이고 E-stop 이 비활성일 때만 True: 시리얼 연결, 센서 준비, AVR 진단 유효, AVR 하트비트 정상 /
# ARM 활성 / 내부 정상 / 릴레이 명령 활성. 즉 AVR 이 이미 ARMED 인 것을 읽어 확인한 뒤에만 PWM 을 요청한다.
def motor_ready(metrics) -> bool:
    required = ("serial_connected", "telemetry_sensor_ready", "stm_avr_live_valid",
                "stm_avr_heartbeat_healthy", "stm_avr_arm_active",
                "stm_avr_internal_healthy", "stm_avr_relay_command")
    return all(metrics.get(name, 0.0) for name in required) and not metrics.get(
        "stm_avr_estop_active", 0.0
    )


if __name__ == "__main__":
    raise SystemExit(main())
