#ifndef R300_AVR_RUNTIME_H_
#define R300_AVR_RUNTIME_H_

#include <stdbool.h>
#include <stdint.h>

#include "r300_safety.h"

typedef struct {
    r300_safety_t safety;
    r300_debounce_t arm;
    r300_debounce_t estop;
    uint8_t heartbeat_edges;
    uint8_t diagnostic_frame[R300_AVR_DIAGNOSTIC_BYTES];
    uint8_t diagnostic_index;
    uint8_t diagnostic_bytes;
    uint32_t diagnostic_sequence;
    uint32_t last_diagnostic_ms;
    uint16_t diagnostic_flags;
} r300_avr_runtime_t;

void r300_avr_runtime_init(r300_avr_runtime_t *runtime);
bool r300_avr_runtime_step(r300_avr_runtime_t *runtime, bool internal_fault);

#endif
