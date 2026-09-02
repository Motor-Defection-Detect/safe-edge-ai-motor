"""Run the real host pipeline and Qt dashboard with synthetic input."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import math
from pathlib import Path
import struct
from time import sleep

from r300_application import R300Application
from r300_link import InferenceResult
from r300_link import link_contract as lc


class SyntheticSerial:
    """Finite-timeout serial stand-in that emits contract-valid 1 kHz blocks."""

    def __init__(self, interval_seconds: float = lc.FRAME_PERIOD_MS / 1000.0) -> None:
        self.interval_seconds = interval_seconds
        self.sequence = 0
        self.writes: list[bytes] = []
        self._pending = b""

    def read(self, size: int) -> bytes:
        if size <= 0:
            return b""
        if not self._pending:
            if self.interval_seconds:
                sleep(self.interval_seconds)
            self._pending = self._frame()
        chunk, self._pending = self._pending[:size], self._pending[size:]
        return chunk

    def write(self, data: bytes) -> int:
        self.writes.append(bytes(data))
        return len(data)

    def _frame(self) -> bytes:
        sequence = self.sequence
        timestamp_us = (sequence * lc.BLOCK_SAMPLES * 1_000_000 //
                        lc.SAMPLE_RATE_HZ) & 0xFFFFFFFF
        samples: list[int] = []
        for offset in range(lc.BLOCK_SAMPLES):
            index = sequence * lc.BLOCK_SAMPLES + offset
            phase = 2.0 * math.pi * index / lc.SAMPLE_RATE_HZ
            unbalance = index >= 2048
            samples.extend((round(180 * math.sin(48 * phase) +
                                  (520 * math.sin(48 * phase + 0.4) if unbalance else 0)),
                            round(140 * math.sin(48 * phase + 0.7) +
                                  (180 * math.sin(48 * phase + 1.1) if unbalance else 0)),
                            round(100 * math.sin(48 * phase + 1.2)),
                            round(412 + (45 if unbalance else 0) +
                                  24 * math.sin(48 * phase + 0.2))))
        flags = lc.TELEMETRY_FLAGS["SENSOR_READY"]
        body = (lc.TELEMETRY_STRUCT.pack(
            lc.TELEMETRY_SOF, lc.PROTOCOL_VERSION | (flags << 8), sequence,
            timestamp_us, lc.BLOCK_SAMPLES) +
            struct.pack("<" + "h" * len(samples), *samples))
        self.sequence = (sequence + 1) & 0xFFFFFFFF
        return body + struct.pack("<H", lc.crc16(body))


class SyntheticInference:
    """Deterministic visual stimulus, explicitly not a diagnostic model."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, vibration, current) -> InferenceResult:
        self.calls += 1
        return (InferenceResult("NORMAL", 0.99) if self.calls <= 5 else
                InferenceResult("UNBALANCE", 0.95))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=0.0,
                        help="quit automatically after this many seconds")
    parser.add_argument("--screenshot", type=Path,
                        help="save the final dashboard image (implies a 3 s run if needed)")
    parser.add_argument("--model", type=Path,
                        help="TorchScript model; omit it for the original no-model stimulus")
    args = parser.parse_args()
    if not math.isfinite(args.seconds) or args.seconds < 0.0:
        parser.error("--seconds must be finite and non-negative")

    from PyQt5.QtCore import QTimer
    from PyQt5.QtWidgets import QApplication
    from r300_dashboard import R300Dashboard

    artifact_dir = Path("build/demo")
    artifact_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    infer = SyntheticInference()
    vibration_mean = (0.0, 0.0, 0.0)
    vibration_scale = (1.0, 1.0, 1.0)
    title = "R300 Condition Monitor - SYNTHETIC / NO MODEL"
    note = "MODEL-FREE SYNTHETIC DEMO; not hardware or diagnostic evidence"
    if args.model:
        # 현장 변경 지점: 실제 R300 데이터로 학습·승인된 모델 경로를 지정한다.
        from r300_model import TorchInference
        infer = TorchInference(args.model)
        vibration_mean, vibration_scale = infer.vibration_mean, infer.vibration_scale
        title = "R300 Condition Monitor - SYNTHETIC INPUT + SYNTHETIC MODEL"
        note = "SYNTHETIC INPUT/MODEL DEMO; not hardware or diagnostic evidence"
    runtime = R300Application(
        SyntheticSerial(), infer, artifact_dir / f"r300-demo-{stamp}.jsonl",
        model_loaded=lambda: bool(args.model), vibration_mean=vibration_mean,
        vibration_scale=vibration_scale, run_id=f"synthetic-{stamp}",
        condition="synthetic-input", sensor_config="synthetic-contract-frames",
        notes=note)
    qt = QApplication([])
    window = R300Dashboard()
    window.setWindowTitle(title)
    runtime.bind_dashboard(window)
    runtime.start()
    window.show()

    screenshot_ok = [not args.screenshot]

    def finish() -> None:
        if args.screenshot:
            args.screenshot.parent.mkdir(parents=True, exist_ok=True)
            screenshot_ok[0] = window.grab().save(str(args.screenshot))
        qt.quit()

    duration = args.seconds or (3.0 if args.screenshot else 0.0)
    if duration:
        QTimer.singleShot(round(duration * 1000), finish)
    try:
        result = qt.exec_()
    finally:
        runtime.stop()
    if not screenshot_ok[0]:
        print(f"[r300-demo] failed to save {args.screenshot}")
        return 2
    if args.screenshot:
        print(f"[r300-demo] screenshot={args.screenshot}")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
