# [테스트 설명] R300Application 이 수신 워커를 소유/정지하고, 잘못된 run 메타데이터와 UI 지연 설정을 거부하며, 대시보드 하트비트를 감독하는 동작을 지킨다.
# 대상 소스: edge/r300_application.py (읽기 순서 11번)
# 읽는 법: 각 test_ 함수 이름이 곧 보호하는 동작이다. 소스를 먼저 읽고 오면 이해가 쉽다.
from __future__ import annotations

import json
import sys
from pathlib import Path
from threading import Event

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "edge"))

from r300_application import R300Application
from r300_link import InferenceResult
from r300_link import link_contract as lc


def test_application_owns_and_stops_the_receiver_worker(tmp_path: Path) -> None:
    read_called = Event()

    class Port:
        def read(self, size: int) -> bytes:
            read_called.set()
            return b""

        def write(self, data: bytes) -> int:
            return len(data)

    app = R300Application(Port(), lambda vibration, current: InferenceResult("NORMAL", 1.0),
                          tmp_path / "events.jsonl", model_loaded=lambda: False)
    app.start()
    assert read_called.wait(1.0)
    app.handle_arm_request()
    app.set_motor_duty(250)
    app.mark_physical_condition("fixture_unbalance", "photos/R1-change.jpg")
    app.handle_ui_e2e_p95(12.0)
    assert app.store.snapshot().metrics["e2e_p95_ms"] == 12.0
    app.stop()
    assert app._thread is not None and not app._thread.is_alive()
    assert app._inference_thread is not None and not app._inference_thread.is_alive()
    assert any(event.category == "OPERATOR" for event in app.store.snapshot().events)
    log = (tmp_path / "events.jsonl").read_text(encoding="utf-8")
    assert '"event":"application_start"' in log and '"sample_rate_hz":1000' in log
    assert '"run_id":"run-' in log and '"class":"unlabeled"' in log
    assert '"sensor_cfg":"mpu6050_1000hz_acs712_1000hz"' in log
    assert '"event":"application_stop"' in log
    assert '"event":"operator_arm_request"' in log
    assert '"event":"motor_command_requested"' in log and '"duty_permille":250' in log
    assert '"event":"physical_condition_transition"' in log
    assert '"current_condition":"fixture_unbalance"' in log
    assert '"event":"ui_metrics"' in log and '"e2e_p95_ms":12.0' in log
    assert '"event":"dashboard_event"' in log and '"category":"HEALTH"' in log
    metadata = (tmp_path / "events.metadata.json").read_text(encoding="utf-8")
    assert '"pwm_duty": 250' in metadata and '"condition": "fixture_unbalance"' in metadata
    assert '"fixture": "unassigned"' in metadata and '"power_cycle": 0' in metadata
    assert '"mount": "unspecified"' in metadata
    assert '"split": "unassigned"' in metadata
    assert '"label_evidence": "unassigned"' in metadata
    assert '"startup_exclusion_seconds": 10.0' in metadata
    assert '"class_contract"' in metadata and '"UNBALANCE"' in metadata
    assert '"vibration_g_per_count"' in metadata and '"vibration_scale"' in metadata
    metadata_record = json.loads(metadata)
    assert metadata_record["protocol_version"] == lc.PROTOCOL_VERSION
    assert metadata_record["contract_sha256"] == lc.CONTRACT_SHA256
    assert metadata_record["condition_policy"] == {
        "window_size": 5,
        "required_abnormal": 4,
        "confidence_threshold": 0.85,
        "clear_normal": 5,
    }
    start_record = next(json.loads(line) for line in log.splitlines()
                        if '"event":"application_start"' in line)
    assert start_record["contract_sha256"] == lc.CONTRACT_SHA256
    assert start_record["condition_policy"] == metadata_record["condition_policy"]
    assert '"event":"ui_metrics"' in (tmp_path / "events.metrics.jsonl").read_text(
        encoding="utf-8")


def test_application_rejects_invalid_run_metadata_and_ui_latency(tmp_path: Path) -> None:
    class Port:
        def read(self, size: int) -> bytes:
            return b""

        def write(self, data: bytes) -> int:
            return len(data)

    infer = lambda vibration, current: InferenceResult("NORMAL", 1.0)
    with pytest.raises(ValueError):
        R300Application(Port(), infer, tmp_path / "bad.jsonl", model_loaded=lambda: False,
                        experiment_class=" ")
    with pytest.raises(ValueError):
        R300Application(Port(), infer, tmp_path / "bad-cycle.jsonl",
                        model_loaded=lambda: False, power_cycle=-1)
    with pytest.raises(ValueError):
        R300Application(Port(), infer, tmp_path / "bad-class.jsonl",
                        model_loaded=lambda: False, experiment_class="BEARING")
    with pytest.raises(ValueError):
        R300Application(Port(), infer, tmp_path / "bad-split.jsonl",
                        model_loaded=lambda: False, dataset_split="random-window")
    with pytest.raises(ValueError):
        R300Application(Port(), infer, tmp_path / "bad-startup.jsonl",
                        model_loaded=lambda: False, startup_exclusion_seconds=4.9)
    with pytest.raises(ValueError):
        R300Application(Port(), infer, tmp_path / "bad-labels.jsonl",
                        model_loaded=lambda: False,
                        allowed_labels=("NORMAL", "BEARING", "LOAD"))
    assert not (tmp_path / "bad-labels.metadata.json").exists()
    with pytest.raises(ValueError):
        R300Application(Port(), infer, tmp_path / "unjustified-fallback.jsonl",
                        model_loaded=lambda: False,
                        allowed_labels=("NORMAL", "UNBALANCE", "LOOSENESS"))
    app = R300Application(Port(), infer, tmp_path / "events.jsonl", model_loaded=lambda: False)
    with pytest.raises(ValueError):
        app.handle_ui_e2e_p95(float("nan"))
    with pytest.raises(ValueError):
        app.mark_physical_condition(" ")
    app.stop()


def test_bound_dashboard_heartbeat_is_supervised(tmp_path: Path) -> None:
    class Port:
        def read(self, size: int) -> bytes:
            return b""

        def write(self, data: bytes) -> int:
            return len(data)

    class Signal:
        def __init__(self) -> None:
            self.callback = None

        def connect(self, callback) -> None:
            self.callback = callback

    class Dashboard:
        def __init__(self) -> None:
            self.arm_requested = Signal()
            self.e2e_p95_updated = Signal()
            self.ui_heartbeat = Signal()

        def bind_store(self, store) -> None:
            self.store = store

    app = R300Application(Port(), lambda vibration, current: InferenceResult("NORMAL", 1.0),
                          tmp_path / "ui-watchdog.jsonl", model_loaded=lambda: True)
    dashboard = Dashboard()
    app.bind_dashboard(dashboard)
    assert app._application_alive()
    app._last_ui_heartbeat_ms -= lc.UI_HEARTBEAT_TIMEOUT_MS + 1
    assert not app._application_alive()
    assert dashboard.ui_heartbeat.callback is not None
    dashboard.ui_heartbeat.callback()
    assert app._application_alive()
    app.stop()
