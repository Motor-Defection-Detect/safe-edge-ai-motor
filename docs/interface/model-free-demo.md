# R300 model-free demo

`edge/r300_demo.py` runs the real receiver, preprocessing, condition voting,
logging and Qt dashboard with contract-valid synthetic telemetry. Its callback
is deterministic and is not a diagnostic model.

On a host with PyQt5 and pyqtgraph installed:

```bash
python3 edge/r300_demo.py
```

In the project container, run a bounded offscreen smoke test and save a
screenshot into the host-mounted workspace:

```bash
QT_QPA_PLATFORM=offscreen python3 edge/r300_demo.py --seconds 3 \
  --screenshot build/demo/r300-demo.png
```

The title and metadata identify the run as `SYNTHETIC / NO MODEL`. AVR safety
remains `UNKNOWN` because no safety hardware or diagnostic frame is simulated.
AI health remains `ERROR` because `MODEL_LOADED` is honestly false; the changing
class/condition values are deterministic UI stimuli only.
The output is not model, sensor, motor, relay or HIL evidence.

After training the explicitly synthetic 1D-CNN in `ml-synthetic-model.md`, pass
`--model build/models/model_B_synthetic.pt` to exercise artifact loading and inference. The title
remains marked synthetic, so that mode is also not hardware or diagnostic evidence.
