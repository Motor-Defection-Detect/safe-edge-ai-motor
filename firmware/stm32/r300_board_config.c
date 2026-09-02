#include "r300_board_config.h"

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

uint16_t r300_motor_pwm_compare(uint16_t duty_permille)
{
    if (duty_permille > 1000u) duty_permille = 1000u;
    return (uint16_t)(((uint32_t)R300_MOTOR_PWM_PERIOD_TICKS * duty_permille) / 1000u);
}
