"""Host-testable implementation of the Jetson side of the R300 link.

This module owns byte framing, loss accounting, window construction and the
health frame.  Opening a serial device, loading a TensorRT engine and updating
Qt widgets stay at the application boundary so this code is usable in tests and
does not claim to operate a Jetson.
"""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass, replace
import csv
import json
import math
from queue import Queue
import struct
from time import perf_counter_ns
from pathlib import Path
from threading import Event, Lock, Thread
from typing import Callable, Deque, Iterator, Mapping, Sequence

from . import link_contract as lc


_U16 = struct.Struct("<H")
_BASELINE_LABELS = frozenset(("NORMAL", "UNBALANCE", "LOOSENESS", "LOAD"))


def telemetry_status_valid(flags: int) -> bool:
    """Return whether TELEMETRY status bits describe a possible STM32 state."""
    flag = lc.TELEMETRY_FLAGS
    avr_state = flags & (flag["AVR_ARMED"] | flag["AVR_TRIPPED"])
    avr_causes = flags & (flag["AVR_ESTOP"] | flag["AVR_HEARTBEAT_TIMEOUT"])
    return not (
        flags & ~sum(flag.values()) or
        avr_state == (flag["AVR_ARMED"] | flag["AVR_TRIPPED"]) or
        (flags & (flag["AVR_ARMED"] | flag["AVR_TRIPPED"] |
                  flag["AVR_ESTOP"] | flag["AVR_HEARTBEAT_TIMEOUT"]) and
         not flags & flag["AVR_DIAG_VALID"]) or
        (avr_causes and not flags & flag["AVR_TRIPPED"]) or
        avr_causes == (flag["AVR_ESTOP"] | flag["AVR_HEARTBEAT_TIMEOUT"]) or
        (flags & flag["MOTOR_ACTIVE"] and
         (not flags & flag["SENSOR_READY"] or flags & flag["LOCAL_FAULT"])))


@dataclass(frozen=True)
class Telemetry:
    sequence: int
    timestamp_us: int
    samples: tuple[tuple[int, int, int, int], ...]
    received_ns: int = 0
    discontinuity: bool = False
    flags: int = 0
    raw_frame: bytes = b""


@dataclass(frozen=True)
class StmDiagnostic:
    sequence: int
    timestamp_ms: int
    telemetry_sequence: int
    dropped_blocks: int
    invalid_health_frames: int
    invalid_health_crc_frames: int
    sensor_overruns: int
    platform_vibration_overruns: int
    platform_adc_overruns: int
    sensor_bus_errors: int
    uart_errors: int
    avr_uart_errors: int
    adc_dma_errors: int
    uart_dma_errors: int
    adc_peripheral_errors: int
    uart_busy_events: int
    invalid_avr_diag_frames: int
    invalid_motor_command_frames: int
    current_zero_count: int
    buffered_samples: int
    status_flags: int


@dataclass(frozen=True)
class InferenceResult:
    label: str
    confidence: float


@dataclass(frozen=True)
class WindowResult:
    telemetry: Telemetry
    vibration: tuple[tuple[float, float, float], ...]
    current_features: tuple[float, float, float]
    inference: InferenceResult
    condition: str
    inference_latency_ms: float
    votes: tuple[str, ...] = ()
    display_vibration: tuple[tuple[float, float, float], ...] = ()


class TelemetryParser:
    """Incrementally extract valid TELEMETRY frames from a UART byte stream."""

    def __init__(self) -> None:
        self._buffer = bytearray()
        self.invalid_frames = 0
        self.crc_errors = 0
        self.sequence_gaps = 0
        self.duplicate_frames = 0
        self.out_of_order_frames = 0
        self.source_restarts = 0
        self.valid_frames = 0
        self.valid_samples = 0
        self.diagnostic_frames = 0
        self.invalid_diagnostic_frames = 0
        self.diagnostic_crc_errors = 0
        self.latest_diagnostic: StmDiagnostic | None = None
        self._diagnostic_updates: list[StmDiagnostic] = []
        self._last_sequence: int | None = None
        self._last_timestamp_us: int | None = None
        self._last_diagnostic_sequence: int | None = None
        self._last_diagnostic_timestamp_ms: int | None = None

    def feed(self, data: bytes) -> Iterator[Telemetry]:
        self._buffer.extend(data)
        sof = _U16.pack(lc.TELEMETRY_SOF)
        diagnostic_sof = _U16.pack(lc.STM_DIAGNOSTIC_SOF)
        while True:
            starts = tuple(start for marker in (sof, diagnostic_sof)
                           if (start := self._buffer.find(marker)) >= 0)
            if not starts:
                # Keep a possible first byte of SOF for the next serial read.
                del self._buffer[:-1]
                return
            start = min(starts)
            if start:
                del self._buffer[:start]
            if self._buffer.startswith(diagnostic_sof):
                if len(self._buffer) < lc.STM_DIAGNOSTIC_BYTES:
                    return
                frame = bytes(self._buffer[:lc.STM_DIAGNOSTIC_BYTES])
                values = lc.STM_DIAGNOSTIC_STRUCT.unpack_from(frame)
                diagnostic = StmDiagnostic(*values[2:])
                received_crc = _U16.unpack_from(
                    frame, lc.STM_DIAGNOSTIC_HEADER_BYTES)[0]
                delta = ((diagnostic.sequence - self._last_diagnostic_sequence) & 0xFFFFFFFF
                         if self._last_diagnostic_sequence is not None else 1)
                restarted = (self._last_diagnostic_sequence is not None and
                             delta >= 0x80000000 and
                             self._last_diagnostic_timestamp_ms is not None and
                             diagnostic.timestamp_ms < self._last_diagnostic_timestamp_ms)
                invalid_crc = lc.crc16(frame[:-2]) != received_crc
                diagnostic_flags = lc.STM_DIAGNOSTIC_FLAGS
                avr_live = (diagnostic_flags["AVR_HEARTBEAT_HEALTHY"] |
                            diagnostic_flags["AVR_ARM_ACTIVE"] |
                            diagnostic_flags["AVR_ESTOP_ACTIVE"] |
                            diagnostic_flags["AVR_INTERNAL_HEALTHY"] |
                            diagnostic_flags["AVR_RELAY_COMMAND"])
                avr_relay_required = (diagnostic_flags["AVR_HEARTBEAT_HEALTHY"] |
                                      diagnostic_flags["AVR_ARM_ACTIVE"] |
                                      diagnostic_flags["AVR_INTERNAL_HEALTHY"])
                local_errors = (diagnostic.sensor_overruns or
                                diagnostic.platform_vibration_overruns or
                                diagnostic.platform_adc_overruns or diagnostic.sensor_bus_errors or
                                diagnostic.uart_errors or diagnostic.adc_dma_errors or
                                diagnostic.uart_dma_errors or diagnostic.adc_peripheral_errors)
                invalid = (values[1] != lc.PROTOCOL_VERSION or
                           diagnostic.current_zero_count > 4095 or
                           diagnostic.buffered_samples > lc.BLOCK_SAMPLES or
                           diagnostic.status_flags & ~sum(diagnostic_flags.values()) or
                           diagnostic.invalid_health_crc_frames >
                           diagnostic.invalid_health_frames or
                           (diagnostic.status_flags &
                            diagnostic_flags["ACQUISITION_OVERRUN"] and
                            diagnostic.status_flags & diagnostic_flags["LOCAL_HEALTHY"]) or
                           (local_errors and diagnostic.status_flags &
                            diagnostic_flags["LOCAL_HEALTHY"]) or
                           (diagnostic.status_flags & diagnostic_flags["HEARTBEAT_LEVEL"] and
                            diagnostic.status_flags & (diagnostic_flags["KEEPALIVE_VALID"] |
                                                       diagnostic_flags["LOCAL_HEALTHY"]) !=
                            (diagnostic_flags["KEEPALIVE_VALID"] |
                             diagnostic_flags["LOCAL_HEALTHY"])) or
                           (diagnostic.status_flags & avr_live and not
                            diagnostic.status_flags & diagnostic_flags["AVR_DIAG_VALID"]) or
                           (diagnostic.status_flags & diagnostic_flags["AVR_RELAY_COMMAND"] and
                            (diagnostic.status_flags & avr_relay_required != avr_relay_required or
                             diagnostic.status_flags &
                             diagnostic_flags["AVR_ESTOP_ACTIVE"])) or
                           (self._last_diagnostic_sequence is not None and
                            (delta == 0 or delta >= 0x80000000) and not restarted))
                if invalid_crc or invalid:
                    self.invalid_diagnostic_frames += 1
                    self.diagnostic_crc_errors += int(invalid_crc)
                    del self._buffer[0]
                    continue
                del self._buffer[:lc.STM_DIAGNOSTIC_BYTES]
                self._last_diagnostic_sequence = diagnostic.sequence
                self._last_diagnostic_timestamp_ms = diagnostic.timestamp_ms
                self.latest_diagnostic = diagnostic
                self._diagnostic_updates.append(diagnostic)
                self.diagnostic_frames += 1
                continue
            if len(self._buffer) < lc.TELEMETRY_HEADER_BYTES + lc.TELEMETRY_TRAILER_BYTES:
                return
            _, version_flags, sequence, timestamp_us, count = lc.TELEMETRY_STRUCT.unpack_from(self._buffer)
            size = lc.telemetry_frame_bytes(count)
            if count == 0 or count > lc.BLOCK_SAMPLES:
                self.invalid_frames += 1
                del self._buffer[0]
                continue
            if len(self._buffer) < size:
                return
            frame = bytes(self._buffer[:size])
            received_crc = _U16.unpack_from(frame, size - lc.TELEMETRY_TRAILER_BYTES)[0]
            flags = version_flags >> 8
            if (version_flags & 0xFF != lc.PROTOCOL_VERSION or
                    not telemetry_status_valid(flags)):
                self.invalid_frames += 1
                del self._buffer[0]
                continue
            if lc.crc16(frame[:-2]) != received_crc:
                self.invalid_frames += 1
                self.crc_errors += 1
                del self._buffer[0]
                continue
            payload = struct.unpack_from("<" + "h" * (count * lc.CHANNEL_COUNT), frame, lc.TELEMETRY_HEADER_BYTES)
            samples = tuple(tuple(payload[i:i + lc.CHANNEL_COUNT]) for i in range(0, len(payload), lc.CHANNEL_COUNT))
            del self._buffer[:size]
            if self._last_sequence is not None:
                delta = (sequence - self._last_sequence) & 0xFFFFFFFF
                if delta == 0:
                    self.duplicate_frames += 1
                    continue
                restarted = (delta >= 0x80000000 and
                             self._last_timestamp_us is not None and
                             timestamp_us < self._last_timestamp_us)
                if delta >= 0x80000000 and not restarted:
                    self.out_of_order_frames += 1
                    continue
                if restarted:
                    self.source_restarts += 1
                    discontinuity = True
                else:
                    self.sequence_gaps += delta - 1
                    discontinuity = delta > 1
            else:
                discontinuity = False
            self._last_sequence = sequence
            self._last_timestamp_us = timestamp_us
            self.valid_frames += 1
            self.valid_samples += count
            yield Telemetry(sequence, timestamp_us, samples,
                            discontinuity=discontinuity, flags=flags, raw_frame=frame)

    def take_diagnostics(self) -> tuple[StmDiagnostic, ...]:
        updates = tuple(self._diagnostic_updates)
        self._diagnostic_updates.clear()
        return updates


def health_frame(sequence: int, timestamp_ms: int, flags: int) -> bytes:
    """Create one CRC-protected HEALTH frame for STM32 supervision."""
    known_flags = sum(lc.HEALTH_FLAGS.values())
    if flags & lc.HEALTH_FLAGS["APP_ALIVE"] == 0 or flags & ~known_flags:
        raise ValueError("HEALTH flags require APP_ALIVE and may not use reserved bits")
    body = lc.HEALTH_STRUCT.pack(
        lc.HEALTH_SOF, lc.PROTOCOL_VERSION, sequence & 0xFFFFFFFF,
        timestamp_ms & 0xFFFFFFFF, flags & 0xFFFF,
    )
    return body + _U16.pack(lc.crc16(body))


def motor_command_frame(sequence: int, timestamp_ms: int, duty_permille: int,
                        direction: int) -> bytes:
    """Create one bounded, CRC-protected motor request; relay authority stays on the AVR."""
    if (not isinstance(duty_permille, int) or isinstance(duty_permille, bool) or
            not 0 <= duty_permille <= 1000):
        raise ValueError("motor duty must be an integer from 0 through 1000 permille")
    if (not isinstance(direction, int) or isinstance(direction, bool) or
            direction not in lc.MOTOR_DIRECTIONS.values()):
        raise ValueError("motor direction is not defined by the link contract")
    if direction == lc.MOTOR_DIRECTIONS["STOP"] and duty_permille != 0:
        raise ValueError("STOP requires zero duty")
    body = lc.MOTOR_COMMAND_STRUCT.pack(
        lc.MOTOR_COMMAND_SOF, lc.PROTOCOL_VERSION, sequence & 0xFFFFFFFF,
        timestamp_ms & 0xFFFFFFFF, duty_permille, direction)
    return body + _U16.pack(lc.crc16(body))


class EventLogger:
    """Write the run's event, metrics, raw, index and metadata evidence asynchronously."""

    _INDEX_FIELDS = ("packet_index", "run_id", "class", "condition", "pwm_duty", "split",
                     "current_zero_count",
                     "sequence", "timestamp_us", "received_utc", "raw_offset",
                     "raw_bytes", "sample_count", "flags", "discontinuity")

    def __init__(self, path: Path, context: Mapping[str, object] | None = None) -> None:
        self.path = path
        stem = path.with_suffix("")
        self.raw_path = stem.with_suffix(".raw.bin")
        self.index_path = stem.with_suffix(".index.csv")
        self.metadata_path = stem.with_suffix(".metadata.json")
        self.metrics_path = stem.with_suffix(".metrics.jsonl")
        existing = tuple(candidate for candidate in
                         (self.path, self.raw_path, self.index_path,
                          self.metadata_path, self.metrics_path) if candidate.exists())
        if existing:
            raise FileExistsError("run artifacts already exist: " +
                                  ", ".join(str(candidate) for candidate in existing))
        self.error: OSError | None = None
        self.dropped_records = 0
        self._closed = False
        self._context = dict(context or {})
        self._context_lock = Lock()
        self._packet_index = 0
        self._ready = Event()
        json.dumps(self._context, ensure_ascii=False)
        self._queue: Queue[tuple[str, object] | None] = Queue()
        self._queue.put(("metadata", dict(self._context)))
        self._worker = Thread(target=self._run, daemon=True)
        self._worker.start()

    def write(self, timestamp: str, event: str, **details: object) -> None:
        if not timestamp or not event:
            raise ValueError("timestamp and event are required")
        if self._closed or self.error is not None or not self._worker.is_alive():
            self.dropped_records += 1
            return
        with self._context_lock:
            context = dict(self._context)
        record = {**context, "timestamp": timestamp, "event": event, **details}
        line = json.dumps(record, separators=(",", ":"), ensure_ascii=False)
        self._queue.put(("event", line))
        if event in ("metrics", "ui_metrics"):
            self._queue.put(("metrics", line))

    def write_telemetry(self, timestamp: str, telemetry: Telemetry) -> None:
        if self._closed or self.error is not None or not self._worker.is_alive():
            self.dropped_records += 1
            return
        expected = lc.telemetry_frame_bytes(len(telemetry.samples))
        if not timestamp or len(telemetry.raw_frame) != expected:
            raise ValueError("validated telemetry timestamp and raw frame are required")
        with self._context_lock:
            context = dict(self._context)
            packet_index = self._packet_index
            self._packet_index += 1
        row = {
            "packet_index": packet_index,
            "run_id": context.get("run_id", ""),
            "class": context.get("class", ""),
            "condition": context.get("condition", ""),
            "pwm_duty": context.get("pwm_duty", ""),
            "split": context.get("split", ""),
            "current_zero_count": context.get("current_zero_count", ""),
            "sequence": telemetry.sequence,
            "timestamp_us": telemetry.timestamp_us,
            "received_utc": timestamp,
            "raw_offset": 0,
            "raw_bytes": len(telemetry.raw_frame),
            "sample_count": len(telemetry.samples),
            "flags": telemetry.flags,
            "discontinuity": int(telemetry.discontinuity),
        }
        self._queue.put(("raw", (telemetry.raw_frame, row)))

    def set_context(self, **values: object) -> None:
        if self._closed or self.error is not None or not self._worker.is_alive():
            self.dropped_records += 1
            return
        with self._context_lock:
            updated = {**self._context, **values}
            json.dumps(updated, ensure_ascii=False)
            self._context = updated
        self._queue.put(("metadata", updated))

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._worker.join()

    def _run(self) -> None:
        try:
            with (self.path.open("x", encoding="utf-8", newline="\n") as events,
                  self.metrics_path.open("x", encoding="utf-8", newline="\n") as metrics,
                  self.raw_path.open("xb") as raw,
                  self.index_path.open("x", encoding="utf-8", newline="") as index):
                self._ready.set()
                writer = csv.DictWriter(index, fieldnames=self._INDEX_FIELDS)
                writer.writeheader()
                index.flush()
                while (item := self._queue.get()) is not None:
                    kind, payload = item
                    if kind == "event":
                        events.write(str(payload) + "\n")
                        events.flush()
                    elif kind == "metrics":
                        metrics.write(str(payload) + "\n")
                        metrics.flush()
                    elif kind == "metadata":
                        metadata_tmp = self.metadata_path.with_name(
                            self.metadata_path.name + ".tmp")
                        metadata_tmp.write_text(
                            json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8", newline="\n")
                        metadata_tmp.replace(self.metadata_path)
                    else:
                        frame, row = payload
                        row["raw_offset"] = raw.tell()
                        raw.write(frame)
                        raw.flush()
                        writer.writerow(row)
                        index.flush()
        except OSError as error:
            self.error = error
            self._ready.set()

    @property
    def ok(self) -> bool:
        return self._ready.is_set() and self.error is None and self._worker.is_alive()


def _preprocess(rows: Sequence[tuple[int, int, int, int]], mean: tuple[float, float, float],
                scale: tuple[float, float, float]) -> tuple[tuple[float, float, float], ...]:
    """Remove each window's DC component, then apply training-set statistics."""
    dc = tuple(sum(row[channel] for row in rows) / len(rows) for channel in range(3))
    result = tuple(tuple(((row[channel] - dc[channel]) - mean[channel]) / scale[channel]
                         for channel in range(3)) for row in rows)
    if not all(math.isfinite(value) for row in result for value in row):
        raise ValueError("vibration preprocessing produced a non-finite value")
    return result


def _display_vibration(rows: Sequence[tuple[int, int, int, int]],
                       g_per_count: tuple[float, float, float]) -> tuple[tuple[float, float, float], ...]:
    result = tuple(tuple(row[channel] * g_per_count[channel]
                         for channel in range(3)) for row in rows)
    if not all(math.isfinite(value) for row in result for value in row):
        raise ValueError("vibration display conversion produced a non-finite value")
    return result


def _current_features(rows: Sequence[tuple[int, int, int, int]]) -> tuple[float, float, float]:
    values = tuple(float(row[3]) for row in rows)
    return (sum(values) / len(values), math.sqrt(sum(value * value for value in values) / len(values)), max(map(abs, values)))


class ConditionVoter:
    """Five-window warning vote with a conservative clear hysteresis."""

    WINDOW_SIZE = 5
    REQUIRED_ABNORMAL = 4
    CONFIDENCE_THRESHOLD = 0.85
    CLEAR_NORMAL = 5

    def __init__(self) -> None:
        self._labels: Deque[str] = deque(maxlen=self.WINDOW_SIZE)
        self.condition = "NORMAL"

    def update(self, label: str, confidence: float) -> str:
        self._labels.append(label if label != "NORMAL" and
                            confidence >= self.CONFIDENCE_THRESHOLD else "NORMAL")
        abnormal = sum(item != "NORMAL" for item in self._labels)
        if len(self._labels) == self._labels.maxlen and abnormal >= self.REQUIRED_ABNORMAL:
            self.condition = "WARNING"
        elif len(self._labels) >= self.CLEAR_NORMAL and abnormal == 0:
            self.condition = "NORMAL"
        return self.condition

    @property
    def history(self) -> tuple[str, ...]:
        return tuple(self._labels)

    def reset_history(self) -> None:
        self._labels.clear()


class EdgePipeline:
    """Apply the documented 512-sample, hop-256 inference flow."""

    def __init__(self, infer: Callable[[tuple[tuple[float, float, float], ...], tuple[float, float, float]], InferenceResult],
                 vibration_mean: tuple[float, float, float] = (0.0, 0.0, 0.0),
                 vibration_scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
                 vibration_g_per_count: tuple[float, float, float] = (0.00048828125,) * 3,
                 allowed_labels: tuple[str, ...] = tuple(sorted(_BASELINE_LABELS))) -> None:
        if not callable(infer):
            raise ValueError("infer must be callable")
        triples = (vibration_mean, vibration_scale, vibration_g_per_count)
        if (any(not isinstance(values, tuple) or len(values) != 3 for values in triples) or
                any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                    not math.isfinite(value) for values in triples for value in values)):
            raise ValueError("vibration normalization and calibration require three finite numbers")
        if any(value <= 0.0 for value in vibration_scale + vibration_g_per_count):
            raise ValueError("vibration scales must be positive")
        if (not isinstance(allowed_labels, tuple) or
                not all(isinstance(label, str) for label in allowed_labels) or
                len(set(allowed_labels)) != len(allowed_labels) or
                not 3 <= len(allowed_labels) <= 4 or "NORMAL" not in allowed_labels or
                not set(allowed_labels) <= _BASELINE_LABELS):
            raise ValueError("allowed_labels must be the documented four classes or a three-class subset")
        self._infer = infer
        self._vibration_mean = vibration_mean
        self._vibration_scale = vibration_scale
        self._vibration_g_per_count = vibration_g_per_count
        self._allowed_labels = frozenset(allowed_labels)
        self._samples: Deque[tuple[int, int, int, int]] = deque(maxlen=lc.WINDOW_SAMPLES)
        self._pending = 0
        self._window_emitted = False
        self._voter = ConditionVoter()
        self._inference_latencies_ms: Deque[float] = deque(maxlen=100)
        self._latency_lock = Lock()
        self.inference_attempted = False
        self.inference_ok = False
        self.last_inference_latency_ms = 0.0

    def append(self, telemetry: Telemetry) -> WindowResult | None:
        self._samples.extend(telemetry.samples)
        self._pending += len(telemetry.samples)
        if len(self._samples) < lc.WINDOW_SAMPLES or (
                self._window_emitted and self._pending < lc.HOP_SAMPLES):
            return None
        if self._window_emitted:
            self._pending -= lc.HOP_SAMPLES
        else:
            self._pending = 0
            self._window_emitted = True
        self.inference_attempted = True
        try:
            rows = tuple(self._samples)
            vibration = _preprocess(rows, self._vibration_mean, self._vibration_scale)
            display_vibration = _display_vibration(rows, self._vibration_g_per_count)
            current = _current_features(rows)
            started_ns = perf_counter_ns()
            inference = self._infer(vibration, current)
            self.last_inference_latency_ms = (perf_counter_ns() - started_ns) / 1_000_000.0
            if not isinstance(inference.label, str) or not inference.label.strip():
                raise ValueError("inference label must be a non-empty string")
            if inference.label not in self._allowed_labels:
                raise ValueError("inference label is outside the configured class contract")
            if (isinstance(inference.confidence, bool) or
                    not isinstance(inference.confidence, (int, float)) or
                    not math.isfinite(inference.confidence) or
                    not 0.0 <= inference.confidence <= 1.0):
                raise ValueError("inference confidence must be within [0, 1]")
        except Exception:
            self.inference_ok = False
            raise
        self.inference_ok = True
        with self._latency_lock:
            self._inference_latencies_ms.append(self.last_inference_latency_ms)
        condition = self._voter.update(inference.label, inference.confidence)
        return WindowResult(telemetry, vibration, current, inference, condition,
                            self.last_inference_latency_ms, self._voter.history,
                            display_vibration)

    def reset_window(self) -> None:
        """Discard samples across a known transport discontinuity."""
        self._samples.clear()
        self._pending = 0
        self._window_emitted = False
        self._voter.reset_history()

    def inference_latency_percentile(self, percentile: int) -> float:
        if not 1 <= percentile <= 100:
            raise ValueError("percentile must be within [1, 100]")
        with self._latency_lock:
            ordered = sorted(self._inference_latencies_ms)
        if not ordered:
            return 0.0
        return ordered[(percentile * len(ordered) + 99) // 100 - 1]

    def health_flags(self, *, model_loaded: bool, storage_ok: bool, link_healthy: bool) -> int:
        flags = lc.HEALTH_FLAGS["APP_ALIVE"]
        if model_loaded:
            flags |= lc.HEALTH_FLAGS["MODEL_LOADED"]
        if self.inference_ok:
            flags |= lc.HEALTH_FLAGS["INFERENCE_OK"]
        if storage_ok:
            flags |= lc.HEALTH_FLAGS["STORAGE_OK"]
        if link_healthy:
            flags |= lc.HEALTH_FLAGS["LINK_HEALTHY"]
        return flags


class ReceiverService:
    """Connect UART chunks to the pipeline without owning a serial device."""

    def __init__(self, pipeline: EdgePipeline, logger: EventLogger | None = None) -> None:
        self.parser = TelemetryParser()
        self.pipeline = pipeline
        self.logger = logger
        self._health_sequence = 0
        self._motor_command_sequence = 0
        self._last_condition = "UNKNOWN"
        self._current_zero_count: int | None = None

    def receive(self, data: bytes, timestamp: str) -> tuple[WindowResult, ...]:
        return tuple(result for telemetry in self.decode(data, timestamp)
                     if (result := self.process(telemetry, timestamp)) is not None)

    def decode(self, data: bytes, timestamp: str) -> tuple[Telemetry, ...]:
        received_ns = perf_counter_ns()
        telemetries = tuple(replace(item, received_ns=received_ns) for item in self.parser.feed(data))
        diagnostics = self.parser.take_diagnostics()
        if self.logger:
            for diagnostic in diagnostics:
                if diagnostic.current_zero_count != self._current_zero_count:
                    self.logger.set_context(current_zero_count=diagnostic.current_zero_count)
                    self._current_zero_count = diagnostic.current_zero_count
                self.logger.write(timestamp, "stm_diagnostic", **asdict(diagnostic))
            for telemetry in telemetries:
                self.logger.write_telemetry(timestamp, telemetry)
        return telemetries

    def process(self, telemetry: Telemetry, timestamp: str) -> WindowResult | None:
        """Run preprocessing/inference for one already validated telemetry block."""
        if telemetry.discontinuity:
            self.pipeline.reset_window()
        try:
            result = self.pipeline.append(telemetry)
        except Exception as error:
            if self.logger:
                self.logger.write(timestamp, "inference_error", error=str(error), sequence=telemetry.sequence)
            return None
        if result is not None and self.logger:
            self.logger.write(timestamp, "inference", sequence=telemetry.sequence,
                              label=result.inference.label, confidence=result.inference.confidence,
                              ai_condition=result.condition, latency_ms=result.inference_latency_ms)
            if result.condition != self._last_condition:
                self.logger.write(timestamp, "condition_transition",
                                  ai_previous=self._last_condition,
                                  ai_current=result.condition, sequence=telemetry.sequence)
        if result is not None:
            self._last_condition = result.condition
        return result

    def next_health_frame(self, timestamp_ms: int, *, model_loaded: bool, storage_ok: bool,
                          link_healthy: bool) -> bytes:
        frame = health_frame(self._health_sequence, timestamp_ms, self.pipeline.health_flags(
            model_loaded=model_loaded, storage_ok=storage_ok, link_healthy=link_healthy,
        ))
        self._health_sequence = (self._health_sequence + 1) & 0xFFFFFFFF
        return frame

    def next_motor_command_frame(self, timestamp_ms: int, duty_permille: int,
                                 direction: int) -> bytes:
        frame = motor_command_frame(self._motor_command_sequence, timestamp_ms,
                                    duty_permille, direction)
        self._motor_command_sequence = (self._motor_command_sequence + 1) & 0xFFFFFFFF
        return frame
