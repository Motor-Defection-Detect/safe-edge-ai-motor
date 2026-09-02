"""Small 1D-CNN and a validated TorchScript inference adapter for R300."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
from typing import Any, Sequence

import torch
from torch import nn

from r300_link import InferenceResult
from r300_link import link_contract as lc


MODEL_SCHEMA_VERSION = 1
DEFAULT_LABELS = ("LOAD", "LOOSENESS", "NORMAL", "UNBALANCE")


class R300Net(nn.Module):
    """Compact vibration CNN fused with ACS712 mean/RMS/peak features."""

    def __init__(self, class_count: int = len(DEFAULT_LABELS)) -> None:
        super().__init__()
        self.vibration = nn.Sequential(
            nn.Conv1d(3, 16, kernel_size=9, stride=2, padding=4),
            nn.ReLU(),
            nn.Conv1d(16, 32, kernel_size=7, stride=2, padding=3),
            nn.ReLU(),
            nn.AdaptiveAvgPool1d(8),
        )
        self.classifier = nn.Sequential(
            nn.Linear(32 * 8 + 3, 64),
            nn.ReLU(),
            nn.Linear(64, class_count),
        )

    def forward(self, vibration: torch.Tensor, current: torch.Tensor) -> torch.Tensor:
        encoded = self.vibration(vibration).flatten(1)
        return self.classifier(torch.cat((encoded, current), dim=1))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def save_artifact(model: R300Net, path: Path, metadata: dict[str, Any]) -> Path:
    """Save an immutable TorchScript model and its hash-bound JSON sidecar."""
    path.parent.mkdir(parents=True, exist_ok=True)
    model = model.cpu().eval()
    scripted = torch.jit.trace(
        model,
        (torch.zeros(1, 3, lc.WINDOW_SAMPLES), torch.zeros(1, 3)),
    )
    scripted.save(str(path))
    sidecar = dict(metadata)
    sidecar.update({
        "schema_version": MODEL_SCHEMA_VERSION,
        "artifact": path.name,
        "artifact_sha256": _sha256(path),
        "input_shapes": {"vibration": [1, 3, lc.WINDOW_SAMPLES], "current": [1, 3]},
        "output_shape": [1, len(sidecar["labels"])],
    })
    metadata_path = path.with_suffix(".json")
    metadata_path.write_text(json.dumps(sidecar, indent=2) + "\n", encoding="utf-8")
    return metadata_path


def _finite_triplet(metadata: dict[str, Any], name: str, *, positive: bool) -> tuple[float, float, float]:
    values = metadata.get(name)
    if (not isinstance(values, list) or len(values) != 3 or
            any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or (positive and value <= 0.0) for value in values)):
        raise ValueError(f"model metadata {name} must contain three finite values")
    return tuple(float(value) for value in values)


class TorchInference:
    """Callable matching ``EdgePipeline`` while enforcing model metadata parity."""

    def __init__(self, path: Path) -> None:
        metadata_path = path.with_suffix(".json")
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ValueError(f"cannot read model metadata: {error}") from error
        labels = metadata.get("labels")
        if (metadata.get("schema_version") != MODEL_SCHEMA_VERSION or
                not isinstance(labels, list) or not 3 <= len(labels) <= 4 or
                len(set(labels)) != len(labels) or "NORMAL" not in labels or
                not set(labels) <= set(DEFAULT_LABELS)):
            raise ValueError("model metadata has an unsupported schema or label contract")
        if metadata.get("artifact_sha256") != _sha256(path):
            raise ValueError("model artifact hash does not match its metadata")
        self.labels = tuple(labels)
        self.vibration_mean = _finite_triplet(metadata, "vibration_mean", positive=False)
        self.vibration_scale = _finite_triplet(metadata, "vibration_scale", positive=True)
        self.current_mean = _finite_triplet(metadata, "current_mean", positive=False)
        self.current_scale = _finite_triplet(metadata, "current_scale", positive=True)
        self.synthetic_training = metadata.get("source_dataset") == "R300_SYNTHETIC_DEMO"
        self._model = torch.jit.load(str(path), map_location="cpu").eval()

    def __call__(self, vibration: Sequence[Sequence[float]],
                 current: Sequence[float]) -> InferenceResult:
        if len(vibration) != lc.WINDOW_SAMPLES or any(len(row) != 3 for row in vibration):
            raise ValueError(f"vibration must have shape [{lc.WINDOW_SAMPLES}, 3]")
        if len(current) != 3:
            raise ValueError("current must have shape [3]")
        vibration_tensor = torch.tensor(vibration, dtype=torch.float32).transpose(0, 1).unsqueeze(0)
        current_tensor = torch.tensor([
            (float(value) - mean) / scale
            for value, mean, scale in zip(current, self.current_mean, self.current_scale)
        ], dtype=torch.float32).unsqueeze(0)
        if not torch.isfinite(vibration_tensor).all() or not torch.isfinite(current_tensor).all():
            raise ValueError("model input contains a non-finite value")
        with torch.inference_mode():
            probabilities = torch.softmax(self._model(vibration_tensor, current_tensor), dim=1)[0]
        confidence, index = torch.max(probabilities, dim=0)
        return InferenceResult(self.labels[int(index)], float(confidence))
