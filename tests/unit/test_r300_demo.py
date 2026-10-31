# [테스트 설명] 데모용 SyntheticSerial 이 내보내는 바이트가 실제 TelemetryParser 로 해석되는 유효한 텔레메트리 프레임인지 지킨다.
# 대상 소스: edge/r300_demo.py (읽기 순서 13번)
# 읽는 법: 각 test_ 함수 이름이 곧 보호하는 동작이다. 소스를 먼저 읽고 오면 이해가 쉽다.
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "edge"))

from r300_demo import SyntheticSerial
from r300_link import TelemetryParser
from r300_link import link_contract as lc


def test_synthetic_serial_emits_parseable_source_timed_blocks() -> None:
    port = SyntheticSerial(interval_seconds=0.0)
    parser = TelemetryParser()
    first = tuple(parser.feed(port.read(4096)))[0]
    second = tuple(parser.feed(port.read(4096)))[0]
    assert (first.sequence, first.timestamp_us, len(first.samples)) == (0, 0, lc.BLOCK_SAMPLES)
    assert (second.sequence, second.timestamp_us) == (1, lc.FRAME_PERIOD_MS * 1000)
    assert first.flags == lc.TELEMETRY_FLAGS["SENSOR_READY"]
    assert first.samples != second.samples
    assert port.write(b"health") == 6 and port.writes == [b"health"]
