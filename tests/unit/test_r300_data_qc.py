from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "edge"))
sys.path.insert(0, str(ROOT / "scripts" / "r300"))

from data_qc import _validate_output_path, audit_session, audit_sessions
from r300_link import EdgePipeline, EventLogger, InferenceResult, ReceiverService
from r300_link import link_contract as lc
from test_r300_runtime import telemetry_frame


def _session(tmp_path: Path, stem: str, run_id: str, split: str, sequence: int = 0,
             *, frames: int = 2, sequence_step: int = 1,
             timestamp_step: int = lc.FRAME_PERIOD_MS * 1000,
             constant: bool = False, crc_errors: int = 0,
             vibration_g_per_count: float = 0.00048828125,
             startup_exclusion_seconds: float = 10.0) -> Path:
    logger = EventLogger(tmp_path / f"{stem}.jsonl", {
        "protocol_version": lc.PROTOCOL_VERSION, "contract_sha256": lc.CONTRACT_SHA256,
        "run_id": run_id, "class": "NORMAL", "pwm_duty": 600,
        "condition": "healthy", "start_utc": "2026-09-02T00:00:00Z",
        "sensor_cfg": "mpu6050_1000hz_acs712_1000hz", "notes": "",
        "fixture": "rig-a", "power_cycle": 1, "mount": "mount-a",
        "split": split, "label_evidence": "photos/run.jpg",
        "startup_exclusion_seconds": startup_exclusion_seconds,
        "class_contract": ["NORMAL", "UNBALANCE", "LOOSENESS", "LOAD"],
        "class_fallback_reason": "",
        "condition_policy": {"window_size": 5, "required_abnormal": 4,
                             "confidence_threshold": 0.85, "clear_normal": 5},
        "current_zero_count": 1000,
        "vibration_g_per_count": [vibration_g_per_count] * 3,
        "vibration_mean": [0.0, 0.0, 0.0], "vibration_scale": [1.0, 1.0, 1.0]})
    service = ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0)), logger)
    samples = (((32767, 32767, 32767, 32767),) * 128 if constant else
               tuple((index, index + 1, index + 2, index + 3) for index in range(128)))
    for index in range(frames):
        service.decode(telemetry_frame(sequence + index * sequence_step, samples,
                                       timestamp_us=index * timestamp_step, flags=1),
                       f"2026-09-02T00:{index // 60:02d}:{index % 60:02d}Z")
    logger.write("2026-09-02T00:00:00.080Z", "metrics", crc_errors=crc_errors)
    logger.close()
    return logger.metadata_path


def test_session_qc_measures_rate_loss_and_artifact_integrity(tmp_path: Path) -> None:
    report = audit_session(_session(tmp_path, "run-a", "R1", "train"))
    assert report["packets"] == 2 and report["lost_packets"] == 0
    assert report["sample_rate_hz"] == 1000.0
    assert report["packet_loss_percent"] == 0.0
    assert report["flags"] == [
        "QC-005 no samples remain after startup exclusion",
        "QC-006 session is shorter than the 90-second acquisition target",
    ]


def test_suite_qc_rejects_run_leakage_and_duplicate_raw_data(tmp_path: Path) -> None:
    first = _session(tmp_path, "run-a", "R1", "train")
    second = _session(tmp_path, "run-b", "R1", "test")
    third = _session(tmp_path, "run-c", "R2", "train")
    report = audit_sessions([first, second, third])
    assert "QC-008 one run_id appears in multiple dataset splits" in report["flags"]
    assert "QC-010 duplicate run_id" in report["flags"]
    assert "QC-010 duplicate raw data exists under different run_id values" in report["flags"]


def test_suite_qc_requires_every_class_and_one_consistent_class_contract(tmp_path: Path) -> None:
    paths = [_session(tmp_path, f"normal-{index}", f"N{index}",
                      "train" if index < 4 else "validation" if index == 4 else "test")
             for index in range(6)]
    report = audit_sessions(paths)
    assert "QC-009 dataset does not contain every class in its class contract" in report["flags"]

    metadata = json.loads(paths[-1].read_text(encoding="utf-8"))
    metadata["class_contract"] = ["NORMAL", "UNBALANCE", "LOOSENESS"]
    metadata["class_fallback_reason"] = "LOAD pilot was not separable"
    paths[-1].write_text(json.dumps(metadata), encoding="utf-8")
    assert "QC-006 sessions use inconsistent class contracts" in audit_sessions(paths)["flags"]


def test_session_qc_flags_loss_rate_drift_clipping_frozen_and_crc_errors(tmp_path: Path) -> None:
    report = audit_session(_session(tmp_path, "bad", "BAD", "train", frames=13,
                                    sequence_step=2, timestamp_step=100_000,
                                    constant=True, crc_errors=2))
    flags = "\n".join(report["flags"])
    assert "QC-001 packet gap rate" in flags and "runtime CRC" in flags
    assert "QC-002 sample rate" in flags and "QC-003" in flags
    frozen = audit_session(_session(tmp_path, "frozen", "FROZEN", "train",
                                    frames=13, constant=True))
    assert any("QC-004" in flag for flag in frozen["flags"])


def test_session_qc_uses_the_recorded_vibration_calibration_for_clipping(tmp_path: Path) -> None:
    metadata_path = _session(tmp_path, "calibrated", "CAL", "train",
                             vibration_g_per_count=0.0078)
    raw_path = tmp_path / "calibrated.raw.bin"
    raw = bytearray(raw_path.read_bytes())
    for frame_offset in (0, len(raw) // 2):
        for sample in range(128):
            offset = frame_offset + 14 + sample * 8
            raw[offset:offset + 2] = (2000).to_bytes(2, "little", signed=True)
        crc_offset = frame_offset + 1038
        crc = lc.crc16(raw[frame_offset:crc_offset])
        raw[crc_offset:crc_offset + 2] = crc.to_bytes(2, "little")
    raw_path.write_bytes(raw)
    report = audit_session(metadata_path)
    assert report["clipping_percent_by_channel"][0] == 100.0
    assert "QC-003 more than 1% of values are at 95% sensor full scale" in report["flags"]


def test_session_qc_rejects_unindexed_raw_bytes_and_invalid_metadata(tmp_path: Path) -> None:
    metadata_path = _session(tmp_path, "corrupt", "CORRUPT", "train")
    raw_path = tmp_path / "corrupt.raw.bin"
    raw_path.write_bytes(raw_path.read_bytes() + b"trailing")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["vibration_scale"] = [1.0, 0.0, 1.0]
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    flags = "\n".join(audit_session(metadata_path)["flags"])
    assert "QC-001 raw data has unindexed" in flags
    assert "QC-006 model normalization" in flags


def test_session_qc_flags_malformed_metadata_and_metric_record_types(tmp_path: Path) -> None:
    metadata_path = _session(tmp_path, "types", "TYPES", "train")
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata.update(split=["train"], notes=[], label_evidence={},
                    class_contract=[{"label": "NORMAL"}], condition_policy={})
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    metrics_path = tmp_path / "types.metrics.jsonl"
    metrics_path.write_text(metrics_path.read_text(encoding="utf-8") + "[]\n",
                            encoding="utf-8")
    flags = "\n".join(audit_session(metadata_path)["flags"])
    assert "session split is unassigned or invalid" in flags
    assert "class and class_contract are inconsistent" in flags
    assert "runtime metrics contain a non-object record" in flags
    assert "notes metadata is not a string" in flags
    assert "label evidence is unassigned" in flags
    assert "condition voting policy is missing or inconsistent" in flags


def test_session_qc_flags_malformed_runtime_counter_without_crashing(tmp_path: Path) -> None:
    metadata_path = _session(tmp_path, "metrics-types", "METRICS", "train")
    metrics_path = tmp_path / "metrics-types.metrics.jsonl"
    metrics_path.write_text(
        '{"crc_errors":[],"source_restarts":-1}\n', encoding="utf-8")
    flags = "\n".join(audit_session(metadata_path)["flags"])
    assert "runtime metric crc_errors is not a non-negative counter" in flags
    assert "runtime metric source_restarts is not a non-negative counter" in flags


def test_session_qc_rejects_semantically_impossible_raw_status(tmp_path: Path) -> None:
    metadata_path = _session(tmp_path, "status", "STATUS", "train")
    raw_path = tmp_path / "status.raw.bin"
    raw = bytearray(raw_path.read_bytes())
    impossible = lc.TELEMETRY_FLAGS["AVR_ARMED"]
    raw[2:4] = (lc.PROTOCOL_VERSION | impossible << 8).to_bytes(2, "little")
    raw[1038:1040] = lc.crc16(raw[:1038]).to_bytes(2, "little")
    raw_path.write_bytes(raw)
    assert "QC-001 raw frame has invalid protocol version or status flags" in audit_session(
        metadata_path)["flags"]


def test_qc_output_cannot_overwrite_session_evidence(tmp_path: Path) -> None:
    metadata_path = _session(tmp_path, "protected", "PROTECTED", "train")
    try:
        _validate_output_path(tmp_path / "protected.raw.bin", [metadata_path])
    except ValueError as error:
        assert "must not overwrite" in str(error)
    else:
        raise AssertionError("QC output must preserve source evidence")


def test_session_qc_crosschecks_requested_pwm_with_stm_motor_evidence(tmp_path: Path) -> None:
    metadata_path = _session(tmp_path, "stopped", "STOPPED", "train", frames=64,
                             startup_exclusion_seconds=5.0)
    assert any("manifest PWM and STM32 motor-active" in flag
               for flag in audit_session(metadata_path)["flags"])


def test_session_qc_reports_stable_signal_features_and_dominant_frequency(tmp_path: Path) -> None:
    report = audit_session(_session(tmp_path, "features", "FEATURES", "train", frames=5,
                                    startup_exclusion_seconds=5.0))
    assert report["analysis_samples"] == 0

    long_path = _session(tmp_path, "features-long", "FEATURES-LONG", "train", frames=68,
                         startup_exclusion_seconds=5.0)
    long_report = audit_session(long_path)
    assert long_report["analysis_samples"] == 68 * lc.BLOCK_SAMPLES - 5 * lc.SAMPLE_RATE_HZ
    assert long_report["signal_features"]["vibration"]["x"]["rms_g"] > 0.0
    assert long_report["signal_features"]["vibration"]["x"]["crest_factor"] > 1.0
    assert long_report["signal_features"]["current"]["rms_ma"] > 0.0
    assert long_report["signal_features"]["dominant_frequency_hz"]["x"] is not None
