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

### [F-004] Rule 15 — `get_cached_or_benchmarked_concurrency` branches on raw `llm_mode == "remote"` instead of `is_remote_llm(llm_mode, base_url)`
- **Piece:** P05 — app/llm_bench.py
- **Location:** `app/llm_bench.py:167,179,183` (`get_cached_or_benchmarked_concurrency`)
- **Severity:** medium
- **Description:** [rule15-candidate] This function takes both `llm_mode` and `base_url` as parameters, but its three remote/local branch points (`profile_key` selection at line 167, the `status` fetch at line 179, and the GPU-probe/label selection at line 183) all test `llm_mode == "remote"` directly rather than calling `lmstudio_settings.is_remote_llm(llm_mode, base_url)`. `is_remote_llm`'s own docstring in `app/lmstudio_settings.py` exists specifically because `llm_mode` alone "misses... a save_config edge case where llm_mode and the active base_url have drifted out of sync" — this function has `base_url` in scope and could hit exactly that drift, picking the wrong cache profile / status-fetch path / GPU probe.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — not resolved here per audit scope; flagging for the Rule 15 cross-cutting pass (Task 4 in PROGRESS.md).

### [F-005] Rule 8 — `tts_vram_benchmark.py`'s VRAM/duration probes swallow all exceptions identically to "no GPU" and feed silently-wrong numbers into the tuning recommendation
- **Piece:** P07 — app/tts_vram_benchmark.py
- **Location:** `app/tts_vram_benchmark.py:70-81` (`vram_state`), `:83-88` (`gpu_name`), `:119-127` (duration-estimation block in `run_sweep`)
- **Severity:** medium
- **Description:** All three sites use `except Exception: return None` / `return "unknown"` / `pass`, with zero logging. `vram_state()` returns `None` both when there's genuinely no CUDA device (`torch.cuda.is_available()` is `False` — expected) and when `torch.cuda.mem_get_info()` itself raises for any other reason (a real bug) — both cases produce the identical `None`. In `main()`, `model_vram_gb = (snap_post["allocated_gb"] - snap_pre["allocated_gb"]) if (snap_pre and snap_post) else 0` silently substitutes `0` for the model's actual VRAM footprint if either snapshot failed, and that `0` then flows straight into `print_summary`'s headroom math and the "Tier table recommendation" that's meant to be pasted into `index.html`'s `_computeAutoSettings`. Likewise the duration-estimation `except Exception: pass` leaves `total_audio = 0.0` on any `soundfile` failure (missing import, corrupt WAV, etc.), silently producing `rtf = None` for every row instead of surfacing that the duration measurement itself failed.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — this is a dev/tuning script whose entire output is meant to be trusted and pasted into production tier tables, so at minimum print the exception type/message at each site before falling back, so a "no GPU" run is distinguishable from a "GPU present but probe broke" run.

### [F-006] Rule 8 — `salvage_json_entries`'s bare `except Exception: continue` silently drops malformed entries
- **Piece:** P08 — app/generate_script.py
- **Location:** `app/generate_script.py:163-172` (`salvage_json_entries`), specifically the `except Exception: continue` at line 171
- **Severity:** medium
- **Description:** Flagged by a prior 2025-06-13 code review and confirmed still present. This is the last-resort regex-salvage path inside `call_llm_for_entries`'s retry loop — when `repair_json_array` has already failed, each individually regex-matched candidate entry is `json.loads`'d inside a bare `except Exception: continue` with no logging of which match failed or why. The caller (`call_llm_for_entries`) prints `Regex-salvaged {len(salvaged_entries)} entries...` only when `salvaged_entries` is non-empty/truthy — if every candidate match fails to parse, the function returns `None`, the caller falls through to `return []` for the whole chunk, and nothing in the output indicates that salvage was even attempted, let alone that every candidate failed. A batch-level caller (`main()`) sees a chunk that produced 0 entries and a generic "Could not parse" warning higher up, but the specific salvage-stage failures are invisible.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — at minimum log the exception (e.g. `print(f"  [salvage] discarding malformed candidate: {e}")`) before `continue`, so a parse failure here is distinguishable from "no candidates matched the regex at all."

### [F-007] Rule 15 — `generate_script.py` never self-heals or checks remote LM Studio settings, unlike its sibling scripts
- **Piece:** P08 — app/generate_script.py
- **Location:** `app/generate_script.py:412-478` (`main()`)
- **Severity:** medium
- **Description:** [rule15-candidate] `generate_script.py`'s `main()` reads `llm_mode`/`base_url` from config and constructs an `OpenAI` client directly, but never calls `ensure_ideal_settings` / `is_remote_llm` / `get_cached_or_benchmarked_concurrency`. Its two closest siblings in the same pipeline both do: `review_script.py:822-830` calls `ensure_ideal_settings(llm_mode, base_url, model_name, ...)` then `get_cached_or_benchmarked_concurrency(...)`, and `find_nicknames.py:326-339` does the identical pair of calls. This means `generate_script.py` runs with no self-heal for a restarted/misconfigured remote LM Studio instance and no remote-aware concurrency — it always effectively runs single-call. Whether this is intentional (script-gen chunks are processed strictly sequentially today, so no per-call context-budget math is needed) or an oversight is unclear without product context.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — confirm whether `generate_script.py` should call `ensure_ideal_settings`/`get_cached_or_benchmarked_concurrency` like `review_script.py` and `find_nicknames.py` do, for consistency and so a stale/unreachable remote LM Studio surfaces the same self-heal message instead of a raw connection-error traceback.

### [F-008] Rule 2 — `train_lora.py` re-implements `resolve_device`/`enable_rocm_optimizations` already present as `TTSEngine` methods
- **Piece:** P09 — app/train_lora.py
- **Location:** `app/train_lora.py:59-65` (`resolve_device`), `app/train_lora.py:68-82` (`enable_rocm_optimizations`) vs `app/tts.py:333-345` (`TTSEngine._resolve_device`), `app/tts.py:349-374` (`TTSEngine._enable_rocm_optimizations`)
- **Severity:** low
- **Description:** Both pairs of functions do the same thing: resolve `"auto"` to `cuda`/`mps`/`cpu` (train_lora.py's version omits the `mps` branch that `tts.py`'s has), and set the same three ROCm env vars (`MIOPEN_FIND_MODE`, `MIOPEN_LOG_LEVEL`, `FLASH_ATTENTION_TRITON_AMD_ENABLE`) plus the same `triton_key` shim guarded by `hasattr(torch.version, "hip")`. `train_lora.py` runs as an independent subprocess (this repo's `app/env` doesn't have the ROCm ML stack — see CLAUDE.md's cross-repo Voice Lab note), so it can't simply `import tts.TTSEngine`, but the logic itself is copy-pasted rather than shared, and the two have already drifted (the `mps` branch is missing from `train_lora.py`'s version).
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — if a shared module is impractical given the separate-env constraint, consider extracting just these two functions into a tiny dependency-free helper module both files import (`device_utils.py` or similar) rather than refactoring across the ROCm-env boundary.

### [F-009] Rule 8 — Per-sample OOM skips during training are logged live but never aggregated into the run's durable output
- **Piece:** P09 — app/train_lora.py
- **Location:** `app/train_lora.py:556-564` (OOM handler in `train()`'s per-sample loop) vs `:642-660` (`training_meta.json` write)
- **Severity:** low
- **Description:** Each OOM is printed (`[TRAIN] OOM at epoch={epoch} step={step_idx}, skipping sample`) at the moment it happens, so it isn't fully silent — but unlike `load_dataset`'s `skipped_missing`/`skipped_too_long` counters (which are tallied and printed in a `[DATA] Prepared N samples (M skipped: ...)` summary), no equivalent counter exists for OOM-skipped samples across the whole training run. `training_meta.json` (the one artifact consumers reach for after the run, e.g. the Voice Lab pipeline) has no `oom_skips` field, so a run where e.g. 30% of samples silently OOM'd every epoch looks identical in the metadata to a clean run — only a full scrollback through stdout/the captured log would reveal it.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — track a running OOM-skip counter (per-epoch and/or total) and include it in the `[EPOCH]` summary line and `training_meta.json`, mirroring the existing `load_dataset` skip-counter pattern.

### [F-010] Rule 2 — `parse_alias_decision` has zero callers anywhere in the repo
- **Piece:** P10 — app/generate_personas.py
- **Location:** `app/generate_personas.py:285-301` (`parse_alias_decision`)
- **Severity:** low
- **Description:** `grep -rn "parse_alias_decision" .` (no file-type filter) returns only the function's own definition line. Nothing in `app/generate_personas.py` itself, `app/app.py`, `app/static/index.html`, or any other file calls it. It appears to be a leftover from an earlier per-speaker alias-decision flow (the file's actual alias resolution today goes through `_resolve_to_canonical` and `_resolve_aliases_batch` instead, both of which are called). No CLI flag, route, or test references it either, so this isn't a dormant external entrypoint.
- **Status:** fixed-inline (commit `36e3277`)
- **Suggested fix:** delete the function; confirmed zero callers, meets fix-now criteria.

### [F-011] Rule 8 — Corrupted `manifest.json` is silently reset to empty list with no logging
- **Piece:** P10 — app/generate_personas.py
- **Location:** `app/generate_personas.py:518-522` (inner `try/except Exception: manifest = []` in `_save_generated_preview`)
- **Severity:** medium
- **Description:** When `designed_voices/manifest.json` exists but fails to parse (corrupt JSON, truncated write, etc.), the inner `except Exception: manifest = []` discards the parse error with zero logging and proceeds as if the manifest were simply empty. Every previously tracked manifest entry for every other speaker is then lost the moment the next speaker's preview is saved (the function rebuilds `manifest` from this now-empty list and overwrites the file via `_atomic_json_write(manifest, manifest_path)` at line 551). This is silent data loss disguised as the normal "no manifest yet" case — the outer `except Exception as e: print(f"Warning: could not update manifest for {speaker}: {e}")` at line 552 only fires for errors in the surrounding block, not for this already-caught inner one.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — log the parse failure distinctly from the legitimate "file doesn't exist yet" case (e.g. `print(f"Warning: manifest.json corrupted, rebuilding from scratch: {e}")`) so the data loss is visible instead of indistinguishable from first-run behavior.

### [F-012] Rule 15 — `generate_personas.py` never self-heals or checks remote LM Studio settings, unlike `review_script.py`/`find_nicknames.py`
- **Piece:** P10 — app/generate_personas.py
- **Location:** `app/generate_personas.py:739-747` (`main()`, LLM client construction)
- **Severity:** medium
- **Description:** [rule15-candidate] Same gap as F-007 (`generate_script.py`), found independently in a third sibling script. `main()` reads `llm_cfg.get("base_url", ...)` directly from config and constructs an `OpenAI` client with no call to `lmstudio_settings.ensure_ideal_settings` / `is_remote_llm` / `llm_bench.get_cached_or_benchmarked_concurrency`, unlike `review_script.py:822-830` and `find_nicknames.py:326-339`, which both call `ensure_ideal_settings(llm_mode, base_url, model_name, ...)` then fetch a concurrency value before making LLM calls. `generate_personas.py` makes many sequential per-speaker/per-batch LLM calls (`_resolve_aliases_batch`, `_discover_batch_characters`, `_compile_persona`, the simple-mode per-speaker loop) with no self-heal for a stale/restarted remote LM Studio and no remote-aware concurrency, so a misconfigured remote endpoint surfaces as a raw connection error per call instead of the shared heal-and-retry path.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — same as F-007: confirm whether this script should adopt the `ensure_ideal_settings`/`get_cached_or_benchmarked_concurrency` pair for consistency with its pipeline siblings.

### [F-013] Rule 8 — Corrupted/missing per-chunk audio files are silently dropped from exports with no skipped-count reported
- **Piece:** P11 — app/project.py
- **Location:** `app/project.py:453-469` (`_load_chunks_with_audio`), consumed by `merge_audio` (`:471-491`), `export_audacity` (`:493-572`), and `merge_m4b` (`:574-670`)
- **Severity:** medium
- **Description:** `_load_chunks_with_audio` silently `continue`s past any chunk whose `audio_path` is missing/empty, whose file doesn't exist on disk, or whose `AudioSegment.from_file` load raises (caught by a bare `except Exception as e: print(...)` with no re-raise) — and returns whatever subset successfully loaded. All three exporters (`merge_audio`, `export_audacity`, `merge_m4b`) consume this subset directly and return `(True, <output>)` on success with no comparison against `len(self.load_chunks())` or a count of chunks with `status == "done"`. A book where, say, 5 of 200 chunks have corrupted/missing audio produces a "successful" export that is silently missing those 5 chunks' narration — the caller (`app.py`'s export routes) has no signal to surface to the user that anything was dropped.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — have `_load_chunks_with_audio` also return (or have callers compute) a skipped count/list, and surface it in each exporter's success message (e.g. `"audiobook.m4b (3 chunks skipped — see logs)"`) instead of a bare success.

### [F-014] Rule 17 — `_finalize_completed_chunk`/`_record_batch_failures` mutate the caller's `results` accumulator instead of returning outcomes
- **Piece:** P11 — app/project.py
- **Location:** `app/project.py:946-975` (`_finalize_completed_chunk`), `:977-990` (`_record_batch_failures`), called from `generate_chunks_batch` (`:1088-1092`)
- **Severity:** low
- **Description:** Both helpers take the caller's `results` dict (`generate_chunks_batch`'s `{"completed": [], "failed": [], "cancelled": 0}` accumulator, built once at the top of the function and threaded across the whole `while pending:` loop) and append to it directly to report success/failure, rather than returning a value for the caller to fold in. This doesn't match CLAUDE.md's documented Rule 17 exceptions: it isn't `process_state`-style concurrently-shared state (the batch loop is single-threaded), and neither helper has a documented mutator-callback contract like `_modify_chunk`'s `mutator(chunk)`. It's also inconsistent with the directly analogous parallel-path helper two methods away in the same class: `generate_chunks_parallel`'s inner `_run_round` (`:780-815`) does the identical job (track completed/oom/hard-failed across a round of chunk generation) but builds fresh local lists and explicitly `return`s `(completed, oom_failed, hard_failed, was_cancelled)` for the caller to extend into its own `results`. `_finalize_completed_chunk` additionally mutates `chunks[idx]` in place (setting `status`/`audio_path`) with no return value at all, so the only way to know what it did is to inspect the dict it was handed.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — have `_finalize_completed_chunk` return `("completed", idx, audio_path)` / `("failed", idx, error)` and `_record_batch_failures` return its existing `oom_failed` plus a list of hard-failure tuples, and have `generate_chunks_batch` fold those into its own `results`/`chunks`, mirroring `_run_round`'s existing return-based pattern in the same file.

### [F-015] Rule 8 — `dedupe_speakers`'s corrupted alias-registry load is silently reset to `{}`, then the registry file is overwritten and prior cross-book aliases are lost
- **Piece:** P12a — app/review_script.py
- **Location:** `app/review_script.py:362-368` (registry load in `dedupe_speakers`), persisted at `:470-478`
- **Severity:** medium
- **Description:** When `registry_path` (the cross-book canonical-alias file, e.g. populated by `find_nicknames.py`) exists but fails to parse (`json.JSONDecodeError`/`ValueError`/`OSError`), `except (...): registry = {}` discards the error with zero logging and proceeds as if no registry existed yet. At the end of the same call, `registry` (now empty) is updated with only this run's `clean_map` and written back via `atomic_json_write(registry, registry_path)` (lines 470-478), permanently erasing every other book's previously recorded alias→canonical entries — indistinguishable from the legitimate first-run "no registry yet" case. Same pattern and severity as F-011 (`generate_personas.py`'s `manifest.json` load).
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — log the parse failure distinctly (e.g. `print(f"  Warning: alias registry {registry_path} corrupted, rebuilding from scratch: {e}")`) so the data loss is visible instead of silently indistinguishable from "no registry yet".

### [F-016] Rule 17 — `dedupe_speakers` mutates the caller's `entries` list in place while also returning a result describing the same effect
- **Piece:** P12a — app/review_script.py
- **Location:** `app/review_script.py:347-480` (`dedupe_speakers`), specifically the in-place rename loop at `:453-461`
- **Severity:** low
- **Description:** `dedupe_speakers(client, model_name, entries, ...)` mutates `entries` (renaming `e["speaker"]`/`e["type"]` in place per its own docstring, "Applies the mapping to `entries` in place") *and* returns `(mapping, renamed_count)` to report what it did — using both the input parameter and a return value to communicate the same outcome. `entries` is `all_corrected`, passed in from `main()` (P12b, around line 1125) and used immediately afterward by the same caller — this is a cross-function handoff, not a function mutating its own local variable, so CLAUDE.md's Rule 17 exception (c) doesn't apply; it also isn't concurrently-shared state (exception a) or a documented mutator-callback contract (exception b). Matches the pattern already logged as F-014 for `project.py`'s `_finalize_completed_chunk`.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — have `dedupe_speakers` return `(mapping, renamed_count, updated_entries)` (or a list of `(index, new_label)` changes) and let `main()` apply them, rather than mutating the list it was handed.

### [F-017] Rule 8 — `_remap_voice_config` swallows a failed config write and still reports a non-zero "moved" count to the caller
- **Piece:** P12a — app/review_script.py
- **Location:** `app/review_script.py:520-524` (`_remap_voice_config`)
- **Severity:** medium
- **Description:** After mutating `cfg` in memory to merge/move renamed-speaker entries, `try: atomic_json_write(cfg, voice_config_path) except OSError: pass` discards a write failure with zero logging, but the function still `return`s `moved` (a count computed before the write was attempted) regardless of whether the write succeeded. `main()` (P12b, `app/review_script.py:1130-1132`) takes this return value at face value and prints `"Remapped {moved} voice config entr(y/ies) to canonical names."` even when the underlying file was never actually updated — a disk-full/permission-denied failure produces a successful-looking log line while the voice config silently still has the old (now-renamed) speaker keys.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — log the `OSError` (e.g. `print(f"  Warning: failed to write {voice_config_path}: {e}")`) and either return `0` or propagate the failure so `main()`'s success message isn't printed for a write that didn't happen.

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

### [F-022] Rule 12 — `test_save_voice_config` never reads back to confirm persistence, unlike every analogous config/script test
- **Piece:** P14a — app/test_api.py (helpers → chunk tests)
- **Location:** `app/test_api.py:451-463` (`test_save_voice_config`)
- **Severity:** medium
- **Description:** Every other write-then-verify test in this range (`test_save_config_roundtrip`, `test_save_pause_config_roundtrip`, `test_save_review_prompts_roundtrip`, `test_save_persona_prompts_roundtrip`, `test_save_script`+`test_list_scripts`+`test_load_script`) follows the POST with a `GET` to confirm the value was actually persisted to disk, not just echoed back in the POST response. `test_save_voice_config` (line 451) only asserts `data.get("status") == "saved"` from the POST response itself and never calls `GET /api/voices` to check that `_test_voice` actually landed in `voice_config.json` with the fields it sent (`type`, `voice`, `character_style`, `seed`). A handler that returns `{"status": "saved"}` without writing anything (or writing the wrong shape) would still pass. It also never cleans up the `_test_voice` entry it creates, unlike the chunk tests in the same range which all restore original state.
- **Status:** logged
- **Suggested fix:** add a follow-up `GET /api/voices`, assert the `_test_voice` entry exists with the expected fields, and delete/restore it afterward to match the cleanup convention used by the surrounding tests.

### [F-023] Rule 10 — `wait_for_task` treats a non-200 poll response identically to "still running," with no distinction surfaced on timeout
- **Piece:** P14a — app/test_api.py (helpers → chunk tests; helper is defined here, called from P14b at lines 1093/1101)
- **Location:** `app/test_api.py:80-88` (`wait_for_task`)
- **Severity:** low
- **Description:** The polling loop's continue condition is `if r.status_code == 200 and not r.json().get("running")`, so a transient server error (500), a route that doesn't exist for that task name, or a normal in-progress `running: true` response all fall through to the same `time.sleep(poll_interval)` and the loop just keeps polling — interpretation of "not done yet" is at least consistent across iterations (no Rule 10 inconsistency within a single call), but the eventual `return False` on timeout gives the caller (`test_generate_batch`/`test_generate_batch_fast` in P14b) no way to tell "task is still legitimately running after 120s" apart from "the status endpoint has been erroring the whole time." A caller only sees a bare `TestFailure` either way.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — track and surface the last non-200 status/last response body when returning `False`, so a timeout caused by a broken status endpoint is distinguishable from a genuinely slow task.

### [F-024] Rule 12 — Every async-task-starting test in the Generation/LoRA/Dataset-Builder sections only checks `status == "started"`, never the task's actual outcome
- **Piece:** P14b — app/test_api.py (status/preparer/voicelab/lora/dataset-builder/audio tests)
- **Location:** `app/test_api.py:1052-1129` (`test_generate_script`, `test_review_script`, `test_generate_chunk`, `test_generate_batch`, `test_generate_batch_fast`, `test_export_audacity`), `:1146-1160` (`test_lora_generate_dataset`), `:1163-1184` (`test_dataset_builder_generate_sample`)
- **Severity:** medium
- **Description:** 8 of the 9 `requires_full=True` tests (every one that kicks off a background task) assert only that the POST response says `status == "started"` (or, for `test_generate_chunk`/`test_dataset_builder_generate_sample`, just `assert_status(r, 200)` / key-presence with `assert_key(data, "status")`, not even a value check). None of them poll the corresponding `/api/status/<task>` to completion and assert the task actually finished successfully or produced the claimed output. The lone exception, `test_generate_batch` (line 1082), does call `wait_for_task("audio", timeout=120)` — but only to block until the task stops running, not to inspect the result: it never re-fetches `/api/chunks` to confirm chunk 0's `status` became `"done"` or that `audio_path` was set and the file exists on disk. A handler that returns `{"status": "started"}` and then crashes/produces no audio in the background thread would pass every one of these tests. This is the single most consequential Rule-12 gap in the file, since these are exactly the code paths (TTS generation, LoRA training/preview, dataset-builder sample generation) where "started but silently failed" is the realistic failure mode CLAUDE.md's Rule 9/12 concerns are about.
- **Status:** logged
- **Suggested fix:** after `wait_for_task` (or an equivalent poll) confirms each task finished, assert on its actual outcome — e.g. re-`GET /api/chunks` and check `status == "done"` + `audio_path` exists for `test_generate_batch`/`test_generate_chunk`; check the produced dataset/adapter file exists for the LoRA/dataset-builder tests — rather than stopping at "the request to start it was accepted."

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

### [F-029] Rule 9 — `/api/lmstudio/optimize` reloads (unloads + loads) the LLM model with no GPU lock check at all
- **Piece:** P16 — app/app.py (/api/system/stats → /api/upload)
- **Location:** `app/app.py:1589-1619` (`lmstudio_optimize`) calling `app/lmstudio_settings.py:243-286` (`apply_lmstudio_settings`) / `:292-321` (`apply_remote_lmstudio_settings`)
- **Severity:** medium
- **Description:** This route toggles the loaded LM Studio model between VRAM-safe and default settings by unloading and reloading it (`lms unload` + `lms load` locally, or the SSH equivalent remotely) — a real VRAM-affecting operation, the same class of operation `check_global_gpu_lock`/`claim_gpu_task` exist to serialize against (per CLAUDE.md's "Concurrency model" section and Rule 9). Unlike every `GPU_TASKS` member, this route never calls `check_global_gpu_lock` and isn't a `process_state` entry at all (it's a synchronous request/response route, not a background task), so nothing stops a user from clicking "optimize" while `review`, `audio`, `script`, or any other GPU task is mid-run and actively holding VRAM against the currently-loaded model — the reload could pull the rug out from under that in-flight LLM/TTS call. By contrast, the same unload/reload functions are also invoked from `ensure_ideal_settings` inside `review_script.py`/`find_nicknames.py`'s `main()`, but those calls happen at the very start of an already-`claim_gpu_task`'d run, before any inference — that call site is safe by construction; this route's call site is not, since it has no relationship to the lock at all.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — either reject the optimize request with 400 if `check_global_gpu_lock` would currently fail (treating it like a lightweight, non-`process_state` GPU operation that still must respect the lock), or confirm this is accepted risk because the Setup-tab toggle is a deliberate, infrequent, user-driven action and document that explicitly near the route.

### [F-030] Rule 2 — `GET /api/annotated_script` has zero frontend callers; only exercised by test_api.py
- **Piece:** P17 — app/app.py (/api/generate_script → /api/logs/{task_name})
- **Location:** `app/app.py:2631-2637` (`get_annotated_script`)
- **Severity:** low
- **Description:** `grep -rn "annotated_script" app/static/index.html` returns nothing — the SPA never calls this route (it reads chunks/voice config through other endpoints instead). The only caller in the repo is `app/test_api.py:380-381` (`test_get_annotated_script`). Unlike a true zero-caller private helper, this is a public `GET` route returning raw `annotated_script.json` — plausibly intended as a programmatic/curl-accessible API per this project's documented "API documentation" convention (CLAUDE.md "Write Documentation" section), not dead code, so it does not meet fix-now criteria (a route, not a private helper) and the test coverage suggests it's a known, intentional surface rather than an accidental leftover.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — confirm whether this route is meant as a documented external/programmatic API (in which case it's fine as-is and could be noted as such) or is genuinely vestigial from before some other endpoint replaced it.

### [F-031] Rule 8 — `GET /api/voices` silently returns an empty list on a corrupted script/voice-config file, indistinguishable from "no script yet"
- **Piece:** P18 — app/app.py (/api/voices → /api/suggest_voices)
- **Location:** `app/app.py:2678-2679` (`except (json.JSONDecodeError, ValueError): pass` on `SCRIPT_PATH`), `:2690-2691` (same pattern on `VOICE_CONFIG_PATH`)
- **Severity:** low
- **Description:** Both `try/except` blocks in `get_voices()` swallow a JSON parse failure with zero logging (not even a `print`), then fall through to the "no voices" / "empty config" path. A user whose `annotated_script.json` got truncated/corrupted (e.g. a crash mid-write before this codebase's `atomic_json_write` was used everywhere, or external tampering) sees the Voices tab render as if no script had ever been generated, with no signal that the file exists but failed to parse. Lower severity than F-011/F-015 (no write-back, so no data loss) but the same "silently treat corruption as absence" pattern this audit has flagged repeatedly.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — log a warning (e.g. `logger.warning(f"Failed to parse {SCRIPT_PATH}: {e}")`) before falling through, so a corrupted file is distinguishable from a missing one in `logs/api/`.

### [F-032] Rule 9 — `POST /api/chunks/{index}/generate` runs real TTS/GPU inference with no GPU-lock check at all
- **Piece:** P19 — app/app.py (/api/audiobook → /api/review/checkpoints)
- **Location:** `app/app.py:3081-3093` (`generate_chunk_endpoint`) calling `app/project.py:371` (`generate_chunk_audio`, which calls `engine.get_engine()` + `engine.generate_voice(...)`)
- **Severity:** high
- **Description:** This route backgrounds a single-chunk TTS generation via `BackgroundTasks.add_task` with no `check_global_gpu_lock`/`claim_gpu_task` call, and it has no `process_state` entry at all (it isn't in `GPU_TASKS` or `NON_GPU_TASKS`). `generate_chunk_audio` calls `self.get_engine()` (loads/uses the TTS model) and `engine.generate_voice(...)` — genuine GPU/VRAM work, the same class of operation its multi-chunk sibling route `/api/generate_batch` (`app/app.py:3211-3268`) correctly guards with `check_global_gpu_lock("audio")` then `claim_gpu_task("audio")`. As written, a user can click "regenerate this line" in the Editor tab while `review`, `script`, `persona`, batch `audio`, or any other `GPU_TASKS` member is actively running, racing for VRAM exactly as CLAUDE.md's "Concurrency model" section and Rule 9 describe — same risk class as F-029 (`/api/lmstudio/optimize`), but here the affected operation is the everyday single-chunk regenerate action used throughout the Editor tab, not an infrequent Setup-tab toggle.
- **Status:** needs-decision (GPU-lock-touching change, per audit instructions automatically needs-decision, not fix-now)
- **Suggested fix:** see needs-decision — add `check_global_gpu_lock("audio")` before backgrounding the task (mirroring `/api/generate_batch`), or — since single-chunk regen from the Editor is plausibly meant to be usable while, say, `persona` runs — explicitly register a lightweight pairing such as routing it through the existing `"audio"` task slot (`check_global_gpu_lock("audio")` + setting `process_state["audio"]["running"]` for the task's duration) so it can't race a concurrent GPU task.

### [F-033] Rule 8 — `merge`/`export_audacity`/`merge_m4b` routes log `project_manager`'s success message verbatim, with no skipped-chunk count, inheriting F-013's blanket-success gap at the API layer
- **Piece:** P19 — app/app.py (/api/audiobook → /api/review/checkpoints)
- **Location:** `app/app.py:3101-3117` (`merge_audio_endpoint`), `:3124-3139` (`export_audacity_endpoint`), `:3161-3184` (`merge_m4b_endpoint`)
- **Severity:** medium
- **Description:** All three task closures do `success, msg = project_manager.merge_audio()` (or `.export_audacity()` / `.merge_m4b(...)`) and append `f"{Verb} complete: {msg}"` to `process_state[...]["logs"]` on `success`, with no comparison against `len(project_manager.load_chunks())` or a count of chunks with `status == "done"`. Per F-013, `project.py`'s underlying `_load_chunks_with_audio` silently drops any chunk with a missing `audio_path`, a missing file on disk, or a failed `AudioSegment.from_file` load — so `msg` itself (just the output filename, e.g. `"cloned_audiobook.mp3"` per `project.py:487-491`) carries no skip information for these routes to pass through even if they wanted to. By contrast, the structurally identical `/api/generate_batch`/`/api/generate_batch_fast` task closures in the same file (`app/app.py:3249-3258`, `:3317-3326`) *do* report `completed`/`failed`/`cancelled` counts from `results`, and even enumerate each failed chunk index — confirming the merge/export routes are the outlier in this same file, not a codebase-wide pattern. The frontend (`static/index.html`'s status poller, e.g. around line 4630) shows the literal last log line to the user, so "Merge complete: cloned_audiobook.mp3" reads as unconditional success even when chunks were silently dropped.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — same root fix as F-013 (have `_load_chunks_with_audio`/the three exporters return a skipped count), but noting here specifically that these three `app.py` task closures are where the resulting count would need to be appended to the log message, mirroring `generate_batch`'s existing `{completed} succeeded, {failed} failed` pattern.

### [F-034] Rule 2 — `DELETE /api/m4b_cover` has zero callers anywhere in the repo (frontend or tests)
- **Piece:** P19 — app/app.py (/api/audiobook → /api/review/checkpoints)
- **Location:** `app/app.py:3203-3209` (`delete_m4b_cover`)
- **Severity:** low
- **Description:** `grep -rn "m4b_cover" --include="*.py" --include="*.html" --include="*.js" .` shows the only caller of any `/api/m4b_cover` endpoint is the upload (`POST`) listener at `app/static/index.html:4663`; there is no "remove cover" button/handler anywhere in `index.html`, and `app/test_api.py` doesn't exercise this route either (confirmed via the same grep — no `DELETE` call site exists). Unlike a true private-helper zero-caller case, this is a public route (not a private helper), so it does not meet fix-now criteria per the audit's own established precedent (F-030 treated a zero-frontend-caller `GET` route the same way) — but unlike F-030, there is no test coverage at all here to suggest it's an intentionally-reserved programmatic API surface.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — either wire up a "remove cover" control in the M4B export section of the Result tab (the natural UI counterpart to the existing upload input), or confirm the route is deliberately reserved for direct/curl use and leave as-is.
