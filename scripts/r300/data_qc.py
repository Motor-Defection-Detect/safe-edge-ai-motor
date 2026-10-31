# [보조 도구] 수집 세션(run) 산출물을 자동 QC 규칙으로 검사해 JSON 리포트를 만든다.
# - 역할: 학습에 쓰기 전에 데이터 품질과 split 누수를 걸러낸다. 입력은 세션별 .metadata.json (옆의
#   .index.csv, .raw.bin, .metrics.jsonl 을 함께 읽는다).
# - 읽기 순서: 핵심 읽기 순서(1~13번) 밖의 보조 도구다. 7번(edge/r300_link/runtime.py)을 읽은 뒤에 보면 좋다.
# - 다음에 볼 곳: edge/r300_link/runtime.py 의 EventLogger(이 산출물을 만드는 쪽), 테스트는 tests/unit/test_r300_data_qc.py
# - 관련 문서: docs/interface/data-manifest-schema.md, docs/interface/test-plan.md
#
# [QC 규칙 요약] 결과의 flags 에 'QC-0xx ...' 문자열로 나온다.
# - QC-001 패킷 무결성: 인덱스/raw/CRC 불일치, 패킷 gap(손실) 비율 0.5% 초과
# - QC-002 샘플레이트: STM32 타임스탬프 기준 계약값에서 +/-1% 초과
# - QC-003 포화(clipping): 센서 풀스케일의 95% 이상인 값이 전체의 1% 초과
# - QC-004 센서 이상: 센서 오류 플래그, 또는 한 채널이 1초(= SAMPLE_RATE_HZ 샘플) 이상 값이 얼어붙음
# - QC-005 시작 구간: 시작 제외 5~10초, 제외 후 남는 샘플이 있어야 함
# - QC-006 메타데이터/세션 구성: 필수 항목, 클래스 계약, 90초 이상 등
# - QC-007 PWM 조건: 60/75/90% 층화, 매니페스트 PWM과 STM32 모터 동작 증거 일치
# - QC-008 split: 세션마다 train/validation/test 배정, 클래스당 4/1/1, 한 run이 여러 split에 걸치면 안 됨
# - QC-009 클래스 균형: 클래스 계약의 모든 클래스 존재, 세션 수 차이 1 이하, 클래스당 6회 이상
# - QC-010 중복: run_id 중복, 서로 다른 run_id에 같은 raw 데이터
"""Audit R300 run artifacts using the automatable QC rules from R300-MLD-001."""
from __future__ import annotations

import argparse
import csv
from datetime import datetime
import hashlib
import json
import math
from pathlib import Path
import struct
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "edge"))

from r300_link import link_contract as lc
from r300_link import ConditionVoter, telemetry_status_valid
from r300_analysis import vibration_spectrum

# 보정값이 없거나 잘못됐을 때 쓰는 대체 진동 스케일(g/count). 0.00048828125 = 1/2048 이다.
_DEFAULT_VIBRATION_G_PER_COUNT = (0.00048828125,) * 3
# 메타데이터에 기록돼야 하는 '이상 판정 투표' 정책. ConditionVoter의 상수와 같아야 QC-006 을 통과한다.
_CONDITION_POLICY = {
    "window_size": ConditionVoter.WINDOW_SIZE,
    "required_abnormal": ConditionVoter.REQUIRED_ABNORMAL,
    "confidence_threshold": ConditionVoter.CONFIDENCE_THRESHOLD,
    "clear_normal": ConditionVoter.CLEAR_NORMAL,
}


# metrics.jsonl 에서 누적 카운터(crc_errors 등)의 최댓값을 구한다.
# 음수/비정수/불리언 같은 이상한 값은 QC-001 플래그로 남기고 건너뛴다.
def _max_counter(records: list[dict[str, Any]], field: str, flags: list[str]) -> int:
    values: list[int] = []
    for record in records:
        value = record.get(field, 0)
        if (isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value < 0 or int(value) != value):
            flags.append(f"QC-001 runtime metric {field} is not a non-negative counter")
            continue
        values.append(int(value))
    return max(values, default=0)


# xxx.metadata.json 이름에서 같은 세션의 index.csv, raw.bin, metrics.jsonl 경로를 유도한다.
# 이름이 .metadata.json 으로 끝나지 않으면 ValueError.
def _paths(metadata_path: Path) -> tuple[Path, Path, Path]:
    suffix = ".metadata.json"
    if not metadata_path.name.endswith(suffix):
        raise ValueError(f"metadata path must end with {suffix}")
    stem = metadata_path.with_name(metadata_path.name[:-len(suffix)])
    return (Path(str(stem) + ".index.csv"), Path(str(stem) + ".raw.bin"),
            Path(str(stem) + ".metrics.jsonl"))


# 리포트 출력 경로가 원본 세션 파일(증거)을 덮어쓰지 않도록 막는다.
def _validate_output_path(output: Path, metadata_paths: list[Path]) -> None:
    protected: set[Path] = set()
    for metadata_path in metadata_paths:
        index_path, raw_path, metrics_path = _paths(metadata_path)
        stem = metadata_path.with_name(metadata_path.name[:-len(".metadata.json")])
        protected.update(path.resolve() for path in
                         (metadata_path, index_path, raw_path, metrics_path,
                          Path(str(stem) + ".jsonl")))
    if output.resolve() in protected:
        raise ValueError("QC output must not overwrite a source session artifact")


# 세션 1개를 검사한다. 입력: .metadata.json 경로. 출력: 측정값 + flags 목록이 든 dict.
# 흐름: (1) 메타데이터 검증(QC-006/007/008) -> (2) 인덱스 행마다 raw 프레임을 CRC로 다시 검증하고
#       손실/샘플레이트/포화/얼어붙음을 누적(QC-001~004) -> (3) 시작 구간을 뺀 통계와 주파수 특징 계산.
# 주의: 길어 보이지만 대부분 '이상하면 flag 추가' 패턴이다. 예외를 던지지 않고 flag로 모으는 것이 의도다.
# 포화 한계: 진동은 +/-16 g, 전류는 5000 mA 풀스케일의 95%로 계산한다.
def audit_session(metadata_path: Path) -> dict[str, Any]:
    """Return measured values and QC flags for one immutable acquisition session."""
    index_path, raw_path, metrics_path = _paths(metadata_path)
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict):
        raise ValueError("session metadata must be a JSON object")
    rows = list(csv.DictReader(index_path.open(encoding="utf-8", newline="")))
    raw = raw_path.read_bytes()
    flags: list[str] = []
    metric_records = ([json.loads(line) for line in
                       metrics_path.read_text(encoding="utf-8").splitlines()]
                      if metrics_path.exists() else [])
    metrics = [record for record in metric_records if isinstance(record, dict)]
    if len(metrics) != len(metric_records):
        flags.append("QC-001 runtime metrics contain a non-object record")
    required = ("protocol_version", "contract_sha256", "run_id", "class", "pwm_duty",
                "condition", "start_utc", "sensor_cfg",
                "notes", "fixture", "power_cycle", "mount", "split", "label_evidence",
                "startup_exclusion_seconds", "class_contract", "class_fallback_reason",
                "condition_policy", "current_zero_count",
                "vibration_g_per_count", "vibration_mean", "vibration_scale")
    missing = [field for field in required if field not in metadata]
    if missing:
        flags.append("QC-006 missing metadata: " + ",".join(missing))
    if (metadata.get("protocol_version") != lc.PROTOCOL_VERSION or
            metadata.get("contract_sha256") != lc.CONTRACT_SHA256):
        flags.append("QC-006 link protocol version or contract digest is inconsistent")
    if any(not isinstance(metadata.get(field), str) or not metadata.get(field, "").strip()
           for field in ("run_id", "class", "condition", "start_utc", "sensor_cfg",
                         "fixture", "mount")):
        flags.append("QC-006 required metadata strings are empty or invalid")
    try:
        started_at = datetime.fromisoformat(str(metadata.get("start_utc", "")).replace("Z", "+00:00"))
        if started_at.tzinfo is None:
            raise ValueError
    except ValueError:
        flags.append("QC-006 start_utc is not a timezone-aware ISO-8601 timestamp")
    if (not isinstance(metadata.get("power_cycle"), int) or
            isinstance(metadata.get("power_cycle"), bool) or metadata.get("power_cycle", -1) < 0):
        flags.append("QC-006 power_cycle is not a non-negative integer")
    if (not isinstance(metadata.get("pwm_duty"), int) or
            isinstance(metadata.get("pwm_duty"), bool) or not 0 <= metadata.get("pwm_duty", -1) <= 1000):
        flags.append("QC-006 pwm_duty is not integer permille within 0..1000")
    if not isinstance(metadata.get("notes"), str):
        flags.append("QC-006 notes metadata is not a string")
    label_evidence = metadata.get("label_evidence")
    if (not isinstance(label_evidence, str) or
            not label_evidence.strip() or label_evidence.strip() == "unassigned"):
        flags.append("QC-006 label evidence is unassigned")
    split = metadata.get("split")
    assigned_split = isinstance(split, str) and split in {"train", "validation", "test"}
    if not assigned_split:
        flags.append("QC-008 session split is unassigned or invalid")
    elif (metadata.get("condition") == "unspecified" or metadata.get("fixture") == "unassigned" or
          metadata.get("mount") == "unspecified"):
        flags.append("QC-006 assigned dataset session retains placeholder experiment metadata")
    startup_exclusion_seconds = metadata.get("startup_exclusion_seconds")
    if (isinstance(startup_exclusion_seconds, bool) or
            not isinstance(startup_exclusion_seconds, (int, float)) or
            not math.isfinite(startup_exclusion_seconds) or
            not 5.0 <= startup_exclusion_seconds <= 10.0):
        flags.append("QC-005 startup exclusion must be between 5 and 10 seconds")
        startup_exclusion_seconds = 10.0
    class_contract = metadata.get("class_contract", [])
    baseline_labels = {"NORMAL", "UNBALANCE", "LOOSENESS", "LOAD"}
    valid_class_contract = (isinstance(class_contract, list) and
                            all(isinstance(label, str) for label in class_contract))
    if (not valid_class_contract or not 3 <= len(class_contract) <= 4 or
            len(set(class_contract)) != len(class_contract) or "NORMAL" not in class_contract or
            not set(class_contract) <= baseline_labels or metadata.get("class") not in class_contract):
        flags.append("QC-006 class and class_contract are inconsistent")
        class_contract = []
    fallback_reason = metadata.get("class_fallback_reason")
    if not isinstance(fallback_reason, str):
        flags.append("QC-006 class fallback reason is not a string")
        fallback_reason = ""
    if len(class_contract) == 3 and not fallback_reason.strip():
        flags.append("QC-006 three-class fallback reason is missing")
    if metadata.get("condition_policy") != _CONDITION_POLICY:
        flags.append("QC-006 condition voting policy is missing or inconsistent")
    if assigned_split and metadata.get("pwm_duty") not in (600, 750, 900):
        flags.append("QC-007 PWM is outside the 60/75/90 acquisition strata")
    vibration_g_per_count = metadata.get("vibration_g_per_count")
    if (not isinstance(vibration_g_per_count, list) or len(vibration_g_per_count) != 3 or
            any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value <= 0.0 for value in vibration_g_per_count)):
        flags.append("QC-006 vibration calibration is unavailable or invalid")
        vibration_g_per_count = list(_DEFAULT_VIBRATION_G_PER_COUNT)
    vibration_mean = metadata.get("vibration_mean")
    vibration_scale = metadata.get("vibration_scale")
    if (not isinstance(vibration_mean, list) or len(vibration_mean) != 3 or
            any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) for value in vibration_mean) or
            not isinstance(vibration_scale, list) or len(vibration_scale) != 3 or
            any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or value <= 0.0 for value in vibration_scale)):
        flags.append("QC-006 model normalization metadata is unavailable or invalid")
    current_zero_count = metadata.get("current_zero_count")
    if (not isinstance(current_zero_count, int) or isinstance(current_zero_count, bool) or
            not 0 <= current_zero_count <= 4095):
        flags.append("QC-006 current zero calibration is unavailable or invalid")
        current_full_scale_ma = 5000.0
    else:
        current_full_scale_ma = 5000.0
    clip_limits = tuple(int(16.0 / value * 0.95) for value in vibration_g_per_count) + (
        int(current_full_scale_ma * 0.95),)
    if not rows:
        flags.append("QC-001 no validated packets")
    if not metrics:
        flags.append("QC-001 runtime metrics unavailable")
    crc_errors = _max_counter(metrics, "crc_errors", flags)
    source_restarts = _max_counter(metrics, "source_restarts", flags)
    if crc_errors:
        flags.append("QC-001 runtime CRC errors are nonzero")
    if source_restarts:
        flags.append("QC-006 STM32 restart requires a new acquisition session")

    lost = 0
    rate_samples = 0
    rate_us = 0
    values_seen = [0] * lc.CHANNEL_COUNT
    clipped = [0] * lc.CHANNEL_COUNT
    sensor_fault = False
    longest_frozen = [0] * lc.CHANNEL_COUNT
    frozen = [0] * lc.CHANNEL_COUNT
    previous_values: list[int | None] = [None] * lc.CHANNEL_COUNT
    previous_sequence: int | None = None
    previous_timestamp_us = 0
    previous_count = 0
    first_timestamp_us: int | None = None
    last_timestamp_us = 0
    last_count = 0
    indexed_conditions: set[str] = set()
    indexed_pwm: set[str] = set()
    indexed_samples = 0
    excluded_samples = round(startup_exclusion_seconds * lc.SAMPLE_RATE_HZ)
    analysis_count = 0
    analysis_sum = [0.0] * lc.CHANNEL_COUNT
    analysis_sum_squares = [0.0] * lc.CHANNEL_COUNT
    analysis_min: list[int | None] = [None] * lc.CHANNEL_COUNT
    analysis_max: list[int | None] = [None] * lc.CHANNEL_COUNT
    spectrum_rows: list[tuple[float, float, float]] = []
    drive_state_mismatch = False
    expected_offset = 0
    expected_packet_index = 0

    for row in rows:
        indexed_conditions.add(row.get("condition", ""))
        indexed_pwm.add(row.get("pwm_duty", ""))
        try:
            received_at = datetime.fromisoformat(row.get("received_utc", "").replace("Z", "+00:00"))
            if received_at.tzinfo is None:
                raise ValueError
        except (AttributeError, ValueError):
            flags.append("QC-006 packet received_utc is not a timezone-aware ISO-8601 timestamp")
        if any(str(metadata.get(field, "")) != row.get(field, "")
               for field in ("run_id", "class", "split")):
            flags.append("QC-006 index and metadata disagree")
        if assigned_split and any(
                str(metadata.get(field, "")) != row.get(field, "")
                for field in ("condition", "pwm_duty")):
            flags.append("QC-006 index and metadata condition or PWM disagree")
        if row.get("current_zero_count", "") != str(current_zero_count):
            flags.append("QC-006 packet index lacks the session current calibration")
        try:
            packet_index = int(row["packet_index"])
            offset, size, count = (int(row["raw_offset"]), int(row["raw_bytes"]),
                                   int(row["sample_count"]))
            row_sequence = int(row["sequence"])
            row_timestamp_us = int(row["timestamp_us"])
            row_flags = int(row["flags"])
        except (KeyError, TypeError, ValueError):
            flags.append("QC-001 index contains missing or invalid integers")
            previous_sequence = None
            continue
        if packet_index != expected_packet_index or offset != expected_offset:
            flags.append("QC-001 index packet order or raw offsets are not contiguous")
        expected_packet_index = packet_index + 1
        expected_offset = offset + max(size, 0)
        frame = raw[offset:offset + size]
        if (offset < 0 or size <= 0 or not 0 < count <= lc.BLOCK_SAMPLES or
                len(frame) != size or size != lc.telemetry_frame_bytes(count) or
                lc.crc16(frame[:-2]) != struct.unpack_from("<H", frame, size - 2)[0]):
            flags.append("QC-001 raw/index integrity or CRC failure")
            previous_sequence = None
            continue
        _, version_flags, frame_sequence, frame_timestamp_us, frame_count = (
            lc.TELEMETRY_STRUCT.unpack_from(frame))
        if (version_flags & 0xFF != lc.PROTOCOL_VERSION or
                not telemetry_status_valid(version_flags >> 8)):
            flags.append("QC-001 raw frame has invalid protocol version or status flags")
            previous_sequence = None
            continue
        if (frame_sequence != row_sequence or frame_timestamp_us != row_timestamp_us or
                frame_count != count or version_flags >> 8 != row_flags):
            flags.append("QC-001 raw frame and CSV index disagree")
            previous_sequence = None
            continue
        payload = struct.unpack_from("<" + "h" * (count * lc.CHANNEL_COUNT),
                                     frame, lc.TELEMETRY_HEADER_BYTES)
        discontinuity = row.get("discontinuity") == "1"
        if previous_sequence is not None:
            sequence_delta = (row_sequence - previous_sequence) & 0xFFFFFFFF
            discontinuity |= sequence_delta != 1
        if discontinuity:
            frozen = [0] * lc.CHANNEL_COUNT
            previous_values = [None] * lc.CHANNEL_COUNT
        frame_flags = row_flags
        sensor_fault |= bool(frame_flags & lc.TELEMETRY_FLAGS["LOCAL_FAULT"] or
                             not frame_flags & lc.TELEMETRY_FLAGS["SENSOR_READY"])
        if indexed_samples + count > round(startup_exclusion_seconds * lc.SAMPLE_RATE_HZ):
            try:
                expected_motor_active = int(row.get("pwm_duty", "")) > 0
            except ValueError:
                expected_motor_active = False
                drive_state_mismatch = True
            drive_state_mismatch |= expected_motor_active != bool(
                frame_flags & lc.TELEMETRY_FLAGS["MOTOR_ACTIVE"])
        for sample_index in range(count):
            sample = payload[sample_index * lc.CHANNEL_COUNT:(sample_index + 1) *
                             lc.CHANNEL_COUNT]
            for channel, value in enumerate(sample):
                values_seen[channel] += 1
                clipped[channel] += abs(value) >= clip_limits[channel]
                frozen[channel] = frozen[channel] + 1 if previous_values[channel] == value else 1
                longest_frozen[channel] = max(longest_frozen[channel], frozen[channel])
                previous_values[channel] = value
                if indexed_samples + sample_index >= excluded_samples:
                    analysis_sum[channel] += value
                    analysis_sum_squares[channel] += value * value
                    analysis_min[channel] = value if analysis_min[channel] is None else min(
                        analysis_min[channel], value)
                    analysis_max[channel] = value if analysis_max[channel] is None else max(
                        analysis_max[channel], value)
            if indexed_samples + sample_index >= excluded_samples:
                analysis_count += 1
                if len(spectrum_rows) < lc.WINDOW_SAMPLES:
                    spectrum_rows.append(tuple(
                        sample[axis] * vibration_g_per_count[axis] for axis in range(3)))
        if previous_sequence is not None:
            timestamp_delta = (row_timestamp_us - previous_timestamp_us) & 0xFFFFFFFF
            if 0 < sequence_delta < 0x80000000 and timestamp_delta:
                lost += sequence_delta - 1
                rate_samples += previous_count * sequence_delta
                rate_us += timestamp_delta
        previous_sequence = row_sequence
        previous_timestamp_us = row_timestamp_us
        previous_count = count
        if first_timestamp_us is None:
            first_timestamp_us = row_timestamp_us
        last_timestamp_us = row_timestamp_us
        last_count = count
        indexed_samples += count

    if expected_offset != len(raw):
        flags.append("QC-001 raw data has unindexed, missing or trailing bytes")

    packet_loss_percent = 100.0 * lost / (len(rows) + lost) if rows or lost else 0.0
    sample_rate_hz = rate_samples * 1_000_000.0 / rate_us if rate_us else 0.0
    clipping_percent_by_channel = [
        100.0 * channel_clipped / channel_seen if channel_seen else 0.0
        for channel_clipped, channel_seen in zip(clipped, values_seen)]
    clipping_percent = max(clipping_percent_by_channel, default=0.0)
    duration_seconds = (((last_timestamp_us - first_timestamp_us) & 0xFFFFFFFF) / 1_000_000.0 +
                        last_count / lc.SAMPLE_RATE_HZ if first_timestamp_us is not None else 0.0)
    if packet_loss_percent > 0.5:
        flags.append("QC-001 packet gap rate exceeds 0.5%")
    if sample_rate_hz and abs(sample_rate_hz - lc.SAMPLE_RATE_HZ) > lc.SAMPLE_RATE_HZ * 0.01:
        flags.append("QC-002 sample rate exceeds +/-1%")
    if clipping_percent > 1.0:
        flags.append("QC-003 more than 1% of values are at 95% sensor full scale")
    if sensor_fault or max(longest_frozen, default=0) >= lc.SAMPLE_RATE_HZ:
        flags.append("QC-004 sensor fault or at least one second frozen channel")
    if not analysis_count:
        flags.append("QC-005 no samples remain after startup exclusion")
    if drive_state_mismatch:
        flags.append("QC-007 manifest PWM and STM32 motor-active evidence disagree after startup")
    if assigned_split and duration_seconds < 90.0:
        flags.append("QC-006 session is shorter than the 90-second acquisition target")
    if assigned_split and (
            len(indexed_conditions) > 1 or len(indexed_pwm) > 1):
        flags.append("QC-006 a dataset session changes physical condition or PWM")

    vibration_features: dict[str, dict[str, float]] = {}
    axes = ("x", "y", "z")
    for axis, name in enumerate(axes):
        mean = analysis_sum[axis] / analysis_count if analysis_count else 0.0
        rms = math.sqrt(max(0.0, analysis_sum_squares[axis] / analysis_count - mean * mean)) \
            if analysis_count else 0.0
        minimum = analysis_min[axis] if analysis_min[axis] is not None else 0
        maximum = analysis_max[axis] if analysis_max[axis] is not None else 0
        peak = max(abs(minimum - mean), abs(maximum - mean))
        scale = vibration_g_per_count[axis]
        vibration_features[name] = {
            "mean_g": mean * scale,
            "rms_g": rms * scale,
            "peak_g": peak * scale,
            "crest_factor": peak / rms if rms else 0.0,
        }
    current_mean = analysis_sum[3] / analysis_count if analysis_count else 0.0
    current_rms = math.sqrt(analysis_sum_squares[3] / analysis_count) if analysis_count else 0.0
    current_min = analysis_min[3] if analysis_min[3] is not None else 0
    current_max = analysis_max[3] if analysis_max[3] is not None else 0
    current_peak = max(abs(current_min), abs(current_max))
    dominant_frequency_hz: dict[str, float | None] = {name: None for name in axes}
    dominant_magnitude_g: dict[str, float | None] = {name: None for name in axes}
    if len(spectrum_rows) == lc.WINDOW_SAMPLES:
        spectrum = vibration_spectrum(tuple(spectrum_rows))
        for axis, name in enumerate(axes):
            dominant = max(spectrum[1:], key=lambda row: row[axis + 1])
            dominant_frequency_hz[name] = dominant[0]
            dominant_magnitude_g[name] = dominant[axis + 1]

    return {
        "metadata": str(metadata_path),
        "run_id": metadata.get("run_id", "") if isinstance(metadata.get("run_id"), str) else "",
        "class": metadata.get("class", "") if isinstance(metadata.get("class"), str) else "",
        "split": split if isinstance(split, str) else "",
        "pwm_duty": metadata.get("pwm_duty", ""),
        "class_contract": class_contract,
        "class_fallback_reason": fallback_reason,
        "current_zero_count": current_zero_count,
        "packets": len(rows), "lost_packets": lost,
        "crc_errors": crc_errors,
        "source_restarts": source_restarts,
        "packet_loss_percent": packet_loss_percent, "sample_rate_hz": sample_rate_hz,
        "duration_seconds": duration_seconds,
        "clipping_percent": clipping_percent,
        "clipping_percent_by_channel": clipping_percent_by_channel,
        "longest_frozen_samples": max(longest_frozen, default=0),
        "startup_exclusion_samples": min(min(values_seen, default=0),
                                         excluded_samples),
        "analysis_samples": analysis_count,
        "signal_features": {
            "vibration": vibration_features,
            "current": {"mean_ma": current_mean, "rms_ma": current_rms,
                        "peak_ma": float(current_peak)},
            "dominant_frequency_hz": dominant_frequency_hz,
            "dominant_magnitude_g": dominant_magnitude_g,
        },
        "raw_sha256": hashlib.sha256(raw).hexdigest(), "flags": sorted(set(flags)),
    }


# 여러 세션을 묶어 '데이터셋 전체' 규칙을 검사한다(세션 단위 검사는 audit_session 결과를 재사용).
# 핵심: 같은 run_id가 여러 split(train/validation/test)에 있으면 QC-008 — 학습 데이터 누수 방지.
# 그 밖에 중복 run/raw(QC-010), 클래스 균형과 세션 수(QC-009), 클래스당 4/1/1 split(QC-008)을 본다.
def audit_sessions(metadata_paths: list[Path]) -> dict[str, Any]:
    sessions = [audit_session(path) for path in metadata_paths]
    flags: list[str] = []
    run_splits: dict[str, set[str]] = {}
    run_counts: dict[str, int] = {}
    hashes: dict[str, set[str]] = {}
    class_sessions: dict[str, int] = {}
    class_pwm: dict[str, set[str]] = {}
    class_splits: dict[str, dict[str, int]] = {}
    class_contracts: set[tuple[str, ...]] = set()
    fallback_reasons: set[str] = set()
    for session in sessions:
        run_splits.setdefault(session["run_id"], set()).add(session["split"])
        run_counts[session["run_id"]] = run_counts.get(session["run_id"], 0) + 1
        hashes.setdefault(session["raw_sha256"], set()).add(session["run_id"])
        class_sessions[session["class"]] = class_sessions.get(session["class"], 0) + 1
        class_pwm.setdefault(session["class"], set()).add(str(session["pwm_duty"]))
        splits = class_splits.setdefault(session["class"], {})
        splits[session["split"]] = splits.get(session["split"], 0) + 1
        if session["class_contract"]:
            class_contracts.add(tuple(session["class_contract"]))
        if len(session["class_contract"]) == 3:
            fallback_reasons.add(session["class_fallback_reason"])
    if any(len(splits - {"unassigned", ""}) > 1 for splits in run_splits.values()):
        flags.append("QC-008 one run_id appears in multiple dataset splits")
    if any(count > 1 for count in run_counts.values()):
        flags.append("QC-010 duplicate run_id")
    if any(len(run_ids) > 1 for run_ids in hashes.values()):
        flags.append("QC-010 duplicate raw data exists under different run_id values")
    if len(class_contracts) > 1:
        flags.append("QC-006 sessions use inconsistent class contracts")
    if len(fallback_reasons) > 1:
        flags.append("QC-006 sessions use inconsistent three-class fallback reasons")
    if len(class_contracts) == 1 and set(class_sessions) != set(next(iter(class_contracts))):
        flags.append("QC-009 dataset does not contain every class in its class contract")
    if class_sessions and max(class_sessions.values()) - min(class_sessions.values()) > 1:
        flags.append("QC-009 class session counts differ by more than one")
    if any(count < 6 for count in class_sessions.values()):
        flags.append("QC-009 a class has fewer than six acquisition sessions")
    if any(splits.get("train", 0) < 4 or splits.get("validation", 0) < 1 or
           splits.get("test", 0) < 1 for splits in class_splits.values()):
        flags.append("QC-008 a class does not meet the 4 train / 1 validation / 1 test session plan")
    if any(class_sessions[label] > 1 and len(values) < 2
           for label, values in class_pwm.items()):
        flags.append("QC-007 a class with multiple sessions uses only one PWM condition")
    return {"sessions": sessions, "class_sessions": class_sessions,
            "flags": sorted(set(flags + [flag for session in sessions
                                          for flag in session["flags"]]))}


# 명령행 진입점. 사용: data_qc.py <metadata.json ...> [--output 리포트.json] [--strict]
# --strict 이면 flag가 하나라도 있을 때 종료 코드 1. 입출력 오류는 코드 2.
def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("metadata", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strict", action="store_true", help="exit nonzero when a QC flag exists")
    args = parser.parse_args()
    try:
        if args.output:
            _validate_output_path(args.output, args.metadata)
        report = audit_sessions(args.metadata)
    except (OSError, ValueError, csv.Error, struct.error) as error:
        print(f"[data-qc] ERROR: {error}", file=sys.stderr)
        return 2
    rendered = json.dumps(report, indent=2, ensure_ascii=False) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8", newline="\n")
    else:
        print(rendered, end="")
    return int(args.strict and bool(report["flags"]))


if __name__ == "__main__":
    raise SystemExit(main())
