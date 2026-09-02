/* Boundary probe for the R300 link contract.
 *
 * Both firmware sides (STM32F411 and ATmega128A) consume the generated C header
 * while the Jetson receiver and the Qt dashboard consume the generated Python
 * module. This program prints the framing values the C side computes so the
 * Python side can assert they are identical. A silent disagreement here is the
 * exact defect the integration tier exists to catch.
 *
 * The header under test is selected with -I by the calling test.
 */
#include <stdio.h>
#include <string.h>
#include "r300_link.h"

int main(void)
{
    /* Published check value for the declared CRC parameters. */
    const char *check = "123456789";
    printf("check %04X\n", r300_crc16((const uint8_t *)check, strlen(check)));

    /* A full TELEMETRY header plus payload with deterministic content. */
    uint8_t frame[R300_TELEMETRY_HEADER_BYTES + 128u * R300_CHANNELS * 2u];
    memset(frame, 0, sizeof frame);
    frame[R300_TELEMETRY_OFFSET_SOF + 0] = (uint8_t)(R300_TELEMETRY_SOF & 0xFFu);
    frame[R300_TELEMETRY_OFFSET_SOF + 1] = (uint8_t)(R300_TELEMETRY_SOF >> 8);
    frame[R300_TELEMETRY_OFFSET_VERSION_FLAGS] = (uint8_t)R300_PROTOCOL_VERSION;
    frame[R300_TELEMETRY_OFFSET_SAMPLE_COUNT] = (uint8_t)(128u & 0xFFu);
    for (size_t i = R300_TELEMETRY_OFFSET_PAYLOAD; i < sizeof frame; ++i) {
        frame[i] = (uint8_t)(i * 7u);
    }
    printf("frame %04X\n", r300_crc16(frame, sizeof frame));

    printf("telemetry_bytes %zu\n", (size_t)R300_TELEMETRY_BYTES(128));
    printf("health_bytes %u\n", (unsigned)R300_HEALTH_BYTES);
    printf("avr_diagnostic_bytes %u\n", (unsigned)R300_AVR_DIAGNOSTIC_BYTES);
    printf("stm_diagnostic_bytes %u\n", (unsigned)R300_STM_DIAGNOSTIC_BYTES);
    printf("avr_diagnostic_baud %u\n", (unsigned)R300_AVR_DIAGNOSTIC_BAUD);
    printf("motor_command_bytes %u\n", (unsigned)R300_MOTOR_COMMAND_BYTES);
    printf("motor_command_timeout_ms %u\n", (unsigned)R300_MOTOR_COMMAND_TIMEOUT_MS);
    printf("motor_forward %u\n", (unsigned)R300_MOTOR_FORWARD);
    printf("heartbeat_toggle_ms %u\n", (unsigned)R300_HEARTBEAT_TOGGLE_MS);
    printf("heartbeat_timeout_ms %u\n", (unsigned)R300_HEARTBEAT_TIMEOUT_MS);
    printf("safety_tripped %u\n", (unsigned)R300_SAFETY_TRIPPED);
    printf("telemetry_local_fault %u\n", (unsigned)R300_TELEMETRY_LOCAL_FAULT);
    printf("telemetry_avr_diag_valid %u\n", (unsigned)R300_TELEMETRY_AVR_DIAG_VALID);
    printf("telemetry_avr_heartbeat_timeout %u\n",
           (unsigned)R300_TELEMETRY_AVR_HEARTBEAT_TIMEOUT);
    return 0;
}
