#!/usr/bin/env python3
"""Train an explicitly synthetic R300 1D-CNN for end-to-end software demos."""
from __future__ import annotations

import argparse
import csv
import json
import math
from pathlib import Path
import random
import sys
from typing import Iterable

import numpy as np
import torch
from torch import nn
from torch.utils.data import DataLoader, TensorDataset

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "edge"))

from r300_link import link_contract as lc  # noqa: E402
from r300_model import DEFAULT_LABELS, R300Net, save_artifact  # noqa: E402


# 현장 변경 지점: 실제 Pilot QC가 LOAD 분리 실패를 보이면 LOAD를 빼고 사유를 기록한다.
LABELS = DEFAULT_LABELS
# 고정 계약: 설계서의 512 sample / hop 256은 MCU·Jetson 계약과 함께 바꿔야 한다.
WINDOW, HOP = lc.WINDOW_SAMPLES, lc.HOP_SAMPLES
# 현장 변경 지점: 실제 수집은 class당 6회(4/1/1), 90초, PWM 60/75/90을 유지한다.
SESSION_SPLITS = ("train", "train", "train", "train", "validation", "test")
SYNTHETIC_SESSION_SAMPLES = 4096


def _session(label: str, session_index: int, seed: int) -> tuple[np.ndarray, np.ndarray, int]:
    """Generate separable signals only for testing the software path, not accuracy claims."""
    rng = np.random.default_rng(seed)
    pwm = (600, 750, 900)[session_index % 3]
    count = SYNTHETIC_SESSION_SAMPLES
    time = np.arange(count, dtype=np.float32) / lc.SAMPLE_RATE_HZ
    speed_hz = {600: 38.0, 750: 48.0, 900: 58.0}[pwm] * rng.uniform(0.97, 1.03)
    phase = rng.uniform(0.0, 2.0 * math.pi, size=3)
    vibration = np.column_stack((
        180.0 * np.sin(2 * math.pi * speed_hz * time + phase[0]),
        140.0 * np.sin(2 * math.pi * speed_hz * time + phase[1]),
        100.0 * np.sin(2 * math.pi * speed_hz * time + phase[2]),
    ))
    current_base, current_ripple = 300.0 + pwm * 0.15, 24.0
    if label == "UNBALANCE":
        vibration[:, 0] += 520.0 * np.sin(2 * math.pi * speed_hz * time + 0.4)
        vibration[:, 1] += 180.0 * np.sin(2 * math.pi * speed_hz * time + 1.1)
        current_base += 45.0
    elif label == "LOOSENESS":
        vibration += np.column_stack((
            320.0 * np.sin(4 * math.pi * speed_hz * time),
            250.0 * np.sin(6 * math.pi * speed_hz * time + 0.3),
            180.0 * np.sin(4 * math.pi * speed_hz * time + 0.8),
        ))
        current_ripple += 35.0
    elif label == "LOAD":
        vibration *= 1.15
        current_base += 360.0
        current_ripple += 20.0
    vibration += rng.normal(0.0, 24.0, size=vibration.shape)
    current = (current_base + current_ripple * np.sin(2 * math.pi * speed_hz * time + 0.2) +
               rng.normal(0.0, 8.0, size=count))
    return vibration.astype(np.float32), current.astype(np.float32), pwm


def _windows(vibration: np.ndarray, current: np.ndarray) -> Iterable[tuple[np.ndarray, np.ndarray]]:
    for start in range(0, len(vibration) - WINDOW + 1, HOP):
        window = vibration[start:start + WINDOW]
        window = window - window.mean(axis=0, keepdims=True)
        current_window = current[start:start + WINDOW]
        current_features = np.array((current_window.mean(),
                                     np.sqrt(np.mean(current_window ** 2)),
                                     np.max(np.abs(current_window))), dtype=np.float32)
        yield window, current_features


def _macro_metrics(expected: np.ndarray, predicted: np.ndarray,
                   class_count: int) -> tuple[float, list[float], np.ndarray]:
    confusion = np.zeros((class_count, class_count), dtype=np.int64)
    for truth, guess in zip(expected, predicted):
        confusion[int(truth), int(guess)] += 1
    recalls, f1_values = [], []
    for index in range(class_count):
        true_positive = confusion[index, index]
        false_negative = confusion[index, :].sum() - true_positive
        false_positive = confusion[:, index].sum() - true_positive
        recall = true_positive / max(1, true_positive + false_negative)
        precision = true_positive / max(1, true_positive + false_positive)
        recalls.append(float(recall))
        f1_values.append(float(2 * precision * recall / max(1e-12, precision + recall)))
    return float(np.mean(f1_values)), recalls, confusion


def _evaluate(model: R300Net, vibration: torch.Tensor, current: torch.Tensor,
              expected: torch.Tensor) -> tuple[float, list[float], np.ndarray]:
    model.eval()
    with torch.inference_mode():
        predicted = model(vibration, current).argmax(dim=1).cpu().numpy()
    return _macro_metrics(expected.cpu().numpy(), predicted, len(LABELS))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("build/models/model_B_synthetic.pt"))
    # 현장 변경 지점: 실제 데이터 수와 수렴 곡선을 보고 epoch을 조정하고 early stopping을 추가한다.
    parser.add_argument("--epochs", type=int, default=18)
    parser.add_argument("--seed", type=int, default=300)
    parser.add_argument("--onnx", type=Path, help="optional ONNX output; requires the onnx package")
    args = parser.parse_args()
    if args.epochs < 1:
        parser.error("--epochs must be positive")
    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)

    by_split: dict[str, list[tuple[np.ndarray, np.ndarray, int]]] = {
        "train": [], "validation": [], "test": []}
    manifest: list[dict[str, object]] = []
    for label_index, label in enumerate(LABELS):
        for session_index, split in enumerate(SESSION_SPLITS):
            run_id = f"synthetic-{label.lower()}-{session_index + 1:02d}"
            vibration, current, pwm = _session(
                label, session_index, args.seed + label_index * 100 + session_index)
            by_split[split].extend((window, features, label_index)
                                   for window, features in _windows(vibration, current))
            manifest.append({"run_id": run_id, "source_dataset": "R300_SYNTHETIC_DEMO",
                             "class": label, "split": split, "pwm_duty": pwm,
                             "samples": len(vibration), "synthetic": True})

    vibration_train = np.stack([item[0] for item in by_split["train"]])
    vibration_mean = vibration_train.mean(axis=(0, 1))
    vibration_scale = vibration_train.std(axis=(0, 1)).clip(min=1e-6)
    current_train = np.stack([item[1] for item in by_split["train"]])
    current_mean = current_train.mean(axis=0)
    current_scale = current_train.std(axis=0).clip(min=1e-6)

    tensors: dict[str, tuple[torch.Tensor, torch.Tensor, torch.Tensor]] = {}
    for split, rows in by_split.items():
        vibration = ((np.stack([item[0] for item in rows]) - vibration_mean) /
                     vibration_scale).transpose(0, 2, 1)
        current = (np.stack([item[1] for item in rows]) - current_mean) / current_scale
        labels = np.array([item[2] for item in rows], dtype=np.int64)
        tensors[split] = (torch.from_numpy(vibration.astype(np.float32)),
                          torch.from_numpy(current.astype(np.float32)),
                          torch.from_numpy(labels))

    model = R300Net(len(LABELS))
    optimizer = torch.optim.Adam(model.parameters(), lr=1e-3)
    loss_function = nn.CrossEntropyLoss()
    train_loader = DataLoader(TensorDataset(*tensors["train"]), batch_size=64, shuffle=True)
    best_state, best_macro_f1 = None, -1.0
    for epoch in range(1, args.epochs + 1):
        model.train()
        for vibration, current, labels in train_loader:
            optimizer.zero_grad()
            loss = loss_function(model(vibration, current), labels)
            loss.backward()
            optimizer.step()
        validation_f1, _, _ = _evaluate(model, *tensors["validation"])
        # 같은 validation 점수라면 더 오래 학습된 가중치를 보존한다.
        if validation_f1 >= best_macro_f1:
            best_macro_f1 = validation_f1
            best_state = {name: value.detach().clone() for name, value in model.state_dict().items()}
        print(f"[train] epoch={epoch:02d} val_macro_f1={validation_f1:.4f}")
    assert best_state is not None
    model.load_state_dict(best_state)
    test_f1, recalls, confusion = _evaluate(model, *tensors["test"])

    metadata = {
        "model_id": "r300-synthetic-1dcnn-v1",
        "framework": "pytorch-torchscript",
        "source_dataset": "R300_SYNTHETIC_DEMO",
        "synthetic_training": True,
        "diagnostic_use_allowed": False,
        "labels": list(LABELS),
        "sample_rate_hz": lc.SAMPLE_RATE_HZ,
        "window_samples": WINDOW,
        "hop_samples": HOP,
        "preprocessing": ["per-window DC removal", "train-only standardization"],
        "vibration_mean": vibration_mean.tolist(),
        "vibration_scale": vibration_scale.tolist(),
        "current_mean": current_mean.tolist(),
        "current_scale": current_scale.tolist(),
        "split_policy": "six synthetic sessions/class; fixed 4 train / 1 validation / 1 test before windowing",
        "test_macro_f1_synthetic_only": test_f1,
        "test_recall_synthetic_only": dict(zip(LABELS, recalls)),
    }
    metadata_path = save_artifact(model, args.output, metadata)
    output_dir = args.output.parent
    output_dir.mkdir(parents=True, exist_ok=True)
    with (output_dir / "manifest_B_synthetic.csv").open("w", newline="", encoding="utf-8") as file:
        writer = csv.DictWriter(file, fieldnames=list(manifest[0]))
        writer.writeheader()
        writer.writerows(manifest)
    np.savetxt(output_dir / "confusion_B_synthetic.csv", confusion, delimiter=",", fmt="%d",
               header=",".join(LABELS), comments="")
    (output_dir / "metrics_B_synthetic.json").write_text(json.dumps({
        "warning": "SYNTHETIC DATA ONLY; not equipment accuracy evidence",
        "validation_macro_f1": best_macro_f1,
        "test_macro_f1": test_f1,
        "test_recall": dict(zip(LABELS, recalls)),
        "confusion_matrix": confusion.tolist(),
    }, indent=2) + "\n", encoding="utf-8")

    if args.onnx:
        args.onnx.parent.mkdir(parents=True, exist_ok=True)
        torch.onnx.export(model, (torch.zeros(1, 3, WINDOW), torch.zeros(1, 3)), args.onnx,
                          input_names=["vibration", "current"], output_names=["logits"],
                          dynamic_axes={"vibration": {0: "batch"}, "current": {0: "batch"},
                                        "logits": {0: "batch"}}, opset_version=17,
                          dynamo=False)
    print(f"[train] SYNTHETIC ONLY model={args.output} metadata={metadata_path}")
    print(f"[train] synthetic_test_macro_f1={test_f1:.4f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
