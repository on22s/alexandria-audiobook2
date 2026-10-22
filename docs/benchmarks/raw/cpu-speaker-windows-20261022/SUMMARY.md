# CPU experiment: sliding-window speaker scoring

**Result: useful detection signal, but both pilots failed their predeclared
acceptance criterion. Keep this experimental; no production gate was changed.**

Two CPU-only runs measured 384 planned cases across 32 distinct AISHELL-3
speakers. Each run used eight calibration speakers and eight different test
speakers, balanced by gender, with same-gender wrong-voice donors. The second
run excluded every speaker from the first and placed voice changes off the
window grid. Thresholds were frozen before held-out scoring.

| Held-out result | Eight-second windows | Four-second windows |
| --- | ---: | ---: |
| Four-second substitutions detected, windowed | 2/16 | 15/16 |
| Four-second substitutions detected, matched whole-clip baseline | 3/16 | 4/16 |
| Eight-second substitutions detected, windowed | 16/16 | 16/16 |
| Eight-second substitutions detected, matched whole-clip baseline | 7/16 | 9/16 |
| False alarms on clean/same-speaker/gain controls, windowed | 0/48 | 6/48 |
| False alarms on those controls, whole-clip | 0/48 | 9/48 |
| Predeclared pilot decision | NO-GO: primary detection did not improve | NO-GO: false alarms exceeded 5% |

The datasets differ between columns, so compare each windowed score with its
own matched baseline rather than treating the columns as a controlled window-size
comparison. The shorter-window follow-up was motivated by the first result and
tested on fresh speakers; it does not erase that first result.

All six windowed false alarms in the follow-up belong to one held-out speaker.
That is a post-hoc diagnostic, not a reason to exclude the speaker or retune the
threshold. It suggests reference/voice-dependent calibration needs investigation.

The scorer used the project's existing ECAPA model, cached offline at revision
`0f99f2d0ebe89ac095bcc5903c4dd8f72b367286`. Every parameter was checked to be on
CPU. Runs used two threads at niceness 15; recorded scoring-run elapsed time was
266.4 + 246.8 seconds. No GPU job, production source, dependency, or default was
modified. Source audio came from [AISHELL-3](https://www.openslr.org/93/),
provided by Beijing Shell Shell Technology Co., Ltd., under Apache-2.0.

Artifact checks passed for all 384 cases: no missing/duplicate cells, no
calibration/test speaker overlap, no donor leakage across splits, and matching
protocol/runner identities. Silence and shorter-than-window controls were
explicitly unmeasured. A preliminary attempt stopped before scoring because
a locally partial speaker directory lacked enough audio; its failure is retained
under `run1/`.

This measures detection of controlled substitutions in Mandarin human speech.
It does not validate real TTS drift, emotion robustness, other languages,
LACI recovery, ASR, or pronunciation training. The metric is windowed ECAPA,
not the paper's WavLM wSIM.

The next justified study is reference-quality or multiple-reference calibration
on new speakers, followed by independently adjudicated generated speech. No
threshold should be selected using these held-out results.

Detailed reports: [eight-second windows](RESULTS.md),
[four-second follow-up](RESULTS_4S.md). Each report lists its raw artifacts;
the source/asset hashes, full window scores, protocols, thresholds, and CPU
runner scripts are preserved in this directory.
