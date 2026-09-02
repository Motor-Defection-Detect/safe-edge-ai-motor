#include "r300_safety.h"

void r300_safety_init(r300_safety_t *safety)
{
    if (!safety) return;
    safety->state = R300_SAFETY_DISARMED;
    safety->trip_cause = R300_TRIP_RESET;
    safety->last_heartbeat_ms = 0u;
    safety->heartbeat_seen = false;
    safety->heartbeat_confirmed = false;
    safety->arm_cycle_ready = false;
}

void r300_heartbeat_edge(r300_safety_t *safety, uint32_t now_ms)
{
    if (!safety) return;
    safety->heartbeat_confirmed = safety->heartbeat_seen &&
        (uint32_t)(now_ms - safety->last_heartbeat_ms) < R300_HEARTBEAT_TIMEOUT_MS;
    safety->last_heartbeat_ms = now_ms;
    safety->heartbeat_seen = true;
}

bool r300_heartbeat_healthy(const r300_safety_t *safety, uint32_t now_ms)
{
    return safety && safety->heartbeat_confirmed &&
        (uint32_t)(now_ms - safety->last_heartbeat_ms) < R300_HEARTBEAT_TIMEOUT_MS;
}

void r300_debounce_init(r300_debounce_t *input, bool raw_pressed, uint32_t now_ms)
{
    if (!input) return;
    input->stable_pressed = raw_pressed;
    input->candidate_pressed = raw_pressed;
    input->candidate_since_ms = now_ms;
}

bool r300_debounce_update(r300_debounce_t *input, bool raw_pressed, uint32_t now_ms)
{
    if (!input) return false;
    if (raw_pressed != input->candidate_pressed) {
        input->candidate_pressed = raw_pressed;
        input->candidate_since_ms = now_ms;
        return false;
    }
    if (input->stable_pressed == raw_pressed ||
        (uint32_t)(now_ms - input->candidate_since_ms) < R300_INPUT_DEBOUNCE_MS) return false;
    input->stable_pressed = raw_pressed;
    return true;
}

bool r300_safety_step(r300_safety_t *safety, uint32_t now_ms, bool arm_active,
                      bool arm_released, bool estop_active, bool internal_fault)
{
    bool arm_request;
    bool heartbeat_ok;
    if (!safety) return false;
    heartbeat_ok = r300_heartbeat_healthy(safety, now_ms);
    arm_request = arm_active && safety->arm_cycle_ready;
    if (!arm_active) {
        if (arm_released) safety->arm_cycle_ready = true;
        if (safety->state == R300_SAFETY_ARMED) {
            safety->state = R300_SAFETY_DISARMED;
            safety->trip_cause = R300_TRIP_NONE;
        }
    } else if (arm_request) {
        /* Consume every press; a rejected attempt needs another release/press cycle. */
        safety->arm_cycle_ready = false;
    }
    if (safety->state != R300_SAFETY_TRIPPED) {
        if (estop_active) {
            safety->state = R300_SAFETY_TRIPPED;
            safety->trip_cause = R300_TRIP_ESTOP;
        } else if (internal_fault) {
            safety->state = R300_SAFETY_TRIPPED;
            safety->trip_cause = R300_TRIP_INTERNAL_FAULT;
        } else if (safety->state == R300_SAFETY_ARMED && !heartbeat_ok) {
            safety->state = R300_SAFETY_TRIPPED;
            safety->trip_cause = R300_TRIP_HEARTBEAT_TIMEOUT;
        }
    }
    if (arm_request && heartbeat_ok && !estop_active && !internal_fault) {
        /* A deliberate ARM request is the only exit from a latched trip. */
        safety->state = R300_SAFETY_ARMED;
        safety->trip_cause = R300_TRIP_NONE;
    }
    return safety->state == R300_SAFETY_ARMED;
}
