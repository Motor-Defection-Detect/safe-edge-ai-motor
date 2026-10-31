/*
 * 이 파일의 역할: AVR이 STM32로 보내는 16바이트 진단 프레임(SOF 0x5AA5, 상태/원인/플래그, CRC16)을 만든다.
 * STM32가 Safety 패널 표시용으로 읽기 전용으로 사용하며, 릴레이 권한과는 무관하다.
 * 읽기 순서: 보조 파일 (2/13 firmware/avr/r300_avr_runtime.c 를 먼저 볼 것)
 * 관련: firmware/stm32/r300_stm32_runtime.c의 r300_stm32_avr_diag_receive (수신/검증 쪽)
 */
#include "r300_avr_diagnostics.h"

/* 16비트 값을 리틀 엔디언(낮은 바이트 먼저)으로 기록한다. 링크 프로토콜은 모두 리틀 엔디언이다. */
static void put_u16le(uint8_t *out, uint16_t value)
{
    out[0] = (uint8_t)value;
    out[1] = (uint8_t)(value >> 8);
}

/* 32비트 값을 리틀 엔디언으로 기록한다. */
static void put_u32le(uint8_t *out, uint32_t value)
{
    for (uint8_t index = 0u; index < 4u; ++index)
        out[index] = (uint8_t)(value >> (8u * index));
}

/*
 * 상태/원인/플래그가 서로 모순되지 않을 때만 16바이트 프레임을 만들고 true를 반환한다.
 * (예: ARMED인데 원인이 있거나, 릴레이 플래그가 켜졌는데 ARMED가 아니면 거부.)
 * STM32 수신 쪽도 같은 규칙으로 다시 검증하므로 두 구현이 어긋나지 않게 유지해야 한다.
 * version/flags 필드의 상위 바이트에 플래그를 싣는다. CRC는 CRC 필드를 제외한 앞부분 전체에 대해 계산한다.
 */
bool r300_avr_diagnostic_pack(uint8_t out[R300_AVR_DIAGNOSTIC_BYTES],
                              uint32_t sequence, uint32_t timestamp_ms,
                              r300_safety_state_t state, r300_trip_cause_t cause,
                              uint16_t flags)
{
    const uint16_t known_flags = R300_AVR_DIAGNOSTIC_HEARTBEAT_HEALTHY |
        R300_AVR_DIAGNOSTIC_ARM_ACTIVE | R300_AVR_DIAGNOSTIC_ESTOP_ACTIVE |
        R300_AVR_DIAGNOSTIC_INTERNAL_HEALTHY | R300_AVR_DIAGNOSTIC_RELAY_COMMAND;
    const uint16_t armed_flags = R300_AVR_DIAGNOSTIC_HEARTBEAT_HEALTHY |
        R300_AVR_DIAGNOSTIC_ARM_ACTIVE | R300_AVR_DIAGNOSTIC_INTERNAL_HEALTHY |
        R300_AVR_DIAGNOSTIC_RELAY_COMMAND;
    uint16_t crc;
    if (!out || (unsigned)state > R300_SAFETY_TRIPPED ||
        (unsigned)cause > R300_TRIP_RESET ||
        (flags & ~known_flags) ||
        (state == R300_SAFETY_ARMED && cause != R300_TRIP_NONE) ||
        (state == R300_SAFETY_TRIPPED && cause == R300_TRIP_NONE) ||
        (state == R300_SAFETY_DISARMED && cause != R300_TRIP_NONE &&
         cause != R300_TRIP_RESET) ||
        (state == R300_SAFETY_ARMED &&
         (flags != armed_flags)) ||
        (flags & R300_AVR_DIAGNOSTIC_RELAY_COMMAND && state != R300_SAFETY_ARMED) ||
        (flags & R300_AVR_DIAGNOSTIC_ESTOP_ACTIVE &&
         (state != R300_SAFETY_TRIPPED || cause != R300_TRIP_ESTOP))) return false;
    put_u16le(out + R300_AVR_DIAGNOSTIC_OFFSET_SOF, R300_AVR_DIAGNOSTIC_SOF);
    put_u16le(out + R300_AVR_DIAGNOSTIC_OFFSET_VERSION_FLAGS,
              R300_PROTOCOL_VERSION | (uint16_t)(flags << 8));
    put_u32le(out + R300_AVR_DIAGNOSTIC_OFFSET_SEQUENCE, sequence);
    put_u32le(out + R300_AVR_DIAGNOSTIC_OFFSET_TIMESTAMP_MS, timestamp_ms);
    put_u16le(out + R300_AVR_DIAGNOSTIC_OFFSET_STATE_CAUSE,
              (uint16_t)state | (uint16_t)((uint16_t)cause << 8));
    crc = r300_crc16(out, R300_AVR_DIAGNOSTIC_BYTES - R300_AVR_DIAGNOSTIC_TRAILER_BYTES);
    put_u16le(out + R300_AVR_DIAGNOSTIC_OFFSET_CRC16, crc);
    return true;
}
