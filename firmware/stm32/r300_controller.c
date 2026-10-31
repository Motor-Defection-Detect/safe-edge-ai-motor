/*
 * 이 파일의 역할: STM32의 '프로토콜 위 계층' - 샘플 누적, TELEMETRY 블록 꺼내기, Jetson HEALTH 수신 결과 기록,
 * HEARTBEAT(PB8) 토글 여부 결정을 한곳에 묶는다. 하드웨어(레지스터)는 전혀 모른다.
 * 읽기 순서: 3/13
 * 다음에 볼 곳: firmware/stm32/r300_protocol.c — 이 파일이 호출하는 프레임 패킹/검증/하트비트 규칙의 실체
 * 관련: firmware/stm32/include/r300_controller.h, firmware/stm32/r300_stm32_runtime.c(이 컨트롤러를 사용)
 */
#include "r300_controller.h"

/* 컨트롤러 전체를 0으로 초기화하고 수집 버퍼를 비운다. 시퀀스 번호도 0부터 시작한다. */
void r300_stm32_controller_init(r300_stm32_controller_t *controller)
{
    if (!controller) return;
    *controller = (r300_stm32_controller_t){0};
    r300_acquisition_reset(&controller->acquisition);
}

/*
 * 동기화된 4채널 샘플(가속도 X/Y/Z, 전류) 1개를 블록 버퍼에 추가한다.
 * 블록이 가득 찬 상태에서 또 들어오면 overrun이 래치되고 dropped_blocks를 한 번만 증가시킨다.
 */
bool r300_stm32_sample(r300_stm32_controller_t *controller,
                       const int16_t sample[R300_CHANNELS])
{
    bool accepted;
    bool was_overrun;
    if (!controller) return false;
    was_overrun = controller->acquisition.overrun;
    accepted = r300_acquisition_append(&controller->acquisition, sample);
    if (!accepted && !was_overrun && controller->acquisition.overrun) ++controller->dropped_blocks;
    return accepted;
}

/*
 * 정확히 128샘플이 모인 블록을 TELEMETRY 프레임(1040바이트)으로 패킹해 out에 쓴다.
 * 패킹에 성공했을 때만 시퀀스를 올리고 버퍼를 비운다(실패하면 블록을 보존). overrun이면 거부한다.
 */
bool r300_stm32_take_telemetry(r300_stm32_controller_t *controller, uint8_t *out,
                               size_t capacity, uint32_t timestamp_us,
                               uint16_t flags, size_t *written)
{
    if (!controller || controller->acquisition.overrun ||
        controller->acquisition.sample_count != R300_BLOCK_SAMPLES) return false;
    if (!r300_telemetry_pack(out, capacity, controller->sequence, timestamp_us,
                             controller->acquisition.samples, controller->acquisition.sample_count,
                             flags, written)) return false;
    ++controller->sequence;
    r300_acquisition_reset(&controller->acquisition);
    return true;
}

/*
 * Jetson HEALTH 프레임(16바이트)을 검증해 건강 상태를 갱신한다. 실패하면 invalid_health_frames를 올리고,
 * 그중 CRC 불일치는 invalid_health_crc_frames로 따로 센다(진단 프레임에 노출). 성공 여부를 반환.
 */
bool r300_stm32_keepalive(r300_stm32_controller_t *controller,
                          const uint8_t frame[R300_HEALTH_BYTES], uint32_t received_at_ms)
{
    if (!controller) return false;
    if (r300_health_receive(&controller->health, frame, received_at_ms)) return true;
    if (frame && r300_crc16(frame, R300_HEALTH_BYTES - R300_HEALTH_TRAILER_BYTES) !=
        ((uint16_t)frame[R300_HEALTH_OFFSET_CRC16] |
         (uint16_t)((uint16_t)frame[R300_HEALTH_OFFSET_CRC16 + 1u] << 8)))
        ++controller->invalid_health_crc_frames;
    ++controller->invalid_health_frames;
    return false;
}

/*
 * STM32 진단 프레임에 넣을 컨트롤러 카운터를 스냅샷으로 복사한다.
 * keepalive_valid는 HEALTH가 유효하고 LINK_HEALTHY 비트까지 켜져 있을 때만 true다.
 */
void r300_stm32_diagnostics(const r300_stm32_controller_t *controller,
                            r300_stm32_diagnostics_t *diagnostics)
{
    if (!controller || !diagnostics) return;
    diagnostics->next_sequence = controller->sequence;
    diagnostics->dropped_blocks = controller->dropped_blocks;
    diagnostics->invalid_health_frames = controller->invalid_health_frames;
    diagnostics->invalid_health_crc_frames = controller->invalid_health_crc_frames;
    diagnostics->buffered_samples = controller->acquisition.sample_count;
    diagnostics->keepalive_valid = controller->health.keepalive_valid &&
                                   controller->health.link_healthy;
    diagnostics->acquisition_overrun = controller->acquisition.overrun;
}

/*
 * HEARTBEAT 레벨(PB8로 나갈 값)을 갱신한다. 실제 판단은 r300_heartbeat_step이 하며,
 * true를 반환하면 이번 호출에서 레벨이 토글되었다는 뜻이다. 핀 출력은 호출자(main)가 한다.
 */
bool r300_stm32_heartbeat(r300_stm32_controller_t *controller, bool local_healthy,
                          uint32_t now_ms)
{
    return controller && r300_heartbeat_step(&controller->health, local_healthy, now_ms);
}
