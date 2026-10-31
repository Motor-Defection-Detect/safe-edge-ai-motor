/*
 * 보드/센서 보정 상수(클럭, MPU-6050 설정, 모터 PWM, ACS712 전류 센서, 타임아웃)와 변환 함수 선언.
 * 변환 함수 구현은 firmware/stm32/r300_board_config.c. 실측으로 확인해야 하는 값은 R300_HW_TUNE 주석으로 표시되어 있다.
 */
#ifndef R300_BOARD_CONFIG_H_
#define R300_BOARD_CONFIG_H_

#include <stdint.h>

/* STM32F411 HSI=16 MHz, PLLM=16, PLLN=192, PLLP=2, PLLQ=4. */
#define R300_SYSCLK_HZ 96000000u
#define R300_APB1_HZ 48000000u
#define R300_APB2_HZ 96000000u

/* GY-521 / MPU-6050: AD0=GND, 8 kHz base / (7+1) = 1 kHz, +/-16 g. */
#define R300_MPU6050_ADDRESS 0x68u
#define R300_MPU6050_WHO_AM_I 0x68u
#define R300_MPU6050_CONFIG 0x00u
#define R300_MPU6050_SMPLRT_DIV 0x07u
#define R300_MPU6050_ACCEL_CONFIG 0x18u

/* TIM4 receives a 96 MHz timer clock when APB1 is divided by two. */
/* Project baseline: LR7843 module accepts 3.3 V active-high input.
 * R300_HW_TUNE: raise 1 kHz only after measuring switching and temperature. */
#define R300_MOTOR_PWM_HZ 1000u
#define R300_MOTOR_PWM_PRESCALER 1u
#define R300_MOTOR_PWM_PERIOD_TICKS 48000u
#define R300_LR7843_PWM_ACTIVE_HIGH 1u
#define R300_MOTOR_INITIAL_DUTY_PERMILLE 0u

#define R300_ADC_FULL_SCALE_COUNTS 4095u
#define R300_ADC_REFERENCE_MV 3300u
/* Project baseline: ACS712-05B (5 A, 185 mV/A) with 12k/22k divider. */
#define R300_ACS712_SENSITIVITY_MV_PER_A 185u
#define R300_ACS712_DIVIDER_TOP_KOHM 12u
#define R300_ACS712_DIVIDER_BOTTOM_KOHM 22u
#define R300_ACS712_RANGE_MA 5000u
#define R300_CURRENT_ZERO_SAMPLES 256u
/* 100 ms permits scheduling jitter but detects either 1 kHz sensor stream loss. */
#define R300_SENSOR_STALE_US 100000u
/* Periodic WHO_AM_I read detects a disconnected or unresponsive MPU-6050. */
#define R300_SENSOR_IDENTITY_CHECK_US 100000u

/* Convert a calibrated ADC count delta to signed milliamps, rounded to nearest. */
int16_t r300_current_milliamps(uint16_t raw_count, uint16_t zero_count);
/* Convert 0..1000 permille to a TIM4 compare value; invalid inputs clamp safely. */
uint16_t r300_motor_pwm_compare(uint16_t duty_permille);

#endif
