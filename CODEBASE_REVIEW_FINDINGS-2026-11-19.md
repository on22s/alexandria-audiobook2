# Code review — `on22s/alexandria-audiobook2`

**Date:** 2026-11-19
**Commit reviewed:** `b6088d3c` (branch `freebuff/b1e177044aa7844e63f55eab`, working tree clean)
**Review type:** deep, read-only, recall-biased code review. No repository code was modified and nothing was executed.

This document is a self-contained review report. It lists six findings (three from a full-file
pass, three from an earlier whole-app pass), the evidence for each, the fix, and what was
explicitly checked and rejected so the next reviewer does not re-open it. It is **not** a patch.

---

## 1. Scope and method

### Files read in full, line by line

| File | Lines | Notes |
|---|---|---|
| `app/tts.py` | 4 102 | `TTSEngine` local/external batch backends, VRAM/OOM handling, timeline helpers |
| `app/project.py` | 4 497 | chunk assembly, batch dispatch, publication, Audacity/M4B export |
| `app/book_state_transaction.py` | 5 665 | journaled book-state transactions, exchange/quarantine protocol |
| `app/routers/script.py` | 5 065 | upload/EPUB/DOCX, three-pass generation, recovery/inject/skip/retry, batch script generation, status/log/manual-LLM/delivery-retry routes |

Plus targeted reads of the code these four files dispatch into, where a finding needed the
callee to be real: `app/pass_quality.py`, `app/three_pass_generate.py`,
`app/synthesis_recovery_context.py`, `app/synthesis_workspace.py`, `app/core.py`,
`app/utils.py`, `app/app.py`, `app/tts_vram_benchmark.py`.

### Explicitly out of scope

- `app/tests/` (≈315k lines), `app/experiments/`, `ab_test_runtime/` evidence and generated
  artifacts. Test code was consulted only to judge whether a suspected defect was already known.
- Runtime behaviour. **Nothing in this review was executed** — there is no `app/env`, FastAPI and
  Torch are not installed in the review environment, so no route, backend, or test was run.
  Every behavioural claim below is established by reading the call graph and the failure paths,
  not by observing a failure.

### Conventions used below

- **Severity** — `Medium`: silently wrong or misleading output on a reachable path.
  `Low`: latent, hardening, or evidence-quality only.
- **Confidence** — how the claim was established. *Mechanism confirmed* means the code path was
  read end to end and the stated consequence follows from it. No finding here rests on a
  reproduction.

### Project rules this review was held to

The repository has an `AGENTS.md` with explicit engineering rules; the relevant ones for reading
these findings are Rule 15 (one source of dispatch per decision, never parallel copies), Rule 16
(verb-first names signal side effects), Rule 19 (measurements are not inferences), Rule 21
(validate the instrument before trusting its readings), and Rule 9 (never weaken a safety net to
make something simpler). Findings are framed against those rules where they apply.

---

## Finding 1 — Manual pass-3 recovery validates against the wrong delivery schema and drops the `direction` block

**Severity:** Medium
**Confidence:** mechanism confirmed
**Area:** `app/routers/script.py:3121-3171`, `app/routers/script.py:3282-3305`,
`app/pass_quality.py:1144-1189`, `app/three_pass_generate.py:603-657, 3410-3460`
**Rule:** 15 (the recovery gate is a second, drifting copy of the pass gate)

### Evidence

`apply_manual_recovery` documents itself as running hand-supplied output through the same gate
the pass uses:

```python
# app/routers/script.py:3121
def apply_manual_recovery(entries, resolution, expected_checkpoint_token=None):
    """Accept hand-supplied output for the failed unit through the SAME gate
    the pass uses ..."""
    ...
    elif failed.get("pass") == "instruct":
        frozen = failed.get("entries") or []
        report = validate_instruct(frozen, entries)          # 3158
        if not report.get("passed"):
            return {"accepted": False, "report": report}
        by_n = {int(e["n"]): e for e in entries}
        annotated = list(checkpoint.get("annotated") or [])
        named = checkpoint.get("named") or []
        for position, index in enumerate(failed.get("indices") or []):
            while len(annotated) <= index:
                annotated.append(None)
            base = named[index] if index < len(named) and isinstance(named[index], dict) else frozen[position]
            annotated[index] = {**base, "instruct": by_n[position]["instruct"]}   # 3168
```

`validate_instruct` is declared with `delivery_direction="off"` (`app/pass_quality.py:1144`) and
enforces mode-specific fields only when the direction is not `off`:

```python
# app/pass_quality.py:1144
def validate_instruct(prior_entries, response_entries, delivery_direction="off"):
    ...
    directed = delivery_direction in ("directed", "action_only")
    for i, (prior, item) in enumerate(zip(prior_entries, ordered), 1):
        if not directed and not is_text(item.get("instruct")):
            add("missing_instruct", i, prior, "Every entry needs a non-empty instruct.")
        if delivery_direction != "off" and item.get("gap") not in GAP_LABELS:
            add("invalid_gap", i, prior, "Every entry needs a gap from the allowed set.")
        ...
        if not is_text(item.get("action")):
            add("missing_action", ...)
        if delivery_direction == "directed" and not is_text(item.get("delivery")):
            add("missing_delivery", ...)
        if "answers" not in item:
            add("invalid_answers", ...)
```

So in `gaps` / `directed` / `action_only` mode, recovery calls the gate with `off` and therefore
skips every requirement that mode imposes. It then binds only `instruct`:

The live pass, by contrast, stores the mode's answer under a `direction` sub-object and derives
`pause_after` from it:

```python
# app/three_pass_generate.py:613
def get_directed_entry(prior, item, delivery_direction):
    entry = {key: value for key, value in prior.items() if key != "instruct_unchecked"}
    if delivery_direction == "off":
        return {**entry, "instruct": item.get("instruct")}
    if delivery_direction == "gaps":
        return {**entry, "instruct": item["instruct"], "direction": {"gap": item["gap"]}}
    return {**entry,
            "instruct": compose_directed_instruct(item["action"], item.get("delivery") or "",
                                                  delivery_direction),
            "direction": {"answers": coerce_index(item.get("answers")),
                          "action": item["action"],
                          "delivery": item.get("delivery"), "gap": item["gap"]}}

# app/three_pass_generate.py:630 — turns a gap label into silence between lines
def apply_gap_pauses(entries):
    ...
    pause = GAP_PAUSE_MS.get((current.get("direction") or {}).get("gap"))
```

The same hole exists on the Skip path, which synthesises a bare instruct per entry and routes it
through the same function:

```python
# app/routers/script.py:3288
elif failed.get("pass") == "instruct":
    # The pass's own fallback: the neutral default direction per entry.
    entries = [{"n": i, "instruct": default_instruct(e)}
               for i, e in enumerate(failed.get("entries") or [])]
...
result = await asyncio.to_thread(apply_manual_recovery, entries, "narrated_as_is",
                                 get_script_recovery_token(checkpoint))   # 3301
```

The failed checkpoint does not record the run's direction, so the recovery code could not pass it
even if it wanted to:

```python
# app/three_pass_generate.py:3417 — the whole `failed` block for pass 3
save("instruct_failed", failed={
    "pass": "instruct",
    "indices": [index for index, _ in current],
    "entries": [{"speaker": entry.get("speaker"), "text": entry.get("text")}
                for _, entry in current],
    "reason": "instruct LLM unavailable; refusing fallback output",
    "attempts": batch_attempts[-20:]})
```

### Impact

A `instruct_failed` checkpoint is reachable in any delivery mode (it is the fail-fast path,
`not collect_all_failures`). Recovering it is only possible through these two routes. The result:

1. The recovery gate accepts rows the live pass would have refused (no `gap`, no `action`, no
   `delivery`, no `answers`), which is a silent weakening of the documented acceptance contract.
2. The recovered rows carry no `direction`, so `apply_gap_pauses` cannot materialise
   `pause_after` for their gap and `get_gap_counts` reports them as `none`. The finished book is
   missing gap-derived pauses on exactly the lines that were hand-recovered, while the run
   manifest still reports the delivery mode as complete.

Not a crash, and it does not corrupt audio: TTS reads only `instruct`
(`three_pass_generate.py:613-616` documents this).

Calibration: the run's own nonverbal fallback also writes direction-less entries
(`app/three_pass_generate.py:3324-3327`), so a missing `direction` is already tolerated
downstream. This finding is that the *recovery path* extends that to arbitrary user rows and
bypasses the gate meant to prevent it — not that direction-less rows are new.

### Fix

- Persist the run's `delivery_direction` in the `failed` block next to
  `quoted_must_be_spoken` / `unquoted_must_be_narrator` (which pass 1 already records the same
  way). Old checkpoints have no such key and must keep behaving exactly as today.
- Pass it to `validate_instruct(frozen, entries, delivery_direction=persisted)`.
- Merge recovered rows through the same constructor the pass uses (`get_directed_entry`), or an
  equivalent, so `direction` survives and `apply_gap_pauses` still applies.
- Apply the same change to the Skip path, and note that `build_recovery_detail`'s fallback prompt
  (`app/routers/script.py:~3070`) is also built with `LLMGenParams()` defaults and therefore shows
  off-mode wording for a directed run when the last attempt did not retain a dispatched request.

### How to verify without guessing

Create a checkpoint with `failed.pass == "instruct"`, a manifest that records
`three_pass_delivery_direction: "gaps"`, then POST a `[{n, instruct}]` body to
`/api/generate_script/inject`. Under the current code it is accepted and the recovered
`annotated` rows have no `direction`; after the fix it is accepted *and* carries `direction.gap`.

---

## Finding 2 — `_external_batch` re-derives the batch output path instead of asking its owner

**Severity:** Low (latent; currently unreachable — hardening)
**Confidence:** mechanism confirmed; not reachable with today's backend dispatch
**Area:** `app/tts.py:3927`, `app/synthesis_recovery_context.py:121-136`,
`app/synthesis_workspace.py:749-788`, `app/project.py:4390-4395`
**Rule:** 15 (one owner for "where does batch chunk N live")

### Evidence

Five batch sites ask the single resolver; the external backend uses a raw f-string:

```python
# app/tts.py — correct call sites
:2221   output_path = get_synthesis_batch_output_path(output_dir, idx)
:2848   output_path = get_synthesis_batch_output_path(output_dir, idx)
:3052   output_path = get_synthesis_batch_output_path(output_dir, idx)
:3226   path = get_synthesis_batch_output_path(output_dir, index)
:3855   output_path = get_synthesis_batch_output_path(output_dir, idx)

# app/tts.py:3927 — _external_batch
output_path = os.path.join(output_dir, f"temp_batch_{idx}.wav")
```

The helper is not a formatter. It consults the registry first:

```python
# app/synthesis_recovery_context.py:121
def get_registered_synthesis_batch_output(output_dir, index):
    binding = _batch_outputs.get()
    if binding is None or os.path.abspath(output_dir) != binding.root:
        return None
    ...
    path = binding.paths.get(index)
    if path is not None and (not binding.active or get_recovery_claim_digest() != binding.claim):
        raise RecoveryAdmissionError('Registered batch output owner changed')
    return path

def get_synthesis_batch_output_path(output_dir, index):      # :134
    path = get_registered_synthesis_batch_output(output_dir, index)
    return path if path is not None else os.path.join(output_dir, f'temp_batch_{index}.wav')
```

So the raw f-string bypasses both the per-chunk workspace substitution and the
`RecoveryAdmissionError` owner check.

### Why it is not live today

Registered batch outputs are only produced for local backends: `_capture_synthesis_recovery_parent`
→ `get_synthesis_workspace_parent(..., batch=True)` returns a parent only when
`engine.get_voice_backend(category) == "local"` (`app/synthesis_workspace.py:749-788`), and
`app/project.py:4390-4395` fills `batch_outputs` only from that parent. In external mode the
registry is empty, so the helper falls through to the identical `temp_batch_{idx}.wav`.

### Impact

No user-visible defect today. The risk is that the resolver is the *only* place that knows about
registration and admission, and one backend silently writes outside it. Any future change that
gives external categories a registered output (or a substituted path) makes `_external_batch`
write to a path the publication/finalisation side does not read, with a failure mode that looks
like "audio was generated but the chunk never completed".

### Fix

```python
output_path = get_synthesis_batch_output_path(output_dir, idx)
```

The helper's own fallback already yields the same string, so this is a one-line change with no
behaviour change and no new failure mode. Leave `app/project.py:4336` alone: that is stale-temp
cleanup performed deliberately under an empty registration.

---

## Finding 3 — Batch result dicts differ by backend, and the VRAM benchmark prints the missing measurement as `0.00`

**Severity:** Low (evidence quality — Rule 19/21: an unmeasured value rendered as a measurement)
**Confidence:** mechanism confirmed
**Area:** `app/tts.py:3254-3385` (`_local_batch_custom`), `:3389-3508` (`_local_batch_clone`),
`:3512` (`_local_batch_lora`), `:3875` (`_external_batch`), `app/tts_vram_benchmark.py:140,210`

### Evidence

Two local backends publish a peak-VRAM figure; the other two publish nothing:

```python
# app/tts.py:3385 (_local_batch_custom)  and  :3508 (_local_batch_clone)
results["peak_vram_gb"] = round(batch_peak_vram_gb, 2)
```

```python
# app/tts.py:3512
def _local_batch_lora(self, chunks, voice_config, output_dir, batch_seed=-1):
    ...
    results = {"completed": [], "failed": []}          # never gains peak_vram_gb

# app/tts.py:3875
def _external_batch(self, chunks, voice_config, output_dir, kind, batch_seed=-1):
    """... Same result shape as local batches."""      # docstring claim is false
    results = {"completed": [], "failed": []}
```

The benchmark consumes the key with a default and formats it as a figure:

```python
# app/tts_vram_benchmark.py:140
peak_gb = batch_results.get("peak_vram_gb", 0.0)
# app/tts_vram_benchmark.py:210
f"{r['peak_vram_gb']:.2f}",
```

Elsewhere the same key is indexed without a default
(`app/experiments/tts_batch_quality_20260929.py:66`: `res["peak_vram_gb"]`), so a LoRA result set
would raise `KeyError` there.

### Impact

A LoRA or external batch reports `peak=0.00GB`, which reads as "measured, no VRAM used" rather
than "not measured". That is the dangerous kind of fallback the project's own Rule 21 warns
about, in a report whose purpose is deciding whether a model fits. It also makes the
`_external_batch` docstring's "same result shape" claim false.

### Fix

- Give every batch backend the same keys, using `None` where the backend genuinely cannot measure
  (an external pool has no local VRAM reading; a LoRA batch can sample like its siblings do).
- Render `None` as `N/A` in `tts_vram_benchmark.py` instead of `0.00`.
- Either drop the "same result shape" sentence or make it true.

---

## Finding 4 — No Python static-analysis gate in CI (140 pyflakes findings in `app/` today)

**Severity:** Medium (whole class of defect is invisible to CI)
**Confidence:** measured + confirmed
**Area:** `.github/workflows/`

### Evidence (measured, reproducible)

```
$ PYTHONPATH=<pyflakes> python3 -m pyflakes app/*.py app/routers/*.py | wc -l
140
$ PYTHONPATH=<pyflakes> python3 -m pyflakes app/*.py app/routers/*.py | cut -d: -f1 | sort -u | wc -l
54
```

(pyflakes 4.1.0, measured 2026-11-19 at `b6088d3c`, on `app/*.py` + `app/routers/*.py` only.
A wider sweep including `app/tests/`, `tools/` and `scripts/` will be larger; this figure is the
narrow, reproducible one.)

```
$ grep -rn "pyflakes\|flake8\|ruff\|pylint" .github/workflows/
(no matches)
```

The only lint-shaped workflow is `.github/workflows/reports-checkjs.yml`, which type-checks
JavaScript (`tools/typecheck/**`). Python has no equivalent.

### Impact

The findings include the kinds of thing a reader would act on immediately:

- unused imports that are load-bearing evidence of dead code paths, e.g.
  `gpu_stats.OOM_MARKERS as _RAW_RECOVERY_ALLOCATION_MARKERS`, three unused `torch` imports at
  `app/tts.py:1627,1665,1700`;
- unused imports in the two largest router modules (`export_staging.MARKER`,
  `export_staging.OWNER_LOCK`, `tts.compute_timeline` in `app/project.py`;
  `three_pass_generate.read_source_text`, `core.claim_gpu_task` in `app/routers/script.py`);
- a dead local `error` at `app/project.py:2918`;
- f-strings with no placeholders at `app/tts.py:1435,2709`.

None of these is a bug on its own. The point is the mechanism: nothing in CI would have caught
them, so the same class of mistake (a rename that leaves a stale reference, an import that hides
a deletion) is only found by a human reading 19k lines.

### Fix

Add a `pyflakes` (or `ruff`) step to an existing workflow, with an explicit baseline so the change
does not fail on day one: either fail only on new findings, or land a cleanup PR first and then
turn the step blocking. Keep it to the four or five runtime trees that matter (`app/`, not
`app/tests/` or `app/experiments/` initially) so the gate is meaningful rather than noisy.

---

## Finding 5 — No baseline security headers except a single `nosniff` on two error paths

**Severity:** Low–Medium (deployment-dependent; local-first app)
**Confidence:** confirmed by search (not by runtime observation)
**Area:** `app/app.py:133-213`, `app/routers/editor.py:1622,1633`

### Evidence

```
$ grep -rn "nosniff\|Content-Security-Policy\|Strict-Transport-Security\|X-Frame-Options\|Referrer-Policy" app/ | grep -v app/tests/
app/routers/editor.py:1622: headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
app/routers/editor.py:1633: headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'}
```

The middleware stack is a task-claim middleware (`app/app.py:136`), an HTTP Basic Auth gate and a
host/origin refusal gate registered as `@app.middleware("http")` (`app/app.py:~165-205`), and
`CORSMiddleware` (`app/app.py:207-213`). None of them sets security headers; the origin gate
answers only host/origin questions.

So: no `Content-Security-Policy`, no `Referrer-Policy`, no `X-Frame-Options` /
`frame-ancestors`, and the one `nosniff` covers two editor error responses only — every other
response, including the SPA shell and JSON API, is served without it.

### Impact

The app is designed to run locally, which is why this is not the top finding. It becomes real the
moment it is exposed — the repo supports remote LLM endpoints and remote serving setups, and an
operator forwarding the port onto a LAN or tunnel gets none of the cheap protections. A CSP is the
control that limits the blast radius of an injected script in the single-page frontend, which
holds LLM output.

### Fix

One middleware that adds the small set to every response, then tighten CSP in report-only mode
first so the vanilla-JS SPA's inline handlers are enumerated rather than assumed safe:
`X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`,
`Content-Security-Policy` starting in report-only. Keep the existing per-response headers as-is
until the middleware is proven, then delete the duplicates (Rule 15).

---

## Finding 6 — `atomic_json_write` silently degrades to a non-atomic copy on `EXDEV`

**Severity:** Low–Medium (correctness under cross-device deployment; every caller inherits it)
**Confidence:** mechanism confirmed
**Area:** `app/utils.py:388-470`

### Evidence

```python
# app/utils.py:388
def atomic_json_write(data, target_path, max_retries=5, *, sort_keys=False, trailing_newline=False,
                      newline=None, allow_nonatomic_fallback=True, return_written_bytes=False,
                      encoder=None):
    """Atomically write JSON data using a temp file and os.replace.
    ...
    On cross-device paths (e.g. Linux bind mounts or NAS shares) os.replace raises EXDEV; in that
    case the function falls back to shutil.move (copy + delete) so the write succeeds rather than
    raising an unhandled OSError.
    Set allow_nonatomic_fallback=False when publication must refuse a cross-device rename instead
    of risking partial replacement by copying.
    """
    ...
            except OSError as e:
                if e.errno == errno.EXDEV:
                    if not allow_nonatomic_fallback:
                        raise
                    # ... shutil.move copy+delete, not atomic ...
```

The fallback is well documented, carries a symlink guard, and is the deliberate default. The
finding is about that default, not about the code being wrong: a function named *atomic* JSON
write, whose entire contract is "a reader never sees a partial file", is non-atomic whenever the
temp directory and the target are on different devices — and it does so silently, on a path the
caller cannot detect from the result.

### Impact

On a bind mount / NAS / container overlay configuration, every journal and state write in the app
gains a window where the destination is truncated or half-written. `book_state_transaction`
journals, the three-pass checkpoint and `state.json` are all written through this function. A
crash or a concurrent reader in that window sees a corrupt file, which is the failure this
function exists to prevent. The `Project`/recovery code already guards its own important writes
with `allow_nonatomic_fallback=False`; the default is what other callers inherit.

### Fix

- Audit the call sites and flip `allow_nonatomic_fallback` to `False` by default for journals,
  checkpoints and any file a reader may load concurrently; keep the opt-in `True` for
  reconstructible caches, where "write something rather than fail" is right.
- Where the fallback is genuinely wanted, log it once (not per write) so a cross-device deployment
  is visible in the log rather than inferred from a corrupted file later.
- Cheapest durable variant: stage the temp file in the *destination's* directory when possible, so
  `EXDEV` does not arise at all.

---

## Checked and deliberately not filed

Recorded so a later pass does not spend time re-deriving them.

### Suspects that turned out to be correct code

- **`by_n[position]` / `by_n[i]` in `apply_manual_recovery`** (`app/routers/script.py:3145,3168`) —
  looks like a 1-based/0-based crash, but `index_head_check`
  (`app/pass_quality.py:596-619`) rejects any missing, duplicated, boolean or out-of-range `n`, so
  after a passing report `by_n` is exactly `{0..k-1}` and neither lookup can raise.
- **Exception escaping the batch script `ThreadPoolExecutor` loop**
  (`app/routers/script.py:~4700`) — `future.result()` can raise before `state["running"] = False`,
  but `_run_claimed_background_task` (`app/core.py:1488-1521`) clears `running` and records the
  failure in its `finally`. No stuck task.
- **A resume discarding recovered rows** — `retry_instructions`
  (`app/three_pass_generate.py:2862-2880`) only resets rows with `instruct_unchecked` or a recorded
  diagnostic failure; the fail-fast path that writes `instruct_failed` records neither, so manual
  recovery survives Retry.
- **`_resolve_batch_output_path` / `ensure_batch_script_output`** (`app/routers/script.py:4111-4210`) —
  the reserved-vs-disk collision split, the re-lock on the renamed candidate, and "replace never
  overwrites an output this same batch will produce" all hold. `lexists` is used consistently, so
  dangling symlinks are treated as collisions.
- **`_capture_and_validate_batch_script_source` / `get_prepared_batch_script_jobs`** — the A/B/A
  identity recheck is bound to the same captured bytes that were decoded, DOCX uses the verified
  group proof, and both the lossy-decode tier and the unsafe-control gate refuse with 400 before
  dispatch.
- **`log_review_characters(review)`** — the `review` it receives is built by
  `get_speakable_entries`, so the shape is right.
- **`_release_local_model_families`'s `None in devices` early return** — it happens before the
  `try/finally`, so there is no `UnboundLocalError`.
- **`ensure_pause_allowed` / `pause_after` bounds** — the gate only rejects `> MAX_PAUSE_MS`, and
  `app/routers/editor.py:345` has `le=MAX_PAUSE_MS` without `ge=0`, but `update_chunk` clamps with
  `max(0, ...)`. Nits only; not filed.
- **`_compute_eta`**, the project-scope `offset` default, and the `changes[0]["to"]` cast — all
  verified correct while reading `app/routers/script.py`.

### Observations not raised as issues

- **Batch dispatch validates a path, not bytes.** `generate_script_batch_start` resolves
  `task.filename` again at dispatch and hands the *path* to the generator subprocess; the identity
  check binds only the preflight/sizing. A source replaced between preparation and dispatch makes
  the preflight refusals (output ceiling, unquoted book, narrator attestation) stale. Cost only —
  the subprocess revalidates its own input — so not filed, but worth folding into any future
  work on the prepared-source cache.
- **`/api/status/{task}` returns a shallow copy of live process state** (`app/routers/script.py:4784`).
  Background threads add keys to nested dicts (e.g. `"saved_as"`) while the endpoint is polled once
  per second. CPython's C JSON encoder does not raise on a size change during iteration, so the
  observed consequence is at most a torn snapshot, not a 500. Rule 17 usually exempts exactly this
  shared `process_state` pattern, so no change recommended without evidence of a bad read.
- **Small nits, each below issue granularity:** `_generate_design_voice_prepared` does
  `seed = int(voice_data.get("seed", -1))` with no `(TypeError, ValueError)` guard, unlike every
  sibling path (callers catch, so no crash); `_local_batch_lora` omits the `peak_vram_gb` key
  covered by Finding 3.

### `app/book_state_transaction.py`

No finding in either pass. Its journaling / exchange / quarantine protocol (v1–v4, owner tokens,
deferred stages, Windows owner failures) resolves every branch I traced before dispatch, and it is
the most defensive code of the four files. That is a coverage statement, not a claim that it is
bug-free.

---

## Limits of this review

- **Nothing was executed.** No route was called, no backend rendered audio, no test was run. The
  unit suite was never run (`app/env` absent, FastAPI/Torch not installed).
- **No UI or browser check.** The SPA at `app/static/index.html` / `app/static/js/*.js` was only
  grepped for wiring (e.g. `tp-delivery-direction`, the recovery panel), never run.
- **The three "whole-app" findings (4, 5, 6) were made in an earlier pass** over the same commit
  and are included here for completeness; only Findings 1–3 come from the line-by-line read of the
  four files, which found no other defect above issue granularity.
- **No GitHub issues were filed.** `gh auth status` reports "You are not logged into any GitHub
  hosts"; there is no `GH_TOKEN`/`GITHUB_TOKEN` in the review environment, and the repo tooling
  available there performs only fetch/pull/push. Run `gh auth login` in the project terminal (do
  not paste a token into a chat) and these six can be filed as-is.

## Suggested filing order

1. Finding 1 (Medium) — reachable, silently wrong output on a recovery path.
2. Finding 4 (Medium) — the mechanism that lets the next instance of these classes through.
3. Finding 6 (Low–Medium) — correctness under cross-device deployment; audit call sites first.
4. Finding 5 (Low–Medium) — cheap, one middleware, one CSP report-only rollout.
5. Finding 2 (Low) — one-line hardening.
6. Finding 3 (Low) — evidence quality; also fixes a `KeyError` in `app/experiments/`.
