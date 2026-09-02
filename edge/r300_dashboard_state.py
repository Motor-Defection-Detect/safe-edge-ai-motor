"""Thread-safe-by-ownership state supplied by workers to the Qt main thread."""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from threading import Lock
from time import perf_counter_ns
from typing import Deque, Mapping


@dataclass(frozen=True)
class DashboardEvent:
    timestamp: str
    message: str
    category: str = "SYSTEM"


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

    def update_health(self, health: Mapping[str, str]) -> None:
        with self._lock:
            self._health = dict(health)

    def update_ai_condition(self, ai_class: str, condition: str, confidence: float,
                            votes: tuple[str, ...] = (), packet_ready_ns: int = 0) -> None:
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence must be within [0, 1]")
        with self._lock:
            self._ai_class, self._condition = ai_class, condition
            self._confidence, self._votes = confidence, votes[-5:]
            self._published_ns = packet_ready_ns or perf_counter_ns()

    def update_safety(self, state: str, detail: str) -> None:
        with self._lock:
            self._safety_state, self._safety_detail = state, detail

    def update_metrics(self, metrics: Mapping[str, float]) -> None:
        with self._lock:
            self._metrics = dict(metrics)

    def update_waveform(self, waveform: tuple[tuple[float, float, float], ...]) -> None:
        with self._lock:
            self._waveform = waveform

    def update_fft(self, spectrum: tuple[tuple[float, float, float, float], ...]) -> None:
        with self._lock:
            self._spectrum = spectrum

    def append_event(self, timestamp: str, message: str, category: str = "SYSTEM") -> None:
        with self._lock:
            self._events.append(DashboardEvent(timestamp, message, category))

    def snapshot(self) -> DashboardState:
        with self._lock:
            return DashboardState(dict(self._health), self._ai_class, self._condition,
                                  self._confidence, self._votes, self._safety_state,
                                  self._safety_detail, dict(self._metrics), self._waveform,
                                  self._spectrum, tuple(self._events), self._published_ns)
