# R300 software function implementation trace

This trace covers the software-only baseline. It does not claim flashing, fuse
configuration, relay energization, Jetson deployment, TensorRT execution, or HIL.

## System requirement coverage

| Requirement | Software evidence / remaining boundary |
| --- | --- |
| SYS-FUN-001 | GY-521/MPU-6050 I2C2/EXTI acquisition, identity/config setup and runtime disconnect supervision are implemented; physical disconnect is TST-SEN-001. |
| SYS-FUN-002 | TIM3-triggered ADC1 with circular DMA2 acquisition is implemented; physical 1 kHz confirmation is TST-DAQ-001. |
| SYS-FUN-003 | Full 128-sample sequence/timestamp/CRC frame generation is implemented and cross-checked in C/Python. |
| SYS-FUN-004 | 921600 8N1 USART2 DMA transport and loss/error counters are implemented; the ten-minute target is TST-COM-001. |
| SYS-FUN-005 | Validated 512-sample ring/window with hop 256 and discontinuity reset is implemented. |
| SYS-FUN-006 | Four-class contract, justified three-class fallback metadata and inference boundary validation are implemented; trained classifier quality is model work and TST-AI-001. |
| SYS-FUN-007 | The six QTS panels, 8-10 Hz store-to-widget refresh and offscreen Qt checks are implemented. |
| SYS-FUN-008 | Confidence-gated five-window voting and five-normal clear hysteresis are implemented. |
| SYS-FUN-009 | Immutable raw/index/metadata plus async event/metrics evidence files are implemented. |
| SYS-FUN-010 | PWM, fixture, sensor configuration, mount and power-cycle context are retained per session. |
| SYS-SAF-001 | AI/condition code has no relay command path; AI and Safety widgets/events are separate. |
| SYS-SAF-002 | PD7 relay authority exists only in the AVR runtime; STM32/Jetson diagnostics are read-only. |
| SYS-SAF-003 | STM32 heartbeat requires fresh Jetson LINK_HEALTHY and local acquisition health. |
| SYS-SAF-004 | Two-edge heartbeat qualification, 500 ms timeout and latched trip are implemented; physical <=700 ms is TST-SAF-001. |
| SYS-SAF-005 | Raw/debounced active E-stop has priority and latches TRIPPED; physical relay response is TST-SAF-002. |
| SYS-SAF-006 | Relay inactive level is preloaded before PD7 becomes an output and watchdog reset returns safe; polarity/reset evidence is TST-SAF-003. |
| SYS-SAF-007 | A cleared fault cannot auto-rearm; a debounced release and deliberate healthy ARM cycle are required. |
| SYS-SAF-008 | Relay command requires ARM, heartbeat, inactive E-stop and internal health in the tested AVR truth table. |
| SYS-PER-001 | UI sample rate is derived from STM32 sequence/timestamp with loss/restart/wrap handling; GPIO accuracy remains TST-DAQ-001. |
| SYS-REL-001 | Sequence-gap loss accounting is implemented; the ten-minute physical target remains TST-COM-001. |
| SYS-REL-002 | CRC failures are discarded and counted, with fault injection and C/Python cross-check evidence. |
| SYS-REL-003 | Acquisition/platform overrun counters and sticky local fault are implemented; zero physical overruns remains TST-COM-001. |
| SYS-AI-001/002 | A demo-only synthetic 1D-CNN emits F1, recall and confusion evidence over session-separated data; actual claims still require QC-passing physical held-out sessions. |
| SYS-PER-002 | Inference p50/p95 measurement and health threshold are implemented; Jetson TensorRT evidence is excluded TST-DEP-001. |
| SYS-PER-003 | Validated-packet-to-render p95 is measured on one Jetson monotonic clock; target execution is TST-SYS-001. |
| SYS-EVT-001 | Four high-confidence abnormal results within the ongoing five-window history produce WARNING inside the 1.5 s software budget; physical/log timing remains system evidence. |
| SYS-REL-004 | AI WARNING and safety trip remain independent and separately logged; a ten-minute run is physical/model evidence. |
| SYS-DAT-001 | Physical condition, label evidence, fixture, PWM and run identity are mandatory in assigned dataset sessions. |
| SYS-DAT-002 | QC rejects a run in multiple splits and windows only after run/session allocation. |
| SYS-DAT-003 | QC enforces class contract, six sessions/class, 4/1/1 split, PWM diversity and fallback rationale; collection remains physical work. |
| SYS-DAT-004 | Runtime accepts validated finite training normalization metadata and QC freezes it per session; deriving statistics is model/data preparation. |
| SYS-DAT-005 | Ablation is model evaluation and intentionally excluded from this software-only baseline. |

## STM32F411

| SFD function | Implementation evidence |
| --- | --- |
| ST-FN-001 system init | `r300_stm32_platform_init()` configures bounded HSI/PLL startup, GPIO, I2C2, EXTI, ADC/DMA, timers, USART1/USART2, DMA and safe outputs through the local typed register layer. PWM and heartbeat levels are preloaded inactive. |
| ST-FN-002 MPU-6050 init | WHO_AM_I plus PWR_MGMT_1, 1 kHz rate, DLPF, +/-16 g and data-ready configuration live in `r300_stm32_platform.c`; calibration knobs are in `r300_board_config.h`. A 100 ms identity check and DRDY age gate local health. |
| ST-FN-003 DRDY ISR | `EXTI1_IRQHandler()` only clears the IRQ, captures the 1 MHz TIM2 event timestamp and marks a pending sample. TIM2 starts before EXTI/measurement so the first edge has a valid timebase. |
| ST-FN-004 burst read | `r300_stm32_vibration_poll()` owns the captured timestamp and performs one six-byte big-endian I2C burst outside the ISR. |
| ST-FN-005 ADC DMA ownership | DMA2 circular buffer exposes the latest ACS712 sample. A 12k/22k divider protects PA0, boot calibration removes zero-current offset, and the sensor variant sensitivity remains a tuning macro. |
| ST-FN-006 block commit | `r300_stm32_commit_sample()` samples that latest, freshness-supervised current value on each vibration timestamp and preserves the first-sample timestamp. |
| ST-FN-007/008 frame and CRC | `r300_telemetry_pack()` and generated `r300_crc16()` carry the contract-defined sensor, local-fault, motor-command and fresh AVR diagnostic flags. |
| ST-FN-009 UART TX DMA | `r300_stm32_prepare_tx()` retains a full frame until `r300_stm32_uart_tx_start()` completes. |
| ST-FN-010/011 command/HEALTH RX | DMA1 Stream5 owns a 64-byte USART2 RX ring while the main loop services I2C. A single parser resynchronizes HEALTH and MOTOR_COMMAND frames with exact version, CRC and sequence validation. |
| ST-FN-012/013 local health and heartbeat | `r300_stm32_local_healthy()` and `r300_heartbeat_step()` fail closed to PB8 low unless both the HEALTH frame and its `LINK_HEALTHY` supervision bit are current. |
| ST-FN-014 PWM | 1 kHz TIM4 CH1 on PB6, PB7 low, boot 0%, forward-only LR7843 control. A fresh command, Jetson HEALTH and local acquisition health are all required; otherwise duty is forced to 0%. |
| ST-FN-015 diagnostics | `r300_stm32_diagnostics()` reports sequence, dropped blocks, invalid HEALTH, CRC-specific failures, buffered samples, acquisition overrun and keepalive validity. `r300_stm32_prepare_diagnostic()` adds runtime/peripheral counters plus the measured current zero calibration. The first calibrated snapshot is sent before the first sensor block, then the fixed CRC frame runs at 1 Hz behind telemetry priority. |
| AVR diagnostic bridge | USART1 receives the optional CRC/version/sequence-protected AVR frame; `r300_stm32_avr_diag_receive()` expires it after 300 ms, exposes latched state/cause in TELEMETRY, and bridges current heartbeat/ARM/E-stop/internal/relay-command flags into the 1 Hz STM diagnostic without changing relay authority. |

## Jetson host runtime (synthetic demo model included)

| SFD function | Implementation evidence |
| --- | --- |
| JS-FN-001 serial loop | `SerialReceiver.run()` uses a dedicated finite-timeout UART worker; validated blocks cross a bounded queue to `InferenceWorker`. A 100 ms in-flight inference/presentation deadline, a latched worker-infrastructure failure and a 300 ms bound-Qt refresh deadline clear `LINK_HEALTHY` for a broken application path while model inference exceptions remain isolated and visible. |
| JS-FN-002/003 framing and loss | `TelemetryParser` resynchronizes mixed TELEMETRY/STM_DIAGNOSTIC SOFs, rejects zero/oversize or semantically inconsistent payloads, validates version/length/CRC and handles uint32 wrap gaps. |
| JS-FN-004/005 ring/window | `EdgePipeline` uses a bounded 512-sample deque and exact 256-sample hop; sequence/queue discontinuities reset buffered samples and vote history so windows never bridge lost data. |
| JS-FN-006 preprocessing | DC removal plus validated finite training mean/std in `_preprocess()`; non-finite derived values fail the AI pipeline closed. An independent validated per-axis g/count calibration feeds operator waveform/FFT so model normalization never changes display units. |
| JS-FN-007 current features | Mean/RMS/absolute peak in `_current_features()`. |
| JS-FN-008 inference wrapper | `TorchInference` hash-checks TorchScript plus preprocessing/label metadata, validates 512x3 vibration and three current features, and returns the same checked callback result used by the pipeline. The shipped weights are synthetic demo evidence only. |
| JS-FN-009 condition voting | Five-window/four-abnormal warning vote gated at confidence 0.85, with five-normal clear hysteresis. The exact policy is recorded in both the startup event and immutable run metadata. |
| JS-FN-010 HEALTH/command | `SerialReceiver` sends HEALTH plus the latest bounded MOTOR_COMMAND every 100 ms. STOP is the default; `R300Application.set_motor_duty()` is the explicit experiment API, unhealthy evidence storage forces the transmitted command to STOP, and commands expire to zero after 300 ms. |
| JS-FN-011 metrics | Loss, CRC failures, duplicate/out-of-order frames, STM32 sequence/timestamp-derived acquisition sample rate, telemetry age/staleness, every STM diagnostic counter/status, queue/log/callback failures, inference health, inference/UI p50/p95 latency, and current features flow through `DashboardPresenter`. Sequence loss and uint32 wrap preserve the source-rate estimate; an STM32 restart resets its horizon. |
| JS-FN-012/013 logging | The asynchronous `EventLogger` writes the validated CRC frame stream unchanged to `*.raw.bin`, packet/run/QC offsets to `*.index.csv`, run/session context to `*.metadata.json`, timestamped events to the requested JSONL path, and runtime/UI metrics to `*.metrics.jsonl`. The manifest preserves `run_id`, `class`, physical `condition`, `pwm_duty`, `start_utc`, `sensor_cfg`, `notes`, `fixture`, `power_cycle`, `mount`, run-level split/evidence/class contract, and the STM-reported current zero count; inferred state uses a separate `ai_condition` field. Health and safety display transitions are also persisted. A failed writer stops accepting records and counts drops. |
| JS-FN-014 TensorRT load | TorchScript loading and optional ONNX export are implemented. TensorRT engine creation and latency remain excluded until the target JetPack/TensorRT environment exists. |

`scripts/r300/data_qc.py` consumes those session artifacts and automates the non-model
parts of QC-001 through QC-010: raw/index CRC, protocol-version and status-semantic
integrity, sequence loss, sample rate,
per-channel clipping using the recorded calibration, frozen/sensor-fault detection, metadata/evidence linkage, run-level split
leakage, session balance, PWM confounding and raw/run duplication. Startup exclusion is
reported as a sample count. The stable region additionally yields calibrated vibration
RMS/peak/crest factor, current mean/RMS/peak and per-axis dominant Hann-FFT evidence;
physical label/photo agreement and cross-session separability remain operator reviews.

`R300Application` is the composition root for the UART/inference workers, logger, pipeline,
presenter and dashboard store. Packet-receipt-to-Qt-render p95 uses one Jetson monotonic
clock; true sensor-acquisition-to-UI latency remains unavailable until STM32/Jetson clock
synchronization is measured. Receiver-cycle and inference callback latency remain separate.

## Qt HMI

QT-FN-001 through QT-FN-007 are implemented by `DashboardStore`,
`DashboardPresenter`, and `R300Dashboard`: five independently colored health states,
one-second waveform with plot-native peak-preserving downsampling, worker-side Hann FFT, class/confidence and five-window vote,
separate AI/Safety badges, sample/loss/inference-p95/UI-delivery-p95 cards, detailed
STM reliability counters, a bounded
three-column model-view event table, 8-10 Hz refresh, and an advisory-only ARM signal.
Packet-to-UI p95 starts at validated-packet receipt, and the documented 0.5% loss,
1% sample-rate and 30 ms inference limits drive `DEGRADED` health. Sensor health and
motor command/local-fault evidence use the contract-defined STM32 status flags, but the
Motor panel remains `UNKNOWN` without physical current/rotation/relay feedback. Fresh
CRC-valid AVR diagnostics drive the AVR health and ARMED/DISARMED/TRIPPED Safety display;
the 1 Hz STM diagnostic supplements the latched state/cause with current heartbeat, E-stop,
ARM, internal-health, and relay-command status. Relay command is explicitly not contact feedback;
missing diagnostics keep the relay state `UNKNOWN` while a current STM_DIAGNOSTIC shows
the healthy/unhealthy STM32 supervision state as the documented fallback. When sensor
TELEMETRY stops but the 1 Hz diagnostic stream continues, the UI identifies the STM32
sensor fault and marks the data path degraded; when both streams stop it returns the
subsystems to stale/unknown. The fallback never claims relay state or GPIO measurement.
The UI has no relay authority.
`R300Application.handle_arm_request()` records the operator action in UI/JSONL evidence
without emitting a firmware or relay command. `e2e_p95_updated` feeds a rate-limited
`ui_metrics` evidence record through the same application binding.

## ATmega128A

AV-FN-001 through AV-FN-009 are implemented by `r300_safety.c`,
`r300_avr_platform.c`, and `r300_avr_runtime.c`: relay-off-first initialization,
active-low input reads, debounce, INT4 timestamp capture, 500 ms timeout, latched
first-out trip causes, deliberate fault-cleared re-arm, deterministic relay update,
and a 500 ms watchdog that resets a stalled main loop within the system's 700 ms
relay-command-off target.
Two timely heartbeat edges are required before ARM so the STM32's reset-to-LOW output
transition cannot by itself satisfy heartbeat health.
The physical ARM input is maintained permission: activation is debounced, raw release
turns the relay command off immediately, and only a debounced release grants a new ARM
cycle. A contact-bounce release therefore cannot re-arm a failed or tripped attempt.
AV-FN-010 uses a non-blocking 38400 8N1 USART0 publisher with a fixed 16-byte CRC frame.
Its version high byte reports the current heartbeat/ARM/E-stop/internal/relay-command snapshot,
while `state_cause` preserves the latched safety decision and first-out cause.
It is read-only diagnostic evidence and is never used as relay authority.
Power-on remains `DISARMED` with a `RESET` cause; heartbeat loss becomes a latched trip
only after an explicit healthy ARM has placed the relay state machine in `ARMED`.

Host and cross-build tests are software evidence only. Relay polarity, clock accuracy,
sensor calibration, current accuracy, electrical levels, thermal behavior and timing on
the physical boards remain HIL evidence boundaries.
