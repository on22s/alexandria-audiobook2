# CPU speaker-window experiment

Completed using CPU-only ECAPA with two threads and process niceness 15.
Elapsed time: 246.8 seconds. No live application code or GPU
job was modified. Existing cached model weights were used offline.

The corpus is [AISHELL-3](https://www.openslr.org/93/), supplied by Beijing Shell
Shell Technology Co., Ltd., under Apache-2.0. Calibration and test each use eight
different speakers, balanced by gender, with same-gender donor voices.
Reference, target, and replacement audio come from separate source utterances.
Each target lasts 24 or 48 seconds. Windows are four seconds with two-second
stride. Injection starts are shifted one second off the window grid. All
speakers are disjoint from the first experiment. Raw source hashes are recorded in `run3/inputs.json`.

Thresholds were frozen after calibration and before test scoring. Each metric
maximizes four-second swap detection subject to at most 5% calibration control
false alarms. Whole-clip threshold: 0.822646;
window-min threshold: 0.489735.

| Held-out condition | Whole-clip flags | Window-min flags |
| --- | ---: | ---: |
| clean | 3/16 | 2/16 |
| same_speaker_splice | 3/16 | 2/16 |
| gain_change | 3/16 | 2/16 |
| swap_2s | 3/16 | 7/16 |
| swap_4s | 4/16 | 15/16 |
| swap_8s | 9/16 | 16/16 |

Wrong-speaker swaps are positive controls. Clean, same-speaker splice, and
local gain-change clips are negative controls: a flag is a false alarm there.
Combined held-out window-min false-alarm rate:
12.5%.
Paired four-second detection difference (window minus whole):
+68.8%; exploratory speaker-cluster bootstrap
95% interval [+43.8%, +93.8%].

Predeclared pilot decision: **NO-GO**. This tests
whether window scoring deserves further investigation, not whether a production
threshold or automatic regeneration should ship.

All 192 required cells were measured. Silence and shorter-than-window controls
were explicitly unmeasured. Completion, split separation, unique cells, donor
separation, and protocol/runner hashes passed independent artifact checks.
An earlier attempt (`run1`) stopped before scoring because a local speaker had
insufficient audio; the failure is retained. The rerun preflights corpus duration.

Limits: one Mandarin human-speech corpus, eight held-out speakers, synthetic
concatenations/splices, and correlated clips. Signal coverage is an energy proxy,
not validated speech activity. This is windowed ECAPA, not the paper's WavLM
wSIM. It has not measured real TTS voice changes, emotion changes, multilingual
robustness, ASR quality, LACI recovery, or pronunciation-adapter training.

Artifacts: `run3/protocol.json`, `run3/model_identity.json`,
`run3/frozen_thresholds.json`, `run3/calibration_rows.json`, `run3/test_rows.json`,
`run3/results.json`, and `run3/analysis.json`. Runners are `run_experiment_4s.py`
and `analyze_results_4s.py`. The runner accepts a fresh output-directory argument;
the analyzer targets the preserved `run3` results.
