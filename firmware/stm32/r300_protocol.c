#include "r300_protocol.h"

static void put_u16le(uint8_t *out, uint16_t value)
{
    out[0] = (uint8_t)value;
    out[1] = (uint8_t)(value >> 8);
}

static void put_u32le(uint8_t *out, uint32_t value)
{
    for (uint8_t index = 0; index < 4u; ++index) out[index] = (uint8_t)(value >> (8u * index));
}

static uint16_t get_u16le(const uint8_t *in)
{
    return (uint16_t)in[0] | (uint16_t)((uint16_t)in[1] << 8);
}

static uint32_t get_u32le(const uint8_t *in)
{
    uint32_t value = 0u;
    for (uint8_t index = 0; index < 4u; ++index) value |= (uint32_t)in[index] << (8u * index);
    return value;
}

void r300_acquisition_reset(r300_acquisition_t *acquisition)
{
    if (!acquisition) return;
    acquisition->sample_count = 0u;
    acquisition->overrun = false;
}

bool r300_acquisition_append(r300_acquisition_t *acquisition, const int16_t sample[R300_CHANNELS])
{
    if (!acquisition || !sample || acquisition->overrun) return false;
    if (acquisition->sample_count == R300_BLOCK_SAMPLES) {
        acquisition->overrun = true;
        return false;
    }
    for (size_t channel = 0; channel < R300_CHANNELS; ++channel)
        acquisition->samples[(size_t)acquisition->sample_count * R300_CHANNELS + channel] = sample[channel];
    ++acquisition->sample_count;
    return true;
}

bool r300_telemetry_pack(uint8_t *out, size_t capacity, uint32_t sequence,
                         uint32_t timestamp_us, const int16_t *samples,
                         uint16_t sample_count, uint16_t flags, size_t *written)
{
    size_t bytes = R300_TELEMETRY_BYTES(sample_count);
    const uint16_t known_flags = R300_TELEMETRY_SENSOR_READY |
        R300_TELEMETRY_LOCAL_FAULT | R300_TELEMETRY_MOTOR_ACTIVE |
        R300_TELEMETRY_AVR_DIAG_VALID | R300_TELEMETRY_AVR_ARMED |
        R300_TELEMETRY_AVR_TRIPPED | R300_TELEMETRY_AVR_ESTOP |
        R300_TELEMETRY_AVR_HEARTBEAT_TIMEOUT;
    const uint16_t avr_state = flags &
        (R300_TELEMETRY_AVR_ARMED | R300_TELEMETRY_AVR_TRIPPED);
    const uint16_t avr_causes = flags &
        (R300_TELEMETRY_AVR_ESTOP | R300_TELEMETRY_AVR_HEARTBEAT_TIMEOUT);
    const uint16_t avr_bits = avr_state | avr_causes;
    if (!out || !samples || !written || sample_count == 0u || flags > 0xffu ||
        (flags & (uint16_t)~known_flags) != 0u ||
        avr_state == (R300_TELEMETRY_AVR_ARMED | R300_TELEMETRY_AVR_TRIPPED) ||
        (avr_bits != 0u && (flags & R300_TELEMETRY_AVR_DIAG_VALID) == 0u) ||
        (avr_causes != 0u && (flags & R300_TELEMETRY_AVR_TRIPPED) == 0u) ||
        avr_causes == (R300_TELEMETRY_AVR_ESTOP |
                       R300_TELEMETRY_AVR_HEARTBEAT_TIMEOUT) ||
        ((flags & R300_TELEMETRY_MOTOR_ACTIVE) != 0u &&
         ((flags & R300_TELEMETRY_SENSOR_READY) == 0u ||
          (flags & R300_TELEMETRY_LOCAL_FAULT) != 0u)) ||
        sample_count > R300_BLOCK_SAMPLES || capacity < bytes) return false;
    put_u16le(out + R300_TELEMETRY_OFFSET_SOF, R300_TELEMETRY_SOF);
    put_u16le(out + R300_TELEMETRY_OFFSET_VERSION_FLAGS,
              (uint16_t)(R300_PROTOCOL_VERSION | (uint16_t)(flags << 8)));
    put_u32le(out + R300_TELEMETRY_OFFSET_SEQUENCE, sequence);
    put_u32le(out + R300_TELEMETRY_OFFSET_TIMESTAMP_US, timestamp_us);
    put_u16le(out + R300_TELEMETRY_OFFSET_SAMPLE_COUNT, sample_count);
    for (size_t index = 0; index < (size_t)sample_count * R300_CHANNELS; ++index)
        put_u16le(out + R300_TELEMETRY_HEADER_BYTES + index * 2u, (uint16_t)samples[index]);
    put_u16le(out + bytes - R300_TELEMETRY_TRAILER_BYTES,
              r300_crc16(out, bytes - R300_TELEMETRY_TRAILER_BYTES));
    *written = bytes;
    return true;
}

bool r300_health_receive(r300_stm32_health_t *health, const uint8_t frame[R300_HEALTH_BYTES],
                         uint32_t received_at_ms)
{
    uint32_t sequence;
    uint32_t delta;
    uint16_t version_flags;
    uint16_t health_flags;
    const uint16_t known_health_flags = R300_HEALTH_APP_ALIVE | R300_HEALTH_MODEL_LOADED |
        R300_HEALTH_INFERENCE_OK | R300_HEALTH_STORAGE_OK | R300_HEALTH_LINK_HEALTHY;
    if (!health || !frame) return false;
    version_flags = get_u16le(frame + R300_HEALTH_OFFSET_VERSION_FLAGS);
    health_flags = get_u16le(frame + R300_HEALTH_OFFSET_HEALTH_FLAGS);
    if (get_u16le(frame + R300_HEALTH_OFFSET_SOF) != R300_HEALTH_SOF ||
        version_flags != R300_PROTOCOL_VERSION ||
        (health_flags & R300_HEALTH_APP_ALIVE) == 0u ||
        (health_flags & (uint16_t)~known_health_flags) != 0u ||
        r300_crc16(frame, R300_HEALTH_BYTES - R300_HEALTH_TRAILER_BYTES) !=
            get_u16le(frame + R300_HEALTH_BYTES - R300_HEALTH_TRAILER_BYTES)) return false;
    sequence = get_u32le(frame + R300_HEALTH_OFFSET_SEQUENCE);
    delta = sequence - health->last_keepalive_sequence;
    if (health->keepalive_valid && health->keepalive_sequence_seen &&
        (delta == 0u || delta >= 0x80000000u)) return false;
    health->last_keepalive_sequence = sequence;
    health->keepalive_sequence_seen = true;
    health->last_keepalive_ms = received_at_ms;
    health->keepalive_valid = true;
    health->link_healthy = (health_flags & R300_HEALTH_LINK_HEALTHY) != 0u;
    return true;
}

bool r300_heartbeat_step(r300_stm32_health_t *health, bool local_healthy, uint32_t now_ms)
{
    if (!health) return false;
    if ((uint32_t)(now_ms - health->last_keepalive_ms) > R300_JETSON_KEEPALIVE_TIMEOUT_MS) {
        health->keepalive_valid = false;
        health->link_healthy = false;
        health->keepalive_sequence_seen = false;
    }
    if (!local_healthy || !health->keepalive_valid || !health->link_healthy) {
        health->heartbeat_level = false;
        return false;
    }
    if ((uint32_t)(now_ms - health->last_heartbeat_toggle_ms) < R300_HEARTBEAT_TOGGLE_MS)
        return false;
    health->last_heartbeat_toggle_ms = now_ms;
    health->heartbeat_level = !health->heartbeat_level;
    return true;
}
