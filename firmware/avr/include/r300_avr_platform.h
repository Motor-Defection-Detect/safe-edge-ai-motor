#ifndef R300_AVR_PLATFORM_H_
#define R300_AVR_PLATFORM_H_

#include <stdbool.h>
#include <stdint.h>

/* R300_AVR_CPU_HZ is a board calibration setting supplied by the build. */
void r300_avr_platform_init(void);
uint32_t r300_avr_millis(void);
uint32_t r300_avr_last_heartbeat_ms(void);
uint8_t r300_avr_heartbeat_edges(void);
bool r300_avr_arm_pressed(void);
bool r300_avr_estop_active(void);
void r300_avr_set_relay(bool energized);
bool r300_avr_diag_byte_write(uint8_t byte);
void r300_avr_watchdog_enable(void);
void r300_avr_watchdog_kick(void);

#endif
