# [테스트 설명] DashboardStore 가 워커 결과를 보존하고 이벤트 기록 수를 제한(bounded)하는 동작을 지킨다.
# 대상 소스: edge/r300_dashboard_state.py (읽기 순서 12번)
# 읽는 법: 각 test_ 함수 이름이 곧 보호하는 동작이다. 소스를 먼저 읽고 오면 이해가 쉽다.
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "edge"))

from r300_dashboard_state import DashboardStore


def test_dashboard_store_preserves_worker_results_and_bounds_events() -> None:
    store = DashboardStore()
    store.update_health({"serial": "OK", "ai": "OK"})
    store.update_ai_condition("BEARING", "WARNING", 0.8, ("NORMAL", "BEARING"))
    store.update_safety("TRIPPED", "heartbeat timeout")
    store.update_metrics({"loss": 2.0})
    store.update_waveform(((1.0, 2.0, 3.0),))
    store.update_fft(((3.125, 9.0),))
    for index in range(201):
        store.append_event(str(index), "event")
    state = store.snapshot()
    assert state.health["serial"] == "OK"
    assert state.ai_class == "BEARING" and state.votes == ("NORMAL", "BEARING")
    assert state.condition == "WARNING" and state.confidence == 0.8
    assert state.safety_state == "TRIPPED" and state.published_ns > 0
    assert state.waveform == ((1.0, 2.0, 3.0),)
    assert state.spectrum == ((3.125, 9.0),)
    assert len(state.events) == 200 and state.events[0].timestamp == "1"
