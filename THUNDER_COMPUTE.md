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

At **$0.35/hr for an A6000** (price checked 2026-07-18), cost is rarely the deciding factor for a few
hours of work. The deciding factors are **VRAM**, **CUDA**, and **wall-clock**.

---

## 2. Use Thunder for

| Reason | Why local fails | Suggested GPU |
|---|---|---|
| **Model needs > 16 GB VRAM** | 9070 XT caps you at ~16 GB. Bigger models or long context won't load. | A6000 (48 GB), $0.35/hr |
| **CUDA-only code** | Your card is ROCm. Anything that assumes CUDA kernels (some `xformers`/`triton`/`sageattention`/`flash-attn` builds, most upstream ML wheels) won't run. | A6000 / L40 |
| **You need your desktop back** | Local LLM work pins the GPU; the global GPU lock blocks every other task in the app. | A6000 |
| **Persona generation with the measured Gemma endpoint** | The A100 completed the production discovery/compile cohort 2.27× faster. | A100 80 GB |
| **A workload that has actually OOMed locally** | The measured A100 was slower for every tested TTS and Voice Lab stage, including LoRA training. Use it only when capacity, not assumed speed, is the blocker. | Size for the observed VRAM need |

## 3. Do NOT use Thunder for

- **CPU-only work.** Audacity, M4B, and Voice Lab naming were measured on both
  machines; local was faster in all three cases.
- **Short or interactive jobs.** Setup overhead (create → SSH → load model →
  forward port) dwarfs a few minutes of inference.
- **Chasing parallelism.** See §4 — this is the big one.
- **Anything you haven't confirmed is actually GPU-bound.** Check first; don't
  rent a GPU to fix an I/O or LLM-latency problem.

---

## 4. Thunder buys VRAM and CUDA — speed must be measured

This is measured, not theoretical, and it's the most counter-intuitive thing
here:

> In a historical Thunder **A6000** run whose raw report is not versioned,
> review with `concurrency`/`parallel` **> 1 was
> slower** than 1 — and **crashed at 4**. Keep concurrency at **1**. Don't
> auto-scale `parallel`.

Locally, `lmstudio_settings.IDEAL_SETTINGS` deliberately pins `parallel: 1` for
VRAM safety — that's an intentional trade-off (Rule 9), not a bug to "fix" by
renting a GPU. So going remote does **not** unlock concurrent requests. If you
rent an A6000 expecting 4× throughput from parallelism, you will pay 4× and get
less than 1×.

Rent Thunder for **VRAM headroom and CUDA**. Do not assume either per-token
speed or fan-out improves without measuring the exact model/runtime.

The bottleneck depends on the full software path, not GPU specifications alone.
The A100 lost the measured LoRA-training calibration despite training being
compute-heavy. The benchmark did not profile the cause; possible contributors
include setup, data, small-batch, kernel, and framework overhead. Do not promote a theoretical compute advantage into a
placement rule without a production-shaped measurement.

### Measured placement matrix — 2026-07-18

These are fixture-scale calibration results, not promises for every model or book.
They do answer the placement question for the tested RX 9070 XT/ROCm and A100
80 GB/CUDA environments. The canonical machine-readable summary is
[`docs/benchmarks/thunder_2026-07-18.json`](docs/benchmarks/thunder_2026-07-18.json).
Pending entries remain explicit rather than being inferred from a component.

| Workload | Local | A100 Thunder | Measured decision |
|---|---:|---:|---|
| Script generation, 9 calls | 366.6 s, 5/9 pass | 677.8 s, 7/9 pass at 98k context | Local for throughput; quality remains stochastic |
| Script review, 9 batches | 243.8 s | 501.1 s | Local, about 2.06× faster |
| Persona discovery + compile | 39.885 s | 17.557 s | **Thunder, 2.27× faster** |
| Nickname detection | 0.796 s | 1.782 s | Local, 2.24× faster |
| CustomVoice warm throughput | 1.18× realtime | 0.28× realtime | Local, about 4.2× higher throughput |
| CustomVoice native batch, cap 8 | 87.3 s | 210.6 s | Local, 2.41× faster |
| Base clone, six generations | 44.8 s | 228.3 s | Local, 5.09× faster |
| Clone native batch, cap 8 | 37.3 s | 107.8 s | Local, 2.89× faster |
| VoiceDesign, six generations | 40.7 s | 173.9 s | Local, 4.27× faster |
| Dataset Builder production batch, 2 samples | 16.095 s, 2/2 complete | Pending; retry result was not captured | No cross-machine decision yet |
| LoRA TTS warm generation | 5.48 s | 32.05 s | Local, 5.85× faster |
| LoRA training, 8 samples / 1 epoch | 2.1 s | 6.5 s | Local, 3.10× faster |
| Voice Lab preparer ASR | 11.24 s | 21.72 s | Local, 1.93× faster |
| Voice Lab dedup | 10.19 s | 19.09 s | Local, 1.87× faster |
| Voice Lab profiling, cold | 6.936 s | 78.689 s | Local, 11.35× faster |
| Voice Lab naming | 0.0216 s | 0.0541 s | Local, 2.50× faster |
| Audacity export | 0.040 s | 0.063 s | Local, 1.58× faster |
| M4B export | 0.167 s | 0.247 s | Local, 1.48× faster |

Quality checks passed on both machines for the completed cohorts: valid audio,
hash-verified inputs, structural script/review gates, exact canonical Voice Lab
content where applicable, complete persona/evidence coverage, exact nickname
mapping, valid Audacity contents, and parseable AAC/chapter M4B output. Cross-
backend audio and compressed-container hashes are not expected to be identical.

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
workload justifies it. Do not extrapolate these decode results to TTS, training,
or Voice Lab; their separate measured results are recorded below.

The current protected `REMOTE_IDEAL_SETTINGS` still configures `parallel: 2`,
and remote self-healing can restore that value. The measured parallel-1 result
is advisory until that safety-sensitive production setting is deliberately
changed and validated; this guide does not silently override it.

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
that result was measured separately below.

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
stacks.

The native clone batch path was then swept with the same 16 deterministic
mixed-length inputs used for CustomVoice:

| Batch cap | RX 9070 XT time / realtime / peak | A100 time / realtime / peak |
|---:|---:|---:|
| 2 | 80.5 s / 1.91× / 6.13 GB | 322.3 s / 0.47× / 5.72 GB |
| 4 | 45.5 s / 3.45× / 7.17 GB | 183.7 s / 0.84× / 7.21 GB |
| 8 | **37.3 s / 4.27× / 9.25 GB** | **107.8 s / 1.43× / 10.17 GB** |
| 16 | 40.1 s / 3.88× / 11.84 GB | 153.5 s / 1.02× / 13.87 GB |

All 128 outputs across both machines and four caps completed successfully.
Cap 8 is the measured optimum on both GPUs. At that shared optimum, local was
**2.89× faster by equal-input wall time**. Cap 16 regressed because the final
three long chunks formed a slower second sub-batch; the A100 still had roughly
67 GB unused, so extra capacity was not the answer.

#### VoiceDesign previews

The production `generate_voice_design` call was measured with three distinct
voice descriptions and short/medium/long target texts, two fixed-seed
repetitions each, and a 1,024-token cap. The benchmark includes the real preview
WAV creation and moves that file into the benchmark output directory.

| Environment | Passed | Cached model load | Six generations | Aggregate audio throughput |
|---|---:|---:|---:|---:|
| RX 9070 XT local | 6/6 | 6.07 s | 40.7 s | 1.15× realtime |
| A100 80 GB Thunder | 6/6 | 24.21 s | 173.9 s | 0.27× realtime |

Local VoiceDesign generation was **4.27× faster by equal-input wall time**.
All outputs were valid, non-silent, and unclipped. Local repetitions were
bit-identical. One A100 medium repetition differed by tiny sample-level values
despite the fixed seed, while its duration and health metrics were effectively
identical; do not treat CUDA VoiceDesign output as guaranteed bit-deterministic.
Keep VoiceDesign previews and designed-voice chunk generation local with these
software stacks.

#### LoRA TTS inference

The production LoRA path used the same hash-verified Watson adapter, seed, and
prompt on both machines. It separately measured model/adapter load, reusable
prompt construction, and warm generation.

| Phase | RX 9070 XT | A100 Thunder |
|---|---:|---:|
| Model + adapter load | 4.54 s | 23.43 s |
| Prompt build | 0.86 s | 7.02 s |
| Warm generation | 5.48 s | 32.05 s |

Local warm generation was **5.85× faster**. Both environments produced healthy,
deterministic outputs across repetitions. Keep LoRA inference local with this
adapter/model stack.

### Persona and nickname LLM utilities

The advanced persona benchmark intentionally captured the preview boundary
instead of generating TTS again. Each target performed one discovery call and
two compilation calls over the same two-speaker fixture. Thunder completed in
17.557 s versus 39.885 s locally: **Thunder was 2.27× faster**, with 100% speaker
and evidence coverage on both. This was the A100's only measured wall-clock speed win.

Nickname detection used one explicit contextual `BETTY → BEATRICE` case. Local
completed in 0.796 s versus 1.782 s remotely. Both achieved precision 1.0,
recall 1.0, and evidence coverage 1.0. Keep short nickname jobs local; consider
Thunder for larger persona discovery/compile cohorts after measuring them.

### CPU exports

Audacity and M4B were run on Thunder instead of being dismissed from GPU specs.
Local still won: 0.040 s versus 0.063 s for Audacity, and 0.167 s versus 0.247 s
for the strengthened M4B rerun. Audacity member/label structure matched exactly.
Both M4Bs contained AAC audio and exactly two positive-duration, titled chapters. Binary
hashes differed because ZIP timestamps and FFmpeg versions are platform-specific.

---

## Case study: Voice Lab — measured components by stage

The listed Voice Lab components ran locally and on the same A100 80 GB instance
through production-backed, hash-verified fixtures. The preparer result covers
only its ASR phase; alignment and annotation were not benchmarked.

| Stage | Local | A100 Thunder | Quality comparison |
|---|---:|---:|---|
| Preparer ASR, 36.08 s audio | 11.24 s | 21.72 s | Same 91-word normalized transcript; alignment floats differed |
| Dedup, two volumes / 8 samples | 10.19 s | 19.09 s | Same cluster and identical canonical merged content |
| LoRA training, 8 samples / 1 epoch | 2.1 s | 6.5 s | Zero OOM skips; both adapters hash-verified |
| Profiling, cold Qwen2.5 14B Q6 | 6.936 s | 78.689 s | Acoustic metrics identical; both casting lines valid |
| Naming, three collision cases | 0.0216 s | 0.0541 s | Byte-identical manifests and identical directory names |

The measured conclusion is stronger than the old estimate: **keep the tested
Voice Lab components local for speed**, not merely because remote setup is
awkward. The bounded LoRA training calibration was 3.10× faster locally, and
profiling was 11.35× faster locally. Transfer and provisioning overhead were
excluded from those timed case results, so including them only strengthens the
local recommendation.

This does not prove that every future large training job is faster locally. It
does prove that GPU specifications alone were a bad predictor for this stack.
If a substantially larger dataset OOMs on the 16 GB card, Thunder remains a
capacity escape hatch; benchmark that new dataset shape before treating the A100
as a throughput upgrade.

Production Voice Lab jobs launched from the UI remain local-only. The SSH
benchmark adapters are calibration infrastructure, not production remote-job
routing. Using Thunder as a capacity escape hatch currently requires a manual
workflow; automatic Voice Lab dispatch to Thunder has not been built.

### Profiling phase breakdown

The cold profiling result combined two very different workloads:

| Phase | RX 9070 XT | A100 Thunder |
|---|---:|---:|
| GGUF model load | 4.246 s | 57.344 s |
| Librosa acoustics | 1.307 s | 16.696 s |
| llama.cpp description | 1.381 s | 4.642 s |

The exact 12.1 GB GGUF had matching SHA-256 on both machines. Seeded text can
still differ slightly across ROCm/CUDA backends; acoustic values matched exactly.

---

## 5. Picking a GPU (pricing checked 2026-07-18, per hour)

| GPU | $/hr | Use when |
|---|---|---|
| **a6000_x1** | **0.35** | Cheapest capacity/CUDA escape hatch. Historical review concurrency above 1 regressed; do not assume speed. |
| l40_x1 | 0.79 | Only if you've measured it beating the A6000 for your job. |
| l40s | 0.99 | Same — measure first. |
| a100xl_x1 | 1.09 | 80 GB capacity and CUDA. In this campaign its only wall-clock win was persona generation; it lost the measured LoRA-training calibration. |
| h100_x1 | 2.19 | Unmeasured in this project. Rent only for a workload that justifies a new calibration. |
| *_x2 / _x4 / _x8 | 2–23 | **Not for this project.** See §4 — you can't use the parallelism. |

Storage: disk $0.0003/GB/hr, snapshots $0.00006849/GB/hr (snapshots are ~4×
cheaper than a running disk and ~1000× cheaper than a running A6000).
Check Thunder's current [pricing](https://www.thundercompute.com/pricing) and
[billing documentation](https://www.thundercompute.com/docs/billing) before renting.

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
- **The CUDA llama.cpp wheel needs NVIDIA library paths.** Thunder used
  `llama-cpp-python==0.3.23` from the official CUDA 12.4 wheel index. Importing
  it directly initially failed because `libcudart.so.12` was under the Python
  environment's `site-packages/nvidia/*/lib` directories rather than the system
  loader path. The persistent `/home/ubuntu/ttsenv/bin/python-cuda` wrapper
  discovers those directories, prepends them to `LD_LIBRARY_PATH`, and then
  executes `/home/ubuntu/ttsenv/bin/python`.
- **Verify the actual GPU path, not just import success.** The wrapper was tested
  with real GGUF inference: llama.cpp detected CUDA, offloaded the model to the
  A100, and generated tokens. The profiling benchmark then exercised the same
  wrapper end to end.
- **Use the same model bytes.** The profiling comparison used
  `Qwen2.5-14B-Instruct-Q6_K.gguf` on both machines, SHA-256
  `18cd6b7d5feb00c57ff81ede8f2164ffd86be90dbee9c05bf09ded1ab179740d`.
  Thunder also had a different Gemma GGUF; it was not substituted for the fair
  profiling comparison.
- **M4B requires FFmpeg/FFprobe.** The A100 image did not include FFmpeg. The
  export comparison installed Ubuntu FFmpeg 4.4.2. Preflight this dependency on
  a fresh instance instead of discovering it after staging audio.

---

## 8. Quick checklist

Before renting:

- [ ] Is this actually GPU-bound? (Exports were measured on both machines and
      local won; use Thunder only if the remote placement serves another need.)
- [ ] Does it need >16 GB VRAM or CUDA? (If no → **probably stay local**.)
- [ ] Is the local run long enough that hours matter? (If no → **stay local**.)
- [ ] Am I expecting a parallelism win? (If yes → re-read §4 and measure the
      exact endpoint; the A100/Gemma optimum was parallel 1.)

After the job:

- [ ] Snapshot if state matters.
- [ ] **Delete the instance.**
- [ ] `list_instances` → confirm empty.
- [ ] Set `llm_mode` back to `local`.
