# ==============================================================================
# edge/r300_dashboard_state.py
# 역할: 워커 스레드(쓰기)와 Qt 메인 스레드(읽기)가 공유하는 화면 상태 저장소. 락 하나로 보호하고, 읽는 쪽에는
#       불변 스냅샷만 건네서 스레드 간 경합을 없앤다.
# 읽기 순서: 12/13 중 두 번째  (presenter -> 이 파일 -> dashboard)
# 다음에 볼 곳: edge/r300_dashboard.py — 이 스냅샷을 위젯에 그리는 Qt 화면
# 관련: edge/r300_presenter.py (쓰는 쪽), edge/r300_dashboard.py (읽는 쪽)
# ==============================================================================

"""Thread-safe-by-ownership state supplied by workers to the Qt main thread."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import Lock
from time import perf_counter_ns
from typing import Deque, Mapping


# 이벤트 표 한 행: 시각, 메시지, 카테고리(SYSTEM 기본, CONDITION / SAFETY / HEALTH / OPERATOR / EXPERIMENT 등).
@dataclass(frozen=True)
class DashboardEvent:
    timestamp: str
    message: str
    category: str = "SYSTEM"


# snapshot() 이 돌려주는 불변 값 묶음. frozen 이라 UI 스레드가 받은 뒤에 워커가 store 를 바꿔도 영향이 없다.
# published_ns 는 해당 결과의 패킷 수신 시각(perf_counter_ns)이며 대시보드가 렌더 지연(e2e) 계산에 쓴다.
@dataclass(frozen=True)
class DashboardState:
    health: Mapping[str, str]
    ai_class: str
    condition: str
    confidence: float
    votes: tuple[str, ...]
    safety_state: str
    safety_detail: str
    metrics: Mapping[str, float]
    waveform: tuple[tuple[float, float, float], ...]
    spectrum: tuple[tuple[float, float, float, float], ...]
    events: tuple[DashboardEvent, ...]
    published_ns: int


# 워커가 update_* 로 쓰고 Qt 스레드가 snapshot() 으로 읽는 공유 상태. 이 클래스는 계산을 하지 않는다(FFT 도 외부에서 받는다)
# -> UI 스레드에 무거운 일이 없다. 이벤트는 최대 200개(deque maxlen)만 유지한다. 다음: r300_dashboard.py.
class DashboardStore:
    """Bounded display state; FFT is supplied by a worker, never calculated here."""

    def __init__(self) -> None:
        self._health: dict[str, str] = {}
        self._ai_class = "UNKNOWN"
        self._condition = "UNKNOWN"
        self._confidence = 0.0
        self._votes: tuple[str, ...] = ()
        self._safety_state = "UNKNOWN"
        self._safety_detail = "AVR diagnostics unavailable"
        self._published_ns = 0
        self._metrics: dict[str, float] = {}
        self._waveform: tuple[tuple[float, float, float], ...] = ()
        self._spectrum: tuple[tuple[float, float, float, float], ...] = ()
        self._events: Deque[DashboardEvent] = deque(maxlen=200)
        self._lock = Lock()

    # 건강 상태 dict 를 통째로 교체(복사본 저장).
    def update_health(self, health: Mapping[str, str]) -> None:
        with self._lock:
            self._health = dict(health)

    # AI 클래스/조건/신뢰도(0~1 검증)/투표(최근 5개)를 갱신한다. published_ns 는 패킷 수신 ns, 없으면 지금 시각.
    def update_ai_condition(self, ai_class: str, condition: str, confidence: float,
                            votes: tuple[str, ...] = (), packet_ready_ns: int = 0) -> None:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")
        with self._lock:
            self._ai_class, self._condition = ai_class, condition
            self._confidence, self._votes = confidence, votes[-5:]
            self._published_ns = packet_ready_ns or perf_counter_ns()

    # AVR 안전 상태 문자열과 상세 설명을 갱신한다(AI 조건과 별도 필드).
    def update_safety(self, state: str, detail: str) -> None:
        with self._lock:
            self._safety_state, self._safety_detail = state, detail

    # 메트릭 dict 를 통째로 교체(복사본 저장).
    def update_metrics(self, metrics: Mapping[str, float]) -> None:
        with self._lock:
            self._metrics = dict(metrics)

    # 화면에 그릴 파형(최근 1초)을 교체한다.
    def update_waveform(self, waveform: tuple[tuple[float, float, float], ...]) -> None:
        with self._lock:
            self._waveform = waveform

    # 화면에 그릴 스펙트럼 [(주파수, X, Y, Z)]를 교체한다. 계산은 워커가 이미 끝냈다.
    def update_fft(self, spectrum: tuple[tuple[float, float, float, float], ...]) -> None:
        with self._lock:
            self._spectrum = spectrum

    # 이벤트 행을 추가한다. 200개를 넘으면 가장 오래된 것부터 밀려난다.
    def append_event(self, timestamp: str, message: str, category: str = "SYSTEM") -> None:
        with self._lock:
            self._events.append(DashboardEvent(timestamp, message, category))

    # 락 안에서 현재 값의 복사본을 만들어 불변 DashboardState 로 돌려준다(dict 는 복사, 튜플은 그대로).
    def snapshot(self) -> DashboardState:
        with self._lock:
            return DashboardState(dict(self._health), self._ai_class, self._condition,
                                  self._confidence, self._votes, self._safety_state,
                                  self._safety_detail, dict(self._metrics), self._waveform,
                                  self._spectrum, tuple(self._events), self._published_ns)
