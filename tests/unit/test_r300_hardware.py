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
