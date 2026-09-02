from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[2]


def compiler() -> str:
    for name in ("cc", "gcc", "clang"):
        if found := shutil.which(name):
            return found
    windows_clang = Path("C:/Program Files/LLVM/bin/clang.exe")
    if windows_clang.exists():
        return str(windows_clang)
    pytest.fail("a host C compiler is required to execute the firmware logic models")


def run(tmp_path: Path, name: str, source: str, implementations: tuple[Path, ...], include: Path) -> None:
    executable = tmp_path / name
    subprocess.run([compiler(), "-std=c11", "-Wall", "-Wextra", "-Werror", "-I", str(include),
                    "-x", "c", "-", *(str(path) for path in implementations), "-o", str(executable)],
                   input=source, text=True, check=True, capture_output=True)
    subprocess.run([str(executable)], check=True, capture_output=True)


def test_avr_safety_model_trips_and_requires_manual_rearm(tmp_path: Path) -> None:
    run(tmp_path, "avr_safety", """
        #include <assert.h>
        #include \"r300_safety.h\"
        int main(void) {
          r300_safety_t s; r300_safety_init(&s);
          r300_debounce_t button; r300_debounce_init(&button, false, 0);
          assert(!r300_debounce_update(&button, true, 1));
          assert(!r300_debounce_update(&button, true, 20));
          assert(r300_debounce_update(&button, true, 21));
          assert(!r300_debounce_update(&button, false, 22));
          assert(r300_debounce_update(&button, false, 42));
          assert(!button.stable_pressed);
          assert(!r300_safety_step(&s, 0, false, true, false, false));
          assert(s.state == R300_SAFETY_DISARMED && s.trip_cause == R300_TRIP_RESET);
          r300_heartbeat_edge(&s, 10);
          assert(!r300_safety_step(&s, 20, true, false, false, false));
          assert(!r300_safety_step(&s, 30, false, true, false, false));
          r300_heartbeat_edge(&s, 110);
          assert(r300_heartbeat_healthy(&s, 120));
          assert(r300_safety_step(&s, 120, true, false, false, false));
          assert(!r300_safety_step(&s, 121, false, true, false, false));
          assert(s.state == R300_SAFETY_DISARMED);
          assert(r300_safety_step(&s, 122, true, false, false, false));
          assert(!r300_safety_step(&s, 123, true, false, true, false));
          assert(s.state == R300_SAFETY_TRIPPED);
          assert(!r300_safety_step(&s, 600, true, false, false, false));
          assert(s.trip_cause == R300_TRIP_ESTOP);
          r300_heartbeat_edge(&s, 610);
          assert(!r300_safety_step(&s, 620, true, false, false, false));
          assert(!r300_safety_step(&s, 625, false, false, false, false));
          assert(!r300_safety_step(&s, 626, true, false, false, false));
          assert(!r300_safety_step(&s, 630, false, true, false, false));
          r300_heartbeat_edge(&s, 710);
          assert(r300_safety_step(&s, 720, true, false, false, false));
          assert(!r300_safety_step(&s, 1210, true, false, false, false));
          assert(!r300_heartbeat_healthy(&s, 1210));
          assert(s.trip_cause == R300_TRIP_HEARTBEAT_TIMEOUT);
          return 0;
        }
    """, (ROOT / "firmware/avr/r300_safety.c",), ROOT / "firmware/avr/include")


def test_avr_diagnostic_frame_is_fixed_crc_protected_and_bounded(tmp_path: Path) -> None:
    run(tmp_path, "avr_diagnostic", """
        #include <assert.h>
        #include "r300_avr_diagnostics.h"
        int main(void) {
          uint8_t frame[R300_AVR_DIAGNOSTIC_BYTES];
          uint16_t flags = R300_AVR_DIAGNOSTIC_ESTOP_ACTIVE |
                           R300_AVR_DIAGNOSTIC_INTERNAL_HEALTHY;
          assert(r300_avr_diagnostic_pack(frame, 7, 123, R300_SAFETY_TRIPPED,
                                          R300_TRIP_ESTOP, flags));
          assert(frame[0] == 0xa5 && frame[1] == 0x5a);
          assert(frame[R300_AVR_DIAGNOSTIC_OFFSET_VERSION_FLAGS + 1] == flags);
          assert(frame[R300_AVR_DIAGNOSTIC_OFFSET_SEQUENCE] == 7);
          assert(frame[R300_AVR_DIAGNOSTIC_OFFSET_STATE_CAUSE] == R300_SAFETY_TRIPPED);
          assert(frame[R300_AVR_DIAGNOSTIC_OFFSET_STATE_CAUSE + 1] == R300_TRIP_ESTOP);
          assert(r300_crc16(frame, R300_AVR_DIAGNOSTIC_HEADER_BYTES) ==
                 ((uint16_t)frame[14] | (uint16_t)((uint16_t)frame[15] << 8)));
          assert(!r300_avr_diagnostic_pack(frame, 0, 0, (r300_safety_state_t)3,
                                           R300_TRIP_NONE, 0));
          assert(!r300_avr_diagnostic_pack(frame, 0, 0, R300_SAFETY_ARMED,
                                           R300_TRIP_ESTOP, 0));
          assert(!r300_avr_diagnostic_pack(frame, 0, 0, R300_SAFETY_DISARMED,
                                           R300_TRIP_NONE, 0x20));
          return 0;
        }
    """, (ROOT / "firmware/avr/r300_avr_diagnostics.c",), ROOT / "firmware/avr/include")


def test_stm32_protocol_model_requires_valid_keepalive_for_heartbeat(tmp_path: Path) -> None:
    run(tmp_path, "stm32_protocol", """
        #include <assert.h>
        #include <string.h>
        #include \"r300_protocol.h\"
        int main(void) {
          uint8_t health[R300_HEALTH_BYTES] = {0}; r300_stm32_health_t s = {0};
          uint8_t telemetry[R300_TELEMETRY_BYTES(1)]; size_t written = 0;
          r300_acquisition_t a; int16_t sample[R300_CHANNELS] = {1, 2, 3, 4};
          r300_acquisition_reset(&a);
          assert(!r300_telemetry_pack(telemetry, sizeof(telemetry), 0, 0,
                                      sample, 0, 0, &written));
          assert(!r300_telemetry_pack(telemetry, sizeof(telemetry), 0, 0,
                                      sample, 1, 0x100, &written));
          assert(!r300_telemetry_pack(telemetry, sizeof(telemetry), 0, 0,
                                      sample, 1, R300_TELEMETRY_AVR_ARMED, &written));
          assert(!r300_telemetry_pack(telemetry, sizeof(telemetry), 0, 0,
                                      sample, 1,
                                      R300_TELEMETRY_SENSOR_READY |
                                      R300_TELEMETRY_LOCAL_FAULT |
                                      R300_TELEMETRY_MOTOR_ACTIVE, &written));
          assert(r300_telemetry_pack(telemetry, sizeof(telemetry), 0, 0,
                                     sample, 1,
                                     R300_TELEMETRY_SENSOR_READY |
                                     R300_TELEMETRY_MOTOR_ACTIVE |
                                     R300_TELEMETRY_AVR_DIAG_VALID |
                                     R300_TELEMETRY_AVR_TRIPPED |
                                     R300_TELEMETRY_AVR_ESTOP, &written));
          assert(r300_acquisition_append(&a, sample));
          assert(a.sample_count == 1 && a.samples[3] == 4);
          health[0] = 0x55; health[1] = 0xaa; health[2] = R300_PROTOCOL_VERSION;
          uint16_t crc = r300_crc16(health, R300_HEALTH_BYTES - 2u);
          health[14] = (uint8_t)crc; health[15] = (uint8_t)(crc >> 8);
          assert(!r300_health_receive(&s, health, 4));
          health[R300_HEALTH_OFFSET_HEALTH_FLAGS] = R300_HEALTH_APP_ALIVE;
          health[R300_HEALTH_OFFSET_VERSION_FLAGS + 1] = 1;
          crc = r300_crc16(health, R300_HEALTH_BYTES - 2u);
          health[14] = (uint8_t)crc; health[15] = (uint8_t)(crc >> 8);
          assert(!r300_health_receive(&s, health, 5));
          health[R300_HEALTH_OFFSET_VERSION_FLAGS + 1] = 0;
          health[R300_HEALTH_OFFSET_HEALTH_FLAGS] |= 0x20;
          crc = r300_crc16(health, R300_HEALTH_BYTES - 2u);
          health[14] = (uint8_t)crc; health[15] = (uint8_t)(crc >> 8);
          assert(!r300_health_receive(&s, health, 5));
          health[R300_HEALTH_OFFSET_HEALTH_FLAGS] = R300_HEALTH_APP_ALIVE;
          crc = r300_crc16(health, R300_HEALTH_BYTES - 2u);
          health[14] = (uint8_t)crc; health[15] = (uint8_t)(crc >> 8);
          assert(r300_health_receive(&s, health, 5));
          assert(s.keepalive_valid && !s.link_healthy);
          assert(!r300_heartbeat_step(&s, true, 100));
          health[R300_HEALTH_OFFSET_SEQUENCE] = 1;
          health[R300_HEALTH_OFFSET_HEALTH_FLAGS] |= R300_HEALTH_LINK_HEALTHY;
          crc = r300_crc16(health, R300_HEALTH_BYTES - 2u);
          health[14] = (uint8_t)crc; health[15] = (uint8_t)(crc >> 8);
          assert(r300_health_receive(&s, health, 101));
          assert(!r300_health_receive(&s, health, 102));
          assert(r300_heartbeat_step(&s, true, 200));
          assert(!r300_heartbeat_step(&s, true, 402));
          assert(!s.heartbeat_level);
          assert(r300_health_receive(&s, health, 403));
          return 0;
        }
    """, (ROOT / "firmware/stm32/r300_protocol.c",), ROOT / "firmware/stm32/include")


def test_stm32_controller_retains_full_block_until_a_frame_is_packed(tmp_path: Path) -> None:
    run(tmp_path, "stm32_controller", """
        #include <assert.h>
        #include \"r300_controller.h\"
        int main(void) {
          r300_stm32_controller_t c; uint8_t frame[R300_TELEMETRY_BYTES(R300_BLOCK_SAMPLES)];
          uint8_t bad_health[R300_HEALTH_BYTES] = {0}; r300_stm32_diagnostics_t diagnostics;
          int16_t sample[R300_CHANNELS] = {1, 2, 3, 4}; size_t written = 0;
          r300_stm32_controller_init(&c);
          assert(!r300_stm32_keepalive(&c, bad_health, 0));
          r300_stm32_diagnostics(&c, &diagnostics);
          assert(diagnostics.invalid_health_frames == 1 && !diagnostics.keepalive_valid);
          assert(diagnostics.invalid_health_crc_frames == 1 && !diagnostics.acquisition_overrun);
          for (unsigned i = 0; i < R300_BLOCK_SAMPLES; ++i) assert(r300_stm32_sample(&c, sample));
          assert(!r300_stm32_take_telemetry(&c, frame, sizeof(frame) - 1u, 42, 0, &written));
          assert(c.acquisition.sample_count == R300_BLOCK_SAMPLES && c.sequence == 0);
          assert(r300_stm32_take_telemetry(&c, frame, sizeof(frame), 42, 0, &written));
          assert(written == sizeof(frame) && c.acquisition.sample_count == 0 && c.sequence == 1);
          for (unsigned i = 0; i < R300_BLOCK_SAMPLES; ++i) assert(r300_stm32_sample(&c, sample));
          assert(!r300_stm32_sample(&c, sample));
          assert(!r300_stm32_sample(&c, sample));
          assert(c.dropped_blocks == 1);
          r300_stm32_diagnostics(&c, &diagnostics);
          assert(diagnostics.acquisition_overrun);
          return 0;
        }
    """, (ROOT / "firmware/stm32/r300_protocol.c", ROOT / "firmware/stm32/r300_controller.c"),
       ROOT / "firmware/stm32/include")


def test_stm32_platform_conversions_are_bounded_and_calibrated(tmp_path: Path) -> None:
    run(tmp_path, "stm32_board_config", """
        #include <assert.h>
        #include \"r300_board_config.h\"
        int main(void) {
          assert(r300_current_milliamps(2000, 2000) == 0);
          assert(r300_current_milliamps(3000, 2000) == 5000);
          assert(r300_current_milliamps(1000, 2000) == -5000);
          assert(r300_current_milliamps(4095, 0) == 5000);
          assert(r300_current_milliamps(0, 4095) == -5000);
          assert(r300_motor_pwm_compare(0) == 0);
          assert(r300_motor_pwm_compare(500) == 24000);
          assert(r300_motor_pwm_compare(1001) == 48000);
          return 0;
        }
    """, (ROOT / "firmware/stm32/r300_board_config.c",), ROOT / "firmware/stm32/include")


def test_stm32_runtime_calibrates_commits_and_retains_async_tx(tmp_path: Path) -> None:
    run(tmp_path, "stm32_runtime", """
        #include <assert.h>
        #include \"r300_stm32_runtime.h\"
        static void put16(uint8_t *out, uint16_t value) {
          out[0] = (uint8_t)value; out[1] = (uint8_t)(value >> 8);
        }
        static void put32(uint8_t *out, uint32_t value) {
          for (unsigned i = 0; i < 4; ++i) out[i] = (uint8_t)(value >> (8u * i));
        }
        static void diagnostic(uint8_t *frame, uint32_t sequence,
                               r300_safety_state_t state, r300_trip_cause_t cause,
                               uint16_t flags) {
          put16(frame + R300_AVR_DIAGNOSTIC_OFFSET_SOF, R300_AVR_DIAGNOSTIC_SOF);
          put16(frame + R300_AVR_DIAGNOSTIC_OFFSET_VERSION_FLAGS,
                R300_PROTOCOL_VERSION | (uint16_t)(flags << 8));
          put32(frame + R300_AVR_DIAGNOSTIC_OFFSET_SEQUENCE, sequence);
          put32(frame + R300_AVR_DIAGNOSTIC_OFFSET_TIMESTAMP_MS, 50);
          put16(frame + R300_AVR_DIAGNOSTIC_OFFSET_STATE_CAUSE,
                (uint16_t)state | (uint16_t)((uint16_t)cause << 8));
          put16(frame + R300_AVR_DIAGNOSTIC_OFFSET_CRC16,
                r300_crc16(frame, R300_AVR_DIAGNOSTIC_HEADER_BYTES));
        }
        static void motor_command(uint8_t *frame, uint32_t sequence, uint16_t duty,
                                  r300_motor_direction_t direction) {
          put16(frame + R300_MOTOR_COMMAND_OFFSET_SOF, R300_MOTOR_COMMAND_SOF);
          put16(frame + R300_MOTOR_COMMAND_OFFSET_VERSION_FLAGS, R300_PROTOCOL_VERSION);
          put32(frame + R300_MOTOR_COMMAND_OFFSET_SEQUENCE, sequence);
          put32(frame + R300_MOTOR_COMMAND_OFFSET_TIMESTAMP_MS, 50);
          put16(frame + R300_MOTOR_COMMAND_OFFSET_DUTY_PERMILLE, duty);
          put16(frame + R300_MOTOR_COMMAND_OFFSET_DIRECTION, (uint16_t)direction);
          put16(frame + R300_MOTOR_COMMAND_OFFSET_CRC16,
                r300_crc16(frame, R300_MOTOR_COMMAND_HEADER_BYTES));
        }
        int main(void) {
          r300_stm32_runtime_t r; int16_t xyz[3] = {10, 20, 30};
          r300_stm32_platform_diagnostics_t platform = {0};
          uint8_t diag[R300_AVR_DIAGNOSTIC_BYTES];
          uint8_t command[R300_MOTOR_COMMAND_BYTES];
          r300_stm32_runtime_init(&r);
          assert(!r300_stm32_set_motor(&r, 100));
          for (unsigned i = 0; i < R300_CURRENT_ZERO_SAMPLES; ++i)
            assert(r300_stm32_current_zero_sample(&r, 1000) ==
                   (i + 1u == R300_CURRENT_ZERO_SAMPLES));
          r.sensor_ready = true;
          assert(r300_stm32_current_dma(&r, 1999));
          assert(r300_stm32_current_dma(&r, 2000));
          assert(r.sensor_overruns == 0);
          for (unsigned i = 0; i < R300_BLOCK_SAMPLES; ++i) {
            assert(r300_stm32_vibration_isr(&r, xyz, i));
            if (i != 0) assert(r300_stm32_current_dma(&r, 2000));
            assert(r300_stm32_commit_sample(&r));
          }
          assert(r300_stm32_local_healthy(&r));
          assert(r300_stm32_telemetry_flags(&r) == R300_TELEMETRY_SENSOR_READY);
          assert(r300_stm32_prepare_tx(&r, r300_stm32_telemetry_flags(&r)));
          assert(r.tx_pending && r.tx_bytes == sizeof(r.tx_frame));
          assert(r.tx_frame[3] == R300_TELEMETRY_SENSOR_READY);
          assert(r.tx_frame[8] == 0 && r.tx_frame[9] == 0);
          assert(!r300_stm32_prepare_tx(&r, 0) && r.uart_busy_events == 1);
          r300_stm32_tx_complete(&r);
          assert(!r.tx_pending && r.tx_bytes == 0);
          r.controller.health.keepalive_valid = true;
          r.controller.health.link_healthy = true;
          platform.sensor_bus_errors = 3;
          assert(r300_stm32_prepare_diagnostic(&r, 1000, &platform));
          assert(r.tx_pending && r.tx_bytes == R300_STM_DIAGNOSTIC_BYTES);
          assert(r.tx_frame[R300_STM_DIAGNOSTIC_OFFSET_SEQUENCE] == 0);
          assert(r.tx_frame[R300_STM_DIAGNOSTIC_OFFSET_TELEMETRY_SEQUENCE] == 1);
          assert(r.tx_frame[R300_STM_DIAGNOSTIC_OFFSET_SENSOR_BUS_ERRORS] == 3);
          assert(((uint16_t)r.tx_frame[R300_STM_DIAGNOSTIC_OFFSET_CURRENT_ZERO_COUNT] |
                  (uint16_t)((uint16_t)r.tx_frame[R300_STM_DIAGNOSTIC_OFFSET_CURRENT_ZERO_COUNT + 1] << 8)) == 1000);
          assert(r.tx_frame[R300_STM_DIAGNOSTIC_OFFSET_STATUS_FLAGS] ==
                 R300_STM_DIAGNOSTIC_KEEPALIVE_VALID);
          assert(r300_crc16(r.tx_frame, R300_STM_DIAGNOSTIC_HEADER_BYTES) ==
                 ((uint16_t)r.tx_frame[R300_STM_DIAGNOSTIC_OFFSET_CRC16] |
                  (uint16_t)((uint16_t)r.tx_frame[R300_STM_DIAGNOSTIC_OFFSET_CRC16 + 1] << 8)));
          r300_stm32_tx_complete(&r);
          assert(r300_stm32_set_motor(&r, 1200) && r.pwm_compare == R300_MOTOR_PWM_PERIOD_TICKS);
          assert((r300_stm32_telemetry_flags(&r) & R300_TELEMETRY_MOTOR_ACTIVE) != 0);
          r.sensor_overruns = 1;
          assert((r300_stm32_telemetry_flags(&r) & R300_TELEMETRY_LOCAL_FAULT) != 0);
          assert(!r300_stm32_set_motor(&r, 100));
          assert(r300_stm32_set_motor(&r, 0) && r.pwm_compare == 0);
          diagnostic(diag, 7, R300_SAFETY_ARMED, R300_TRIP_NONE,
                     R300_AVR_DIAGNOSTIC_HEARTBEAT_HEALTHY |
                     R300_AVR_DIAGNOSTIC_ARM_ACTIVE |
                     R300_AVR_DIAGNOSTIC_INTERNAL_HEALTHY |
                     R300_AVR_DIAGNOSTIC_RELAY_COMMAND);
          assert(r300_stm32_avr_diag_receive(&r, diag, 100));
          assert((r300_stm32_telemetry_flags(&r) &
                  (R300_TELEMETRY_AVR_DIAG_VALID | R300_TELEMETRY_AVR_ARMED)) ==
                 (R300_TELEMETRY_AVR_DIAG_VALID | R300_TELEMETRY_AVR_ARMED));
          assert(r300_stm32_prepare_diagnostic(&r, 100, &platform));
          assert((r.tx_frame[R300_STM_DIAGNOSTIC_OFFSET_STATUS_FLAGS] &
                  (R300_STM_DIAGNOSTIC_AVR_DIAG_VALID |
                   R300_STM_DIAGNOSTIC_AVR_HEARTBEAT_HEALTHY |
                   R300_STM_DIAGNOSTIC_AVR_ARM_ACTIVE)) ==
                 (R300_STM_DIAGNOSTIC_AVR_DIAG_VALID |
                  R300_STM_DIAGNOSTIC_AVR_HEARTBEAT_HEALTHY |
                  R300_STM_DIAGNOSTIC_AVR_ARM_ACTIVE));
          assert((r.tx_frame[R300_STM_DIAGNOSTIC_OFFSET_STATUS_FLAGS + 1] & 0x03u) == 0x03u);
          r300_stm32_tx_complete(&r);
          assert(!r300_stm32_avr_diag_receive(&r, diag, 101));
          assert(r.invalid_avr_diag_frames == 1);
          assert(r300_stm32_avr_diag_fresh(&r, 400));
          assert(!r300_stm32_avr_diag_fresh(&r, 401));
          diagnostic(diag, 0, R300_SAFETY_TRIPPED, R300_TRIP_ESTOP,
                     R300_AVR_DIAGNOSTIC_ESTOP_ACTIVE |
                     R300_AVR_DIAGNOSTIC_INTERNAL_HEALTHY);
          assert(r300_stm32_avr_diag_receive(&r, diag, 500));
          assert((r300_stm32_telemetry_flags(&r) &
                  (R300_TELEMETRY_AVR_TRIPPED | R300_TELEMETRY_AVR_ESTOP)) ==
                 (R300_TELEMETRY_AVR_TRIPPED | R300_TELEMETRY_AVR_ESTOP));
          diagnostic(diag, 1, R300_SAFETY_ARMED, R300_TRIP_ESTOP,
                     R300_AVR_DIAGNOSTIC_HEARTBEAT_HEALTHY |
                     R300_AVR_DIAGNOSTIC_ARM_ACTIVE |
                     R300_AVR_DIAGNOSTIC_INTERNAL_HEALTHY |
                     R300_AVR_DIAGNOSTIC_RELAY_COMMAND);
          assert(!r300_stm32_avr_diag_receive(&r, diag, 501));
          diag[14] ^= 1;
          assert(!r300_stm32_avr_diag_receive(&r, diag, 502));
          r.sensor_overruns = 0;
          r.controller.health.keepalive_valid = true;
          r.controller.health.link_healthy = true;
          motor_command(command, 9, 500, R300_MOTOR_FORWARD);
          assert(!r300_stm32_rx_byte(&r, 0xff, 600));
          assert(!r300_stm32_rx_byte(&r, 0x3c, 600));
          assert(!r300_stm32_rx_byte(&r, 0x3c, 600));
          for (unsigned i = 1; i + 1 < R300_MOTOR_COMMAND_BYTES; ++i)
            assert(!r300_stm32_rx_byte(&r, command[i], 600));
          assert(r300_stm32_rx_byte(&r, command[R300_MOTOR_COMMAND_BYTES - 1], 600));
          assert(r300_stm32_motor_service(&r, 600));
          assert(r.pwm_duty_permille == 500 && r.pwm_compare == 24000);
          r.controller.health.link_healthy = false;
          assert(r300_stm32_motor_service(&r, 601) && r.pwm_duty_permille == 0);
          r.controller.health.link_healthy = true;
          assert(!r300_stm32_motor_command_receive(&r, command, 601));
          assert(r300_stm32_motor_service(&r, 900));
          assert(r300_stm32_motor_service(&r, 901) && r.pwm_duty_permille == 0);
          motor_command(command, 0, 1, R300_MOTOR_STOP);
          assert(!r300_stm32_motor_command_receive(&r, command, 1000));
          return 0;
        }
    """, (ROOT / "firmware/stm32/r300_protocol.c", ROOT / "firmware/stm32/r300_controller.c",
            ROOT / "firmware/stm32/r300_board_config.c", ROOT / "firmware/stm32/r300_stm32_runtime.c"),
       ROOT / "firmware/stm32/include")
