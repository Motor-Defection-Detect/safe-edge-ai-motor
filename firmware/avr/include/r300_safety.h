/*
 * 안전 상태기계(r300_safety_t)와 입력 디바운서(r300_debounce_t) 선언. 구현은 firmware/avr/r300_safety.c.
 * 상태/원인 enum과 타임아웃 상수는 생성 파일 r300_link.h에 있다.
 */
#ifndef R300_SAFETY_H_
#define R300_SAFETY_H_

#include <stdbool.h>
#include <stdint.h>

#include "r300_link.h"

typedef struct {
    r300_safety_state_t state;
    r300_trip_cause_t trip_cause;
    /* 마지막 HEARTBEAT 엣지 시각(AVR 밀리초 카운터) */
    uint32_t last_heartbeat_ms;
    bool heartbeat_seen;
    /* 시간 안에 연속 엣지 2개가 확인되었는지 (true여야 정상 HEARTBEAT) */
    bool heartbeat_confirmed;
    /* ARM 버튼이 디바운스되어 해제된 적이 있어야 다음 ARM 요청을 받아들인다 */
    bool arm_cycle_ready;
} r300_safety_t;

typedef struct {
    bool stable_pressed;
    bool candidate_pressed;
    uint32_t candidate_since_ms;
} r300_debounce_t;

void r300_safety_init(r300_safety_t *safety);
void r300_heartbeat_edge(r300_safety_t *safety, uint32_t now_ms);
bool r300_heartbeat_healthy(const r300_safety_t *safety, uint32_t now_ms);
void r300_debounce_init(r300_debounce_t *input, bool raw_pressed, uint32_t now_ms);
/* Returns true only when a raw level has been stable for INPUT_DEBOUNCE_MS. */
bool r300_debounce_update(r300_debounce_t *input, bool raw_pressed, uint32_t now_ms);
/*
 * arm_active drops immediately on a raw release; arm_released becomes true only
 * after that release is debounced. The return value is the PD7 relay command.
 */
bool r300_safety_step(r300_safety_t *safety, uint32_t now_ms, bool arm_active,
                      bool arm_released, bool estop_active, bool internal_fault);

#endif
