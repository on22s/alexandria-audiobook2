# Completed development and Japanese controls

The nine-book A100 development comparison independently verifies **61.77% base (1,640/2,655) → 72.54% LoRA (1,926/2,655)**, +10.77 percentage points. It fixes 521 predictions and regresses 235; unanswered rows remain wrong (40 base, 29 LoRA). Six accepting/rejecting scorer controls passed, all 27 frozen inputs matched, and all 5,310 saved predictions agree with their answer keys. Reconstructing the sampled windows recovers every expected ID. The shared-answered comparison has 2,594 IDs: 62.84% → 73.94%, 513 fixes and 225 regressions.

Against the previously verified nineteen-book held-out LoRA accuracy of 69.28%, the development accuracy is **3.27 points higher**. This is a quote-weighted comparison for this frozen adapter and serving configuration, not a universal generalisation result. The development books were used for training. No goal is closed or adapter promoted. Raw responses and per-row prompt hashes were not retained; the verifier recomputes saved predictions rather than reparsing replies.

## Japanese grouping

Thirty fresh same-narrator pairs were generated separately and newline-grouped at two fixed seeds: 180 WAVs, all independently hash-checked, finite, nonzero, 24 kHz, with zero duration rejects. Grouped audio is compared with the sum of the two separately generated durations against the matching concatenated human recording.

| Seed | Grouped closer to human duration | Separate closer | Ties | Median absolute duration error, separate → grouped | Paired sign test, two-sided |
|---|---:|---:|---:|---|---:|
| 1234 | 21 | 8 | 1 | 11.71% → 8.75% | 0.0241 |
| 1235 | 24 | 6 | 0 | 11.48% → 7.20% | 0.00143 |

These paired measurements support a duration improvement on these thirty pairs. They do not establish perceptual quality or a general benefit across narrators.

Aligned accent and acoustic scoring completed for all 180 generated and 90 human recordings, with no alignment skips or partial acoustic rows. All eleven accent tests ran without skips; tone, silence and irregular-phonation controls passed. The grouped accent-drop measure is 81.86% / 80.36% at the two seeds versus 78.67% for matching human recordings. Those measures use different voiced-phrase denominators; a higher percentage is not proof of better pronunciation. Grouped median pitch is 131.25 / 130.50 Hz versus human 137.00 Hz; spread is 61.55 / 64.20 Hz versus human 62.75 Hz. Jitter, shimmer and HNR measurements are included in the aggregate artifact. HNR is measured in dB, not interpreted as a ratio.

Whisper-base mean CER is 19.49% / 19.85% on grouped generation, versus 26.96% on the matching human recordings. That human error rate makes this a noisy instrument, not a pronunciation pass. Independent large-v3 transcription is now complete for all 180 generated and 90 human recordings, with accepting/rejecting CER controls passed.

| Seed | Left CER | Right CER | Grouped CER | Matching grouped human CER |
|---|---:|---:|---:|---:|
| 1234 | 15.43% | 14.26% | 13.14% | 27.01% |
| 1235 | 13.28% | 13.21% | 12.32% | 27.01% |

Grouped mean CER is descriptively lower in both seeds. Separate segments and grouped recordings have different reference lengths; these arm means are not a token-weighted concatenation comparison or a significance test. The high human CER remains a warning about recognizer/source-transcript mismatch, so lower generated CER does not prove better pronunciation than humans. This is thirty pairs from one narrator, not native listening approval.

## The identity control changes the interpretation

The earlier grouped-versus-individual-short-clip ECAPA advantage does **not** survive concatenating the separately generated clips and comparing them against the identical human anchor.

| Seed | Full recording ECAPA, separate concatenation / grouped | Equal-prefix-duration ECAPA, separate concatenation / grouped | Human ceiling fails to bound both arms |
|---|---|---|---|
| 1234 | 0.8847 / 0.8770 | 0.8756 / 0.8692 | 16/30 pairs |
| 1235 | 0.8826 / 0.8759 | 0.8816 / 0.8740 | 17/30 pairs |

The equal-duration control truncates all compared recordings to the same prefix length; it is an instrument control, not approved playback. The human ceiling uses a different held-out pair from the same narrator. Because it fails to bound many arms, no normalized speaker-identity goal pass is claimed. These numbers are descriptive; the small ordering difference is not a listening preference or a causal explanation.

Native Japanese and Chinese pronunciation, accent and listening validation remain pending until fluent raters can help. No language-specific ratings were collected. The private delivery package is unrated. Raw recordings, transcripts, input paths, responses, notes and sealed keys remain outside Git. No Chunagon material is involved.

[Aggregate results](../../ab_test_runtime/experiments/thunder_japanese_development_verification__20260930.json) and [portable development/duration verifier](../../ab_test_runtime/evidence/thunder_results_20260930/verify_japanese_development.py). The verifier repeats attribution and waveform/duration checks; it does not independently repeat cloud ASR, accent, ECAPA or acoustic extraction.

[Completed quality score replay](../../ab_test_runtime/evidence/thunder_results_20260930/verify_completed_quality.py) checks all 19 downloaded manifest files and both scorer hashes, then recomputes the Chinese common-line and Japanese per-arm CER summaries from saved scores. It does not re-transcribe audio or independently repeat the GPU instruments. [Quality aggregate](../../ab_test_runtime/experiments/thunder_completed_quality_verification__20260930.json).
