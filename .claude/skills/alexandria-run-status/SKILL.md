---
name: alexandria-run-status
description: Report status and ETA for a long-running preparer/batch corpus build in this project (Alexandria audiobook dataset prep). Triggers when the user asks "check on the run", "how's it going", "what's the status", "any progress", "how long left", "ETA", "is it done yet", or any variation while a multi-day preparer/batch job is in flight. Use this instead of re-deriving the inspection commands from scratch — it encodes the tmux/watchdog/dataset_temp inspection pattern, the WAV-wrap-aware true-duration formula, and the per-book chunk math.
---

# Alexandria run status

The user runs `alexandria_batch_processor.py` (or `run_subset.sh` driving `run_with_restart.sh`) on long batches that can run for days. Use this skill to produce a tight status + ETA in one message, without re-deriving the inspection commands every session.

## Inspection sequence (run these in parallel)

1. **tmux session alive?** — `tmux has-session -t prep 2>&1`
   - Session name is usually `prep` for the subset run; `batch` if started by the full `build_test_corpus.sh`. If neither, ask.
2. **Watchdog alive?** — `ps -p $(pgrep -f watch_subset.sh) -o pid,etime,cmd 2>&1` (or `pgrep -f build_test_corpus`)
3. **Watchdog log** — `cat test_corpus_output/watchdog.log` for start time and any completion records
4. **Sentinel** — `ls test_corpus_output/{DONE.flag,ABORTED.flag} 2>&1` — if `DONE.flag` exists the run completed; if `ABORTED.flag` the wrapper was stopped early
5. **In-flight scratch state** — `ls test_corpus_output/*.zip 2>&1 | wc -l` for finished books, `ls dataset_temp/ | tail -5` for current book's sample count
6. **Live progress** — `tmux capture-pane -t prep -p -S -120 | tail -120` and grep for `Progress: \d+/\d+ chunks | Avg: \S+ | Elapsed:` to find the latest annotation-segment line. That gives current/total/avg-per-chunk/elapsed/internal-ETA for the current book.

## True audio duration (WAV-wrap aware)

`soundfile`, `librosa.load`, and even `ffprobe -show_entries stream=duration` are all fooled by the 32-bit WAV data-chunk-size field wrapping at 4 GiB. **Don't trust any tool's reported duration on these files.** Compute it directly from filesize for 16-bit stereo 44.1 kHz PCM:

```
true_seconds ≈ filesize_bytes / 176400         # 44100 × 2 channels × 2 bytes
```

(176400 = byte rate for s16le stereo @ 44.1k. Change if the WAV is mono or a different rate, but the project's converted WAVs are all stereo 44.1k.)

The preparer's `_wav_overflow_info()` at `alexandria_preparer_rocm_compatible.py:243` does the same calculation; if it's already logged the "implies XXXXs (Y hr)" line, use that number directly.

## Chunks-per-hour rate

From the in-flight run on Spice and Wolf 10 (6.80 hr audiobook → 5,618 annotation chunks):

- **~826 annotation chunks per hour of audio**
- **~12–13 s per chunk on RX 7900 XTX** with the current Qwen 2.5 14B Q6_K LLM (CPU-LLM bound, GPU mostly idle)

Per-book wall = `chunks × seconds_per_chunk`. Cross-book this is roughly linear with audio duration.

## Current subset (May 2026 run)

Encoded for quick reference — confirm against `run_subset.sh` if it's been edited:

| # | Book | True audio | ~Chunks | ~Wall @12.5 s/ch |
|---|---|---:|---:|---:|
| 1 | J Michael Tatum — Spice and Wolf Vol. 10 | 6.80 hr | 5,618 | ~19.5 hr |
| 2 | Cliff Kurt — Mushoku Tensei Vol. 01 | 7.29 hr | ~6,020 | ~20.9 hr |
| 3 | Cherami Leigh — Cyberpunk 2077 | 14.06 hr | ~11,610 | ~40.3 hr |
| 4 | Michael Kramer — The Hero of Ages | 27.42 hr | ~22,650 | ~78.6 hr |

Total ~159 hr ≈ **6.5 days from book-1 start**. Hero of Ages alone is ~half the wall clock.

## Report format

Keep it tight. One short paragraph + a 3–4 row table, no more. Useful elements:

- Session/watchdog alive (one line)
- Current book of N, chunk progress + percent, per-chunk avg, elapsed
- Remaining wall for current book + total remaining across all books
- Calendar ETA in user-local time (today is in the auto-memory; reference it, don't ask)
- Optional: any anomalies (chunk rate slowing, GPU spike, watchdog gone)

End with one sentence asking if they want to drop any books to shorten the wait, **only** if the remaining wall is > 2 days. Otherwise just report.

## Anti-patterns

- **Don't** use `ffprobe` / `soundfile.info()` for these WAV durations. They lie. Use the filesize formula.
- **Don't** re-read the full `alexandria_preparer_rocm_compatible.py` to find progress patterns — `tmux capture-pane` is the source of truth for live state, the python file is irrelevant for status queries.
- **Don't** grep the full log file in `logs/` for progress — `tmux capture-pane -p -S -120` is faster and cheaper than reading a multi-MB log.
- **Don't** sleep/poll waiting for completion. Watchdog (`watch_subset.sh`) writes `DONE.flag` and fires `notify-send` when the tmux session exits. If the user wants to be notified, they already have it.
