# [테스트 설명] 모터 요청이 명시적이고 상한과 시간 제한이 있는지, AVR 안전 증거가 완전해야 모터가 준비되는지(fail-closed) 지킨다.
# 대상 소스: edge/r300_hardware.py (읽기 순서 밖)
# 읽는 법: 각 test_ 함수 이름이 곧 보호하는 동작이다. 소스를 먼저 읽고 오면 이해가 쉽다.
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "edge"))

from r300_hardware import motor_ready, validate_motor_request


def test_no_avr_motor_request_is_explicit_capped_and_timed() -> None:
    validate_motor_request(0, 0.0, False)
    validate_motor_request(100, 5.0, True)
    for request in ((1, 1.0, False), (101, 1.0, True), (100, 0.0, True),
                    (100, 5.1, True), (0, 1.0, True)):
        with pytest.raises(ValueError):
            validate_motor_request(*request)


def test_motor_waits_for_complete_avr_safety_evidence() -> None:
    metrics = {name: 1.0 for name in (
        "serial_connected", "telemetry_sensor_ready", "stm_avr_live_valid",
        "stm_avr_heartbeat_healthy", "stm_avr_arm_active",
        "stm_avr_internal_healthy", "stm_avr_relay_command",
    )}
    assert motor_ready(metrics)
    assert not motor_ready({**metrics, "stm_avr_arm_active": 0.0})
    assert not motor_ready({**metrics, "stm_avr_estop_active": 1.0})
