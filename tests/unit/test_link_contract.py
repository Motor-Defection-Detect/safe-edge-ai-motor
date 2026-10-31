# [테스트 설명] 생성된 Python 계약 모듈의 불변식(프레임 레이아웃, 타이밍 순서)을 지킨다.
# 대상 소스: edge/r300_link/link_contract.py (scripts/r300/link_contract.py 가 생성)
# 읽는 법: 각 test_ 함수 이름이 곧 보호하는 동작이다. 소스를 먼저 읽고 오면 이해가 쉽다.
"""Host unit tier: invariants of the generated link-contract Python module.

These need no compiler and no hardware. They assert the properties the rest of
the system depends on, so a contract edit that breaks framing or the supervision
ordering fails at the cheapest tier instead of at the boundary or on the bench.

The C/Python boundary itself is checked one tier up, in tests/integration.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "edge"))

from r300_link import link_contract as lc  # noqa: E402


def test_crc_matches_published_check_value() -> None:
    """CRC-16/CCITT-FALSE is only pinned down if the standard check value holds."""
    assert lc.crc16(b"123456789") == lc.CRC_CHECK == 0x29B1


def test_generated_consumer_exposes_its_full_contract_digest() -> None:
    assert len(lc.CONTRACT_SHA256) == 64
    assert set(lc.CONTRACT_SHA256) <= set("0123456789abcdef")


def test_crc_is_order_sensitive() -> None:
    """A CRC that ignores byte order would not detect a reordered frame."""
    assert lc.crc16(b"\x01\x02") != lc.crc16(b"\x02\x01")


def test_empty_input_returns_init() -> None:
    assert lc.crc16(b"") == lc.CRC_INIT ^ lc.CRC_XOROUT


def test_supervision_timeouts_are_ordered() -> None:
    """The STM32 must stop the heartbeat before the AVR times out.

    If the AVR timeout were the shorter of the two, the relay would trip on its
    own schedule and the STM32's supervision of the Jetson link would never be
    the thing that caused the trip. The ordering is what makes the heartbeat
    supervised rather than decorative.
    """
    assert lc.JETSON_KEEPALIVE_TIMEOUT_MS < lc.HEARTBEAT_TIMEOUT_MS


def test_heartbeat_timeout_tolerates_one_late_edge() -> None:
    """A 100 ms toggle must not trip on a single missed edge."""
    assert lc.HEARTBEAT_TIMEOUT_MS > 2 * lc.HEARTBEAT_TOGGLE_MS


def test_avr_diagnostic_timeout_tolerates_two_missed_frames() -> None:
    assert lc.AVR_DIAGNOSTIC_TIMEOUT_MS >= 3 * lc.AVR_DIAGNOSTIC_PERIOD_MS


def test_stm_diagnostic_timeout_tolerates_two_missed_frames() -> None:
    assert lc.STM_DIAGNOSTIC_TIMEOUT_MS >= 3 * lc.STM_DIAGNOSTIC_PERIOD_MS


def test_motor_command_timeout_tolerates_two_missed_frames() -> None:
    assert lc.MOTOR_COMMAND_TIMEOUT_MS >= 3 * lc.MOTOR_COMMAND_PERIOD_MS


def test_application_liveness_deadline_exceeds_inference_target() -> None:
    assert lc.INFERENCE_P95_TARGET_MS < lc.INFERENCE_WORKER_STALL_MS
    assert lc.PACKET_TO_UI_P95_TARGET_MS <= lc.UI_HEARTBEAT_TIMEOUT_MS


def test_frame_period_follows_from_block_and_rate() -> None:
    assert lc.FRAME_PERIOD_MS == lc.BLOCK_SAMPLES * 1000 / lc.SAMPLE_RATE_HZ
    assert dict(zip(lc.CHANNELS, lc.CHANNEL_UNITS))["current"].startswith("mA")


def test_telemetry_frame_size() -> None:
    """14 B header + 128 samples x 4 ch x 2 B + 2 B CRC."""
    assert lc.telemetry_frame_bytes(128) == 14 + 128 * 4 * 2 + 2 == 1040


def test_frame_stream_fits_the_uart_budget() -> None:
    """The configured block stream must fit inside 921600 8N1 (92160 B/s)."""
    frames_per_second = lc.SAMPLE_RATE_HZ / lc.BLOCK_SAMPLES
    on_wire = (lc.telemetry_frame_bytes(lc.BLOCK_SAMPLES) * frames_per_second +
               lc.STM_DIAGNOSTIC_BYTES * 1000 / lc.STM_DIAGNOSTIC_PERIOD_MS)
    assert on_wire < lc.BAUD / 10


def test_health_frame_is_fixed_length() -> None:
    assert lc.HEALTH_BYTES == lc.HEALTH_HEADER_BYTES + lc.HEALTH_TRAILER_BYTES


def test_struct_layout_matches_declared_header_size() -> None:
    """struct.Struct must consume exactly the declared fixed header."""
    assert lc.TELEMETRY_STRUCT.size == lc.TELEMETRY_HEADER_BYTES
    assert lc.HEALTH_STRUCT.size == lc.HEALTH_HEADER_BYTES
    assert lc.AVR_DIAGNOSTIC_STRUCT.size == lc.AVR_DIAGNOSTIC_HEADER_BYTES
    assert lc.AVR_DIAGNOSTIC_BYTES == (lc.AVR_DIAGNOSTIC_HEADER_BYTES +
                                       lc.AVR_DIAGNOSTIC_TRAILER_BYTES)
    assert lc.STM_DIAGNOSTIC_STRUCT.size == lc.STM_DIAGNOSTIC_HEADER_BYTES
    assert lc.STM_DIAGNOSTIC_BYTES == (lc.STM_DIAGNOSTIC_HEADER_BYTES +
                                       lc.STM_DIAGNOSTIC_TRAILER_BYTES)
    assert lc.MOTOR_COMMAND_STRUCT.size == lc.MOTOR_COMMAND_HEADER_BYTES
    assert lc.MOTOR_COMMAND_BYTES == (lc.MOTOR_COMMAND_HEADER_BYTES +
                                      lc.MOTOR_COMMAND_TRAILER_BYTES)


def test_fft_window_and_hop_are_consistent() -> None:
    """Hop 256 over a 512 window is 50 percent overlap."""
    assert lc.HOP_SAMPLES * 2 == lc.WINDOW_SAMPLES
    assert lc.SAMPLE_RATE_HZ / lc.HOP_SAMPLES == 3.90625


def test_safe_states_leave_the_relay_off() -> None:
    assert lc.SAFETY_STATES["DISARMED"] == 0
    assert set(lc.SAFETY_STATES) == {"DISARMED", "ARMED", "TRIPPED"}
    assert lc.MOTOR_DIRECTIONS == {"STOP": 0, "FORWARD": 1}


def test_trip_causes_cover_every_documented_trip() -> None:
    assert {"HEARTBEAT_TIMEOUT", "ESTOP", "INTERNAL_FAULT", "RESET"} <= set(lc.TRIP_CAUSES)


def test_health_flags_are_distinct_bits() -> None:
    values = list(lc.HEALTH_FLAGS.values())
    assert set(lc.HEALTH_FLAGS) == {
        "APP_ALIVE", "MODEL_LOADED", "INFERENCE_OK", "STORAGE_OK", "LINK_HEALTHY"}
    assert len(set(values)) == len(values)
    assert all(value & (value - 1) == 0 for value in values)
    assert lc.JETSON_LINK_HEALTHY_MAX_AGE_MS <= 2 * lc.FRAME_PERIOD_MS
    assert lc.JETSON_LINK_HEALTHY_MAX_AGE_MS < lc.JETSON_KEEPALIVE_TIMEOUT_MS
    assert lc.HEARTBEAT_TOGGLE_MS < lc.JETSON_KEEPALIVE_TIMEOUT_MS < 2 * lc.HEARTBEAT_TOGGLE_MS
    assert (lc.JETSON_KEEPALIVE_TIMEOUT_MS + lc.HEARTBEAT_TIMEOUT_MS <
            lc.SUPERVISION_FAILURE_TO_RELAY_TARGET_MS)


def test_telemetry_flags_are_distinct_one_byte_bits() -> None:
    values = list(lc.TELEMETRY_FLAGS.values())
    assert set(lc.TELEMETRY_FLAGS) == {
        "SENSOR_READY", "LOCAL_FAULT", "MOTOR_ACTIVE", "AVR_DIAG_VALID",
        "AVR_ARMED", "AVR_TRIPPED", "AVR_ESTOP", "AVR_HEARTBEAT_TIMEOUT"}
    assert len(set(values)) == len(values)
    assert all(value & (value - 1) == 0 and value <= 0x80 for value in values)


def test_stm_diagnostic_flags_are_distinct_bits() -> None:
    values = list(lc.STM_DIAGNOSTIC_FLAGS.values())
    assert set(lc.STM_DIAGNOSTIC_FLAGS) == {
        "KEEPALIVE_VALID", "ACQUISITION_OVERRUN", "LOCAL_HEALTHY", "HEARTBEAT_LEVEL",
        "AVR_DIAG_VALID", "AVR_HEARTBEAT_HEALTHY", "AVR_ARM_ACTIVE",
        "AVR_ESTOP_ACTIVE", "AVR_INTERNAL_HEALTHY", "AVR_RELAY_COMMAND"}
    assert len(set(values)) == len(values)
    assert all(value & (value - 1) == 0 for value in values)


def test_avr_diagnostic_flags_are_distinct_one_byte_bits() -> None:
    values = list(lc.AVR_DIAGNOSTIC_FLAGS.values())
    assert set(lc.AVR_DIAGNOSTIC_FLAGS) == {
        "HEARTBEAT_HEALTHY", "ARM_ACTIVE", "ESTOP_ACTIVE", "INTERNAL_HEALTHY",
        "RELAY_COMMAND"}
    assert len(set(values)) == len(values)
    assert all(value & (value - 1) == 0 and value <= 0x80 for value in values)
