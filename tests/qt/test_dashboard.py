# [테스트 설명] Qt 대시보드가 워커가 준 스냅샷/파형/FFT를 그대로 그리고(직접 계산하지 않음) 무장 요청을 '권고'로만 내보내는지 지킨다.
# 대상 소스: edge/r300_dashboard.py (읽기 순서 12번)
# 읽는 법: 각 test_ 함수 이름이 곧 보호하는 동작이다. 소스를 먼저 읽고 오면 이해가 쉽다.
from __future__ import annotations

import sys
from pathlib import Path

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import QLabel, QProgressBar, QPushButton, QTableView
import pyqtgraph as pg

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "edge"))

from r300_dashboard import DashboardSnapshot, R300Dashboard
from r300_dashboard_state import DashboardStore


def test_dashboard_renders_worker_snapshot_and_emits_advisory_arm_request(qtbot) -> None:
    dashboard = R300Dashboard()
    qtbot.addWidget(dashboard)
    dashboard.update_snapshot(DashboardSnapshot(
        {"serial": "OK", "sensor": "OK", "ai": "DEGRADED", "avr": "UNKNOWN", "motor": "UNKNOWN"},
        "WARNING", 0.8, {"packet_loss_percent": 2.0, "inference_p95_ms": 12.0},
        "BEARING", ("NORMAL", "BEARING", "BEARING"), "TRIPPED", "E-stop active"))
    assert dashboard.findChild(QLabel, "health_serial").text() == "OK"
    assert "#16a34a" in dashboard.findChild(QLabel, "health_serial").styleSheet()
    assert "#d97706" in dashboard.findChild(QLabel, "health_ai").styleSheet()
    assert "#6b7280" in dashboard.findChild(QLabel, "health_avr").styleSheet()
    assert dashboard.findChild(QLabel, "ai_class").text() == "BEARING"
    assert dashboard.findChild(QLabel, "condition").text() == "WARNING (80.0%)"
    assert dashboard.findChild(QProgressBar, "confidence").value() == 800
    assert dashboard.findChild(QLabel, "safety_state").text() == "TRIPPED"
    assert "#dc2626" in dashboard.findChild(QLabel, "safety_state").styleSheet()
    assert dashboard.findChild(QLabel, "metric_packet_loss_percent").text() == "2 %"
    assert dashboard.findChild(QLabel, "vote_4").text() == "A"
    assert dashboard.findChild(QLabel, "vote_2").text() == ""
    assert "#9ca3af" in dashboard.findChild(QLabel, "vote_2").styleSheet()
    with qtbot.waitSignal(dashboard.arm_requested):
        qtbot.mouseClick(dashboard.findChild(QPushButton, "arm_request"), Qt.LeftButton)


def test_dashboard_renders_worker_waveform_and_fft_without_computing_them(qtbot) -> None:
    dashboard = R300Dashboard()
    qtbot.addWidget(dashboard)
    store = DashboardStore()
    store.update_waveform(((1.0, 2.0, 3.0), (4.0, 5.0, 6.0)))
    store.update_fft(((3.125, 9.0, 8.0, 7.0), (6.25, 4.0, 3.0, 2.0)))
    store.update_ai_condition("NORMAL", "NORMAL", 0.9, ("NORMAL",) * 5)
    store.append_event("2026-09-01T00:00:00Z", "condition UNKNOWN -> NORMAL", "CONDITION")
    with qtbot.waitSignal(dashboard.e2e_p95_updated) as delivered:
        dashboard.update_state(store.snapshot())
    assert len(dashboard.findChild(pg.PlotWidget, "waveform").listDataItems()) == 3
    assert len(dashboard.findChild(pg.PlotWidget, "fft").listDataItems()) == 3
    waveform_x, _ = dashboard.findChild(pg.PlotWidget, "waveform").listDataItems()[0].getData()
    assert tuple(waveform_x) == (-1.0 / 1000.0, 0.0)
    assert dashboard.findChild(QLabel, "condition").text() == "MONITORING (90.0%)"
    assert dashboard.findChild(QLabel, "metric_e2e_p95_ms").text() != "N/A"
    assert delivered.args[0] >= 0.0
    event_model = dashboard.findChild(QTableView, "events").model()
    assert event_model.rowCount() == 1 and event_model.columnCount() == 3
    assert event_model.index(0, 1).data() == "▲ AI"
    dashboard.bind_store(store)
    assert dashboard._refresh_timer is not None and dashboard._refresh_timer.interval() == 100
    with qtbot.waitSignal(dashboard.ui_heartbeat):
        dashboard._refresh_store(store)
