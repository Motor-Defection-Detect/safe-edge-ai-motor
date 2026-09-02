#include "r300_board_config.h"
#include "r300_link.h"
#include "r300_stm32_platform.h"
#include "r300_stm32f411_regs.h"

#define RCC_CR_PLLON R300_BIT(24)
#define RCC_CR_PLLRDY R300_BIT(25)
#define RCC_CFGR_SW_PLL 2u
#define RCC_CFGR_SWS_MASK (3u << 2)
#define RCC_CFGR_SWS_PLL (2u << 2)
#define RCC_CFGR_PPRE1_DIV2 (4u << 10)
#define PWR_CR_VOS_SCALE1 (3u << 14)
#define PWR_CSR_VOSRDY R300_BIT(14)
#define I2C_CR1_PE R300_BIT(0)
#define I2C_CR1_START R300_BIT(8)
#define I2C_CR1_STOP R300_BIT(9)
#define I2C_CR1_ACK R300_BIT(10)
#define I2C_SR1_SB R300_BIT(0)
#define I2C_SR1_ADDR R300_BIT(1)
#define I2C_SR1_BTF R300_BIT(2)
#define I2C_SR1_RXNE R300_BIT(6)
#define I2C_SR1_TXE R300_BIT(7)
#define I2C_SR1_ERRORS (R300_BIT(8) | R300_BIT(9) | R300_BIT(10) | R300_BIT(11) | R300_BIT(14))
#define I2C_SR2_BUSY R300_BIT(1)
#define MPU6050_REG_SMPLRT_DIV 0x19u
#define MPU6050_REG_CONFIG 0x1au
#define MPU6050_REG_GYRO_CONFIG 0x1bu
#define MPU6050_REG_ACCEL_CONFIG 0x1cu
#define MPU6050_REG_INT_ENABLE 0x38u
#define MPU6050_REG_INT_STATUS 0x3au
#define MPU6050_REG_PWR_MGMT_1 0x6bu
#define MPU6050_REG_WHO_AM_I 0x75u
#define USART_SR_ERRORS 0x0fu
#define USART_SR_RXNE R300_BIT(5)
#define DMA_STREAM_EN R300_BIT(0)
#define DMA_STREAM0_ERRORS (R300_BIT(0) | R300_BIT(2) | R300_BIT(3))
#define DMA_STREAM5_ERRORS (R300_BIT(6) | R300_BIT(8) | R300_BIT(9))
#define DMA_STREAM5_CLEAR 0x00000f40u
#define DMA_STREAM6_ERRORS (R300_BIT(16) | R300_BIT(18) | R300_BIT(19))
#define DMA_STREAM6_CLEAR 0x003d0000u
#define R300_HEALTH_RX_BYTES 64u

static volatile uint16_t adc_samples[R300_BLOCK_SAMPLES];
static volatile uint8_t health_rx[R300_HEALTH_RX_BYTES];
static volatile bool vibration_pending;
static volatile uint32_t vibration_timestamp_us;
static uint16_t last_adc_index;
static uint16_t last_health_rx_index;
static uint32_t last_adc_us;
static uint32_t last_vibration_us;
static uint32_t last_identity_check_us;
static volatile uint32_t vibration_overruns;
static uint32_t adc_overruns;
static uint32_t sensor_bus_errors;
static uint32_t uart_errors;
static uint32_t avr_uart_errors;
static bool adc_seen;
static bool vibration_seen;
static bool sensor_identity_valid;
static volatile uint32_t system_ms;

_Static_assert(R300_APB2_HZ % R300_AVR_DIAGNOSTIC_BAUD == 0u,
               "AVR diagnostic baud must divide the USART1 clock exactly");
_Static_assert(R300_HEALTH_RX_BYTES >= R300_HEALTH_BYTES + R300_MOTOR_COMMAND_BYTES,
               "Jetson RX ring must hold one complete HEALTH and command burst");
_Static_assert(R300_SYSCLK_HZ % R300_SAMPLE_RATE_HZ == 0u,
               "sample rate must divide the timer clock exactly");
_Static_assert(R300_SYSCLK_HZ /
               ((R300_MOTOR_PWM_PRESCALER + 1u) * R300_MOTOR_PWM_PERIOD_TICKS) ==
               R300_MOTOR_PWM_HZ, "PWM constants must produce the requested frequency");

static bool wait_bits(volatile uint32_t *reg, uint32_t mask, bool set)
{
    for (uint32_t timeout = 1000000u; timeout != 0u; --timeout)
        if (((*reg & mask) != 0u) == set) return true;
    return false;
}

static bool wait_value(volatile uint32_t *reg, uint32_t mask, uint32_t value)
{
    for (uint32_t timeout = 1000000u; timeout != 0u; --timeout)
        if ((*reg & mask) == value) return true;
    return false;
}

static void gpio_mode(r300_gpio_regs_t *gpio, uint8_t pin, uint32_t mode)
{
    gpio->MODER = (gpio->MODER & ~(3u << (pin * 2u))) | (mode << (pin * 2u));
}

static void gpio_speed(r300_gpio_regs_t *gpio, uint8_t pin, uint32_t speed)
{
    gpio->OSPEEDR = (gpio->OSPEEDR & ~(3u << (pin * 2u))) | (speed << (pin * 2u));
}

static void gpio_pull(r300_gpio_regs_t *gpio, uint8_t pin, uint32_t pull)
{
    gpio->PUPDR = (gpio->PUPDR & ~(3u << (pin * 2u))) | (pull << (pin * 2u));
}

static void gpio_af(r300_gpio_regs_t *gpio, uint8_t pin, uint8_t af)
{
    uint8_t bank = pin >= 8u ? 1u : 0u;
    uint8_t shift = (uint8_t)((pin & 7u) * 4u);
    gpio->AFR[bank] = (gpio->AFR[bank] & ~(15u << shift)) | ((uint32_t)af << shift);
}

static bool i2c_wait(volatile uint32_t *reg, uint32_t mask, bool set)
{
    for (uint32_t timeout = 100000u; timeout != 0u; --timeout) {
        if ((R300_I2C2->SR1 & I2C_SR1_ERRORS) != 0u) {
            ++sensor_bus_errors;
            R300_I2C2->CR1 |= I2C_CR1_STOP;
            R300_I2C2->SR1 &= ~I2C_SR1_ERRORS;
            return false;
        }
        if (((*reg & mask) != 0u) == set) return true;
    }
    ++sensor_bus_errors;
    R300_I2C2->CR1 |= I2C_CR1_STOP;
    return false;
}

static bool i2c_address(uint8_t address, bool read)
{
    R300_I2C2->CR1 |= I2C_CR1_START;
    if (!i2c_wait(&R300_I2C2->SR1, I2C_SR1_SB, true)) return false;
    R300_I2C2->DR = (uint32_t)((address << 1) | (read ? 1u : 0u));
    return i2c_wait(&R300_I2C2->SR1, I2C_SR1_ADDR, true);
}

static void i2c_clear_address(void)
{
    volatile uint32_t clear = R300_I2C2->SR1;
    clear = R300_I2C2->SR2;
    (void)clear;
}

static bool mpu_write(uint8_t reg, uint8_t value)
{
    if (!i2c_wait(&R300_I2C2->SR2, I2C_SR2_BUSY, false) ||
        !i2c_address(R300_MPU6050_ADDRESS, false)) return false;
    i2c_clear_address();
    if (!i2c_wait(&R300_I2C2->SR1, I2C_SR1_TXE, true)) return false;
    R300_I2C2->DR = reg;
    if (!i2c_wait(&R300_I2C2->SR1, I2C_SR1_TXE, true)) return false;
    R300_I2C2->DR = value;
    if (!i2c_wait(&R300_I2C2->SR1, I2C_SR1_BTF, true)) return false;
    R300_I2C2->CR1 |= I2C_CR1_STOP;
    return true;
}

static bool mpu_read(uint8_t reg, uint8_t *data, size_t bytes)
{
    if (!data || bytes == 0u || !i2c_wait(&R300_I2C2->SR2, I2C_SR2_BUSY, false) ||
        !i2c_address(R300_MPU6050_ADDRESS, false)) return false;
    i2c_clear_address();
    if (!i2c_wait(&R300_I2C2->SR1, I2C_SR1_TXE, true)) return false;
    R300_I2C2->DR = reg;
    if (!i2c_wait(&R300_I2C2->SR1, I2C_SR1_BTF, true) ||
        !i2c_address(R300_MPU6050_ADDRESS, true)) return false;
    if (bytes == 1u) {
        R300_I2C2->CR1 &= ~I2C_CR1_ACK;
        i2c_clear_address();
        R300_I2C2->CR1 |= I2C_CR1_STOP;
        if (!i2c_wait(&R300_I2C2->SR1, I2C_SR1_RXNE, true)) return false;
        data[0] = (uint8_t)R300_I2C2->DR;
    } else {
        R300_I2C2->CR1 |= I2C_CR1_ACK;
        i2c_clear_address();
        size_t index = 0u;
        while (bytes - index > 3u) {
            if (!i2c_wait(&R300_I2C2->SR1, I2C_SR1_RXNE, true)) return false;
            data[index++] = (uint8_t)R300_I2C2->DR;
        }
        if (!i2c_wait(&R300_I2C2->SR1, I2C_SR1_BTF, true)) return false;
        R300_I2C2->CR1 &= ~I2C_CR1_ACK;
        data[index++] = (uint8_t)R300_I2C2->DR;
        R300_I2C2->CR1 |= I2C_CR1_STOP;
        data[index++] = (uint8_t)R300_I2C2->DR;
        if (!i2c_wait(&R300_I2C2->SR1, I2C_SR1_RXNE, true)) return false;
        data[index] = (uint8_t)R300_I2C2->DR;
    }
    R300_I2C2->CR1 |= I2C_CR1_ACK;
    return true;
}

static bool clock_init(void)
{
    R300_RCC->APB1ENR |= R300_BIT(28);
    R300_PWR->CR = (R300_PWR->CR & ~(3u << 14)) | PWR_CR_VOS_SCALE1;
    if (!wait_bits(&R300_PWR->CSR, PWR_CSR_VOSRDY, true)) return false;
    R300_RCC->PLLCFGR = 16u | (192u << 6) | (4u << 24);
    R300_FLASH->ACR = R300_BIT(10) | R300_BIT(9) | R300_BIT(8) | 3u;
    R300_RCC->CR |= RCC_CR_PLLON;
    if (!wait_bits(&R300_RCC->CR, RCC_CR_PLLRDY, true)) return false;
    R300_RCC->CFGR = RCC_CFGR_PPRE1_DIV2 | RCC_CFGR_SW_PLL;
    return wait_value(&R300_RCC->CFGR, RCC_CFGR_SWS_MASK, RCC_CFGR_SWS_PLL);
}

bool r300_stm32_platform_init(void)
{
    if (!clock_init()) return false;
    R300_RCC->AHB1ENR |= R300_BIT(0) | R300_BIT(1) | R300_BIT(21) | R300_BIT(22);
    R300_RCC->APB1ENR |= R300_BIT(0) | R300_BIT(1) | R300_BIT(2) | R300_BIT(17) | R300_BIT(22);
    R300_RCC->APB2ENR |= R300_BIT(4) | R300_BIT(8) | R300_BIT(14);

    gpio_mode(R300_GPIOA, 0u, 3u);
    for (uint8_t pin = 2u; pin <= 3u; ++pin) {
        gpio_mode(R300_GPIOA, pin, 2u); gpio_speed(R300_GPIOA, pin, 2u); gpio_af(R300_GPIOA, pin, 7u);
    }
    for (uint8_t pin = 9u; pin <= 10u; ++pin) {
        gpio_mode(R300_GPIOA, pin, 2u); gpio_speed(R300_GPIOA, pin, 2u); gpio_af(R300_GPIOA, pin, 7u);
    }
    /* Preload inactive/safe GPIO levels before their output modes can drive a pin. */
    R300_GPIOB->BSRR = R300_BIT(23) | R300_BIT(24);
    gpio_mode(R300_GPIOB, 1u, 0u);
    gpio_mode(R300_GPIOB, 6u, 2u); gpio_speed(R300_GPIOB, 6u, 2u); gpio_af(R300_GPIOB, 6u, 2u);
    gpio_mode(R300_GPIOB, 7u, 1u); gpio_mode(R300_GPIOB, 8u, 1u);
    for (uint8_t pin = 10u; pin <= 11u; ++pin) {
        gpio_mode(R300_GPIOB, pin, 2u); gpio_speed(R300_GPIOB, pin, 2u);
        gpio_af(R300_GPIOB, pin, 4u); gpio_pull(R300_GPIOB, pin, 1u);
        R300_GPIOB->OTYPER |= R300_BIT(pin);
    }

    /* Start the 1 MHz source clock before sensor initialization. */
    R300_TIM2->PSC = 95u; R300_TIM2->ARR = 0xffffffffu;
    R300_TIM2->EGR = 1u; R300_TIM2->SR = 0u; R300_TIM2->CR1 = 1u;
    R300_RCC->APB1RSTR |= R300_BIT(22); R300_RCC->APB1RSTR &= ~R300_BIT(22);
    R300_I2C2->CR2 = 48u;
    R300_I2C2->CCR = R300_BIT(15) | 40u; /* 400 kHz fast mode, duty 2. */
    R300_I2C2->TRISE = 15u;
    R300_I2C2->CR1 = I2C_CR1_PE | I2C_CR1_ACK;
    uint8_t identity = 0u;
    if (!mpu_read(MPU6050_REG_WHO_AM_I, &identity, 1u) ||
        identity != R300_MPU6050_WHO_AM_I ||
        !mpu_write(MPU6050_REG_PWR_MGMT_1, 0x01u) ||
        !mpu_write(MPU6050_REG_SMPLRT_DIV, R300_MPU6050_SMPLRT_DIV) ||
        !mpu_write(MPU6050_REG_CONFIG, R300_MPU6050_CONFIG) ||
        !mpu_write(MPU6050_REG_GYRO_CONFIG, 0x00u) ||
        !mpu_write(MPU6050_REG_ACCEL_CONFIG, R300_MPU6050_ACCEL_CONFIG) ||
        !mpu_write(MPU6050_REG_INT_ENABLE, 0x01u) ||
        !mpu_read(MPU6050_REG_ACCEL_CONFIG, &identity, 1u) ||
        identity != R300_MPU6050_ACCEL_CONFIG) return false;
    R300_SYSCFG->EXTICR[0] = (R300_SYSCFG->EXTICR[0] & ~(15u << 4)) | (1u << 4);
    R300_EXTI->PR = R300_BIT(1);
    R300_EXTI->IMR |= R300_BIT(1);
    R300_EXTI->RTSR |= R300_BIT(1);
    R300_NVIC->ISER[0] = R300_BIT(7);
    sensor_identity_valid = true;

    R300_SYSTICK->RVR = R300_SYSCLK_HZ / 1000u - 1u;
    R300_SYSTICK->CVR = 0u;
    R300_SYSTICK->CSR = R300_BIT(2) | R300_BIT(1) | R300_BIT(0);
    R300_TIM4->PSC = 0u; R300_TIM4->ARR = R300_MOTOR_PWM_PERIOD_TICKS - 1u;
    R300_TIM4->CCMR1 = (6u << 4) | R300_BIT(3);
    /* R300_HW_TUNE: CC1P inverts PWM for a verified active-low module input. */
    R300_TIM4->CCER = R300_LR7843_PWM_ACTIVE_HIGH ? 1u : 3u;
    R300_TIM4->CCR1 = 0u; R300_TIM4->CR1 = R300_BIT(7) | 1u;

    R300_DMA2->STREAM[0].CR = 0u;
    R300_DMA2->LIFCR = 0x3du;
    R300_DMA2->STREAM[0].NDTR = R300_BLOCK_SAMPLES;
    R300_DMA2->STREAM[0].PAR = (uint32_t)(uintptr_t)&R300_ADC1->DR;
    R300_DMA2->STREAM[0].M0AR = (uint32_t)(uintptr_t)adc_samples;
    R300_DMA2->STREAM[0].CR = R300_BIT(13) | R300_BIT(11) | R300_BIT(10) |
                                     R300_BIT(8) | DMA_STREAM_EN;
    R300_ADC_COMMON->CCR = R300_BIT(16);
    R300_ADC1->SMPR2 = 4u; R300_ADC1->SQR3 = 0u;
    R300_ADC1->CR2 = (8u << 24) | R300_BIT(28) | R300_BIT(14) | R300_BIT(9) | 1u;
    R300_TIM3->PSC = 0u; R300_TIM3->ARR = (R300_SYSCLK_HZ / R300_SAMPLE_RATE_HZ) - 1u;
    R300_TIM3->CR2 = 2u << 4; R300_TIM3->EGR = 1u; R300_TIM3->CR1 = 1u;

    R300_DMA1->STREAM[5].CR = 0u;
    R300_DMA1->HIFCR = DMA_STREAM5_CLEAR;
    R300_DMA1->STREAM[5].NDTR = R300_HEALTH_RX_BYTES;
    R300_DMA1->STREAM[5].PAR = (uint32_t)(uintptr_t)&R300_USART2->DR;
    R300_DMA1->STREAM[5].M0AR = (uint32_t)(uintptr_t)health_rx;
    R300_DMA1->STREAM[5].CR = (4u << 25) | R300_BIT(10) | R300_BIT(8) |
                                     DMA_STREAM_EN;
    R300_USART2->BRR = 52u;
    R300_USART2->CR3 = R300_BIT(7) | R300_BIT(6);
    R300_USART2->CR1 = R300_BIT(13) | R300_BIT(3) | R300_BIT(2);
    R300_USART1->BRR = R300_APB2_HZ / R300_AVR_DIAGNOSTIC_BAUD;
    R300_USART1->CR1 = R300_BIT(13) | R300_BIT(2);
    return sensor_bus_errors == 0u;
}

bool r300_stm32_platform_healthy(void)
{
    uint32_t now_us = R300_TIM2->CNT;
    const uint32_t adc_dma_errors = R300_DMA2->LISR & DMA_STREAM0_ERRORS;
    const uint32_t uart_dma_errors = R300_DMA1->HISR &
        (DMA_STREAM5_ERRORS | DMA_STREAM6_ERRORS);
    const uint32_t adc_errors = R300_ADC1->SR & R300_BIT(5);
    if (sensor_identity_valid &&
        (uint32_t)(now_us - last_identity_check_us) >= R300_SENSOR_IDENTITY_CHECK_US) {
        uint32_t errors_before = sensor_bus_errors;
        last_identity_check_us = now_us;
        uint8_t identity = 0u;
        if (!mpu_read(MPU6050_REG_WHO_AM_I, &identity, 1u) ||
            identity != R300_MPU6050_WHO_AM_I) {
            if (sensor_bus_errors == errors_before) ++sensor_bus_errors;
            sensor_identity_valid = false;
        }
    }
    return sensor_identity_valid && sensor_bus_errors == 0u && uart_errors == 0u && adc_seen && vibration_seen &&
           vibration_overruns == 0u && adc_overruns == 0u &&
           adc_dma_errors == 0u && uart_dma_errors == 0u && adc_errors == 0u &&
           (uint32_t)(now_us - last_adc_us) <= R300_SENSOR_STALE_US &&
           (uint32_t)(now_us - last_vibration_us) <= R300_SENSOR_STALE_US;
}

void r300_stm32_platform_diagnostics(r300_stm32_platform_diagnostics_t *diagnostics)
{
    if (!diagnostics) return;
    diagnostics->vibration_overruns = vibration_overruns;
    diagnostics->adc_overruns = adc_overruns;
    diagnostics->sensor_bus_errors = sensor_bus_errors;
    diagnostics->uart_errors = uart_errors;
    diagnostics->avr_uart_errors = avr_uart_errors;
    diagnostics->adc_dma_errors = R300_DMA2->LISR & DMA_STREAM0_ERRORS;
    diagnostics->uart_dma_errors = R300_DMA1->HISR &
                                   (DMA_STREAM5_ERRORS | DMA_STREAM6_ERRORS);
    diagnostics->adc_peripheral_errors = R300_ADC1->SR & R300_BIT(5);
}

void EXTI1_IRQHandler(void)
{
    R300_EXTI->PR = R300_BIT(1);
    if (vibration_pending) ++vibration_overruns;
    vibration_timestamp_us = R300_TIM2->CNT;
    vibration_pending = true;
}

void SysTick_Handler(void)
{
    ++system_ms;
}

bool r300_stm32_vibration_poll(int16_t xyz[3], uint32_t *timestamp_us)
{
    uint8_t raw[7];
    uint32_t event_timestamp_us;
    if (!xyz || !timestamp_us || !vibration_pending || sensor_bus_errors != 0u) return false;
    event_timestamp_us = vibration_timestamp_us;
    vibration_pending = false;
    /* INT_STATUS first clears the data-ready latch in the same burst. */
    if (!mpu_read(MPU6050_REG_INT_STATUS, raw, sizeof(raw))) return false;
    for (size_t axis = 0; axis < 3u; ++axis)
        xyz[axis] = (int16_t)(((uint16_t)raw[axis * 2u + 1u] << 8) |
                              (uint16_t)raw[axis * 2u + 2u]);
    *timestamp_us = event_timestamp_us;
    last_vibration_us = *timestamp_us;
    vibration_seen = true;
    return true;
}

bool r300_stm32_current_poll(uint16_t *adc_count)
{
    uint16_t index;
    uint32_t now_us;
    uint32_t elapsed_us;
    if (!adc_count) return false;
    now_us = R300_TIM2->CNT;
    elapsed_us = now_us - last_adc_us;
    index = (uint16_t)((R300_BLOCK_SAMPLES - R300_DMA2->STREAM[0].NDTR) % R300_BLOCK_SAMPLES);
    if (index == last_adc_index && (!adc_seen || elapsed_us < R300_FRAME_PERIOD_MS * 1000u))
        return false;
    /* Latest-value hold permits skipped intermediate ADC samples; only a full
       128-sample DMA ring overwrite loses ownership. */
    if (adc_seen && elapsed_us >= R300_FRAME_PERIOD_MS * 1000u) ++adc_overruns;
    last_adc_index = index;
    adc_seen = true;
    last_adc_us = now_us;
    *adc_count = adc_samples[index == 0u ? R300_BLOCK_SAMPLES - 1u : index - 1u];
    return true;
}

bool r300_stm32_health_byte(uint8_t *byte)
{
    uint16_t write_index;
    uint32_t status;
    if (!byte) return false;
    status = R300_USART2->SR;
    if ((status & USART_SR_ERRORS) != 0u) {
        (void)R300_USART2->DR;
        ++uart_errors;
        return false;
    }
    write_index = (uint16_t)((R300_HEALTH_RX_BYTES -
        R300_DMA1->STREAM[5].NDTR) % R300_HEALTH_RX_BYTES);
    if (write_index == last_health_rx_index) return false;
    *byte = health_rx[last_health_rx_index];
    last_health_rx_index = (uint16_t)((last_health_rx_index + 1u) %
                                      R300_HEALTH_RX_BYTES);
    return true;
}

bool r300_stm32_avr_diag_byte(uint8_t *byte)
{
    uint32_t status;
    if (!byte) return false;
    status = R300_USART1->SR;
    if ((status & USART_SR_ERRORS) != 0u) {
        (void)R300_USART1->DR;
        ++avr_uart_errors;
        return false;
    }
    if ((status & USART_SR_RXNE) == 0u) return false;
    *byte = (uint8_t)R300_USART1->DR;
    return true;
}

bool r300_stm32_uart_tx_busy(void)
{
    if ((R300_DMA1->HISR & DMA_STREAM6_ERRORS) != 0u) ++uart_errors;
    return (R300_DMA1->STREAM[6].CR & DMA_STREAM_EN) != 0u;
}

bool r300_stm32_uart_tx_start(const uint8_t *data, size_t bytes)
{
    if (!data || bytes == 0u || bytes > 65535u || r300_stm32_uart_tx_busy()) return false;
    R300_DMA1->HIFCR = DMA_STREAM6_CLEAR;
    R300_DMA1->STREAM[6].PAR = (uint32_t)(uintptr_t)&R300_USART2->DR;
    R300_DMA1->STREAM[6].M0AR = (uint32_t)(uintptr_t)data;
    R300_DMA1->STREAM[6].NDTR = (uint32_t)bytes;
    R300_DMA1->STREAM[6].CR = (4u << 25) | R300_BIT(10) | R300_BIT(6) | DMA_STREAM_EN;
    return true;
}

void r300_stm32_heartbeat_write(bool level)
{
    R300_GPIOB->BSRR = R300_BIT(level ? 8u : 24u);
}

void r300_stm32_pwm_write(uint16_t compare)
{
    R300_TIM4->CCR1 = compare > R300_MOTOR_PWM_PERIOD_TICKS ?
                      R300_MOTOR_PWM_PERIOD_TICKS : compare;
}

uint32_t r300_stm32_millis(void)
{
    return system_ms;
}
