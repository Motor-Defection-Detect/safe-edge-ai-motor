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


INFERENCE_QUEUE_BLOCKS = 3


class SerialPort(Protocol):
    def read(self, size: int) -> bytes: ...
    def write(self, data: bytes) -> int: ...


def open_serial_port(device: str, timeout_seconds: float = 0.05) -> SerialPort:
    if not device or not math.isfinite(timeout_seconds) or timeout_seconds <= 0.0:
        raise ValueError("a device path and positive finite timeout are required")
    import serial
    return serial.Serial(port=device, baudrate=lc.BAUD, bytesize=serial.EIGHTBITS,
                         parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                         timeout=timeout_seconds, write_timeout=timeout_seconds)


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
    def queue_depth(self) -> int:
        return self._queue.qsize()

    def stalled(self, now_ns: int | None = None) -> bool:
        with self._state_lock:
            started_ns = self._processing_started_ns
        return (started_ns is not None and
                (perf_counter_ns() if now_ns is None else now_ns) - started_ns >=
                lc.INFERENCE_WORKER_STALL_MS * 1_000_000)

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


class SerialReceiver:
    """Own UART polling and health cadence without owning a device path or model."""

    def __init__(self, port: SerialPort, service: ReceiverService, *,
                 model_loaded: Callable[[], bool], storage_ok: Callable[[], bool],
                 application_ok: Callable[[], bool] | None = None,
                 on_windows: Callable[[tuple[WindowResult, ...], str], None] | None = None,
                 on_metrics: Callable[[dict[str, float]], None] | None = None,
                 inference_worker: InferenceWorker | None = None,
                 health_period_ms: int = lc.HEARTBEAT_TOGGLE_MS) -> None:
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

    def set_motor_command(self, duty_permille: int) -> None:
        direction = (lc.MOTOR_DIRECTIONS["FORWARD"] if duty_permille
                     else lc.MOTOR_DIRECTIONS["STOP"])
        # Validate at the caller boundary before the serial worker owns the request.
        motor_command_frame(0, 0, duty_permille, direction)
        self._motor_request = (direction, duty_permille)

    def poll(self, timestamp: str, now_ms: int) -> tuple[WindowResult, ...]:
        """Process one bounded UART read; suitable for a dedicated worker thread."""
        started_ns = perf_counter_ns()
        try:
            data = self._port.read(4096)
            self._set_connected(True, timestamp)
        except OSError:
            self.serial_errors += 1
            self._set_connected(False, timestamp)
            data = b""
        diagnostic_frames = self._service.parser.diagnostic_frames
        telemetries = self._service.decode(data, timestamp)
        if telemetries:
            self._last_telemetry_flags = telemetries[-1].flags
            self._last_telemetry_ms = now_ms
            for telemetry in telemetries:
                restarted = bool(self._sample_points and
                                 (telemetry.sequence - self._sample_points[-1][0]) & 0xffffffff >=
                                 0x80000000 and
                                 telemetry.timestamp_us < self._sample_points[-1][1])
                if restarted or len(telemetry.samples) != lc.BLOCK_SAMPLES:
                    self._sample_points.clear()
                if len(telemetry.samples) == lc.BLOCK_SAMPLES:
                    self._sample_points.append((telemetry.sequence, telemetry.timestamp_us))
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
        worker_stalled_now = bool(self._inference_worker and self._inference_worker.stalled())
        inference_worker_failed = bool(self._inference_worker and self._inference_worker.failed)
        self._inference_stall_latched |= worker_stalled_now
        inference_worker_stalled = self._inference_stall_latched
        try:
            self._application_ok_state = bool(self._application_ok())
        except Exception:
            self._application_ok_state = False
            self.health_state_errors += 1
        telemetry_link_healthy = (self._application_ok_state and not inference_worker_stalled and
                                  not inference_worker_failed and
                                  self._last_telemetry_ms is not None and
                                  now_ms - self._last_telemetry_ms <=
                                  lc.JETSON_LINK_HEALTHY_MAX_AGE_MS)
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
        if self._on_metrics or self._service.logger:
            telemetry_age_ms = (-1 if self._last_telemetry_ms is None else
                                max(0, now_ms - self._last_telemetry_ms))
            expected_frames = self._service.parser.valid_frames + self._service.parser.sequence_gaps
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

    def _set_connected(self, connected: bool, timestamp: str) -> None:
        if not connected:
            self._motor_request = (lc.MOTOR_DIRECTIONS["STOP"], 0)
        if connected == self.connected:
            return
        self.connected = connected
        if self._service.logger:
            event = "serial_connected" if connected else "serial_disconnected"
            self._service.logger.write(timestamp, event)

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
