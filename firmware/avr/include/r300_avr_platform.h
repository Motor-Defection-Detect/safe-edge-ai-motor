/*
 * AVR 하드웨어 접근 계층 선언(타이머, 입력 핀, 릴레이, 진단 UART, 워치독). 구현은
 * firmware/avr/r300_avr_platform.c. 위의 로직 계층(runtime/safety)은 이 함수들로만 하드웨어에 접근한다.
 */
#ifndef R300_AVR_PLATFORM_H_
#define R300_AVR_PLATFORM_H_

#include <stdbool.h>
#include <stdint.h>

/* R300_AVR_CPU_HZ is a board calibration setting supplied by the build. */
void r300_avr_platform_init(void);
uint32_t r300_avr_millis(void);
uint32_t r300_avr_last_heartbeat_ms(void);
/* 아래 함수는 PE4(HEARTBEAT) 엣지 누적 카운터(8비트, 한 바퀴 돌아도 '변화 여부'만 쓰임)를 반환 */
uint8_t r300_avr_heartbeat_edges(void);
bool r300_avr_arm_pressed(void);
bool r300_avr_estop_active(void);
void r300_avr_set_relay(bool energized);
bool r300_avr_diag_byte_write(uint8_t byte);
void r300_avr_watchdog_enable(void);
void r300_avr_watchdog_kick(void);

#endif
