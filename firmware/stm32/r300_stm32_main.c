/*
 * 이 파일의 역할: STM32 진입점과 메인 루프. 한 바퀴마다 AVR 진단 수신 -> Jetson 바이트 수신 -> 전류/진동 샘플 취득 ->
 * HEARTBEAT 갱신 -> 모터 PWM 서비스 -> TELEMETRY/진단 송신 -> PB8 출력 순으로 처리한다(블로킹 없는 폴링 루프).
 * 읽기 순서: 보조 파일 (5/13 firmware/stm32/r300_stm32_runtime.c 가 호출되는 함수들의 실체)
 * 관련: firmware/stm32/r300_stm32_platform.c
 */
#include "r300_stm32_platform.h"
#include "r300_stm32_runtime.h"

/*
 * 플랫폼 초기화에 실패하면 PWM과 HEARTBEAT를 끄고 영구 정지한다(HEARTBEAT 엣지가 끊기므로 AVR이 이를 감지한다).
 * AVR 진단은 SOF 0x5AA5(리틀 엔디언으로 0xA5, 0x5A)를 찾아 16바이트를 모아 처리한다.
 */
int main(void)
{
    r300_stm32_runtime_t runtime;
    uint8_t avr_diag[R300_AVR_DIAGNOSTIC_BYTES];
    uint16_t current = 0u;
    uint16_t avr_diag_bytes = 0u;
    uint32_t last_diagnostic_ms = 0u;
    bool tx_started = false;
    bool calibration_reported = false;

    r300_stm32_runtime_init(&runtime);
    if (!r300_stm32_platform_init()) {
        r300_stm32_pwm_write(0u);
        r300_stm32_heartbeat_write(false);
        for (;;) { }
    }
    r300_stm32_pwm_write(runtime.pwm_compare);

    for (;;) {
        uint8_t byte;
        int16_t xyz[3];
        uint32_t timestamp_us;
        uint32_t now_ms = r300_stm32_millis();

        while (r300_stm32_avr_diag_byte(&byte)) {
            if (avr_diag_bytes == 0u && byte != 0xa5u) continue;
            if (avr_diag_bytes == 1u && byte != 0x5au) {
                avr_diag_bytes = byte == 0xa5u ? 1u : 0u;
                continue;
            }
            avr_diag[avr_diag_bytes++] = byte;
            if (avr_diag_bytes == R300_AVR_DIAGNOSTIC_BYTES) {
                (void)r300_stm32_avr_diag_receive(&runtime, avr_diag, now_ms);
                avr_diag_bytes = 0u;
            }
        }
        (void)r300_stm32_avr_diag_fresh(&runtime, now_ms);

        while (r300_stm32_health_byte(&byte))
            (void)r300_stm32_rx_byte(&runtime, byte, now_ms);

        if (r300_stm32_current_poll(&current)) {
            if (!runtime.current_calibrated)
                (void)r300_stm32_current_zero_sample(&runtime, current);
            else if (runtime.sensor_ready)
                (void)r300_stm32_current_dma(&runtime, current);
        }
        runtime.sensor_ready = runtime.current_calibrated && r300_stm32_platform_healthy();
        (void)r300_stm32_heartbeat(&runtime.controller,
                                   r300_stm32_local_healthy(&runtime), now_ms);
        (void)r300_stm32_motor_service(&runtime, now_ms);
        r300_stm32_pwm_write(runtime.pwm_compare);

        if (r300_stm32_vibration_poll(xyz, &timestamp_us) && runtime.sensor_ready)
            (void)r300_stm32_vibration_isr(&runtime, xyz, timestamp_us);
        (void)r300_stm32_commit_sample(&runtime);

        if (!runtime.tx_pending &&
            runtime.controller.acquisition.sample_count == R300_BLOCK_SAMPLES)
            (void)r300_stm32_prepare_tx(&runtime, r300_stm32_telemetry_flags(&runtime));
        if (!runtime.tx_pending && runtime.current_calibrated &&
            (!calibration_reported ||
             (uint32_t)(now_ms - last_diagnostic_ms) >= R300_STM_DIAGNOSTIC_PERIOD_MS)) {
            r300_stm32_platform_diagnostics_t diagnostics;
            r300_stm32_platform_diagnostics(&diagnostics);
            if (r300_stm32_prepare_diagnostic(&runtime, now_ms, &diagnostics)) {
                last_diagnostic_ms = now_ms;
                calibration_reported = true;
            }
        }
        if (runtime.tx_pending && !tx_started)
            tx_started = r300_stm32_uart_tx_start(runtime.tx_frame, runtime.tx_bytes);
        if (tx_started && !r300_stm32_uart_tx_busy()) {
            tx_started = false;
            r300_stm32_tx_complete(&runtime);
        }

        r300_stm32_heartbeat_write(runtime.controller.health.heartbeat_level);
    }
}
