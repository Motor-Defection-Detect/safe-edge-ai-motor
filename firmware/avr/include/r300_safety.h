#ifndef R300_SAFETY_H_
#define R300_SAFETY_H_

#include <stdbool.h>
#include <stdint.h>

#include "r300_link.h"

typedef struct {
    r300_safety_state_t state;
    r300_trip_cause_t trip_cause;
    uint32_t last_heartbeat_ms;
    bool heartbeat_seen;
    bool heartbeat_confirmed;
    bool arm_cycle_ready;
} r300_safety_t;

typedef struct {
    bool stable_pressed;
    bool candidate_pressed;
    uint32_t candidate_since_ms;
} r300_debounce_t;

void r300_safety_init(r300_safety_t *safety);
void r300_heartbeat_edge(r300_safety_t *safety, uint32_t now_ms);
bool r300_heartbeat_healthy(const r300_safety_t *safety, uint32_t now_ms);
void r300_debounce_init(r300_debounce_t *input, bool raw_pressed, uint32_t now_ms);
/* Returns true only when a raw level has been stable for INPUT_DEBOUNCE_MS. */
bool r300_debounce_update(r300_debounce_t *input, bool raw_pressed, uint32_t now_ms);
/*
 * arm_active drops immediately on a raw release; arm_released becomes true only
 * after that release is debounced. The return value is the PD7 relay command.
 */
bool r300_safety_step(r300_safety_t *safety, uint32_t now_ms, bool arm_active,
                      bool arm_released, bool estop_active, bool internal_fault);

#endif
