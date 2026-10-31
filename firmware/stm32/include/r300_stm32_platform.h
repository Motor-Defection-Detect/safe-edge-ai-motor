/*
 * STM32 하드웨어 접근 계층 선언(초기화, 센서/ADC 폴링, UART 수신/송신, HEARTBEAT 핀, PWM, 시간).
 * 구현은 firmware/stm32/r300_stm32_platform.c. 로직 계층은 이 함수들로만 하드웨어에 접근한다.
 */
#ifndef R300_STM32_PLATFORM_H_
#define R300_STM32_PLATFORM_H_

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>

typedef struct {
    uint32_t vibration_overruns;
    uint32_t adc_overruns;
    uint32_t sensor_bus_errors;
    uint32_t uart_errors;
    uint32_t avr_uart_errors;
    uint32_t adc_dma_errors;
    uint32_t uart_dma_errors;
    uint32_t adc_peripheral_errors;
} r300_stm32_platform_diagnostics_t;

bool r300_stm32_platform_init(void);
bool r300_stm32_platform_healthy(void);
void r300_stm32_platform_diagnostics(r300_stm32_platform_diagnostics_t *diagnostics);
bool r300_stm32_vibration_poll(int16_t xyz[3], uint32_t *timestamp_us);
bool r300_stm32_current_poll(uint16_t *adc_count);
/* 아래 health_byte는 Jetson에서 오는 바이트(HEALTH, MOTOR_COMMAND)를 모두 꺼내는 함수 (이름은 HEALTH지만 둘 다 처리) */
bool r300_stm32_health_byte(uint8_t *byte);
bool r300_stm32_avr_diag_byte(uint8_t *byte);
bool r300_stm32_uart_tx_start(const uint8_t *data, size_t bytes);
bool r300_stm32_uart_tx_busy(void);
void r300_stm32_heartbeat_write(bool level);
void r300_stm32_pwm_write(uint16_t compare);
uint32_t r300_stm32_millis(void);

#endif
