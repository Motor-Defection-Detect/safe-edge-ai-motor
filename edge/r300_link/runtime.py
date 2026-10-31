# ==============================================================================
# edge/r300_link/runtime.py
# 역할: Jetson 쪽 링크 처리의 핵심. UART 바이트를 프레임으로 검증해 자르고(TelemetryParser),
#       512샘플 창을 만들어 모델을 호출하고(EdgePipeline), 결과를 투표로 묶고(ConditionVoter),
#       나가는 HEALTH / MOTOR_COMMAND 프레임을 만든다. 시리얼 장치 열기, 모델 로딩, Qt 위젯은
#       일부러 이 파일 밖(다른 파일)에 두어서 장치 없이 호스트에서 테스트할 수 있다.
# 읽기 순서: 7/13  (앞: firmware 1~6 -> 여기서 Jetson 쪽 시작)
# 먼저 읽을 클래스: ConditionVoter (짧고 규칙이 분명해 입문용) -> EdgePipeline -> ReceiverService
#                   -> TelemetryParser (가장 길고 방어 코드가 많으니 마지막에)
# 데이터 흐름: 바이트 -> TelemetryParser.feed -> Telemetry -> EdgePipeline.append (512 창, hop 256)
#              -> infer 콜백(모델) -> ConditionVoter.update -> WindowResult -> presenter -> dashboard
#              반대 방향: health_frame / motor_command_frame 이 STM32 로 나가는 바이트를 만든다.
# 핵심 설계: AI(추론/투표)에는 릴레이 권한이 없다. 이 파일의 어떤 결과도 릴레이 명령으로 가지 않는다.
# 다음에 볼 곳: edge/r300_model.py — EdgePipeline 이 부르는 infer 콜백의 실제 구현(TorchScript 모델)
# 관련: edge/r300_link/link_contract.py (자동 생성 파일, 상수/구조체 정의라 직접 수정 금지),
#       docs/interface/r300-link.md (프레임 표), edge/r300_receiver.py (이 클래스들의 호출자)
# ==============================================================================

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


# 리틀엔디언 uint16 한 개를 읽고 쓰는 헬퍼. 프레임 끝의 CRC 2바이트를 다룰 때 쓴다.
_U16 = struct.Struct("<H")
# 허용 가능한 분류 라벨 전체(정상 + 고장 3종). 3클래스 대체(fallback)는 NORMAL 을 포함한
# 이 집합의 부분집합만 허용한다(EdgePipeline.__init__ 의 allowed_labels 검사).
_BASELINE_LABELS = frozenset(("NORMAL", "UNBALANCE", "LOOSENESS", "LOAD"))


# TELEMETRY 상태 바이트(version_flags 상위 8비트)가 실제로 가능한 조합인지 검사한다.
# True 면 유효. 거부하는 경우: 예약 비트 사용, AVR 상태/원인 비트가 있는데 AVR_DIAG_VALID 없음,
# ARMED 와 TRIPPED 동시, 원인(E-stop/하트비트 타임아웃) 둘 동시 또는 TRIPPED 없는 원인,
# MOTOR_ACTIVE 인데 SENSOR_READY 가 없거나 LOCAL_FAULT 가 있음(docs/interface/r300-link.md 의 Status invariants).
# 모순된 비트는 CRC 가 맞아도 버린다 -> 펌웨어/파서 어긋남을 조용히 믿지 않기 위함.
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


# 검증을 통과한 TELEMETRY 블록 1개(보통 128샘플, 1 kHz 기준 128ms 분량).
# samples 는 (vib_x, vib_y, vib_z, current) int16 4개 묶음의 튜플. received_ns 는 Jetson 단조시계로 잰
# 수신 시각(ReceiverService.decode 에서 채움), discontinuity 는 직전 블록과 이어지지 않음(손실/재시작),
# flags 는 상태 8비트, raw_frame 은 CRC 포함 원본 바이트(로그에 그대로 저장).
@dataclass(frozen=True)
class Telemetry:
    sequence: int
    timestamp_us: int
    samples: tuple[tuple[int, int, int, int], ...]
    received_ns: int = 0
    discontinuity: bool = False
    flags: int = 0
    raw_frame: bytes = b""


# STM32 가 1초마다 보내는 읽기 전용 신뢰성 카운터 프레임(STM_DIAGNOSTIC, 84바이트)을 푼 값.
# 필드는 link contract 의 STM_DIAGNOSTIC 표와 같은 순서. 진단/표시용일 뿐 릴레이 권한과 무관하다.
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


# 모델이 돌려주는 값: 라벨 문자열 + 신뢰도(0~1). EdgePipeline.append 가 범위/라벨 계약을 다시 검사한다.
@dataclass(frozen=True)
class InferenceResult:
    label: str
    confidence: float


# 추론 1회(창 1개)의 결과 묶음. vibration 은 모델 입력용(정규화됨), display_vibration 은 화면용(g 단위)
# 으로 일부러 분리해 두었다. current_features = (평균, RMS, 절대 피크). votes = 최근 투표 이력(최대 5).
# condition 은 ConditionVoter 의 'NORMAL'/'WARNING' (조언일 뿐 안전 판단이 아님).
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


# UART 바이트 스트림에서 유효한 TELEMETRY / STM_DIAGNOSTIC 프레임만 골라내는 상태 기계.
# 읽은 조각이 프레임 중간에서 끊겨도 내부 버퍼에 남겨 두었다가 이어 붙인다(feed 를 여러 번 호출).
# 검증 실패(길이/버전/상태/CRC)는 버퍼 맨 앞 1바이트를 버리고 다시 SOF 를 찾는 '재동기' 방식이며,
# 원인별 카운터(invalid_frames, crc_errors, ...)로 남겨 대시보드가 볼 수 있게 한다.
# 시퀀스는 uint32 라서 & 0xFFFFFFFF 로 wrap 을 다루고, 차이가 0x80000000 이상이면 '뒤로 간 것'으로 본다.
# 다음: 통과한 Telemetry 는 ReceiverService.decode -> EdgePipeline.append 로 넘어간다.
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

    # [구간 지도] feed() — 호출 한 번에 새 바이트를 받아 완성된 프레임을 0개 이상 yield 한다.
    #  1) 버퍼에 누적하고 두 종류의 SOF 중 가장 앞의 시작점을 찾는다
    #  2) STM_DIAGNOSTIC 프레임(고정 길이): 길이 대기 -> 값/CRC/일관성 검증 -> 시퀀스·재시작 판정 -> 저장
    #  3) TELEMETRY 헤더 검사: 최소 길이, sample_count(0 이거나 128 초과면 거부), 전체 길이 대기
    #  4) 프레임 잘라내기 후 버전·상태 비트·CRC 검증(실패하면 1바이트 버리고 재동기)
    #  5) 페이로드 해석 -> 시퀀스 연속성 판정(중복/역순/재시작/손실) -> Telemetry 생성
    # 주의: 제너레이터라서 반환된 iterator 를 끝까지 소비해야 버퍼가 진행된다.
    def feed(self, data: bytes) -> Iterator[Telemetry]:
        # --- 구간 1: 새 바이트 누적, SOF 탐색 ---
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
            # --- 구간 2: STM_DIAGNOSTIC 프레임 처리 ---
            if self._buffer.startswith(diagnostic_sof):
                if len(self._buffer) < lc.STM_DIAGNOSTIC_BYTES:
                    return
                frame = bytes(self._buffer[:lc.STM_DIAGNOSTIC_BYTES])
                values = lc.STM_DIAGNOSTIC_STRUCT.unpack_from(frame)
                diagnostic = StmDiagnostic(*values[2:])
                received_crc = _U16.unpack_from(
                    frame, lc.STM_DIAGNOSTIC_HEADER_BYTES)[0]
                # 진단 시퀀스 간격 계산. 시퀀스가 뒤로 가면서(delta >= 0x80000000) 타임스탬프도 줄었으면
                # STM32 재시작(restarted)으로 보고 허용, 그렇지 않은 0/역행은 invalid 로 거부한다.
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
                # 의미상 모순되는 조합은 CRC 가 맞아도 invalid 로 거른다. 예: 오류 카운터가 있는데 LOCAL_HEALTHY,
                # AVR 실시간 비트가 있는데 AVR_DIAG_VALID 없음, 하트비트 레벨이 켜졌는데 KEEPALIVE_VALID 와
                # LOCAL_HEALTHY 가 둘 다는 아님(PB8 은 둘 다 건강할 때만 토글), 릴레이 명령 비트가 켜졌는데 하트비트/ARM/내부정상이
                # 아니거나 E-stop 활성.
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
            # --- 구간 3: TELEMETRY 헤더 검사, 전체 프레임 길이 대기 ---
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
            # --- 구간 4: 프레임 잘라내기, 버전/상태 비트/CRC 검증 ---
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
            # --- 구간 5: 페이로드 해석, 시퀀스 연속성 판정, Telemetry 생성 ---
            payload = struct.unpack_from("<" + "h" * (count * lc.CHANNEL_COUNT), frame, lc.TELEMETRY_HEADER_BYTES)
            samples = tuple(tuple(payload[i:i + lc.CHANNEL_COUNT]) for i in range(0, len(payload), lc.CHANNEL_COUNT))
            del self._buffer[:size]
            # 시퀀스 판정: delta==0 중복(버림), 뒤로 갔지만 타임스탬프도 줄면 STM32 재시작(discontinuity),
            # 뒤로만 갔으면 역순(버림), 그 외 delta-1 개를 손실로 세고 delta>1 이면 discontinuity=True.
            # discontinuity 는 이후 EdgePipeline 이 창을 비우게 하는 신호다(끊긴 데이터에 걸친 창 방지).
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

    # 마지막 호출 이후 쌓인 StmDiagnostic 들을 한 번에 꺼내고 비운다(ReceiverService.decode 가 로그 기록용으로 호출).
    def take_diagnostics(self) -> tuple[StmDiagnostic, ...]:
        updates = tuple(self._diagnostic_updates)
        self._diagnostic_updates.clear()
        return updates


# Jetson -> STM32 HEALTH 프레임(CRC 포함 16바이트)을 만든다. APP_ALIVE 가 없거나 예약 비트가 있으면 ValueError.
# health_flags 의 LINK_HEALTHY 비트가 핵심: 이 비트가 꺼지면 STM32 가 PB8 하트비트와 PWM 을 멈추고
# (fail-closed) AVR 이 하트비트 끊김을 보고 릴레이를 끊는다. 릴레이 권한은 AVR 에만 있다.
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


# Jetson -> STM32 MOTOR_COMMAND 프레임(CRC 포함 18바이트)을 만든다. 듀티 0~1000 permille, 방향은
# STOP/FORWARD 만 허용하고 STOP 이면 듀티 0 을 강제한다. bool 은 int 의 하위 클래스라 isinstance 로 따로 거른다.
# 이것은 PWM '요청'일 뿐이며 릴레이와 무관하다. STM32 는 300ms 안에 새 유효 명령이 없으면 PWM 을 0 으로 만든다.
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


# 한 번의 실행(run) 증거 파일을 비동기로 쓴다: 이벤트 JSONL, 메트릭 JSONL, 원본 프레임 *.raw.bin,
# 패킷 색인 *.index.csv, 메타데이터 *.metadata.json. 전용 쓰기 스레드 + Queue 라서 UART/추론 스레드가
# 디스크 I/O 에 막히지 않는다. 같은 이름의 파일이 이미 있으면 덮어쓰지 않고 FileExistsError.
# 쓰기 오류가 나면 error 를 기록하고 이후 레코드는 dropped_records 로만 센다 -> ok 가 False 가 되고
# HEALTH 의 STORAGE_OK 가 꺼진다(그러면 SerialReceiver 가 모터 명령도 STOP 으로 강제한다).
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

    # 이벤트 1건(JSON 한 줄)을 큐에 넣는다. run 공통 context(run_id, class, condition ...)가 매 레코드에 합쳐진다.
    # event 가 metrics / ui_metrics 면 메트릭 파일에도 복사된다. 닫혔거나 오류 상태면 예외 없이 dropped_records 만 올린다.
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

    # 검증된 프레임의 원본 바이트를 *.raw.bin 에 그대로, 위치/시퀀스/플래그는 *.index.csv 한 줄로 남긴다.
    # raw_offset 은 여기서 0 으로 두고 실제 파일 위치는 쓰기 스레드(_run)가 채운다. 이 산출물을
    # scripts/r300/data_qc.py 가 데이터 품질 검사에 사용한다.
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

    # run 메타데이터(pwm_duty, condition 등)를 갱신하고 metadata.json 을 다시 쓰도록 큐에 넣는다.
    def set_context(self, **values: object) -> None:
        if self._closed or self.error is not None or not self._worker.is_alive():
            self.dropped_records += 1
            return
        with self._context_lock:
            updated = {**self._context, **values}
            json.dumps(updated, ensure_ascii=False)
            self._context = updated
        self._queue.put(("metadata", updated))

    # None 센티널을 큐에 넣고 쓰기 스레드가 남은 항목을 모두 기록한 뒤 끝나길 기다린다(join).
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._queue.put(None)
        self._worker.join()

    # 쓰기 스레드 본체. 네 파일을 'x' 모드(이미 있으면 실패)로 열고 큐를 항목 단위로 처리한다.
    # 매 레코드 flush 해서 비정상 종료에도 증거가 남게 하고, metadata.json 은 .tmp 에 쓴 뒤 replace 로
    # 교체해 반쯤 쓴 파일이 남지 않게 한다. OSError 가 나면 self.error 에 저장하고 종료한다.
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

    # 쓰기 스레드가 준비됐고 살아 있으며 오류가 없을 때만 True. HEALTH 의 STORAGE_OK 근거.
    @property
    def ok(self) -> bool:
        return self._ready.is_set() and self.error is None and self._worker.is_alive()


# 모델 입력용 전처리. 창(512행)마다 축별 평균(DC)을 빼고, 학습 때 구한 mean/scale 로 정규화한다.
# 진동 3채널만 다루며 전류는 _current_features 가 따로 처리한다. 유한하지 않은 값이 나오면 ValueError
# -> 추론 실패(inference_ok=False)로 이어져 fail-closed.
def _preprocess(rows: Sequence[tuple[int, int, int, int]], mean: tuple[float, float, float],
                scale: tuple[float, float, float]) -> tuple[tuple[float, float, float], ...]:
    """Remove each window's DC component, then apply training-set statistics."""
    dc = tuple(sum(row[channel] for row in rows) / len(rows) for channel in range(3))
    result = tuple(tuple(((row[channel] - dc[channel]) - mean[channel]) / scale[channel]
                         for channel in range(3)) for row in rows)
    if not all(math.isfinite(value) for row in result for value in row):
        raise ValueError("vibration preprocessing produced a non-finite value")
    return result


# 화면용 변환: raw count 에 축별 g/count 보정계수(기본 1/2048 g)를 곱한다. 모델 정규화와 일부러
# 분리해서, 모델 쪽 정규화가 화면의 g 단위를 바꾸지 않게 한다.
def _display_vibration(rows: Sequence[tuple[int, int, int, int]],
                       g_per_count: tuple[float, float, float]) -> tuple[tuple[float, float, float], ...]:
    result = tuple(tuple(row[channel] * g_per_count[channel]
                         for channel in range(3)) for row in rows)
    if not all(math.isfinite(value) for row in result for value in row):
        raise ValueError("vibration display conversion produced a non-finite value")
    return result


# 전류 채널(mA)의 (평균, RMS, 절대 피크) 3개 특징. 모델의 current 입력(길이 3)이 된다.
def _current_features(rows: Sequence[tuple[int, int, int, int]]) -> tuple[float, float, float]:
    values = tuple(float(row[3]) for row in rows)
    return (sum(values) / len(values), math.sqrt(sum(value * value for value in values) / len(values)), max(map(abs, values)))


# 연속 추론 결과를 투표로 묶어 'NORMAL' / 'WARNING' 만 결정하는 짧은 상태 기계(입문용으로 먼저 읽기 좋다).
# 규칙: 최근 5개 창 중 4개 이상이 '신뢰도 0.85 이상인 비정상'이면 WARNING. 신뢰도가 0.85 미만이면 비정상 라벨도 정상 표.
# 한 번 WARNING 이 되면 이력 5칸이 모두 정상(5회 연속 정상)일 때만 NORMAL 로 돌아온다(히스테리시스) -> 경고 깜빡임 방지.
# 안전 설계: 이 결과는 조언(advisory)일 뿐이다. AI 에는 릴레이 권한이 없고, 이 클래스에서 나가는 안전 경로도 없다.
# 릴레이 차단은 AVR 안전 상태기계만 결정한다. 다음: EdgePipeline.append 가 추론 직후 update() 를 부른다.
class ConditionVoter:
    """Five-window warning vote with a conservative clear hysteresis."""

    WINDOW_SIZE = 5
    REQUIRED_ABNORMAL = 4
    CONFIDENCE_THRESHOLD = 0.85
    CLEAR_NORMAL = 5

    def __init__(self) -> None:
        self._labels: Deque[str] = deque(maxlen=self.WINDOW_SIZE)
        self.condition = "NORMAL"

    # (라벨, 신뢰도)를 투표 1표로 바꿔 이력에 넣고 현재 condition 을 돌려준다.
    # WARNING 진입: 이력이 꽉 찼고 비정상 >= 4. 해제: 이력이 5개 이상이고 비정상 0. 그 사이 구간은 이전 상태를 유지한다.
    def update(self, label: str, confidence: float) -> str:
        # 신뢰도 임계(0.85) 미만이면 비정상 라벨도 NORMAL 표로 기록한다.
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

    # 투표 이력만 비운다(condition 값 자체는 그대로 유지됨). 데이터가 끊겼을 때 끊긴 구간에 걸쳐 투표하지 않기 위함.
    def reset_history(self) -> None:
        self._labels.clear()


# 검증된 Telemetry 블록(128샘플)을 쌓아 512샘플 창을 만들고, 256샘플(hop)이 더 쌓일 때마다 모델을 한 번 호출한다.
# 흐름: append() -> _preprocess / _display_vibration / _current_features -> infer 콜백 -> 출력 검증 -> ConditionVoter -> WindowResult.
# infer 는 콜백으로 주입한다(실제 TorchInference, 합성 데모, 테스트 공용). 모델 출력이 라벨 계약이나 [0,1] 범위를 벗어나면
# 예외를 던지고 inference_ok=False 가 되어 HEALTH 의 INFERENCE_OK 가 꺼진다(fail-closed).
# 첫 창은 512샘플이 찼을 때, 이후는 블록 2개(256샘플)마다 한 번씩 나온다. 다음: ReceiverService.process 가 append 를 호출한다.
class EdgePipeline:
    """Apply the documented 512-sample, hop-256 inference flow."""

    def __init__(self, infer: Callable[[tuple[tuple[float, float, float], ...], tuple[float, float, float]], InferenceResult],
                 vibration_mean: tuple[float, float, float] = (0.0, 0.0, 0.0),
                 vibration_scale: tuple[float, float, float] = (1.0, 1.0, 1.0),
                 vibration_g_per_count: tuple[float, float, float] = (0.00048828125,) * 3,
                 allowed_labels: tuple[str, ...] = tuple(sorted(_BASELINE_LABELS))) -> None:
        # 설정 검증: 정규화/보정 값은 유한한 숫자 3개, scale 은 양수, 라벨은 3~4개(NORMAL 포함, 기본 4클래스의 부분집합). 틀리면 시작 시점에 ValueError.
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

    # [구간 지도] append() — 블록 1개를 받아 창이 준비되면 추론, 아니면 None.
    #  1) 샘플 누적, 아직 512개가 안 찼거나 hop(256)이 덜 쌓였으면 None
    #  2) 전처리 + 추론 + 출력 검증(어떤 예외든 inference_ok=False 후 재전파)
    #  3) 지연 시간 기록, 투표, WindowResult 반환
    def append(self, telemetry: Telemetry) -> WindowResult | None:
        # --- 구간 1: 샘플 누적, 창 준비 여부 판단 ---
        self._samples.extend(telemetry.samples)
        self._pending += len(telemetry.samples)
        if len(self._samples) < lc.WINDOW_SAMPLES or (
                self._window_emitted and self._pending < lc.HOP_SAMPLES):
            return None
        # 첫 창 이후에는 hop(256)만큼만 소비하고 남은 샘플 수는 다음 창 몫으로 이월한다.
        if self._window_emitted:
            self._pending -= lc.HOP_SAMPLES
        else:
            self._pending = 0
            self._window_emitted = True
        # --- 구간 2: 전처리, 추론, 출력 검증 ---
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
        # 어떤 예외든 inference_ok=False 로 표시한 뒤 다시 던진다. 로그 기록과 None 처리는 호출한 ReceiverService.process 가 한다.
        except Exception:
            self.inference_ok = False
            raise
        # --- 구간 3: 지연 기록, 투표, 결과 반환 ---
        self.inference_ok = True
        with self._latency_lock:
            self._inference_latencies_ms.append(self.last_inference_latency_ms)
        condition = self._voter.update(inference.label, inference.confidence)
        return WindowResult(telemetry, vibration, current, inference, condition,
                            self.last_inference_latency_ms, self._voter.history,
                            display_vibration)

    # 전송 불연속(손실/재시작/큐 오버런) 시 샘플 버퍼와 투표 이력을 비워, 창이 끊어진 데이터를 이어 붙이지 않게 한다.
    def reset_window(self) -> None:
        """Discard samples across a known transport discontinuity."""
        self._samples.clear()
        self._pending = 0
        self._window_emitted = False
        self._voter.reset_history()

    # 최근 100회 추론 지연의 nearest-rank 백분위(예: 50, 95). 수신 스레드와 추론 스레드가 함께 접근하므로 락으로 보호한다.
    # p95 목표는 30ms(INFERENCE_P95_TARGET_MS). 기록이 없으면 0.0.
    def inference_latency_percentile(self, percentile: int) -> float:
        if not 1 <= percentile <= 100:
            raise ValueError("percentile must be within [1, 100]")
        with self._latency_lock:
            ordered = sorted(self._inference_latencies_ms)
        if not ordered:
            return 0.0
        return ordered[(percentile * len(ordered) + 99) // 100 - 1]

    # HEALTH 프레임의 health_flags 비트를 조립한다. APP_ALIVE 는 항상 켜고 MODEL_LOADED / STORAGE_OK / LINK_HEALTHY 는 인자,
    # INFERENCE_OK 는 마지막 추론 성공 여부로 켠다. link_healthy 는 SerialReceiver.poll 이 계산한다
    # (추론 정지 100ms 초과, Qt 하트비트 300ms 초과, 텔레메트리 128ms 초과 중 하나라도 있으면 False).
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


# UART 청크를 파이프라인에 연결하는 얇은 접착층(시리얼 장치는 소유하지 않는다).
# decode: 바이트 -> 검증된 Telemetry(+로깅), process: 블록 1개 추론, next_*_frame: 나가는 프레임의 시퀀스 번호 관리.
# 다음: edge/r300_receiver.py 의 SerialReceiver / InferenceWorker 가 이 객체를 호출한다.
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

    # decode + process 를 이어 붙인 편의 메서드. 실제 런타임(SerialReceiver)은 두 단계를 나눠서, decode 는 UART 스레드에서, process 는 추론 스레드에서 실행한다.
    def receive(self, data: bytes, timestamp: str) -> tuple[WindowResult, ...]:
        return tuple(result for telemetry in self.decode(data, timestamp)
                     if (result := self.process(telemetry, timestamp)) is not None)

    # 바이트 파싱 + 수신 시각(received_ns, Jetson 단조시계) 부착 + 로깅(STM 진단, 검증된 원본 프레임).
    # 진단의 current_zero_count 가 바뀌면 run 메타데이터에도 반영한다. 추론은 하지 않는다(UART 스레드를 짧게 유지).
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

    # 검증된 블록 1개를 처리한다. discontinuity 면 먼저 창을 비운다. 추론 예외는 여기서 잡아 inference_error 이벤트로
    # 기록하고 None 을 반환한다(실패는 pipeline.inference_ok=False 로 남아 HEALTH 에 반영된다). AI 조건 전이는 condition_transition 으로 로그.
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

    # HEALTH 시퀀스를 1 올리고(uint32 wrap) 현재 파이프라인 상태로 HEALTH 프레임을 만든다.
    def next_health_frame(self, timestamp_ms: int, *, model_loaded: bool, storage_ok: bool,
                          link_healthy: bool) -> bytes:
        frame = health_frame(self._health_sequence, timestamp_ms, self.pipeline.health_flags(
            model_loaded=model_loaded, storage_ok=storage_ok, link_healthy=link_healthy,
        ))
        self._health_sequence = (self._health_sequence + 1) & 0xFFFFFFFF
        return frame

    # MOTOR_COMMAND 시퀀스를 1 올리고(uint32 wrap) 프레임을 만든다. 값 검증은 motor_command_frame 이 한다.
    def next_motor_command_frame(self, timestamp_ms: int, duty_permille: int,
                                 direction: int) -> bytes:
        frame = motor_command_frame(self._motor_command_sequence, timestamp_ms,
                                    duty_permille, direction)
        self._motor_command_sequence = (self._motor_command_sequence + 1) & 0xFFFFFFFF
        return frame
