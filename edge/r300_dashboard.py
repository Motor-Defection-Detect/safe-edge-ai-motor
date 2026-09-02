"""Qt operator dashboard for the software-only R300 runtime.

The dashboard presents health and evidence; its ARM request signal is advisory.
The independent AVR safety state machine remains the only relay authority.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from time import perf_counter_ns
from typing import Mapping

from PyQt5.QtCore import Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QStandardItem, QStandardItemModel
from PyQt5.QtWidgets import (QApplication, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout,
                             QLabel, QProgressBar, QPushButton, QTableView, QVBoxLayout, QWidget)
import pyqtgraph as pg

from r300_dashboard_state import DashboardState, DashboardStore
from r300_link import link_contract as lc


@dataclass(frozen=True)
class DashboardSnapshot:
    health: Mapping[str, str]
    condition: str
    confidence: float
    metrics: Mapping[str, float]
    ai_class: str = "UNKNOWN"
    votes: tuple[str, ...] = ()
    safety_state: str = "UNKNOWN"
    safety_detail: str = "AVR diagnostics unavailable"


class R300Dashboard(QWidget):
    """Small, non-blocking status view updated by the receiver worker."""

    arm_requested = pyqtSignal()
    e2e_p95_updated = pyqtSignal(float)
    ui_heartbeat = pyqtSignal()

    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("R300 Condition Monitor")
        self._health = QLabel("UNKNOWN")
        self._health.setObjectName("health")
        self._health_labels = {name: QLabel("UNKNOWN") for name in ("serial", "sensor", "ai", "avr", "motor")}
        for name, label in self._health_labels.items():
            label.setObjectName(f"health_{name}")
        self._ai_class = QLabel("UNKNOWN")
        self._ai_class.setObjectName("ai_class")
        self._condition = QLabel("UNKNOWN")
        self._condition.setObjectName("condition")
        self._confidence = QProgressBar()
        self._confidence.setObjectName("confidence")
        self._confidence.setRange(0, 1000)
        self._confidence.setFormat("%p%")
        self._votes = tuple(QLabel("-") for _ in range(5))
        for index, vote in enumerate(self._votes):
            vote.setObjectName(f"vote_{index}")
            vote.setAlignment(Qt.AlignCenter)
        self._safety = QLabel("UNKNOWN")
        self._safety.setObjectName("safety_state")
        self._safety_detail = QLabel("AVR diagnostics unavailable")
        self._safety_detail.setObjectName("safety_detail")
        self._metrics = QLabel("No telemetry")
        self._metrics.setObjectName("metrics")
        self._metrics.setWordWrap(True)
        self._metric_labels = {name: QLabel("N/A") for name in
                               ("sample_rate_hz", "packet_loss_percent", "inference_p95_ms", "e2e_p95_ms")}
        for name, label in self._metric_labels.items():
            label.setObjectName(f"metric_{name}")
        self._waveform = pg.PlotWidget(title="Vibration waveform")
        self._waveform.setObjectName("waveform")
        self._waveform.setLabel("left", "Acceleration", units="g")
        self._waveform.setLabel("bottom", "Time", units="s")
        self._waveform.addLegend()
        self._wave_curves = tuple(self._waveform.plot(name=name) for name in ("X", "Y", "Z"))
        for curve in self._wave_curves:
            curve.setDownsampling(auto=True, method="peak")
            curve.setClipToView(True)
        self._fft = pg.PlotWidget(title="FFT magnitude")
        self._fft.setObjectName("fft")
        self._fft.setLabel("left", "Magnitude", units="g")
        self._fft.setLabel("bottom", "Frequency", units="Hz")
        self._fft.addLegend()
        self._fft_curves = tuple(self._fft.plot(name=name) for name in ("X", "Y", "Z"))
        self._events = QTableView()
        self._events.setObjectName("events")
        self._event_model = QStandardItemModel(0, 3, self)
        self._event_model.setHorizontalHeaderLabels(("Timestamp", "Category", "Event"))
        self._events.setModel(self._event_model)
        self._events.horizontalHeader().setStretchLastSection(True)
        self._rendered_events: tuple[object, ...] = ()
        self._e2e_latencies: deque[float] = deque(maxlen=100)
        self._last_published_ns = 0
        arm = QPushButton("Request ARM (advisory)")
        arm.setObjectName("arm_request")
        arm.clicked.connect(self.arm_requested)

        health_layout = QGridLayout()
        for index, name in enumerate(("serial", "sensor", "ai", "avr", "motor")):
            health_layout.addWidget(QLabel(name.title()), 0, index)
            health_layout.addWidget(self._health_labels[name], 1, index)
        health_layout.addWidget(self._health, 2, 0, 1, 5)
        health_panel = QGroupBox("System Health")
        health_panel.setLayout(health_layout)

        ai_layout = QFormLayout()
        ai_layout.addRow("Class", self._ai_class)
        ai_layout.addRow("State", self._condition)
        ai_layout.addRow("Confidence", self._confidence)
        vote_row = QHBoxLayout()
        for vote in self._votes:
            vote_row.addWidget(vote)
        ai_layout.addRow("5-window vote", vote_row)
        ai_panel = QGroupBox("AI Condition (advisory)")
        ai_panel.setLayout(ai_layout)

        safety_layout = QFormLayout()
        safety_layout.addRow("AVR state", self._safety)
        safety_layout.addRow("Evidence", self._safety_detail)
        safety_layout.addRow(arm)
        safety_panel = QGroupBox("Safety State")
        safety_panel.setLayout(safety_layout)

        metrics_layout = QGridLayout()
        metric_titles = ("Sample rate", "Packet loss", "Inference p95", "E2E p95")
        for index, (title, label) in enumerate(zip(metric_titles, self._metric_labels.values())):
            metrics_layout.addWidget(QLabel(title), 0, index)
            metrics_layout.addWidget(label, 1, index)
        metrics_layout.addWidget(self._metrics, 2, 0, 1, 4)
        metrics_panel = QGroupBox("Metrics")
        metrics_panel.setLayout(metrics_layout)

        plots = QVBoxLayout()
        plots.addWidget(self._waveform)
        plots.addWidget(self._fft)
        status = QVBoxLayout()
        status.addWidget(ai_panel)
        status.addWidget(safety_panel)
        center = QHBoxLayout()
        center.addLayout(plots, 2)
        center.addLayout(status, 1)
        layout = QVBoxLayout(self)
        layout.addWidget(health_panel)
        layout.addLayout(center)
        layout.addWidget(metrics_panel)
        layout.addWidget(self._events)
        self._refresh_timer: QTimer | None = None

    def update_snapshot(self, snapshot: DashboardSnapshot) -> None:
        self._health.setText(", ".join(f"{name}: {state}" for name, state in snapshot.health.items()))
        for name, label in self._health_labels.items():
            state = snapshot.health.get(name, "UNKNOWN")
            label.setText(state)
            self._set_badge(label, state)
        self._ai_class.setText(snapshot.ai_class)
        condition = "MONITORING" if snapshot.condition == "NORMAL" else snapshot.condition
        self._condition.setText(f"{condition} ({snapshot.confidence:.1%})")
        self._set_badge(self._condition, condition, safety=False)
        self._confidence.setValue(round(snapshot.confidence * 1000))
        votes = ("UNKNOWN",) * (5 - len(snapshot.votes)) + snapshot.votes[-5:]
        for label, vote in zip(self._votes, votes):
            abnormal = vote not in ("NORMAL", "UNKNOWN")
            label.setText("A" if abnormal else ("" if vote == "NORMAL" else "-"))
            color = "#f59e0b" if abnormal else "#9ca3af"
            label.setStyleSheet(f"background:{color};padding:4px")
        self._safety.setText(snapshot.safety_state)
        self._set_badge(self._safety, snapshot.safety_state, safety=True)
        self._safety_detail.setText(snapshot.safety_detail)
        self._metrics.setText(", ".join(f"{name}={value:.3g}" for name, value in snapshot.metrics.items()))
        units = {"sample_rate_hz": "Hz", "packet_loss_percent": "%",
                 "inference_p95_ms": "ms", "e2e_p95_ms": "ms"}
        for name, label in self._metric_labels.items():
            value = snapshot.metrics.get(name)
            label.setText("N/A" if value is None else f"{value:.3g} {units[name]}")

    @staticmethod
    def _set_badge(label: QLabel, state: str, *, safety: bool = False) -> None:
        colors = ({"ARMED": "#16a34a", "TRIPPED": "#dc2626", "DISARMED": "#6b7280"}
                  if safety else {"OK": "#16a34a", "DEGRADED": "#d97706", "WAIT": "#d97706",
                                  "WARNING": "#f59e0b", "MONITORING": "#6b7280",
                                  "ERROR": "#dc2626", "STALE": "#6b7280"})
        label.setStyleSheet(f"color:white;background:{colors.get(state, '#6b7280')};padding:4px")

    def append_event(self, timestamp: str, message: str, category: str = "SYSTEM") -> None:
        timestamp_item = QStandardItem(timestamp)
        category_item = QStandardItem(
            {"CONDITION": "▲ AI", "SAFETY": "● Safety"}.get(category, category))
        message_item = QStandardItem(message)
        if category == "CONDITION":
            category_item.setForeground(QColor("#d97706"))
            message_item.setForeground(QColor("#d97706"))
        elif category == "SAFETY":
            color = QColor("#dc2626" if "trip" in message.lower() else "#16a34a")
            category_item.setForeground(color)
            message_item.setForeground(color)
        elif "trip" in message.lower():
            message_item.setForeground(QColor("#dc2626"))
        self._event_model.appendRow((timestamp_item, category_item, message_item))
        if self._event_model.rowCount() > 200:
            self._event_model.removeRow(0)

    def update_state(self, state: DashboardState) -> None:
        """Render worker-provided values only; no acquisition or FFT runs on the UI thread."""
        metrics = dict(state.metrics)
        new_delivery = False
        if state.published_ns and state.published_ns != self._last_published_ns:
            self._e2e_latencies.append((perf_counter_ns() - state.published_ns) / 1_000_000.0)
            self._last_published_ns = state.published_ns
            new_delivery = True
        if self._e2e_latencies:
            ordered = sorted(self._e2e_latencies)
            metrics["e2e_p95_ms"] = ordered[(95 * len(ordered) + 99) // 100 - 1]
            if new_delivery:
                self.e2e_p95_updated.emit(metrics["e2e_p95_ms"])
        self.update_snapshot(DashboardSnapshot(state.health, state.condition, state.confidence, metrics,
                                               state.ai_class, state.votes, state.safety_state,
                                               state.safety_detail))
        waveform_x = tuple((index - len(state.waveform) + 1) / lc.SAMPLE_RATE_HZ
                           for index in range(len(state.waveform)))
        for index, curve in enumerate(self._wave_curves):
            curve.setData(waveform_x, [row[index] for row in state.waveform])
        for index, curve in enumerate(self._fft_curves):
            curve.setData([row[0] for row in state.spectrum],
                          [row[index + 1] for row in state.spectrum])
        if state.events != self._rendered_events:
            self._event_model.setRowCount(0)
            for event in state.events:
                self.append_event(event.timestamp, event.message, event.category)
            self._rendered_events = state.events

    def bind_store(self, store: DashboardStore, interval_ms: int = 100) -> None:
        if not 100 <= interval_ms <= 125:
            raise ValueError("dashboard refresh must remain within the documented 8-10 Hz range")
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(lambda: self._refresh_store(store))
        self._refresh_timer.start(interval_ms)

    def _refresh_store(self, store: DashboardStore) -> None:
        self.update_state(store.snapshot())
        self.ui_heartbeat.emit()


def main() -> int:
    app = QApplication([])
    window = R300Dashboard()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
