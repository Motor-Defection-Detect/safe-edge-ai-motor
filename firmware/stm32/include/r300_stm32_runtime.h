#ifndef R300_STM32_RUNTIME_H_
#define R300_STM32_RUNTIME_H_

#include "r300_board_config.h"
#include "r300_controller.h"
#include "r300_stm32_platform.h"

typedef struct {
    r300_stm32_controller_t controller;
    uint8_t tx_frame[R300_TELEMETRY_BYTES(R300_BLOCK_SAMPLES)];
    uint8_t rx_frame[R300_MOTOR_COMMAND_BYTES];
    size_t tx_bytes;
    uint32_t sample_timestamp_us;
    uint32_t block_timestamp_us;
    uint32_t current_zero_sum;
    uint32_t sensor_overruns;
    uint32_t uart_busy_events;
    uint32_t avr_diag_sequence;
    uint32_t avr_diag_timestamp_ms;
    uint32_t last_avr_diag_ms;
    uint32_t invalid_avr_diag_frames;
    uint32_t motor_command_sequence;
    uint32_t last_motor_command_ms;
    uint32_t invalid_motor_command_frames;
    uint32_t diagnostic_sequence;
    uint16_t current_zero_count;
    uint16_t current_zero_samples;
    uint16_t pwm_duty_permille;
    uint16_t pwm_compare;
    uint16_t requested_duty_permille;
    uint16_t avr_diag_flags;
    uint16_t rx_bytes;
    uint16_t rx_target;
    int16_t vibration[3];
    uint16_t current_adc;
    bool vibration_ready;
    bool current_seen;
    bool current_calibrated;
    bool sensor_ready;
    bool tx_pending;
    bool avr_diag_valid;
    bool avr_diag_sequence_seen;
    bool motor_command_valid;
    bool motor_command_sequence_seen;
    r300_safety_state_t avr_state;
    r300_trip_cause_t avr_trip_cause;
    r300_motor_direction_t requested_direction;
} r300_stm32_runtime_t;

void r300_stm32_runtime_init(r300_stm32_runtime_t *runtime);
bool r300_stm32_current_zero_sample(r300_stm32_runtime_t *runtime, uint16_t adc_count);
bool r300_stm32_vibration_isr(r300_stm32_runtime_t *runtime, const int16_t xyz[3],
                              uint32_t timestamp_us);
bool r300_stm32_current_dma(r300_stm32_runtime_t *runtime, uint16_t adc_count);
bool r300_stm32_commit_sample(r300_stm32_runtime_t *runtime);
bool r300_stm32_prepare_tx(r300_stm32_runtime_t *runtime, uint16_t flags);
bool r300_stm32_prepare_diagnostic(
    r300_stm32_runtime_t *runtime, uint32_t now_ms,
    const r300_stm32_platform_diagnostics_t *platform_diagnostics);
void r300_stm32_tx_complete(r300_stm32_runtime_t *runtime);
bool r300_stm32_set_motor(r300_stm32_runtime_t *runtime, uint16_t duty_permille);
bool r300_stm32_local_healthy(const r300_stm32_runtime_t *runtime);
uint16_t r300_stm32_telemetry_flags(const r300_stm32_runtime_t *runtime);
bool r300_stm32_avr_diag_receive(r300_stm32_runtime_t *runtime,
                                 const uint8_t frame[R300_AVR_DIAGNOSTIC_BYTES],
                                 uint32_t received_at_ms);
bool r300_stm32_avr_diag_fresh(r300_stm32_runtime_t *runtime, uint32_t now_ms);
bool r300_stm32_motor_command_receive(r300_stm32_runtime_t *runtime,
                                      const uint8_t frame[R300_MOTOR_COMMAND_BYTES],
                                      uint32_t received_at_ms);
bool r300_stm32_motor_service(r300_stm32_runtime_t *runtime, uint32_t now_ms);
bool r300_stm32_rx_byte(r300_stm32_runtime_t *runtime, uint8_t byte, uint32_t now_ms);

#endif
