/*
 * 이 파일의 역할: 링크 프로토콜의 STM32 쪽 순수 로직 - 샘플 블록 버퍼, TELEMETRY 프레임 패킹(128샘플/1040바이트),
 * Jetson HEALTH 프레임 검증, HEARTBEAT(PB8) 토글 규칙. 모든 다중 바이트 필드는 리틀 엔디언.
 * 읽기 순서: 4/13
 * 다음에 볼 곳: firmware/stm32/r300_stm32_runtime.c — 이 로직 위에 MOTOR_COMMAND, AVR 진단, 수신 상태기계를 얹는 계층
 * 관련: docs/interface/r300-link.md(프레임 정의), firmware/avr/include/r300_link.h(생성 파일 - 수정 금지)
 */
#include "r300_protocol.h"

/* 16비트 값을 리틀 엔디언으로 기록한다. */
static void put_u16le(uint8_t *out, uint16_t value)
{
    out[0] = (uint8_t)value;
    out[1] = (uint8_t)(value >> 8);
}

/* 32비트 값을 리틀 엔디언으로 기록한다. */
static void put_u32le(uint8_t *out, uint32_t value)
{
    for (uint8_t index = 0; index < 4u; ++index) out[index] = (uint8_t)(value >> (8u * index));
}

/* 리틀 엔디언 16비트 값을 읽는다. */
static uint16_t get_u16le(const uint8_t *in)
{
    return (uint16_t)in[0] | (uint16_t)((uint16_t)in[1] << 8);
}

/* 리틀 엔디언 32비트 값을 읽는다. */
static uint32_t get_u32le(const uint8_t *in)
{
    uint32_t value = 0u;
    for (uint8_t index = 0; index < 4u; ++index) value |= (uint32_t)in[index] << (8u * index);
    return value;
}

/* 수집 블록을 비운다(샘플 수 0, overrun 해제). 블록 내용을 지우지는 않고 카운트만 되돌린다. */
void r300_acquisition_reset(r300_acquisition_t *acquisition)
{
    if (!acquisition) return;
    acquisition->sample_count = 0u;
    acquisition->overrun = false;
}

/*
 * 샘플 1개(4채널)를 블록 끝에 추가한다. 이미 128개가 찬 상태에서 추가하려 하면 overrun을 래치하고
 * false를 반환하며, overrun 상태에서는 reset 전까지 모든 추가가 실패한다.
 */
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

/*
 * 샘플 블록을 TELEMETRY 프레임(헤더 14 + 샘플 128*4채널*2바이트 + CRC 2 = 1040바이트)으로 직렬화한다.
 * status 플래그(8비트)가 모순되면(예: ARMED와 TRIPPED 동시, 진단 무효인데 AVR 비트 존재) 거부한다.
 * 플래그는 version 필드 상위 바이트에 실리고, CRC16은 CRC 필드를 제외한 전체에 대해 계산된다.
 */
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

/*
 * Jetson HEALTH 프레임을 받아들일지 검증한다: SOF, 버전, APP_ALIVE 비트, 알 수 없는 플래그 없음, CRC16.
 * 시퀀스는 직전 값보다 '앞서' 있어야 한다(같거나 뒤처진 재전송은 거부, uint32 wrap 허용).
 * 통과하면 수신 시각을 기록하고 LINK_HEALTHY 비트를 link_healthy에 반영한다.
 */
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

/*
 * HEARTBEAT(PB8) 레벨을 결정한다. Jetson HEALTH가 180 ms 넘게 오지 않으면 keepalive를 무효화한다.
 * 로컬 센서 건강, 유효한 HEALTH, LINK_HEALTHY가 모두 참일 때만 100 ms마다 레벨을 뒤집고,
 * 하나라도 어긋나면 레벨을 LOW로 고정(토글 중단)한다. AVR이 엣지 부재(500 ms)로 TRIP하는 근거가 된다.
 * 반환값 true = 이번 호출에서 토글이 일어남.
 */
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
