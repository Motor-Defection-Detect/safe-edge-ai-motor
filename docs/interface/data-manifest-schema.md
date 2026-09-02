# R300 session artifact and manifest schema

One previously unused event path such as `runs/R001.jsonl` produces the following immutable
session set. Startup fails if any sibling artifact already exists, preventing accidental
append or overwrite across runs.

- `R001.raw.bin`: concatenated validated TELEMETRY frames, unchanged.
- `R001.index.csv`: packet/run fields plus raw byte offsets and QC linkage.
- `R001.metadata.json`: latest session context.
- `R001.jsonl`: timestamped application, AI, health and safety events.
- `R001.metrics.jsonl`: rate-limited runtime and UI metrics.

Required metadata fields are `protocol_version`, `contract_sha256`, `run_id`, `class`,
`pwm_duty`, `condition`, `start_utc`,
`sensor_cfg`, `notes`, `fixture`, `power_cycle`, `mount`, `split`, and
`label_evidence`. `startup_exclusion_seconds` records the fixed 5-10 second prefix that
downstream dataset construction must omit (10 seconds by default). The runtime also records `class_contract` and
`class_fallback_reason`; a three-class contract is rejected unless the reason is present.
Every session in a suite must use the same class contract/fallback rationale, and the suite
must contain at least one labeled session for every declared class before it is training-ready.
`condition_policy` freezes the five-window vote, abnormal count, confidence threshold and
normal-clear hysteresis used for operator warnings during the run; QC rejects a missing or
drifted policy so the displayed decision remains reproducible.
The initial calibrated STM diagnostic updates `current_zero_count` from `null` to the
measured motor-off ADC calibration before the first sensor frame; each CSV packet row
copies that value alongside its raw offset.
Per-axis `vibration_g_per_count` and the model-boundary `vibration_mean`/
`vibration_scale` are stored separately so display calibration never gets confused with
training normalization.
`split` is one of `unassigned`, `train`, `validation`, or `test` and
is assigned at the run/session level before windowing. `unassigned` and missing label
evidence are valid during setup but are QC flags and are not training-ready.
The protocol version and full canonical contract SHA-256 bind every run to the exact
generated frame/timing schema; QC rejects data audited with a different consumer contract.

Audit one or more sessions without changing them:

```text
python scripts/r300/data_qc.py runs/*.metadata.json --output results/data_qc.json --strict
```

The report contains measured packet loss, CRC errors, sample rate, per-channel clipping
against the recorded calibration, frozen-run
length, the first ten seconds' training-exclusion count, hashes, and suite-level
leakage/balance/duplicate flags. Assigned dataset runs shorter than 90 seconds are flagged;
the post-exclusion stable region also reports calibrated vibration RMS/peak/crest factor,
current mean/RMS/peak, and the dominant Hann-FFT frequency/magnitude per axis for pilot
separability review. These are measured signal summaries, not inferred labels.
the suite audit also requires at least six sessions per class with a 4/1/1 train/validation/test
allocation. After the startup exclusion, each packet's STM32 `MOTOR_ACTIVE` evidence must
agree with its indexed `pwm_duty`; this validates the PWM output command but does not claim
physical rotation or closed relay contacts. A flag requests review or recollection; the tool never
deletes or silently edits source evidence.

For a controlled condition change inside a demo run, call
`R300Application.mark_physical_condition(condition, evidence)`. It timestamps the
ground-truth transition, updates subsequent packet-index context, and leaves the AI
condition transition as a separate event so fault-to-warning delay can be derived without
treating model output as ground truth.
