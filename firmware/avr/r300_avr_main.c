/*
 * 이 파일의 역할: AVR 진입점. 런타임을 초기화하고 r300_avr_runtime_step을 무한 반복한다.
 * internal_fault는 현재 항상 false로 전달된다(내부 고장 훅은 자리만 있음).
 * 읽기 순서: 보조 파일 (2/13 firmware/avr/r300_avr_runtime.c 가 실제 동작)
 */
#include "r300_avr_runtime.h"

int main(void)
{
    r300_avr_runtime_t runtime;
    r300_avr_runtime_init(&runtime);
    for (;;) (void)r300_avr_runtime_step(&runtime, false);
}
