# When to use Thunder Compute (and what for)

A decision guide for this project. **Default is local** — Thunder is the
exception, not the baseline.

Your hardware: **AMD Radeon RX 9070 XT** (16 GB, RDNA4/ROCm) running LM Studio
at `localhost:1234`. The app defaults to `llm_mode: local`; a Thunder remote
profile may also be configured. Always check `tnr status` before assuming
nothing is billing.

---

## 1. The one-line rule

> Use Thunder when the job **can't run locally at all**, or when it would run
> so long locally that the wall-clock matters more than the dollars.
> Everything else: stay local.

At **$0.35/hr for an A6000**, cost is rarely the deciding factor for a few
hours of work. The deciding factors are **VRAM**, **CUDA**, and **wall-clock**.

---

## 2. Use Thunder for

| Reason | Why local fails | Suggested GPU |
|---|---|---|
| **Model needs > 16 GB VRAM** | 9070 XT caps you at ~16 GB. Bigger models or long context won't load. | A6000 (48 GB), $0.35/hr |
| **CUDA-only code** | Your card is ROCm. Anything that assumes CUDA kernels (some `xformers`/`triton`/`sageattention`/`flash-attn` builds, most upstream ML wheels) won't run. | A6000 / L40 |
| **Multi-day batch review or script-gen** | A 6-volume Re:Zero-scale batch on one local GPU is a very long single stream. | A6000 |
| **You need your desktop back** | Local LLM work pins the GPU; the global GPU lock blocks every other task in the app. | A6000 |
| **LoRA training that OOMs or crawls locally** | Training wants headroom the 16 GB card doesn't have. Note: needs a **CUDA** torch env, not the sibling repo's ROCm one. | A6000, L40 if faster |

## 3. Do NOT use Thunder for

- **CPU-only work.** `audacity_export` and `m4b_export` are in `NON_GPU_TASKS`
  for a reason — they get nothing from a rented GPU.
- **Short or interactive jobs.** Setup overhead (create → SSH → load model →
  forward port) dwarfs a few minutes of inference.
- **Chasing parallelism.** See §4 — this is the big one.
- **Anything you haven't confirmed is actually GPU-bound.** Check first; don't
  rent a GPU to fix an I/O or LLM-latency problem.

---

## 4. Thunder buys VRAM and CUDA — speed must be measured

This is measured, not theoretical, and it's the most counter-intuitive thing
here:

> On the Thunder **A6000**, review with `concurrency`/`parallel` **> 1 was
> slower** than 1 — and **crashed at 4**. Keep concurrency at **1**. Don't
> auto-scale `parallel`.

Locally, `lmstudio_settings.IDEAL_SETTINGS` deliberately pins `parallel: 1` for
VRAM safety — that's an intentional trade-off (Rule 9), not a bug to "fix" by
renting a GPU. So going remote does **not** unlock concurrent requests. If you
rent an A6000 expecting 4× throughput from parallelism, you will pay 4× and get
less than 1×.

Rent Thunder for **VRAM headroom and CUDA**. Do not assume either per-token
speed or fan-out improves without measuring the exact model/runtime.

Note the bottleneck depends on the workload: LLM **review/script-gen is decode,
which is memory-bandwidth-bound** (so a faster GPU barely helps per-token, and
parallelism hurts — above). **LoRA training is compute-bound** (matmul/backprop),
which is the one place raw GPU throughput genuinely buys speed. Don't apply the
"faster GPU barely helps" lesson to training — it's a different bottleneck.

### A100 script-generation benchmark — 2026-07-18

This is a small controlled cohort, not a universal GPU ranking. The same Gemma
4 E4B Q8 model, three historical ~6,000-character chunks, three repetitions
each, and `max_retries: 0` were used on both machines. Zero retries measures
raw single-attempt reliability; it is not the production completion rate.

| Environment | Passed | Total time | Mean successful call | Aggregate completion rate |
|---|---:|---:|---:|---:|
| RX 9070 XT, 32,768 context, parallel 2 | 5/9 | 366.6 s | 50.1 s | 61.5 tok/s |
| A100 80 GB, 98,304 context, parallel 2 | 7/9 | 677.8 s | 94.0 s | 31.4 tok/s |
| A100 80 GB, 32,768 context, parallel 2 | 6/9 | 1,032.7 s | 105.3 s | 20.6 tok/s |

Failures were mostly stochastic early stops or structurally invalid output;
the quality gates correctly rejected incomplete scripts. Reducing Thunder's
context did **not** fix its single-stream speed, so the 98k KV allocation was
not the demonstrated bottleneck.

A production-shaped three-trial concurrency sweep at 32,768 context found:

| LM Studio parallel / simultaneous requests | Median aggregate completion rate |
|---|---:|
| 1 / 1 | 43.5 tok/s |
| 2 / 2 | 17.9 tok/s |

Parallel 2 was 59% slower, so the safety policy stopped before 4/8/16. For
this exact A100/Gemma/LM Studio combination, **parallel 1 is the measured
optimum**. The practical recommendation is to keep Gemma script generation on
the local RX 9070 XT; use this A100 only when VRAM/CUDA or a different measured
workload justifies it. TTS and Voice Lab require their own benchmarks—do not
extrapolate these decode results to training.

### A100 script-review benchmark — 2026-07-18

The production `review_batch` path was measured on three immutable 25-entry
batches from Volumes 3, 9, and 10, with three repetitions and zero retries.
Both environments used the same model and 32,768 context; Thunder used its
measured parallel-1 optimum while local retained its parallel-2 configuration.

| Environment | Passed | Total time | Mean batch | Aggregate completion rate | Mean word ratio |
|---|---:|---:|---:|---:|---:|
| RX 9070 XT local | 9/9 | 243.8 s | 27.1 s | 62.0 tok/s | 1.0002 |
| A100 80 GB Thunder | 9/9 | 501.1 s | 55.7 s | 31.0 tok/s | 0.9973 |

Both preserved structure and stayed within the production 95–105% text-loss
gate. Thunder provided no reliability advantage in this cohort and was almost
exactly 2× slower. **Keep Gemma script review local** for this model/runtime.
This reinforces the script-generation result but still says nothing about
LoRA training performance.

### A100 CustomVoice TTS benchmark — 2026-07-18

The production `TTSEngine.generate_custom_voice` path was measured with the
same three short/medium/long narration fixtures, fixed seeds, 1,024 generated
token cap, and two repetitions per fixture. Model loading and generation were
timed separately. Both environments lacked `flash-attn` and used the manual
PyTorch attention path; local used Torch 2.10.0+ROCm 7.0 and Thunder used Torch
2.7.0+CUDA 12.6. LM Studio remained resident on both machines.

| Environment | Passed | Cached model load | Warm audio throughput | Clipped outputs |
|---|---:|---:|---:|---:|
| RX 9070 XT local | 6/6 | 6.2 s | 1.18 audio-sec/wall-sec | 0/6 |
| A100 80 GB Thunder | 6/6 | 21.8 s | 0.28 audio-sec/wall-sec | 0/6 |

The fixed seed produced identical hashes across repetitions on each machine;
cross-backend hashes and durations differed slightly, which is expected from
different Torch/GPU kernels. All WAVs were valid 24 kHz mono PCM, non-silent,
and unclipped.

For this exact serial CustomVoice path, the local RX 9070 XT delivered about
**4.2× more audio per wall-clock second** and was already faster than realtime;
the A100 ran at only about 15% utilization during observation. The A100's large
VRAM capacity therefore did not translate into single-stream speed. Keep serial
CustomVoice generation local with this software stack. Native list batching
was measured separately below. LoRA training remains a separate cohort, so
this result must not be extrapolated to it.

#### CustomVoice native-batch capacity

The production `_local_batch_custom` path was swept with 16 identical
mixed-length source chunks on each machine. Caps above 8 did not help: at 16,
length-ratio splitting still produced two sub-batches and autoregressive
padding reduced throughput. The initial capacity sweep found:

| Batch cap | RX 9070 XT realtime | A100 realtime | A100 failures |
|---:|---:|---:|---:|
| 2 | 1.71× | 0.47× | 0/16 |
| 4 | 3.12× | 0.86× | 0/16 |
| 8 | 4.93× | 1.32× | 0/16 |
| 16 | 4.03× | 0.89× | 0/16 |

That sweep exposed two measurement defects: the tool reported only the final
sub-batch's VRAM peak and did not fix the TTS seed. Both were corrected before
the confirmation run. With `batch_seed=42` and cap 8, local completed all 16
inputs in **87.3s** at 9.64 GB peak; the A100 completed them in **210.6s** at
10.12 GB peak. Output durations differ across ROCm/CUDA kernels even with the
same seed, so equal-input wall time is the primary comparison: local was
**2.41× faster**. The measured optimum is cap 8 on both machines; A100 capacity
did not permit useful scaling beyond it for this mixed-length workload.

#### Base-model voice cloning

The production clone path used the same hash-verified Watson reference audio
and transcript on both machines. Three short/medium/long target texts were run
twice with fixed seeds and a 1,024-token cap. Model loading, reusable clone
prompt construction, and generation were timed separately.

| Environment | Passed | Base load | Prompt build | Six generations | Aggregate audio throughput |
|---|---:|---:|---:|---:|---:|
| RX 9070 XT local | 6/6 | 6.44 s | 0.99 s | 44.8 s | 1.15× realtime |
| A100 80 GB Thunder | 6/6 | 30.14 s | 7.20 s | 228.3 s | 0.25× realtime |

All outputs were valid 24 kHz mono PCM, deterministic within each backend,
non-silent, and unclipped. For equal target inputs, local generation was
**5.09× faster**. Keep serial Base-model cloning local with these Torch/Qwen
stacks. Native clone batching remains a separate measurement.

---

## Case study: Voice Lab — estimated, and keep it local anyway

*(This section is reasoned from specs, **not benchmarked** like §4. Treat the
numbers as order-of-magnitude.)*

Voice Lab (dataset ZIPs → dedup → train → profile → name → named LoRA voices) is
the **clearest keep-local case in the project**, for three structural reasons:

1. **It's a ROCm pipeline; Thunder is CUDA.** Every stage runs locally on the
   9070 XT via the configured `rocm_python` interpreter. Going remote isn't a
   toggle — it's re-provisioning a whole CUDA ML environment.
2. **There is no remote plumbing for it.** The app's Local/Remote switch only
   repoints the *LM Studio LLM endpoint*. Voice Lab stages run as local
   subprocesses under the shared GPU lock — nothing routes them off-box.
3. **Amdahl's law.** Only one of the four stages is the kind of work a faster
   GPU accelerates.

| Stage | Bottleneck | Remote benefit |
|---|---|---|
| **Dedup** | speechbrain embeds (GPU) + umap (CPU) | small — and *pins you local*: `voice_analysis.py` needs speechbrain/umap/matplotlib/seaborn, only in the sibling `alexandria-audiobook.git/app/env`. |
| **Train** | LoRA matmul/backprop — **compute-bound** | the only real win. `batch_train_lora.py` has no top-level third-party imports, so it's the one portable stage. |
| **Profile** | `llama_cpp` decode (**bandwidth-bound**) + acoustic feats (CPU) | small — needs the local Qwen GGUF; same "decode barely speeds up" lesson as §4. |
| **Name** | pure stdlib | none — already instant. |

**How much would it actually benefit?**

- **Train on an A100:** ~2–3× the local card on paper. But LoRA training is
  small-batch with heavy per-step/data-loading overhead that caps *any* GPU well
  below peak — so realistically expect **2–4×**, and an **H100 would be only
  marginally better than the A100** because that same overhead eats its extra
  peak throughput. **With H100s sold out, the A100 is the right pick and you
  lose very little.** (An A6000 would give almost nothing on Train.)
- **A100 also helps Profile** more than an A6000 would (~2 TB/s vs ~768 GB/s ≈
  ~3× local bandwidth vs "barely faster"), but Profile is a minor slice.
- **End-to-end**, even a 3× Train win is diluted by the CPU/bandwidth-bound
  stages to roughly **1.3–1.5×** — *before* subtracting overhead.

**Why it's still net-negative for normal runs:** the overhead is unchanged by
which GPU you rent — (a) shuttling GB of audio ZIPs up and adapters back, (b)
rebuilding dedup's speechbrain/umap + profile's `llama_cpp` on CUDA per
instance, (c) billing the whole setup+transfer time. For a small or mixed run
that overhead **exceeds** the ~1.3–1.5× saved.

**The one scenario where it pays off:** a **large standalone Train batch** —
many narrators, dedup/profile already done locally, datasets shipped once. Then
you skip the local-pinned stages, and the 2–4× lands on the part that dominates.

**Before building any of this:** the cheap experiment is to time **one
narrator's Train stage** locally vs. on a rented A100. That single number pins
the real speedup; everything else is transfer/setup you can measure directly.
Don't build the remote path on the estimates above — measure Train first.

---

## 5. Picking a GPU (real pricing, per hour)

| GPU | $/hr | Use when |
|---|---|---|
| **a6000_x1** | **0.35** | **Default choice.** 48 GB, cheapest, benchmarked for this project's review workload. |
| l40_x1 | 0.79 | Only if you've measured it beating the A6000 for your job. |
| l40s | 0.99 | Same — measure first. |
| a100xl_x1 | 1.09 | **Best pick for a big LoRA-training batch** — compute-bound, ~2 TB/s, and in practice close to an H100 once small-batch overhead is factored in (see Voice Lab case study). |
| h100_x1 | 2.19 | Marginally faster than the A100 for training here (overhead eats its extra peak). Often sold out — the A100 is the practical substitute. |
| *_x2 / _x4 / _x8 | 2–23 | **Not for this project.** See §4 — you can't use the parallelism. |

Storage: disk $0.0003/GB/hr, snapshots $0.00006849/GB/hr (snapshots are ~4×
cheaper than a running disk and ~1000× cheaper than a running A6000).

---

## 6. Cost model — the part that bites

**There is no idle auto-stop and no stop/pause. Billing ends only when the
instance is DELETED.**

An A6000 you forget about:

| Forgotten for | Cost |
|---|---|
| Overnight (12 h) | $4.20 |
| A weekend | $16.80 |
| A month | **$252** |

The workflow that follows from that:

1. **Snapshot before deleting** to preserve state — snapshot storage is
   effectively free next to a running instance.
2. **Delete the instance** the moment the job is done.
3. **Verify with `list_instances`** — don't assume it's gone.

> **The app cannot help you here.** There is no billing/balance API integration
> anywhere in this codebase. The `confirmIfRemote()` prompt in
> `static/index.html` (before `batch_review` / batch script-gen) is a deliberate
> stand-in: **it warns about cost, it does not compute it.** Nothing will tell
> you an instance is still running. That's on you.

---

## 7. Setup gotchas (each of these has bitten before)

- **LM Studio must bind `0.0.0.0`.** A remote LM Studio that reports "running"
  is still unreachable through the forwarding URL without
  `--bind 0.0.0.0`. This looks like a network bug and isn't.
- **SSH output has a decorative banner.** Parse the **last non-empty line**;
  never assume clean stdout (see `lmstudio_settings.get_remote_lmstudio_status`).
- **Multi-statement SSH must be ONE pre-quoted argv element** —
  `"bash -lc " + shlex.quote(cmd)`. Passing `"bash", "-lc", cmd` separately lets
  `ssh` re-join them with bare spaces and breaks `;`-separated commands.
- **Cached `concurrency` goes stale.** After a model or endpoint change, check
  `concurrency_for` matches the current `base_url::model_name` before trusting
  the cached number in `llm_remote`.
- **Port forwarding** follows `https://<uuid>-<port>.thundercompute.net`.
- **One source of "is remote".** Use `lmstudio_settings.is_remote_llm` — never
  re-derive it (Rule 15; this already caused a real drift bug).

---

## 8. Quick checklist

Before renting:

- [ ] Is this actually GPU-bound? (If it's an export → **stop, stay local**.)
- [ ] Does it need >16 GB VRAM or CUDA? (If no → **probably stay local**.)
- [ ] Is the local run long enough that hours matter? (If no → **stay local**.)
- [ ] Am I expecting a parallelism win? (If yes → **re-read §4, I'm not getting one**.)

After the job:

- [ ] Snapshot if state matters.
- [ ] **Delete the instance.**
- [ ] `list_instances` → confirm empty.
- [ ] Set `llm_mode` back to `local`.
