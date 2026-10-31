# ==============================================================================
# edge/r300_dashboard.py
# 역할: PyQt5 운영자 화면. DashboardStore 의 스냅샷을 100ms 마다 읽어 건강 상태, AI 조건, Safety 상태,
#       메트릭, 파형/FFT, 이벤트 표로 그린다. 계산은 하지 않고 그리기만 한다(무거운 일은 워커가 끝낸 값).
# 읽기 순서: 12/13 중 세 번째  (presenter -> dashboard_state -> 이 파일)
# 다음에 볼 곳: edge/r300_demo.py — 이 화면과 전체 파이프라인을 합성 입력으로 실제로 돌려 보는 실행 파일
# 관련: edge/r300_application.py (bind_dashboard 로 시그널 연결), edge/r300_dashboard_state.py (읽는 대상)
# 핵심 설계: 화면에는 릴레이 권한이 없다. ARM 버튼은 advisory 요청 시그널만 내보내며 릴레이는 AVR 의 물리 ARM 으로만 켜진다.
#            ui_heartbeat 시그널이 300ms 넘게 끊기면 Application 이 LINK_HEALTHY 를 끈다(UI 정지 = 애플리케이션 정지).
# ==============================================================================

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


# 위젯에 그릴 값 묶음. DashboardState 에서 파형/FFT/이벤트를 뺀 부분에, 화면에서 계산한 e2e p95 를 더한 메트릭이 들어간다. update_snapshot 의 입력.
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


# 패널: System Health, AI Condition(advisory), Safety State(ARM 요청 버튼 포함), Metrics, 파형 + FFT 플롯, 이벤트 표.
# 시그널: arm_requested(advisory), e2e_p95_updated(렌더 지연 p95), ui_heartbeat(갱신 때마다) - 뒤의 둘을 R300Application 이 받는다.
# 다음: 실제 갱신 경로는 맨 아래 bind_store -> _refresh_store -> update_state 순서로 읽으면 된다.
class R300Dashboard(QWidget):
    """Small, non-blocking status view updated by the receiver worker."""

    arm_requested = pyqtSignal()
    e2e_p95_updated = pyqtSignal(float)
    ui_heartbeat = pyqtSignal()

    # 위젯을 만들고 objectName 을 붙이는 구간(위)과 레이아웃으로 조립하는 구간(아래, health_layout 부터)으로 나뉜다.
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
            # peak 다운샘플링: 점이 화면 폭보다 많아도 피크를 보존한다(순간적인 충격성 진동이 사라지지 않게).
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

        # 여기부터 레이아웃 조립.
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

    # 스냅샷을 위젯 텍스트/색으로 반영한다. 조건이 NORMAL 이면 화면에는 MONITORING 으로 바꿔 표시한다.
    # 투표 5칸: 비정상은 'A', 정상은 빈칸, 정보 없음(UNKNOWN)은 '-'.
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

    # 상태 문자열 -> 배경색. safety=True 면 Safety 용 색표(ARMED 녹색 / TRIPPED 적색 / DISARMED 회색), 아니면 건강/AI 용. 표에 없는 값(UNKNOWN 등)은 회색.
    @staticmethod
    def _set_badge(label: QLabel, state: str, *, safety: bool = False) -> None:
        colors = ({"ARMED": "#16a34a", "TRIPPED": "#dc2626", "DISARMED": "#6b7280"}
                  if safety else {"OK": "#16a34a", "DEGRADED": "#d97706", "WAIT": "#d97706",
                                  "WARNING": "#f59e0b", "MONITORING": "#6b7280",
                                  "ERROR": "#dc2626", "STALE": "#6b7280"})
        label.setStyleSheet(f"color:white;background:{colors.get(state, '#6b7280')};padding:4px")

    # 이벤트 표에 행 1개 추가. CONDITION 은 주황 '▲ AI', SAFETY 는 trip 이 들어 있으면 빨강 아니면 초록 '● Safety'. 200행을 넘으면 맨 위부터 삭제(store 와 같은 상한).
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

    # store 스냅샷을 그린다(100ms 마다). 렌더 지연 e2e = 지금 - published_ns(패킷 수신 시각)이고, 최근 100개의 nearest-rank p95 를
    # metrics['e2e_p95_ms'] 로 넣는다. 새 결과일 때만 e2e_p95_updated 를 emit 한다. 파형 시간축은 최신 샘플이 0 인 음수 초.
    # 이벤트 표는 내용이 바뀐 경우에만 다시 만든다.
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

    # store 를 주기적으로 읽는 QTimer 를 시작한다. 주기는 100~125ms(8~10Hz)만 허용하며 그 밖이면 ValueError.
    def bind_store(self, store: DashboardStore, interval_ms: int = 100) -> None:
        if not 100 <= interval_ms <= 125:
            raise ValueError("dashboard refresh must remain within the documented 8-10 Hz range")
        self._refresh_timer = QTimer(self)
        self._refresh_timer.timeout.connect(lambda: self._refresh_store(store))
        self._refresh_timer.start(interval_ms)

    # 한 번 그린 뒤 ui_heartbeat 를 emit 한다. Application 이 이 하트비트가 300ms 넘게 끊기면 LINK_HEALTHY 를 끈다.
    def _refresh_store(self, store: DashboardStore) -> None:
        self.update_state(store.snapshot())
        self.ui_heartbeat.emit()


# 대시보드만 단독으로 띄운다(데이터가 없어 모든 상태가 UNKNOWN).
def main() -> int:
    app = QApplication([])
    window = R300Dashboard()
    window.show()
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
