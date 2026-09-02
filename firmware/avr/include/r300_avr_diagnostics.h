#ifndef R300_AVR_DIAGNOSTICS_H_
#define R300_AVR_DIAGNOSTICS_H_

#include <stdbool.h>
#include <stdint.h>

#include "r300_link.h"

bool r300_avr_diagnostic_pack(uint8_t out[R300_AVR_DIAGNOSTIC_BYTES],
                              uint32_t sequence, uint32_t timestamp_ms,
                              r300_safety_state_t state, r300_trip_cause_t cause,
                              uint16_t flags);

#endif
