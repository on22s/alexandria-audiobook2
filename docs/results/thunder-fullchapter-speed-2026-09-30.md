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
