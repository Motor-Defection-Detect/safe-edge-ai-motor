# R300 verification plan and evidence boundary

Targets are not actual measurements. Host/SIL and cross-compile results prove software
behavior only; rows requiring a powered rig remain blocked until an approved operator and
fixture produce the named evidence.

| Test | Automated software evidence | Required physical evidence |
| --- | --- | --- |
| TST-COM-002 CRC reject / sequence loss | Parser fault injection and C/Python CRC cross-check | None |
| TST-SEN-001 GY-521 disconnect | Periodic WHO_AM_I/read timeout, sticky sensor-bus error and local-health/heartbeat fail-closed code plus compile/static checks | Disconnect the powered fixture and observe sensor error plus heartbeat/relay response |
| TST-AI-001 held-out evaluation | Hash-bound synthetic 1D-CNN, 4/1/1 session split, finite inference, Macro F1/recall/confusion output (demo evidence only) | QC-passing physical R300 sessions and their held-out metrics |
| TST-AI-002 warning policy / no relay authority | Five-window voter and advisory event tests | Relay observation for final integrated claim |
| TST-DEP-001 PyTorch/TRT latency | TorchScript save/load, optional ONNX export, inference timing instrumentation and p50/p95 aggregation | TensorRT FP16 engine and measured Jetson deployment comparison |
| TST-SYS-001 receiver-to-Qt flow | Offscreen Qt render and monotonic packet-ready delivery metric | Jetson p95 result/video |
| TST-SAF-001 heartbeat loss | C state-machine test: missing `LINK_HEALTHY` stops heartbeat and PWM; host tests clear it on inference or bound-Qt main-loop stalls | Logic-analyzer/GPIO-to-relay delay, target <=700 ms |
| TST-SAF-002 E-stop / TST-SAF-003 reset | AVR state-machine/default-off tests plus CRC-protected live input/output flags bridged into the 1 Hz host diagnostic | Relay polarity, reset and physical E-stop capture |
| TST-SAF-004 no auto re-arm | AVR latched-state transition test | Repeated powered-trip observation |
| TST-SAF-005 AI/safety separation | Application ARM action is evidence-only; AI voting has no relay command path; motor requests remain independently bounded and supervised | Observe unchanged relay during injected AI abnormal events |
| TST-DAT-001 leakage | `data_qc.py` run/split/hash audit | Label-evidence review |
| TST-DAT-002 class separability pilot | Post-startup calibrated RMS/peak/crest factor, current features and dominant-frequency summaries | Three physical repetitions per class plus waveform/PSD/distribution review |
| TST-DAQ-001 / TST-COM-001 | STM32 sequence/timestamp-derived rate including loss/restart/wrap tests, counters and build diagnostics | GPIO-confirmed 1 kHz +/-1%, ten-minute loss <0.5%, overrun 0 |

Use `verify-fast.sh`, `verify-full.sh`, and `verify-integration.sh` only for software
evidence. Do not run the HIL gate, flash, alter fuses, energize a relay, or report a
physical PASS from this repository without the separately approved hardware procedure.
