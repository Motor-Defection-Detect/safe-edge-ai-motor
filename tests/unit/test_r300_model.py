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
