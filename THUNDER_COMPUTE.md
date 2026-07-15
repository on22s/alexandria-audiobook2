# When to use Thunder Compute (and what for)

A decision guide for this project. **Default is local** — Thunder is the
exception, not the baseline.

Your hardware: **AMD Radeon RX 9070 XT** (16 GB, RDNA4/ROCm) running LM Studio
at `localhost:1234`. Current `config.json`: `llm_mode: local`, no remote
configured. As of writing, **no Thunder instances exist** (nothing billing).

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

## 4. Thunder buys you a *bigger, faster single stream* — not throughput

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

Rent Thunder for **VRAM headroom, CUDA, and per-token speed**. Not for fan-out.

---

## 5. Picking a GPU (real pricing, per hour)

| GPU | $/hr | Use when |
|---|---|---|
| **a6000_x1** | **0.35** | **Default choice.** 48 GB, cheapest, benchmarked for this project's review workload. |
| l40_x1 | 0.79 | Only if you've measured it beating the A6000 for your job. |
| l40s | 0.99 | Same — measure first. |
| a100xl_x1 | 1.09 | Big training runs that genuinely need it. |
| h100_x1 | 2.19 | Rarely justified here. |
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
