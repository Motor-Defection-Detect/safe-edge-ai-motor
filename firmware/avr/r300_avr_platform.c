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

void r300_avr_early_watchdog_off(void) __attribute__((naked, section(".init3")));
void r300_avr_early_watchdog_off(void)
{
    MCUCSR = 0u;
    wdt_disable();
}

ISR(TIMER0_COMP_vect)
{
    ++r300_ms;
}

ISR(INT4_vect)
{
    r300_heartbeat_ms = r300_ms;
    ++r300_heartbeat_count;
}

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

uint32_t r300_avr_millis(void)
{
    uint32_t value;
    uint8_t status = SREG;
    cli();
    value = r300_ms;
    SREG = status;
    return value;
}

uint32_t r300_avr_last_heartbeat_ms(void)
{
    uint32_t value;
    uint8_t status = SREG;
    cli();
    value = r300_heartbeat_ms;
    SREG = status;
    return value;
}

uint8_t r300_avr_heartbeat_edges(void)
{
    return r300_heartbeat_count;
}

bool r300_avr_arm_pressed(void)
{
    return (PIND & _BV(PD5)) == 0u;
}

bool r300_avr_estop_active(void)
{
    return (PIND & _BV(PD6)) == 0u;
}

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

bool r300_avr_diag_byte_write(uint8_t byte)
{
    if ((UCSR0A & _BV(UDRE0)) == 0u) return false;
    UDR0 = byte;
    return true;
}

void r300_avr_watchdog_enable(void)
{
    wdt_enable(WDTO_500MS);
}

void r300_avr_watchdog_kick(void)
{
    wdt_reset();
}
