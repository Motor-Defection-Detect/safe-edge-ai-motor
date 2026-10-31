/*
 * 이 파일의 역할: Cortex-M4 시동 코드 - 리셋 시 .data 복사, .bss 0 초기화 후 main 호출, 인터럽트 벡터 테이블.
 * 이 프로젝트가 쓰는 핸들러는 SysTick과 EXTI1뿐이고 나머지는 무한 루프(Default_Handler)로 연결된다.
 * 읽기 순서: 보조 파일 (6/13 firmware/stm32/r300_stm32_platform.c 의 SysTick_Handler/EXTI1_IRQHandler 가 여기 연결됨)
 */
#include <stdint.h>

extern uint32_t _estack, _sidata, _sdata, _edata, _sbss, _ebss;
extern int main(void);
void EXTI1_IRQHandler(void);
void SysTick_Handler(void);

/* 예기치 않은 예외/인터럽트가 오면 무한 루프에 머문다. */
static void Default_Handler(void)
{
    for (;;) { }
}

/* 리셋 진입점. 초기값이 있는 전역변수를 플래시에서 RAM으로 복사하고 .bss를 0으로 채운 뒤 main()을 호출한다. */
void Reset_Handler(void)
{
    uint32_t *source = &_sidata;
    for (uint32_t *dest = &_sdata; dest < &_edata; ++dest) *dest = *source++;
    for (uint32_t *dest = &_sbss; dest < &_ebss; ++dest) *dest = 0u;
    (void)main();
    for (;;) { }
}

__attribute__((section(".isr_vector"), used))
const uintptr_t r300_vectors[] = {
    (uintptr_t)&_estack,                /* 0: initial stack */
    (uintptr_t)Reset_Handler,           /* 1: reset */
    (uintptr_t)Default_Handler,         /* 2: NMI */
    (uintptr_t)Default_Handler,         /* 3: HardFault */
    (uintptr_t)Default_Handler,         /* 4: MemManage */
    (uintptr_t)Default_Handler,         /* 5: BusFault */
    (uintptr_t)Default_Handler,         /* 6: UsageFault */
    0u, 0u, 0u, 0u,                    /* 7-10: reserved */
    (uintptr_t)Default_Handler,         /* 11: SVCall */
    (uintptr_t)Default_Handler,         /* 12: Debug monitor */
    0u,                                 /* 13: reserved */
    (uintptr_t)Default_Handler,         /* 14: PendSV */
    (uintptr_t)SysTick_Handler,         /* 15: SysTick */
    (uintptr_t)Default_Handler,         /* 16: WWDG */
    (uintptr_t)Default_Handler,         /* 17: PVD */
    (uintptr_t)Default_Handler,         /* 18: TAMP_STAMP */
    (uintptr_t)Default_Handler,         /* 19: RTC_WKUP */
    (uintptr_t)Default_Handler,         /* 20: FLASH */
    (uintptr_t)Default_Handler,         /* 21: RCC */
    (uintptr_t)Default_Handler,         /* 22: EXTI0 */
    (uintptr_t)EXTI1_IRQHandler,        /* 23: EXTI1 */
};
