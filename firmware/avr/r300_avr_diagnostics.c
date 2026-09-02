#include "r300_avr_diagnostics.h"

static void put_u16le(uint8_t *out, uint16_t value)
{
    out[0] = (uint8_t)value;
    out[1] = (uint8_t)(value >> 8);
}

static void put_u32le(uint8_t *out, uint32_t value)
{
    for (uint8_t index = 0u; index < 4u; ++index)
        out[index] = (uint8_t)(value >> (8u * index));
}

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
