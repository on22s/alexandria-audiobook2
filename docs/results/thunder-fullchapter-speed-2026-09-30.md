# Full-chapter worker-count confirmation

The same frozen 768-entry chapter completed at 16 workers in **100.10 minutes**, versus **133.80 minutes** at four workers: **25.18% less generation wall time**, or 1.337 times the throughput. Generation includes model loads and adapter changes; transfer and scoring are excluded. All 768 downloaded WAV hashes, durations, finite nonzero 24 kHz mono samples, and saved text/index mappings were independently verified. No samples reached the 0.9999 clipping threshold; peak absolute amplitude was 0.9961.

| Measure | Four workers | Sixteen workers |
|---|---:|---:|
| Generation | 133.80 min | 100.10 min |
| Mean clip WER | 11.68% | 11.56% |
| Clips above 20% WER | 126/764 | 132/764 |

The saved Whisper base.en hypotheses were independently rescored on the same 764 scoreable references; four punctuation-only entries are excluded in both arms. WER improves on 57 clips, worsens on 60, and is unchanged on 647. Mean paired change is -0.123 percentage points; a seeded 10,000-draw clip bootstrap gives a 95% interval of **-0.433 to +0.176 points**, including zero. This is descriptive uncertainty within one chapter, not independent-book generalization or proof of equal listening quality.

The new run produces 72.64 minutes of audio in 100.10 minutes, so it remains slower than realtime (1.378 generation minutes per audio minute). The measurement confirms a speed gain on this workload; it does not establish improved transcription, speaker identity, or listening preference, and does not change production settings or promote weights.

Generation used clean runtime `454417276a323649acb6c2c98a02f97acd2edfaf`, the original 24 batches of 32, unchanged weights and seed 1234, with only the workload's worker count changed. Input and result hashes are retained in the aggregate artifact. Raw workloads, audio and transcripts remain private.

- [Aggregate verification](../../ab_test_runtime/experiments/thunder_fullchapter_speed_verification__20260930.json)
- [Portable verifier](../../ab_test_runtime/evidence/thunder_results_20260930/verify_fullchapter_speed.py), accepting private raw-root, baseline ASR/summary, app, and aggregate output paths as CLI arguments.

## Blind listening follow-up

The project owner rated all 24 blinded same-text pairs on September 30. Four preference controls passed: both identical pairs tied, and both deliberately truncated clips lost to their intact counterparts. The sealed key and page hashes were verified before decoding; controls are excluded from the following counts.

| Preference across 20 real comparisons | Count |
|---|---:|
| Four workers | 8 |
| Sixteen workers | 7 |
| Tie | 5 |

The rater judged the same speaker on 16 pairs, different speakers on 2, and was unsure on 2. Selection deliberately included the eight largest ASR worsenings, four largest improvements, and eight randomly chosen remaining clips. Among those groups, preferences for four/sixteen/tie were respectively 4/4/0, 1/0/3, and 3/3/2. These descriptive counts from one rater and one enriched chapter sample establish neither a preference winner nor quality equivalence.

Listener reports flagged cut-off endings in real pairs 1, 11 and 12: sixteen-worker audio in all three, and four-worker audio also in pair 12. Both arms read a letter rather than the intended stutter sound in pair 2, according to the rater. Rushed delivery was reported for four-worker audio in pair 17 and sixteen-worker audio in pair 21. These are listener reports; their cause has not been verified. The deliberately truncated controls, pairs 13 and 23, are excluded from TTS defect claims. No production setting is promoted.

## Existing-voice seed retest

Four voices previously rated 1/5 were retested on the exact original held-out line, alongside two previously clean controls, at generation seeds 20260930 and 20261001. This used the original honest-retrain checkpoints, verified against their recorded hashes; **no training was performed**. Twelve main renders and four same-text foreign-voice controls completed, and every downloaded 24 kHz mono WAV hash, duration and finite nonzero samples was checked.

One `husky_baritone_20s_m_anime` render lasted **163.76 seconds**, versus **10.40 seconds** at the other new seed. This duration anomaly was subsequently reproduced byte-for-byte and reported as garbled/runaway audio by the listener; see the completed follow-up below. Pitch was measured using pYIN with 100/200 Hz tone controls and a silence rejection control. For the three previously reported wrong-voice pairs:

| Adapter | Human median f0 | New seed 20260930 | New seed 20261001 |
|---|---:|---:|---:|
| warm_tenor_20s_m | 159 Hz | 104 Hz | 108 Hz |
| warm_baritone_40s_m_fantasy | 93 Hz | 201 Hz | 197 Hz |
| breathy_tenor_18s_m_supernatural | 293 Hz | 124 Hz | 156 Hz |

These measurements describe pitch differences. They do not establish perceived sex, audible cracking, the cause of voice mismatch, or acceptable quality. An 18-pair private blinded package is prepared (12 main pairs, four foreign-voice controls, two identical-human controls); **listening is now scored**, with one missing similarity rating retained explicitly; see [completed Chinese and voice follow-ups](thunder-completed-followups-2026-09-30.md). Goals 6.5 and 7.1 gain the completed chapter listening comparison; their broader targets remain open.

- [Aggregate listening/retest verification](../../ab_test_runtime/experiments/thunder_listening_followup_verification__20260930.json)
- [Portable follow-up verifier](../../ab_test_runtime/evidence/thunder_results_20260930/verify_listening_followup.py), using private `--raw-root` and aggregate `--out` arguments.

Raw notes, transcripts, keys, audio and weights stay private outside Git. No Chunagon material was used.
