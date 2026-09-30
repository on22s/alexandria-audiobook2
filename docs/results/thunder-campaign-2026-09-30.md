# Thunder campaign: chapter and unseen-voice results

The two A6000 generation jobs completed their frozen workloads. Independent
local verification checked every generated WAV and recomputed timing totals,
ASR scores, and speaker-identity scores. This report contains measurements and
their limits; it does not promote weights or certify listening quality.

## Scope and provenance

Both generation jobs used production `TTSEngine.generate_batch`, seed 1234,
and source commit `c3870a68d5c606e885458521ee386154f66fcc4c`. Their recorded
harness fingerprint matches that commit. Chapter inputs: 768 entries in 24
batches of 32, workers 4, seven distinct LoRA adapters, 765 LoRA entries and
three VoiceDesign entries. The voice comparison used workers 1, two speakers,
and shipped/clean weights on the same 20 held-out human clips per speaker:
four arms of 20 renders, not 80 independent speakers.

All 40 downloaded human references matched the original holdout WAV hashes
and transcripts in both rendering arms. Their selected source volumes are
outside the holdout manifests' excluded training volumes. This verifies the
recorded holdout policy, not an independent reconstruction of every training
example in the shipped and clean adapters.

All 28 chapter input files and 56 voice input files matched their frozen
hashes when checked remotely after completion. Generation provenance records
clean source checkouts. Subsequent ECAPA scoring refreshed five tracked ECAPA
cache files on the voice box; this is recorded separately from generation's
source provenance and is not described as an unchanged post-scoring checkout.

Raw workloads, recordings, reference transcripts, generated audio, ASR
transcripts, and logs are retained outside Git. Public artifacts contain
aggregate metrics, anonymous score ordinals, and provenance hashes. No
Chunagon material is part of this campaign or these artifacts.

## Completion, audio validity, and speed

| Workload | Verified WAVs | Generation wall time | Output audio | Audio / generation time |
|---|---:|---:|---:|---:|
| Complete chapter | 768/768 | 133.80 min | 73.17 min | 0.547× realtime |
| Four unseen-voice arms | 80/80 | 51.43 min | 11.97 min | 0.233× realtime |

Timing includes cold model loading and adapter changes in each production
batch call. Transfer and independent scoring are excluded. These workloads
have no matched local generation baseline, so these figures do not establish
a cloud-versus-local speed advantage.

Every WAV matched its recorded hash and duration, decoded to finite, nonzero
24 kHz mono audio, and mapped exactly to the frozen batch inputs. The
verification recomputed each batch's audio duration, wall/audio ratio, total
durations, median ratio, and worst ratio. A known one-second sine passed;
silence, nonfinite samples, and a truncated WAV were rejected before scale.
Waveform validity alone does not establish text fidelity or naturalness.
No samples reached the verifier's absolute clipping threshold of 0.9999;
maximum absolute peaks were 0.9492 for the chapter and 0.8164 for the voices.

## ASR text fidelity

The scorer used cached Whisper base.en on CPU and the existing
`word_error_rate` implementation. An exact match, three known negative cases,
and an unscoreable punctuation-only reference were checked first.

| Workload | Scoreable / rendered | Mean clip WER | Median clip WER | Clips with WER >20% |
|---|---:|---:|---:|---:|
| Chapter | 764/768 | 11.68% | 7.41% | 126/764 |
| Voice comparison | 80/80 | 8.83% | 8.35% | 7/80 |

Four punctuation-only chapter entries have no reference-token denominator and
are explicitly unscored, not assigned zero error. None of the scored clips
had an empty ASR hypothesis. Mean/median WER is calculated per clip and
includes recognizer errors; it is not pooled word-weighted WER or a human
listening score. The chapter contains proper names and short utterances, so
the recognizer itself can contribute errors; no error cause is inferred from
these aggregate values.

| Voice | Weights | Clips | Mean clip WER | Median clip WER | Clips with WER >20% |
|---|---|---:|---:|---:|---:|
| Silky mezzo | Shipped | 20 | 9.60% | 10.91% | 2 |
| Silky mezzo | Clean | 20 | 11.16% | 10.64% | 3 |
| Silky alto literary 2 | Shipped | 20 | 7.59% | 6.97% | 1 |
| Silky alto literary 2 | Clean | 20 | 6.98% | 5.21% | 1 |

The measured means move in opposite directions across the two voices. That,
and the speaker-identity comparison below, does not justify a universal
clean-weights improvement or automatic promotion.

## Independently recomputed speaker identity

| Voice | Weights | Clips | Median ECAPA | Minimum ECAPA | Clips below 0.45 |
|---|---|---:|---:|---:|---:|
| Silky mezzo | Shipped | 20 | 0.6723 | 0.5109 | 0 |
| Silky mezzo | Clean | 20 | 0.6873 | 0.5651 | 0 |
| Silky alto literary 2 | Shipped | 20 | 0.7232 | 0.6169 | 0 |
| Silky alto literary 2 | Clean | 20 | 0.7196 | 0.5729 | 0 |

Local CPU rescoring agrees with the cloud scores to a maximum absolute
difference of `3.5762786865234375e-07`. The verifier also independently checked
the saved cloud medians, minima, counts, and source-result hash.

Known controls preceded the generated-pair scoring: two self-pairs exceeded
0.999; 40 same-speaker/different-clip pairs had speaker-level medians 0.8027
and 0.6832; 20 different-speaker pairs had median 0.3375. The 0.45 screening
threshold rejected 18/20 different-speaker pairs and accepted 2/20. Thus it is
an imperfect screening policy, not a calibrated universal identity test.

For matched clips, clean-minus-shipped mean cosine was +0.0235 for mezzo
(clean higher in 13/20 pairs), and -0.0083 for alto (clean higher in 8/20).
These measurements do not support a blanket claim that clean weights improve
both voices. Only two speakers were tested, and no perceptual assessment or
automatic promotion is performed.

## A100 fold evaluation

The separate A100 job finished at 8:42 AM CDT on September 30. Independent
verification of 5,543 sampled quotations per arm gives 63.03% base versus
69.28% LoRA, a gain of 6.24 percentage points including unanswered rows as
wrong. The adapter improves 16 of 19 held-out books and leaves 113 unanswered
versus 89 for base. Training used 975 windows from nine disjoint books.
See the [A100 scoring report](thunder-a100-foldswap-2026-09-30.md) for per-book
results, frozen-input checks, and the absence of retained raw responses and
prompt hashes. No live cloud scripts or jobs were changed to score this run.

## Artifacts and replay

- [Audio verification](../../ab_test_runtime/experiments/thunder_audio_verification__20260930.json).
- [Identity verification](../../ab_test_runtime/experiments/thunder_identity_verification__20260930.json).
- [Verification environment and cached ASR model hashes](../../ab_test_runtime/evidence/thunder_results_20260930/verification_environment.json).
- [Independently fetched remote file hashes](../../ab_test_runtime/evidence/thunder_results_20260930/remote_manifest.json).
- [Holdout validation](../../ab_test_runtime/evidence/thunder_results_20260930/holdout_validation.json).
- [Release validation and explicit API skips](../../ab_test_runtime/evidence/thunder_results_20260930/release_validation.json).
- [Portable audio verifier](../../ab_test_runtime/evidence/thunder_results_20260930/verify_audio_portable.py).
- [Identity verifier](../../ab_test_runtime/evidence/thunder_results_20260930/verify_identity.py).

Replay requires privately retained campaign inputs/audio, the project's Python
environment, the recorded cached ASR model, and an interpreter with
SpeechBrain for ECAPA. Both verifiers accept explicit paths on their CLI; use
CPU and offline model settings. Reference content and hypotheses stay in the
private checkpoint. The initial verifier stopped on a punctuation-only
reference; its exact source is retained as a gzip capture. The final portable
verifier records four chapter entries with no reference tokens as unscored,
resumes cached hypotheses, and records its source hash in the result artifact.

Pitch accent, prosody, audible speaker consistency across the full chapter,
segmentation quality, alignment, export correctness, and listening preference
remain unmeasured by this report. Completion and identity screening must not
be substituted for those assessments.

## Publication checks

The quick release gate passed in an isolated checkout: 3,451 unit tests with
zero skips, 70 API checks passed, and 12 API checks explicitly skipped because
they require full mode. Evidence indexes and API contracts passed. Candidate
source, result artifacts, and regenerated indexes matched that checkout byte
for byte before validation finished.

An earlier run in the shared worktree failed one test because a pre-existing
local `info/exclude` rule hides its temporary untracked runtime probe. The
isolated checkout resolves that environment conflict without removing the
local privacy exclusions. The original failed attempt is not represented as
a successful gate.
