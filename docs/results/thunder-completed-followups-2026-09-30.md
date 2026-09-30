# Completed Chinese and voice diagnostic follow-ups

## Chinese reference comparison

The same 150 held-out AISHELL-3 lines were generated with the original short reference and the rebuilt long reference, at seed 1234 and unchanged generation limits. All 300 downloaded WAV hashes, finite nonzero 24 kHz mono samples, durations and paired line mappings were independently verified. The existing gate rejects output longer than three times the corresponding human recording.

| Measure | Long reference | Original short reference |
|---|---:|---:|
| Attempted | 150 | 150 |
| Duration rejected | **49 (32.67%)** | **0** |
| Median generated/human duration | 0.852 | 0.914 |
| Maximum duration ratio | 74.20 | 1.217 |
| Longest generated clip | 163.76 s | 5.76 s |

Every long-reference rejection passed with the short reference. This supports the long-reference configuration causing duration failures in this paired test, rather than these sentences inevitably failing. It does not isolate which property of that reference causes the failures. The failed campaign status records the 49 gate verdicts; it is not an infrastructure failure. Automated Chinese ASR, ECAPA and acoustic scoring is now complete; native listening approval remains pending. No goal is closed.

### Completed automated quality scoring

All 300 generated clips and 150 human controls were scored. Pitch tones, silence rejection, regular/perturbed phonation, ECAPA self-similarity and accepting/rejecting CER controls passed. Downloaded scorer/results hashes were verified and aggregate calculations replayed; ASR, embeddings and acoustic extraction were not independently rerun. All 49 duration failures remain failures.

On the **same 101 lines where both arms pass duration**, mean character error rate (CER) is **81.78% long versus 40.20% short**, and median human-anchor ECAPA is **0.7553 versus 0.7672**. The short arm has lower CER on 37/101 pairs; the mean gap does not imply most pairs improved. Across all 150 short clips CER is 40.59%; human controls average 43.15%. That human error rate makes CER a noisy recognizer/source-transcript measure, not pure TTS pronunciation error.

Together with the duration failures, these measurements favor retaining the original short-reference configuration for this frozen test. They do not establish native pronunciation quality, a speaker-identity pass or a general reference-length rule. Common-line acoustic comparisons are retained in the aggregate artifact: long/short median pitch 178.8/186.1 Hz versus human 181.9 Hz; HNR differences from human +0.419/+0.914 dB. Acoustic proximity does not override failed durations.

## Original 18-pair listening retest

One blinded listener supplied all 18 human selections and 17 similarity ratings; pair 17's similarity was omitted and remains unscored. Of 12 main comparisons, 11 have ratings: five 1/5, three 3/5, one 4/5 and two 5/5. Human identification was correct on ten main pairs and undecidable on two. Both identical-human controls rated 5/5; one was identified as Both and the other as Cannot tell. The four foreign-voice controls rated 2/1/1/3; no retrospective passing threshold is assigned.

The anime and warm-baritone control each changed from 5/5 to 1/5 between seeds. Warm tenor remained 1/5 on both seeds. The listener reported garbling in the 163.76-second anime output and multiple speakers in the fantasy held-out human recording. That report does not establish contamination of the training reference or dataset. The key, page and rating hashes are retained; raw notes stay private.

## Four-voice, three-line diagnostic

Twenty-four renders cover anime, warm tenor, breathy tenor and a literary control: the original held-out sentence plus two other frozen held-out lines, at seeds 20260930 and 20261001. There was no training or weight change. Exact source-reference hashes matched training metadata, and the selected 15 source recordings across five audited voices had no exact audio-hash overlap with their recorded training clips. These checks do not establish a single speaker or unseen source volumes.

All 24 generated waveforms were verified. **All eight original sentence/seed repeats are byte-identical to the previous retest**, including the runaway. The anime original line lasts 10.40 seconds at one seed and 163.76 seconds at the other, versus a 9.42-second human recording. Its other four renders have duration ratios 1.008–1.207. This is a reproduced sentence/seed failure, not evidence that every render from the adapter fails.

Stored CPU ECAPA measurements show warm tenor at 0.050/0.083 on the original line versus 0.675–0.770 on the other four renders; breathy tenor at 0.070/0.090 versus 0.481–0.600. Source variation is a lead requiring listening/provenance checks, not proof of multiple speakers. The acoustic metrics are retained from the scorer; the portable verifier does not independently rerun ASR, ECAPA or pYIN. Controls include an ECAPA self-pair of approximately 1 and foreign-human pair 0.369, accepted 100/200 Hz pitch tones, rejected silence, and WER identical/deletion/repetition fixtures. Twelve human ASR controls average **9.36% WER**, so generated WER cannot be interpreted as pure TTS pronunciation error.

### Completed diagnostic listening

The diagnostic package selects the top two WER, top two absolute log duration-ratio deviations and bottom two ECAPA results, includes their other seed, the original literary control line, and two identical-human controls. Overlap leaves ten pairs: eight real comparisons and two controls. Both controls were correctly identified as Both and rated 5/5.

| Voice and line | Seed 20260930 | Seed 20261001 |
|---|---:|---:|
| Warm tenor, original | 1/5 | 1/5 |
| Warm tenor, other selected line | 5/5 | 5/5 |
| Literary control, original | 5/5 | 5/5 |
| Anime, original | 4/5 | 4/5 |

The warm-tenor contrast supports a line/source-specific discrepancy. No breathy examples were selected into this package, so this sitting supplies no human confirmation of that voice's acoustic diagnosis. Five real human clips were correctly identified; Both was selected on three actual human/generated pairs, retained separately from similarity ratings.

The listener again reported garbage/runaway audio on the long anime render despite rating its similarity 4/5. Similarity does not override a quality complaint. Anime ratings were 5/1 in the prior sitting and 4/4 here, showing rating variability; the severe quality complaint recurred. These are one listener's enriched diagnostic selections, not population failure rates, quality equivalence or a new passing gate.

## Evidence and limits

- [Aggregate verification](../../ab_test_runtime/experiments/thunder_completed_followups_verification__20260930.json)
- [Portable verifier](../../ab_test_runtime/evidence/thunder_results_20260930/verify_completed_followups.py), accepting private `--chinese-root`, `--voice-root`, `--retest-root` and aggregate `--out` paths.

Raw corpus text, audio, ASR output, keys, detailed listening notes and weights remain private. No Chunagon material is used. Existing production settings, safety limits and voice weights remain unchanged. A100 development and Japanese confirmation are documented in [the completed development report](thunder-japanese-development-2026-09-30.md).
