/*
 * 이 파일의 역할: 보드 상수(r300_board_config.h)를 쓰는 변환 함수 2개 - ADC 카운트를 mA로, 듀티(permille)를 PWM compare로.
 * 읽기 순서: 보조 파일 (5/13 firmware/stm32/r300_stm32_runtime.c 가 이 함수들을 사용)
 * 관련: firmware/stm32/include/r300_board_config.h, docs/interface/r300-platform-decisions.md(전류 변환식)
 */
#include "r300_board_config.h"

/*
 * ADC 카운트 차이(raw - zero)를 mA로 변환한다. ACS712-05B(185 mV/A)와 12k/22k 분압, 3.3 V 12비트 ADC를 반영한 식이며
 * 정수 연산(64비트)으로 반올림하고 +/-5 A로 제한한다. 센서 영점과 ADC 기준 전압은 실측 보정 대상이다.
 */
int16_t r300_current_milliamps(uint16_t raw_count, uint16_t zero_count)
{
    int64_t delta = (int64_t)raw_count - (int64_t)zero_count;
    int64_t numerator = delta * (int64_t)R300_ADC_REFERENCE_MV *
                        (R300_ACS712_DIVIDER_TOP_KOHM +
                         R300_ACS712_DIVIDER_BOTTOM_KOHM) * 1000;
    int64_t denominator = (int64_t)R300_ADC_FULL_SCALE_COUNTS *
                          (int64_t)R300_ACS712_DIVIDER_BOTTOM_KOHM *
                          (int64_t)R300_ACS712_SENSITIVITY_MV_PER_A;
    numerator += numerator >= 0 ? denominator / 2 : -(denominator / 2);
    int64_t current_ma = numerator / denominator;
    if (current_ma > R300_ACS712_RANGE_MA) current_ma = R300_ACS712_RANGE_MA;
    if (current_ma < -(int64_t)R300_ACS712_RANGE_MA) current_ma = -(int64_t)R300_ACS712_RANGE_MA;
    return (int16_t)current_ma;
}

/*
 * 듀티(0~1000 permille)를 TIM4 compare 틱 값으로 변환한다. 1000을 넘는 입력은 1000으로 제한한다.
 */
uint16_t r300_motor_pwm_compare(uint16_t duty_permille)
{
    if (duty_permille > 1000u) duty_permille = 1000u;
    return (uint16_t)(((uint32_t)R300_MOTOR_PWM_PERIOD_TICKS * duty_permille) / 1000u);
}
