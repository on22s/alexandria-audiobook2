# CPU speaker-window experiment

Completed using CPU-only ECAPA with two threads and process niceness 15.
Elapsed time: 266.4 seconds. No live application code or GPU
job was modified. Existing cached model weights were used offline.

The corpus is [AISHELL-3](https://www.openslr.org/93/), supplied by Beijing Shell
Shell Technology Co., Ltd., under Apache-2.0. Calibration and test each use eight
different speakers, balanced by gender, with same-gender donor voices.
Reference, target, and replacement audio come from separate source utterances.
Each target lasts 24 or 48 seconds. Windows are eight seconds with four-second
stride. Raw source hashes are recorded in `run2/inputs.json`.

Thresholds were frozen after calibration and before test scoring. Each metric
maximizes four-second swap detection subject to at most 5% calibration control
false alarms. Whole-clip threshold: 0.813880;
window-min threshold: 0.581009.

| Held-out condition | Whole-clip flags | Window-min flags |
| --- | ---: | ---: |
| clean | 0/16 | 0/16 |
| same_speaker_splice | 0/16 | 0/16 |
| gain_change | 0/16 | 0/16 |
| swap_2s | 1/16 | 0/16 |
| swap_4s | 3/16 | 2/16 |
| swap_8s | 7/16 | 16/16 |

Wrong-speaker swaps are positive controls. Clean, same-speaker splice, and
local gain-change clips are negative controls: a flag is a false alarm there.
Combined held-out window-min false-alarm rate:
0.0%.
Paired four-second detection difference (window minus whole):
-6.2%; exploratory speaker-cluster bootstrap
95% interval [-31.2%, +31.2%].

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

Artifacts: `run2/protocol.json`, `run2/model_identity.json`,
`run2/frozen_thresholds.json`, `run2/calibration_rows.json`, `run2/test_rows.json`,
`run2/results.json`, and `run2/analysis.json`. Runners are `run_experiment.py`
and `analyze_results.py`. The runner accepts a fresh output-directory argument;
the analyzer targets the preserved `run2` results.
