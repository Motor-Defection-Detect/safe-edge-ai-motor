# R300 acquisition labels

Labels are physical experiment conditions, not conclusions inferred from a waveform.
Every run must retain a fixture/checklist or photo reference in `label_evidence`.

| Label | Physical condition | Required evidence | Do not do |
| --- | --- | --- | --- |
| `NORMAL` | Fan and motor mounted to the documented baseline with no added mass or load change | Mount photo/checklist, PWM and power-cycle ID | Reuse after remount without a new run ID |
| `UNBALANCE` | One controlled, repeatable eccentric-mass fixture; mass and radius recorded in `condition`/notes | Fixture photo and mass/radius record | Loose or uncontained mass |
| `LOOSENESS` | One predetermined fixture joint changed from the baseline; all other joints unchanged | Before/after checklist and marked joint photo | Uncontrolled free-running hardware or changing multiple joints |
| `LOAD` | A repeatable external load fixture with the same mount as `NORMAL` | Load-setting record plus current evidence | Use PWM alone as the load label |

Use PWM 60/75/90% as the initial acquisition strata from R300-MLD-001. Each class must
span more than one PWM before model use. Run three pilot repetitions per candidate class;
if the intended RMS/PSD/current effect is not directionally repeatable in at least two,
change the fixture or record a justified three-class fallback. Never relabel a weak run
from the sensor plot alone.
