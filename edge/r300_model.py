# ==============================================================================
# edge/r300_model.py
# 역할: 진동 3축 + 전류 특징으로 상태를 분류하는 작은 1D-CNN(R300Net)과, 저장된 TorchScript 모델을
#       안전하게 불러 EdgePipeline 의 infer 콜백으로 쓰는 어댑터(TorchInference).
#       모델이 내놓는 것은 라벨 + 신뢰도뿐이며 릴레이 권한이 없다(조언용).
# 읽기 순서: 8/13  (앞: 7 runtime.py 의 EdgePipeline 이 이 콜백을 부른다)
# 다음에 볼 곳: edge/r300_analysis.py — 같은 창으로 화면용 FFT 스펙트럼을 만드는 작은 파일
# 관련: edge/r300_link/runtime.py (EdgePipeline: 전처리 후 이 객체를 호출), edge/r300_demo.py (--model 옵션으로 연결),
#       docs/interface/r300-software-trace.md (JS-FN-008)
# 핵심 설계: 모델 파일의 SHA-256 이 같은 이름의 .json(sidecar)에 적힌 값과 다르면 로드를 거부한다.
# ==============================================================================

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


# sidecar JSON 스키마 버전. 다르면 TorchInference 가 로드를 거부한다.
MODEL_SCHEMA_VERSION = 1
# 기본 4클래스(정상 + 고장 3종). 실제 출력 인덱스 -> 라벨 매핑은 sidecar JSON 의 labels 순서를 따른다.
DEFAULT_LABELS = ("LOAD", "LOOSENESS", "NORMAL", "UNBALANCE")


# 진동 [3, 512] 는 Conv1d 2층(stride 2) + AdaptiveAvgPool(8)로 32x8=256 특징으로 줄이고,
# 전류 특징 3개(평균/RMS/피크)를 이어 붙여(256+3) 선형 분류기에 넣는 작은 모델. 출력은 logits(softmax 는 TorchInference 에서).
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

    # vibration: [배치, 3, 512], current: [배치, 3] -> logits [배치, 클래스 수].
    def forward(self, vibration: torch.Tensor, current: torch.Tensor) -> torch.Tensor:
        encoded = self.vibration(vibration).flatten(1)
        return self.classifier(torch.cat((encoded, current), dim=1))


# 파일을 1 MiB 씩 읽어 SHA-256 16진 문자열을 만든다. 모델 파일 변조/불일치 검사에 쓴다.
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


# TorchScript(trace)로 모델을 저장하고 같은 이름의 .json sidecar 를 쓴다. sidecar 에 artifact_sha256 을 넣어
# 모델 파일과 메타데이터(라벨, 정규화 값 등)를 해시로 묶는다. trace 입력 형태는 (1,3,512), (1,3). metadata 에 'labels' 필수.
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


# sidecar 값이 '유한한 숫자 3개' 리스트인지 검증해 float 튜플로 돌려준다. positive=True(scale 용)면 0 초과도 요구. 틀리면 ValueError.
def _finite_triplet(metadata: dict[str, Any], name: str, *, positive: bool) -> tuple[float, float, float]:
    values = metadata.get(name)
    if (not isinstance(values, list) or len(values) != 3 or
            any(isinstance(value, bool) or not isinstance(value, (int, float)) or
                not math.isfinite(value) or (positive and value <= 0.0) for value in values)):
        raise ValueError(f"model metadata {name} must contain three finite values")
    return tuple(float(value) for value in values)


# EdgePipeline 이 부르는 infer 콜백 구현. 로드 시 fail-closed 로 검증한다:
# 스키마/라벨 계약, 모델 파일 SHA-256 == sidecar 의 artifact_sha256(다르면 ValueError), mean/scale 값이 유한한지.
# synthetic_training 은 source_dataset 이 R300_SYNTHETIC_DEMO 인 모델을 표시한다(합성 데모는 진단 근거가 아니다).
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

    # 입력: vibration [512][3](EdgePipeline._preprocess 에서 이미 정규화됨), current 3개 특징(여기서 current_mean/scale 로 정규화).
    # 주의: 진동 정규화는 파이프라인에서, 전류 정규화는 여기서 한다. 형태/유한성 검사 후 softmax 최대 확률을 confidence,
    # 해당 라벨을 label 로 돌려준다. 이상한 입력은 ValueError(상위에서 추론 실패로 처리).
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
