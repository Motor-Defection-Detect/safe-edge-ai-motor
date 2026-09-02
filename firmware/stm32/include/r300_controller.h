#ifndef R300_CONTROLLER_H_
#define R300_CONTROLLER_H_

#include "r300_protocol.h"

typedef struct {
    r300_acquisition_t acquisition;
    r300_stm32_health_t health;
    uint32_t sequence;
    uint32_t dropped_blocks;
    uint32_t invalid_health_frames;
    uint32_t invalid_health_crc_frames;
} r300_stm32_controller_t;

typedef struct {
    uint32_t next_sequence;
    uint32_t dropped_blocks;
    uint32_t invalid_health_frames;
    uint32_t invalid_health_crc_frames;
    uint16_t buffered_samples;
    bool keepalive_valid;
    bool acquisition_overrun;
} r300_stm32_diagnostics_t;

void r300_stm32_controller_init(r300_stm32_controller_t *controller);
bool r300_stm32_sample(r300_stm32_controller_t *controller,
                       const int16_t sample[R300_CHANNELS]);
/* A full acquisition block is retained until it is packed successfully. */
bool r300_stm32_take_telemetry(r300_stm32_controller_t *controller, uint8_t *out,
                               size_t capacity, uint32_t timestamp_us,
                               uint16_t flags, size_t *written);
bool r300_stm32_keepalive(r300_stm32_controller_t *controller,
                          const uint8_t frame[R300_HEALTH_BYTES], uint32_t received_at_ms);
bool r300_stm32_heartbeat(r300_stm32_controller_t *controller, bool local_healthy,
                          uint32_t now_ms);
void r300_stm32_diagnostics(const r300_stm32_controller_t *controller,
                            r300_stm32_diagnostics_t *diagnostics);

#endif
