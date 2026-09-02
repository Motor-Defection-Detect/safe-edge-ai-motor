from __future__ import annotations

import json
import csv
import struct
import sys
import math
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "edge"))
from r300_link import (EdgePipeline, EventLogger, InferenceResult, ReceiverService, Telemetry,
                       TelemetryParser, health_frame, motor_command_frame)
from r300_link import link_contract as lc


def telemetry_frame(sequence: int, samples: tuple[tuple[int, int, int, int], ...], flags: int = 0,
                    timestamp_us: int = 1234) -> bytes:
    body = lc.TELEMETRY_STRUCT.pack(
        lc.TELEMETRY_SOF, lc.PROTOCOL_VERSION | flags << 8, sequence, timestamp_us, len(samples))
    body += struct.pack("<" + "h" * (len(samples) * lc.CHANNEL_COUNT), *(value for row in samples for value in row))
    return body + struct.pack("<H", lc.crc16(body))


def stm_diagnostic_frame(sequence: int = 0, *, timestamp_ms: int = 1000,
                         status_flags: int | None = None) -> bytes:
    if status_flags is None:
        status_flags = (lc.STM_DIAGNOSTIC_FLAGS["KEEPALIVE_VALID"] |
                        lc.STM_DIAGNOSTIC_FLAGS["LOCAL_HEALTHY"])
    body = lc.STM_DIAGNOSTIC_STRUCT.pack(
        lc.STM_DIAGNOSTIC_SOF, lc.PROTOCOL_VERSION, sequence, timestamp_ms, 7,
        1, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4, 5, 6, 1000, 12, status_flags)
    return body + struct.pack("<H", lc.crc16(body))


def test_parser_resynchronizes_and_counts_sequence_gaps() -> None:
    parser = TelemetryParser()
    first = telemetry_frame(4, ((1, 2, 3, 4),), lc.TELEMETRY_FLAGS["SENSOR_READY"])
    second = telemetry_frame(7, ((5, 6, 7, 8),))
    assert list(parser.feed(b"noise" + first[:5])) == []
    frames = list(parser.feed(first[5:] + second))
    assert [frame.sequence for frame in frames] == [4, 7]
    assert frames[0].flags == lc.TELEMETRY_FLAGS["SENSOR_READY"]
    assert not frames[0].discontinuity and frames[1].discontinuity
    assert parser.sequence_gaps == 2


def test_parser_rejects_bad_crc_without_losing_the_following_frame() -> None:
    parser = TelemetryParser()
    bad = bytearray(telemetry_frame(1, ((1, 2, 3, 4),)))
    bad[-1] ^= 1
    assert [frame.sequence for frame in parser.feed(bytes(bad) + telemetry_frame(2, ((1, 2, 3, 4),)))] == [2]
    assert parser.invalid_frames == 1 and parser.crc_errors == 1


def test_parser_rejects_zero_sample_frames() -> None:
    parser = TelemetryParser()
    assert list(parser.feed(telemetry_frame(1, ()))) == []
    assert parser.invalid_frames == 1 and parser.valid_frames == 0


def test_parser_preserves_every_contract_telemetry_flag() -> None:
    parser = TelemetryParser()
    flags = (lc.TELEMETRY_FLAGS["SENSOR_READY"] |
             lc.TELEMETRY_FLAGS["MOTOR_ACTIVE"] |
             lc.TELEMETRY_FLAGS["AVR_DIAG_VALID"] |
             lc.TELEMETRY_FLAGS["AVR_TRIPPED"] |
             lc.TELEMETRY_FLAGS["AVR_ESTOP"])
    parsed = list(parser.feed(telemetry_frame(1, ((1, 2, 3, 4),), flags)))
    assert parsed[0].flags == flags and parser.invalid_frames == 0


def test_parser_rejects_semantically_impossible_telemetry_status() -> None:
    impossible = (
        lc.TELEMETRY_FLAGS["AVR_ARMED"],
        lc.TELEMETRY_FLAGS["AVR_DIAG_VALID"] | lc.TELEMETRY_FLAGS["AVR_ARMED"] |
        lc.TELEMETRY_FLAGS["AVR_TRIPPED"],
        lc.TELEMETRY_FLAGS["AVR_DIAG_VALID"] | lc.TELEMETRY_FLAGS["AVR_ESTOP"],
        lc.TELEMETRY_FLAGS["SENSOR_READY"] | lc.TELEMETRY_FLAGS["LOCAL_FAULT"] |
        lc.TELEMETRY_FLAGS["MOTOR_ACTIVE"],
    )
    parser = TelemetryParser()
    stream = b"".join(telemetry_frame(index, ((1, 2, 3, 4),), flags)
                      for index, flags in enumerate(impossible))
    assert list(parser.feed(stream)) == []
    assert parser.invalid_frames == len(impossible)


def test_parser_consumes_interleaved_stm_diagnostics_and_rejects_invalid_status() -> None:
    parser = TelemetryParser()
    bad = bytearray(stm_diagnostic_frame(status_flags=(
        lc.STM_DIAGNOSTIC_FLAGS["ACQUISITION_OVERRUN"] |
        lc.STM_DIAGNOSTIC_FLAGS["LOCAL_HEALTHY"])))
    bad[-2:] = struct.pack("<H", lc.crc16(bad[:-2]))
    bad_live = stm_diagnostic_frame(1, status_flags=
                                    lc.STM_DIAGNOSTIC_FLAGS["AVR_RELAY_COMMAND"])
    stream = bytes(bad) + bad_live + telemetry_frame(4, ((1, 2, 3, 4),)) + \
        stm_diagnostic_frame(2)
    assert [item.sequence for item in parser.feed(stream)] == [4]
    diagnostics = parser.take_diagnostics()
    assert len(diagnostics) == 1 and diagnostics[0].sequence == 2
    assert diagnostics[0].dropped_blocks == 1 and diagnostics[0].buffered_samples == 12
    assert diagnostics[0].current_zero_count == 1000
    assert parser.invalid_diagnostic_frames == 2 and parser.diagnostic_crc_errors == 0


def test_parser_drops_duplicates_and_old_frames_without_inflating_loss() -> None:
    parser = TelemetryParser()
    stream = telemetry_frame(8, ((1, 2, 3, 4),))
    stream += telemetry_frame(8, ((1, 2, 3, 4),))
    stream += telemetry_frame(7, ((1, 2, 3, 4),))
    assert [item.sequence for item in parser.feed(stream)] == [8]
    assert parser.duplicate_frames == 1 and parser.out_of_order_frames == 1
    assert parser.sequence_gaps == 0


def test_parser_accepts_uint32_sequence_wrap() -> None:
    parser = TelemetryParser()
    stream = telemetry_frame(0xFFFFFFFF, ((1, 2, 3, 4),))
    stream += telemetry_frame(0, ((1, 2, 3, 4),))
    assert [item.sequence for item in parser.feed(stream)] == [0xFFFFFFFF, 0]
    assert parser.sequence_gaps == 0


def test_parser_accepts_an_explicit_stm_restart_as_a_discontinuity() -> None:
    parser = TelemetryParser()
    stream = telemetry_frame(10, ((1, 2, 3, 4),), timestamp_us=5000)
    stream += telemetry_frame(0, ((1, 2, 3, 4),), timestamp_us=10)
    frames = list(parser.feed(stream))
    assert [item.sequence for item in frames] == [10, 0]
    assert frames[-1].discontinuity and parser.source_restarts == 1
    assert parser.out_of_order_frames == 0 and parser.sequence_gaps == 0


def test_parser_recovers_when_the_first_frames_after_stm_restart_were_missed() -> None:
    parser = TelemetryParser()
    stream = telemetry_frame(100, ((1, 2, 3, 4),), timestamp_us=5_000_000)
    stream += telemetry_frame(2, ((1, 2, 3, 4),), timestamp_us=160_000)
    frames = list(parser.feed(stream))
    assert [item.sequence for item in frames] == [100, 2]
    assert frames[-1].discontinuity and parser.source_restarts == 1


def test_diagnostic_parser_recovers_after_a_missed_restart_frame() -> None:
    parser = TelemetryParser()
    list(parser.feed(stm_diagnostic_frame(100, timestamp_ms=5_000)))
    list(parser.feed(stm_diagnostic_frame(2, timestamp_ms=200)))
    assert parser.latest_diagnostic is not None and parser.latest_diagnostic.sequence == 2
    assert parser.diagnostic_frames == 2 and parser.invalid_diagnostic_frames == 0


def test_health_frame_is_contract_sized_and_crc_protected() -> None:
    frame = health_frame(9, 100, lc.HEALTH_FLAGS["APP_ALIVE"])
    assert len(frame) == lc.HEALTH_BYTES
    assert lc.crc16(frame[:-2]) == struct.unpack_from("<H", frame, len(frame) - 2)[0]


def test_health_frame_rejects_missing_alive_and_reserved_flags() -> None:
    for flags in (0, 0x20 | lc.HEALTH_FLAGS["APP_ALIVE"]):
        try:
            health_frame(0, 0, flags)
        except ValueError as error:
            assert "reserved" in str(error)
        else:
            raise AssertionError("invalid HEALTH flags must fail before transmission")


def test_motor_command_frame_is_bounded_crc_protected_and_fail_safe() -> None:
    frame = motor_command_frame(4, 100, 500, lc.MOTOR_DIRECTIONS["FORWARD"])
    assert len(frame) == lc.MOTOR_COMMAND_BYTES
    assert lc.MOTOR_COMMAND_STRUCT.unpack_from(frame)[4:] == (500, lc.MOTOR_DIRECTIONS["FORWARD"])
    assert lc.crc16(frame[:-2]) == struct.unpack_from("<H", frame, len(frame) - 2)[0]
    for duty, direction in ((1001, lc.MOTOR_DIRECTIONS["FORWARD"]),
                            (1, lc.MOTOR_DIRECTIONS["STOP"]), (0, 2)):
        try:
            motor_command_frame(0, 0, duty, direction)
        except ValueError:
            pass
        else:
            raise AssertionError("unsafe motor command must fail before transmission")


def test_event_logger_writes_timestamped_json_lines(tmp_path: Path) -> None:
    logger = EventLogger(tmp_path / "events.jsonl")
    logger.write("2026-09-01T00:00:00Z", "inference", label="NORMAL")
    logger.close()
    assert (tmp_path / "events.jsonl").read_text(encoding="utf-8") == (
        '{"timestamp":"2026-09-01T00:00:00Z","event":"inference","label":"NORMAL"}\n'
    )


def test_event_logger_refuses_to_append_to_an_existing_run(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    path.write_text("old run\n", encoding="utf-8")
    try:
        EventLogger(path)
    except FileExistsError as error:
        assert "already exist" in str(error)
    else:
        raise AssertionError("a run logger must not append to previous evidence")


def test_event_logger_applies_and_updates_run_context(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, {"run_id": "R1", "pwm_duty": 0})
    logger.set_context(pwm_duty=500)
    logger.write("now", "telemetry")
    logger.close()
    record = json.loads(path.read_text(encoding="utf-8"))
    assert record["run_id"] == "R1" and record["pwm_duty"] == 500
    metadata = json.loads(logger.metadata_path.read_text(encoding="utf-8"))
    assert metadata == {"run_id": "R1", "pwm_duty": 500}


def test_event_logger_exposes_storage_failure(tmp_path: Path) -> None:
    logger = EventLogger(tmp_path / "missing" / "events.jsonl")
    logger.close()
    assert not logger.ok and isinstance(logger.error, OSError)
    logger.write("now", "event")
    assert logger.dropped_records == 1


def test_event_logger_rejects_missing_timestamp(tmp_path: Path) -> None:
    logger = EventLogger(tmp_path / "events.jsonl")
    try:
        logger.write("", "event")
    except ValueError as error:
        assert "timestamp" in str(error)
    else:
        raise AssertionError("timestamp is mandatory")
    try:
        logger.write_telemetry("now", Telemetry(0, 0, ((1, 2, 3, 4),)))
    except ValueError as error:
        assert "raw frame" in str(error)
    else:
        raise AssertionError("raw evidence must come from a validated frame")
    finally:
        logger.close()


def test_receiver_logs_validated_raw_telemetry_with_sequence_metadata(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path, {"run_id": "R1", "class": "NORMAL",
                                "condition": "healthy", "pwm_duty": 250})
    service = ReceiverService(EdgePipeline(lambda vibration, current: InferenceResult("NORMAL", 1.0)), logger)
    frame = telemetry_frame(9, ((1, 2, 3, 4),))
    decoded = service.decode(stm_diagnostic_frame() + frame, "2026-09-01T00:00:00Z")
    logger.close()
    record = path.read_text(encoding="utf-8")
    assert decoded[0].received_ns > 0 and decoded[0].raw_frame == frame
    assert '"event":"stm_diagnostic"' in record and '"dropped_blocks":1' in record
    assert '"event":"telemetry"' not in record
    assert logger.raw_path.read_bytes() == frame
    index = next(csv.DictReader(logger.index_path.open(encoding="utf-8", newline="")))
    assert index["packet_index"] == "0" and index["run_id"] == "R1"
    assert index["sequence"] == "9" and index["raw_offset"] == "0"
    assert index["raw_bytes"] == str(len(frame)) and index["sample_count"] == "1"
    assert json.loads(logger.metadata_path.read_text(encoding="utf-8"))["current_zero_count"] == 1000


def test_inference_events_do_not_overwrite_the_physical_manifest_condition(tmp_path: Path) -> None:
    logger = EventLogger(tmp_path / "events.jsonl", {"condition": "fixture_loosened"})
    service = ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0)), logger)
    block = tuple((1, 2, 3, 4) for _ in range(lc.BLOCK_SAMPLES))
    for sequence in range(4):
        service.process(Telemetry(sequence, sequence, block), "now")
    logger.close()
    records = [json.loads(line) for line in logger.path.read_text(
        encoding="utf-8").splitlines()]
    inference = next(record for record in records if record["event"] == "inference")
    assert inference["condition"] == "fixture_loosened"
    assert inference["ai_condition"] == "NORMAL"
    assert json.loads(logger.metadata_path.read_text(encoding="utf-8"))["condition"] == (
        "fixture_loosened")


def test_pipeline_emits_512_sample_window_then_every_256_samples() -> None:
    pipeline = EdgePipeline(lambda vibration, current: InferenceResult("UNBALANCE", 0.9))
    block = tuple((index, index + 1, index + 2, index + 3) for index in range(lc.BLOCK_SAMPLES))
    outputs = [pipeline.append(Telemetry(index, index, block)) for index in range(4)]
    assert outputs[:-1] == [None, None, None]
    assert outputs[-1] is not None
    assert len(outputs[-1].vibration) == lc.WINDOW_SAMPLES
    assert outputs[-1].inference_latency_ms >= 0.0
    assert pipeline.append(Telemetry(4, 4, block)) is None
    assert pipeline.append(Telemetry(5, 5, block)) is not None
    assert 0.0 <= pipeline.inference_latency_percentile(50) <= pipeline.inference_latency_percentile(95)


def test_pipeline_preserves_hop_remainder_for_partial_telemetry_blocks() -> None:
    pipeline = EdgePipeline(lambda vibration, current: InferenceResult("NORMAL", 1.0))
    block = tuple((1, 2, 3, 4) for _ in range(127))
    results = [pipeline.append(Telemetry(index, index, block)) for index in range(12)]
    assert [index for index, result in enumerate(results) if result is not None] == [4, 7, 9, 11]


def test_receiver_does_not_mix_samples_across_a_sequence_gap() -> None:
    service = ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0)))
    block = tuple((1, 2, 3, 4) for _ in range(lc.BLOCK_SAMPLES))
    for sequence in range(3):
        assert service.process(Telemetry(sequence, sequence, block), "now") is None
    assert service.process(Telemetry(4, 4, block, discontinuity=True), "now") is None
    assert service.process(Telemetry(5, 5, block), "now") is None
    assert service.process(Telemetry(6, 6, block), "now") is None
    assert service.process(Telemetry(7, 7, block), "now") is not None


def test_pipeline_applies_training_statistics_after_dc_removal() -> None:
    captured = []
    pipeline = EdgePipeline(lambda vibration, current: captured.append(vibration) or InferenceResult("NORMAL", 1.0),
                            vibration_mean=(1.0, 2.0, 3.0), vibration_scale=(2.0, 2.0, 2.0))
    block = tuple((10, 20, 30, 4) for _ in range(lc.BLOCK_SAMPLES))
    for sequence in range(4):
        pipeline.append(Telemetry(sequence, sequence, block))
    assert captured[0][0] == (-0.5, -1.0, -1.5)


def test_pipeline_keeps_display_units_separate_from_model_normalization() -> None:
    pipeline = EdgePipeline(lambda vibration, current: InferenceResult("NORMAL", 1.0),
                            vibration_mean=(100.0, 100.0, 100.0),
                            vibration_g_per_count=(0.01, 0.02, 0.03))
    block = tuple((index, index + 1, index + 2, 4) for index in range(lc.BLOCK_SAMPLES))
    result = None
    for sequence in range(4):
        result = pipeline.append(Telemetry(sequence, sequence, block))
    assert result is not None
    assert all(math.isclose(value, expected) for value, expected in zip(
        result.display_vibration[0], (0.0, 0.02, 0.06)))
    assert result.vibration[0] == (-163.5, -163.5, -163.5)


def test_pipeline_rejects_non_finite_training_statistics() -> None:
    try:
        EdgePipeline(lambda vibration, current: InferenceResult("NORMAL", 1.0),
                     vibration_mean=(math.nan, 0.0, 0.0))
    except ValueError as error:
        assert "finite" in str(error)
    else:
        raise AssertionError("non-finite normalization must fail closed")
    for values in ((1.0, 2.0), (1.0, True, 3.0)):
        try:
            EdgePipeline(lambda vibration, current: InferenceResult("NORMAL", 1.0),
                         vibration_mean=values)
        except ValueError as error:
            assert "three finite" in str(error)
        else:
            raise AssertionError("invalid normalization shape or type must fail at startup")


def test_pipeline_fails_closed_when_finite_statistics_overflow_preprocessing() -> None:
    pipeline = EdgePipeline(lambda vibration, current: InferenceResult("NORMAL", 1.0),
                            vibration_mean=(1e308, 0.0, 0.0),
                            vibration_scale=(1e-308, 1.0, 1.0))
    block = tuple((1, 2, 3, 4) for _ in range(lc.BLOCK_SAMPLES))
    try:
        for sequence in range(4):
            pipeline.append(Telemetry(sequence, sequence, block))
    except ValueError as error:
        assert "non-finite" in str(error)
    else:
        raise AssertionError("non-finite preprocessed values must not reach inference")
    assert pipeline.inference_attempted and not pipeline.inference_ok


def test_pipeline_rejects_an_empty_inference_label() -> None:
    pipeline = EdgePipeline(lambda vibration, current: InferenceResult("", 0.9))
    block = tuple((1, 2, 3, 4) for _ in range(lc.BLOCK_SAMPLES))
    try:
        for sequence in range(4):
            pipeline.append(Telemetry(sequence, sequence, block))
    except ValueError as error:
        assert "non-empty" in str(error)
    else:
        raise AssertionError("an empty model label must fail closed")


def test_pipeline_rejects_non_numeric_inference_confidence() -> None:
    pipeline = EdgePipeline(lambda vibration, current: InferenceResult("NORMAL", "high"))
    block = tuple((1, 2, 3, 4) for _ in range(lc.BLOCK_SAMPLES))
    try:
        for sequence in range(4):
            pipeline.append(Telemetry(sequence, sequence, block))
    except ValueError as error:
        assert "confidence" in str(error)
    else:
        raise AssertionError("non-numeric confidence must fail at the model boundary")


def test_pipeline_rejects_labels_outside_the_configured_three_or_four_classes() -> None:
    try:
        EdgePipeline(lambda vibration, current: InferenceResult("NORMAL", 1.0),
                     allowed_labels=("NORMAL", "BEARING"))
    except ValueError as error:
        assert "three-class" in str(error)
    else:
        raise AssertionError("an undocumented model label set must fail at startup")

    pipeline = EdgePipeline(lambda vibration, current: InferenceResult("LOAD", 0.9),
                            allowed_labels=("NORMAL", "UNBALANCE", "LOOSENESS"))
    block = tuple((1, 2, 3, 4) for _ in range(lc.BLOCK_SAMPLES))
    try:
        for sequence in range(4):
            pipeline.append(Telemetry(sequence, sequence, block))
    except ValueError as error:
        assert "class contract" in str(error)
    else:
        raise AssertionError("an unexpected inference class must fail closed")


def test_receiver_service_keeps_going_after_an_inference_error() -> None:
    calls = 0

    def infer(vibration: object, current: object) -> InferenceResult:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("model unavailable")
        return InferenceResult("NORMAL", 1.0)

    service = ReceiverService(EdgePipeline(infer))
    block = tuple((1, 2, 3, 4) for _ in range(lc.BLOCK_SAMPLES))
    assert service.receive(b"".join(telemetry_frame(index, block) for index in range(4)), "now") == ()
    assert service.pipeline.inference_attempted and not service.pipeline.inference_ok
    assert len(service.receive(telemetry_frame(4, block) + telemetry_frame(5, block), "now")) == 1
    health = service.next_health_frame(20, model_loaded=True, storage_ok=True,
                                       link_healthy=True)
    assert health[4:8] == b"\x00\x00\x00\x00"
    assert (lc.HEALTH_STRUCT.unpack_from(health)[-1] & lc.HEALTH_FLAGS["LINK_HEALTHY"])


def test_voter_requires_four_high_confidence_abnormal_windows_and_clear_hysteresis() -> None:
    predictions = iter([("UNBALANCE", 0.9), ("UNBALANCE", 0.8), ("UNBALANCE", 0.9),
                        ("NORMAL", 1.0), ("UNBALANCE", 0.9), ("UNBALANCE", 0.9),
                        ("UNBALANCE", 0.9), *(("NORMAL", 1.0),) * 5])
    pipeline = EdgePipeline(lambda vibration, current: InferenceResult(*next(predictions)))
    block = tuple((1, 2, 3, 4) for _ in range(lc.BLOCK_SAMPLES))
    conditions = []
    for sequence in range(26):
        result = pipeline.append(Telemetry(sequence, sequence, block))
        if result:
            conditions.append(result.condition)
    assert conditions[4] == "NORMAL"
    assert conditions[6] == "WARNING"
    assert conditions[-1] == "NORMAL"
