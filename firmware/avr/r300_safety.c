/*
 * 이 파일의 역할: ATmega128A 안전 상태기계(DISARMED/ARMED/TRIPPED). 릴레이(PD7)를 켤지 말지를 결정하는
 * 순수 로직이며 하드웨어 레지스터는 만지지 않는다. 릴레이 권한은 AVR만 독점한다(STM32/Jetson은 읽기 전용).
 * 읽기 순서: 1/13 (가장 쉬움, 여기서 시작)
 * 다음에 볼 곳: firmware/avr/r300_avr_runtime.c — 이 상태기계에 디바운스된 입력과 HEARTBEAT 엣지를 넣어주는 루프
 * 관련: firmware/avr/include/r300_safety.h, firmware/avr/include/r300_link.h(생성 파일, 상수/enum 정의)
 */
#include "r300_safety.h"

/*
 * 안전 상태기계를 전원 인가 직후 상태로 초기화한다.
 * 부팅 시 DISARMED + 원인 RESET이며, 릴레이는 꺼져 있다. 첫 ARM(+정상 HEARTBEAT) 전에는
 * HEARTBEAT 타임아웃이 TRIP을 만들지 않는다(Jetson/STM32 링크가 올라오는 시간을 준다).
 */
void r300_safety_init(r300_safety_t *safety)
{
    if (!safety) return;
    safety->state = R300_SAFETY_DISARMED;
    safety->trip_cause = R300_TRIP_RESET;
    safety->last_heartbeat_ms = 0u;
    safety->heartbeat_seen = false;
    safety->heartbeat_confirmed = false;
    safety->arm_cycle_ready = false;
}

/*
 * STM32 PB8 -> AVR PE4 HEARTBEAT 엣지 하나가 들어올 때마다 호출된다.
 * 직전 엣지가 타임아웃(500 ms) 안에 있었던 경우에만 heartbeat_confirmed를 true로 만든다.
 * 즉 "정상 판정"에는 시간 안에 연속된 엣지 두 개가 필요하다. 리셋 직후 STM32가 LOW를 내보내는
 * 것을 정상 HEARTBEAT로 오인하지 않기 위한 장치다.
 */
void r300_heartbeat_edge(r300_safety_t *safety, uint32_t now_ms)
{
    if (!safety) return;
    safety->heartbeat_confirmed = safety->heartbeat_seen &&
        (uint32_t)(now_ms - safety->last_heartbeat_ms) < R300_HEARTBEAT_TIMEOUT_MS;
    safety->last_heartbeat_ms = now_ms;
    safety->heartbeat_seen = true;
}

/*
 * HEARTBEAT가 현재 정상인지 판정한다: 두 엣지로 확인된 상태이고, 마지막 엣지가 500 ms 이내여야 한다.
 * 시간 차는 uint32_t 뺄셈으로 계산해 밀리초 카운터가 한 바퀴 돌아도(wrap) 올바르게 동작한다.
 */
bool r300_heartbeat_healthy(const r300_safety_t *safety, uint32_t now_ms)
{
    return safety && safety->heartbeat_confirmed &&
        (uint32_t)(now_ms - safety->last_heartbeat_ms) < R300_HEARTBEAT_TIMEOUT_MS;
}

/*
 * 디바운서를 현재 원시(raw) 입력 레벨로 초기화한다. 부팅 시 버튼이 이미 눌려 있어도
 * '방금 눌림 이벤트'가 발생하지 않도록 stable 값을 현재 값으로 맞춘다.
 */
void r300_debounce_init(r300_debounce_t *input, bool raw_pressed, uint32_t now_ms)
{
    if (!input) return;
    input->stable_pressed = raw_pressed;
    input->candidate_pressed = raw_pressed;
    input->candidate_since_ms = now_ms;
}

/*
 * ARM 버튼/E-stop 입력의 채터링을 제거한다. 원시 레벨이 20 ms(R300_INPUT_DEBOUNCE_MS) 동안
 * 변하지 않을 때만 stable_pressed를 바꾸고, 그 순간에만 true(= 상태가 바뀜)를 반환한다.
 * 레벨이 흔들리면 candidate 타이머를 다시 시작한다.
 */
bool r300_debounce_update(r300_debounce_t *input, bool raw_pressed, uint32_t now_ms)
{
    if (!input) return false;
    if (raw_pressed != input->candidate_pressed) {
        input->candidate_pressed = raw_pressed;
        input->candidate_since_ms = now_ms;
        return false;
    }
    if (input->stable_pressed == raw_pressed ||
        (uint32_t)(now_ms - input->candidate_since_ms) < R300_INPUT_DEBOUNCE_MS) return false;
    input->stable_pressed = raw_pressed;
    return true;
}

/*
 * 안전 상태기계 한 스텝(메인 루프에서 매번 호출). 반환값이 PD7 릴레이 명령이다.
 * 릴레이 ON = ARM 허가 AND HEARTBEAT 정상 AND E-stop 비활성 AND 내부 정상.
 * 입력: arm_active(원시 해제 시 즉시 false), arm_released(디바운스된 해제), estop_active, internal_fault.
 * 주의: TRIPPED는 래치되어 스스로 풀리지 않는다. 버튼을 한 번 떼었다가(arm_cycle_ready) 다시 누르는
 * 의도적인 ARM만 TRIPPED에서 빠져나올 수 있다 - 고장이 사라져도 자동 재투입(auto-rearm)은 없다.
 */
bool r300_safety_step(r300_safety_t *safety, uint32_t now_ms, bool arm_active,
                      bool arm_released, bool estop_active, bool internal_fault)
{
    bool arm_request;
    bool heartbeat_ok;
    if (!safety) return false;
    heartbeat_ok = r300_heartbeat_healthy(safety, now_ms);
    arm_request = arm_active && safety->arm_cycle_ready;
    if (!arm_active) {
        if (arm_released) safety->arm_cycle_ready = true;
        if (safety->state == R300_SAFETY_ARMED) {
            safety->state = R300_SAFETY_DISARMED;
            safety->trip_cause = R300_TRIP_NONE;
        }
    } else if (arm_request) {
        /* Consume every press; a rejected attempt needs another release/press cycle. */
        safety->arm_cycle_ready = false;
    }
    if (safety->state != R300_SAFETY_TRIPPED) {
        if (estop_active) {
            safety->state = R300_SAFETY_TRIPPED;
            safety->trip_cause = R300_TRIP_ESTOP;
        } else if (internal_fault) {
            safety->state = R300_SAFETY_TRIPPED;
            safety->trip_cause = R300_TRIP_INTERNAL_FAULT;
        } else if (safety->state == R300_SAFETY_ARMED && !heartbeat_ok) {
            safety->state = R300_SAFETY_TRIPPED;
            safety->trip_cause = R300_TRIP_HEARTBEAT_TIMEOUT;
        }
    }
    if (arm_request && heartbeat_ok && !estop_active && !internal_fault) {
        /* A deliberate ARM request is the only exit from a latched trip. */
        safety->state = R300_SAFETY_ARMED;
        safety->trip_cause = R300_TRIP_NONE;
    }
    return safety->state == R300_SAFETY_ARMED;
}
