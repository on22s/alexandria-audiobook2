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

### [F-035] Rule 8 — `_load_voice_library` silently resets a corrupted `voice_library.json` to empty, and the next save overwrites it, erasing every cast/shared entry
- **Piece:** P20 — app/app.py (/api/scripts → /api/voice_library/apply_bulk)
- **Location:** `app/app.py:3612-3623` (`_load_voice_library`), write-back via `_save_voice_library`/`_save_voice_library_async` (`:3626-3642`)
- **Severity:** medium
- **Description:** When `VOICE_LIBRARY_PATH` exists but fails to parse (`json.JSONDecodeError`/`ValueError`), `except (json.JSONDecodeError, ValueError): pass` discards the error with zero logging and returns the freshly-initialized `{"shared": {}, "casts": {}}` as if the library had never been populated. Nearly every voice_library route (`voice_library_create_cast`, `voice_library_delete_cast`, `voice_library_delete_member`, `voice_library_save`, `voice_library_apply`, `voice_library_apply_bulk`) follows the `_load_voice_library()` call with an unconditional `_save_voice_library_async(lib)`/`_save_voice_library(lib)` write-back of that same (now-empty-if-corrupted) `lib` object — so the very next library mutation after a corruption event permanently erases every other cast and every shared-pool entry (e.g. the cross-book narrator voice). Same pattern and severity as F-011 (`generate_personas.py` manifest) and F-015 (`review_script.py` alias registry), now confirmed a third time in `app.py` itself.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — log the parse failure distinctly (e.g. `logger.warning(f"voice_library.json corrupted, rebuilding from scratch: {e}")`) so the data loss is visible instead of indistinguishable from "no library yet"; consider also refusing to overwrite (return 500) rather than silently proceeding with an empty library when the file demonstrably existed and had content.

### [F-037] Rule 17 — `_apply_cast_mapping` mutates the `current_config` dict it's handed while also returning it, duplicating how the outcome is reported
- **Piece:** P20 — app/app.py (/api/scripts → /api/voice_library/apply_bulk)
- **Location:** `app/app.py:3692-3719` (`_apply_cast_mapping`), called from `_apply_cast_to_config_file` (`:3722-3738`)
- **Severity:** low
- **Description:** `_apply_cast_mapping`'s own docstring states it is "mutating and returning" `current_config` — it sets `current_config[char] = cfg` in place (line 3717) for every applied character, and then also returns `(current_config, applied)`. Its only caller, `_apply_cast_to_config_file`, immediately does `current_config, applied = _apply_cast_mapping(lib, cast_name, mapping, current_config, chars=chars)`, i.e. it relies on the return value, not the in-place mutation — so the mutation is redundant in practice but still a real side effect on a parameter that crosses a function boundary (this is not case (c)'s "local variable with no cross-function handoff," since `_apply_cast_mapping` is a distinct function from its caller). Matches the pattern already logged as F-014 (`project.py::_finalize_completed_chunk`) and F-016 (`review_script.py::dedupe_speakers`).
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — have `_apply_cast_mapping` build and return a fresh dict (e.g. `{**current_config, ...}` updates applied to a copy) rather than mutating its `current_config` parameter in place, so the only way to observe the outcome is the return value.

### [F-036] Rule 8 — `_load_manifest` silently resets a corrupted designed-voice/clone-voice manifest to `[]`, and every save route overwrites it unconditionally
- **Piece:** P21 — app/app.py (/api/voice_design/preview → /api/clone_voices/{voice_id})
- **Location:** `app/app.py:3986-3994` (`_load_manifest`, defined at the end of P20's range but consumed exclusively by P21's `voice_design_save`/`voice_design_delete`/`clone_voices_upload`/`clone_voices_delete`)
- **Severity:** medium
- **Description:** Identical pattern to F-035/F-011/F-015: `except (json.JSONDecodeError, ValueError): pass` swallows a corrupt-manifest parse failure with zero logging, returning `[]`. `voice_design_save` (`app/app.py:4043-4051`) and `clone_voices_upload` (`:4111-4117`) both `_load_manifest(...)` then unconditionally `manifest.append(...)` and `_save_manifest(...)` — no check that the load actually succeeded vs. fell back to the corruption case — so the first voice saved/uploaded after `manifest.json` becomes corrupted silently erases every previously-saved designed voice or uploaded clone-voice entry from the manifest (the underlying `.wav` files on disk are untouched, but become permanently unreferenced/orphaned since nothing in `voice_design_list`/`clone_voices_list` can find them anymore).
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — same fix as F-035: log the parse failure distinctly from "no manifest yet," and consider having callers refuse to save/overwrite if the manifest file existed but failed to parse, rather than silently proceeding from an empty list.

### [F-038] Rule 9 — `POST /api/voice_design/preview` runs synchronous local TTS/GPU inference with no GPU-lock check, racing any in-flight background GPU task
- **Piece:** P21 — app/app.py (/api/voice_design/preview → /api/clone_voices/{voice_id})
- **Location:** `app/app.py:4000-4018` (`voice_design_preview`) calling `app/tts.py:776-829` (`TTSEngine.generate_voice_design`)
- **Severity:** medium
- **Description:** Unlike F-029/F-032 (which flagged unlocked *backgrounded* GPU work), this route calls `engine.generate_voice_design(...)` directly and synchronously inside the request handler — no `BackgroundTasks`, no `process_state` entry, no `await asyncio.to_thread`. `generate_voice_design` calls `self._init_local_design()` (loads a local model) and `model.generate_voice_design(...)` (real GPU inference via `torch`), genuinely VRAM-affecting work, with zero call to `check_global_gpu_lock`/`claim_gpu_task`. The risk profile is narrower than F-032's backgrounded case — the caller's own HTTP request blocks for the full duration and the GPU usage can't outlive that request — but it's still a real, unguarded race: a user can submit a voice-design preview while `review`/`audio`/`persona`/`script`/etc. is actively running and holding VRAM via the documented `GPU_TASKS` lock, and this route has no awareness of that lock at all (it doesn't even check, let alone wait or reject). The route is also synchronous Python running inside an `async def` handler with no `to_thread` offload, so it blocks the FastAPI event loop for the full TTS-generation duration — a separate (not Rule-9) concern but compounding the same call site.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — at minimum call `check_global_gpu_lock("voice_design_preview"-equivalent)` (or reuse an existing slot) before invoking `generate_voice_design`, so a preview request gets a clean 400 instead of silently racing a running `GPU_TASKS` member for VRAM; separately consider `await asyncio.to_thread(...)` so the synchronous inference doesn't block the event loop for other requests.

### [F-039] Rule 9 — `POST /api/lora/test` runs synchronous GPU TTS inference with no GPU-lock check at all
- **Piece:** P22 — app/app.py (/api/lora/upload_dataset → /api/lora/preview/{adapter_id})
- **Location:** `app/app.py:4539-4596` (`lora_test_model`) calling `engine.generate_voice(...)` (line 4582)
- **Severity:** medium
- **Description:** Same gap class as F-038 (`/api/voice_design/preview`), found a second time in this range. `lora_test_model` calls `project_manager.get_engine()` then `engine.generate_voice(...)` directly inside the `async def` handler — real GPU/VRAM inference — with no `check_global_gpu_lock`/`claim_gpu_task` call and no `process_state` entry at all. A user can hit "Test" on a LoRA adapter while `review`/`audio`/`script`/`lora_training`/etc. holds the documented `GPU_TASKS` lock, and this route has no awareness of the lock whatsoever. The request blocks the FastAPI event loop for the duration of `engine.generate_voice` with no `asyncio.to_thread` offload, compounding the same call site (as also noted for F-038).
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — add `check_global_gpu_lock(...)` (reusing an existing slot such as `"audio"`, or a new lightweight one) before calling `generate_voice`, and consider `await asyncio.to_thread(...)` so the synchronous call doesn't block the event loop.

### [F-040] Rule 9 — `POST /api/lora/preview/{adapter_id}` runs synchronous GPU TTS inference with no GPU-lock check at all
- **Piece:** P22 — app/app.py (/api/lora/upload_dataset → /api/lora/preview/{adapter_id})
- **Location:** `app/app.py:4600-4656` (`lora_preview`) calling `engine.generate_voice(...)` (line 4646)
- **Severity:** medium
- **Description:** Identical gap to F-039, in the immediately adjacent route. `lora_preview` generates (and caches) a preview sample via `engine.generate_voice(...)` with no `check_global_gpu_lock`/`claim_gpu_task` call and no `process_state` entry — same unguarded VRAM race against any running `GPU_TASKS` member, same synchronous-in-`async def` event-loop blocking concern. Both `lora_test_model` and `lora_preview` sit directly between `lora_train` (line 4391, correctly does `check_global_gpu_lock("lora_training")` + `claim_gpu_task("lora_training")`) and the Dataset Builder's `generate_batch` (P23, correctly locked) — they are the only two GPU-touching routes in this file's LoRA section with zero lock awareness.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — same fix as F-039; both routes likely want the identical treatment since they share the `engine.generate_voice` call pattern almost verbatim.

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

### [F-043] Rule 9 — `POST /api/dataset_builder/generate_sample` runs synchronous GPU TTS inference with no GPU-lock check, and isn't registered in `process_state["dataset_builder"]` at all
- **Piece:** P23 — app/app.py (/api/dataset_builder/*)
- **Location:** `app/app.py:4761-4816` (`dataset_builder_generate_sample`) vs `:4818-4924` (`dataset_builder_generate_batch`, which correctly calls `check_global_gpu_lock("dataset_builder")` at line 4821 and `claim_gpu_task("dataset_builder")` at line 4922)
- **Severity:** high
- **Description:** Same gap class as F-038/F-039/F-040, but worse here: this route's sibling in the very same section (`generate_batch`) demonstrates the correct pattern exists and is known, making the omission in `generate_sample` more clearly a gap rather than an unexplored area. `generate_sample` calls `project_manager.get_engine()` then `engine.generate_voice_design(...)` synchronously inside the `async def` handler — real GPU inference — with no `check_global_gpu_lock` call, no `claim_gpu_task` call, and it never touches `process_state["dataset_builder"]["running"]` at all (unlike `generate_batch`, which sets it `True`/`False` around the whole job). This means: (1) a single-sample preview can race any other `GPU_TASKS` member for VRAM with zero lock awareness, exactly like F-038/39/40; and (2) it can *also* race `generate_batch` on the very same `dataset_builder` work directory/state file concurrently, since neither route's lock state is visible to the other (a `generate_sample` call mid-flight during a `generate_batch` run wouldn't be blocked by `check_global_gpu_lock`, because `generate_sample` never calls it, and the two would both call `_save_builder_state` on the same `state.json` without coordination).
- **Status:** needs-decision (GPU-lock-touching change, per audit instructions automatically needs-decision, not fix-now)
- **Suggested fix:** see needs-decision — add `check_global_gpu_lock("dataset_builder")` before calling `generate_voice_design`, mirroring `generate_batch`; since this route is synchronous rather than backgrounded, it likely shouldn't set `process_state["dataset_builder"]["running"]` itself (that field is polled by the UI as "batch is running"), but it must still respect the same lock so it can't run concurrently with `generate_batch` or any other GPU task.

### [F-044] Rule 2 / Rule 8 — `PreparerConfig` declares 17 fields; `/api/preparer/start` silently drops 12 of them when building the subprocess command
- **Piece:** P24 — app/app.py (/api/preparer/*)
- **Location:** `app/app.py:391-417` (`PreparerConfig`) vs `:5099-5104` (`preparer_start`'s `cmd` construction)
- **Severity:** medium
- **Description:** `PreparerConfig` accepts `source_filename`, `model`, `fallback_model`, `source_threshold`, `keep_unaligned`, `chunk_size`, `resume`, `skip_annotation`, `source_start`, `source_start_text`, `no_auto_anchor`, `batch_size`, `enrich_with_llm`, `llm_model_path`, `enrich_speaker_attribution`, `enrich_narration_style`, `enrich_emotional_tone`, and `min_chunk_duration` — all of which map directly to real, currently-supported flags on `alexandria_preparer_rocm_compatible.py` (`--source`, `--model`, `--fallback-model`, `--source-threshold`, `--keep-unaligned`, `--chunk-size`, `--resume`, `--skip-annotation`, `--source-start`, `--source-start-text`, `--no-auto-anchor`, `--batch-size`, `--enrich-with-llm`, `--llm-model-path`, `--enrich-speaker-attribution`, `--enrich-narration-style`, `--enrich-emotional-tone`, `--min-chunk-duration`; confirmed via `grep -n "add_argument" alexandria_preparer_rocm_compatible.py`). But `preparer_start`'s actual `cmd` list (lines 5099-5104) only forwards `--audio`, `--output`, `--lang`, `--min-confidence`, `--min-snr` — 5 of 17 fields. The frontend (`app/static/index.html:6246-6252`, `startPreparer`) only ever sends those same 5 fields, so the other 12 are not just unforwarded but completely unreachable from the UI: a caller has no way to request source-text alignment, LLM enrichment, resume, or any of the other documented preparer capabilities through this route at all. `preparer_batch_start`'s much smaller `BatchPreparerRequest`/`BatchPreparerTask` (3 knobs: `lang`/`min_confidence`/`min_snr`) has no such mismatch — every batch field is wired through.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — either wire the remaining 12 `PreparerConfig` fields into `preparer_start`'s `cmd` list and surface them in the UI (if single-file preparer is meant to expose the same capability as direct CLI use), or shrink `PreparerConfig` to the 5 fields actually used (matching `BatchPreparerRequest`'s pattern) so the schema doesn't silently promise functionality the route never delivers.

### [F-045] Rule 8 — `GET /api/voicelab/config` swallows any exception while checking `zips_dir` validity, with zero logging
- **Piece:** P25 — app/app.py (/api/voicelab/*)
- **Location:** `app/app.py:5340-5344` (`voicelab_get_config`)
- **Severity:** low
- **Description:** `except Exception: pass` around `_resolve_zips_dir(cfg["zips_dir"])` / `os.path.isdir(resolved_zips)` discards any failure — not just the expected "folder doesn't exist" case (which `os.path.isdir` already returns `False` for without raising) but also genuine errors like a malformed `zips_dir` value causing `os.path.normpath`/`os.path.join` to raise, or a `PermissionError` from `os.path.isdir`. The route then returns `zips_dir_ok: False` in its JSON, indistinguishable from the legitimate "configured but missing" case — same recurring silent-swallow-into-success-looking-response shape as F-035/F-036/F-046.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — narrow the except to expected exception types (or none, since `os.path.isdir` doesn't raise for a missing path) and `logger.warning` anything unexpected so a real bug in `zips_dir` resolution isn't silently reported as "folder not found."

### [F-046] Rule 8 — `_load_voicelab_config` silently falls back to defaults on a corrupted `voicelab_config.json`, with zero logging
- **Piece:** P25 — app/app.py (/api/voicelab/*)
- **Location:** `app/app.py:5303-5313` (`_load_voicelab_config`)
- **Severity:** low
- **Description:** Same recurring pattern as F-011/F-015/F-035/F-036 (already logged for other manifest/config files in this repo), found again here for `voicelab_config.json`: `except (json.JSONDecodeError, ValueError, OSError): pass` discards a corrupted-file parse failure with no `logger.warning`/`logger.error`, silently returning `VOICELAB_DEFAULTS` instead. Every voicelab route (`voicelab_get_config`, `voicelab_save_config`, `voicelab_inspect`, `voicelab_start`, and `_resolve_preparer_interpreter` via `_load_voicelab_config()["rocm_python"]`) calls this helper, so a corrupted config silently reverts paths like `rocm_python`/`pipeline_repo`/`zips_dir` to their environment-variable/repo-relative defaults with no signal to the user that their saved settings were lost — `voicelab_save_config` (line 5362) would then happily overwrite the corrupted file with `cfg` built from defaults plus whatever the current request supplied, permanently erasing any other previously-saved fields.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — same fix as F-035/F-036: `logger.warning` the parse failure distinctly, and consider whether `voicelab_save_config` should refuse to silently overwrite a config that failed to parse (vs. one that simply didn't exist yet).

### [F-047] Rule 2 — Three `sub-batch-*-group` wrapper divs follow the file's established show/hide-toggle-target naming convention but are never toggled
- **Piece:** P26 — app/static/index.html (HTML/CSS shell + tab markup, lines 1-1789)
- **Location:** `app/static/index.html:622,627,632` (`sub-batch-min-group`, `sub-batch-ratio-group`, `sub-batch-max-items-group`), sibling of `sub-batch-enabled` checkbox at line 617
- **Severity:** low
- **Description:** This file has an established pattern of giving a wrapper `<div id="X-group">` to fields that get conditionally shown/hidden via `document.getElementById('X-group').style.display = ...` — confirmed for `tts-url-group`/`tts-device-group` (toggled by `toggleTTSMode()`, lines 2024-2025) and `llm-ssh-group` (toggled by `onLlmModeChange`, line 2159). The three `sub-batch-*-group` divs follow the identical naming convention and wrap fields that are logically dependent on the adjacent `sub-batch-enabled` checkbox (line 617, "Sub-batching" toggle, "Split batches by text length to reduce padding waste") — but `grep -n "sub-batch-min-group\|sub-batch-ratio-group\|sub-batch-max-items-group"` shows zero JS references anywhere in the file, and `sub-batch-enabled` itself has no `onchange` handler at all. The three sub-fields (`sub-batch-min-size`, `sub-batch-ratio`, `sub-batch-max-items`) remain visible and editable even when "Sub-batching" is unchecked, which is also a minor UX inconsistency (editing settings that the adjacent toggle implies are inactive) but the audit-relevant point is the dead `-group` ids: either the toggle wiring was planned and never added, or it existed and was removed.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — either wire `sub-batch-enabled`'s `onchange` to toggle these three `-group` divs' `style.display` (mirroring `toggleTTSMode`/`onLlmModeChange`), or remove the unused `id` attributes if the fields are meant to always stay visible/editable regardless of the toggle.

### [F-048] Rule 18 — 10 single-line `if` bodies without braces in showToast→testLlmConnection
- **Piece:** P27 — app/static/index.html (showToast → testLlmConnection, lines ~1790-2207)
- **Location:** `app/static/index.html:1842` (`confirmIfRemote`), `:1851` (`escapeHtml`), `:1888-1889` (`notifyJobDone`), `:1979` (`API._handleError`), `:1983` (`API._handleError`), `:2101-2103` (`_computeAutoSettings`), `:2148` (`renderActiveLlmModeBadge`)
- **Severity:** low
- **Description:** Per CLAUDE.md Rule 18, every `if`/`for`/`while` in this file must brace its body even when it's a single statement. Found 10 violations, all single-line `if (...) <statement>;` with no `{ }`: `if (currentLlmMode !== 'remote') return true;` (1842); `if (str == null) return '';` (1851); `if (!('Notification' in window) || Notification.permission !== 'granted') return;` (1888); `if (document.visibilityState === 'visible' && document.hasFocus()) return;` (1889); `if (res.ok) return;` (1979); `if (body && body.detail) detail = body.detail;` (1983); `if (gpuName) parts.push(gpuName);` (2101); `if (ramGb) parts.push(...);` (2102); `if (stats.cpu_count) parts.push(...);` (2103); `if (!badge) return;` (2148).
- **Status:** fixed-inline (commit `574ec35`)
- **Suggested fix:** add `{ }` around each one-line body, preserving behavior exactly — fix-now per audit plan.

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

### [F-051] Rule 18 — 11 single-line `if` bodies without braces in loadConfig→_onReviewDone (plus toggleReviewBatchMode)
- **Piece:** P28
- **Location:** `app/static/index.html:2457` (`file-upload` change handler), `:2496` (`btn-gen-script` click handler), `:2521` (`_resetPauseBtn`), `:2549` (`_makePauseResumeHandler`'s returned handler), `:2634` (`_startBatchScript`), `:2669,2675,2678` (`_pollScriptBatchLogs`), `:2691` (`_pollScriptBatchLogs`'s `state.tasks.forEach` callback), `:2724` (`_showReviewControls`), `:2796` (`toggleReviewBatchMode`, just past `_onReviewDone` but inside this piece's contiguous range)
- **Severity:** low
- **Description:** Per CLAUDE.md Rule 18, every `if`/`for`/`while` in this file must brace its body even when it's a single statement. Found 11 violations, all single-line `if (...) <statement>;` with no `{ }`: `if (fileInput.files.length === 0) return;` (2457); `if (!scriptBatchPoller) genBtn.disabled = false;` (2496); `if (!btn) return;` (2521); `if (btn.disabled) return;` (2549); `if (!(await confirmIfRemote('this batch script generation'))) return;` (2634); `if (scriptBatchPoller) clearTimeout(scriptBatchPoller);` (2669); `if (myGen !== scriptBatchPollGen) return;` (2675 and again at 2678); `if (!el) return;` (2691); `if (show) _resetPauseBtn('btn-pause-review');` (2724); `if (isBatch) loadReviewBatchScripts();` (2796).
- **Status:** fixed-inline (commit `c5c1a46`)
- **Suggested fix:** add `{ }` around each one-line body, preserving behavior exactly — fix-now per audit plan.

### [F-052] Rule 8 — `cancelBatchScript`/`cancelBatchReview` swallow cancel failures with `/* ignore */`, unlike their single-item siblings `cancelScript`/`cancelReview`
- **Piece:** P28
- **Location:** `app/static/index.html:2622-2627` (`cancelBatchScript`), `:2779-2784` (`cancelBatchReview`) vs `:2577-2584` (`cancelScript`) and `:2773-2778` (`cancelReview`)
- **Severity:** low
- **Description:** `cancelScript` and `cancelReview` both call `showToast('Cancel failed: ' + (e.message || 'unknown error'), 'warning')` in their `catch` block so the user learns a cancel request didn't go through. Their batch counterparts, `cancelBatchScript` and `cancelBatchReview`, use `catch (e) { /* ignore */ }` instead — a failed batch-cancel (e.g. network error, 500) is completely invisible: the Cancel button's pause-button reset (`_resetPauseBtn`) never runs, the batch keeps running server-side, and the user gets no feedback that their click did nothing. This is an inconsistency within the same file between two near-identical pairs of functions, not just a generic missing-log case.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — mirror `cancelScript`/`cancelReview`'s `showToast('Cancel failed: ...', 'warning')` in both batch variants' `catch` blocks.

### [F-053] Rule 10 — `_pollScriptBatchLogs`'s poll loop retries indefinitely on any error with no cap or surfaced error, unlike `_makePauseResumeHandler`'s bounded/typed retry
- **Piece:** P28
- **Location:** `app/static/index.html:2674-2713` (`_pollScriptBatchLogs`'s inner `poll` function), specifically the `catch (e) { scriptBatchPoller = setTimeout(poll, 2000); }` at lines 2709-2711
- **Severity:** low
- **Description:** On any error from `API.get('/api/status/batch_script')` — a transient network blip, a 500, or a real client bug — the loop just reschedules itself with a longer timeout (2000ms vs. the normal 1000ms) and keeps going forever; there's no attempt counter, no max-duration cutoff, and no `console.error`/`showToast` distinguishing "still polling through a hiccup" from "this has been failing for 10 minutes." This contrasts with `_makePauseResumeHandler`'s `postWithRetry` (same piece, lines 2530-2542), which has an explicit, bounded, single-condition retry policy (`e.status === 503 && attempt < 2`) consistent with Rule 10's "decide one policy and follow it on every attempt." It's not strictly inconsistent within itself (every iteration is treated the same way), but the complete absence of a cap or visible failure signal means a persistent server-side outage during a long batch-script run would silently poll forever with the user seeing only a frozen log panel, no error.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — track a consecutive-failure counter and surface a `showToast`/visible warning (without necessarily stopping the poll) once it crosses a threshold, so a stuck poll is distinguishable from a quiet but healthy one.

### [F-054] Rule 16 — `_reviewDedupe` has no leading verb despite being a pure read function
- **Piece:** P28
- **Location:** `app/static/index.html:2717-2720` (`_reviewDedupe`)
- **Severity:** low
- **Description:** `_reviewDedupe()` reads the `review-dedupe-speakers` checkbox and returns a boolean (defaulting to `true` if the checkbox doesn't exist) — a pure read with zero side effects, the same shape as F-050's `numFieldValue` and exactly the case Rule 16 says should read as `get_`/`is_`-style. The name is a bare noun phrase with no verb, so a reader can't tell from the name alone whether it reads, writes, or toggles dedupe state.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — rename to `_isReviewDedupeChecked` or `_getReviewDedupe` (4 call sites would need updating); not fix-now since renaming isn't in the fix-now criteria.

### [F-055] Rule 18 — 14 single-line `if` bodies without braces in _loadScriptList→pollPersonaStatus
- **Piece:** P29
- **Location:** `app/static/index.html:2847` (`_sortScriptList`), `:2887` (`startBatchReview`), `:2956` (`loadCharacterAliases`), `:2980` (`addAliasRow`), `:2996` (`saveCharacterAliases`), `:3010-3013` (`_formatBookStats`, 4 lines), `:3018,3022` (`_formatTotalsLine`, 2 lines), `:3028` (`_updateReviewBatchTotals`), `:3054` (`pollReviewBatch`), `:3063` (`pollReviewBatch`'s `state.tasks.forEach` callback)
- **Severity:** low
- **Description:** Per CLAUDE.md Rule 18, every `if`/`for`/`while` in this file must brace its body even when it's a single statement. Found 14 violations, all single-line `if (...) <statement>;` with no `{ }`: `if (!list.length) return;` (2847); `if (!(await confirmIfRemote('this batch review'))) return;` (2887); `if (show) panel.style.display = 'block';` (2956); `if (placeholder) placeholder.remove();` (2980); `if (a && c) map[a] = c;` (2996); `if (s.narrators_merged) txt += ...;` / `if (s.speakers_merged) txt += ...;` / `if (s.batches_failed) txt += ...;` / `if (s.batches_skipped_vram) txt += ...;` (3010-3013); `if (!t || !t.books_done) return ...;` (3018); `if (t.batches_failed) txt += ...;` (3022); `if (!el) return;` (3028); `if (reviewBatchPoller) clearInterval(reviewBatchPoller);` (3054); `if (!cb) return;` (3063).
- **Status:** fixed-inline (commit `e81f926`)
- **Suggested fix:** add `{ }` around each one-line body, preserving behavior exactly — fix-now per audit plan.

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

### [F-058] Rule 10 — `pollReviewBatch` and `pollPersonaStatus` disagree on what a poll error means: one retries forever silently, the other aborts immediately with a toast
- **Piece:** P29
- **Location:** `app/static/index.html:3052-3083` (`pollReviewBatch`, `catch (e) { /* keep polling through hiccups */ }` at line 3081) vs `:3129-3167` (`pollPersonaStatus`, `catch (e) { clearInterval(interval); showToast(...); ... }` at lines 3158-3164)
- **Severity:** medium
- **Description:** Both functions poll a near-identical `/api/status/<task>` shape on a fixed interval and can fail for the same reasons (network blip, transient 500, JSON parse error). `pollReviewBatch` treats every error as a harmless hiccup: it swallows the error completely and lets `setInterval` fire again on schedule, with no cap, no logging, and no user-visible signal — if the underlying cause is not transient (e.g. the server crashed), this polls forever with a frozen-looking UI and no error ever surfaces. `pollPersonaStatus` does the opposite for the same class of error: on the very first failure it immediately `clearInterval`s, shows `showToast('Persona status poll failed: ...', 'error')`, and gives up — a single transient network blip permanently stops the poll and tells the user persona generation failed even if it's still running fine server-side. Per Rule 10 ("decide on ONE consistent policy... and follow it on every attempt"), this is the same decision (how to interpret a poll failure) answered two incompatible ways by two structurally identical loops in the same file.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — pick one policy (e.g. retry up to N times with a visible warning after the first failure, then stop and toast) and apply it to both `pollReviewBatch` and `pollPersonaStatus` (and ideally `_pollScriptBatchLogs` from F-053, which has its own third variant).

### [F-059] Rule 16 — `_scriptVolumeNum` has no leading verb despite being a pure computation function
- **Piece:** P29
- **Location:** `app/static/index.html:2839-2842` (`_scriptVolumeNum`)
- **Severity:** low
- **Description:** `_scriptVolumeNum(name)` regex-matches a trailing number out of a script name and returns it (or `Infinity` if none found) — a pure computation with zero side effects, the same shape as F-050 (`numFieldValue`) and F-054 (`_reviewDedupe`), now a third instance of this naming gap. The name is a bare noun phrase with no verb, so a reader can't tell from the name alone that it's a read/extraction rather than something that mutates state.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — rename to `_getScriptVolumeNum` or `_extractScriptVolumeNum` (2 call sites would need updating); not fix-now since renaming isn't in the fix-now criteria.

### [F-060] Rule 18 — 15 single-line `if` bodies without braces in createVoiceCard→submitCastApplyBulk
- **Piece:** P30
- **Location:** `app/static/index.html:3411` (`renderVoiceSuggestions`), `:3433,3435` (`applyVoiceSuggestion`), `:3460` (`applyVoiceSuggestion`), `:3495` (`setCastStatus`), `:3560` (`createCast`), `:3570,3571` (`deleteCast`), `:3589` (`openCastSave`), `:3659,3660` (`submitCastSave`), `:3692` (`openCastApply`), `:3727` (`submitCastApply`), `:3741` (`openCastApplyBulk`), `:3833` (`submitCastApplyBulk`)
- **Severity:** low
- **Description:** Per CLAUDE.md Rule 18, every `if`/`for`/`while` in this file must brace its body even when it's a single statement. Found 15 violations, all single-line `if (...) <statement>;` with no `{ }`: `if (banner) banner.remove();` (3411, 3460 — two separate functions); `if (!sugg) return;` / `if (!card) return;` (3433, 3435); `if (el) el.innerHTML = ...;` (3495); `if (!name) return;` (3560); `if (!window._selectedCast) return;` (3570, 3589, 3692, 3741 — four separate functions); `if (!confirm(...)) return;` (3571); `if (ns) msg += ...;` / `if (skipped > 0) msg += ...;` (3659-3660); `if (sel && sel.value) mapping[char] = sel.value;` (3727, 3833 — two separate functions with verbatim-identical bodies, see also F-062).
- **Status:** fixed-inline (commit `90f728d`)
- **Suggested fix:** add `{ }` around each one-line body, preserving behavior exactly — fix-now per audit plan. Verified with `node --check` on the extracted `<script>` content before committing.

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

### [F-063] Rule 18 — 10 single-line `if` bodies without braces in collectVoiceConfig→exportM4B
- **Piece:** P31
- **Location:** `app/static/index.html:3921, 3963, 3971, 4239, 4252, 4270, 4355, 4604, 4659, 4664`
- **Severity:** low
- **Description:** Ten `if (...) <statement>;` one-liners with no `{ }`: `3921` (`if (cards.length === 0) return;` in `debouncedSaveVoices`), `3963` (`if (!audio.paused && !audio.ended) return true;` in `isAudioPlaying`), `3971` (`if (!tr) return false;` in `updateChunkRow`), `4239` (`if (toast) toast.hide();` in `undoDeleteChunk`), `4252` (`if (isPlayingSequence) return;` in `stopOthers`), `4270` (`if (!isPlayingSequence) return;` in `playSequence`'s `playNext`), `4355` (`if (!tr) return;` in `saveRowEdits`), `4604` (`if (!await showConfirm(...)) return;` in the merge button handler), `4659` (`if (!file) return;` in the M4B cover-upload handler), `4664` (`if (!resp.ok) throw new Error(...);` in the same handler). Same recurring pattern already logged for other pieces of this file (e.g. F-060).
- **Status:** fixed-inline (commit `52cb10f`)
- **Suggested fix:** add `{ }` to all 10, preserving behavior exactly (fix-now criterion 2).

### [F-064] Rule 10 — `renderAll` enforces a "regenerate all" confirmation dialog that `renderBatchFast` silently skips, despite both being reachable from the same `startRender` button via the same `regenerateAll` flag
- **Piece:** P31
- **Location:** `app/static/index.html:4441-4448` (`startRender`), `:4467-4470` (`renderAll`'s confirm gate), `:4530-4545` (`renderBatchFast`, no equivalent gate)
- **Severity:** medium
- **Description:** `startRender(regenerateAll)` dispatches to `renderAll(regenerateAll)` when `tts-mode === 'external'` and to `renderBatchFast(regenerateAll)` otherwise — both triggered by the same "Regenerate All" button (`onclick="startRender(true)"`, line 1605). `renderAll` gates a true `regenerateAll` behind `showConfirm` ("Regenerate all N non-empty chunks? This will replace existing audio.") before proceeding (lines 4467-4470), but `renderBatchFast` has no such check at all — it goes straight from building `toProcess` to firing `/api/generate_batch_fast`. This is the same "regenerate all and overwrite existing audio" decision made two different ways depending on which TTS mode happens to be selected, rather than one consistent policy applied regardless of path.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — likely move the `regenerateAll` confirm check up into `startRender` (or duplicate it identically into `renderBatchFast`) so the same destructive action always asks for confirmation regardless of TTS mode.

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

### [F-069] Rule 18 — Six unbraced single-statement `if` bodies in `pollLogs`, `loadScript`, `deleteScript`
- **Piece:** P32
- **Location:** `app/static/index.html:4724`, `:4727`, `:4733` (all in `pollLogs`), `:4745` (`pollLogs`'s `onDone` continuation), `:4821` (`loadScript`), `:4845` (`deleteScript`)
- **Severity:** low
- **Description:** Six `if` statements in this piece have a single-statement body with no `{ }`: `if (myGen !== _pollLogsGen[taskName]) return;` (×2, lines 4724 and 4727), `if (onDone) onDone(status);` (line 4733), `if (tbody) tbody.innerHTML = '';` (line 4745), `if (!await showConfirm(...)) return;` in `loadScript` (line 4821), and the identical guard in `deleteScript` (line 4845). Per Rule 18 these all need braces to prevent a future second statement silently falling outside the conditional.
- **Status:** fixed-inline (commit `d1c34fe`)
- **Suggested fix:** add `{ }` around each single-line body, preserving behavior exactly.

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

### [F-072] Rule 15 (candidate) — `pollLogs` is the only status-poller with stale-poll protection; ~10 other hand-rolled `setInterval`/`setTimeout` pollers in the file lack it
- **Piece:** P32
- **Location:** `app/static/index.html:4717-4760` (`pollLogs`, using the `_pollLogsGen` generation counter) vs. independent poll loops at `:2707` (`scriptBatchPoller`), `:3055` (`reviewBatchPoller`), `:3133`, `:4491`, `:4566`, `:4625`, `:4688` (M4B export poll), `:5309`, `:5843` (`dsbPolling`), `:6310` (`prepPoller`), `:6488` (`voicelabPoller`)
- **Severity:** low
- **Description:** `[rule15-candidate]` `pollLogs` solves "don't act on a response that arrived after a newer poll superseded it" by incrementing `_pollLogsGen[taskName]` on each new call and checking it before applying any response (lines 4721, 4724, 4727) — this guards against e.g. rapid start/cancel/restart of the same task name producing out-of-order UI updates. None of the ~10 other independent `setInterval`/`setTimeout`-based polling loops elsewhere in the file use this or an equivalent guard (they rely on a single module-level interval-id variable being cleared, which doesn't protect against a request already in flight resolving late). This is "is this poll response still current?" answered one good way in `pollLogs` and not at all everywhere else — the existing F-065 and the P29 `pollReviewBatch`/`pollPersonaStatus` finding already document the broader duplication; this tags `pollLogs` itself as the candidate reusable building block.
- **Status:** logged
- **Suggested fix:** see needs-decision — not resolved here; a future pass could extract `_pollLogsGen`'s generation-guard pattern into a small shared helper (or have the other pollers `await`/cancel a stored promise) so every status poller gets stale-response protection, not just `pollLogs`.

### [F-073] Rule 10 — `pollLoraTraining` and `dsbPollStatus` interpret a poll-request error with opposite retry policies
- **Piece:** P33
- **Location:** `app/static/index.html:5368-5371` (`pollLoraTraining`'s `catch`) vs `:5907-5914` (`dsbPollStatus`'s `catch`)
- **Severity:** medium
- **Description:** Both functions are `setInterval`-driven pollers for a long-running GPU job's status endpoint, but they answer "what do we do when the status fetch itself throws?" in opposite ways. `pollLoraTraining`'s catch does `console.error('LoRA poll error:', e); clearInterval(interval);` — on the very first fetch error (a single dropped request, a transient 502, etc.) it permanently stops polling, leaving the UI stuck on "Training in progress..." with the train button still disabled and no `notifyJobDone`/error state ever shown, with nothing but a console line as evidence. `dsbPollStatus`'s catch (the default, non-`silent` path actually used in production — see F-074) does `if (!silent) console.error('Poll error:', e);` and otherwise falls through, leaving `dsbPolling`'s `setInterval` running — it retries unconditionally on every subsequent tick forever, even if the backend is permanently down. Per Rule 10 this is the same kind of failure (a poll request to a long-running-job status endpoint failing) decided two different ways with no documented reason — one silently gives up forever, the other silently keeps trying forever, and neither tells the user.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — pick one consistent policy (e.g. retry up to N consecutive failures with backoff, then stop and show an error toast) and apply it to both pollers, or factor a shared poller helper that encodes the decision once (relates to the `[rule15-candidate]` already raised for pollers in F-072).

### [F-074] Rule 2 — `dsbPollStatus`'s `silent` parameter and its two dead branches are never exercised
- **Piece:** P33
- **Location:** `app/static/index.html:5846` (signature `dsbPollStatus(name, silent = false)`), `:5903-5906`, `:5910-5913` (the two `if (silent ...)` branches)
- **Severity:** low
- **Description:** `dsbPollStatus` is called from exactly one place in the file — `dsbStartPolling`'s `setInterval(() => dsbPollStatus(name), 2000)` (line 5843) — which never passes a second argument, so `silent` is always `false`. The two `if (... && silent ...)` blocks that stop polling on a one-time/silent check (lines 5903-5906 success path, 5910-5913 catch path) are unreachable in the current codebase.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — either remove the unused `silent` parameter and its two dead branches (Rule 2: minimum code that solves the problem), or wire up the presumably-intended one-time-status-check caller if one was planned but never added.

### [F-075] Rule 8 — `dsbLoadProject`'s catch fully swallows a project-load failure with no logging or user-facing feedback
- **Piece:** P33
- **Location:** `app/static/index.html:5572-5575` (`dsbLoadProject`)
- **Severity:** medium
- **Description:** On any failure of `API.get('/api/dataset_builder/status/...')` (network error, 404, malformed JSON), the catch does only `dsbRows = []; dsbAddRow();` — no `console.error`, no `showToast`. This is indistinguishable from successfully loading a genuinely-empty project; a user selecting a project from the dropdown whose status fetch actually failed (e.g. backend down, corrupted state file) sees a single blank row with zero indication anything went wrong, unlike every sibling function in this same piece (`dsbLoadProjects`, `dsbCreateProject`, `dsbDeleteProject`, `dsbSaveForm`, `dsbSaveRows`, `dsbGenSample`, `dsbGenerateAll`) which all log and/or toast on failure.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — add `console.error('Failed to load project:', e)` at minimum, and consider `showToast` since this is a direct user-initiated action (selecting a project), not a background refresh.

### [F-076] Rule 18 — 22 unbraced single-statement `if` bodies from handleCloneVoiceUpload through dsbImport
- **Piece:** P33
- **Location:** `app/static/index.html:5150, 5181, 5185, 5213, 5258, 5419, 5479, 5562, 5580, 5590, 5606, 5620, 5640, 5699, 5703, 5721, 5842, 5858, 5859, 5860, 5908, 5935`
- **Severity:** low
- **Description:** 22 `if (...) <statement>;` one-liners with no `{ }`, spanning multiple functions: `5150` (`if (!file) return;` in `handleCloneVoiceUpload`), `5181` (`if (!await showConfirm(...)) return;` in `deleteCloneVoice`), `5185` (`if (!val.startsWith('clone:')) return;`, same function), `5213` (`if (currentVal) selectEl.value = currentVal;` in `loadLoraDatasets`), `5258` (`if (!await showConfirm(...)) return;` in `deleteLoraDataset`), `5419` (`if (prevVal && models.some(...)) dropdown.value = prevVal;` in `loadLoraModels`), `5479` (`if (!await showConfirm(...)) return;` in `deleteLoraModel`), `5562` (`if (dsbRows.length === 0) dsbAddRow();` in `dsbLoadProject`), `5580` (`if (!name || !name.trim()) return;` in `dsbCreateProject`), `5590` (`if (!dsbCurrentProject) return;` in `dsbDeleteProject`), `5606` (`if (!dsbCurrentProject) return;` in `dsbSaveForm`), `5620` (`if (!dsbCurrentProject) return;` in `dsbSaveRows`), `5640` (`if (last) last.querySelector('input')?.focus();` in `dsbAddRow`), `5699` (`if (!existing) continue;` in `dsbRenderTable`), `5703` (`if (oldStatus === ... ) continue;`, same function), `5721` (`if (row && ... ) audio.pause();` in `dsbStopOthers`), `5842` (`if (dsbPolling) clearInterval(dsbPolling);` in `dsbStartPolling`), `5858`/`5859`/`5860` (three in `dsbPollStatus`'s merge loop: `if (s.status) dsbRows[i].status = s.status;`, `if (s.audio_url) dsbRows[i].audio_url = s.audio_url;`, `if (...) changed.push(i);`), `5908` (`if (!silent) console.error('Poll error:', e);`, same function's catch), `5935` (`if (!file) return;` in `dsbImport`). Same recurring pattern already logged for other pieces of this file (e.g. F-060, F-063, F-069).
- **Status:** fixed-inline (commit `1f41597`)
- **Suggested fix:** add `{ }` around each one-line body, preserving behavior exactly (fix-now criterion 2).

### [F-077] Rule 18 — 19 unbraced single-statement `if` bodies from dsbImport (tail) through loadCheckpoints
- **Piece:** P34
- **Location:** `app/static/index.html:5940, 5961, 6040, 6042, 6044, 6078, 6143, 6146, 6158, 6176, 6260, 6306, 6326, 6349, 6382-6384, 6396, 6446, 6454-6455, 6487, 6514, 6537`
- **Severity:** low
- **Description:** 19 `if (...) <statement>;` one-liners with no `{ }`, spanning multiple functions in the file's final piece: `5940` (`if (!Array.isArray(data)) throw new Error(...);` in `dsbImport`'s tail, just past F-076's last entry), `5961` (`if (r.seed !== '' && r.seed !== undefined) entry.seed = parseInt(r.seed);` in `dsbExport`), `6040`/`6042`/`6044` (three early-return guards in `formatDuration`), `6078` (`if (!badge || !toggle) return;` in `refreshLmStudioStatus`), `6143`/`6146` (`const el = ...; if (el) el.style.display = disp;` / `if (el) el.disabled = true;` in `reattachRunningPollers`'s `show`/`disable` closures), `6158` (`const b = ...; if (b) b.disabled = false;`, same function), `6176` (`if (btn) btn.disabled = false;`, same function), `6260` (`if (!res.ok) throw new Error(...);` in `startPreparer`), `6306` (`if (prepPoller) clearInterval(prepPoller);` in `_pollPreparerLogs`), `6326` (`if (!el) return;`, same function's batch-badge loop), `6349` (`if (!el) return;` in `loadPreparerOutputs`), `6382-6384` (`if (el) el.innerHTML = ok ? ... : ...;` spanning 3 lines, in `_vlChk`), `6396` (`if (!input.value) input.value = c.zips_dir || '';` in `loadVoicelabConfig`), `6446` (`if (running) _resetPauseBtn(...);` in `_vlSetRunning`), `6454-6455` (a 2-line condition `if (stages.includes('name') && ... && !confirm(...)) return;` in `startVoicelab`), `6487` (`if (voicelabPoller) clearInterval(voicelabPoller);` in `pollVoicelab`), `6514` (`if (!listEl) return;` in `loadReports`), `6537` (`if (!listEl) return;` in `loadCheckpoints`). Same recurring pattern already logged for every other piece of this file (e.g. F-060, F-063, F-069, F-076).
- **Status:** fixed-inline (commit `b5318c4`)
- **Suggested fix:** add `{ }` around each one-line body, preserving behavior exactly (fix-now criterion 2). Verified with `node --check` on the extracted `<script>` content before and after, and confirmed via `diff` that every change was a pure brace addition with no other text altered.

### [F-078] Rule 8 — `cancelPreparer` swallows a cancel-request failure with `/* ignore */`, a third instance of the `cancelBatchScript`/`cancelBatchReview` vs. `cancelScript`/`cancelReview` inconsistency (F-052)
- **Piece:** P34
- **Location:** `app/static/index.html:6272` (`cancelPreparer`) vs `:6478-6481` (`cancelVoicelab`, same piece) and F-052's `cancelScript`/`cancelReview`
- **Severity:** low
- **Description:** `cancelPreparer` does `try { await API.post(url, {}); } catch (e) { /* ignore */ }` — a failed cancel request (network error, 500, task already finished) is completely invisible: the Cancel button stays visible, the preparer/batch-preparer job keeps running server-side, and the user gets no feedback that their click did nothing. Its sibling in the very same piece, `cancelVoicelab` (lines 6478-6481), handles the identical failure mode correctly: `catch (e) { showToast('Cancel failed: ' + (e.message || 'unknown'), 'warning'); }`. This is the same inconsistency already logged as F-052 (`cancelBatchScript`/`cancelBatchReview` swallow vs. `cancelScript`/`cancelReview` toast) — `cancelPreparer` is a third cancel-button handler in this file that silently swallows where the established (and locally adjacent, in `cancelVoicelab`) convention is to toast.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — mirror `cancelVoicelab`'s `showToast('Cancel failed: ...', 'warning')` in `cancelPreparer`'s catch, closing out the F-052 pattern across all 5 of the file's cancel-button handlers.

### [F-079] Rule 10 — `_pollPreparerLogs` and `pollVoicelab` both retry forever silently on poll error, agreeing with each other but disagreeing with `pollLoraTraining`'s give-up-on-first-error policy (F-073) and `pollPersonaStatus`'s abort-with-toast policy (F-058)
- **Piece:** P34
- **Location:** `app/static/index.html:6342` (`_pollPreparerLogs`'s `catch (e) { /* network hiccup — keep polling */ }`) and `:6507` (`pollVoicelab`'s `catch (e) { /* keep polling through hiccups */ }`) vs `:5368-5371` (`pollLoraTraining`, F-073) and `app/static/index.html` P29's `pollPersonaStatus` (F-058)
- **Severity:** medium
- **Description:** This piece's two job-completion pollers (`_pollPreparerLogs` for the Preparer tab, `pollVoicelab` for the Voice Lab tab) are internally consistent with each other — both use the identical policy of swallowing any `API.get('/api/status/...')` failure with a comment-only catch and letting `setInterval` retry on the next tick forever, with no cap, no logging, and no user-visible signal. This is the same "retry forever silently" half of the inconsistency already flagged in F-073 (where it was `dsbPollStatus`'s default behavior) and in F-058 (where it was `pollReviewBatch`'s behavior) — so this piece *adds two more instances* of that policy rather than resolving the split. The audit has now found 4 pollers using "retry forever silently" (`pollReviewBatch`, `dsbPollStatus`, `_pollPreparerLogs`, `pollVoicelab`), 1 using "give up immediately, no toast" (`pollLoraTraining`), and 1 using "give up immediately, with toast" (`pollPersonaStatus`) — three distinct policies for the identical "the status-poll fetch itself failed" decision, spread across 6 hand-rolled pollers in this one file, none of which share an implementation.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — same root fix as F-073/F-058: pick one consistent policy (e.g. a shared poller helper with a consecutive-failure counter and a visible warning after N failures) and apply it to all 6 pollers identified across this audit (`pollReviewBatch`, `pollPersonaStatus`, `pollLoraTraining`, `dsbPollStatus`, `_pollPreparerLogs`, `pollVoicelab`) plus `_pollScriptBatchLogs` (F-053), which has its own seventh variant (bounded backoff, no cap, no toast).

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

### [F-083] Rule 8 — `_parse_llm_output`'s JSON-parse-failure fallback is byte-for-byte identical to a legitimate LLM "couldn't determine" answer
- **Piece:** P36 — llm_enricher.py
- **Location:** `llm_enricher.py:93-113` (`_parse_llm_output`) vs the prompt's own instruction at `:85` ("If any information cannot be determined, use 'N/A'")
- **Severity:** medium
- **Description:** When neither regex matches or `json.loads` raises, `_parse_llm_output` falls back to `{"speaker_attribution": "N/A", "narration_style": "N/A", "emotional_tone": "N/A"}` — logging a `logger.warning` first, so it isn't silent in the log stream, but the *data* this returns is indistinguishable from the LLM genuinely answering "N/A" to all three fields per its own prompt instructions (line 85). `alexandria_preparer_rocm_compatible.py`'s enrichment phase (lines 3274-3276) reads these three keys straight into the final `word_segments` output with a `"N/A"` default of its own, so by the time the enriched dataset is consumed downstream, "the LLM call/parse failed for this chunk" and "the LLM legitimately couldn't classify this chunk" are the same value with no flag distinguishing them.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — have the failure path return a distinct sentinel (e.g. `"PARSE_ERROR"`) or set an extra `_enrichment_failed: true` key on the dict, so downstream consumers (and a human skimming the enriched JSON) can tell a parse failure apart from a genuine LLM "N/A".

### [F-084] Rule 8 / Rule 17 — `enrich_transcript_chunk` silently no-ops on failure (mutating-and-returning the same unmodified `chunk`) with no failure marker surviving to the output file
- **Piece:** P36 — llm_enricher.py
- **Location:** `llm_enricher.py:37-62` (`enrich_transcript_chunk`), called from `main()` at `:143` (and again wrapped in its own `try/except` at `:142-148`)
- **Severity:** medium
- **Description:** Two issues in one function. (1) Rule 8: on `self.llm` being unset (line 39-41) or any exception from the LLM call/parse (line 59-62), the function logs and returns `chunk` completely unmodified — no `speaker_attribution`/`narration_style`/`emotional_tone` keys are added at all, not even the "N/A" sentinel. `main()`'s outer `try/except` (lines 142-148) has an independent, redundant third layer of the same swallow-and-continue behavior (catches any exception from the call itself and appends the original `chunk`). The net effect: a chunk that fails enrichment for any reason ends up with no enrichment keys, and `alexandria_preparer_rocm_compatible.py`'s flattening step (`chunk.get("speaker_attribution", "N/A")`, lines 3274-3276) silently defaults it to the same "N/A" used for a successful-but-uncertain LLM answer — same downstream ambiguity as F-083, compounding it. The overall subprocess still exits 0 (`main()` never sets a non-zero exit code or even a summary count for per-chunk failures), so the parent `alexandria_preparer_rocm_compatible.py` (which treats any non-zero `res.returncode` as fatal, line 3252-3254) has no way to detect that some chunks silently failed enrichment. (2) Rule 17: on the success path, `enrich_transcript_chunk` mutates the `chunk` parameter in place via `chunk.update(enriched_data)` (line 56) *and* returns the same object — `main()`'s caller relies on the return value (`enriched_chunk = enricher.enrich_transcript_chunk(chunk)`; `enriched_data.append(enriched_chunk)`), not the in-place mutation, since `chunk` and `transcript_data[i]` are the same object handed across a function-boundary (not case (c)'s same-function load/mutate/save). Matches the pattern already logged as F-016/F-037.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — for Rule 8: track and log a per-run count of chunks that fell through to "no enrichment data" vs. successfully enriched, and consider a non-zero-but-distinct exit code (or a summary line `main()` can grep) when any chunk failed, so the parent script can choose to treat partial-enrichment differently from total success. For Rule 17: build and return a new dict (`{**chunk, **enriched_data}`) instead of mutating the parameter in place.

### [F-085] Rule 9 — `name_voices.py`'s apply loop has no exception handling around `os.rename`; a mid-loop failure leaves on-disk directories renamed while the manifest backup/write either lags behind or never happens
- **Piece:** P37 — name_voices.py
- **Location:** `name_voices.py:231-249` (rename loop + manifest write in `main()`)
- **Severity:** medium
- **Description:** The `--apply` path backs up `manifest.json` (line 227-229) before any change — a real safety net — but the `for e, new in renames:` loop that follows (lines 232-245) has no `try/except` around `os.rename(old_dir, new_dir)`. If rename N of M raises (e.g. `OSError` from a permission error, a stale file handle, or a cross-filesystem move), the script crashes via an uncaught traceback: directories 1..N-1 have already been renamed on disk, but `manifest.json` is only written once, after the entire loop completes (lines 247-248) — so the on-disk `.bak` and the not-yet-rewritten `manifest.json` both still show the *old* names for the already-renamed directories, with no record (beyond scrollback in stdout, which a backgrounded subprocess call from `app.py` may not preserve) of exactly which renames had already succeeded before the crash. A re-run after fixing whatever caused the failure would treat those already-renamed directories as "adapter dir missing" (line 240-241's `else` branch) for entries whose `id` in the manifest is still the old name, since `_is_named`/`candidates` filtering depends on the manifest, not the filesystem.
- **Status:** needs-decision
- **Suggested fix:** see needs-decision — wrap each `os.rename` in its own `try/except OSError`, log the failure, leave that entry's manifest `id`/`name` unchanged (so a re-run can retry just that one), and continue the loop instead of letting one failure abort the whole batch with already-applied-but-unrecorded renames.
