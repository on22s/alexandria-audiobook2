# CLAUDE.md Rule-Compliance Audit — Findings Log

Plan: `docs/superpowers/plans/2026-06-19-claude-md-rule-compliance-audit.md`
Progress: `docs/audits/claude_md_rule_audit/PROGRESS.md`

Findings use a single incrementing `F-001, F-002, ...` counter across the whole audit. Use the Finding Template and Fix-now criteria from the plan's Task 1.

---

### [F-001] Rule 9 — `atomic_json_write` retry budget quietly halved for manifest cache write
- **Piece:** P02 — app/hf_utils.py
- **Location:** `app/hf_utils.py:44` (`fetch_builtin_manifest`)
- **Severity:** low
- **Description:** `_atomic_json_write(entries, local_path, max_retries=3)` overrides the shared helper's default `max_retries=5` (see `app/utils.py:69`) with no comment explaining why this call site needs a smaller retry budget than every other caller. This is the only call site in the repo that overrides `max_retries`.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — either restore the default 5 retries, or add a comment explaining why the local manifest-cache write can tolerate fewer retries than other atomic writes (e.g. it's a best-effort offline-fallback cache rather than authoritative state).

### [F-002] Rule 2 — Three near-duplicate prompt-file loaders, only one cached
- **Piece:** P03 — app/default_prompts.py + app/persona_prompts.py + app/review_prompts.py
- **Location:** `app/default_prompts.py:9-41` (`load_default_prompts`) vs `app/persona_prompts.py:6-27` (`load_persona_prompts`) vs `app/review_prompts.py:6-26` (`load_review_prompts`)
- **Severity:** low
- **Description:** All three modules implement the same pattern (read a `---SEPARATOR---`-delimited `.txt` file, split into N parts, raise `RuntimeError` if missing/malformed), but `load_default_prompts` alone added an mtime-based cache while the other two re-read and re-split the file from disk on every call. `app/app.py`'s `get_config()` and `get_default_prompts()` call all three loaders together, repeatedly, on the same request paths (e.g. `app/app.py:1716-1830`), so the caching behavior is inconsistent across what is otherwise one conceptual operation.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — either factor the shared read/split/validate logic into one parametrized helper (delimiter count, error message) used by all three, with caching applied uniformly, or add a comment explaining why `default_prompts.py` needed caching and the others didn't.

### [F-003] Rule 8 — `measure_throughput` swallows any exception identically, indistinguishable from a real bug
- **Piece:** P05 — app/llm_bench.py
- **Location:** `app/llm_bench.py:78-82` (`measure_throughput`)
- **Severity:** low
- **Description:** `except Exception: return None` inside the `as_completed` loop catches every possible error from `_one_call` (network timeout, auth failure, a `TypeError` from a future code change, malformed API response, etc.) with no logging at all, then silently returns `None`. The docstring documents `None` as meaning "this concurrency level isn't safe," but a latent bug producing the exact same `None` every time would be indistinguishable from a real server limitation in `get_cached_or_benchmarked_concurrency`'s caller-facing prints (which only print the chosen concurrency, never that a request actually raised an exception or what kind).
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — log the exception type/message (e.g. `logger.debug` or a print) before returning `None`, so a real bug surfaces differently from an expected timeout/connection failure.

### [F-008] Rule 2 — `train_lora.py` re-implements `resolve_device`/`enable_rocm_optimizations` already present as `TTSEngine` methods
- **Piece:** P09 — app/train_lora.py
- **Location:** `app/train_lora.py:59-65` (`resolve_device`), `app/train_lora.py:68-82` (`enable_rocm_optimizations`) vs `app/tts.py:333-345` (`TTSEngine._resolve_device`), `app/tts.py:349-374` (`TTSEngine._enable_rocm_optimizations`)
- **Severity:** low
- **Description:** Both pairs of functions do the same thing: resolve `"auto"` to `cuda`/`mps`/`cpu` (train_lora.py's version omits the `mps` branch that `tts.py`'s has), and set the same three ROCm env vars (`MIOPEN_FIND_MODE`, `MIOPEN_LOG_LEVEL`, `FLASH_ATTENTION_TRITON_AMD_ENABLE`) plus the same `triton_key` shim guarded by `hasattr(torch.version, "hip")`. `train_lora.py` runs as an independent subprocess (this repo's `app/env` doesn't have the ROCm ML stack — see CLAUDE.md's cross-repo Voice Lab note), so it can't simply `import tts.TTSEngine`, but the logic itself is copy-pasted rather than shared, and the two have already drifted (the `mps` branch is missing from `train_lora.py`'s version).
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — if a shared module is impractical given the separate-env constraint, consider extracting just these two functions into a tiny dependency-free helper module both files import (`device_utils.py` or similar) rather than refactoring across the ROCm-env boundary.

### [F-087] Rule 2 — `extract_prosody` computes `mfcc_mean`/`mfcc_std` that are never consumed anywhere in the file
- **Piece:** P38a — voice_analysis.py
- **Location:** `voice_analysis.py:93-119` (`extract_prosody`), specifically lines 107, 116-117
- **Severity:** low
- **Description:** `extract_prosody` computes a 13-coefficient MFCC (`librosa.feature.mfcc(..., n_mfcc=13, ...)`) and returns `mfcc_mean`/`mfcc_std` arrays in its result dict, but `PROSODY_METRICS` (line 52-57, the only list used to iterate prosody fields downstream in `run_analyze`/`write_pipeline_summary`) contains none of the mfcc keys — confirmed via `grep -n "mfcc" voice_analysis.py`, which shows the two keys are written once and never read. Every call to `extract_prosody` pays the cost of computing and storing a 13-row MFCC matrix per sample purely to discard it.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — either drop the `mfcc` computation/keys from `extract_prosody` if genuinely unused, or add the relevant mfcc fields to `PROSODY_METRICS` if cross-group MFCC divergence was intended to be part of the analyze-phase report.

### [F-088] Rule 9 — Embeddings cache (`embeddings_cache.pkl`) has no invalidation path if `DEDUP_SAMPLES`, the model, or the WAV set changes
- **Piece:** P38a — voice_analysis.py
- **Location:** `voice_analysis.py:151-152` (cache load), `:182-188` (cache hit), `:207` (cache write)
- **Severity:** low
- **Description:** `cache_key` is just `f"{folder_name}/{label}"` (zip stem), with no fingerprint of `DEDUP_SAMPLES`, the random sample selection, or the model used to produce the embeddings. If `DEDUP_SAMPLES` is changed (e.g. 150 → 300) or the model/checkpoint is upgraded, previously cached folders silently keep using the old embeddings (the cache-hit branch at line 184-188 only logs `(cached, N samples)`, not which sample count or model produced them) rather than re-extracting — `run_dedup`'s similarity numbers would then reflect a stale, smaller/older sample set for some folders and a fresh one for others in the same run, with nothing in the printed report to indicate that.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — not a bug introduced by this audit, just flagging the missing invalidation key (e.g. include `DEDUP_SAMPLES` and a model identifier in `cache_key` or alongside the cached tuple) in case stale-cache-after-config-change has bitten anyone in practice.

### [F-091] Rule 2/15 — Narrator-name normalization regex duplicated verbatim between `run_analyze` and `write_pipeline_summary`
- **Piece:** P38b — voice_analysis.py
- **Location:** `voice_analysis.py:330-331` (`run_analyze`) and `voice_analysis.py:573-574` (`write_pipeline_summary`)
- **Severity:** low
- **Description:** [rule15-candidate] The exact same normalization expression — `re.sub(r"[^a-z0-9]+", "_", name.replace("-converted", "").strip().lower()).strip("_")` — appears character-for-character in two functions in the same file: once to build `zip_groups` keys from zip filenames in `run_analyze`, and again in `write_pipeline_summary` to recompute the same key from narrator-folder names so it can check membership in `analyzed_groups`. Both encode the same "how do we name an analyze-phase group" decision; if one site's normalization rule changes (e.g. to strip an additional suffix), the other silently drifts and `write_pipeline_summary`'s DONE/PENDING ANALYZE classification would become wrong for any narrator whose old vs. new normalized name differ.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — extract a single `normalize_group_key(name)` helper used by both call sites, since they must always agree for `write_pipeline_summary`'s cache-membership check to be meaningful.

### [F-009] Rule 8 — Per-sample OOM skips during training are logged live but never aggregated into the run's durable output
- **Piece:** P09 — app/train_lora.py
- **Location:** `app/train_lora.py:556-564` (OOM handler in `train()`'s per-sample loop) vs `:642-660` (`training_meta.json` write)
- **Severity:** low
- **Description:** Each OOM is printed (`[TRAIN] OOM at epoch={epoch} step={step_idx}, skipping sample`) at the moment it happens, so it isn't fully silent — but unlike `load_dataset`'s `skipped_missing`/`skipped_too_long` counters (which are tallied and printed in a `[DATA] Prepared N samples (M skipped: ...)` summary), no equivalent counter exists for OOM-skipped samples across the whole training run. `training_meta.json` (the one artifact consumers reach for after the run, e.g. the Voice Lab pipeline) has no `oom_skips` field, so a run where e.g. 30% of samples silently OOM'd every epoch looks identical in the metadata to a clean run — only a full scrollback through stdout/the captured log would reveal it.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — track a running OOM-skip counter (per-epoch and/or total) and include it in the `[EPOCH]` summary line and `training_meta.json`, mirroring the existing `load_dataset` skip-counter pattern.

### [F-014] Rule 17 — `_finalize_completed_chunk`/`_record_batch_failures` mutate the caller's `results` accumulator instead of returning outcomes
- **Piece:** P11 — app/project.py
- **Location:** `app/project.py:946-975` (`_finalize_completed_chunk`), `:977-990` (`_record_batch_failures`), called from `generate_chunks_batch` (`:1088-1092`)
- **Severity:** low
- **Description:** Both helpers take the caller's `results` dict (`generate_chunks_batch`'s `{"completed": [], "failed": [], "cancelled": 0}` accumulator, built once at the top of the function and threaded across the whole `while pending:` loop) and append to it directly to report success/failure, rather than returning a value for the caller to fold in. This doesn't match CLAUDE.md's documented Rule 17 exceptions: it isn't `process_state`-style concurrently-shared state (the batch loop is single-threaded), and neither helper has a documented mutator-callback contract like `_modify_chunk`'s `mutator(chunk)`. It's also inconsistent with the directly analogous parallel-path helper two methods away in the same class: `generate_chunks_parallel`'s inner `_run_round` (`:780-815`) does the identical job (track completed/oom/hard-failed across a round of chunk generation) but builds fresh local lists and explicitly `return`s `(completed, oom_failed, hard_failed, was_cancelled)` for the caller to extend into its own `results`. `_finalize_completed_chunk` additionally mutates `chunks[idx]` in place (setting `status`/`audio_path`) with no return value at all, so the only way to know what it did is to inspect the dict it was handed.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — have `_finalize_completed_chunk` return `("completed", idx, audio_path)` / `("failed", idx, error)` and `_record_batch_failures` return its existing `oom_failed` plus a list of hard-failure tuples, and have `generate_chunks_batch` fold those into its own `results`/`chunks`, mirroring `_run_round`'s existing return-based pattern in the same file.

### [F-018] Rule 8 — `main()` unconditionally prints "Task review completed successfully" even after a VRAM abort or batch failures
- **Piece:** P12b — app/review_script.py
- **Location:** `app/review_script.py:1184-1194` (end of `main()`)
- **Severity:** low
- **Description:** The final lines of `main()` always print `"Task review completed successfully."` regardless of `vram_aborted` (some entries were never reviewed and saved as-is) or `total_stats["batches_failed"] > 0` (some batches kept their original unreviewed entries after every retry failed) — both conditions are detected and printed as warnings just above (`"Stopped early..."` / per-batch `"FAILED — keeping original entries..."`), but the literal final status line doesn't reflect either. `app/app.py` mitigates the practical impact by regex-parsing `Batches failed:\s*(\d+)` (line 946) from stdout rather than trusting this literal string, but anyone reading raw script output or `logs/api/*.log` directly (which CLAUDE.md instructs as the first debugging step) sees a "completed successfully" line on a run that left part of the script unreviewed.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — gate the final line on `vram_aborted`/`batches_failed`, e.g. print "Task review completed with N batch failure(s)/early stop" instead of an unconditional success message.

### [F-019] Rule 2 — `--source`/`source_text` is loaded but never wired into review calls; "mode 2" context is dead scaffolding
- **Piece:** P12b — app/review_script.py
- **Location:** `app/review_script.py:732,759-766` (`--source` arg + `source_text` load) vs `:1017` (`source_context=None,  # Mode 2: would pass source text chunk here`)
- **Severity:** low
- **Description:** `main()` parses `--source`, opens the file, reads it into `source_text`, and prints its length — but `source_text` is never read again anywhere in the file. The non-contextual review path's own comment at line 1017 confirms this is deliberately deferred ("Mode 2: would pass source text chunk here"), and the `--source` arg's help text says "mode 2, not yet implemented." This is exactly the speculative-branch pattern Rule 2 flags: a parameter and its file-load side effect (and printed length) exist purely for a documented-but-unbuilt future caller, with zero effect on today's review behavior.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — either remove `--source`/the load until mode 2 is actually implemented, or leave as-is if the team wants the CLI surface pre-reserved; not fix-now since it's a user-facing CLI flag, not a zero-caller private helper.

### [F-020] Rule 2 — Redundant local `import re` shadows the already-imported module-level `re`
- **Piece:** P12b — app/review_script.py
- **Location:** `app/review_script.py:648` (`check_text_loss`)
- **Severity:** low
- **Description:** `check_text_loss` has `import re` as its first statement, even though `re` is already imported at module level (`app/review_script.py:4`) and used freely elsewhere in the same file (e.g. `_is_group_label`, `_is_section_break`) without a local re-import. Harmless (same module object either way) but unnecessary — minimum-code-that-solves-the-problem violation.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — delete the local `import re` line; trivial one-line cleanup, left as needs-decision only because Rule 3 (surgical changes / don't touch adjacent code) governs whether unrelated cleanup is in scope for this audit pass.

### [F-021] Rule 9 — CLAUDE.md's "review_batch retry/gc.collect() loop" doesn't match any code in review_script.py
- **Piece:** P12b (controller spot-check while reviewing the P12a/b subagent's work)
- **Location:** CLAUDE.md (Rule 9 text) vs `app/review_script.py:596-629` (`review_batch`) vs `app/project.py:818-836` and `app/project.py:1085-1100` (`_run_round`/`generate_batch` worker-stepdown loops, both `gc.collect()` then retry on VRAM OOM)
- **Severity:** low
- **Description:** CLAUDE.md's Rule 9 names "the `review_batch` retry/`gc.collect()` loop" as a protected safety net. `review_batch` (app/review_script.py:596) has no `gc.collect()` and delegates its only retry loop to `generate_script.py::call_llm_for_entries`, which also has no `gc.collect()` — confirmed via `grep -rn "gc.collect" app/`. The actual mechanism matching CLAUDE.md's description (step the worker/batch count down on VRAM OOM, `gc.collect()`, retry just the failed items) lives in `app/project.py`'s two audio-generation loops instead, which is unrelated to script review. This is a documentation-accuracy gap in CLAUDE.md itself, not a code defect — flagging because a future audit session searching for "the review_batch retry/gc.collect() loop" inside review_script.py will not find it, same as happened here.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — this is for the user to correct in CLAUDE.md (e.g. retarget the citation at `project.py`'s worker-stepdown loops), not something the audit can fix in code.

### [F-023] Rule 10 — `wait_for_task` treats a non-200 poll response identically to "still running," with no distinction surfaced on timeout
- **Piece:** P14a — app/test_api.py (helpers → chunk tests; helper is defined here, called from P14b at lines 1093/1101)
- **Location:** `app/test_api.py:80-88` (`wait_for_task`)
- **Severity:** low
- **Description:** The polling loop's continue condition is `if r.status_code == 200 and not r.json().get("running")`, so a transient server error (500), a route that doesn't exist for that task name, or a normal in-progress `running: true` response all fall through to the same `time.sleep(poll_interval)` and the loop just keeps polling — interpretation of "not done yet" is at least consistent across iterations (no Rule 10 inconsistency within a single call), but the eventual `return False` on timeout gives the caller (`test_generate_batch`/`test_generate_batch_fast` in P14b) no way to tell "task is still legitimately running after 120s" apart from "the status endpoint has been erroring the whole time." A caller only sees a bare `TestFailure` either way.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — track and surface the last non-200 status/last response body when returning `False`, so a timeout caused by a broken status endpoint is distinguishable from a genuinely slow task.

### [F-025] Rule 12 — `test_voice_design_save_and_delete` skips the list-membership round-trip that the analogous `test_clone_voices_upload_and_delete` performs
- **Piece:** P14b — app/test_api.py (status/preparer/voicelab/lora/dataset-builder/audio tests)
- **Location:** `app/test_api.py:775-793` (`test_voice_design_save_and_delete`) vs `:817-852` (`test_clone_voices_upload_and_delete`)
- **Severity:** low
- **Description:** `test_clone_voices_upload_and_delete` (the structurally identical "create a resource → verify it's in the list → delete it → verify it's gone from the list" test for the sibling clone-voices feature) does all four steps explicitly (lines 838-852). `test_voice_design_save_and_delete` does only "create → delete": it asserts `voice_id` is present in the save response and that the DELETE returns 200, but never calls `GET /api/voice_design/list` either before deleting (to confirm the save actually persisted the entry) or after (to confirm the delete actually removed it). A save handler that returns a `voice_id` without writing anything, or a delete handler that returns 200 without removing the entry, would both pass.
- **Status:** logged
- **Suggested fix:** mirror `test_clone_voices_upload_and_delete`'s pattern — `GET /api/voice_design/list` after save to confirm `voice_id` is present, and again after delete to confirm it's gone.

### [F-026] Rule 8 — `cleanup()`'s five `except Exception: pass` blocks silently hide cleanup failures with no logging
- **Piece:** P14b — app/test_api.py (status/preparer/voicelab/lora/dataset-builder/audio tests)
- **Location:** `app/test_api.py:1314-1346` (`cleanup()`)
- **Severity:** low
- **Description:** Each of the five cleanup attempts (test script, builder project, gen project, test dataset, stray voice-design entries) is wrapped in its own bare `except Exception: pass`, so any failure — a real server bug, a typo'd endpoint, a changed response shape — is indistinguishable from "there was nothing to clean up." The only observable signal is that the item's label is missing from the printed `"Cleaned: ..."` line, which requires a human to notice an absence rather than see an explicit warning. Lower severity than F-006/F-011/F-015 (which hide production data loss) since this only affects test-run hygiene, but it's the same swallow-with-no-trace pattern and means leftover `_test_*` fixtures from a failed cleanup accumulate invisibly across runs.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — print the exception (e.g. `print(f"  [cleanup] failed to delete {item}: {e}")`) in each `except` block instead of `pass`, so a cleanup regression is visible in the test output.

### [F-027] Rule 9 — CLAUDE.md's documented `NON_GPU_TASKS` set is stale; code (correctly) excludes "voices" but docs still list it
- **Piece:** P15 — app/app.py (imports → check_global_gpu_lock)
- **Location:** CLAUDE.md ("Concurrency model: `process_state` + GPU lock" section) vs `app/app.py:529-533` (`NON_GPU_TASKS`/`GPU_TASKS` definitions + adjacent comment)
- **Severity:** low
- **Description:** CLAUDE.md states `NON_GPU_TASKS = {"voices", "audacity_export", "m4b_export"}`, but the actual code at `app/app.py:532` defines `NON_GPU_TASKS = {"audacity_export", "m4b_export"}` only, with an explicit comment immediately above it: `"voices" (suggest_voices) is intentionally NOT here: it runs local LLM inference, so it must respect the GPU lock to avoid OOM alongside TTS/review.` Confirmed correct in code — `/api/suggest_voices` (`app/app.py:2885`) calls `claim_gpu_task("voices")` and makes a real LLM call via `_suggest_voices_impl`, so excluding it from `NON_GPU_TASKS` (i.e. keeping it inside `GPU_TASKS`) is the safe, correct behavior; CLAUDE.md's text is simply out of date relative to this fix. Verified the full `GPU_TASKS`/`NON_GPU_TASKS` partition is otherwise complete and correct: all 14 `GPU_TASKS` members (script, voices, persona, audio, review, batch_review, nicknames, lora_training, dataset_gen, dataset_builder, preparer, batch_preparer, batch_script, voicelab) are genuinely GPU/LLM-bound, and the 2 `NON_GPU_TASKS` members (audacity_export, m4b_export) call only `project_manager`'s ffmpeg/pydub audio-stitching with no GPU/LLM involvement. Every `check_global_gpu_lock(task)` call site (14 found) is paired with a later `claim_gpu_task(task)` call for the same task name in the same function (confirmed via grep across all of app.py); `voices` and `nicknames` skip the standalone `check_global_gpu_lock` call and go straight to `claim_gpu_task` (which internally re-checks), which is consistent and not a gap. `check_global_gpu_lock` itself raises `HTTPException(400)` with no silent bypass path when any other `GPU_TASKS` member is running.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — this is a CLAUDE.md correction, not a code fix (same category as F-021): update the "Concurrency model" section's `NON_GPU_TASKS` description to drop "voices" and ideally cite the in-code comment explaining why, so future audits don't need to re-derive this from scratch.

### [F-028] Rule 16 — `check_global_gpu_lock` raises rather than returns bool/tuple, unlike the codebase's other two `check_`-named functions
- **Piece:** P15 — app/app.py (imports → check_global_gpu_lock)
- **Location:** `app/app.py:535-552` (`check_global_gpu_lock`) vs `app/app.py:271-279` (`check_disk_space`) and `app/review_script.py:640` (`check_text_loss`)
- **Severity:** low
- **Description:** Grepped every `check_`-prefixed top-level function in `app/*.py` (3 total): `check_disk_space` returns `(has_space, free_gb)` and `check_text_loss` returns `(passed, original_text, corrected_text, ratio)` — both report success/failure via a return value with zero side effects. `check_global_gpu_lock` is the only one of the three that reports failure by raising `HTTPException(400)` directly, which is a real side effect (aborting the request) rather than a value the caller inspects. This isn't a Rule 17 dual-purpose-parameter violation (no parameter is mutated) and the raise-on-conflict behavior is itself correct, intentional, and necessary for a route guard (every one of its 14 call sites relies on the raise propagating straight out of the FastAPI handler) — flagging only because it's a narrower naming-convention inconsistency than the audit's `check_`-as-pure-read assumption suggests: this codebase's own `check_` convention (per `check_disk_space`/`check_text_loss`) is "return a value," and `check_global_gpu_lock` deviates from that, even though it's still clearly a verification/guard function rather than a hidden mutator.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — likely not worth changing: every caller already depends on the raise-and-propagate behavior, and rewriting 14 call sites to check a returned bool and raise themselves would be a larger, riskier change for a purely cosmetic naming-convention gain. If anything, a one-line docstring note ("raises HTTPException on conflict, unlike check_disk_space/check_text_loss's return-based convention") would close the gap cheaply.

### [F-030] Rule 2 — `GET /api/annotated_script` has zero frontend callers; only exercised by test_api.py
- **Piece:** P17 — app/app.py (/api/generate_script → /api/logs/{task_name})
- **Location:** `app/app.py:2631-2637` (`get_annotated_script`)
- **Severity:** low
- **Description:** `grep -rn "annotated_script" app/static/index.html` returns nothing — the SPA never calls this route (it reads chunks/voice config through other endpoints instead). The only caller in the repo is `app/test_api.py:380-381` (`test_get_annotated_script`). Unlike a true zero-caller private helper, this is a public `GET` route returning raw `annotated_script.json` — plausibly intended as a programmatic/curl-accessible API per this project's documented "API documentation" convention (CLAUDE.md "Write Documentation" section), not dead code, so it does not meet fix-now criteria (a route, not a private helper) and the test coverage suggests it's a known, intentional surface rather than an accidental leftover.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — confirm whether this route is meant as a documented external/programmatic API (in which case it's fine as-is and could be noted as such) or is genuinely vestigial from before some other endpoint replaced it.

### [F-034] Rule 2 — `DELETE /api/m4b_cover` has zero callers anywhere in the repo (frontend or tests)
- **Piece:** P19 — app/app.py (/api/audiobook → /api/review/checkpoints)
- **Location:** `app/app.py:3203-3209` (`delete_m4b_cover`)
- **Severity:** low
- **Description:** `grep -rn "m4b_cover" --include="*.py" --include="*.html" --include="*.js" .` shows the only caller of any `/api/m4b_cover` endpoint is the upload (`POST`) listener at `app/static/index.html:4663`; there is no "remove cover" button/handler anywhere in `index.html`, and `app/test_api.py` doesn't exercise this route either (confirmed via the same grep — no `DELETE` call site exists). Unlike a true private-helper zero-caller case, this is a public route (not a private helper), so it does not meet fix-now criteria per the audit's own established precedent (F-030 treated a zero-frontend-caller `GET` route the same way) — but unlike F-030, there is no test coverage at all here to suggest it's an intentionally-reserved programmatic API surface.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — either wire up a "remove cover" control in the M4B export section of the Result tab (the natural UI counterpart to the existing upload input), or confirm the route is deliberately reserved for direct/curl use and leave as-is.

### [F-041] Rule 2 — `POST /api/lora/generate_dataset` has zero frontend callers; only exercised by test_api.py
- **Piece:** P22 — app/app.py (/api/lora/upload_dataset → /api/lora/preview/{adapter_id})
- **Location:** `app/app.py:4246-4354` (`lora_generate_dataset`)
- **Severity:** low
- **Description:** `grep -n "generate_dataset" app/static/index.html` returns nothing; the LoRA tab's UI only calls `/api/lora/upload_dataset` (file upload), `/api/lora/datasets` (list), `/api/lora/train`, `/api/lora/models`, `/api/lora/preview/*`, `/api/lora/test`, `/api/lora/download/*`. The only caller of `/api/lora/generate_dataset` in the repo is `app/test_api.py:1146-1160` (`test_lora_generate_dataset`). Same category as F-030/F-034: a public route (not a private helper), so it doesn't meet fix-now criteria, but unlike F-030 there's no obvious "documented external API" framing — the route's whole purpose (generate N Voice-Design samples sharing one description, into a ready-to-train dataset) is fully superseded in the UI by the separate Dataset Builder tab (P23's `/api/dataset_builder/*` routes), which the frontend exclusively uses for that workflow instead.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — confirm whether this route predates the Dataset Builder tab and is now dead surface (in which case remove it and its `dataset_gen` `process_state`/`GPU_TASKS` entry), or is deliberately kept as a simpler programmatic alternative to the Dataset Builder's multi-step UI flow.

### [F-042] Rule 8 — Malformed `metadata.jsonl` lines in an uploaded LoRA dataset are silently skipped during validation, with zero logging of which lines or how many
- **Piece:** P22 — app/app.py (/api/lora/upload_dataset → /api/lora/preview/{adapter_id})
- **Location:** `app/app.py:4214-4220` (`lora_upload_dataset`)
- **Severity:** low
- **Description:** Inside the per-line metadata-validation loop, `except (json.JSONDecodeError, KeyError): pass` discards the parse error with no logging, while `sample_count` was already incremented for that line just above (line 4213) before the `try`. A dataset whose `metadata.jsonl` has e.g. 50 well-formed lines and 10 malformed ones reports `sample_count: 60` in the upload response and logs "60 metadata entries" — overcounting by the number of unparseable lines, with no signal anywhere (response or log) that some entries failed to parse at all, only that some *referenced audio files* might be missing (a separate, already-logged check).
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — track a separate `malformed_count` alongside `missing_audio`, log it the same way (`logger.warning` with examples) if non-zero, and consider excluding malformed lines from `sample_count` so the reported number reflects usable entries.

### [F-045] Rule 8 — `GET /api/voicelab/config` swallows any exception while checking `zips_dir` validity, with zero logging
- **Piece:** P25 — app/app.py (/api/voicelab/*)
- **Location:** `app/app.py:5340-5344` (`voicelab_get_config`)
- **Severity:** low
- **Description:** `except Exception: pass` around `_resolve_zips_dir(cfg["zips_dir"])` / `os.path.isdir(resolved_zips)` discards any failure — not just the expected "folder doesn't exist" case (which `os.path.isdir` already returns `False` for without raising) but also genuine errors like a malformed `zips_dir` value causing `os.path.normpath`/`os.path.join` to raise, or a `PermissionError` from `os.path.isdir`. The route then returns `zips_dir_ok: False` in its JSON, indistinguishable from the legitimate "configured but missing" case — same recurring silent-swallow-into-success-looking-response shape as F-035/F-036/F-046.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — narrow the except to expected exception types (or none, since `os.path.isdir` doesn't raise for a missing path) and `logger.warning` anything unexpected so a real bug in `zips_dir` resolution isn't silently reported as "folder not found."

### [F-047] Rule 2 — Three `sub-batch-*-group` wrapper divs follow the file's established show/hide-toggle-target naming convention but are never toggled
- **Piece:** P26 — app/static/index.html (HTML/CSS shell + tab markup, lines 1-1789)
- **Location:** `app/static/index.html:622,627,632` (`sub-batch-min-group`, `sub-batch-ratio-group`, `sub-batch-max-items-group`), sibling of `sub-batch-enabled` checkbox at line 617
- **Severity:** low
- **Description:** This file has an established pattern of giving a wrapper `<div id="X-group">` to fields that get conditionally shown/hidden via `document.getElementById('X-group').style.display = ...` — confirmed for `tts-url-group`/`tts-device-group` (toggled by `toggleTTSMode()`, lines 2024-2025) and `llm-ssh-group` (toggled by `onLlmModeChange`, line 2159). The three `sub-batch-*-group` divs follow the identical naming convention and wrap fields that are logically dependent on the adjacent `sub-batch-enabled` checkbox (line 617, "Sub-batching" toggle, "Split batches by text length to reduce padding waste") — but `grep -n "sub-batch-min-group\|sub-batch-ratio-group\|sub-batch-max-items-group"` shows zero JS references anywhere in the file, and `sub-batch-enabled` itself has no `onchange` handler at all. The three sub-fields (`sub-batch-min-size`, `sub-batch-ratio`, `sub-batch-max-items`) remain visible and editable even when "Sub-batching" is unchecked, which is also a minor UX inconsistency (editing settings that the adjacent toggle implies are inactive) but the audit-relevant point is the dead `-group` ids: either the toggle wiring was planned and never added, or it existed and was removed.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — either wire `sub-batch-enabled`'s `onchange` to toggle these three `-group` divs' `style.display` (mirroring `toggleTTSMode`/`onLlmModeChange`), or remove the unused `id` attributes if the fields are meant to always stay visible/editable regardless of the toggle.

### [F-049] Rule 15 — `confirmIfRemote`/`testLlmConnection` derive "is remote" from the frontend's own `currentLlmMode` string compare, not a backend dispatch call
- **Piece:** P27 — app/static/index.html (showToast → testLlmConnection, lines ~1790-2207)
- **Location:** `app/static/index.html:1842` (`confirmIfRemote`), `:2188` (`testLlmConnection`'s `modeLabel`)
- **Severity:** low
- **Description:** [rule15-candidate] Both sites compute "is this remote?" via a bare `currentLlmMode !== 'remote'` / `currentLlmMode === 'remote'` string comparison against the frontend's own local mirror of `llm_mode`. The backend's canonical answer to this question is `lmstudio_settings.is_remote_llm(llm_mode, base_url)` (cited in CLAUDE.md Rule 15 and F-004/F-007/F-012 as the single source of truth specifically because raw `llm_mode` alone can drift from the actual active `base_url`). `GET /api/config` (confirmed via `grep -n "llm_mode" app/app.py`) only ever returns the raw `llm_mode` string, never a precomputed `is_remote` boolean from `is_remote_llm` — so the frontend has no way to detect the same drift case `is_remote_llm` exists to catch, and never will unless the backend starts exposing that computed value. This is the same class of "two independently-maintained is-remote checks can drift" risk Rule 15 names, just split across the frontend/backend boundary rather than between two backend files.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — not resolved here per audit scope (tag only); flagging for the Rule 15 cross-cutting pass (Task 4 in PROGRESS.md). A fix would have `/api/config` (or `/api/llm/test`) include a precomputed `is_remote` field for the frontend to consume instead of re-deriving it from `llm_mode` alone.

### [F-050] Rule 16 — `numFieldValue` has no leading verb despite being a pure read function
- **Piece:** P27 — app/static/index.html (showToast → testLlmConnection, lines ~1790-2207)
- **Location:** `app/static/index.html:1863-1867` (`numFieldValue`)
- **Severity:** low
- **Description:** `numFieldValue(id, def, isInt)` reads a DOM input's `.value`, parses it, and returns the parsed number or a fallback default — a pure read with zero side effects, exactly the case CLAUDE.md's Rule 16 says should read as `get_`-style. Every other function in this range either matches the file's documented `render*`/`on*`/`open*` conventions or has a clear action verb (`show`, `escape`, `notify`, `apply`, `cycle`, `toggle`, `populate`, `sync`, `test`, `confirm`); `numFieldValue` alone is a bare noun phrase with no verb at all, so a reader can't tell from the name whether it reads, writes, or both.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — rename to `getNumFieldValue` (all 15 call sites would need updating); left as needs-decision rather than fix-now since renaming isn't in the fix-now criteria (zero-caller deletion or Rule 18 braces only) and this function has many call sites outside the P27 range.

### [F-052] Rule 8 — `cancelBatchScript`/`cancelBatchReview` swallow cancel failures with `/* ignore */`, unlike their single-item siblings `cancelScript`/`cancelReview`
- **Piece:** P28
- **Location:** `app/static/index.html:2622-2627` (`cancelBatchScript`), `:2779-2784` (`cancelBatchReview`) vs `:2577-2584` (`cancelScript`) and `:2773-2778` (`cancelReview`)
- **Severity:** low
- **Description:** `cancelScript` and `cancelReview` both call `showToast('Cancel failed: ' + (e.message || 'unknown error'), 'warning')` in their `catch` block so the user learns a cancel request didn't go through. Their batch counterparts, `cancelBatchScript` and `cancelBatchReview`, use `catch (e) { /* ignore */ }` instead — a failed batch-cancel (e.g. network error, 500) is completely invisible: the Cancel button's pause-button reset (`_resetPauseBtn`) never runs, the batch keeps running server-side, and the user gets no feedback that their click did nothing. This is an inconsistency within the same file between two near-identical pairs of functions, not just a generic missing-log case.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — mirror `cancelScript`/`cancelReview`'s `showToast('Cancel failed: ...', 'warning')` in both batch variants' `catch` blocks.

### [F-054] Rule 16 — `_reviewDedupe` has no leading verb despite being a pure read function
- **Piece:** P28
- **Location:** `app/static/index.html:2717-2720` (`_reviewDedupe`)
- **Severity:** low
- **Description:** `_reviewDedupe()` reads the `review-dedupe-speakers` checkbox and returns a boolean (defaulting to `true` if the checkbox doesn't exist) — a pure read with zero side effects, the same shape as F-050's `numFieldValue` and exactly the case Rule 16 says should read as `get_`/`is_`-style. The name is a bare noun phrase with no verb, so a reader can't tell from the name alone whether it reads, writes, or toggles dedupe state.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — rename to `_isReviewDedupeChecked` or `_getReviewDedupe` (4 call sites would need updating); not fix-now since renaming isn't in the fix-now criteria.

### [F-056] Rule 8 — `loadCharacterAliases` silently treats any fetch failure as "no aliases yet," with no logging
- **Piece:** P29
- **Location:** `app/static/index.html:2954` (`loadCharacterAliases`)
- **Severity:** low
- **Description:** `try { aliases = await API.get('/api/character_aliases'); } catch (e) { aliases = {}; }` discards any fetch failure (network error, 500, malformed JSON) with zero logging, then renders the panel as if no aliases had ever been found/saved — indistinguishable from the legitimate "nothing discovered yet" case. Same recurring silent-swallow-into-success-looking-render pattern already logged for other files' manifest/config loads (e.g. F-031, F-035, F-036, F-046), now found in the frontend for `character_aliases.json`.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — `console.error`/`console.warn` the caught error before falling back to `{}`, so a fetch failure is distinguishable in the browser console from "no aliases yet."

### [F-057] Rule 8 — `pollPersonaStatus`'s post-completion `loadVoices()` refresh swallows its error silently while its two neighboring cache-refresh calls both log
- **Piece:** P29
- **Location:** `app/static/index.html:3149` (`pollPersonaStatus`) vs `:3150-3151` (same function, two lines below)
- **Severity:** low
- **Description:** Inside the `if (!status.running)` block, three sequential cache/UI-refresh calls each have their own `try/catch`: `try { await loadVoices(); } catch (e) { /* ignore */ }` (3149) swallows with no logging at all, while the next two lines — `_designedVoicesCache`/`_cloneVoicesCache` prefetch — both call `console.debug('...failed', e)` on the identical kind of failure. `loadVoices()` refreshes the actual Voices tab the user is about to look at after persona generation finishes, arguably the most user-visible of the three refreshes, yet it's the one with zero diagnostic trail if it fails — inconsistent with its own immediate neighbors in the same block.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — change `catch (e) { /* ignore */ }` to `catch (e) { console.debug('voices refresh failed', e); }`, matching the pattern already used two lines below.

### [F-059] Rule 16 — `_scriptVolumeNum` has no leading verb despite being a pure computation function
- **Piece:** P29
- **Location:** `app/static/index.html:2839-2842` (`_scriptVolumeNum`)
- **Severity:** low
- **Description:** `_scriptVolumeNum(name)` regex-matches a trailing number out of a script name and returns it (or `Infinity` if none found) — a pure computation with zero side effects, the same shape as F-050 (`numFieldValue`) and F-054 (`_reviewDedupe`), now a third instance of this naming gap. The name is a bare noun phrase with no verb, so a reader can't tell from the name alone that it's a read/extraction rather than something that mutates state.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — rename to `_getScriptVolumeNum` or `_extractScriptVolumeNum` (2 call sites would need updating); not fix-now since renaming isn't in the fix-now criteria.

### [F-061] Rule 8 — `loadVoices` and `suggestVoices` swallow four cache-refresh fetch failures with comment-only `catch` blocks, no logging
- **Piece:** P30
- **Location:** `app/static/index.html:3333-3341` (`loadVoices`, three `try/catch` blocks for `/api/voice_design/list`, `/api/clone_voices/list`, `/api/lora/models`) and `:3344-3346` (`loadVoices`, `loadCastLibrary()` call) and `:3379` (`suggestVoices`, `/api/lora/models` refresh)
- **Severity:** low
- **Description:** Five separate `try { await API.get(...) } catch (e) { /* ignore if ... */ }` blocks across these two functions discard any fetch failure (network error, 500, malformed JSON) with zero logging, then proceed as if the cache were simply empty — indistinguishable from "nothing to show yet." This is the same recurring silent-swallow-into-success-looking pattern already logged for other parts of this file (F-031, F-035, F-036, F-046, F-056, F-057), now found concentrated in the Voices-tab cache refreshes that back the voice-type dropdowns (built-in LoRA, clone, LoRA-adapter selects) — a real failure here would silently leave those dropdowns empty/stale with no diagnostic trail.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — add `console.debug`/`console.warn` in each `catch`, matching the pattern already used elsewhere in the file (e.g. `pollPersonaStatus`'s `_designedVoicesCache`/`_cloneVoicesCache` prefetch from F-057).

### [F-062] Rule 15 (candidate) — `submitCastApply` and `submitCastApplyBulk` duplicate the same checkbox→mapping extraction logic verbatim
- **Piece:** P30
- **Location:** `app/static/index.html:3723-3729` (`submitCastApply`) vs `:3829-3835` (`submitCastApplyBulk`)
- **Severity:** low
- **Description:** `[rule15-candidate]` Both functions build a `mapping` object from `.cast-apply-check:checked` / `.cast-apply-target` DOM elements with identical 7-line logic (`const mapping = {}; document.querySelectorAll(...).forEach(cb => {...}); if (!Object.keys(mapping).length) {...}`), then diverge only in which API endpoint they POST to. The file already extracted the shared match-table-rendering logic into `_getCastMatchPool`/`_renderCastMatchRows` for the analogous "open" half of these two flows, but the "submit" half's mapping-extraction was left duplicated rather than also factored into a shared helper.
- **Status:** logged
- **Suggested fix:** see needs-decision — not resolving per audit scope (tag only); a future pass could extract a shared `_collectCastApplyMapping()` helper used by both submit functions.

### [F-065] Rule 2 / Rule 15 (candidate) — `renderAll` and `renderBatchFast` are ~95%-identical polling/error-handling logic differing only by endpoint and one missing confirm gate
- **Piece:** P31
- **Location:** `app/static/index.html:4450-4528` (`renderAll`) vs `:4530-4601` (`renderBatchFast`)
- **Severity:** medium
- **Description:** `[rule15-candidate]` These two ~75-line functions are line-for-line identical except: the API endpoint (`/api/generate_batch` vs `/api/generate_batch_fast`), the `regenerateAll` confirm gate (see F-064, present only in `renderAll`), two comments, and the `console.error` label string. The entire `toProcess` filtering, optimistic-UI marking loop, and `setInterval`-based completion-polling block (including the `isRenderingAll` bail-out and the completed/failed toast summary) is duplicated verbatim rather than factored into one shared helper parametrized by endpoint.
- **Status:** logged
- **Suggested fix:** see needs-decision — not resolved here per audit scope; a future pass could extract a shared `_pollBatchCompletion(indices, onDone)` (or similar) used by both, which would also have prevented F-064 by construction.

### [F-066] Rule 8 — `playSequence`'s playback-failure handlers use `console.log` only (not even `console.error`) and never surface a failed chunk to the user
- **Piece:** P31
- **Location:** `app/static/index.html:4296-4316` (`playSequence`'s `playNext`, `playPromise.catch` and `audio.onerror`)
- **Severity:** low
- **Description:** When `audio.play()` rejects (line 4299-4304) or the `<audio>` element fires `onerror` (line 4312-4316), the handler logs `console.log("Play failed (empty or skipped):", e)` / `console.log("Audio error, skipping")` and silently advances to the next chunk in the sequence — no `console.error`, no `showToast`. A user listening to "Play Sequence" who hits a corrupt/missing audio file gets no indication a chunk was skipped; it just looks like silence or a jump in the sequence.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — at minimum use `console.error` instead of `console.log`; consider a one-time `showToast` (not per-chunk-spam) summarizing how many chunks were skipped due to playback errors, if any.

### [F-067] Rule 8 — `debouncedSaveVoices` and the M4B-cover-upload handler surface save/upload failures via UI text only, with no `console.error`
- **Piece:** P31
- **Location:** `app/static/index.html:3927-3929` (`debouncedSaveVoices`'s `catch`) and `:4667-4670` (M4B-cover-upload `change` handler's `catch`)
- **Severity:** low
- **Description:** Both `catch` blocks set `statusEl`'s text/class to show a "save failed" / error message — so the failure isn't fully silent to the user — but neither logs to the console, unlike most other catches in this same range (e.g. `cancelRender`'s `console.error('Cancel error:', e)` at line 4437, `generateChunk`'s `console.error`+`showToast` pair at lines 4422-4423). This makes the two inconsistent with the file's own dominant convention of pairing user-facing feedback with a console log for diagnosability.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — add `console.error(e)` alongside the existing status-text update in both catches, matching the convention used elsewhere in this range.

### [F-068] Rule 16 — `debouncedSaveVoices` has no leading verb despite triggering a network write (`/api/save_voice_config` POST)
- **Piece:** P31
- **Location:** `app/static/index.html:3915` (`debouncedSaveVoices`)
- **Severity:** low
- **Description:** The function's first word is the adjective "debounced," not a verb — unlike every other side-effecting function in this range (`updateChunkRow`, `loadChunks`, `saveRowEdits`, `cancelRender`, `renderAll`, etc., all verb-first per Rule 16). The side effect (`Save...Voices`, an `await API.post('/api/save_voice_config', ...)`) is present but pushed to the middle of the name instead of the front.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — e.g. rename to `saveVoicesDebounced` so the verb leads, or treat "debounced" as an accepted modifier prefix alongside `render*`/`on*`/`open*` if the team prefers not to rename (this is the only such case found so far in the audit).

### [F-070] Rule 8 — `saveScript`, `loadScript`, `deleteScript` surface failures via `showToast` only, with no `console.error`
- **Piece:** P32
- **Location:** `app/static/index.html:4815-4817` (`saveScript`'s `catch`), `:4839-4841` (`loadScript`'s `catch`), `:4854-4856` (`deleteScript`'s `catch`)
- **Severity:** low
- **Description:** All three `catch (e)` blocks call only `showToast('Error ...: ' + e.message, 'error')` — the user sees a message, but nothing is logged to the console, unlike the file's dominant convention elsewhere of pairing user-facing feedback with `console.error` (e.g. `pollLogs`'s own catch two functions above, line 4754-4757, logs `console.error("Poll error", e)`). This is the same inconsistency already logged for `debouncedSaveVoices`/M4B-cover-upload in F-067, recurring in three more functions in the very next piece.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — add `console.error(e)` alongside the existing `showToast` call in all three catches.

### [F-071] Rule 8 — `loadDesignedVoices`'s catch logs to console only, with zero user-facing feedback on failure
- **Piece:** P32
- **Location:** `app/static/index.html:4892-4894` (`loadDesignedVoices`)
- **Severity:** low
- **Description:** Unlike `saveScript`/`loadScript`/`deleteScript` in the same piece (F-070, toast-only/no console), this catch is the opposite: `console.error('Failed to load designed voices:', e)` with no `showToast` and no DOM update at all. If `/api/voice_design/list` fails (e.g. when called from `resetDesignerForm`'s neighbor `loadScript` flow at line 4838, or the page-load call at line 6190), the designed-voices list silently stays in whatever state it was previously in — a user has no indication the refresh failed.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — add a `showToast('Failed to load designed voices', 'error')` (or equivalent inline status message) alongside the existing `console.error`.

### [F-078] Rule 8 — `cancelPreparer` swallows a cancel-request failure with `/* ignore */`, a third instance of the `cancelBatchScript`/`cancelBatchReview` vs. `cancelScript`/`cancelReview` inconsistency (F-052)
- **Piece:** P34
- **Location:** `app/static/index.html:6272` (`cancelPreparer`) vs `:6478-6481` (`cancelVoicelab`, same piece) and F-052's `cancelScript`/`cancelReview`
- **Severity:** low
- **Description:** `cancelPreparer` does `try { await API.post(url, {}); } catch (e) { /* ignore */ }` — a failed cancel request (network error, 500, task already finished) is completely invisible: the Cancel button stays visible, the preparer/batch-preparer job keeps running server-side, and the user gets no feedback that their click did nothing. Its sibling in the very same piece, `cancelVoicelab` (lines 6478-6481), handles the identical failure mode correctly: `catch (e) { showToast('Cancel failed: ' + (e.message || 'unknown'), 'warning'); }`. This is the same inconsistency already logged as F-052 (`cancelBatchScript`/`cancelBatchReview` swallow vs. `cancelScript`/`cancelReview` toast) — `cancelPreparer` is a third cancel-button handler in this file that silently swallows where the established (and locally adjacent, in `cancelVoicelab`) convention is to toast.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — mirror `cancelVoicelab`'s `showToast('Cancel failed: ...', 'warning')` in `cancelPreparer`'s catch, closing out the F-052 pattern across all 5 of the file's cancel-button handlers.

### [F-080] Rule 8 — `loadVoicelabConfig`'s catch silently leaves all Voice Lab settings fields untouched on any fetch failure, with zero logging
- **Piece:** P34
- **Location:** `app/static/index.html:6407` (`loadVoicelabConfig`)
- **Severity:** low
- **Description:** `catch (e) { /* leave fields as-is */ }` discards any failure of `API.get('/api/voicelab/config')` (network error, 500, malformed JSON) with no `console.error`/`showToast`. On the page-load call this just leaves the Voice Lab settings form blank, which is hard to distinguish from a real failure; on the explicit refresh inside `saveVoicelabConfig`'s success path (line 6418's `await loadVoicelabConfig()`), a failure here means the just-saved settings silently fail to redisplay with no error shown, even though the save itself succeeded. Same recurring silent-swallow-on-fetch-failure pattern already logged for this file (F-056 `loadCharacterAliases`, F-061 `loadVoices`/`suggestVoices`, F-071 the opposite half for `loadDesignedVoices`) and for `app.py`'s analogous corrupted-config-load cases (F-046, for this exact `voicelab_config.json`, server-side).
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — add `console.error('Failed to load Voice Lab config:', e)` (matching the file's dominant convention elsewhere) so a fetch failure is at least visible in the browser console instead of looking identical to "nothing configured yet."

### [F-081] Rule 16 — `_vlChk` reads as an abbreviated noun, not a clear verb, despite mutating `innerHTML`
- **Piece:** P34
- **Location:** `app/static/index.html:6380` (`_vlChk`)
- **Severity:** low
- **Description:** `_vlChk(id, ok)` sets a readiness-checklist icon's `innerHTML` to a check-circle or times-circle glyph — a real side effect. "Chk" most naturally reads as a truncation of the noun "check(mark)" rather than the imperative verb "check," so unlike its neighbor `_vlSetRunning` (clearly verb-first, `Set`), a reader scanning this file's Voice Lab section can't tell from the name alone that `_vlChk` mutates the DOM rather than just returning a boolean. Narrow scope (private helper, 1 call site via `.forEach`) keeps the impact low.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — rename to `_vlSetCheckIcon` or similar so the verb leads, consistent with `_vlSetRunning`; not fix-now since renaming isn't in the fix-now criteria.

### [F-082] Rule 2 — Model id/path duplicated between `download_model.py` and `alexandria_preparer_rocm_compatible.py` with no shared constant
- **Piece:** P35 — download_model.py
- **Location:** `download_model.py:18,33,42` (`model_path`, `"openai/whisper-base"` x2) vs `alexandria_preparer_rocm_compatible.py:741-742` (`model_name = "openai/whisper-base"`, `local_model_path = os.path.join(script_dir, "models", "whisper-base")`)
- **Severity:** low
- **Description:** `download_model.py` is a standalone helper script (no callers in the repo other than its own `if __name__ == "__main__"`) whose entire purpose is to pre-populate `models/whisper-base` so `alexandria_preparer_rocm_compatible.py`'s ASR phase (line 742) can find and prefer it over the HuggingFace model id. Both the relative path `"models/whisper-base"` and the upstream model id `"openai/whisper-base"` are independently hardcoded as string literals in both files, with no shared constant or comment cross-referencing the other file. If the model were ever swapped (e.g. to `whisper-small`), one file could be updated and the other silently missed, since nothing ties them together.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — low priority given the two files run in different environments/contexts (one is a one-shot setup script, the other a pipeline phase); if addressed, a shared tiny constants module (or at least a comment in each file pointing at the other) would prevent silent drift.

### [F-094] Rule 15-candidate — `check_disk_space` duplicated between `alexandria_batch_processor.py` and `app/app.py` with a different signature, return type, and exception-handling policy
- **Piece:** P39a — alexandria_batch_processor.py (get_gpu_stats → check_disk_space)
- **Location:** `alexandria_batch_processor.py:230-249` (`check_disk_space(path, required_gb_per_file, num_files)`) vs `app/app.py:271-279` (`check_disk_space(path, required_gb)`)
- **Severity:** low
- **Description:** Same function name, same underlying decision ("is there enough free disk space at `path`?"), but the two versions disagree on every axis: signature (`app.py` takes a single pre-computed `required_gb`; this file takes `required_gb_per_file` × `num_files` and multiplies internally), return type (`app.py` returns `(bool, free_gb)` so callers get the actual free-space number; this file returns a bare `bool`, discarding `free_gb` after only logging it), and exception handling (`app.py` catches the narrow `(OSError, ValueError)` and logs via `logger.warning`; this file catches bare `Exception` and logs via `logger.debug`, i.e. effectively invisible at the default console log level set at line 35 (`ch.setLevel(logging.INFO)`)). Both fail open (return "has space" / `True` on check failure), so the safety-net intent agrees, but nothing ties the two implementations together — these run in genuinely different environments (`app.py` is the FastAPI server, this file is a standalone CLI orchestrator subprocess-launching the ROCm preparer) so an accidental-vs-legitimate-duplication call is left to the Task 4 cross-cutting pass per audit instructions.
- **Status:** logged
- **Suggested fix:** see needs-decision (tag only, not resolving) — if unified, prefer `app.py`'s `(bool, free_gb)` return shape since it doesn't discard information, and its narrower exception type.

### [F-095] Rule 8 — `get_gpu_stats`'s inner rocm-smi `except Exception` and outer `except Exception` both log only at `logger.debug`, invisible at the console's default INFO level
- **Piece:** P39a — alexandria_batch_processor.py (get_gpu_stats → check_disk_space)
- **Location:** `alexandria_batch_processor.py:90-92` (inner rocm-smi `except Exception as e: logger.debug(...)`) and `:94-96` (outer `except Exception as e: logger.debug(...); return None`)
- **Severity:** low
- **Description:** Both catch-alls in `get_gpu_stats` log via `logger.debug`, but the console handler set up at lines 33-37 is `ch.setLevel(logging.INFO)` — so any unexpected exception here (not just the three narrowly-anticipated `FileNotFoundError`/`TimeoutExpired`/`JSONDecodeError`/`ValueError` cases already handled by the more specific `except` blocks at lines 86-89) is recorded only to the file handler (`fh`, DEBUG level, line 28-29) and never appears on the console a user watching a live batch run would see. `log_gpu_stats` (the only caller, per the grep at lines 533/564/579) then just silently returns nothing for that interval with zero console indication that GPU stats collection itself failed vs. there being no GPU.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — bump the outer catch-all (line 94) to `logger.warning` so an unexpected GPU-stats failure is visible on console, distinct from the expected "no GPU" / "rocm-smi unavailable" cases which can stay at debug.

### [F-097] Rule 8 — `validate_files`'s `sys.exit(1)` paths (missing model / missing fallback model) skip `print_summary()` entirely, so no `batch_results_*.json` is written even though `self.results["skipped"]` may already hold real entries
- **Piece:** P39b — alexandria_batch_processor.py (class BatchProcessor → main)
- **Location:** `alexandria_batch_processor.py:320-329` (`validate_files`, the two `sys.exit(1)` calls) vs `:630-637` (`print_summary`, the only place `batch_results_*.json` is written)
- **Severity:** low
- **Description:** `validate_files` runs its per-file loop (lines 284-318, populating `self.results["skipped"]` for not-found/unsupported-format/already-processed files) *before* checking whether `self.model_path` or `self.fallback_model` exist (lines 320-329). If either check fails, the function calls `sys.exit(1)` directly — terminating the process immediately, before `run()` ever reaches `self.print_summary()` (the sole place `batch_results_*.json` gets written, line 630-637). The error is visible in the console/log (✓ fail-loud in that sense), but no on-disk JSON artifact records the batch attempt at all, including whatever skip reasons were already accumulated for files that *were* validated before the model-path check ran. Anything consuming `batch_results_*.json` as the authoritative record of "what did this invocation do" (e.g. a wrapper script, or a human checking after the fact) sees nothing for a model-path-typo run, even though partial validation work had already happened.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — either move the model-path/fallback-model existence checks to the start of `validate_files` (before the per-file loop) so the fast-fail happens before any wasted validation work, or write a minimal results JSON before the `sys.exit(1)` so there's always an artifact for the invocation.

### [F-099] Rule 2 — `apply_targeted_reset`'s `--also-clear-log` branch reimplements `remove_log_entries` inline instead of calling it
- **Piece:** P40a — alexandria_compare.py (load_jsonl → write_output)
- **Location:** `alexandria_compare.py:438-456` (inline block inside `apply_targeted_reset`) vs `:319-341` (`remove_log_entries(log_path, indices)`)
- **Severity:** low
- **Description:** The `also_clear_log` branch of `apply_targeted_reset` (lines 438-456) reimplements the exact same algorithm as the standalone `remove_log_entries` function line-for-line: open the log, skip blank lines, `json.loads` each line inside a `try`/`except json.JSONDecodeError: pass`, drop records whose `entry_idx` is in `indices` while counting removals, keep everything else, then rewrite the file. `remove_log_entries`'s own docstring even says it's "Used by in-session [u]ndo and by --reset-* flags" — but `apply_targeted_reset` (the function backing the `--reset-*` flags) doesn't actually call it; it has its own copy of the loop with a separate `log_removed` counter instead of reusing `remove_log_entries`'s return value. Confirmed via grep that `remove_log_entries` is otherwise called only once, from the in-session `[u]ndo` handler in `run()` (line 749, in P40b's range).
- **Status:** logged
- **Suggested fix:** replace the inline block at lines 438-456 with `log_removed = remove_log_entries(log_path, indices) if also_clear_log and log_path.exists() else 0` (the `if log_path.exists()` guard is already inside `remove_log_entries` itself, so the caller only needs to gate on `also_clear_log`).

### [F-100] Rule 8 — `log_decision`'s best-effort `except Exception: pass` gives the human reviewer zero indication that a decision failed to log
- **Piece:** P40a — alexandria_compare.py (load_jsonl → write_output)
- **Location:** `alexandria_compare.py:310-317` (`log_decision`)
- **Severity:** low
- **Description:** `log_decision`'s docstring explicitly documents the swallow as intentional ("Best-effort: a logging failure must never abort the user's review session") — this is a deliberate design choice, not an oversight, and the review log is supplementary (feeds the separate `alexandria-compare-review` pattern-mining workflow) rather than authoritative state, so silently continuing is defensible. However the `except Exception: pass` gives zero signal even to stderr/console that a decision's log record was lost — if the log file becomes unwritable (disk full, permissions) every subsequent decision in the session silently stops being recorded with no indication to the user reviewing entries in real time, and they would only discover it later when `alexandria-compare-review` or `remove_log_entries`/`find_last_manual_idx` find a thinner-than-expected log than the checkpoint's decision count.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — keep the swallow (don't abort the session) but print a one-time warning to console (e.g. via a module-level flag so it only fires once per session) the first time `log_decision` fails, so the user knows the log stopped tracking decisions.

### [F-101] Rule 17 — `run()`'s `decisions` parameter is mutated in place throughout the review loop rather than returned, and `main()` pre-populates the same dict before handing it to `run()`
- **Piece:** P40b — alexandria_compare.py (run → main)
- **Location:** `alexandria_compare.py:483-797` (`run`, every `decisions[key] = {...}` assignment and `decisions.pop(...)` at lines 585, 658, 672, 682, 698, 706, 716, 739) and `:1020` (`main()`'s pre-anchor block writing `decisions[str(i)] = {...}` before calling `run(decisions=decisions, ...)` at line 1079)
- **Severity:** low
- **Description:** `decisions` is a parameter `run()` receives from its caller (`main()`) and grows/shrinks throughout the function via direct key assignment and `.pop()`, with no return value — `run()` returns `None`. `main()` itself also mutates the same dict directly (pre-anchor auto-keep block, line 1020) before passing it to `run()`. This matches the dual-purpose-parameter shape Rule 17 calls out generally, but it's arguably closer to the documented exception (a) in spirit (a long-lived interactive session's working state, continuously checkpointed to disk via `save_checkpoint` as the authoritative record, rather than a single result being "reported back" to a caller that inspects the dict post-call) — `main()` never reads `decisions` again after `run()` returns; the call is the last statement in `main()`. Flagging per the audit instructions' explicit call-out to check this dict carefully, but the question of whether this qualifies as a Rule 17 violation vs. legitimate shared-session-state (closer to exception (a)/(c)) is left as a judgment call rather than resolved here.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — if treated as a violation, `run()` could return the final `decisions` dict (or a summary) explicitly rather than relying on in-place mutation of the caller's object, but since nothing currently reads it back, this would be a stylistic change with no behavior difference today.

### [F-102] Rule 9 — auto-approve checkpoint saves only every 200 entries, unlike every manual decision which saves immediately
- **Piece:** P40b — alexandria_compare.py (run → main)
- **Location:** `alexandria_compare.py:593-596` (auto-approve branch: `if auto_ct % 200 == 0: save_checkpoint(...)`) vs `:795-796` (manual-decision tail: unconditional `save_checkpoint(jsonl_path, decisions, cursor)` every iteration)
- **Severity:** low
- **Description:** Noting for completeness, not flagged as a safety-net violation: a crash or kill between two 200-entry checkpoint saves loses up to 199 already-auto-approved decisions from the on-disk checkpoint, while every manually-reviewed decision is checkpointed on the very next line after the decision is made. This asymmetry is intentional (manual decisions are irreplaceable human judgment worth saving immediately; auto-approvals are a deterministic function of `(chunk_text, cursor, threshold)` and will simply redo the same approval on a re-run with no data loss or behavior change) — documented here only so a future reader doesn't mistake the batched cadence for an oversight.
- **Status:** logged
- **Suggested fix:** none — auto-approvals are idempotent on re-run, so no fix needed; this entry exists to record that the asymmetry was reviewed and is intentional.

### [F-103] Rule 2 (tag, not fix-now) — duplicated 1↔1/1↔2/2↔1 best-of-three scoring block between the trailing and leading extension loops in `trim_span_to_alignment`
- **Piece:** P41a — alexandria_alignment.py (_expand_honorifics → trim_span_to_alignment)
- **Location:** `alexandria_alignment.py:402-440` (trailing extension loop) vs `:446-482` (leading extension loop)
- **Severity:** low
- **Description:** The two `while` loops inside `trim_span_to_alignment` are structurally near-identical: compute `s11`/`s12`/`s21` via `_char_sim`, pick the best of three via the same `max(...)` pattern, compute `step_th` via `_step_threshold`, then try the same four fallback tiers in the same order (`_num_eq_step_*`, `_one_to_N_*`, `_two_by_two_*`, `_lookahead_anchor_*`) gated by the same `if sim < step_th:` pattern, differing only in direction (`+`/`-` on indices) and which of the `_trailing`/`_leading` helper variants is called. This mirrors why the file already has paired `_trailing`/`_leading` helpers for every fallback tier (`_num_eq_step_trailing`/`_leading`, `_one_to_N_trailing`/`_leading`, `_two_by_two_trailing`/`_leading`, `_lookahead_anchor_trailing`/`_leading`) — the duplication pattern is consistent and deliberate throughout this file (each tier already pays the cost of two near-identical functions rather than one parameterized-by-direction function), so this is likely an accepted style tradeoff for readability/directness in a hot path, not an oversight. Tagging only, not proposing a fix, since unifying it would touch the core safety-net chain itself (Rule 9 territory) and the existing paired-helper pattern suggests this was a deliberate choice already made and repeated consistently.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — if Task 4's cross-cutting pass wants to address this, the cleanest unification would be a single direction-parameterized inner loop, but given every fallback tier already duplicates this same trailing/leading split as separate functions, doing so here alone (without also collapsing the eight `_*_trailing`/`_*_leading` helper pairs) would be inconsistent with the rest of the file's established pattern.

### [F-105] Rule 9 / Rule 16 — `find_anchor_position`'s `min_ratio` parameter only affects its search prefilter, never gates its own return value, unlike `realign`'s same-named parameter
- **Piece:** P41b — alexandria_alignment.py (_num_eq_step_trailing → merge_annotations_with_source)
- **Location:** `alexandria_alignment.py:754-804` (`find_anchor_position`, `min_ratio` used only at line 770 to compute `required_overlap`) vs `:829-893` (`realign`, `min_ratio` also gates the return at line 885 `if best_ratio < min_ratio: return cursor, cursor, best_ratio`)
- **Severity:** low
- **Description:** Both functions are adjacent tiers in the same alignment-recovery chain and share a `min_ratio: float` parameter with the same name and similar docstring framing ("confident match"), but their contracts differ: `realign` treats `min_ratio` as a hard gate — if the best ratio found is below it, `realign` returns the sentinel `(cursor, cursor, best_ratio)` signalling "no confident match, don't advance." `find_anchor_position` only uses `min_ratio` to compute the coarse-search overlap prefilter (`required_overlap = max(1, int(min_ratio * 0.6 * n))`); it always returns `best_start, best_start + n, max(0.0, best_ratio)` regardless of how low `best_ratio` actually is — there's no internal check against `min_ratio` before returning. Every current caller (`auto_anchor`, `run()`, `estimate_alignment_quality`) happens to re-check `ratio >= min_ratio` itself after the call, so this isn't causing a live bug today, but the asymmetry between two same-named parameters in the same fallback chain (one gates the return, one doesn't) is the kind of inconsistency that invites a future caller to assume `find_anchor_position` already filtered low-confidence results the way `realign` does, and skip the check.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — either make `find_anchor_position` also gate its return on `min_ratio` (returning a `(start, start, 0.0)`-style "no match" sentinel below the bar, matching `realign`'s contract) or rename the parameter to something like `overlap_ratio_hint` to make clear it only tunes the prefilter, not the result.

### [F-109] Rule 10 — fallback chain's failure interpretation is at least internally consistent (uniform broad `except Exception`), but doesn't distinguish OOM/missing-dependency/bad-audio before deciding to fall through
- **Piece:** P42b — alexandria_preparer_rocm_compatible.py (ASR transcription)
- **Location:** `alexandria_preparer_rocm_compatible.py:1011-1021` (Wav2Vec2 tier), `:1028-1037` (Insanely Fast Whisper tier), `:1044-1052` (WhisperX-CPU tier) — all three `except Exception as e:` blocks
- **Severity:** low
- **Description:** Noting for completeness per Rule 10's "decide before you retry" framing: all three tiers apply the exact same policy on failure — catch `Exception` broadly, `logger.warning`/`logger.error` the message, `logger.debug` the traceback, and unconditionally fall through to the next backend (or `sys.exit(1)` if it's the last tier). This is consistent across the chain — no tier reads the error differently or applies a different fallback decision based on *why* a backend failed. That said, the policy itself never distinguishes a transient/recoverable failure (e.g. one corrupt audio chunk crashing mid-loop) from a fundamental incompatibility (e.g. `ImportError`, OOM) — both are treated identically and trigger the same "abandon this backend entirely, move to the next" decision. Since the prompt's "decide before you retry" concern is about not changing interpretation *between* attempts of the *same* failure (which this code doesn't do — there's no retry-of-same-backend here, only fallthrough-to-different-backend), this is logged as a low-severity note rather than a violation: the consistency requirement is met, but the chain has no mechanism to retry a transient failure on the same backend before giving up on it, which is a design choice rather than an Rule-10 violation per se.
- **Status:** logged
- **Suggested fix:** none required for Rule 10 compliance as written; if backend-specific retry (e.g. retry once on a likely-transient subprocess timeout before falling through) is ever wanted, that would be a new feature, not a fix to existing inconsistency.

### [F-110] Rule 15 — Hyphen/dash compound-split regex duplicated verbatim between `_build_source_state` and `alexandria_compare.py`'s `main()`
- **Piece:** P42c — alexandria_preparer_rocm_compatible.py (source loading + chunking)
- **Location:** `alexandria_preparer_rocm_compatible.py:1228-1229` (`_build_source_state`, local `compound_split = re.compile(r'[-‐‑‒–—―─━]')`) vs `alexandria_compare.py:943-944` (`_COMPOUND_SPLIT = re.compile(r'[-‐‑‒–—―─━]')` inside `main()`)
- **Severity:** low
- **Description:** [rule15-candidate] Both files build `orig_display`/`orig_match` word lists from the source text by first splitting on the exact same dash/hyphen character class (including the U+2500 box-drawing-character special case) before calling `alignment.normalize()` per token. `_build_source_state`'s own comment says "Same logic as compare's main()", confirming the duplication is known/intentional rather than convergent coincidence — but the regex, its ordering relative to `normalize()`, and its accompanying comment are all copy-pasted across two files that both already import `alexandria_alignment.py` as their shared primitives module. A future change to the dash character set (e.g. adding another Unicode dash variant) requires remembering to update both call sites; only one is part of the audited piece, but the pattern itself fits Rule 15's "two independently-maintained copies will drift" framing already used for other findings in this log (e.g. F-004, F-015-style tags).
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — not resolved here per audit scope (tag only); candidate fix is hoisting this as a small shared helper (e.g. `alignment.split_compounds(text)` or a module-level `alignment._COMPOUND_SPLIT` pattern) in `alexandria_alignment.py`, used by both `_build_source_state` and `alexandria_compare.py`'s `main()`.

### [F-111] Rule 16 — `_provisional_entries_for_anchor` is adjective-first, not verb-first
- **Piece:** P42c — alexandria_preparer_rocm_compatible.py (source loading + chunking)
- **Location:** `alexandria_preparer_rocm_compatible.py:1096` (`_provisional_entries_for_anchor`)
- **Severity:** low
- **Description:** The function name leads with the adjective "provisional" rather than a verb describing what it does (it builds/packs a list of provisional chunk-entries from ASR `word_segments`, per its own docstring: "Pack the first N chunks' worth of ASR words into the entry shape..."). This is a minor outlier next to this file's other audited helpers in the same piece, which are correctly verb-first (`_build_source_state`, `_find_best_cut`). Low severity since the name is still unambiguous in context and is a private helper with one caller.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — a verb-first rename (e.g. `_build_provisional_entries_for_anchor` or `_pack_provisional_entries_for_anchor`) would match Rule 16 and the file's own established convention more closely; not renamed here since this is a read-only audit pass and the call site is single (`alexandria_preparer_rocm_compatible.py:3319`).

### [F-112] Rule 15 — Resolved: `find_best_match`/`realign`/`find_anchor_position` are imported, not duplicated, in this file
- **Piece:** P42d — alexandria_preparer_rocm_compatible.py (multi-tier alignment recovery)
- **Location:** `alexandria_preparer_rocm_compatible.py:63` (`import alexandria_alignment as alignment`), call sites `:2050` (`alignment.find_best_match`), `:2077` (`alignment.realign`), `:2099` (`alignment.find_anchor_position`) inside `annotate_chunks`
- **Severity:** low (documentation-accuracy, not a code defect)
- **Description:** Per this piece's explicit task to resolve the open duplication question: confirmed via `grep -n "^def find_best_match\|^def realign\|^def find_anchor_position"` that **zero** local definitions of these three names exist anywhere in `alexandria_preparer_rocm_compatible.py`. All three call sites use the `alignment.` module prefix, resolving to `alexandria_alignment.py`'s definitions (lines 685/754/829, confirmed in a prior pass). The file's own header comment at line 59 ("Shared alignment primitives (load_source, lexicon, find_best_match, ...)") corroborates this. The architecture skill's phase-7 table entry describing the script column as "same" for this file is imprecise wording — it should say "imported from alexandria_alignment.py" rather than implying a local duplicate. This is **not** a new Rule 15 violation and is distinct from the already-logged F-098 (which covers genuinely duplicated `parse_annotated_tokens`/`merge_annotations_with_source` between `alexandria_compare.py` and `alexandria_alignment.py` — an unrelated function pair).
- **Status:** needs-decision
- **Suggested fix:** update the alexandria-preparer-architecture skill's phase-7 table to say "imported from alexandria_alignment.py" instead of "same," so a future reader doesn't re-open this question. No code change needed — this file already follows Rule 15 correctly (single source of dispatch via the shared module) for these three functions.

### [F-120] Rule 9 (tag) — `_wipe_temp_dir` also fires whenever `--resume` is omitted, even when the `.source` marker matches the current audio file, which is broader than "confirmed mismatch"
- **Piece:** P42f — alexandria_preparer_rocm_compatible.py (resume/checkpoint/scratch-state)
- **Location:** `alexandria_preparer_rocm_compatible.py:1788-1797` (`annotate_chunks`'s `else` branch, reached whenever `not (resume and marker_matches)`); `--resume` defined at `:2879` (`action="store_true"`, default `False`)
- **Severity:** low
- **Description:** This piece's brief frames the wipe as gated on "source marker mismatch confirmed," but the actual condition reaching `_wipe_temp_dir` (`:1796`) is the boolean complement of `resume and marker_matches` — which is also true whenever the caller simply did not pass `--resume` (default), independent of whether the marker matches. In that sub-case (`not resume`, `marker_matches=True` — i.e., re-running on the exact same audio file without `--resume`), the code takes the generic `elif os.listdir(temp_dir): logger.info("Wiping stale dataset_temp/ contents for fresh start")` branch (`:1794-1795`), not the mismatch-warning branch (`:1789-1793`), and silently destroys all prior progress on the SAME book. This matches the documented CLI semantics (`--resume` help text: "Resume from existing dataset_temp/ instead of starting over" — resuming is opt-in, starting over is the default) and is not a code defect, but it means the wipe trigger is strictly broader than "mismatch confirmed," and a user who forgets `--resume` on a same-book re-run gets a full silent wipe with no mismatch warning at all — only the generic "stale contents" log line, which reads identically whether the prior run was stale garbage or hours of unfinished progress on the current book.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — purely a clarity/footgun question, not a logic bug: e.g. log a distinct, louder message specifically for the `not resume and marker_matches` sub-case ("dataset_temp/ contains N segments of unfinished progress on THIS source — pass --resume to continue, or this run will discard them") so the wipe-without-mismatch case is distinguishable from genuinely stale/foreign contents.

---

## Task 4: Rule 15 cross-cutting pass

All `[rule15-candidate]`/Rule-15 tags from the 54-piece sweep, plus a fresh repo-wide grep for the same decision patterns (to catch anything an individual piece's narrower scope missed). Grouped by the underlying decision. No code changes made in this task — every cluster below is `needs-decision`, consistent with Rule 9/14 (cross-file consolidation requires judgment and user approval, never auto-fixed).

### Cluster A — "Is this LLM endpoint remote?"
**Canonical helper:** `lmstudio_settings.is_remote_llm(llm_mode, base_url)` (`app/lmstudio_settings.py:45-52`), written specifically to handle `llm_mode`/`base_url` drift (see its own docstring).

| Call site | Uses canonical helper? |
|---|---|
| `app/app.py:1572` (`lmstudio_status`) | Yes |
| `app/app.py:1600` (`lmstudio_optimize`) | Yes |
| `app/app.py:1856` (`save_config`, picking which submitted profile — `llm_remote` vs `llm_local` — to mirror into the active `llm` config) | No, but **likely not a violation** — re-checked during this pass: this is selecting between two profile *objects the user just submitted in this request*, not deciding whether a resolved endpoint is remote; `is_remote_llm` answers a different question (and `base_url` may be ambiguous mid-save). Closing as a false-positive-on-inspection, no action needed. |
| `app/llm_bench.py:167,179,183` (`get_cached_or_benchmarked_concurrency`) — **F-004** | No — flagged in original sweep, confirmed real (`base_url` is in scope and unused) |
| `app/static/index.html` `confirmIfRemote`/`testLlmConnection` — **F-049** | No — frontend has no access to a backend-computed drift-aware value at all; `/api/config` never exposes one |

**Resolution:** F-004 is the one clear violation — `llm_bench.py` has `base_url` in scope and should call `is_remote_llm(llm_mode, base_url)` instead of `llm_mode == "remote"` at all three sites. F-049 is a smaller, lower-leverage gap (frontend-only cost-warning gate, not a routing decision) — fixing it well would mean `/api/config` starts returning the drift-aware computed value, which is a small API change, not a one-line fix. `app/app.py:1856` is closed, not a violation.

### Cluster B — "Self-heal/check ideal LM Studio settings before running"
**Canonical pattern:** `ensure_ideal_settings(...)` from `app/lmstudio_settings.py`, called by exactly 2 of the 4 LLM-driving scripts in `app/`:

| Script | Calls `ensure_ideal_settings`? |
|---|---|
| `app/review_script.py:823` | Yes |
| `app/find_nicknames.py:326` | Yes |
| `app/generate_script.py` — **F-007** | No |
| `app/generate_personas.py` — **F-012** | No |

**Resolution:** confirmed via repo-wide grep this is exactly a 2-of-4 split, not a wider problem. `generate_script.py` and `generate_personas.py` are the two outliers — both should plausibly call `ensure_ideal_settings` at the same point in their startup as their two siblings do, for the same reason (VRAM-safety/remote-detection self-heal before LLM work begins). This is a `needs-decision` add-the-missing-call fix, not a refactor — flagging as the single highest-value, lowest-risk Rule 15 fix in this whole pass.

### Cluster C — `format_duration` (3 independent implementations, one diverging pair)
| File | Behavior |
|---|---|
| `alexandria_batch_processor.py:114` | No negative-clamp; always shows seconds even with hours |
| `alexandria_preparer_rocm_compatible.py:287` | Clamps negatives to 0; drops seconds once hours are present |
| `app/static/index.html:6039` `formatDuration` | Frontend-only, different context (UI display, not log lines) — not part of the same drift risk since it never shares a log stream with the other two |

**Resolution (F-093):** the first two genuinely interleave in the same log stream (the batch processor launches the preparer as a subprocess) and produce visibly different formatting for the same duration — confirmed still drifted. `index.html`'s copy is unrelated (different process, different purpose) and is not part of this cluster's risk, even though it's nominally "the same decision."

### Cluster D — `check_disk_space` (2 implementations, different contract)
`alexandria_batch_processor.py:230` vs `app/app.py:271` — confirmed (F-094) these are the only two definitions in the repo. Different signature, return type, and exception narrowness, as already documented. Both fail open. Genuinely separate processes/environments (CLI orchestrator vs. FastAPI server) — a real Rule 15 case, but consolidation would require introducing a shared module both environments can import, which is a larger structural change than a fix-now edit.

### Cluster E — Alignment/annotation token-parsing duplication (already fully resolved during the sweep)
- `parse_annotated_tokens` + `merge_annotations_with_source`: verbatim-duplicated between `alexandria_compare.py:72-156/159-253` and `alexandria_alignment.py:985-1069/1072-1166` (**F-098**, confirmed from both sides).
- Compound-split regex: duplicated between `alexandria_preparer_rocm_compatible.py`'s `_build_source_state` and `alexandria_compare.py`'s `main()` (**F-110**).
- `find_best_match`/`realign`/`find_anchor_position`: **closed, not a duplicate** (**F-112**) — `alexandria_preparer_rocm_compatible.py` imports these from `alexandria_alignment.py` (`import alexandria_alignment as alignment`, line 63); the architecture skill's phase-table wording ("same") was just imprecise and should be corrected to say "imported from alexandria_alignment.py."

**Resolution:** `alexandria_compare.py` and `alexandria_alignment.py` already share `alexandria_alignment.py` as an import dependency in the opposite direction is not established — confirmed `alexandria_compare.py` does NOT `import alexandria_alignment`, it has its own inline copies. Since `alexandria_preparer_rocm_compatible.py` already proves importing `alexandria_alignment.py` works fine from a sibling root script, the cleanest fix for F-098/F-110 is for `alexandria_compare.py` to import these three from `alexandria_alignment.py` too, the same way the preparer already does, rather than maintaining inline forks.

### Cluster F — Internal self-inconsistency (not cross-file, but same root cause: one copy claims to mirror another and doesn't)
**F-104** — `alexandria_preparer_rocm_compatible.py`'s `estimate_alignment_quality` docstring claims to mirror `annotate_chunks`'s own recovery-chain gating, but the entry-gating condition differs (drops short chunks entirely vs. only gating one tier). Not a cross-*file* duplication, but the same underlying problem Rule 15 cares about: two copies of one decision, one drifting silently because nothing keeps them in sync.

### Cluster G — index.html-internal duplications (lower priority, single-file, already fully documented)
F-062 (`submitCastApply`/`submitCastApplyBulk` checkbox→mapping extraction), F-065 (`renderAll`/`renderBatchFast` ~95%-identical polling logic, with the Rule 10 confirm-gate gap from F-064 living in the part that differs), F-072 (`pollLogs` alone has stale-response protection among ~10 hand-rolled pollers). These are all single-file (`index.html`) maintenance-burden duplications rather than cross-process drift risks — lower severity than Clusters A-E, included here for completeness since they were tagged `[rule15-candidate]` during the sweep.

**Summary for Task 5:** of the 7 clusters, **B (missing `ensure_ideal_settings` calls)** is the clearest, lowest-risk, highest-value fix. **A's `llm_bench.py` branch (F-004)** is the clearest pure-refactor swap-to-canonical-helper fix. The rest (C, D, E, F, G) require either a new shared module across environments that don't currently share one, or accepting the duplication as a documented tradeoff — genuine `needs-decision` judgment calls for the user, not mechanical fixes.

---

## Task 5: Synthesis report

**Totals:** 120 findings across all 54 pieces. 5 high severity, 41 medium, 74 low. 10 fixed-inline (all Rule 18 brace additions + 1 dead-code deletion in `app/generate_personas.py` + 1 dead-code deletion in `alexandria_preparer_rocm_compatible.py`), 14 logged/closed (informational or false-positive-on-inspection, no action needed), **96 needs-decision** (require a judgment call before any fix lands, per this audit's fix policy).

**By rule:** Rule 8 (Fail Loud) 41 · Rule 9 (Safety Nets) 21 · Rule 2 (Simplicity) 18 · Rule 15 (Single Dispatch) 11 · Rule 18 (JS braces) 8, all fixed · Rule 10 (Retry Consistency) 7 · Rule 16 (Verb Naming) 7 · Rule 17 (Dual-Purpose Params) 4 · Rule 12 (Test Rigor) 3.

### The 5 high-severity findings

1. **F-032** — `POST /api/chunks/{index}/generate` runs real TTS/GPU inference with zero GPU-lock check or `process_state` entry at all.
2. **F-043** — `POST /api/dataset_builder/generate_sample` same gap, plus isn't registered in `process_state["dataset_builder"]` at all (its sibling `generate_batch` is correctly registered).
3. **F-092** — `alexandria_batch_processor.py`'s `_normalize_filename_tokens` calls `re.findall` but `re` is never imported — guaranteed `NameError` the moment the documented fuzzy-source-match fallback path is actually exercised.
4. **F-115** — The final dataset ZIP in the preparer pipeline is written directly to its destination path with no scratch-then-rename; a crash mid-write leaves a truncated/corrupt zip at the permanent output path, and a re-run destroys any prior good zip before the new one is confirmed good.
5. **F-119** — `_wipe_temp_dir`'s per-file removal is best-effort, not atomic: one locked/undeletable file silently produces a partial wipe — the exact cross-book corruption scenario the source-marker/wipe system exists to prevent, reintroduced at file-removal granularity.

### Two systemic patterns (each one fix-pattern resolves multiple findings)

**Pattern 1 — Single-item/synchronous GPU routes skip the GPU lock that their batch siblings correctly use (6 findings, 2 high + 4 medium):** F-029 (`lmstudio/optimize`), F-032 (chunk generate, high), F-038 (voice_design/preview), F-039 (lora/test), F-040 (lora/preview), F-043 (dataset_builder/generate_sample, high). Every one of these is a "preview"/"test"/single-item GPU route that races `GPU_TASKS` because it was apparently written by analogy to its read-only siblings rather than its GPU-heavy batch sibling. Same fix shape every time: add `check_global_gpu_lock`/`claim_gpu_task` (or for synchronous-not-backgrounded ones, at least the check) around the GPU call, matching the pattern every batch-equivalent route already uses correctly.

**Pattern 2 — Corrupted JSON state files are silently reset to empty/default, then the next save overwrites the file, permanently losing all prior data (6 findings, all medium/low):** F-011 (`manifest.json`), F-015 (alias registry), F-031 (`GET /api/voices`), F-035 (`voice_library.json`), F-036 (designed-voice/clone manifest), F-046 (`voicelab_config.json`). Every one of these is the same shape: `except Exception: <reset to empty>` with no logging, in a `_load_*` helper whose result later gets unconditionally written back to disk. Same fix shape every time: log the parse failure (so a human notices before the silent overwrite happens) and/or refuse to overwrite a file that failed to parse until a human confirms starting fresh is intended.

**See Task 4 above** for the third systemic pattern (Rule 15 dispatch duplication, 7 clusters) and its own recommended fixes — the clearest of which is **Cluster B**: `generate_script.py`/`generate_personas.py` are missing the same `ensure_ideal_settings` self-heal call their siblings `review_script.py`/`find_nicknames.py` already have.

**Frontend poller inconsistency (4 findings, no single canonical answer yet):** F-058, F-073, F-079 (tagged under Task 4 alongside F-072) collectively show `index.html`'s ~10 hand-rolled `setInterval` pollers split three ways on what a poll error means — retry forever silently, give up immediately with no toast, or give up immediately with a toast. No fix-now shape here since the three policies are genuinely different *design choices*, not one obviously-correct pattern accidentally not applied everywhere — this needs a decision on which policy is right before any fix.

### Everything else
The remaining ~100 findings are one-off `needs-decision` items (a missing read-back in a test, a duplicated regex, a misnamed function, a dual-purpose parameter) — full detail and exact locations are in this file under each piece's section above, searchable by `F-###` ID.
