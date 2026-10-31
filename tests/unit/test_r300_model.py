# [테스트 설명] TorchScript 모델이 해시 사이드카에 묶여 있고 런타임 콜백과 같은 결과를 내는지 지킨다(torch 가 없으면 건너뜀).
# 대상 소스: edge/r300_model.py (읽기 순서 8번)
# 읽는 법: 각 test_ 함수 이름이 곧 보호하는 동작이다. 소스를 먼저 읽고 오면 이해가 쉽다.
from __future__ import annotations

import sys
from pathlib import Path

import pytest

torch = pytest.importorskip("torch")

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "edge"))

from r300_model import DEFAULT_LABELS, R300Net, TorchInference, save_artifact


def test_torchscript_artifact_is_hash_bound_and_matches_runtime_callback(tmp_path: Path) -> None:
    artifact = tmp_path / "model.pt"
    save_artifact(R300Net(), artifact, {
        "labels": list(DEFAULT_LABELS),
        "source_dataset": "R300_SYNTHETIC_DEMO",
        "vibration_mean": [0.0, 0.0, 0.0],
        "vibration_scale": [1.0, 1.0, 1.0],
        "current_mean": [0.0, 0.0, 0.0],
        "current_scale": [1.0, 1.0, 1.0],
    })
    infer = TorchInference(artifact)
    result = infer([(0.0, 0.0, 0.0)] * 512, (0.0, 0.0, 0.0))
    assert result.label in DEFAULT_LABELS and 0.0 <= result.confidence <= 1.0
    artifact.write_bytes(artifact.read_bytes() + b"tampered")
    with pytest.raises(ValueError, match="hash"):
        TorchInference(artifact)
