#include "r300_controller.h"

void r300_stm32_controller_init(r300_stm32_controller_t *controller)
{
    if (!controller) return;
    *controller = (r300_stm32_controller_t){0};
    r300_acquisition_reset(&controller->acquisition);
}

bool r300_stm32_sample(r300_stm32_controller_t *controller,
                       const int16_t sample[R300_CHANNELS])
{
    bool accepted;
    bool was_overrun;
    if (!controller) return false;
    was_overrun = controller->acquisition.overrun;
    accepted = r300_acquisition_append(&controller->acquisition, sample);
    if (!accepted && !was_overrun && controller->acquisition.overrun) ++controller->dropped_blocks;
    return accepted;
}

bool r300_stm32_take_telemetry(r300_stm32_controller_t *controller, uint8_t *out,
                               size_t capacity, uint32_t timestamp_us,
                               uint16_t flags, size_t *written)
{
    if (!controller || controller->acquisition.overrun ||
        controller->acquisition.sample_count != R300_BLOCK_SAMPLES) return false;
    if (!r300_telemetry_pack(out, capacity, controller->sequence, timestamp_us,
                             controller->acquisition.samples, controller->acquisition.sample_count,
                             flags, written)) return false;
    ++controller->sequence;
    r300_acquisition_reset(&controller->acquisition);
    return true;
}

bool r300_stm32_keepalive(r300_stm32_controller_t *controller,
                          const uint8_t frame[R300_HEALTH_BYTES], uint32_t received_at_ms)
{
    if (!controller) return false;
    if (r300_health_receive(&controller->health, frame, received_at_ms)) return true;
    if (frame && r300_crc16(frame, R300_HEALTH_BYTES - R300_HEALTH_TRAILER_BYTES) !=
        ((uint16_t)frame[R300_HEALTH_OFFSET_CRC16] |
         (uint16_t)((uint16_t)frame[R300_HEALTH_OFFSET_CRC16 + 1u] << 8)))
        ++controller->invalid_health_crc_frames;
    ++controller->invalid_health_frames;
    return false;
}

void r300_stm32_diagnostics(const r300_stm32_controller_t *controller,
                            r300_stm32_diagnostics_t *diagnostics)
{
    if (!controller || !diagnostics) return;
    diagnostics->next_sequence = controller->sequence;
    diagnostics->dropped_blocks = controller->dropped_blocks;
    diagnostics->invalid_health_frames = controller->invalid_health_frames;
    diagnostics->invalid_health_crc_frames = controller->invalid_health_crc_frames;
    diagnostics->buffered_samples = controller->acquisition.sample_count;
    diagnostics->keepalive_valid = controller->health.keepalive_valid &&
                                   controller->health.link_healthy;
    diagnostics->acquisition_overrun = controller->acquisition.overrun;
}

bool r300_stm32_heartbeat(r300_stm32_controller_t *controller, bool local_healthy,
                          uint32_t now_ms)
{
    return controller && r300_heartbeat_step(&controller->health, local_healthy, now_ms);
}
