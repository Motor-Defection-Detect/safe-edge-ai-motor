/*
 * 이 파일의 역할: ATmega128A 레지스터 직접 제어 - Timer0 1 ms 시간 기준, INT4(PE4) HEARTBEAT 엣지 캡처,
 * ARM(PD5)/E-stop(PD6) 입력, 릴레이(PD7) 출력, 진단 UART0(TX), 워치독(500 ms).
 * 읽기 순서: 보조 파일 (2/13 firmware/avr/r300_avr_runtime.c 가 이 함수들을 호출)
 * 관련: firmware/avr/include/r300_avr_platform.h, docs/interface/r300-platform-decisions.md(릴레이 극성/워치독 근거)
 */
#include <avr/interrupt.h>
#include <avr/io.h>
#include <avr/wdt.h>

#include "r300_avr_platform.h"
#include "r300_link.h"

#ifndef R300_AVR_CPU_HZ
#error "Define R300_AVR_CPU_HZ for the actual ATmega128A board clock"
#endif

#ifndef R300_RELAY_ACTIVE_HIGH
#error "Define R300_RELAY_ACTIVE_HIGH from the measured relay module polarity"
#endif

#if R300_RELAY_ACTIVE_HIGH != 0 && R300_RELAY_ACTIVE_HIGH != 1
#error "R300_RELAY_ACTIVE_HIGH must be 0 or 1"
#endif

#if (R300_AVR_CPU_HZ % 64000UL) != 0
#error "R300_AVR_CPU_HZ must support the Timer0 64-prescaler 1 ms timebase"
#endif

#define R300_TIMER0_COMPARE ((R300_AVR_CPU_HZ / 64000UL) - 1UL)
#define R300_AVR_DIAG_UBRR ((R300_AVR_CPU_HZ / (16UL * R300_AVR_DIAGNOSTIC_BAUD)) - 1UL)
#define R300_AVR_DIAG_ACTUAL_BAUD (R300_AVR_CPU_HZ / (16UL * (R300_AVR_DIAG_UBRR + 1UL)))

#if R300_AVR_DIAG_ACTUAL_BAUD > R300_AVR_DIAGNOSTIC_BAUD
#define R300_AVR_DIAG_BAUD_ERROR (R300_AVR_DIAG_ACTUAL_BAUD - R300_AVR_DIAGNOSTIC_BAUD)
#else
#define R300_AVR_DIAG_BAUD_ERROR (R300_AVR_DIAGNOSTIC_BAUD - R300_AVR_DIAG_ACTUAL_BAUD)
#endif

#if R300_TIMER0_COMPARE > 255UL
#error "R300_AVR_CPU_HZ is too high for the Timer0 1 ms compare configuration"
#endif

_Static_assert(R300_AVR_WATCHDOG_TIMEOUT_MS == 500u,
               "update the AVR watchdog prescaler with the link timing contract");

#if R300_AVR_DIAG_UBRR > 4095UL || (R300_AVR_DIAG_BAUD_ERROR * 100UL) > (R300_AVR_DIAGNOSTIC_BAUD * 2UL)
#error "R300 AVR diagnostic UART baud error exceeds 2 percent"
#endif

static volatile uint32_t r300_ms;
static volatile uint32_t r300_heartbeat_ms;
static volatile uint8_t r300_heartbeat_count;

/*
 * 부팅 초기(.init3, main 이전)에 워치독을 꺼서 워치독 리셋 직후 곧바로 또 리셋되는 것을 막는다.
 * 워치독은 런타임 초기화가 끝난 뒤 r300_avr_watchdog_enable()에서 다시 켠다.
 */
void r300_avr_early_watchdog_off(void) __attribute__((naked, section(".init3")));
void r300_avr_early_watchdog_off(void)
{
    MCUCSR = 0u;
    wdt_disable();
}

/* Timer0 비교 일치 인터럽트(1 ms마다). AVR의 밀리초 시간 기준 r300_ms를 증가시킨다. */
ISR(TIMER0_COMP_vect)
{
    ++r300_ms;
}

/*
 * PE4(STM32 PB8 HEARTBEAT) 레벨이 바뀔 때마다(양 엣지) 실행된다.
 * 엣지 시각과 누적 횟수만 기록하고 판정은 메인 루프(r300_safety)가 한다.
 */
ISR(INT4_vect)
{
    r300_heartbeat_ms = r300_ms;
    ++r300_heartbeat_count;
}

/*
 * 핀/타이머/UART 초기화. 릴레이 핀(PD7)은 출력으로 바꾸기 '전에' 비활성 레벨을 먼저 PORT에 써 두어
 * 전환 순간 릴레이가 잠깐 켜지는 일이 없게 한다. PE4는 STM32가 리셋 중 High-Z일 때 떠다니지
 * 않도록 내부 풀업을 켠다. ARM/E-stop은 풀업 + active-low 입력이다.
 */
void r300_avr_platform_init(void)
{
    /* Preload the measured inactive level before enabling the relay output. */
#if R300_RELAY_ACTIVE_HIGH
    PORTD &= (uint8_t)~_BV(PD7);
#else
    PORTD |= _BV(PD7);
#endif
    DDRD |= _BV(PD7);
    DDRD &= (uint8_t)~(_BV(PD5) | _BV(PD6));
    PORTD |= _BV(PD5) | _BV(PD6);

    DDRE &= (uint8_t)~_BV(PE4);
    /* Bias the heartbeat input while STM32 PB8 is high-impedance during reset. */
    PORTE |= _BV(PE4);
    EICRB = (EICRB & (uint8_t)~(_BV(ISC41) | _BV(ISC40))) | _BV(ISC40);
    EIFR = _BV(INTF4);
    EIMSK |= _BV(INT4);

    TCCR0 = _BV(WGM01) | _BV(CS01) | _BV(CS00);
    OCR0 = (uint8_t)R300_TIMER0_COMPARE;
    TIMSK |= _BV(OCIE0);

    UBRR0H = (uint8_t)(R300_AVR_DIAG_UBRR >> 8);
    UBRR0L = (uint8_t)R300_AVR_DIAG_UBRR;
    UCSR0A = 0u;
    UCSR0B = _BV(TXEN0);
    UCSR0C = _BV(UCSZ01) | _BV(UCSZ00);
}

/* 밀리초 카운터를 읽는다. 32비트 값이 인터럽트 중간에 갱신되어 깨지지 않도록 잠시 인터럽트를 끄고 복사한다. */
uint32_t r300_avr_millis(void)
{
    uint32_t value;
    uint8_t status = SREG;
    cli();
    value = r300_ms;
    SREG = status;
    return value;
}

/* 마지막 HEARTBEAT 엣지가 ISR에서 기록된 시각을 반환한다(인터럽트 보호 읽기). */
uint32_t r300_avr_last_heartbeat_ms(void)
{
    uint32_t value;
    uint8_t status = SREG;
    cli();
    value = r300_heartbeat_ms;
    SREG = status;
    return value;
}

/* HEARTBEAT 엣지 누적 카운터. 메인 루프가 이전 값과 비교해 새 엣지가 있었는지만 확인한다. */
uint8_t r300_avr_heartbeat_edges(void)
{
    return r300_heartbeat_count;
}

/* ARM 버튼(PD5)이 눌렸는지. 풀업 입력이라 눌리면 0(active-low)이다. */
bool r300_avr_arm_pressed(void)
{
    return (PIND & _BV(PD5)) == 0u;
}

/* E-stop(PD6)이 활성인지. active-low 입력이라 핀이 0이면 활성이다. */
bool r300_avr_estop_active(void)
{
    return (PIND & _BV(PD6)) == 0u;
}

/*
 * 릴레이 코일 구동. 모듈 극성은 빌드 설정 R300_RELAY_ACTIVE_HIGH로 정해지며 실제 모듈 측정 전까지는
 * 소프트웨어 검증용 기본값이다. 두 극성 분기 모두 'energized=false'가 비활성 레벨을 출력한다.
 */
void r300_avr_set_relay(bool energized)
{
#if R300_RELAY_ACTIVE_HIGH
    if (energized) PORTD |= _BV(PD7);
    else PORTD &= (uint8_t)~_BV(PD7);
#else
    if (energized) PORTD &= (uint8_t)~_BV(PD7);
    else PORTD |= _BV(PD7);
#endif
}

/* 진단 UART 송신 레지스터가 비어 있으면 1바이트를 쓰고 true, 아니면 기다리지 않고 false를 반환한다(논블로킹). */
bool r300_avr_diag_byte_write(uint8_t byte)
{
    if ((UCSR0A & _BV(UDRE0)) == 0u) return false;
    UDR0 = byte;
    return true;
}

/* 워치독을 500 ms로 켠다. 메인 루프가 멈추면 리셋되어 릴레이가 안전 상태(OFF)로 돌아온다. */
void r300_avr_watchdog_enable(void)
{
    wdt_enable(WDTO_500MS);
}

/* 워치독 카운터를 리셋한다. 안전 루프 한 바퀴가 정상 종료될 때마다 호출된다. */
void r300_avr_watchdog_kick(void)
{
    wdt_reset();
}
