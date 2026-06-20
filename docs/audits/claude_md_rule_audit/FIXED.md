# CLAUDE.md Rule-Compliance Audit — Fixed Findings

Plan: `docs/superpowers/plans/2026-06-19-claude-md-rule-compliance-audit.md`
Progress: `docs/audits/claude_md_rule_audit/PROGRESS.md`
Open findings: `docs/audits/claude_md_rule_audit/FINDINGS.md`

Entries here were moved out of `FINDINGS.md` once resolved — cut, not copied, so each finding lives in exactly one of the two files at any time. `F-###` numbering is preserved from the original audit, not renumbered on move.

---

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
- **Status:** fixed-inline (commit `fa0d613`)
- **Suggested fix:** see needs-decision — log the parse failure distinctly from the legitimate "file doesn't exist yet" case (e.g. `print(f"Warning: manifest.json corrupted, rebuilding from scratch: {e}")`) so the data loss is visible instead of indistinguishable from first-run behavior.

### [F-015] Rule 8 — `dedupe_speakers`'s corrupted alias-registry load is silently reset to `{}`, then the registry file is overwritten and prior cross-book aliases are lost
- **Piece:** P12a — app/review_script.py
- **Location:** `app/review_script.py:362-368` (registry load in `dedupe_speakers`), persisted at `:470-478`
- **Severity:** medium
- **Description:** When `registry_path` (the cross-book canonical-alias file, e.g. populated by `find_nicknames.py`) exists but fails to parse (`json.JSONDecodeError`/`ValueError`/`OSError`), `except (...): registry = {}` discards the error with zero logging and proceeds as if no registry existed yet. At the end of the same call, `registry` (now empty) is updated with only this run's `clean_map` and written back via `atomic_json_write(registry, registry_path)` (lines 470-478), permanently erasing every other book's previously recorded alias→canonical entries — indistinguishable from the legitimate first-run "no registry yet" case. Same pattern and severity as F-011 (`generate_personas.py`'s `manifest.json` load).
- **Status:** fixed-inline (commit `fa0d613`)
- **Suggested fix:** see needs-decision — log the parse failure distinctly (e.g. `print(f"  Warning: alias registry {registry_path} corrupted, rebuilding from scratch: {e}")`) so the data loss is visible instead of silently indistinguishable from "no registry yet".

### [F-029] Rule 9 — `/api/lmstudio/optimize` reloads (unloads + loads) the LLM model with no GPU lock check at all
- **Piece:** P16 — app/app.py (/api/system/stats → /api/upload)
- **Location:** `app/app.py:1589-1619` (`lmstudio_optimize`) calling `app/lmstudio_settings.py:243-286` (`apply_lmstudio_settings`) / `:292-321` (`apply_remote_lmstudio_settings`)
- **Severity:** medium
- **Description:** This route toggles the loaded LM Studio model between VRAM-safe and default settings by unloading and reloading it (`lms unload` + `lms load` locally, or the SSH equivalent remotely) — a real VRAM-affecting operation, the same class of operation `check_global_gpu_lock`/`claim_gpu_task` exist to serialize against (per CLAUDE.md's "Concurrency model" section and Rule 9). Unlike every `GPU_TASKS` member, this route never calls `check_global_gpu_lock` and isn't a `process_state` entry at all (it's a synchronous request/response route, not a background task), so nothing stops a user from clicking "optimize" while `review`, `audio`, `script`, or any other GPU task is mid-run and actively holding VRAM against the currently-loaded model — the reload could pull the rug out from under that in-flight LLM/TTS call. By contrast, the same unload/reload functions are also invoked from `ensure_ideal_settings` inside `review_script.py`/`find_nicknames.py`'s `main()`, but those calls happen at the very start of an already-`claim_gpu_task`'d run, before any inference — that call site is safe by construction; this route's call site is not, since it has no relationship to the lock at all.
- **Status:** fixed-inline (commit `25b6216`)
- **Suggested fix:** see needs-decision — either reject the optimize request with 400 if `check_global_gpu_lock` would currently fail (treating it like a lightweight, non-`process_state` GPU operation that still must respect the lock), or confirm this is accepted risk because the Setup-tab toggle is a deliberate, infrequent, user-driven action and document that explicitly near the route.

### [F-031] Rule 8 — `GET /api/voices` silently returns an empty list on a corrupted script/voice-config file, indistinguishable from "no script yet"
- **Piece:** P18 — app/app.py (/api/voices → /api/suggest_voices)
- **Location:** `app/app.py:2678-2679` (`except (json.JSONDecodeError, ValueError): pass` on `SCRIPT_PATH`), `:2690-2691` (same pattern on `VOICE_CONFIG_PATH`)
- **Severity:** low
- **Description:** Both `try/except` blocks in `get_voices()` swallow a JSON parse failure with zero logging (not even a `print`), then fall through to the "no voices" / "empty config" path. A user whose `annotated_script.json` got truncated/corrupted (e.g. a crash mid-write before this codebase's `atomic_json_write` was used everywhere, or external tampering) sees the Voices tab render as if no script had ever been generated, with no signal that the file exists but failed to parse. Lower severity than F-011/F-015 (no write-back, so no data loss) but the same "silently treat corruption as absence" pattern this audit has flagged repeatedly.
- **Status:** fixed-inline (commit `fa0d613`)
- **Suggested fix:** see needs-decision — log a warning (e.g. `logger.warning(f"Failed to parse {SCRIPT_PATH}: {e}")`) before falling through, so a corrupted file is distinguishable from a missing one in `logs/api/`.

### [F-032] Rule 9 — `POST /api/chunks/{index}/generate` runs real TTS/GPU inference with no GPU-lock check at all
- **Piece:** P19 — app/app.py (/api/audiobook → /api/review/checkpoints)
- **Location:** `app/app.py:3081-3093` (`generate_chunk_endpoint`) calling `app/project.py:371` (`generate_chunk_audio`, which calls `engine.get_engine()` + `engine.generate_voice(...)`)
- **Severity:** high
- **Description:** This route backgrounds a single-chunk TTS generation via `BackgroundTasks.add_task` with no `check_global_gpu_lock`/`claim_gpu_task` call, and it has no `process_state` entry at all (it isn't in `GPU_TASKS` or `NON_GPU_TASKS`). `generate_chunk_audio` calls `self.get_engine()` (loads/uses the TTS model) and `engine.generate_voice(...)` — genuine GPU/VRAM work, the same class of operation its multi-chunk sibling route `/api/generate_batch` (`app/app.py:3211-3268`) correctly guards with `check_global_gpu_lock("audio")` then `claim_gpu_task("audio")`. As written, a user can click "regenerate this line" in the Editor tab while `review`, `script`, `persona`, batch `audio`, or any other `GPU_TASKS` member is actively running, racing for VRAM exactly as CLAUDE.md's "Concurrency model" section and Rule 9 describe — same risk class as F-029 (`/api/lmstudio/optimize`), but here the affected operation is the everyday single-chunk regenerate action used throughout the Editor tab, not an infrequent Setup-tab toggle.
- **Status:** fixed-inline (commit `25b6216`)
- **Suggested fix:** see needs-decision — add `check_global_gpu_lock("audio")` before backgrounding the task (mirroring `/api/generate_batch`), or — since single-chunk regen from the Editor is plausibly meant to be usable while, say, `persona` runs — explicitly register a lightweight pairing such as routing it through the existing `"audio"` task slot (`check_global_gpu_lock("audio")` + setting `process_state["audio"]["running"]` for the task's duration) so it can't race a concurrent GPU task.

### [F-035] Rule 8 — `_load_voice_library` silently resets a corrupted `voice_library.json` to empty, and the next save overwrites it, erasing every cast/shared entry
- **Piece:** P20 — app/app.py (/api/scripts → /api/voice_library/apply_bulk)
- **Location:** `app/app.py:3612-3623` (`_load_voice_library`), write-back via `_save_voice_library`/`_save_voice_library_async` (`:3626-3642`)
- **Severity:** medium
- **Description:** When `VOICE_LIBRARY_PATH` exists but fails to parse (`json.JSONDecodeError`/`ValueError`), `except (json.JSONDecodeError, ValueError): pass` discards the error with zero logging and returns the freshly-initialized `{"shared": {}, "casts": {}}` as if the library had never been populated. Nearly every voice_library route (`voice_library_create_cast`, `voice_library_delete_cast`, `voice_library_delete_member`, `voice_library_save`, `voice_library_apply`, `voice_library_apply_bulk`) follows the `_load_voice_library()` call with an unconditional `_save_voice_library_async(lib)`/`_save_voice_library(lib)` write-back of that same (now-empty-if-corrupted) `lib` object — so the very next library mutation after a corruption event permanently erases every other cast and every shared-pool entry (e.g. the cross-book narrator voice). Same pattern and severity as F-011 (`generate_personas.py` manifest) and F-015 (`review_script.py` alias registry), now confirmed a third time in `app.py` itself.
- **Status:** fixed-inline (commit `fa0d613`)
- **Suggested fix:** see needs-decision — log the parse failure distinctly (e.g. `logger.warning(f"voice_library.json corrupted, rebuilding from scratch: {e}")`) so the data loss is visible instead of indistinguishable from "no library yet"; consider also refusing to overwrite (return 500) rather than silently proceeding with an empty library when the file demonstrably existed and had content.

### [F-036] Rule 8 — `_load_manifest` silently resets a corrupted designed-voice/clone-voice manifest to `[]`, and every save route overwrites it unconditionally
- **Piece:** P21 — app/app.py (/api/voice_design/preview → /api/clone_voices/{voice_id})
- **Location:** `app/app.py:3986-3994` (`_load_manifest`, defined at the end of P20's range but consumed exclusively by P21's `voice_design_save`/`voice_design_delete`/`clone_voices_upload`/`clone_voices_delete`)
- **Severity:** medium
- **Description:** Identical pattern to F-035/F-011/F-015: `except (json.JSONDecodeError, ValueError): pass` swallows a corrupt-manifest parse failure with zero logging, returning `[]`. `voice_design_save` (`app/app.py:4043-4051`) and `clone_voices_upload` (`:4111-4117`) both `_load_manifest(...)` then unconditionally `manifest.append(...)` and `_save_manifest(...)` — no check that the load actually succeeded vs. fell back to the corruption case — so the first voice saved/uploaded after `manifest.json` becomes corrupted silently erases every previously-saved designed voice or uploaded clone-voice entry from the manifest (the underlying `.wav` files on disk are untouched, but become permanently unreferenced/orphaned since nothing in `voice_design_list`/`clone_voices_list` can find them anymore).
- **Status:** fixed-inline (commit `fa0d613`)
- **Suggested fix:** see needs-decision — same fix as F-035: log the parse failure distinctly from "no manifest yet," and consider having callers refuse to save/overwrite if the manifest file existed but failed to parse, rather than silently proceeding from an empty list.

### [F-038] Rule 9 — `POST /api/voice_design/preview` runs synchronous local TTS/GPU inference with no GPU-lock check, racing any in-flight background GPU task
- **Piece:** P21 — app/app.py (/api/voice_design/preview → /api/clone_voices/{voice_id})
- **Location:** `app/app.py:4000-4018` (`voice_design_preview`) calling `app/tts.py:776-829` (`TTSEngine.generate_voice_design`)
- **Severity:** medium
- **Description:** Unlike F-029/F-032 (which flagged unlocked *backgrounded* GPU work), this route calls `engine.generate_voice_design(...)` directly and synchronously inside the request handler — no `BackgroundTasks`, no `process_state` entry, no `await asyncio.to_thread`. `generate_voice_design` calls `self._init_local_design()` (loads a local model) and `model.generate_voice_design(...)` (real GPU inference via `torch`), genuinely VRAM-affecting work, with zero call to `check_global_gpu_lock`/`claim_gpu_task`. The risk profile is narrower than F-032's backgrounded case — the caller's own HTTP request blocks for the full duration and the GPU usage can't outlive that request — but it's still a real, unguarded race: a user can submit a voice-design preview while `review`/`audio`/`persona`/`script`/etc. is actively running and holding VRAM via the documented `GPU_TASKS` lock, and this route has no awareness of that lock at all (it doesn't even check, let alone wait or reject). The route is also synchronous Python running inside an `async def` handler with no `to_thread` offload, so it blocks the FastAPI event loop for the full TTS-generation duration — a separate (not Rule-9) concern but compounding the same call site.
- **Status:** fixed-inline (commit `25b6216`)
- **Suggested fix:** see needs-decision — at minimum call `check_global_gpu_lock("voice_design_preview"-equivalent)` (or reuse an existing slot) before invoking `generate_voice_design`, so a preview request gets a clean 400 instead of silently racing a running `GPU_TASKS` member for VRAM; separately consider `await asyncio.to_thread(...)` so the synchronous inference doesn't block the event loop for other requests.

### [F-039] Rule 9 — `POST /api/lora/test` runs synchronous GPU TTS inference with no GPU-lock check at all
- **Piece:** P22 — app/app.py (/api/lora/upload_dataset → /api/lora/preview/{adapter_id})
- **Location:** `app/app.py:4539-4596` (`lora_test_model`) calling `engine.generate_voice(...)` (line 4582)
- **Severity:** medium
- **Description:** Same gap class as F-038 (`/api/voice_design/preview`), found a second time in this range. `lora_test_model` calls `project_manager.get_engine()` then `engine.generate_voice(...)` directly inside the `async def` handler — real GPU/VRAM inference — with no `check_global_gpu_lock`/`claim_gpu_task` call and no `process_state` entry at all. A user can hit "Test" on a LoRA adapter while `review`/`audio`/`script`/`lora_training`/etc. holds the documented `GPU_TASKS` lock, and this route has no awareness of the lock whatsoever. The request blocks the FastAPI event loop for the duration of `engine.generate_voice` with no `asyncio.to_thread` offload, compounding the same call site (as also noted for F-038).
- **Status:** fixed-inline (commit `25b6216`)
- **Suggested fix:** see needs-decision — add `check_global_gpu_lock(...)` (reusing an existing slot such as `"audio"`, or a new lightweight one) before calling `generate_voice`, and consider `await asyncio.to_thread(...)` so the synchronous call doesn't block the event loop.

### [F-040] Rule 9 — `POST /api/lora/preview/{adapter_id}` runs synchronous GPU TTS inference with no GPU-lock check at all
- **Piece:** P22 — app/app.py (/api/lora/upload_dataset → /api/lora/preview/{adapter_id})
- **Location:** `app/app.py:4600-4656` (`lora_preview`) calling `engine.generate_voice(...)` (line 4646)
- **Severity:** medium
- **Description:** Identical gap to F-039, in the immediately adjacent route. `lora_preview` generates (and caches) a preview sample via `engine.generate_voice(...)` with no `check_global_gpu_lock`/`claim_gpu_task` call and no `process_state` entry — same unguarded VRAM race against any running `GPU_TASKS` member, same synchronous-in-`async def` event-loop blocking concern. Both `lora_test_model` and `lora_preview` sit directly between `lora_train` (line 4391, correctly does `check_global_gpu_lock("lora_training")` + `claim_gpu_task("lora_training")`) and the Dataset Builder's `generate_batch` (P23, correctly locked) — they are the only two GPU-touching routes in this file's LoRA section with zero lock awareness.
- **Status:** fixed-inline (commit `25b6216`)
- **Suggested fix:** see needs-decision — same fix as F-039; both routes likely want the identical treatment since they share the `engine.generate_voice` call pattern almost verbatim.

### [F-043] Rule 9 — `POST /api/dataset_builder/generate_sample` runs synchronous GPU TTS inference with no GPU-lock check, and isn't registered in `process_state["dataset_builder"]` at all
- **Piece:** P23 — app/app.py (/api/dataset_builder/*)
- **Location:** `app/app.py:4761-4816` (`dataset_builder_generate_sample`) vs `:4818-4924` (`dataset_builder_generate_batch`, which correctly calls `check_global_gpu_lock("dataset_builder")` at line 4821 and `claim_gpu_task("dataset_builder")` at line 4922)
- **Severity:** high
- **Description:** Same gap class as F-038/F-039/F-040, but worse here: this route's sibling in the very same section (`generate_batch`) demonstrates the correct pattern exists and is known, making the omission in `generate_sample` more clearly a gap rather than an unexplored area. `generate_sample` calls `project_manager.get_engine()` then `engine.generate_voice_design(...)` synchronously inside the `async def` handler — real GPU inference — with no `check_global_gpu_lock` call, no `claim_gpu_task` call, and it never touches `process_state["dataset_builder"]["running"]` at all (unlike `generate_batch`, which sets it `True`/`False` around the whole job). This means: (1) a single-sample preview can race any other `GPU_TASKS` member for VRAM with zero lock awareness, exactly like F-038/39/40; and (2) it can *also* race `generate_batch` on the very same `dataset_builder` work directory/state file concurrently, since neither route's lock state is visible to the other (a `generate_sample` call mid-flight during a `generate_batch` run wouldn't be blocked by `check_global_gpu_lock`, because `generate_sample` never calls it, and the two would both call `_save_builder_state` on the same `state.json` without coordination).
- **Status:** fixed-inline (commit `25b6216`)
- **Suggested fix:** see needs-decision — add `check_global_gpu_lock("dataset_builder")` before calling `generate_voice_design`, mirroring `generate_batch`; since this route is synchronous rather than backgrounded, it likely shouldn't set `process_state["dataset_builder"]["running"]` itself (that field is polled by the UI as "batch is running"), but it must still respect the same lock so it can't run concurrently with `generate_batch` or any other GPU task.

### [F-046] Rule 8 — `_load_voicelab_config` silently falls back to defaults on a corrupted `voicelab_config.json`, with zero logging
- **Piece:** P25 — app/app.py (/api/voicelab/*)
- **Location:** `app/app.py:5303-5313` (`_load_voicelab_config`)
- **Severity:** low
- **Description:** Same recurring pattern as F-011/F-015/F-035/F-036 (already logged for other manifest/config files in this repo), found again here for `voicelab_config.json`: `except (json.JSONDecodeError, ValueError, OSError): pass` discards a corrupted-file parse failure with no `logger.warning`/`logger.error`, silently returning `VOICELAB_DEFAULTS` instead. Every voicelab route (`voicelab_get_config`, `voicelab_save_config`, `voicelab_inspect`, `voicelab_start`, and `_resolve_preparer_interpreter` via `_load_voicelab_config()["rocm_python"]`) calls this helper, so a corrupted config silently reverts paths like `rocm_python`/`pipeline_repo`/`zips_dir` to their environment-variable/repo-relative defaults with no signal to the user that their saved settings were lost — `voicelab_save_config` (line 5362) would then happily overwrite the corrupted file with `cfg` built from defaults plus whatever the current request supplied, permanently erasing any other previously-saved fields.
- **Status:** fixed-inline (commit `fa0d613`)
- **Suggested fix:** see needs-decision — same fix as F-035/F-036: `logger.warning` the parse failure distinctly, and consider whether `voicelab_save_config` should refuse to silently overwrite a config that failed to parse (vs. one that simply didn't exist yet).

### [F-048] Rule 18 — 10 single-line `if` bodies without braces in showToast→testLlmConnection
- **Piece:** P27 — app/static/index.html (showToast → testLlmConnection, lines ~1790-2207)
- **Location:** `app/static/index.html:1842` (`confirmIfRemote`), `:1851` (`escapeHtml`), `:1888-1889` (`notifyJobDone`), `:1979` (`API._handleError`), `:1983` (`API._handleError`), `:2101-2103` (`_computeAutoSettings`), `:2148` (`renderActiveLlmModeBadge`)
- **Severity:** low
- **Description:** Per CLAUDE.md Rule 18, every `if`/`for`/`while` in this file must brace its body even when it's a single statement. Found 10 violations, all single-line `if (...) <statement>;` with no `{ }`: `if (currentLlmMode !== 'remote') return true;` (1842); `if (str == null) return '';` (1851); `if (!('Notification' in window) || Notification.permission !== 'granted') return;` (1888); `if (document.visibilityState === 'visible' && document.hasFocus()) return;` (1889); `if (res.ok) return;` (1979); `if (body && body.detail) detail = body.detail;` (1983); `if (gpuName) parts.push(gpuName);` (2101); `if (ramGb) parts.push(...);` (2102); `if (stats.cpu_count) parts.push(...);` (2103); `if (!badge) return;` (2148).
- **Status:** fixed-inline (commit `574ec35`)
- **Suggested fix:** add `{ }` around each one-line body, preserving behavior exactly — fix-now per audit plan.

### [F-051] Rule 18 — 11 single-line `if` bodies without braces in loadConfig→_onReviewDone (plus toggleReviewBatchMode)
- **Piece:** P28
- **Location:** `app/static/index.html:2457` (`file-upload` change handler), `:2496` (`btn-gen-script` click handler), `:2521` (`_resetPauseBtn`), `:2549` (`_makePauseResumeHandler`'s returned handler), `:2634` (`_startBatchScript`), `:2669,2675,2678` (`_pollScriptBatchLogs`), `:2691` (`_pollScriptBatchLogs`'s `state.tasks.forEach` callback), `:2724` (`_showReviewControls`), `:2796` (`toggleReviewBatchMode`, just past `_onReviewDone` but inside this piece's contiguous range)
- **Severity:** low
- **Description:** Per CLAUDE.md Rule 18, every `if`/`for`/`while` in this file must brace its body even when it's a single statement. Found 11 violations, all single-line `if (...) <statement>;` with no `{ }`: `if (fileInput.files.length === 0) return;` (2457); `if (!scriptBatchPoller) genBtn.disabled = false;` (2496); `if (!btn) return;` (2521); `if (btn.disabled) return;` (2549); `if (!(await confirmIfRemote('this batch script generation'))) return;` (2634); `if (scriptBatchPoller) clearTimeout(scriptBatchPoller);` (2669); `if (myGen !== scriptBatchPollGen) return;` (2675 and again at 2678); `if (!el) return;` (2691); `if (show) _resetPauseBtn('btn-pause-review');` (2724); `if (isBatch) loadReviewBatchScripts();` (2796).
- **Status:** fixed-inline (commit `c5c1a46`)
- **Suggested fix:** add `{ }` around each one-line body, preserving behavior exactly — fix-now per audit plan.

### [F-055] Rule 18 — 14 single-line `if` bodies without braces in _loadScriptList→pollPersonaStatus
- **Piece:** P29
- **Location:** `app/static/index.html:2847` (`_sortScriptList`), `:2887` (`startBatchReview`), `:2956` (`loadCharacterAliases`), `:2980` (`addAliasRow`), `:2996` (`saveCharacterAliases`), `:3010-3013` (`_formatBookStats`, 4 lines), `:3018,3022` (`_formatTotalsLine`, 2 lines), `:3028` (`_updateReviewBatchTotals`), `:3054` (`pollReviewBatch`), `:3063` (`pollReviewBatch`'s `state.tasks.forEach` callback)
- **Severity:** low
- **Description:** Per CLAUDE.md Rule 18, every `if`/`for`/`while` in this file must brace its body even when it's a single statement. Found 14 violations, all single-line `if (...) <statement>;` with no `{ }`: `if (!list.length) return;` (2847); `if (!(await confirmIfRemote('this batch review'))) return;` (2887); `if (show) panel.style.display = 'block';` (2956); `if (placeholder) placeholder.remove();` (2980); `if (a && c) map[a] = c;` (2996); `if (s.narrators_merged) txt += ...;` / `if (s.speakers_merged) txt += ...;` / `if (s.batches_failed) txt += ...;` / `if (s.batches_skipped_vram) txt += ...;` (3010-3013); `if (!t || !t.books_done) return ...;` (3018); `if (t.batches_failed) txt += ...;` (3022); `if (!el) return;` (3028); `if (reviewBatchPoller) clearInterval(reviewBatchPoller);` (3054); `if (!cb) return;` (3063).
- **Status:** fixed-inline (commit `e81f926`)
- **Suggested fix:** add `{ }` around each one-line body, preserving behavior exactly — fix-now per audit plan.

### [F-060] Rule 18 — 15 single-line `if` bodies without braces in createVoiceCard→submitCastApplyBulk
- **Piece:** P30
- **Location:** `app/static/index.html:3411` (`renderVoiceSuggestions`), `:3433,3435` (`applyVoiceSuggestion`), `:3460` (`applyVoiceSuggestion`), `:3495` (`setCastStatus`), `:3560` (`createCast`), `:3570,3571` (`deleteCast`), `:3589` (`openCastSave`), `:3659,3660` (`submitCastSave`), `:3692` (`openCastApply`), `:3727` (`submitCastApply`), `:3741` (`openCastApplyBulk`), `:3833` (`submitCastApplyBulk`)
- **Severity:** low
- **Description:** Per CLAUDE.md Rule 18, every `if`/`for`/`while` in this file must brace its body even when it's a single statement. Found 15 violations, all single-line `if (...) <statement>;` with no `{ }`: `if (banner) banner.remove();` (3411, 3460 — two separate functions); `if (!sugg) return;` / `if (!card) return;` (3433, 3435); `if (el) el.innerHTML = ...;` (3495); `if (!name) return;` (3560); `if (!window._selectedCast) return;` (3570, 3589, 3692, 3741 — four separate functions); `if (!confirm(...)) return;` (3571); `if (ns) msg += ...;` / `if (skipped > 0) msg += ...;` (3659-3660); `if (sel && sel.value) mapping[char] = sel.value;` (3727, 3833 — two separate functions with verbatim-identical bodies, see also F-062).
- **Status:** fixed-inline (commit `90f728d`)
- **Suggested fix:** add `{ }` around each one-line body, preserving behavior exactly — fix-now per audit plan. Verified with `node --check` on the extracted `<script>` content before committing.

### [F-063] Rule 18 — 10 single-line `if` bodies without braces in collectVoiceConfig→exportM4B
- **Piece:** P31
- **Location:** `app/static/index.html:3921, 3963, 3971, 4239, 4252, 4270, 4355, 4604, 4659, 4664`
- **Severity:** low
- **Description:** Ten `if (...) <statement>;` one-liners with no `{ }`: `3921` (`if (cards.length === 0) return;` in `debouncedSaveVoices`), `3963` (`if (!audio.paused && !audio.ended) return true;` in `isAudioPlaying`), `3971` (`if (!tr) return false;` in `updateChunkRow`), `4239` (`if (toast) toast.hide();` in `undoDeleteChunk`), `4252` (`if (isPlayingSequence) return;` in `stopOthers`), `4270` (`if (!isPlayingSequence) return;` in `playSequence`'s `playNext`), `4355` (`if (!tr) return;` in `saveRowEdits`), `4604` (`if (!await showConfirm(...)) return;` in the merge button handler), `4659` (`if (!file) return;` in the M4B cover-upload handler), `4664` (`if (!resp.ok) throw new Error(...);` in the same handler). Same recurring pattern already logged for other pieces of this file (e.g. F-060).
- **Status:** fixed-inline (commit `52cb10f`)
- **Suggested fix:** add `{ }` to all 10, preserving behavior exactly (fix-now criterion 2).

### [F-069] Rule 18 — Six unbraced single-statement `if` bodies in `pollLogs`, `loadScript`, `deleteScript`
- **Piece:** P32
- **Location:** `app/static/index.html:4724`, `:4727`, `:4733` (all in `pollLogs`), `:4745` (`pollLogs`'s `onDone` continuation), `:4821` (`loadScript`), `:4845` (`deleteScript`)
- **Severity:** low
- **Description:** Six `if` statements in this piece have a single-statement body with no `{ }`: `if (myGen !== _pollLogsGen[taskName]) return;` (×2, lines 4724 and 4727), `if (onDone) onDone(status);` (line 4733), `if (tbody) tbody.innerHTML = '';` (line 4745), `if (!await showConfirm(...)) return;` in `loadScript` (line 4821), and the identical guard in `deleteScript` (line 4845). Per Rule 18 these all need braces to prevent a future second statement silently falling outside the conditional.
- **Status:** fixed-inline (commit `d1c34fe`)
- **Suggested fix:** add `{ }` around each single-line body, preserving behavior exactly.

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

### [F-107] Rule 2 — `transcribe_with_whisper_v3` is dead code, zero callers anywhere in the repo
- **Piece:** P42b — alexandria_preparer_rocm_compatible.py (ASR transcription)
- **Location:** `alexandria_preparer_rocm_compatible.py:647-691` (`transcribe_with_whisper_v3`)
- **Severity:** low
- **Description:** `choose_and_transcribe`'s fallback chain (lines 993-1058) dispatches to exactly three backends — `transcribe_with_wav2vec2`, `transcribe_with_insanely_fast_whisper`, `transcribe_with_whisperx_cpu` — gated by `TRANSFORMERS_WHISPER_AVAILABLE` / `INSANELY_FAST_WHISPER_AVAILABLE` / `WHISPERX_AVAILABLE` respectively. `transcribe_with_whisper_v3` (OpenAI Whisper-large-v3 via `transformers.pipeline`, CPU) is defined alongside them with the same signature shape and docstring style ("best model, CPU stable") but is never referenced by `choose_and_transcribe` or anywhere else — confirmed via `grep -rn "transcribe_with_whisper_v3" .` returning only its own `def` line. It reuses the same `TRANSFORMERS_WHISPER_AVAILABLE` flag as `transcribe_with_wav2vec2` (line 649 `if not TRANSFORMERS_WHISPER_AVAILABLE: raise ImportError(...)`) but that flag only gates the Wav2Vec2 tier in the dispatch chain — this function is simply unreachable.
- **Status:** fixed-inline (commit `2a09db5`)
- **Suggested fix:** delete the function (meets fix-now criteria: private/standalone helper, zero callers anywhere in the repo, confirmed via grep).
