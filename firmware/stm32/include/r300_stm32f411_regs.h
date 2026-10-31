/*
 * STM32F411 주변장치 레지스터 구조체와 베이스 주소 매크로(CMSIS 스타일 최소 집합). HAL 의존성을 없애기 위해
 * 이 프로젝트에서 쓰는 레지스터만 정의한다. 사용처는 firmware/stm32/r300_stm32_platform.c. 값은 RM0383 기준이며
 * 파일 끝의 _Static_assert가 구조체 레이아웃(오프셋)이 맞는지 컴파일 시 검사한다.
 */
#ifndef R300_STM32F411_REGS_H_
#define R300_STM32F411_REGS_H_

#include <stddef.h>
#include <stdint.h>

/* Minimal CMSIS-style STM32F411 register view used by this firmware. */
typedef struct {
    volatile uint32_t CR, PLLCFGR, CFGR, CIR;
    volatile uint32_t AHB1RSTR, AHB2RSTR, reserved0[2];
    volatile uint32_t APB1RSTR, APB2RSTR, reserved1[2];
    volatile uint32_t AHB1ENR, AHB2ENR, reserved2[2];
    volatile uint32_t APB1ENR, APB2ENR;
} r300_rcc_regs_t;

typedef struct { volatile uint32_t ACR; } r300_flash_regs_t;
typedef struct { volatile uint32_t CR, CSR; } r300_pwr_regs_t;

typedef struct {
    volatile uint32_t MODER, OTYPER, OSPEEDR, PUPDR, IDR, ODR, BSRR, LCKR, AFR[2];
} r300_gpio_regs_t;

typedef struct {
    volatile uint32_t CR1, CR2, SMCR, DIER, SR, EGR, CCMR1, CCMR2;
    volatile uint32_t CCER, CNT, PSC, ARR, RCR, CCR1;
} r300_tim_regs_t;

typedef struct { volatile uint32_t SR, DR, BRR, CR1, CR2, CR3; } r300_usart_regs_t;
typedef struct { volatile uint32_t CR1, CR2, SR, DR; } r300_spi_regs_t;
typedef struct {
    volatile uint32_t CR1, CR2, OAR1, OAR2, DR, SR1, SR2, CCR, TRISE, FLTR;
} r300_i2c_regs_t;

typedef struct {
    volatile uint32_t SR, CR1, CR2, SMPR1, SMPR2, JOFR[4], HTR, LTR;
    volatile uint32_t SQR1, SQR2, SQR3, JSQR, JDR[4], DR;
} r300_adc_regs_t;

typedef struct { volatile uint32_t CSR, CCR, CDR; } r300_adc_common_regs_t;

typedef struct {
    volatile uint32_t CR, NDTR, PAR, M0AR, M1AR, FCR;
} r300_dma_stream_regs_t;

typedef struct {
    volatile uint32_t LISR, HISR, LIFCR, HIFCR;
    r300_dma_stream_regs_t STREAM[8];
} r300_dma_regs_t;

typedef struct { volatile uint32_t MEMRMP, PMC, EXTICR[4]; } r300_syscfg_regs_t;
typedef struct { volatile uint32_t IMR, EMR, RTSR, FTSR, SWIER, PR; } r300_exti_regs_t;
typedef struct { volatile uint32_t ISER[8]; } r300_nvic_regs_t;
typedef struct { volatile uint32_t CSR, RVR, CVR, CALIB; } r300_systick_regs_t;

#define R300_RCC ((r300_rcc_regs_t *)(uintptr_t)0x40023800u)
#define R300_FLASH ((r300_flash_regs_t *)(uintptr_t)0x40023c00u)
#define R300_PWR ((r300_pwr_regs_t *)(uintptr_t)0x40007000u)
#define R300_GPIOA ((r300_gpio_regs_t *)(uintptr_t)0x40020000u)
#define R300_GPIOB ((r300_gpio_regs_t *)(uintptr_t)0x40020400u)
#define R300_TIM2 ((r300_tim_regs_t *)(uintptr_t)0x40000000u)
#define R300_TIM3 ((r300_tim_regs_t *)(uintptr_t)0x40000400u)
#define R300_TIM4 ((r300_tim_regs_t *)(uintptr_t)0x40000800u)
#define R300_USART1 ((r300_usart_regs_t *)(uintptr_t)0x40011000u)
#define R300_USART2 ((r300_usart_regs_t *)(uintptr_t)0x40004400u)
#define R300_SPI1 ((r300_spi_regs_t *)(uintptr_t)0x40013000u)
#define R300_I2C2 ((r300_i2c_regs_t *)(uintptr_t)0x40005800u)
#define R300_ADC1 ((r300_adc_regs_t *)(uintptr_t)0x40012000u)
#define R300_ADC_COMMON ((r300_adc_common_regs_t *)(uintptr_t)0x40012300u)
#define R300_DMA1 ((r300_dma_regs_t *)(uintptr_t)0x40026000u)
#define R300_DMA2 ((r300_dma_regs_t *)(uintptr_t)0x40026400u)
#define R300_SYSCFG ((r300_syscfg_regs_t *)(uintptr_t)0x40013800u)
#define R300_EXTI ((r300_exti_regs_t *)(uintptr_t)0x40013c00u)
#define R300_NVIC ((r300_nvic_regs_t *)(uintptr_t)0xe000e100u)
#define R300_SYSTICK ((r300_systick_regs_t *)(uintptr_t)0xe000e010u)

/* 비트 번호를 마스크로 바꾸는 헬퍼 */
#define R300_BIT(bit) (1u << (bit))

_Static_assert(offsetof(r300_rcc_regs_t, AHB1ENR) == 0x30u, "RCC layout");
_Static_assert(offsetof(r300_gpio_regs_t, BSRR) == 0x18u, "GPIO layout");
_Static_assert(offsetof(r300_tim_regs_t, CCR1) == 0x34u, "TIM layout");
_Static_assert(offsetof(r300_i2c_regs_t, SR1) == 0x14u, "I2C layout");
_Static_assert(offsetof(r300_adc_regs_t, DR) == 0x4cu, "ADC layout");
_Static_assert(offsetof(r300_dma_regs_t, STREAM) == 0x10u, "DMA layout");
_Static_assert(sizeof(r300_dma_stream_regs_t) == 0x18u, "DMA stream layout");

#endif
