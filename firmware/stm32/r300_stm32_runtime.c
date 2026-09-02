#include "r300_stm32_runtime.h"

static uint16_t get_u16le(const uint8_t *in)
{
    return (uint16_t)in[0] | (uint16_t)((uint16_t)in[1] << 8);
}

static uint32_t get_u32le(const uint8_t *in)
{
    uint32_t value = 0u;
    for (uint8_t index = 0u; index < 4u; ++index)
        value |= (uint32_t)in[index] << (8u * index);
    return value;
}

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

void r300_stm32_runtime_init(r300_stm32_runtime_t *runtime)
{
    if (!runtime) return;
    *runtime = (r300_stm32_runtime_t){0};
    r300_stm32_controller_init(&runtime->controller);
    runtime->pwm_compare = r300_motor_pwm_compare(R300_MOTOR_INITIAL_DUTY_PERMILLE);
}

bool r300_stm32_current_zero_sample(r300_stm32_runtime_t *runtime, uint16_t adc_count)
{
    if (!runtime || runtime->pwm_duty_permille != 0u ||
        runtime->current_zero_samples >= R300_CURRENT_ZERO_SAMPLES ||
        adc_count > R300_ADC_FULL_SCALE_COUNTS) return false;
    runtime->current_zero_sum += adc_count;
    ++runtime->current_zero_samples;
    if (runtime->current_zero_samples == R300_CURRENT_ZERO_SAMPLES) {
        runtime->current_zero_count = (uint16_t)((runtime->current_zero_sum +
            R300_CURRENT_ZERO_SAMPLES / 2u) / R300_CURRENT_ZERO_SAMPLES);
        runtime->current_calibrated = true;
    }
    return runtime->current_calibrated;
}

bool r300_stm32_vibration_isr(r300_stm32_runtime_t *runtime, const int16_t xyz[3],
                              uint32_t timestamp_us)
{
    if (!runtime || !xyz) return false;
    if (runtime->vibration_ready) {
        ++runtime->sensor_overruns;
        return false;
    }
    for (size_t axis = 0; axis < 3u; ++axis) runtime->vibration[axis] = xyz[axis];
    runtime->sample_timestamp_us = timestamp_us;
    runtime->vibration_ready = true;
    return true;
}

bool r300_stm32_current_dma(r300_stm32_runtime_t *runtime, uint16_t adc_count)
{
    if (!runtime || adc_count > R300_ADC_FULL_SCALE_COUNTS) return false;
    /* The ADC timer and MPU-6050 have independent clocks; retain the newest ADC sample. */
    runtime->current_adc = adc_count;
    runtime->current_seen = true;
    return true;
}

bool r300_stm32_commit_sample(r300_stm32_runtime_t *runtime)
{
    int16_t sample[R300_CHANNELS];
    if (!runtime || !runtime->sensor_ready || !runtime->current_calibrated ||
        !runtime->vibration_ready || !runtime->current_seen) return false;
    if (runtime->controller.acquisition.sample_count == 0u)
        runtime->block_timestamp_us = runtime->sample_timestamp_us;
    for (size_t axis = 0; axis < 3u; ++axis) sample[axis] = runtime->vibration[axis];
    sample[3] = r300_current_milliamps(runtime->current_adc, runtime->current_zero_count);
    runtime->vibration_ready = false;
    return r300_stm32_sample(&runtime->controller, sample);
}

bool r300_stm32_prepare_tx(r300_stm32_runtime_t *runtime, uint16_t flags)
{
    if (!runtime || runtime->tx_pending) {
        if (runtime) ++runtime->uart_busy_events;
        return false;
    }
    if (!r300_stm32_take_telemetry(&runtime->controller, runtime->tx_frame,
                                    sizeof(runtime->tx_frame), runtime->block_timestamp_us,
                                    flags, &runtime->tx_bytes)) return false;
    runtime->tx_pending = true;
    return true;
}

bool r300_stm32_prepare_diagnostic(
    r300_stm32_runtime_t *runtime, uint32_t now_ms,
    const r300_stm32_platform_diagnostics_t *platform_diagnostics)
{
    r300_stm32_diagnostics_t diagnostics;
    uint16_t status = 0u;
    uint16_t crc;
    if (!runtime || !platform_diagnostics || runtime->tx_pending) {
        if (runtime) ++runtime->uart_busy_events;
        return false;
    }
    r300_stm32_diagnostics(&runtime->controller, &diagnostics);
    if (diagnostics.keepalive_valid) status |= R300_STM_DIAGNOSTIC_KEEPALIVE_VALID;
    if (diagnostics.acquisition_overrun) status |= R300_STM_DIAGNOSTIC_ACQUISITION_OVERRUN;
    if (r300_stm32_local_healthy(runtime) &&
        platform_diagnostics->vibration_overruns == 0u &&
        platform_diagnostics->adc_overruns == 0u &&
        platform_diagnostics->sensor_bus_errors == 0u &&
        platform_diagnostics->uart_errors == 0u &&
        platform_diagnostics->adc_dma_errors == 0u &&
        platform_diagnostics->uart_dma_errors == 0u &&
        platform_diagnostics->adc_peripheral_errors == 0u)
        status |= R300_STM_DIAGNOSTIC_LOCAL_HEALTHY;
    if (runtime->controller.health.heartbeat_level)
        status |= R300_STM_DIAGNOSTIC_HEARTBEAT_LEVEL;
    if (runtime->avr_diag_valid) {
        status |= R300_STM_DIAGNOSTIC_AVR_DIAG_VALID;
        if (runtime->avr_diag_flags & R300_AVR_DIAGNOSTIC_HEARTBEAT_HEALTHY)
            status |= R300_STM_DIAGNOSTIC_AVR_HEARTBEAT_HEALTHY;
        if (runtime->avr_diag_flags & R300_AVR_DIAGNOSTIC_ARM_ACTIVE)
            status |= R300_STM_DIAGNOSTIC_AVR_ARM_ACTIVE;
        if (runtime->avr_diag_flags & R300_AVR_DIAGNOSTIC_ESTOP_ACTIVE)
            status |= R300_STM_DIAGNOSTIC_AVR_ESTOP_ACTIVE;
        if (runtime->avr_diag_flags & R300_AVR_DIAGNOSTIC_INTERNAL_HEALTHY)
            status |= R300_STM_DIAGNOSTIC_AVR_INTERNAL_HEALTHY;
        if (runtime->avr_diag_flags & R300_AVR_DIAGNOSTIC_RELAY_COMMAND)
            status |= R300_STM_DIAGNOSTIC_AVR_RELAY_COMMAND;
    }
    put_u16le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_SOF,
              R300_STM_DIAGNOSTIC_SOF);
    put_u16le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_VERSION_FLAGS,
              R300_PROTOCOL_VERSION);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_SEQUENCE,
              runtime->diagnostic_sequence++);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_TIMESTAMP_MS, now_ms);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_TELEMETRY_SEQUENCE,
              diagnostics.next_sequence);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_DROPPED_BLOCKS,
              diagnostics.dropped_blocks);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_INVALID_HEALTH_FRAMES,
              diagnostics.invalid_health_frames);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_INVALID_HEALTH_CRC_FRAMES,
              diagnostics.invalid_health_crc_frames);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_SENSOR_OVERRUNS,
              runtime->sensor_overruns);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_PLATFORM_VIBRATION_OVERRUNS,
              platform_diagnostics->vibration_overruns);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_PLATFORM_ADC_OVERRUNS,
              platform_diagnostics->adc_overruns);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_SENSOR_BUS_ERRORS,
              platform_diagnostics->sensor_bus_errors);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_UART_ERRORS,
              platform_diagnostics->uart_errors);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_AVR_UART_ERRORS,
              platform_diagnostics->avr_uart_errors);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_ADC_DMA_ERRORS,
              platform_diagnostics->adc_dma_errors);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_UART_DMA_ERRORS,
              platform_diagnostics->uart_dma_errors);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_ADC_PERIPHERAL_ERRORS,
              platform_diagnostics->adc_peripheral_errors);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_UART_BUSY_EVENTS,
              runtime->uart_busy_events);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_INVALID_AVR_DIAG_FRAMES,
              runtime->invalid_avr_diag_frames);
    put_u32le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_INVALID_MOTOR_COMMAND_FRAMES,
              runtime->invalid_motor_command_frames);
    put_u16le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_CURRENT_ZERO_COUNT,
              runtime->current_zero_count);
    put_u16le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_BUFFERED_SAMPLES,
              diagnostics.buffered_samples);
    put_u16le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_STATUS_FLAGS, status);
    crc = r300_crc16(runtime->tx_frame, R300_STM_DIAGNOSTIC_HEADER_BYTES);
    put_u16le(runtime->tx_frame + R300_STM_DIAGNOSTIC_OFFSET_CRC16, crc);
    runtime->tx_bytes = R300_STM_DIAGNOSTIC_BYTES;
    runtime->tx_pending = true;
    return true;
}

void r300_stm32_tx_complete(r300_stm32_runtime_t *runtime)
{
    if (!runtime) return;
    runtime->tx_pending = false;
    runtime->tx_bytes = 0u;
}

bool r300_stm32_set_motor(r300_stm32_runtime_t *runtime, uint16_t duty_permille)
{
    if (!runtime) return false;
    if (duty_permille > 1000u) duty_permille = 1000u;
    if (duty_permille != 0u && !r300_stm32_local_healthy(runtime)) return false;
    runtime->pwm_duty_permille = duty_permille;
    runtime->pwm_compare = r300_motor_pwm_compare(duty_permille);
    return true;
}

bool r300_stm32_local_healthy(const r300_stm32_runtime_t *runtime)
{
    return runtime && runtime->sensor_ready && runtime->current_calibrated &&
           !runtime->controller.acquisition.overrun && runtime->sensor_overruns == 0u;
}

uint16_t r300_stm32_telemetry_flags(const r300_stm32_runtime_t *runtime)
{
    uint16_t flags = 0u;
    if (!runtime) return R300_TELEMETRY_LOCAL_FAULT;
    if (runtime->sensor_ready) flags |= R300_TELEMETRY_SENSOR_READY;
    if (!r300_stm32_local_healthy(runtime)) flags |= R300_TELEMETRY_LOCAL_FAULT;
    if (runtime->pwm_duty_permille != 0u) flags |= R300_TELEMETRY_MOTOR_ACTIVE;
    if (runtime->avr_diag_valid) {
        flags |= R300_TELEMETRY_AVR_DIAG_VALID;
        if (runtime->avr_state == R300_SAFETY_ARMED) flags |= R300_TELEMETRY_AVR_ARMED;
        if (runtime->avr_state == R300_SAFETY_TRIPPED) flags |= R300_TELEMETRY_AVR_TRIPPED;
        if (runtime->avr_trip_cause == R300_TRIP_ESTOP) flags |= R300_TELEMETRY_AVR_ESTOP;
        if (runtime->avr_trip_cause == R300_TRIP_HEARTBEAT_TIMEOUT)
            flags |= R300_TELEMETRY_AVR_HEARTBEAT_TIMEOUT;
    }
    return flags;
}

bool r300_stm32_avr_diag_receive(r300_stm32_runtime_t *runtime,
                                 const uint8_t frame[R300_AVR_DIAGNOSTIC_BYTES],
                                 uint32_t received_at_ms)
{
    uint16_t state_cause;
    uint16_t version_flags;
    uint16_t flags;
    uint16_t state;
    uint16_t cause;
    uint32_t sequence;
    uint32_t delta;
    if (!runtime || !frame) return false;
    state_cause = get_u16le(frame + R300_AVR_DIAGNOSTIC_OFFSET_STATE_CAUSE);
    version_flags = get_u16le(frame + R300_AVR_DIAGNOSTIC_OFFSET_VERSION_FLAGS);
    flags = version_flags >> 8;
    state = state_cause & 0xffu;
    cause = state_cause >> 8;
    sequence = get_u32le(frame + R300_AVR_DIAGNOSTIC_OFFSET_SEQUENCE);
    delta = sequence - runtime->avr_diag_sequence;
    if (get_u16le(frame + R300_AVR_DIAGNOSTIC_OFFSET_SOF) != R300_AVR_DIAGNOSTIC_SOF ||
        (version_flags & 0xffu) != R300_PROTOCOL_VERSION ||
        (flags & ~(R300_AVR_DIAGNOSTIC_HEARTBEAT_HEALTHY |
                   R300_AVR_DIAGNOSTIC_ARM_ACTIVE | R300_AVR_DIAGNOSTIC_ESTOP_ACTIVE |
                   R300_AVR_DIAGNOSTIC_INTERNAL_HEALTHY |
                   R300_AVR_DIAGNOSTIC_RELAY_COMMAND)) ||
        state > R300_SAFETY_TRIPPED || cause > R300_TRIP_RESET ||
        (state == R300_SAFETY_ARMED && cause != R300_TRIP_NONE) ||
        (state == R300_SAFETY_TRIPPED && cause == R300_TRIP_NONE) ||
        (state == R300_SAFETY_DISARMED && cause != R300_TRIP_NONE &&
         cause != R300_TRIP_RESET) ||
        (state == R300_SAFETY_ARMED &&
         flags != (R300_AVR_DIAGNOSTIC_HEARTBEAT_HEALTHY |
                   R300_AVR_DIAGNOSTIC_ARM_ACTIVE |
                   R300_AVR_DIAGNOSTIC_INTERNAL_HEALTHY |
                   R300_AVR_DIAGNOSTIC_RELAY_COMMAND)) ||
        (flags & R300_AVR_DIAGNOSTIC_RELAY_COMMAND && state != R300_SAFETY_ARMED) ||
        (flags & R300_AVR_DIAGNOSTIC_ESTOP_ACTIVE &&
         (state != R300_SAFETY_TRIPPED || cause != R300_TRIP_ESTOP)) ||
        r300_crc16(frame, R300_AVR_DIAGNOSTIC_BYTES - R300_AVR_DIAGNOSTIC_TRAILER_BYTES) !=
            get_u16le(frame + R300_AVR_DIAGNOSTIC_OFFSET_CRC16) ||
        (runtime->avr_diag_sequence_seen && (delta == 0u || delta >= 0x80000000u))) {
        ++runtime->invalid_avr_diag_frames;
        return false;
    }
    runtime->avr_diag_sequence = sequence;
    runtime->avr_diag_timestamp_ms = get_u32le(frame + R300_AVR_DIAGNOSTIC_OFFSET_TIMESTAMP_MS);
    runtime->last_avr_diag_ms = received_at_ms;
    runtime->avr_state = (r300_safety_state_t)state;
    runtime->avr_trip_cause = (r300_trip_cause_t)cause;
    runtime->avr_diag_flags = flags;
    runtime->avr_diag_sequence_seen = true;
    runtime->avr_diag_valid = true;
    return true;
}

bool r300_stm32_avr_diag_fresh(r300_stm32_runtime_t *runtime, uint32_t now_ms)
{
    if (!runtime || !runtime->avr_diag_valid) return false;
    if ((uint32_t)(now_ms - runtime->last_avr_diag_ms) > R300_AVR_DIAGNOSTIC_TIMEOUT_MS) {
        runtime->avr_diag_valid = false;
        runtime->avr_diag_sequence_seen = false;
        runtime->avr_diag_flags = 0u;
    }
    return runtime->avr_diag_valid;
}

bool r300_stm32_motor_command_receive(r300_stm32_runtime_t *runtime,
                                      const uint8_t frame[R300_MOTOR_COMMAND_BYTES],
                                      uint32_t received_at_ms)
{
    uint16_t duty;
    uint16_t direction;
    uint32_t sequence;
    uint32_t delta;
    if (!runtime || !frame) return false;
    duty = get_u16le(frame + R300_MOTOR_COMMAND_OFFSET_DUTY_PERMILLE);
    direction = get_u16le(frame + R300_MOTOR_COMMAND_OFFSET_DIRECTION);
    sequence = get_u32le(frame + R300_MOTOR_COMMAND_OFFSET_SEQUENCE);
    delta = sequence - runtime->motor_command_sequence;
    if (get_u16le(frame + R300_MOTOR_COMMAND_OFFSET_SOF) != R300_MOTOR_COMMAND_SOF ||
        get_u16le(frame + R300_MOTOR_COMMAND_OFFSET_VERSION_FLAGS) != R300_PROTOCOL_VERSION ||
        duty > 1000u || direction > R300_MOTOR_FORWARD ||
        (direction == R300_MOTOR_STOP && duty != 0u) ||
        r300_crc16(frame, R300_MOTOR_COMMAND_BYTES - R300_MOTOR_COMMAND_TRAILER_BYTES) !=
            get_u16le(frame + R300_MOTOR_COMMAND_OFFSET_CRC16) ||
        (runtime->motor_command_sequence_seen && (delta == 0u || delta >= 0x80000000u))) {
        ++runtime->invalid_motor_command_frames;
        return false;
    }
    runtime->motor_command_sequence = sequence;
    runtime->last_motor_command_ms = received_at_ms;
    runtime->requested_duty_permille = duty;
    runtime->requested_direction = (r300_motor_direction_t)direction;
    runtime->motor_command_sequence_seen = true;
    runtime->motor_command_valid = true;
    return true;
}

bool r300_stm32_motor_service(r300_stm32_runtime_t *runtime, uint32_t now_ms)
{
    if (!runtime) return false;
    if (runtime->motor_command_valid &&
        (uint32_t)(now_ms - runtime->last_motor_command_ms) > R300_MOTOR_COMMAND_TIMEOUT_MS) {
        runtime->motor_command_valid = false;
        runtime->motor_command_sequence_seen = false;
    }
    if (!runtime->motor_command_valid || !runtime->controller.health.keepalive_valid ||
        !runtime->controller.health.link_healthy ||
        !r300_stm32_local_healthy(runtime) || runtime->requested_direction != R300_MOTOR_FORWARD)
        return r300_stm32_set_motor(runtime, 0u);
    return r300_stm32_set_motor(runtime, runtime->requested_duty_permille);
}

bool r300_stm32_rx_byte(r300_stm32_runtime_t *runtime, uint8_t byte, uint32_t now_ms)
{
    bool accepted;
    if (!runtime) return false;
    if (runtime->rx_bytes == 0u) {
        if (byte == 0x55u) runtime->rx_target = R300_HEALTH_BYTES;
        else if (byte == 0x3cu) runtime->rx_target = R300_MOTOR_COMMAND_BYTES;
        else return false;
        runtime->rx_frame[runtime->rx_bytes++] = byte;
        return false;
    }
    if (runtime->rx_bytes == 1u &&
        !((runtime->rx_frame[0] == 0x55u && byte == 0xaau) ||
          (runtime->rx_frame[0] == 0x3cu && byte == 0xc3u))) {
        runtime->rx_bytes = 0u;
        if (byte == 0x55u) runtime->rx_target = R300_HEALTH_BYTES;
        else if (byte == 0x3cu) runtime->rx_target = R300_MOTOR_COMMAND_BYTES;
        else return false;
    }
    runtime->rx_frame[runtime->rx_bytes++] = byte;
    if (runtime->rx_bytes != runtime->rx_target) return false;
    if (runtime->rx_target == R300_HEALTH_BYTES)
        accepted = r300_stm32_keepalive(&runtime->controller, runtime->rx_frame, now_ms);
    else
        accepted = r300_stm32_motor_command_receive(runtime, runtime->rx_frame, now_ms);
    runtime->rx_bytes = 0u;
    return accepted;
}
