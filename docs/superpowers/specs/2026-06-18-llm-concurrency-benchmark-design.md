# LLM concurrency benchmark + wave-based review pipeline

*2026-06-18*

## Problem

Real-world measurement on this project's batch review (`app/review_script.py`,
`app/find_nicknames.py`) showed a Thunder-hosted A6000 running ~3x slower per
batch than a recent local run, for the identical book. Direct benchmarking
ruled out context length and network latency as the cause; the real issue is
that the pipeline sends LLM requests **one at a time**, even though the LM
Studio server can serve multiple requests concurrently (`--parallel N`).
Single-request decoding leaves a large GPU mostly idle (~20% utilization
measured). Concurrency is the lever that actually uses the GPU you're paying
for (Thunder) or already own (local) — not faster single-request latency,
which is close to a hard floor set by the model/hardware.

## Goals

1. A benchmark tool that empirically finds the concurrency level (number of
   simultaneous in-flight requests) where throughput stops improving, for
   whatever endpoint/model is currently configured (local or remote).
2. Change `review_script.py` and `find_nicknames.py` to actually use that
   concurrency, instead of processing batches/chunks sequentially.
3. Applies to both the local and remote (Thunder) LLM paths.

## Design

### 1. Benchmark tool — `app/llm_bench.py` (new file)

- `measure_throughput(client, model, concurrency, prompt_tokens=~4000,
  completion_tokens=~1500) -> tokens_per_sec`: fires `concurrency` simultaneous
  chat-completion requests shaped like a real review batch (using a
  representative synthetic prompt sized to match production batches), via
  `concurrent.futures.ThreadPoolExecutor`. Returns aggregate completion
  tokens / wall-clock time for the whole concurrent group.
- `find_optimal_concurrency(client, model, max_concurrency=16) -> int`: sweeps
  concurrency 1, 2, 4, 8, 16 (geometric, to bound benchmark time). Stops
  climbing and returns the previous level once a step either (a) improves
  throughput by less than 10% over the prior step, or (b) any request at that
  level errors or times out (treated as "this level isn't safe here, back
  off"). This is what keeps it safe on a VRAM-constrained local card without
  needing to know the card's specs.
- Has a `if __name__ == "__main__":` CLI entry point (`--base-url`, `--model`,
  `--max-concurrency`) so it doubles as a standalone manual test, not just
  internal plumbing called by the pipeline.

### 2. Pipeline integration — wave-based concurrency

`review_script.py`'s batch loop and `find_nicknames.py`'s chunk loop currently
do `for item in items: process(item)`, sequentially, with each item able to
see the previous item's results (review: `previous_tail`; discovery:
`all_aliases` accumulated so far) for consistency.

This changes to wave-based: take `wave_size` items at a time (`wave_size` =
the benchmarked concurrency), submit them all to a `ThreadPoolExecutor`,
`wait()` for the whole wave to finish, merge each item's results into the
shared state (aliases / corrected entries) in submission order, *then* start
the next wave using that merged state as context. Cross-wave context is
exactly as accurate as today's fully-sequential behavior; only items
processed in the *same* wave lose visibility into each other's results before
their own LLM call is made.

`wave_size` is read once at the start of `main()` (see caching below) and
used for the whole run.

### 3. Caching — `config.json`

`llm_local` and `llm_remote` each gain two fields: `concurrency` (int) and
`concurrency_for` (the `base_url`+`model_name` pair it was measured against,
e.g. `"http://localhost:1234/v1::modelname"`). At the start of
`review_script.py main()` / `find_nicknames.py main()`:

- If `concurrency_for` matches the active `base_url`+`model_name`, reuse
  `concurrency` directly (no benchmark run, no added startup latency).
- Otherwise, run `find_optimal_concurrency` once, then persist the result
  back into `config.json` (mirroring the existing `atomic_json_write` pattern
  already used elsewhere in the app) so subsequent runs reuse it.

A "Re-benchmark" path (calling `find_optimal_concurrency` directly, ignoring
the cache) stays available for whenever settings change underneath the
cached number (e.g. after using the Optimize button to change `--parallel`).

### 4. Safety / non-goals

- Does not touch `check_global_gpu_lock` / `claim_gpu_task` in `app.py`. That
  lock is cross-*task* exclusion (review vs. voicelab vs. training running at
  the same time); this concurrency is *within* one already-running review
  task, an orthogonal axis.
- Does not auto-adjust LM Studio's server-side `--parallel` setting. The
  benchmark measures whatever's already configured and works within it;
  changing `--parallel` itself stays a manual action via the existing
  Optimize button.
- Existing safety nets (retry loop, `gc.collect()`, checkpointing, pause/
  cancel handling) in `review_script.py`'s batch loop are preserved across
  the refactor to wave-based — they apply per-item inside a wave, same as
  they apply per-item today.

## Testing / verification plan

1. **Correctness**: run a known book through the old sequential code path and
   the new wave-based path (wave_size > 1) and confirm equivalent output —
   same or compatible alias detections, same class of corrections, no batch
   silently dropped.
2. **Throughput**: measure tokens-reviewed-per-minute before/after on the
   local endpoint (and on a fresh Thunder instance if one is available) to
   confirm wave-based concurrency actually improves throughput, not just
   that it runs without error.
3. **Safety**: confirm the benchmark backs off gracefully (returns a smaller
   `wave_size`) when a concurrency level causes a request failure, rather
   than crashing or silently using an unsafe level.
