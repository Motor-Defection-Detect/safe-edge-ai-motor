# R300 synthetic model demo

This path exercises training, artifact hashing, loading, preprocessing parity and the existing
Qt/serial pipeline before physical R300 data exists. Its scores are never equipment-accuracy
evidence; every generated artifact says `SYNTHETIC` and `diagnostic_use_allowed=false`.

Train the four-class 1D-CNN in the project Docker container:

```bash
python3 scripts/r300/train_synthetic_model.py
python3 edge/r300_demo.py --model build/models/model_B_synthetic.pt
```

The trainer creates six independent sessions per class and assigns 4/1/1 sessions to
train/validation/test before making 512-sample, hop-256 windows. It writes the TorchScript model,
hash-bound metadata, session manifest, metrics and confusion CSV under `build/models/`.

Optional ONNX export (the R300 Docker image includes `onnx`):

```bash
python3 scripts/r300/train_synthetic_model.py \
  --onnx build/models/model_B_synthetic.onnx
```

Before real diagnostic use, replace the synthetic generator with QC-passing R300 session
artifacts: three pilot sessions per candidate class first, then six 90-second sessions per retained
class at mixed 60/75/90% PWM. Keep the session split fixed before windowing. TensorRT FP16 and
p50/p95 latency must be measured on the actual Jetson Orin Nano; desktop or Docker timing is not a
substitute.
