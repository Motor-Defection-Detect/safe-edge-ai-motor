from __future__ import annotations

import math
import json
import struct
import sys
from threading import Event, Thread
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "edge"))

from r300_link import (ConditionVoter, EdgePipeline, EventLogger, InferenceResult,
                       ReceiverService, Telemetry)
from r300_link import link_contract as lc
from r300_receiver import (INFERENCE_QUEUE_BLOCKS, InferenceWorker, SerialReceiver,
                           open_serial_port)


def frame(sequence: int, flags: int = 0) -> bytes:
    body = lc.TELEMETRY_STRUCT.pack(
        lc.TELEMETRY_SOF, lc.PROTOCOL_VERSION | flags << 8, sequence, 0, 1)
    body += struct.pack("<hhhh", 1, 2, 3, 4)
    return body + struct.pack("<H", lc.crc16(body))


def block_frame(sequence: int, flags: int = 0, timestamp_us: int = 0) -> bytes:
    body = lc.TELEMETRY_STRUCT.pack(
        lc.TELEMETRY_SOF, lc.PROTOCOL_VERSION | flags << 8,
        sequence, timestamp_us, lc.BLOCK_SAMPLES)
    body += struct.pack("<" + "h" * (lc.BLOCK_SAMPLES * lc.CHANNEL_COUNT),
                        *((1, 2, 3, 4) * lc.BLOCK_SAMPLES))
    return body + struct.pack("<H", lc.crc16(body))


def diagnostic_frame(sequence: int = 0) -> bytes:
    status = (lc.STM_DIAGNOSTIC_FLAGS["KEEPALIVE_VALID"] |
              lc.STM_DIAGNOSTIC_FLAGS["LOCAL_HEALTHY"] |
              lc.STM_DIAGNOSTIC_FLAGS["AVR_DIAG_VALID"] |
              lc.STM_DIAGNOSTIC_FLAGS["AVR_HEARTBEAT_HEALTHY"] |
              lc.STM_DIAGNOSTIC_FLAGS["AVR_ARM_ACTIVE"] |
              lc.STM_DIAGNOSTIC_FLAGS["AVR_INTERNAL_HEALTHY"] |
              lc.STM_DIAGNOSTIC_FLAGS["AVR_RELAY_COMMAND"])
    body = lc.STM_DIAGNOSTIC_STRUCT.pack(
        lc.STM_DIAGNOSTIC_SOF, lc.PROTOCOL_VERSION, sequence, 1000, 9,
        1, 2, 1, 0, 0, 0, 0, 0, 0, 0, 0, 0, 4, 5, 6, 1000, 12, status)
    return body + struct.pack("<H", lc.crc16(body))


class FakePort:
    def __init__(self, chunks: list[bytes]) -> None:
        self.chunks, self.writes = chunks, []

    def read(self, size: int) -> bytes:
        assert size == 4096
        return self.chunks.pop(0) if self.chunks else b""

    def write(self, data: bytes) -> int:
        self.writes.append(data)
        return len(data)


def test_serial_receiver_polls_uart_and_sends_health_before_timeout() -> None:
    port = FakePort([frame(1), frame(3)])
    service = ReceiverService(EdgePipeline(lambda vibration, current: InferenceResult("NORMAL", 1.0)))
    metrics: list[dict[str, float]] = []
    receiver = SerialReceiver(port, service, model_loaded=lambda: False, storage_ok=lambda: True,
                              on_metrics=metrics.append)
    assert receiver.poll("now", 0) == ()
    assert receiver.poll("now", 99) == ()
    assert receiver.poll("now", 100) == ()
    assert len(port.writes) == 4
    assert metrics[-1]["sequence_gaps"] == 1.0
    assert metrics[-1]["crc_errors"] == 0.0
    assert metrics[-1]["packet_loss_percent"] == 100.0 / 3.0
    assert metrics[-1]["inference_p50_ms"] == 0.0
    assert metrics[-1]["inference_p95_ms"] == 0.0
    assert metrics[-1]["model_loaded"] == 0.0 and metrics[-1]["storage_ok"] == 1.0
    receiver.poll("now", 400)
    assert metrics[-1]["telemetry_age_ms"] == 301.0
    assert metrics[-1]["telemetry_stale"] == 1.0
    assert metrics[-1]["jetson_link_healthy"] == 0.0
    health_flags = lc.HEALTH_STRUCT.unpack_from(port.writes[-2])[-1]
    assert health_flags & lc.HEALTH_FLAGS["APP_ALIVE"]
    assert not health_flags & lc.HEALTH_FLAGS["LINK_HEALTHY"]


def test_health_reports_link_only_while_telemetry_is_recent() -> None:
    port = FakePort([frame(1)])
    receiver = SerialReceiver(port, ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0))),
        model_loaded=lambda: False, storage_ok=lambda: True)
    receiver.poll("now", 0)
    assert lc.HEALTH_STRUCT.unpack_from(port.writes[-2])[-1] & lc.HEALTH_FLAGS["LINK_HEALTHY"]
    receiver.poll("later", lc.JETSON_LINK_HEALTHY_MAX_AGE_MS + 1)
    assert not (lc.HEALTH_STRUCT.unpack_from(port.writes[-2])[-1] &
                 lc.HEALTH_FLAGS["LINK_HEALTHY"])


def test_health_clears_link_when_the_bound_application_is_not_alive() -> None:
    port = FakePort([frame(1)])
    application_ok = [True]
    metrics: list[dict[str, float]] = []
    receiver = SerialReceiver(port, ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0))),
        model_loaded=lambda: True, storage_ok=lambda: True,
        application_ok=lambda: application_ok[0], on_metrics=metrics.append)
    receiver.poll("now", 0)
    assert lc.HEALTH_STRUCT.unpack_from(port.writes[-2])[-1] & lc.HEALTH_FLAGS["LINK_HEALTHY"]
    application_ok[0] = False
    receiver.poll("ui-stalled", 100)
    assert not (lc.HEALTH_STRUCT.unpack_from(port.writes[-2])[-1] &
                lc.HEALTH_FLAGS["LINK_HEALTHY"])
    assert metrics[-1]["application_ok"] == 0.0


def test_health_clears_link_when_the_inference_worker_stalls() -> None:
    port = FakePort([frame(1)])
    service = ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0)))
    worker = InferenceWorker(service, lambda windows, timestamp: None)
    worker._processing_started_ns = 1
    receiver = SerialReceiver(port, service, model_loaded=lambda: True,
                              storage_ok=lambda: True, inference_worker=worker)
    receiver.poll("now", 0)
    flags = lc.HEALTH_STRUCT.unpack_from(port.writes[-2])[-1]
    assert not flags & lc.HEALTH_FLAGS["LINK_HEALTHY"]
    assert worker.stalled(lc.INFERENCE_WORKER_STALL_MS * 1_000_000 + 1)


def test_health_clears_link_after_an_inference_worker_infrastructure_failure() -> None:
    port = FakePort([frame(1)])
    service = ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0)))
    worker = InferenceWorker(service, lambda windows, timestamp: None)
    worker.failed = True
    metrics: list[dict[str, float]] = []
    receiver = SerialReceiver(port, service, model_loaded=lambda: True,
                              storage_ok=lambda: True, inference_worker=worker,
                              on_metrics=metrics.append)
    receiver.poll("failed", 0)
    assert not (lc.HEALTH_STRUCT.unpack_from(port.writes[-2])[-1] &
                lc.HEALTH_FLAGS["LINK_HEALTHY"])
    assert metrics[-1]["inference_worker_failed"] == 1.0


def test_observed_inference_stall_is_latched_until_a_health_frame_reports_it() -> None:
    port = FakePort([frame(1)])
    service = ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0)))
    worker = InferenceWorker(service, lambda windows, timestamp: None)
    receiver = SerialReceiver(port, service, model_loaded=lambda: True,
                              storage_ok=lambda: True, inference_worker=worker)
    receiver.poll("now", 0)
    assert lc.HEALTH_STRUCT.unpack_from(port.writes[-2])[-1] & lc.HEALTH_FLAGS["LINK_HEALTHY"]
    worker._processing_started_ns = 1
    receiver.poll("stalled", 50)
    worker._processing_started_ns = None
    receiver.poll("recovered", 100)
    assert not (lc.HEALTH_STRUCT.unpack_from(port.writes[-2])[-1] &
                lc.HEALTH_FLAGS["LINK_HEALTHY"])
    assert not receiver._inference_stall_latched


def test_sample_rate_uses_stm32_timestamps_not_jetson_receive_timing() -> None:
    metrics: list[dict[str, float]] = []
    flags = (lc.TELEMETRY_FLAGS["SENSOR_READY"] |
             lc.TELEMETRY_FLAGS["MOTOR_ACTIVE"] |
             lc.TELEMETRY_FLAGS["AVR_DIAG_VALID"] |
             lc.TELEMETRY_FLAGS["AVR_ARMED"])
    receiver = SerialReceiver(FakePort([block_frame(1, flags, 10_000),
                                        block_frame(2, flags, 10_000 +
                                                    lc.FRAME_PERIOD_MS * 1000)]),
                              ReceiverService(EdgePipeline(
                                  lambda vibration, current: InferenceResult("NORMAL", 1.0))),
                              model_loaded=lambda: False, storage_ok=lambda: True,
                              on_metrics=metrics.append)
    receiver.poll("now", 10_000)
    receiver.poll("now", 11_000)
    assert metrics[-1]["sample_rate_hz"] == lc.SAMPLE_RATE_HZ
    assert metrics[-1]["sample_rate_observations"] == 2.0
    assert metrics[-1]["telemetry_sensor_ready"] == 1.0
    assert metrics[-1]["telemetry_motor_active"] == 1.0
    assert metrics[-1]["telemetry_avr_diag_valid"] == 1.0
    assert metrics[-1]["telemetry_avr_armed"] == 1.0


def test_sample_rate_handles_loss_and_resets_on_stm32_restart() -> None:
    metrics: list[dict[str, float]] = []
    receiver = SerialReceiver(FakePort([block_frame(10, timestamp_us=1_000_000),
                                        block_frame(12, timestamp_us=1_000_000 +
                                                    2 * lc.FRAME_PERIOD_MS * 1000),
                                        block_frame(1, timestamp_us=
                                                    lc.FRAME_PERIOD_MS * 1000)]),
                              ReceiverService(EdgePipeline(
                                  lambda vibration, current: InferenceResult("NORMAL", 1.0))),
                              model_loaded=lambda: False, storage_ok=lambda: True,
                              on_metrics=metrics.append)
    receiver.poll("first", 0)
    receiver.poll("loss", 500)
    assert metrics[-1]["sample_rate_hz"] == lc.SAMPLE_RATE_HZ
    receiver.poll("restart", 1000)
    assert metrics[-1]["sample_rate_hz"] == 0.0
    assert metrics[-1]["sample_rate_observations"] == 1.0


def test_sample_rate_handles_stm32_timestamp_and_sequence_wrap() -> None:
    metrics: list[dict[str, float]] = []
    first_us = 0xffffff00
    receiver = SerialReceiver(FakePort([
        block_frame(0xffffffff, timestamp_us=first_us),
        block_frame(0, timestamp_us=(first_us + lc.FRAME_PERIOD_MS * 1000) & 0xffffffff)]),
        ReceiverService(EdgePipeline(
            lambda vibration, current: InferenceResult("NORMAL", 1.0))),
        model_loaded=lambda: False, storage_ok=lambda: True, on_metrics=metrics.append)
    receiver.poll("before-wrap", 0)
    receiver.poll("after-wrap", 80)
    assert metrics[-1]["sample_rate_hz"] == lc.SAMPLE_RATE_HZ


def test_bounded_queue_and_voter_fit_the_warning_response_target() -> None:
    backlog_ms = INFERENCE_QUEUE_BLOCKS * lc.FRAME_PERIOD_MS
    voting_ms = ConditionVoter.REQUIRED_ABNORMAL * lc.HOP_SAMPLES * 1000 / lc.SAMPLE_RATE_HZ
    assert backlog_ms + voting_ms + lc.INFERENCE_P95_TARGET_MS <= \
        lc.CONDITION_WARNING_TARGET_MS


def test_serial_receiver_publishes_stm_diagnostics_and_marks_them_stale() -> None:
    metrics: list[dict[str, float]] = []
    receiver = SerialReceiver(FakePort([diagnostic_frame()]), ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0))),
        model_loaded=lambda: False, storage_ok=lambda: True, on_metrics=metrics.append)
    receiver.poll("now", 0)
    assert metrics[-1]["stm_diagnostic_valid"] == 1.0
    assert metrics[-1]["stm_dropped_blocks"] == 1.0
    assert metrics[-1]["stm_invalid_health_crc_frames"] == 1.0
    assert metrics[-1]["stm_current_zero_count"] == 1000.0
    assert metrics[-1]["stm_keepalive_valid"] == 1.0
    assert metrics[-1]["stm_local_healthy"] == 1.0
    assert metrics[-1]["stm_avr_live_valid"] == 1.0
    assert metrics[-1]["stm_avr_heartbeat_healthy"] == 1.0
    assert metrics[-1]["stm_avr_arm_active"] == 1.0
    assert metrics[-1]["stm_avr_estop_active"] == 0.0
    assert metrics[-1]["stm_avr_internal_healthy"] == 1.0
    assert metrics[-1]["stm_avr_relay_command"] == 1.0
    assert metrics[-1]["stm_diagnostic_stale"] == 0.0
    receiver.poll("later", lc.STM_DIAGNOSTIC_TIMEOUT_MS + 1)
    assert metrics[-1]["stm_diagnostic_stale"] == 1.0


def test_serial_receiver_loop_stops_after_a_bounded_read() -> None:
    stop = Event()

    class OneReadPort(FakePort):
        def read(self, size: int) -> bytes:
            stop.set()
            return super().read(size)

    port = OneReadPort([])
    receiver = SerialReceiver(port, ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0))),
        model_loaded=lambda: False, storage_ok=lambda: True)
    receiver.run(stop, lambda: "now", lambda: 0)
    assert len(port.writes) == 2


def test_serial_receiver_refreshes_the_latest_bounded_motor_request() -> None:
    port = FakePort([])
    receiver = SerialReceiver(port, ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0))),
        model_loaded=lambda: False, storage_ok=lambda: True)
    receiver.set_motor_command(500)
    receiver.poll("now", 0)
    command = port.writes[1]
    fields = lc.MOTOR_COMMAND_STRUCT.unpack_from(command)
    assert fields[4:] == (500, lc.MOTOR_DIRECTIONS["FORWARD"])
    assert lc.crc16(command[:-2]) == struct.unpack_from("<H", command, len(command) - 2)[0]


def test_serial_receiver_forces_motor_stop_when_evidence_storage_is_unhealthy() -> None:
    port = FakePort([])
    receiver = SerialReceiver(port, ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0))),
        model_loaded=lambda: False, storage_ok=lambda: False)
    receiver.set_motor_command(500)
    receiver.poll("now", 0)
    assert lc.MOTOR_COMMAND_STRUCT.unpack_from(port.writes[1])[4:] == (
        0, lc.MOTOR_DIRECTIONS["STOP"])


def test_serial_receiver_reports_disconnect_without_killing_worker() -> None:
    class BrokenReadPort(FakePort):
        def read(self, size: int) -> bytes:
            raise OSError("disconnected")

    metrics: list[dict[str, float]] = []
    receiver = SerialReceiver(BrokenReadPort([]), ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0))),
        model_loaded=lambda: False, storage_ok=lambda: True, on_metrics=metrics.append)
    receiver.set_motor_command(500)
    assert receiver.poll("now", 0) == ()
    assert metrics[-1]["serial_connected"] == 0.0
    assert metrics[-1]["serial_errors"] == 1.0
    assert lc.MOTOR_COMMAND_STRUCT.unpack_from(receiver._port.writes[1])[4:] == (
        0, lc.MOTOR_DIRECTIONS["STOP"])


def test_serial_receiver_rejects_a_short_health_write() -> None:
    class ShortWritePort(FakePort):
        def write(self, data: bytes) -> int:
            return len(data) - 1

    metrics: list[dict[str, float]] = []
    receiver = SerialReceiver(ShortWritePort([]), ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0))),
        model_loaded=lambda: False, storage_ok=lambda: True, on_metrics=metrics.append)
    receiver.poll("now", 0)
    assert metrics[-1]["serial_connected"] == 0.0 and metrics[-1]["serial_errors"] == 1.0


def test_serial_receiver_contains_health_callback_failures() -> None:
    def broken() -> bool:
        raise RuntimeError("status unavailable")

    metrics: list[dict[str, float]] = []
    receiver = SerialReceiver(FakePort([]), ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0))),
        model_loaded=broken, storage_ok=broken, on_metrics=metrics.append)
    receiver.poll("now", 0)
    assert metrics[-1]["health_state_errors"] == 2.0
    assert metrics[-1]["model_loaded"] == 0.0 and metrics[-1]["storage_ok"] == 0.0


def test_serial_receiver_contains_metrics_callback_failures() -> None:
    receiver = SerialReceiver(FakePort([]), ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0))),
        model_loaded=lambda: False, storage_ok=lambda: True,
        on_metrics=lambda metrics: (_ for _ in ()).throw(RuntimeError("UI unavailable")))
    receiver.poll("now", 0)
    assert receiver.metrics_callback_errors == 1


def test_serial_worker_reports_a_fatal_non_transport_exception_and_stops(tmp_path: Path) -> None:
    class BrokenPort:
        def read(self, size: int) -> bytes:
            raise ValueError("receiver invariant failed")

        def write(self, data: bytes) -> int:
            return len(data)

    path = tmp_path / "fatal.jsonl"
    logger = EventLogger(path)
    metrics: list[dict[str, float]] = []
    receiver = SerialReceiver(BrokenPort(), ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0)), logger),
        model_loaded=lambda: True, storage_ok=lambda: True, on_metrics=metrics.append)
    receiver.run(Event(), lambda: "fatal-time", lambda: 0)
    logger.close()
    assert not receiver.connected and receiver.serial_errors == 1
    assert metrics[-1]["serial_connected"] == 0.0 and metrics[-1]["application_ok"] == 0.0
    assert '"event":"serial_worker_error"' in path.read_text(encoding="utf-8")


def test_serial_open_rejects_an_unbounded_device_configuration() -> None:
    for device, timeout in (("", 0.1), ("tty", 0.0), ("tty", math.nan), ("tty", math.inf)):
        try:
            open_serial_port(device, timeout)
        except ValueError as error:
            assert "positive finite timeout" in str(error)
        else:
            raise AssertionError("invalid serial configuration must fail before opening a device")


def test_serial_receiver_logs_metrics_once_per_second(tmp_path: Path) -> None:
    path = tmp_path / "events.jsonl"
    logger = EventLogger(path)
    receiver = SerialReceiver(FakePort([]), ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0)), logger),
        model_loaded=lambda: False, storage_ok=lambda: True)
    receiver.poll("now", 0)
    receiver.poll("now", 999)
    receiver.poll("now", 1000)
    logger.close()
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len([record for record in records if record["event"] == "metrics"]) == 2
    metrics = [json.loads(line) for line in logger.metrics_path.read_text(
        encoding="utf-8").splitlines()]
    assert len(metrics) == 2 and all(record["event"] == "metrics" for record in metrics)


def test_inference_worker_processes_validated_blocks_on_its_own_thread() -> None:
    published = Event()
    outputs = []
    service = ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0)))
    worker = InferenceWorker(service, lambda windows, timestamp:
                             (outputs.extend(windows), published.set()), capacity=4)
    stop = Event()
    thread = Thread(target=worker.run, args=(stop,))
    thread.start()
    block = tuple((1, 2, 3, 4) for _ in range(lc.BLOCK_SAMPLES))
    worker.submit(tuple(Telemetry(index, index, block) for index in range(4)), "now")
    assert published.wait(1.0)
    stop.set()
    thread.join(1.0)
    assert not thread.is_alive() and len(outputs) == 1


def test_inference_worker_contains_presentation_callback_failures() -> None:
    failed = Event()

    def broken(windows: object, timestamp: str) -> None:
        failed.set()
        raise RuntimeError("presentation unavailable")

    service = ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0)))
    worker = InferenceWorker(service, broken, capacity=4)
    stop = Event()
    thread = Thread(target=worker.run, args=(stop,))
    thread.start()
    block = tuple((1, 2, 3, 4) for _ in range(lc.BLOCK_SAMPLES))
    worker.submit(tuple(Telemetry(index, index, block) for index in range(4)), "now")
    assert failed.wait(1.0)
    assert worker.callback_errors == 1 and worker.failed and thread.is_alive()
    stop.set()
    thread.join(1.0)
    assert not thread.is_alive()


def test_inference_queue_overrun_discards_backlog_and_marks_a_window_reset() -> None:
    service = ReceiverService(EdgePipeline(
        lambda vibration, current: InferenceResult("NORMAL", 1.0)))
    worker = InferenceWorker(service, lambda windows, timestamp: None, capacity=2)
    sample = ((1, 2, 3, 4),)
    worker.submit(tuple(Telemetry(index, index, sample) for index in range(3)), "now")
    assert worker.queue_drops == 2 and worker.queue_depth == 1
    stop = Event()
    stop.set()
    worker.run(stop)
    assert worker.queue_depth == 0
