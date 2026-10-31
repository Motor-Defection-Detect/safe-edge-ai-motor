/*
 * STM32 건강 상태(r300_stm32_health_t)와 수집 블록(r300_acquisition_t) 구조체, TELEMETRY 패킹/HEALTH 수신/
 * HEARTBEAT 함수 선언. 구현은 firmware/stm32/r300_protocol.c.
 */
#ifndef R300_PROTOCOL_H_
#define R300_PROTOCOL_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

#include "r300_link.h"

typedef struct {
    /* Jetson HEALTH 마지막 수신 시각(ms) / HEARTBEAT 마지막 토글 시각(ms) / 마지막 HEALTH 시퀀스 */
    uint32_t last_keepalive_ms;
    uint32_t last_heartbeat_toggle_ms;
    uint32_t last_keepalive_sequence;
    bool keepalive_valid;
    bool link_healthy;
    bool keepalive_sequence_seen;
    /* PB8에 출력할 현재 레벨 */
    bool heartbeat_level;
} r300_stm32_health_t;

typedef struct {
    /* 샘플 단위로 [X, Y, Z, 전류(mA)] 4채널이 교차 저장된다 */
    int16_t samples[R300_BLOCK_SAMPLES * R300_CHANNELS];
    uint16_t sample_count;
    bool overrun;
} r300_acquisition_t;

/* Pack samples in the contract's little-endian TELEMETRY representation. */
bool r300_telemetry_pack(uint8_t *out, size_t capacity, uint32_t sequence,
                         uint32_t timestamp_us, const int16_t *samples,
                         uint16_t sample_count, uint16_t flags, size_t *written);

void r300_acquisition_reset(r300_acquisition_t *acquisition);
/* Append one synchronized vibration/current sample; false means the block is not usable. */
bool r300_acquisition_append(r300_acquisition_t *acquisition, const int16_t sample[R300_CHANNELS]);

/* Accept only a complete, versioned and CRC-valid HEALTH frame. */
bool r300_health_receive(r300_stm32_health_t *health, const uint8_t frame[R300_HEALTH_BYTES],
                         uint32_t received_at_ms);

/* Toggle at the contract interval only while local acquisition and Jetson health remain valid. */
bool r300_heartbeat_step(r300_stm32_health_t *health, bool local_healthy, uint32_t now_ms);

#endif
