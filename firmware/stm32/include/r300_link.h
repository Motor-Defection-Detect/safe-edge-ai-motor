// GENERATED FILE - DO NOT EDIT.
// Source: contracts/r300-link.json
// Regenerate: python3 scripts/r300/link_contract.py generate
// r300-link-sha256:c6ef7e3e1452425cec56811fe875986c0f952a9920acc9c0dca19faccffeaf96

#ifndef R300_LINK_STM32_H_
#define R300_LINK_STM32_H_

#include <stdint.h>
#include <stddef.h>

#define R300_PROTOCOL_VERSION 2u

#define R300_AVR_DIAGNOSTIC_BAUD 38400u

/* Acquisition */
#define R300_SAMPLE_RATE_HZ 1000u
#define R300_CHANNELS 4u
/* Channel units: vib_x=MPU-6050 +/-16 g signed count, vib_y=MPU-6050 +/-16 g signed count, vib_z=MPU-6050 +/-16 g signed count, current=mA after motor-off zero calibration */
#define R300_BLOCK_SAMPLES 128u
#define R300_WINDOW_SAMPLES 512u
#define R300_HOP_SAMPLES 256u

/* Timing */
#define R300_HEARTBEAT_TOGGLE_MS 100u  /* PB8 toggle period while the Jetson link and local acquisition are both healthy */
#define R300_HEARTBEAT_TIMEOUT_MS 500u  /* the AVR trips the relay when no heartbeat edge arrives within this window */
#define R300_JETSON_KEEPALIVE_TIMEOUT_MS 180u  /* one missed 100 ms HEALTH plus 80 ms jitter stops PB8; combined with the 500 ms AVR timeout this leaves 20 ms of the 700 ms target for relay response */
#define R300_JETSON_LINK_HEALTHY_MAX_AGE_MS 128u  /* one 128 ms TELEMETRY period may elapse before HEALTH clears LINK_HEALTHY */
#define R300_INFERENCE_WORKER_STALL_MS 100u  /* clear LINK_HEALTHY when one inference or presentation callback exceeds the application-liveness deadline */
#define R300_INFERENCE_P95_TARGET_MS 30u  /* target p95 model callback latency on the deployed Jetson */
#define R300_PACKET_TO_UI_P95_TARGET_MS 100u  /* target p95 from validated packet receipt to Qt render */
#define R300_CONDITION_WARNING_TARGET_MS 1500u  /* target from a persistent abnormal condition to advisory WARNING */
#define R300_UI_HEARTBEAT_TIMEOUT_MS 300u  /* clear LINK_HEALTHY when a bound Qt main loop misses three 100 ms refresh heartbeats */
#define R300_AVR_WATCHDOG_TIMEOUT_MS 500u  /* reset a stalled safety main loop early enough to preserve the 700 ms relay-off target */
#define R300_AVR_RESET_TO_SAFE_MS 700u  /* worst case reset to relay OFF budget */
#define R300_SUPERVISION_FAILURE_TO_RELAY_TARGET_MS 700u  /* target from Jetson or link supervision failure to physical relay OFF; physical response requires HIL measurement */
#define R300_FRAME_PERIOD_MS 128u  /* 128 samples at 1000 S/s */
#define R300_INPUT_DEBOUNCE_MS 20u  /* ARM and E-stop debounce window */
#define R300_AVR_DIAGNOSTIC_PERIOD_MS 100u  /* non-blocking safety diagnostic publication period */
#define R300_AVR_DIAGNOSTIC_TIMEOUT_MS 300u  /* maximum age for exposing AVR diagnostic status to Jetson */
#define R300_STM_DIAGNOSTIC_PERIOD_MS 1000u  /* read-only STM32 reliability counter publication period */
#define R300_STM_DIAGNOSTIC_TIMEOUT_MS 3000u  /* maximum age for presenting STM32 reliability counters */
#define R300_MOTOR_COMMAND_PERIOD_MS 100u  /* periodic refresh of the latest operator or experiment motor request */
#define R300_MOTOR_COMMAND_TIMEOUT_MS 300u  /* fail-safe PWM stop when no fresh valid motor request is received */

/* CRC: CRC-16/CCITT-FALSE */
#define R300_CRC_POLYNOMIAL 0x1021u
#define R300_CRC_INIT 0xFFFFu
#define R300_CRC_XOROUT 0x0000u
#define R300_CRC_CHECK 0x29B1u

/* TELEMETRY frame: firmware-to-jetson */
#define R300_TELEMETRY_SOF 0xAA55u
#define R300_TELEMETRY_HEADER_BYTES 14u
#define R300_TELEMETRY_TRAILER_BYTES 2u
#define R300_TELEMETRY_OFFSET_SOF 0u
#define R300_TELEMETRY_OFFSET_VERSION_FLAGS 2u
#define R300_TELEMETRY_OFFSET_SEQUENCE 4u
#define R300_TELEMETRY_OFFSET_TIMESTAMP_US 8u
#define R300_TELEMETRY_OFFSET_SAMPLE_COUNT 12u
#define R300_TELEMETRY_OFFSET_PAYLOAD 14u
#define R300_TELEMETRY_BYTES(samples) (R300_TELEMETRY_HEADER_BYTES + (size_t)(samples) * R300_CHANNELS * 2u + R300_TELEMETRY_TRAILER_BYTES)
/* HEALTH frame: jetson-to-firmware */
#define R300_HEALTH_SOF 0xAA55u
#define R300_HEALTH_HEADER_BYTES 14u
#define R300_HEALTH_TRAILER_BYTES 2u
#define R300_HEALTH_OFFSET_SOF 0u
#define R300_HEALTH_OFFSET_VERSION_FLAGS 2u
#define R300_HEALTH_OFFSET_SEQUENCE 4u
#define R300_HEALTH_OFFSET_TIMESTAMP_MS 8u
#define R300_HEALTH_OFFSET_HEALTH_FLAGS 12u
#define R300_HEALTH_OFFSET_CRC16 14u
#define R300_HEALTH_BYTES 16u
/* MOTOR_COMMAND frame: jetson-to-firmware */
#define R300_MOTOR_COMMAND_SOF 0xC33Cu
#define R300_MOTOR_COMMAND_HEADER_BYTES 16u
#define R300_MOTOR_COMMAND_TRAILER_BYTES 2u
#define R300_MOTOR_COMMAND_OFFSET_SOF 0u
#define R300_MOTOR_COMMAND_OFFSET_VERSION_FLAGS 2u
#define R300_MOTOR_COMMAND_OFFSET_SEQUENCE 4u
#define R300_MOTOR_COMMAND_OFFSET_TIMESTAMP_MS 8u
#define R300_MOTOR_COMMAND_OFFSET_DUTY_PERMILLE 12u
#define R300_MOTOR_COMMAND_OFFSET_DIRECTION 14u
#define R300_MOTOR_COMMAND_OFFSET_CRC16 16u
#define R300_MOTOR_COMMAND_BYTES 18u
/* STM_DIAGNOSTIC frame: firmware-to-jetson */
#define R300_STM_DIAGNOSTIC_SOF 0xD44Du
#define R300_STM_DIAGNOSTIC_HEADER_BYTES 82u
#define R300_STM_DIAGNOSTIC_TRAILER_BYTES 2u
#define R300_STM_DIAGNOSTIC_OFFSET_SOF 0u
#define R300_STM_DIAGNOSTIC_OFFSET_VERSION_FLAGS 2u
#define R300_STM_DIAGNOSTIC_OFFSET_SEQUENCE 4u
#define R300_STM_DIAGNOSTIC_OFFSET_TIMESTAMP_MS 8u
#define R300_STM_DIAGNOSTIC_OFFSET_TELEMETRY_SEQUENCE 12u
#define R300_STM_DIAGNOSTIC_OFFSET_DROPPED_BLOCKS 16u
#define R300_STM_DIAGNOSTIC_OFFSET_INVALID_HEALTH_FRAMES 20u
#define R300_STM_DIAGNOSTIC_OFFSET_INVALID_HEALTH_CRC_FRAMES 24u
#define R300_STM_DIAGNOSTIC_OFFSET_SENSOR_OVERRUNS 28u
#define R300_STM_DIAGNOSTIC_OFFSET_PLATFORM_VIBRATION_OVERRUNS 32u
#define R300_STM_DIAGNOSTIC_OFFSET_PLATFORM_ADC_OVERRUNS 36u
#define R300_STM_DIAGNOSTIC_OFFSET_SENSOR_BUS_ERRORS 40u
#define R300_STM_DIAGNOSTIC_OFFSET_UART_ERRORS 44u
#define R300_STM_DIAGNOSTIC_OFFSET_AVR_UART_ERRORS 48u
#define R300_STM_DIAGNOSTIC_OFFSET_ADC_DMA_ERRORS 52u
#define R300_STM_DIAGNOSTIC_OFFSET_UART_DMA_ERRORS 56u
#define R300_STM_DIAGNOSTIC_OFFSET_ADC_PERIPHERAL_ERRORS 60u
#define R300_STM_DIAGNOSTIC_OFFSET_UART_BUSY_EVENTS 64u
#define R300_STM_DIAGNOSTIC_OFFSET_INVALID_AVR_DIAG_FRAMES 68u
#define R300_STM_DIAGNOSTIC_OFFSET_INVALID_MOTOR_COMMAND_FRAMES 72u
#define R300_STM_DIAGNOSTIC_OFFSET_CURRENT_ZERO_COUNT 76u
#define R300_STM_DIAGNOSTIC_OFFSET_BUFFERED_SAMPLES 78u
#define R300_STM_DIAGNOSTIC_OFFSET_STATUS_FLAGS 80u
#define R300_STM_DIAGNOSTIC_OFFSET_CRC16 82u
#define R300_STM_DIAGNOSTIC_BYTES 84u
/* AVR_DIAGNOSTIC frame: safety-to-firmware */
#define R300_AVR_DIAGNOSTIC_SOF 0x5AA5u
#define R300_AVR_DIAGNOSTIC_HEADER_BYTES 14u
#define R300_AVR_DIAGNOSTIC_TRAILER_BYTES 2u
#define R300_AVR_DIAGNOSTIC_OFFSET_SOF 0u
#define R300_AVR_DIAGNOSTIC_OFFSET_VERSION_FLAGS 2u
#define R300_AVR_DIAGNOSTIC_OFFSET_SEQUENCE 4u
#define R300_AVR_DIAGNOSTIC_OFFSET_TIMESTAMP_MS 8u
#define R300_AVR_DIAGNOSTIC_OFFSET_STATE_CAUSE 12u
#define R300_AVR_DIAGNOSTIC_OFFSET_CRC16 14u
#define R300_AVR_DIAGNOSTIC_BYTES 16u

/* Motor direction allowed by the one-direction LR7843 wiring */
typedef enum {
    R300_MOTOR_STOP = 0, /* both bridge inputs low */
    R300_MOTOR_FORWARD = 1, /* PB6 PWM with PB7 held low */
} r300_motor_direction_t;

/* Safety state machine (owned by the ATmega128A) */
typedef enum {
    R300_SAFETY_DISARMED = 0, /* relay off */
    R300_SAFETY_ARMED = 1, /* relay on */
    R300_SAFETY_TRIPPED = 2, /* relay off */
} r300_safety_state_t;

typedef enum {
    R300_TRIP_NONE = 0,
    R300_TRIP_HEARTBEAT_TIMEOUT = 1,
    R300_TRIP_ESTOP = 2,
    R300_TRIP_INTERNAL_FAULT = 3,
    R300_TRIP_RESET = 4,
} r300_trip_cause_t;

/* Jetson health flags carried in the HEALTH frame */
#define R300_HEALTH_APP_ALIVE (1u << 0u)
#define R300_HEALTH_MODEL_LOADED (1u << 1u)
#define R300_HEALTH_INFERENCE_OK (1u << 2u)
#define R300_HEALTH_STORAGE_OK (1u << 3u)
#define R300_HEALTH_LINK_HEALTHY (1u << 4u)

/* STM32 status flags carried in TELEMETRY version_flags bits 15:8 */
#define R300_TELEMETRY_SENSOR_READY (1u << 0u)
#define R300_TELEMETRY_LOCAL_FAULT (1u << 1u)
#define R300_TELEMETRY_MOTOR_ACTIVE (1u << 2u)
#define R300_TELEMETRY_AVR_DIAG_VALID (1u << 3u)
#define R300_TELEMETRY_AVR_ARMED (1u << 4u)
#define R300_TELEMETRY_AVR_TRIPPED (1u << 5u)
#define R300_TELEMETRY_AVR_ESTOP (1u << 6u)
#define R300_TELEMETRY_AVR_HEARTBEAT_TIMEOUT (1u << 7u)

/* ATmega128A live status carried in AVR_DIAGNOSTIC version_flags bits 15:8 */
#define R300_AVR_DIAGNOSTIC_HEARTBEAT_HEALTHY (1u << 0u)
#define R300_AVR_DIAGNOSTIC_ARM_ACTIVE (1u << 1u)
#define R300_AVR_DIAGNOSTIC_ESTOP_ACTIVE (1u << 2u)
#define R300_AVR_DIAGNOSTIC_INTERNAL_HEALTHY (1u << 3u)
#define R300_AVR_DIAGNOSTIC_RELAY_COMMAND (1u << 4u)

/* STM32 reliability status carried in STM_DIAGNOSTIC.status_flags */
#define R300_STM_DIAGNOSTIC_KEEPALIVE_VALID (1u << 0u)
#define R300_STM_DIAGNOSTIC_ACQUISITION_OVERRUN (1u << 1u)
#define R300_STM_DIAGNOSTIC_LOCAL_HEALTHY (1u << 2u)
#define R300_STM_DIAGNOSTIC_HEARTBEAT_LEVEL (1u << 3u)
#define R300_STM_DIAGNOSTIC_AVR_DIAG_VALID (1u << 4u)
#define R300_STM_DIAGNOSTIC_AVR_HEARTBEAT_HEALTHY (1u << 5u)
#define R300_STM_DIAGNOSTIC_AVR_ARM_ACTIVE (1u << 6u)
#define R300_STM_DIAGNOSTIC_AVR_ESTOP_ACTIVE (1u << 7u)
#define R300_STM_DIAGNOSTIC_AVR_INTERNAL_HEALTHY (1u << 8u)
#define R300_STM_DIAGNOSTIC_AVR_RELAY_COMMAND (1u << 9u)

/* Bit-serial CRC. No table, so it costs no SRAM on the ATmega128A. */
static inline uint16_t r300_crc16(const uint8_t *data, size_t length)
{
    uint16_t reg = R300_CRC_INIT;
    for (size_t i = 0; i < length; ++i) {
        reg ^= (uint16_t)((uint16_t)data[i] << 8);
        for (uint8_t bit = 0; bit < 8u; ++bit) {
            reg = (reg & 0x8000u) ? (uint16_t)((reg << 1) ^ R300_CRC_POLYNOMIAL)
                                  : (uint16_t)(reg << 1);
        }
    }
    return (uint16_t)(reg ^ R300_CRC_XOROUT);
}

#endif /* R300_LINK_STM32_H_ */
