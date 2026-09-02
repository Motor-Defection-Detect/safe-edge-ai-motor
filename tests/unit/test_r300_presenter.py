from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "edge"))

from r300_dashboard_state import DashboardStore
from r300_link import InferenceResult, Telemetry, WindowResult
from r300_link import link_contract as lc
from r300_presenter import DashboardPresenter


def test_presenter_projects_worker_results_into_bounded_dashboard_state() -> None:
    store = DashboardStore()
    presenter = DashboardPresenter(store)
    vibration = tuple((float(index), float(index + 1), float(index + 2))
                      for index in range(lc.WINDOW_SAMPLES))
    result = WindowResult(Telemetry(1, 2, ()), vibration, (1.0, 2.0, 3.0),
                          InferenceResult("UNBALANCE", 0.75), "WARNING", 4.5)
    presenter.publish_windows((result,), "2026-09-01T00:00:00Z")
    presenter.publish_metrics({"sequence_gaps": 2.0, "inference_ok": 1.0,
                               "inference_p95_ms": 4.5})
    state = store.snapshot()
    assert len(state.waveform) == lc.WINDOW_SAMPLES
    assert len(state.spectrum) == lc.WINDOW_SAMPLES // 2 + 1
    assert state.condition == "WARNING" and state.confidence == 0.75
    assert state.ai_class == "UNBALANCE" and state.metrics["inference_p95_ms"] == 4.5
    assert state.metrics["current_rms"] == 2.0
    assert state.metrics["sequence_gaps"] == 2.0
    assert state.health == {"serial": "OK", "sensor": "OK", "ai": "OK",
                            "avr": "UNKNOWN", "motor": "UNKNOWN"}
    assert state.events[-1].message == "condition UNKNOWN -> WARNING"
    assert state.events[-1].category == "CONDITION"
    presenter.publish_metrics({"valid_frames": 13.0, "sample_rate_hz": 1500.0,
                               "packet_loss_percent": 0.6, "inference_p95_ms": 31.0,
                               "serial_errors": 1.0, "inference_ok": 1.0})
    degraded = store.snapshot()
    assert tuple(degraded.health[name] for name in ("serial", "sensor", "ai")) == (
        "DEGRADED", "DEGRADED", "DEGRADED")
    presenter.publish_metrics({"inference_p95_ms": 1.0, "e2e_p95_ms": 101.0,
                               "serial_errors": 0.0, "packet_loss_percent": 0.0,
                               "sample_rate_hz": float(lc.SAMPLE_RATE_HZ)})
    assert store.snapshot().health["ai"] == "DEGRADED"
    presenter.publish_metrics({"telemetry_status_valid": 1.0,
                               "telemetry_sensor_ready": 1.0,
                               "telemetry_local_fault": 1.0})
    faulted = store.snapshot()
    assert faulted.health["sensor"] == "ERROR" and faulted.health["motor"] == "ERROR"
    presenter.publish_metrics({"telemetry_stale": 1.0})
    stale = store.snapshot()
    assert stale.health["serial"] == "STALE" and stale.health["sensor"] == "STALE"
    assert stale.safety_state == "UNKNOWN" and "unavailable" in stale.safety_detail


def test_presenter_projects_fresh_avr_diagnostics_without_relay_authority() -> None:
    store = DashboardStore()
    events = []
    presenter = DashboardPresenter(store, lambda timestamp, message, category:
                                   events.append((timestamp, message, category)))
    presenter.publish_metrics({"telemetry_status_valid": 1.0, "telemetry_stale": 0.0,
                               "telemetry_avr_diag_valid": 1.0,
                               "telemetry_avr_armed": 1.0,
                               "stm_diagnostic_valid": 1.0,
                               "stm_diagnostic_stale": 0.0,
                               "stm_avr_live_valid": 1.0,
                               "stm_avr_heartbeat_healthy": 1.0,
                               "stm_avr_arm_active": 1.0,
                               "stm_avr_internal_healthy": 1.0,
                               "stm_avr_relay_command": 1.0}, "now")
    armed = store.snapshot()
    assert armed.health["avr"] == "OK" and armed.safety_state == "ARMED"
    assert "heartbeat healthy" in armed.safety_detail and "E-stop inactive" in armed.safety_detail
    presenter.publish_metrics({"telemetry_avr_armed": 0.0,
                               "telemetry_avr_tripped": 1.0,
                               "telemetry_avr_estop": 1.0,
                               "stm_avr_heartbeat_healthy": 0.0,
                               "stm_avr_arm_active": 0.0,
                               "stm_avr_estop_active": 1.0,
                               "stm_avr_relay_command": 0.0}, "later")
    tripped = store.snapshot()
    assert tripped.health["avr"] == "ERROR" and tripped.safety_state == "TRIPPED"
    assert "E-stop" in tripped.safety_detail and "E-stop active" in tripped.safety_detail
    assert tripped.events[-1].category == "SAFETY"
    assert events[-1][2] == "SAFETY" and "TRIPPED" in events[-1][1]


def test_presenter_reports_model_failure_before_the_first_successful_window() -> None:
    store = DashboardStore()
    DashboardPresenter(store).publish_metrics({"model_loaded": 0.0, "telemetry_stale": 0.0})
    assert store.snapshot().health["ai"] == "ERROR"


def test_presenter_waits_for_a_new_rate_horizon_after_stm32_restart() -> None:
    store = DashboardStore()
    presenter = DashboardPresenter(store)
    presenter.publish_metrics({"telemetry_status_valid": 1.0,
                               "telemetry_sensor_ready": 1.0,
                               "valid_frames": 20.0,
                               "sample_rate_observations": 1.0,
                               "sample_rate_hz": 0.0})
    assert store.snapshot().health["sensor"] == "OK"


def test_presenter_records_a_bound_application_stall_as_ai_health_error() -> None:
    store = DashboardStore()
    presenter = DashboardPresenter(store)
    presenter.publish_metrics({"application_ok": 0.0, "telemetry_stale": 0.0}, "stalled")
    state = store.snapshot()
    assert state.health["ai"] == "ERROR"
    assert any(event.category == "HEALTH" and "ai=ERROR" in event.message
               for event in state.events)


def test_presenter_uses_stm32_supervision_as_an_honest_avr_fallback() -> None:
    store = DashboardStore()
    presenter = DashboardPresenter(store)
    presenter.publish_metrics({"serial_connected": 1.0, "telemetry_stale": 0.0,
                               "telemetry_status_valid": 1.0,
                               "telemetry_sensor_ready": 1.0,
                               "telemetry_local_fault": 0.0,
                               "telemetry_avr_diag_valid": 0.0,
                               "stm_diagnostic_valid": 1.0,
                               "stm_diagnostic_stale": 0.0,
                               "stm_keepalive_valid": 1.0,
                               "stm_local_healthy": 1.0,
                               "stm_acquisition_overrun": 0.0})
    state = store.snapshot()
    assert state.safety_state == "UNKNOWN"
    assert "STM32 supervision healthy" in state.safety_detail


def test_fresh_stm_diagnostic_identifies_sensor_failure_after_telemetry_stops() -> None:
    store = DashboardStore()
    presenter = DashboardPresenter(store)
    presenter.publish_metrics({"serial_connected": 1.0, "telemetry_stale": 1.0,
                               "telemetry_avr_diag_valid": 0.0,
                               "stm_diagnostic_valid": 1.0,
                               "stm_diagnostic_stale": 0.0,
                               "stm_keepalive_valid": 0.0,
                               "stm_local_healthy": 0.0,
                               "stm_sensor_bus_errors": 1.0})
    state = store.snapshot()
    assert state.health["serial"] == "DEGRADED" and state.health["sensor"] == "ERROR"
    assert state.health["ai"] == "STALE" and state.health["motor"] == "ERROR"
    assert state.safety_state == "UNKNOWN"
    assert "STM32 supervision not healthy" in state.safety_detail

    presenter.publish_metrics({"stm_diagnostic_age_ms":
                               float(lc.STM_DIAGNOSTIC_PERIOD_MS +
                                     lc.HEARTBEAT_TOGGLE_MS + 1)})
    stale = store.snapshot()
    assert stale.health["serial"] == "STALE" and stale.health["sensor"] == "STALE"
