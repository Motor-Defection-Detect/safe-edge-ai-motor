/*
 * 이 파일의 역할: AVR 메인 루프 한 스텝 - 하드웨어 입력(ARM, E-stop, HEARTBEAT)을 읽어 안전 상태기계에
 * 넣고, 결과를 릴레이(PD7)와 진단 UART 프레임으로 내보낸다. 매 스텝 워치독을 kick한다.
 * 읽기 순서: 2/13
 * 다음에 볼 곳: firmware/stm32/r300_controller.c — AVR이 받는 HEARTBEAT(PB8)를 STM32가 만들어내는 쪽
 * 관련: firmware/avr/r300_safety.c(판정 로직), firmware/avr/r300_avr_platform.c(레지스터 접근),
 *       firmware/avr/r300_avr_diagnostics.c(진단 프레임 패킹)
 */
#include <avr/interrupt.h>

#include "r300_avr_diagnostics.h"
#include "r300_avr_platform.h"
#include "r300_avr_runtime.h"

/*
 * AVR 진단 프레임(16바이트)을 논블로킹으로 송신한다. 100 ms 주기로 새 프레임을 만들고,
 * UART 데이터 레지스터가 비어 있을 때 한 바이트씩만 밀어 넣는다(안전 루프를 막지 않기 위해).
 * 이전 프레임 송신이 끝나기 전에는 새 프레임을 만들지 않는다. 이 진단은 STM32가 읽기 전용으로 사용한다.
 */
static void diagnostic_service(r300_avr_runtime_t *runtime, uint32_t now)
{
    if (runtime->diagnostic_bytes == 0u &&
        (uint32_t)(now - runtime->last_diagnostic_ms) >= R300_AVR_DIAGNOSTIC_PERIOD_MS) {
        if (r300_avr_diagnostic_pack(runtime->diagnostic_frame,
                                     runtime->diagnostic_sequence, now,
                                     runtime->safety.state, runtime->safety.trip_cause,
                                     runtime->diagnostic_flags)) {
            ++runtime->diagnostic_sequence;
            runtime->last_diagnostic_ms = now;
            runtime->diagnostic_index = 0u;
            runtime->diagnostic_bytes = R300_AVR_DIAGNOSTIC_BYTES;
        }
    }
    if (runtime->diagnostic_index < runtime->diagnostic_bytes &&
        r300_avr_diag_byte_write(runtime->diagnostic_frame[runtime->diagnostic_index])) {
        ++runtime->diagnostic_index;
        if (runtime->diagnostic_index == runtime->diagnostic_bytes)
            runtime->diagnostic_bytes = 0u;
    }
}

/*
 * AVR 런타임 초기화. 순서가 중요하다: 플랫폼 초기화 -> 상태기계/디바운서 초기화 ->
 * 릴레이를 명시적으로 OFF -> 인터럽트 허용(sei) -> 마지막에 워치독 활성화.
 */
void r300_avr_runtime_init(r300_avr_runtime_t *runtime)
{
    uint32_t now;
    if (!runtime) return;
    *runtime = (r300_avr_runtime_t){0};
    r300_avr_platform_init();
    now = r300_avr_millis();
    r300_safety_init(&runtime->safety);
    r300_debounce_init(&runtime->arm, r300_avr_arm_pressed(), now);
    r300_debounce_init(&runtime->estop, r300_avr_estop_active(), now);
    runtime->heartbeat_edges = r300_avr_heartbeat_edges();
    r300_avr_set_relay(false);
    sei();
    r300_avr_watchdog_enable();
}

/*
 * 메인 루프 한 스텝: HEARTBEAT 엣지 반영 -> ARM/E-stop 디바운스 -> 안전 상태기계 실행 ->
 * 릴레이 출력 -> 워치독 kick -> 진단 송신. 반환값은 릴레이 명령.
 * 주의: ARM은 '활성화'는 디바운스하지만 원시 해제는 즉시 반영해 릴레이 권한을 바로 거둔다.
 * E-stop도 원시 입력이 눌리는 즉시 활성으로 취급한다(안전 쪽으로 빠른 반응).
 */
bool r300_avr_runtime_step(r300_avr_runtime_t *runtime, bool internal_fault)
{
    uint32_t now;
    uint8_t heartbeat_edges;
    bool arm_active;
    bool raw_arm;
    bool raw_estop;
    bool estop_active;
    bool relay;
    if (!runtime) return false;
    now = r300_avr_millis();
    heartbeat_edges = r300_avr_heartbeat_edges();
    if (heartbeat_edges != runtime->heartbeat_edges) {
        runtime->heartbeat_edges = heartbeat_edges;
        r300_heartbeat_edge(&runtime->safety, r300_avr_last_heartbeat_ms());
    }
    raw_arm = r300_avr_arm_pressed();
    (void)r300_debounce_update(&runtime->arm, raw_arm, now);
    /* Activation is debounced; any raw release removes relay permission immediately. */
    arm_active = raw_arm && runtime->arm.stable_pressed;
    raw_estop = r300_avr_estop_active();
    (void)r300_debounce_update(&runtime->estop, raw_estop, now);
    estop_active = raw_estop || runtime->estop.stable_pressed;
    relay = r300_safety_step(&runtime->safety, now, arm_active,
                             !runtime->arm.stable_pressed,
                             estop_active, internal_fault);
    runtime->diagnostic_flags = 0u;
    if (r300_heartbeat_healthy(&runtime->safety, now))
        runtime->diagnostic_flags |= R300_AVR_DIAGNOSTIC_HEARTBEAT_HEALTHY;
    if (arm_active) runtime->diagnostic_flags |= R300_AVR_DIAGNOSTIC_ARM_ACTIVE;
    if (estop_active) runtime->diagnostic_flags |= R300_AVR_DIAGNOSTIC_ESTOP_ACTIVE;
    if (!internal_fault) runtime->diagnostic_flags |= R300_AVR_DIAGNOSTIC_INTERNAL_HEALTHY;
    if (relay) runtime->diagnostic_flags |= R300_AVR_DIAGNOSTIC_RELAY_COMMAND;
    r300_avr_set_relay(relay);
    r300_avr_watchdog_kick();
    diagnostic_service(runtime, now);
    return relay;
}
