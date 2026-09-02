# GENERATED FILE - DO NOT EDIT.
# Source: contracts/r300-link.json
# Regenerate: python3 scripts/r300/link_contract.py generate
# r300-link-sha256:29f2f1859f8436d95040e46e33088260242a9141ccccad423b9ca28e59bb3bf2
"""Frame layout, CRC and timing shared by the Jetson receiver and the Qt dashboard."""
from __future__ import annotations

import struct
from typing import Final

CONTRACT_SHA256: Final = '29f2f1859f8436d95040e46e33088260242a9141ccccad423b9ca28e59bb3bf2'
PROTOCOL_VERSION: Final = 2
ENDIANNESS: Final = 'little'

SAMPLE_RATE_HZ: Final = 1000
CHANNELS: Final = ('vib_x', 'vib_y', 'vib_z', 'current')
CHANNEL_UNITS: Final = ('MPU-6050 +/-16 g signed count', 'MPU-6050 +/-16 g signed count', 'MPU-6050 +/-16 g signed count', 'mA after motor-off zero calibration')
CHANNEL_COUNT: Final = 4
BLOCK_SAMPLES: Final = 128
WINDOW_SAMPLES: Final = 512
HOP_SAMPLES: Final = 256

BAUD: Final = 921600
UART_FORMAT: Final = '8N1'
AVR_DIAGNOSTIC_BAUD: Final = 38400
AVR_DIAGNOSTIC_UART_FORMAT: Final = '8N1'

HEARTBEAT_TOGGLE_MS: Final = 100  # PB8 toggle period while the Jetson link and local acquisition are both healthy
HEARTBEAT_TIMEOUT_MS: Final = 500  # the AVR trips the relay when no heartbeat edge arrives within this window
JETSON_KEEPALIVE_TIMEOUT_MS: Final = 180  # one missed 100 ms HEALTH plus 80 ms jitter stops PB8; combined with the 500 ms AVR timeout this leaves 20 ms of the 700 ms target for relay response
JETSON_LINK_HEALTHY_MAX_AGE_MS: Final = 128  # one 128 ms TELEMETRY period may elapse before HEALTH clears LINK_HEALTHY
INFERENCE_WORKER_STALL_MS: Final = 100  # clear LINK_HEALTHY when one inference or presentation callback exceeds the application-liveness deadline
INFERENCE_P95_TARGET_MS: Final = 30  # target p95 model callback latency on the deployed Jetson
PACKET_TO_UI_P95_TARGET_MS: Final = 100  # target p95 from validated packet receipt to Qt render
CONDITION_WARNING_TARGET_MS: Final = 1500  # target from a persistent abnormal condition to advisory WARNING
UI_HEARTBEAT_TIMEOUT_MS: Final = 300  # clear LINK_HEALTHY when a bound Qt main loop misses three 100 ms refresh heartbeats
AVR_WATCHDOG_TIMEOUT_MS: Final = 500  # reset a stalled safety main loop early enough to preserve the 700 ms relay-off target
AVR_RESET_TO_SAFE_MS: Final = 700  # worst case reset to relay OFF budget
SUPERVISION_FAILURE_TO_RELAY_TARGET_MS: Final = 700  # target from Jetson or link supervision failure to physical relay OFF; physical response requires HIL measurement
FRAME_PERIOD_MS: Final = 128  # 128 samples at 1000 S/s
INPUT_DEBOUNCE_MS: Final = 20  # ARM and E-stop debounce window
AVR_DIAGNOSTIC_PERIOD_MS: Final = 100  # non-blocking safety diagnostic publication period
AVR_DIAGNOSTIC_TIMEOUT_MS: Final = 300  # maximum age for exposing AVR diagnostic status to Jetson
STM_DIAGNOSTIC_PERIOD_MS: Final = 1000  # read-only STM32 reliability counter publication period
STM_DIAGNOSTIC_TIMEOUT_MS: Final = 3000  # maximum age for presenting STM32 reliability counters
MOTOR_COMMAND_PERIOD_MS: Final = 100  # periodic refresh of the latest operator or experiment motor request
MOTOR_COMMAND_TIMEOUT_MS: Final = 300  # fail-safe PWM stop when no fresh valid motor request is received

CRC_NAME: Final = 'CRC-16/CCITT-FALSE'
CRC_POLYNOMIAL: Final = 0x1021
CRC_INIT: Final = 0xFFFF
CRC_XOROUT: Final = 0x0000
CRC_CHECK: Final = 0x29B1

TELEMETRY_SOF: Final = 0xAA55
TELEMETRY_HEADER_BYTES: Final = 14
TELEMETRY_TRAILER_BYTES: Final = 2
TELEMETRY_FIELDS: Final = ('sof', 'version_flags', 'sequence', 'timestamp_us', 'sample_count')
TELEMETRY_STRUCT: Final = struct.Struct('<HHIIH')

HEALTH_SOF: Final = 0xAA55
HEALTH_HEADER_BYTES: Final = 14
HEALTH_TRAILER_BYTES: Final = 2
HEALTH_FIELDS: Final = ('sof', 'version_flags', 'sequence', 'timestamp_ms', 'health_flags')
HEALTH_STRUCT: Final = struct.Struct('<HHIIH')
HEALTH_BYTES: Final = 16

MOTOR_COMMAND_SOF: Final = 0xC33C
MOTOR_COMMAND_HEADER_BYTES: Final = 16
MOTOR_COMMAND_TRAILER_BYTES: Final = 2
MOTOR_COMMAND_FIELDS: Final = ('sof', 'version_flags', 'sequence', 'timestamp_ms', 'duty_permille', 'direction')
MOTOR_COMMAND_STRUCT: Final = struct.Struct('<HHIIHH')
MOTOR_COMMAND_BYTES: Final = 18

STM_DIAGNOSTIC_SOF: Final = 0xD44D
STM_DIAGNOSTIC_HEADER_BYTES: Final = 82
STM_DIAGNOSTIC_TRAILER_BYTES: Final = 2
STM_DIAGNOSTIC_FIELDS: Final = ('sof', 'version_flags', 'sequence', 'timestamp_ms', 'telemetry_sequence', 'dropped_blocks', 'invalid_health_frames', 'invalid_health_crc_frames', 'sensor_overruns', 'platform_vibration_overruns', 'platform_adc_overruns', 'sensor_bus_errors', 'uart_errors', 'avr_uart_errors', 'adc_dma_errors', 'uart_dma_errors', 'adc_peripheral_errors', 'uart_busy_events', 'invalid_avr_diag_frames', 'invalid_motor_command_frames', 'current_zero_count', 'buffered_samples', 'status_flags')
STM_DIAGNOSTIC_STRUCT: Final = struct.Struct('<HHIIIIIIIIIIIIIIIIIIHHH')
STM_DIAGNOSTIC_BYTES: Final = 84

AVR_DIAGNOSTIC_SOF: Final = 0x5AA5
AVR_DIAGNOSTIC_HEADER_BYTES: Final = 14
AVR_DIAGNOSTIC_TRAILER_BYTES: Final = 2
AVR_DIAGNOSTIC_FIELDS: Final = ('sof', 'version_flags', 'sequence', 'timestamp_ms', 'state_cause')
AVR_DIAGNOSTIC_STRUCT: Final = struct.Struct('<HHIIH')
AVR_DIAGNOSTIC_BYTES: Final = 16

MOTOR_DIRECTIONS: Final = {
    'STOP': 0,
    'FORWARD': 1,
}

SAFETY_STATES: Final = {
    'DISARMED': 0,
    'ARMED': 1,
    'TRIPPED': 2,
}

TRIP_CAUSES: Final = {
    'NONE': 0,
    'HEARTBEAT_TIMEOUT': 1,
    'ESTOP': 2,
    'INTERNAL_FAULT': 3,
    'RESET': 4,
}

HEALTH_FLAGS: Final = {
    'APP_ALIVE': 1 << 0,
    'MODEL_LOADED': 1 << 1,
    'INFERENCE_OK': 1 << 2,
    'STORAGE_OK': 1 << 3,
    'LINK_HEALTHY': 1 << 4,
}

TELEMETRY_FLAGS: Final = {
    'SENSOR_READY': 1 << 0,
    'LOCAL_FAULT': 1 << 1,
    'MOTOR_ACTIVE': 1 << 2,
    'AVR_DIAG_VALID': 1 << 3,
    'AVR_ARMED': 1 << 4,
    'AVR_TRIPPED': 1 << 5,
    'AVR_ESTOP': 1 << 6,
    'AVR_HEARTBEAT_TIMEOUT': 1 << 7,
}

AVR_DIAGNOSTIC_FLAGS: Final = {
    'HEARTBEAT_HEALTHY': 1 << 0,
    'ARM_ACTIVE': 1 << 1,
    'ESTOP_ACTIVE': 1 << 2,
    'INTERNAL_HEALTHY': 1 << 3,
    'RELAY_COMMAND': 1 << 4,
}

STM_DIAGNOSTIC_FLAGS: Final = {
    'KEEPALIVE_VALID': 1 << 0,
    'ACQUISITION_OVERRUN': 1 << 1,
    'LOCAL_HEALTHY': 1 << 2,
    'HEARTBEAT_LEVEL': 1 << 3,
    'AVR_DIAG_VALID': 1 << 4,
    'AVR_HEARTBEAT_HEALTHY': 1 << 5,
    'AVR_ARM_ACTIVE': 1 << 6,
    'AVR_ESTOP_ACTIVE': 1 << 7,
    'AVR_INTERNAL_HEALTHY': 1 << 8,
    'AVR_RELAY_COMMAND': 1 << 9,
}


def crc16(data: bytes) -> int:
    """Bit-serial CRC matching r300_crc16() in the generated C headers."""
    reg = CRC_INIT
    for byte in data:
        reg ^= byte << 8
        for _ in range(8):
            reg = ((reg << 1) ^ CRC_POLYNOMIAL) & 0xFFFF if reg & 0x8000 else (reg << 1) & 0xFFFF
    return reg ^ CRC_XOROUT


def telemetry_frame_bytes(sample_count: int) -> int:
    """Total on-wire size of a TELEMETRY frame carrying `sample_count` samples."""
    return TELEMETRY_HEADER_BYTES + sample_count * CHANNEL_COUNT * 2 + TELEMETRY_TRAILER_BYTES

