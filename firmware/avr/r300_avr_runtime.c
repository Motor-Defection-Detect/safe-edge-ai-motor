#include <avr/interrupt.h>

#include "r300_avr_diagnostics.h"
#include "r300_avr_platform.h"
#include "r300_avr_runtime.h"

static void diagnostic_service(r300_avr_runtime_t *runtime, uint32_t now)
{
    if (runtime->diagnostic_bytes == 0u &&
        (uint32_t)(now - runtime->last_diagnostic_ms) >= R300_AVR_DIAGNOSTIC_PERIOD_MS) {
        if (r300_avr_diagnostic_pack(runtime->diagnostic_frame,
                                     runtime->diagnostic_sequence, now,
                                     runtime->safety.state, runtime->safety.trip_cause,
                                     runtime->diagnostic_flags)) {
            ++runtime->diagnostic_sequence;
            runtime->last_diagnostic_ms = now;
            runtime->diagnostic_index = 0u;
            runtime->diagnostic_bytes = R300_AVR_DIAGNOSTIC_BYTES;
        }
    }
    if (runtime->diagnostic_index < runtime->diagnostic_bytes &&
        r300_avr_diag_byte_write(runtime->diagnostic_frame[runtime->diagnostic_index])) {
        ++runtime->diagnostic_index;
        if (runtime->diagnostic_index == runtime->diagnostic_bytes)
            runtime->diagnostic_bytes = 0u;
    }
}

void r300_avr_runtime_init(r300_avr_runtime_t *runtime)
{
    uint32_t now;
    if (!runtime) return;
    *runtime = (r300_avr_runtime_t){0};
    r300_avr_platform_init();
    now = r300_avr_millis();
    r300_safety_init(&runtime->safety);
    r300_debounce_init(&runtime->arm, r300_avr_arm_pressed(), now);
    r300_debounce_init(&runtime->estop, r300_avr_estop_active(), now);
    runtime->heartbeat_edges = r300_avr_heartbeat_edges();
    r300_avr_set_relay(false);
    sei();
    r300_avr_watchdog_enable();
}

bool r300_avr_runtime_step(r300_avr_runtime_t *runtime, bool internal_fault)
{
    uint32_t now;
    uint8_t heartbeat_edges;
    bool arm_active;
    bool raw_arm;
    bool raw_estop;
    bool estop_active;
    bool relay;
    if (!runtime) return false;
    now = r300_avr_millis();
    heartbeat_edges = r300_avr_heartbeat_edges();
    if (heartbeat_edges != runtime->heartbeat_edges) {
        runtime->heartbeat_edges = heartbeat_edges;
        r300_heartbeat_edge(&runtime->safety, r300_avr_last_heartbeat_ms());
    }
    raw_arm = r300_avr_arm_pressed();
    (void)r300_debounce_update(&runtime->arm, raw_arm, now);
    /* Activation is debounced; any raw release removes relay permission immediately. */
    arm_active = raw_arm && runtime->arm.stable_pressed;
    raw_estop = r300_avr_estop_active();
    (void)r300_debounce_update(&runtime->estop, raw_estop, now);
    estop_active = raw_estop || runtime->estop.stable_pressed;
    relay = r300_safety_step(&runtime->safety, now, arm_active,
                             !runtime->arm.stable_pressed,
                             estop_active, internal_fault);
    runtime->diagnostic_flags = 0u;
    if (r300_heartbeat_healthy(&runtime->safety, now))
        runtime->diagnostic_flags |= R300_AVR_DIAGNOSTIC_HEARTBEAT_HEALTHY;
    if (arm_active) runtime->diagnostic_flags |= R300_AVR_DIAGNOSTIC_ARM_ACTIVE;
    if (estop_active) runtime->diagnostic_flags |= R300_AVR_DIAGNOSTIC_ESTOP_ACTIVE;
    if (!internal_fault) runtime->diagnostic_flags |= R300_AVR_DIAGNOSTIC_INTERNAL_HEALTHY;
    if (relay) runtime->diagnostic_flags |= R300_AVR_DIAGNOSTIC_RELAY_COMMAND;
    r300_avr_set_relay(relay);
    r300_avr_watchdog_kick();
    diagnostic_service(runtime, now);
    return relay;
}
