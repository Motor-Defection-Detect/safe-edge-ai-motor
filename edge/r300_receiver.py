# ==============================================================================
# edge/r300_receiver.py
# 역할: UART 를 읽고 100ms 마다 HEALTH / MOTOR_COMMAND 를 내보내는 직렬 워커(SerialReceiver)와,
#       추론을 UART 스레드에서 떼어내는 유한 큐 워커(InferenceWorker). 시리얼 장치/모델 경로는 모른 채
#       주입받은 포트와 콜백만 사용한다.
# 읽기 순서: 10/13  (앞: 9 r300_analysis.py, runtime.py 의 ReceiverService 를 감싸서 사용)
# 다음에 볼 곳: edge/r300_application.py — 이 객체들을 한 곳에서 조립하고 스레드를 띄우는 곳
# 먼저 읽을 클래스: InferenceWorker(짧음) -> SerialReceiver.poll(길다; 위쪽 [구간 지도] 참고)
# 데이터 흐름: SerialReceiver.poll -> ReceiverService.decode -> InferenceWorker.submit(큐) -> process -> presenter
#              반대 방향: poll 이 100ms 마다 HEALTH, MOTOR_COMMAND 를 포트에 쓴다.
# 핵심 안전 설계: LINK_HEALTHY 는 fail-closed. 추론 100ms 정지, 워커 실패, Qt 하트비트 300ms 단절,
#                 텔레메트리 128ms 초과 중 하나라도 있으면 다음 HEALTH 에서 꺼지고 STM32 가 하트비트/PWM 을 멈춘다.
#                 AI 판정(WARNING)은 이 경로에 영향이 없다 - AI 에는 릴레이 권한이 없다.
# ==============================================================================

"""Jetson serial worker; the caller supplies the actual serial port."""
from __future__ import annotations

from collections import deque
from collections.abc import Callable
import math
from queue import Empty, Full, Queue
from threading import Event, Lock
from time import perf_counter_ns
from typing import Protocol

from r300_link import ReceiverService, Telemetry, WindowResult, motor_command_frame
from r300_link import link_contract as lc


# 추론 대기 큐 크기(블록 수). 블록 128ms x 3 에 비정상 4 hop 과 추론 30ms 를 더해도 약 1.438s 라서
# 경고 목표 1.5s 안에 든다(docs/interface/r300-platform-decisions.md). 넘치면 쌓인 것을 버리고 창을 초기화한다.
INFERENCE_QUEUE_BLOCKS = 3


# 시리얼 포트가 만족해야 할 최소 인터페이스(read/write). 실제는 pyserial, 데모/테스트는 가짜 포트를 넣어 장치 없이 돌린다.
class SerialPort(Protocol):
    def read(self, size: int) -> bytes: ...
    def write(self, data: bytes) -> int: ...


# pyserial 포트를 연다: 921600 8N1, 읽기/쓰기 모두 유한 timeout. timeout 이 필수인 이유는 read 가 영원히 막히면
# stop 신호를 확인하지 못해 워커가 끝나지 않기 때문(R300Application.stop 참고). pyserial 은 함수 안에서 import 한다.
def open_serial_port(device: str, timeout_seconds: float = 0.05) -> SerialPort:
    if not device or not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0:
        raise ValueError("a device path and positive finite timeout are required")
    import serial
    return serial.Serial(port=device, baudrate=lc.BAUD, bytesize=serial.EIGHTBITS,
                         parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                         timeout=timeout_seconds, write_timeout=timeout_seconds)


# UART 스레드와 추론 스레드를 분리하는 '유한 큐' 핸드오프. 추론이 느려도 UART 읽기가 막혀 데이터를 놓치지 않게 한다.
# 큐가 차면 쌓인 블록을 전부 버리고(queue_drops) 새 블록에 reset 표시를 달아 창을 초기화한다(끊긴 데이터에 걸친 창 방지).
# fail-closed 신호 두 개: stalled()(콜백이 100ms 이상 진행 중), failed(콜백/표시 예외 래치).
# 둘 다 SerialReceiver 가 LINK_HEALTHY 를 끄는 근거가 된다. 다음: SerialReceiver.poll 이 submit() 을 부른다.
class InferenceWorker:
    """Bounded handoff from validated UART blocks to preprocessing/inference."""

    def __init__(self, service: ReceiverService,
                 on_windows: Callable[[tuple[WindowResult, ...], str], None],
                 capacity: int = INFERENCE_QUEUE_BLOCKS) -> None:
        if capacity <= 0:
            raise ValueError("inference queue capacity must be positive")
        self._service, self._on_windows = service, on_windows
        self._queue: Queue[tuple[Telemetry, str, bool]] = Queue(maxsize=capacity)
        self._state_lock = Lock()
        self._processing_started_ns: int | None = None
        self.queue_drops = 0
        self.callback_errors = 0
        self.failed = False

    # UART 스레드가 호출. 가득 차면 큐를 비우고 그 수를 queue_drops 에 더한 뒤 새 항목을 reset=True 로 넣고 inference_queue_overrun 을 기록한다.
    def submit(self, telemetries: tuple[Telemetry, ...], timestamp: str) -> None:
        for telemetry in telemetries:
            try:
                self._queue.put_nowait((telemetry, timestamp, False))
            except Full:
                dropped = 0
                try:
                    while True:
                        self._queue.get_nowait()
                        dropped += 1
                except Empty:
                    pass
                self.queue_drops += dropped
                self._queue.put_nowait((telemetry, timestamp, dropped > 0))
                if dropped and self._service.logger:
                    self._service.logger.write(timestamp, "inference_queue_overrun",
                                               dropped_blocks=dropped)

    @property
    # 현재 큐에 대기 중인 블록 수(메트릭용).
    def queue_depth(self) -> int:
        return self._queue.qsize()

    # 지금 처리 중인 콜백이 INFERENCE_WORKER_STALL_MS(100ms) 이상 걸리면 True. 이것은 '비정상 분류'가 아니라
    # 애플리케이션 정지로 취급하며, 다음 HEALTH 에서 LINK_HEALTHY 를 끈다. 처리 중이 아니면 항상 False.
    def stalled(self, now_ns: int | None = None) -> bool:
        with self._state_lock:
            started_ns = self._processing_started_ns
        return (started_ns is not None and
                (perf_counter_ns() if now_ns is None else now_ns) - started_ns >=
                lc.INFERENCE_WORKER_STALL_MS * 1_000_000)

    # 추론 스레드 본체. 큐에서 하나씩 꺼내 process -> on_windows(프레젠터) 순으로 호출한다. 시작/종료 시각을
    # _processing_started_ns 에 기록해 stalled() 가 볼 수 있게 한다. 예외는 callback_errors / failed 로 남기고 로그
    # (inference_worker_error / presentation_error)를 쓴 뒤 루프를 계속한다. stop 이 설정돼도 큐가 빌 때까지 처리한다.
    def run(self, stop: Event) -> None:
        while not stop.is_set() or not self._queue.empty():
            try:
                telemetry, timestamp, reset = self._queue.get(timeout=0.05)
            except Empty:
                continue
            with self._state_lock:
                self._processing_started_ns = perf_counter_ns()
            try:
                if reset:
                    self._service.pipeline.reset_window()
                try:
                    result = self._service.process(telemetry, timestamp)
                except Exception as error:
                    self.callback_errors += 1
                    self.failed = True
                    if self._service.logger:
                        self._service.logger.write(timestamp, "inference_worker_error",
                                                   error=str(error), sequence=telemetry.sequence)
                    continue
                if result is not None:
                    try:
                        self._on_windows((result,), timestamp)
                    except Exception as error:
                        self.callback_errors += 1
                        self.failed = True
                        if self._service.logger:
                            self._service.logger.write(timestamp, "presentation_error",
                                                       error=str(error),
                                                       sequence=telemetry.sequence)
            finally:
                with self._state_lock:
                    self._processing_started_ns = None


# UART 폴링 + 100ms 주기 HEALTH/MOTOR_COMMAND 송신 + 메트릭 집계를 맡는 워커. 포트와 모델 경로는 모른다.
# telemetry_link_healthy 는 (Qt 하트비트 정상) and (추론 정지 없음) and (워커 실패 없음) and (마지막 유효 텔레메트리가 128ms 이내)
# 일 때만 True 이며, 하나라도 깨지면 HEALTH 의 LINK_HEALTHY 가 꺼져 STM32 가 PB8 하트비트와 PWM 을 멈춘다.
# 이후 AVR 이 하트비트 끊김을 감지하면 릴레이를 끊는다(릴레이 권한은 AVR 에만 있음).
# 다음: edge/r300_application.py 가 이 객체를 만들고 전용 스레드에서 run() 을 돌린다.
class SerialReceiver:
    """Own UART polling and health cadence without owning a device path or model."""

    def __init__(self, port: SerialPort, service: ReceiverService, *,
                 model_loaded: Callable[[], bool], storage_ok: Callable[[], bool],
                 application_ok: Callable[[], bool] | None = None,
                 on_windows: Callable[[tuple[WindowResult, ...], str], None] | None = None,
                 on_metrics: Callable[[dict[str, float]], None] | None = None,
                 inference_worker: InferenceWorker | None = None,
                 health_period_ms: int = lc.HEARTBEAT_TOGGLE_MS) -> None:
        # 송신 주기는 계약 주기(MOTOR_COMMAND_PERIOD_MS=100ms) 이하만 허용한다. 모터 요청 기본값은 STOP.
        if not 0 < health_period_ms <= lc.MOTOR_COMMAND_PERIOD_MS:
            raise ValueError("health/command period must be positive and at most the contract period")
        self._port, self._service = port, service
        self._model_loaded, self._storage_ok = model_loaded, storage_ok
        self._application_ok = application_ok or (lambda: True)
        self._on_windows, self._on_metrics = on_windows, on_metrics
        self._inference_worker = inference_worker
        self._health_period_ms = health_period_ms
        self._last_health_ms: int | None = None
        self.serial_errors = 0
        self.health_state_errors = 0
        self.metrics_callback_errors = 0
        self.connected = False
        self._model_loaded_state = False
        self._storage_ok_state = False
        self._application_ok_state = True
        self._last_telemetry_ms: int | None = None
        self._last_stm_diagnostic_ms: int | None = None
        self._last_metrics_log_ms: int | None = None
        self._inference_stall_latched = False
        self._sample_points: deque[tuple[int, int]] = deque(maxlen=128)
        self._last_telemetry_flags = 0
        self._motor_request = (lc.MOTOR_DIRECTIONS["STOP"], 0)

    # 모터 PWM 요청을 저장한다(듀티 0이면 STOP, 아니면 FORWARD). 호출자 경계에서 motor_command_frame 으로 먼저 검증하므로
    # 잘못된 값은 여기서 ValueError. 실제 전송은 poll() 의 100ms 주기에서 한다.
    def set_motor_command(self, duty_permille: int) -> None:
        direction = (lc.MOTOR_DIRECTIONS["FORWARD"] if duty_permille
                     else lc.MOTOR_DIRECTIONS["STOP"])
        # Validate at the caller boundary before the serial worker owns the request.
        motor_command_frame(0, 0, duty_permille, direction)
        self._motor_request = (direction, duty_permille)

    # [구간 지도] poll() — 한 번 호출 = UART 한 번 읽기 + (주기가 되면) 송신 + 메트릭. 전용 스레드에서 반복 호출된다.
    #  1) 시작 시각 기록, 포트에서 최대 4096바이트 읽기(OSError 면 연결 끊김 처리)
    #  2) 디코드 -> Telemetry; 마지막 수신 시각/플래그 갱신, 샘플레이트 관측점(시퀀스, timestamp_us) 갱신
    #  3) 추론 워커에 전달(없으면 직접 process), STM 진단 수신 시각 갱신, 표시 콜백
    #  4) LINK_HEALTHY 판정: 추론 정지 래치, 워커 실패, Qt 하트비트(application_ok), 텔레메트리 나이
    #  5) health 주기가 되면 model_loaded / storage_ok 평가 -> HEALTH 쓰기 -> MOTOR_COMMAND 쓰기
    #  6) 메트릭 dict 조립 -> on_metrics 콜백, 로그는 1초에 한 번
    def poll(self, timestamp: str, now_ms: int) -> tuple[WindowResult, ...]:
        """Process one bounded UART read; suitable for a dedicated worker thread."""
        # --- 구간 1: 포트 읽기 ---
        started_ns = perf_counter_ns()
        try:
            data = self._port.read(4096)
            self._set_connected(True, timestamp)
        except OSError:
            self.serial_errors += 1
            self._set_connected(False, timestamp)
            data = b""
        # --- 구간 2: 디코드, 수신 시각/플래그/샘플레이트 관측점 갱신 ---
        diagnostic_frames = self._service.parser.diagnostic_frames
        telemetries = self._service.decode(data, timestamp)
        if telemetries:
            self._last_telemetry_flags = telemetries[-1].flags
            self._last_telemetry_ms = now_ms
            for telemetry in telemetries:
                # 시퀀스가 뒤로 가고 timestamp 도 줄면 STM32 재시작으로 보고 샘플레이트 관측점을 초기화한다. 128샘플이 아닌 블록도 초기화.
                restarted = bool(self._sample_points and
                                 (telemetry.sequence - self._sample_points[-1][0]) & 0xffffffff >=
                                 0x80000000 and
                                 telemetry.timestamp_us < self._sample_points[-1][1])
                if restarted or len(telemetry.samples) != lc.BLOCK_SAMPLES:
                    self._sample_points.clear()
                if len(telemetry.samples) == lc.BLOCK_SAMPLES:
                    self._sample_points.append((telemetry.sequence, telemetry.timestamp_us))
        # --- 구간 3: 추론 워커로 전달(또는 직접 처리), 진단 수신 시각, 표시 콜백 ---
        if self._inference_worker:
            self._inference_worker.submit(telemetries, timestamp)
            windows = ()
        else:
            windows = tuple(result for telemetry in telemetries
                            if (result := self._service.process(telemetry, timestamp)) is not None)
        if self._service.parser.diagnostic_frames != diagnostic_frames:
            self._last_stm_diagnostic_ms = now_ms
        if self._on_windows and windows:
            self._on_windows(windows, timestamp)
        # --- 구간 4: LINK_HEALTHY 판정 ---
        worker_stalled_now = bool(self._inference_worker and self._inference_worker.stalled())
        inference_worker_failed = bool(self._inference_worker and self._inference_worker.failed)
        # 정지는 래치한다: 한 번 감지되면 구간 5에서 HEALTH 가 실제로 써지고 그 시점에 정지가 풀려 있을 때까지 유지된다.
        # 그래서 짧은 정지도 최소 한 번의 HEALTH 에는 LINK_HEALTHY=꺼짐으로 반영된다.
        self._inference_stall_latched |= worker_stalled_now
        inference_worker_stalled = self._inference_stall_latched
        try:
            self._application_ok_state = bool(self._application_ok())
        except Exception:
            self._application_ok_state = False
            self.health_state_errors += 1
        # 아래 조건이 모두 참일 때만 LINK_HEALTHY(fail-closed). 텔레메트리 나이 한도는 JETSON_LINK_HEALTHY_MAX_AGE_MS=128ms.
        telemetry_link_healthy = (self._application_ok_state and not inference_worker_stalled and
                                  not inference_worker_failed and
                                  self._last_telemetry_ms is not None and
                                  now_ms - self._last_telemetry_ms <=
                                  lc.JETSON_LINK_HEALTHY_MAX_AGE_MS)
        # --- 구간 5: 주기적 HEALTH / MOTOR_COMMAND 송신 ---
        if self._last_health_ms is None or (now_ms - self._last_health_ms) >= self._health_period_ms:
            try:
                self._model_loaded_state = bool(self._model_loaded())
            except Exception:
                self._model_loaded_state = False
                self.health_state_errors += 1
            try:
                self._storage_ok_state = bool(self._storage_ok())
            except Exception:
                self._storage_ok_state = False
                self.health_state_errors += 1
            try:
                health = self._service.next_health_frame(
                    now_ms, model_loaded=self._model_loaded_state,
                    storage_ok=self._storage_ok_state,
                    link_healthy=telemetry_link_healthy)
                if self._port.write(health) != len(health):
                    raise OSError("short HEALTH write")
                # 저장소(로거)가 불량이면 어떤 모터 요청이 있어도 STOP/0 으로 바꿔 보낸다. 쓰기 길이가 다르면(short write) OSError 로 취급해
                # 연결 끊김으로 처리한다. STM32 는 이 명령이 300ms 안에 갱신되지 않으면 PWM 을 0 으로 만든다.
                direction, duty_permille = (self._motor_request if self._storage_ok_state else
                                             (lc.MOTOR_DIRECTIONS["STOP"], 0))
                command = self._service.next_motor_command_frame(
                    now_ms, duty_permille, direction)
                if self._port.write(command) != len(command):
                    raise OSError("short MOTOR_COMMAND write")
                self._last_health_ms = now_ms
                if self._inference_stall_latched and not worker_stalled_now:
                    self._inference_stall_latched = False
            except OSError:
                self.serial_errors += 1
                self._set_connected(False, timestamp)
        # --- 구간 6: 메트릭 조립 및 전달 ---
        # 메트릭은 float 로 통일한 평탄한 dict 이다. 'stm_*' 는 STM_DIAGNOSTIC(1초 주기, 없으면 0), 'telemetry_*' 는 마지막 TELEMETRY 의
        # 상태 비트, 그 밖은 파서 카운터와 Jetson 측 상태. DashboardPresenter.publish_metrics 가 이 키 이름으로 건강 상태를 계산하므로
        # 키 이름을 바꾸면 presenter 와 어긋난다. 참고: telemetry_stale 은 180ms(JETSON_KEEPALIVE_TIMEOUT_MS) 기준이고
        # LINK_HEALTHY 는 128ms 기준으로 서로 다른 임계값이다.
        if self._on_metrics or self._service.logger:
            telemetry_age_ms = (-1 if self._last_telemetry_ms is None else
                                max(0, now_ms - self._last_telemetry_ms))
            expected_frames = self._service.parser.valid_frames + self._service.parser.sequence_gaps
            # 샘플레이트 = (블록 수 차이 x 128샘플) / STM32 타임스탬프 경과(us). Jetson 스케줄링 지터가 아니라 STM32 시간 기준이다(최근 128블록).
            if len(self._sample_points) >= 2:
                first_sequence, first_us = self._sample_points[0]
                last_sequence, last_us = self._sample_points[-1]
                frame_delta = (last_sequence - first_sequence) & 0xffffffff
                elapsed_us = (last_us - first_us) & 0xffffffff
                sample_rate_hz = (frame_delta * lc.BLOCK_SAMPLES * 1_000_000.0 / elapsed_us
                                  if 0 < frame_delta < 0x80000000 and elapsed_us else 0.0)
            else:
                sample_rate_hz = 0.0
            diagnostic = self._service.parser.latest_diagnostic
            diagnostic_age_ms = (-1 if self._last_stm_diagnostic_ms is None else
                                 max(0, now_ms - self._last_stm_diagnostic_ms))
            diagnostic_flags = diagnostic.status_flags if diagnostic else 0
            metrics = {"invalid_frames": float(self._service.parser.invalid_frames),
                       "crc_errors": float(self._service.parser.crc_errors),
                       "sequence_gaps": float(self._service.parser.sequence_gaps),
                       "packet_loss_percent": (100.0 * self._service.parser.sequence_gaps /
                                               expected_frames if expected_frames else 0.0),
                       "duplicate_frames": float(self._service.parser.duplicate_frames),
                       "out_of_order_frames": float(self._service.parser.out_of_order_frames),
                       "source_restarts": float(self._service.parser.source_restarts),
                       "stm_diagnostic_frames": float(self._service.parser.diagnostic_frames),
                       "stm_invalid_diagnostic_frames": float(
                           self._service.parser.invalid_diagnostic_frames),
                       "stm_diagnostic_crc_errors": float(
                           self._service.parser.diagnostic_crc_errors),
                       "stm_diagnostic_valid": float(diagnostic is not None),
                       "stm_diagnostic_age_ms": float(diagnostic_age_ms),
                       "stm_diagnostic_stale": float(
                           self._last_stm_diagnostic_ms is None or
                           diagnostic_age_ms > lc.STM_DIAGNOSTIC_TIMEOUT_MS),
                       "stm_telemetry_sequence": float(
                           diagnostic.telemetry_sequence if diagnostic else 0),
                       "stm_dropped_blocks": float(diagnostic.dropped_blocks if diagnostic else 0),
                       "stm_invalid_health_frames": float(
                           diagnostic.invalid_health_frames if diagnostic else 0),
                       "stm_invalid_health_crc_frames": float(
                           diagnostic.invalid_health_crc_frames if diagnostic else 0),
                       "stm_sensor_overruns": float(diagnostic.sensor_overruns if diagnostic else 0),
                       "stm_platform_vibration_overruns": float(
                           diagnostic.platform_vibration_overruns if diagnostic else 0),
                       "stm_platform_adc_overruns": float(
                           diagnostic.platform_adc_overruns if diagnostic else 0),
                       "stm_sensor_bus_errors": float(diagnostic.sensor_bus_errors if diagnostic else 0),
                       "stm_uart_errors": float(diagnostic.uart_errors if diagnostic else 0),
                       "stm_avr_uart_errors": float(
                           diagnostic.avr_uart_errors if diagnostic else 0),
                       "stm_adc_dma_errors": float(
                           diagnostic.adc_dma_errors if diagnostic else 0),
                       "stm_uart_dma_errors": float(
                           diagnostic.uart_dma_errors if diagnostic else 0),
                       "stm_adc_peripheral_errors": float(
                           diagnostic.adc_peripheral_errors if diagnostic else 0),
                       "stm_uart_busy_events": float(
                           diagnostic.uart_busy_events if diagnostic else 0),
                       "stm_invalid_avr_diag_frames": float(
                           diagnostic.invalid_avr_diag_frames if diagnostic else 0),
                       "stm_invalid_motor_command_frames": float(
                           diagnostic.invalid_motor_command_frames if diagnostic else 0),
                       "stm_current_zero_count": float(
                           diagnostic.current_zero_count if diagnostic else 0),
                       "stm_buffered_samples": float(
                           diagnostic.buffered_samples if diagnostic else 0),
                       "stm_keepalive_valid": float(bool(
                           diagnostic_flags & lc.STM_DIAGNOSTIC_FLAGS["KEEPALIVE_VALID"])),
                       "stm_acquisition_overrun": float(bool(
                           diagnostic_flags &
                           lc.STM_DIAGNOSTIC_FLAGS["ACQUISITION_OVERRUN"])),
                       "stm_local_healthy": float(bool(
                           diagnostic_flags & lc.STM_DIAGNOSTIC_FLAGS["LOCAL_HEALTHY"])),
                       "stm_heartbeat_level": float(bool(
                           diagnostic_flags & lc.STM_DIAGNOSTIC_FLAGS["HEARTBEAT_LEVEL"])),
                       "stm_avr_live_valid": float(bool(
                           diagnostic_flags & lc.STM_DIAGNOSTIC_FLAGS["AVR_DIAG_VALID"])),
                       "stm_avr_heartbeat_healthy": float(bool(
                           diagnostic_flags &
                           lc.STM_DIAGNOSTIC_FLAGS["AVR_HEARTBEAT_HEALTHY"])),
                       "stm_avr_arm_active": float(bool(
                           diagnostic_flags & lc.STM_DIAGNOSTIC_FLAGS["AVR_ARM_ACTIVE"])),
                       "stm_avr_estop_active": float(bool(
                           diagnostic_flags & lc.STM_DIAGNOSTIC_FLAGS["AVR_ESTOP_ACTIVE"])),
                       "stm_avr_internal_healthy": float(bool(
                           diagnostic_flags &
                           lc.STM_DIAGNOSTIC_FLAGS["AVR_INTERNAL_HEALTHY"])),
                       "stm_avr_relay_command": float(bool(
                           diagnostic_flags & lc.STM_DIAGNOSTIC_FLAGS["AVR_RELAY_COMMAND"])),
                       "valid_frames": float(self._service.parser.valid_frames),
                       "telemetry_status_valid": float(self._service.parser.valid_frames > 0),
                       "telemetry_sensor_ready": float(bool(
                           self._last_telemetry_flags & lc.TELEMETRY_FLAGS["SENSOR_READY"])),
                       "telemetry_local_fault": float(bool(
                           self._last_telemetry_flags & lc.TELEMETRY_FLAGS["LOCAL_FAULT"])),
                       "telemetry_motor_active": float(bool(
                           self._last_telemetry_flags & lc.TELEMETRY_FLAGS["MOTOR_ACTIVE"])),
                       "telemetry_avr_diag_valid": float(bool(
                           self._last_telemetry_flags & lc.TELEMETRY_FLAGS["AVR_DIAG_VALID"])),
                       "telemetry_avr_armed": float(bool(
                           self._last_telemetry_flags & lc.TELEMETRY_FLAGS["AVR_ARMED"])),
                       "telemetry_avr_tripped": float(bool(
                           self._last_telemetry_flags & lc.TELEMETRY_FLAGS["AVR_TRIPPED"])),
                       "telemetry_avr_estop": float(bool(
                           self._last_telemetry_flags & lc.TELEMETRY_FLAGS["AVR_ESTOP"])),
                       "telemetry_avr_heartbeat_timeout": float(bool(
                           self._last_telemetry_flags &
                           lc.TELEMETRY_FLAGS["AVR_HEARTBEAT_TIMEOUT"])),
                       "sample_rate_hz": sample_rate_hz,
                       "sample_rate_observations": float(len(self._sample_points)),
                       "telemetry_age_ms": float(telemetry_age_ms),
                       "telemetry_stale": float(self._last_telemetry_ms is None or
                                                telemetry_age_ms > lc.JETSON_KEEPALIVE_TIMEOUT_MS),
                       "jetson_link_healthy": float(telemetry_link_healthy),
                       "serial_connected": float(self.connected),
                       "serial_errors": float(self.serial_errors),
                       "health_state_errors": float(self.health_state_errors),
                       "metrics_callback_errors": float(self.metrics_callback_errors),
                       "logger_dropped_records": float(self._service.logger.dropped_records
                                                       if self._service.logger else 0),
                       "model_loaded": float(self._model_loaded_state),
                       "storage_ok": float(self._storage_ok_state),
                       "application_ok": float(self._application_ok_state),
                       "inference_attempted": float(self._service.pipeline.inference_attempted),
                       "inference_ok": float(self._service.pipeline.inference_ok),
                       "inference_latency_ms": self._service.pipeline.last_inference_latency_ms,
                       "inference_p50_ms": self._service.pipeline.inference_latency_percentile(50),
                       "inference_p95_ms": self._service.pipeline.inference_latency_percentile(95),
                       "receiver_cycle_ms": (perf_counter_ns() - started_ns) / 1_000_000.0}
            if self._inference_worker:
                metrics.update(inference_queue_depth=float(self._inference_worker.queue_depth),
                               inference_queue_drops=float(self._inference_worker.queue_drops),
                               inference_worker_errors=float(self._inference_worker.callback_errors),
                               inference_worker_stalled=float(inference_worker_stalled),
                               inference_worker_failed=float(inference_worker_failed))
            if self._on_metrics:
                try:
                    self._on_metrics(metrics)
                except Exception as error:
                    self.metrics_callback_errors += 1
                    if self._service.logger:
                        self._service.logger.write(timestamp, "metrics_callback_error",
                                                   error=str(error))
            if self._service.logger and (self._last_metrics_log_ms is None or
                                         now_ms - self._last_metrics_log_ms >= 1000):
                self._service.logger.write(timestamp, "metrics", **metrics)
                self._last_metrics_log_ms = now_ms
        return windows

    # 연결 상태가 바뀔 때만 serial_connected / serial_disconnected 이벤트를 기록한다. 연결이 끊기면 모터 요청을 STOP 으로 되돌려
    # 재연결 시 이전 PWM 요청이 되살아나지 않게 한다.
    def _set_connected(self, connected: bool, timestamp: str) -> None:
        if not connected:
            self._motor_request = (lc.MOTOR_DIRECTIONS["STOP"], 0)
        if connected == self.connected:
            return
        self.connected = connected
        if self._service.logger:
            event = "serial_connected" if connected else "serial_disconnected"
            self._service.logger.write(timestamp, event)

    # 전용 스레드 루프. poll() 밖으로 예외가 나오면 오류 카운트/로그/메트릭(application_ok=0) 후 루프를 끝낸다(return).
    # 조용히 재시도하지 않는 fail-closed 선택: 워커가 멈추면 HEALTH 송신도 멈추므로 STM32 쪽 keepalive 타임아웃이 걸린다.
    # 연결이 안 된 상태에서는 50ms 쉬며 다시 읽는다.
    def run(self, stop: Event, timestamp: Callable[[], str], now_ms: Callable[[], int]) -> None:
        """Run on a dedicated thread; configure the serial port with a finite read timeout."""
        while not stop.is_set():
            current_timestamp = "receiver-fatal"
            try:
                current_timestamp = timestamp()
                self.poll(current_timestamp, now_ms())
            except Exception as error:
                self.serial_errors += 1
                self._set_connected(False, current_timestamp)
                if self._service.logger:
                    self._service.logger.write(current_timestamp, "serial_worker_error",
                                               error=str(error))
                if self._on_metrics:
                    try:
                        self._on_metrics({"serial_connected": 0.0, "telemetry_stale": 1.0,
                                          "application_ok": 0.0,
                                          "serial_errors": float(self.serial_errors)})
                    except Exception:
                        self.metrics_callback_errors += 1
                return
            if not self.connected:
                stop.wait(0.05)
