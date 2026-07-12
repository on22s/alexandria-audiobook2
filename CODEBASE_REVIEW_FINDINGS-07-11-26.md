# Whole-Codebase Review Findings — 2026-07-11 (fresh run)

Executed per `CODEBASE_REVIEW_PLAN.md`: 17 whole-file review units (every file read
top-to-bottom), followed by a 6-agent verification pass. 157 raw findings → 2 refuted,
3 reclassified as scope/liveness notes, 3 duplicates merged → **150 verified findings**.

- **Verdicts:** 110 CONFIRMED (failure constructible directly from the code), 40 PLAUSIBLE
  (real code path, needs a realistic-but-unproven state). `V` column: `✓` = CONFIRMED,
  `~` = PLAUSIBLE.
- **Counts:** bugs 70 (4 high / 21 med / 45 low) · missing 58 (1 high / 21 med / 36 low)
  · dead 22 (3 med / 19 low).
- **⚠︎FB tag:** finding is in a file carrying uncommitted checkpoint-resume work on
  `feature/night-super-night-themes` (`app.py`, `generate_script.py`, `find_nicknames.py`,
  `lmstudio_settings.py`, `index.html`, `test_checkpoint_resume.py`, `utils.py`) — 60 of 150.
  Not necessarily *caused* by that work, just located in those files; don't mistake them
  for shipped bugs without checking `main`.
- Full per-finding detail (evidence, failure scenario, suggested fix, verifier notes):
  `logs/review_checkpoints/fresh_VERIFIED.json`. Per-unit sources: `unit*-findings.json`;
  verifier outputs: `verify_*.json`.

## Top 10 risks

1. **`app/app.py:4132` (high, bug, ⚠︎FB)** — `/api/scripts/load` never clears the active
   review checkpoint, and neither detect nor `review_script.load_checkpoint` validates book
   identity. Resume after loading a different book silently splices Book A's corrected
   entries into Book B.
2. **`app/lmstudio_settings.py:158` (high, bug, ⚠︎FB)** — `decide_healed_urls` adopts `None`
   as base_url after every *successful* local (and literal-alias remote) heal. Local runs
   are silently redirected to the Ollama default `:11434`; non-Thunder remote runs crash
   with ValueError right after a successful reload. Both call sites (review, nicknames)
   inherit it.
3. **`app/static/index.html:5659` (high, bug, ⚠︎FB)** — one transient error on the
   dataset-builder status GET makes `dsbLoadProject`'s catch add-and-save a single empty
   row, and the server rebuilds `samples` from it: the project's saved rows are destroyed
   on disk.
4. **`llm_enricher.py:59` (high, bug)** — the deliberate "every chunk failed → exit(1)"
   guard is unreachable because `enrich_transcript_chunk` swallows all exceptions. An
   LLM-down enrichment run returns rc=0 and the preparer records a fully-degraded dataset
   build as success.
5. **`app/test_api.py:1206` (high, missing)** — the global GPU-lock / `claim_gpu_task`
   contract (the subsystem that previously shipped a deadlock) has zero tests; a
   reintroduced double-claim or deadlock passes the suite green.
6. **`app/app.py:2884` + `:3136` (med, bug, ⚠︎FB)** — batch review and batch script-gen
   end-of-run cleanup wipes the plan and *every* per-book checkpoint even when books
   finished failed/VRAM-incomplete — destroying exactly the checkpoints the resume feature
   promises to keep.
7. **`app/generate_script.py:499` (med, missing, ⚠︎FB)** — truncated (`finish_reason ==
   "length"`) LLM responses are only warned about; partial entries get checkpointed as a
   *completed* chunk, so book text silently vanishes and resume never retries it.
8. **`app/app.py:3649` (med, bug, ⚠︎FB)** — `/api/chunks/{i}/generate` runs GPU TTS with no
   GPU-lock check and no process_state tracking; rapid clicks stack concurrent generations
   (the exact OOM the lock exists to prevent). Related: `:3669` merge/export TOCTOU
   double-start race; `:4635` sync GPU inference blocking the event loop in four async
   routes.
9. **`app/app.py:5029` (med, missing, ⚠︎FB)** — LoRA training has no cancel endpoint and no
   process/pid in its state entry: a multi-hour run holding the global GPU lock can only be
   stopped by killing the server.
10. **`app/project.py:700` + `:513` (med)** — a failed/timed-out ffmpeg leaves a partial
    `audiobook.m4b` that the download route serves as valid; and the libmp3lame WAV
    fallback writes a "successful" merge to a filename no route serves (user gets a 404
    after a success message). Honorable mention: `torch.js:76` — Intel-Mac branch missing
    `"next": null` force-reinstalls torch 2.7.0 over the deliberate 2.2.2 pin.

## Deletion candidates (dead code with grep evidence)

| Candidate | Evidence | Caveat |
|---|---|---|
| `.alexandria_audio_24k_*.wav` ×4 (repo root, **6.6 GB**) | No producer/consumer in current code; preparer now uses `dataset_temp/audio_24k.wav` | Data not code — confirm no personal workflow reads them |
| `POST /api/lora/generate_dataset` + `dataset_gen` state + model + test | SPA uses `dataset_builder/*` exclusively; only refs are its own test and one README curl example | Documented public API — deprecate deliberately; also uncancellable (see findings) |
| `GET /api/annotated_script` (`app.py:3192`) | Only ref is `test_api.py:400`; not in README API docs | Or document it instead |
| `old_scripts/dedupe_zips2.py` | Zero programmatic callers (sibling repo included) | **Do not delete standalone** — still the only producer of `_deduped/` that the analyze phase consumes; port its copy step into `run_dedup` first |
| `review_script.py:15` unused imports; `tts_vram_benchmark.py` `ROOT_DIR`/`args` param; `alexandria_alignment.py` `os/sys/json` imports; mfcc computation in `voice_analysis.py`; various dead params/branches | See dead-code table | Mechanical cleanups |
| Stale AI-rule mirrors (`.clinerules`, `.cursorrules`, `.windsurfrules`, `QWEN.md`, `AGENTS.md`) | All identical 42,315-byte pre-Jun-20 CLAUDE.md snapshots | **Refresh, don't delete** — `AGENTS.md` is referenced by the review plan §0 |

## Notes (not defects)

- Unit 3 explicitly confirmed the Rule-15 optimize/heal drift focus was *checked and absent*
  in app.py lines 4200–6300 (it lives in units 1/2 scope and was covered there).
- `name_voices.py` / `alexandria_batch_processor.py` / `alexandria_compare.py` are live
  (app.py subprocess / documented CLI entrypoints) — not deletion candidates.
- Unit 16 found **no stale tests** — everything the suite exercises matches current app.py;
  the problems are unregistered tests, destructive state mutation, and coverage holes.
- Unit 11 re-verified 9 old-run candidates as already fixed by commit 4b0cff5 and dropped
  them; only 4 preparer findings survive (2 introduced by the fix code itself).

## Bugs (70)

| Sev | Location | V | Summary |
|---|---|---|---|
| high | `app/app.py:4132` | ✓ | /api/scripts/load does not clear the active script's review checkpoint, and neither the detect endpoint nor review_script.py's load_checkpoint validates book identity, so resuming after a load splices Book A's corrected entries into Book B. ⚠︎FB |
| high | `app/lmstudio_settings.py:158` | ✓ | decide_healed_urls adopts None as the live base_url whenever an apply succeeds without producing a resolved URL (every local heal and every literal-alias remote heal), so callers overwrite a working base_url with None. ⚠︎FB |
| high | `app/static/index.html:5659` | ✓ | A transient failure of the dataset-builder status GET wipes the project's saved rows: dsbLoadProject's catch resets dsbRows to [] and calls dsbAddRow(), which calls dsbSaveRows(), POSTing a single empty row to /api/dataset_builder/update_rows, which replaces state['samples'] server-side. ⚠︎FB |
| high | `llm_enricher.py:59` | ✓ | The deliberate fail-loud guard for total enrichment failure is unreachable because enrich_transcript_chunk swallows every exception and returns the chunk unchanged. |
| med | `alexandria_batch_processor.py:440` | ✓ | 24h subprocess timeout can never fire while the child is silent — the check lives inside the blocking `for line in process.stdout` loop. |
| med | `alexandria_batch_processor.py:539` | ✓ | When no files are valid, run() prints 'All N files already processed' and exits 0 even if the skips were 'File not found' or 'Unsupported audio format'. |
| med | `alexandria_preparer_rocm_compatible.py:2210` | ✓ | The sub-1.0s chunk logging blocks (2210 and 2227, added by the recent stale-intermediates fix) read drop_chunk/reason_rejected/text that are only assigned inside the chunk_duration >= 1.0 branch, causing NameError on a first sub-1s chunk and stale-value garbage rows otherwise. |
| med | `app/app.py:1924` | ✓ | _deep_merge skips None overrides, so save_config can never UNSET a previously-saved value — clearing the batch seed or the remote SSH alias in Setup silently keeps the old value. ⚠︎FB |
| med | `app/app.py:2884` | ✓ | Batch review end-of-run cleanup deletes the resume plan and every per-book review checkpoint even when books finished 'incomplete' (VRAM-abort) or 'failed', destroying exactly the checkpoints the code promises to keep for a future resume. ⚠︎FB |
| med | `app/app.py:3136` | ✓ | Batch script generation has the same end-of-run over-clear: failed files' per-file script checkpoints are deleted when the batch finishes, defeating generate_script.py's deliberate keep-checkpoint-on-failure design. ⚠︎FB |
| med | `app/app.py:3649` | ✓ | /api/chunks/{index}/generate runs GPU TTS as a background task with no GPU-lock check and no process_state tracking at all. ⚠︎FB |
| med | `app/app.py:3669` | ✓ | merge_audio / export_audacity / merge_m4b endpoints have a TOCTOU double-start race: the running check happens on the request thread but running=True is only set later inside the background task. ⚠︎FB |
| med | `app/app.py:4635` | ✓ | Synchronous GPU generation runs inline in async def handlers, blocking the FastAPI event loop for the whole inference. ⚠︎FB |
| med | `app/lmstudio_settings.py:165` | ✓ | resolve_client_base_url's docstring claims generate_script.py uses it, but generate_script.py hand-rolls its own base_url fallback, re-creating the exact silent-local-fallback the helper exists to prevent (Rule 15 drift). ⚠︎FB |
| med | `app/project.py:513` | ✓ | merge_audio's libmp3lame WAV fallback writes cloned_audiobook.wav and returns success, but the download route only serves cloned_audiobook.mp3, so the 'successful' merge is undownloadable. |
| med | `app/project.py:889` | ~ | generate_chunks_parallel's cancel-reset does an unlocked load->mutate->save while shutdown(wait=False) workers may still be finishing, so a chunk that completes after the snapshot read gets its fresh 'done'+audio_path clobbered back to the stale 'generating' state and reset to 'pending'. |
| med | `app/review_script.py:319` | ~ | Resume space-detection can misfire on a clean resume: load-time de_generic_speakers renames labels in the on-disk output, breaking the prefix==all_corrected test and wrongly switching to input-space offsets against an output-space file. |
| med | `app/static/index.html:496` | ✓ | #sys-eta can never actually be hidden: Bootstrap's .d-flex (display:flex !important) overrides both the inline style="display: none;" and every JS wrap.style.display='none' in updateEtaStatus (line 6144/6159). ⚠︎FB |
| med | `app/static/index.html:4730` | ✓ | Audacity and M4B export pollers kill themselves on the first transient poll error (clearInterval in catch), a straggler of the 'keep pollers alive' fix in 4a70dac. ⚠︎FB |
| med | `app/static/index.html:5761` | ✓ | dsbBuildRowHtml renders row.seed unescaped inside a double-quoted value attribute; seed round-trips as an arbitrary string via dsbImport JSON and server state, allowing attribute breakout; adjacent fields use partial hand-rolled escaping. ⚠︎FB |
| med | `app/static/index.html:5839` | ~ | XSS straggler: dsbUpdateRefDropdown interpolates user-typed emotion and text into innerHTML with no escaping. ⚠︎FB |
| med | `app/test_api.py:1072` | ✓ | Full-mode generation tests start background GPU tasks and never wait or cancel, so under the global GPU lock the subsequent tests self-skip via 400 — the '--full' suite silently tests almost nothing after test_generate_script succeeds. |
| med | `app/test_checkpoint_resume.py:136` | ✓ | test_resume_offset_skips_completed_chunks re-implements the resume loop's arithmetic inside the test instead of exercising generate_script's real chunk loop — it cannot fail when the real loop regresses. (On unmerged feature work.) ⚠︎FB |
| med | `torch.js:76` | ✓ | Intel-mac branch is missing "next": null, so after installing the pinned torch 2.2.2 it falls through to the unconditional CPU step which force-reinstalls torch 2.7.0. |
| med | `voice_analysis.py:557` | ✓ | write_pipeline_summary reads the analyze cache's top-level structural keys as group names, so no narrator can ever be classified DONE. |
| low | `alexandria_alignment.py:304` | ✓ | _parse_number sums year-style spelled numbers additively, so 'nineteen eighty' parses as 99 — enabling a false number-equivalence match against a source token '99' and never matching '1980'. |
| low | `alexandria_batch_processor.py:185` | ✓ | _find_source_for docstring says fuzzy default 0.55; the signature default is 0.50. |
| low | `alexandria_compare.py:781` | ✓ | 'skip' actions are appended to the review log, so a skipped-then-redecided entry produces duplicate entry_idx records (skip + final decision) in the JSONL log. |
| low | `alexandria_preparer_rocm_compatible.py:2309` | ✓ | Batch-mode ETA appends one chunk_times sample per batch flush (spanning the whole batch's LLM call, measured from the last chunk's chunk_t0) but multiplies the rolling average by remaining CHUNKS, inflating displayed ETA by roughly batch_size. |
| low | `alexandria_preparer_rocm_compatible.py:3462` | ✓ | The KeyboardInterrupt handler does not clear _delete_scratch_on_exit, so an interrupted annotate phase deletes the 24k scratch WAV in the finally block despite logging that partial results are preserved for --resume. |
| low | `app/app.py:765` | ~ | run_process's signal-name lookup signal.Signals(-return_code) raises ValueError for a non-standard negative exit code, mislabelling a cancelled task as 'failed'. ⚠︎FB |
| low | `app/app.py:1663` | ~ | lmstudio_optimize crashes with AttributeError (HTTP 500) if config.json contains an explicit "llm_remote": null. ⚠︎FB |
| low | `app/app.py:2446` | ✓ | The single-review detect endpoint reports 'done' as completed_batches without applying the failed-batch rewind, so the resume prompt can overstate progress relative to where the worker will actually resume. ⚠︎FB |
| low | `app/app.py:3793` | ~ | generate_batch / generate_batch_fast crash with an uncaught TypeError (500) if config.json's tts.parallel_workers is a non-numeric value, because the surrounding try only catches JSONDecodeError/ValueError. ⚠︎FB |
| low | `app/app.py:4617` | ~ | _prune_dir_to_recent's mtime sort is outside its try, so a concurrently-deleted file raises OSError despite the 'best-effort (ignores errors)' contract. ⚠︎FB |
| low | `app/app.py:5522` | ~ | Dataset-builder state.json read-modify-write races: batch task keeps re-saving a stale snapshot over concurrent row/meta edits. ⚠︎FB |
| low | `app/app.py:5986` | ~ | preparer_batch_cancel only sets the flag; termination relies on _stream_subprocess_to_logs' queue.Empty branch, which a continuously-chatty subprocess starves. ⚠︎FB |
| low | `app/find_nicknames.py:185` | ✓ | _parse_alias_response resolves alias keys to the real speaker-label casing but returns evidence keyed by the model's raw casing, so evidence.get(variant) at l.420 misses whenever the model changed case and the printed '(why)' evidence is silently dropped. ⚠︎FB |
| low | `app/find_nicknames.py:338` | ~ | The _ContextTooSmall retry re-chunks the prompt to real_n_ctx but still requests max_tokens=2000 completions, so when real_n_ctx <= ~max_tokens+prompt floor (e.g. 2048) every retried chunk overflows again and the retry can never succeed. ⚠︎FB |
| low | `app/generate_script.py:96` | ✓ | save_script_checkpoint's comment claims file_lock protects concurrent reads from torn appends, but the read side (_read_checkpoint_entries) never takes the lock. ⚠︎FB |
| low | `app/generate_script.py:342` | ✓ | salvage_json_entries unescapes only \" and \n, leaving \\, \t, \r, \uXXXX as literal escape sequences in salvaged text. ⚠︎FB |
| low | `app/hf_utils.py:32` | ✓ | fetch_builtin_manifest's module-level cache ignores its builtin_dir/hf_repo arguments, so a call with a different repo or dir within the TTL returns the wrong cached manifest. |
| low | `app/llm_bench.py:63` | ~ | _one_call assumes resp.usage is non-None; OpenAI-compatible servers that omit usage make every benchmark call raise AttributeError, which measure_throughput swallows into None, so the sweep silently settles on concurrency=1 with no hint why. |
| low | `app/project.py:600` | ✓ | export_audacity keys tracks by raw speaker but writes zip entries/LOF lines by sanitize_filename(speaker); two distinct speakers that sanitize to the same name produce duplicate zip entry names and duplicate LOF lines, so one speaker's track is lost on extract. |
| low | `app/project.py:724` | ~ | _escape_ffmeta replaces \n but not \r, so a CR (e.g. from CRLF text pasted into the description/title fields) survives into the FFMETADATA file and can corrupt the key=value line. |
| low | `app/review_script.py:219` | ✓ | Checkpoint rewind only rewinds the batches_failed stat; diff stats from rewound successful batches stay in total_stats and are double-counted when those entries are re-reviewed. |
| low | `app/review_script.py:607` | ✓ | merge_consecutive_narrators indexes entry['text'] directly while its guards use .get — a NARRATOR entry missing 'text' raises KeyError after the entire (expensive) review finished, before the output write. |
| low | `app/review_script.py:1053` | ~ | No sanity clamp on wave_size before `range(0, len(remaining_batches), wave_size)`: a corrupted cached concurrency in config.json (negative or non-int) crashes the run or — for a negative int on a fresh run — silently writes an EMPTY script over the book. |
| low | `app/static/index.html:463` | ✓ | Unbraced single-line if in the anti-flash theme snippet — the only Rule 18 violation in lines 1-3350. ⚠︎FB |
| low | `app/static/index.html:2731` | ✓ | Batch script-gen resume still demands a fresh file selection and index-maps saved-plan statuses onto whatever files the user re-picked. ⚠︎FB |
| low | `app/static/index.html:2811` | ~ | ${t.status} interpolated into badge innerHTML unescaped in _pollScriptBatchLogs (2811) and pollReviewBatch (3213). ⚠︎FB |
| low | `app/static/index.html:4644` | ✓ | _runBatchRender: cancelling a batch render leaves the blue 'table-info' row highlights in place — only cleared on the stillGenerating===0 completion branch. ⚠︎FB |
| low | `app/test_api.py:753` | ✓ | test_batch_preparer_start_schema can actually START a real batch-preparer background job in quick mode (200 is an accepted outcome), relying on the following cancel test to stop it; the job runs briefly with a nonexistent test.wav. |
| low | `app/test_api.py:1095` | ✓ | test_generate_chunk asserts only HTTP 200 from /api/chunks/0/generate, which unconditionally returns {'status':'started'} after scheduling a background task — a Rule 12 placeholder assertion that also leaks a running TTS job into the next test. |
| low | `app/test_api.py:1113` | ✓ | wait_for_task('audio') races task startup: /api/generate_batch sets running=True only inside the BackgroundTasks body, so the first poll can see running=False and declare completion before generation begins. |
| low | `app/test_api.py:1183` | ✓ | test_dataset_builder_generate_sample asserts only that a 'status' key exists in the response — not its value, nor that a sample wav/state entry was produced (Rule 12). |
| low | `app/train_lora.py:418` | ✓ | target_loss is tested by truthiness in two places but by 'is not None' in the two that matter — a falsy-zero --target_loss 0.0 enables safe-checkpoint mode while the summary/zone prints claim no target is set. |
| low | `app/train_lora.py:566` | ~ | OOM handler calls torch.cuda.empty_cache() without first deleting the partially built tensors still referenced by locals, so the recovery frees less VRAM than intended and the next sample likely OOMs again. |
| low | `app/train_lora.py:680` | ~ | If every epoch is fully OOM-skipped, final_loss is written to training_meta.json as bare NaN, which then flows into the LoRA models manifest and can 500 the /api/lora/models JSON response. |
| low | `app/tts.py:100` | ~ | compute_timeline indexes chunk['speaker'] directly (KeyError on a chunk missing 'speaker'), unlike every generation path which uses .get('speaker') |
| low | `app/tts.py:897` | ~ | Bare 'from hf_utils import download_builtin_adapter' lacks the relative-import fallback the module header uses, breaking builtin auto-download if tts is imported as a package |
| low | `app/tts_vram_benchmark.py:108` | ✓ | run_sweep calls torch.cuda.reset_peak_memory_stats()/max_memory_allocated() unguarded, crashing the benchmark on CPU-only machines |
| low | `app/utils.py:168` | ~ | file_lock stale-reaping keys off the lock file's creation mtime with no holder-side refresh, so a live holder whose critical section exceeds stale_after=30s gets its lock reaped and two processes proceed concurrently. ⚠︎FB |
| low | `install.js:14` | ~ | Explicit `python -m venv env` step is redundant with the venv attribute and pins the env to whatever `python` resolves to, risking mismatch with torch.js's cp310-only pinned wheels. |
| low | `install.js:34` | ~ | `uv pip uninstall google-genai` runs BEFORE `uv pip install -r requirements.txt` in the same step, so a transitively reinstalled google-genai survives; on a fresh env it is a no-op. |
| low | `llm_enricher.py:56` | ✓ | enrich_transcript_chunk mutates its input chunk in place (chunk.update) and also returns it — a dual-purpose parameter. |
| low | `name_voices.py:112` | ~ | _is_named treats any entry lacking dataset_id as unnamed, so an already-slugged adapter whose manifest entry has no dataset_id gets renamed again without --overwrite. |
| low | `name_voices.py:206` | ~ | untouched_ids builds via dict-equality membership (`e not in candidates`) and unguarded e['id'], so duplicate-by-value entries corrupt the reserved set and an id-less entry crashes with KeyError. |
| low | `voice_analysis.py:252` | ~ | Dedup clustering is single-seed, non-transitive: a volume similar to a cluster member but not to the seed lands in the wrong cluster. |
| low | `voice_analysis.py:330` | ~ | zip_groups assignment clobbers on normalized-key collision, silently dropping a zip from the analyze phase. |

## Missing code (58)

| Sev | Location | V | Summary |
|---|---|---|---|
| high | `app/test_api.py:1206` | ✓ | Coverage hole: the global GPU lock / claim_gpu_task contract has no test at all — nothing asserts that starting task B while task A runs returns 400, nor that a claim is released when a start fails after claiming. |
| med | `alexandria_compare.py:296` | ✓ | save_checkpoint writes the session checkpoint non-atomically and load_checkpoint has no corrupt-file handling, so a crash mid-write bricks resume for a long review session. |
| med | `app/app.py:2671` | ~ | The shared batch-review alias registry scripts/.series_aliases.json is never cleared or reset anywhere, so a fresh batch review of a different series inherits the previous series' aliases and can silently merge/rename its characters. ⚠︎FB |
| med | `app/app.py:5029` | ✓ | LoRA training cannot be cancelled: no cancel endpoint exists and the task's state entry lacks cancel/process/pid keys. ⚠︎FB |
| med | `app/app.py:5103` | ~ | LORA_MODELS_MANIFEST is read-modify-written by 3+ independent writers with no file_lock. ⚠︎FB |
| med | `app/default_prompts.py:45` | ✓ | Import-time load_default_prompts() is unguarded, so a missing/malformed default_prompts.txt prevents app.py from starting at all — asymmetric with review_prompts.py, which guards for exactly this reason. |
| med | `app/generate_personas.py:670` | ✓ | persona_refs/ is never cleared or namespaced per book: _append_character_ref loads any existing {speaker}.json and appends, so refs from a previously-loaded book leak into the new book's personas when character names collide (e.g. NARRATOR, JOHN). |
| med | `app/generate_personas.py:696` | ✓ | Advanced persona path ignores _save_generated_preview's return value and keeps no failure count, so an --advanced run where every preview/LLM call failed still exits 0. |
| med | `app/generate_personas.py:742` | ✓ | generate_personas.py builds its LLM client straight from config['llm'].base_url with no ensure_ideal_settings/resolve_client_base_url/self-heal, unlike find_nicknames.py (l.383-393) and review_script.py, which both heal the URL before use. |
| med | `app/generate_script.py:499` | ✓ | Script generation has no text-loss/truncation safeguard: a finish_reason=='length' response only prints a warning, then partial salvaged entries are accepted and checkpointed as a completed chunk. ⚠︎FB |
| med | `app/project.py:700` | ✓ | merge_m4b does not delete the partial audiobook.m4b on ffmpeg failure or timeout; the finally block only removes temp_wav/meta_path, so GET /api/audiobook_m4b then serves a truncated/partial file as if valid. |
| med | `app/test_api.py:385` | ✓ | test_upload_file permanently mutates real user state (state.json input_file_path -> the test book) and cleanup() never reverts it; in --full mode test_generate_script then overwrites the user's active annotated_script.json with output from the 3-line test book. |
| med | `app/test_api.py:696` | ✓ | test_status_known_tasks omits six live process_state tasks — batch_review, batch_script, nicknames, voicelab, m4b_export, voices — so the status contract for the newest/riskiest subsystems is untested. |
| med | `app/test_api.py:1047` | ✓ | Coverage hole: export/assembly POST paths are essentially untested — /api/merge, /api/merge_m4b, /api/m4b_cover (POST and DELETE) never appear, and export_audacity is only asserted to return 'started'. |
| med | `app/test_api.py:1206` | ✓ | Coverage hole: batch review and batch script generation endpoints (/api/review_script/batch/*, /api/generate_script/batch/*) — including the batch-review clear/restore logic — have zero API tests. |
| med | `app/test_api.py:1346` | ✓ | Coverage hole: voice library casts (save/match/match_bulk/apply/apply_bulk, casts CRUD) and nicknames (/api/find_nicknames*, /api/character_aliases) have no tests; the lone voice_library GET smoke never runs (see the unregistered-test finding). |
| med | `app/train_lora.py:43` | ✓ | No lower-bound validation on gradient_accumulation_steps (or lora_r) anywhere in the chain: 0 causes ZeroDivisionError only after the full model is loaded and the dataset tokenized. |
| med | `app/tts.py:1380` | ✓ | Batch clone/LoRA paths ignore both batch_seed and the per-voice 'seed', so batch output is never reproducible and differs from single-chunk regeneration |
| med | `app/tts.py:1544` | ✓ | _local_batch_lora does not auto-download builtin_ adapters, unlike the single-chunk path |
| med | `pinokio.js:117` | ✓ | The two 'Start LLM' menu entries reference .gguf model files that are gitignored and never downloaded by any launcher script; one of the two does not exist even on this machine. |
| med | `start_llm.js:13` | ✓ | start_llm.js hard-depends on the sibling repo's venv (../alexandria-audiobook.git/app/env) for llama_cpp, but nothing in this launcher installs or verifies that dependency. |
| med | `voice_analysis.py:497` | ✓ | run_analyze has no guard for zero successfully-extracted groups and crashes deep in plotting/UMAP instead of exiting cleanly. |
| low | `alexandria_batch_processor.py:521` | ✓ | Generic-exception handler kills the child without wait(), unlike the Timeout/KeyboardInterrupt handlers. |
| low | `alexandria_compare.py:374` | ✓ | --reset-from N with N >= total entries yields an empty reset set and the script silently falls through into a normal review session instead of resetting or erroring. |
| low | `alexandria_compare.py:832` | ✓ | Review-log line count opens the file without closing it. |
| low | `alexandria_preparer_rocm_compatible.py:3451` | ✓ | --skip-annotation remains a half-built feature: it now fails fast at validation (403-408), leaving the annotate-phase 'not yet implemented' branch (3450-3452) unreachable, and app/app.py still declares a skip_annotation request field it never forwards to the CLI. |
| low | `app/app.py:1792` | ~ | _fill_missing_prompt_defaults wraps the review and persona prompt loaders in try/except RuntimeError but leaves load_default_prompts() unwrapped, so /api/config 500s if default_prompts.txt is deleted/malformed after startup. ⚠︎FB |
| low | `app/app.py:1872` | ~ | get_config and upload_file read state.json with a raw open() guarded only against JSON errors — an OSError escapes as a 500. ⚠︎FB |
| low | `app/app.py:2183` | ✓ | upload_file's EPUB branch writes the extracted text with a blocking sync open() inside the async route, and a write failure there leaks both the claimed .txt placeholder and the source .epub. ⚠︎FB |
| low | `app/app.py:3197` | ~ | /api/annotated_script parses SCRIPT_PATH with a raw json.load, so a corrupt/partially-written file returns an opaque 500 instead of the clean handling every neighboring reader has. ⚠︎FB |
| low | `app/app.py:3769` | ✓ | upload_m4b_cover reads the entire upload into memory with no size cap and stores any image bytes as m4b_cover.jpg regardless of actual format. ⚠︎FB |
| low | `app/app.py:4138` | ✓ | load_script's busy-guard blocks audio/script/review but not 'persona' (or 'nicknames'), and a persona run finishing after the load overwrites the freshly-loaded book's voice_config.json. ⚠︎FB |
| low | `app/app.py:4673` | ~ | Designed-voices and clone-voices manifests use unlocked load/append/save read-modify-write. ⚠︎FB |
| low | `app/app.py:4739` | ✓ | clone_voices_upload and lora_upload_dataset read the entire upload body into memory with no size cap. ⚠︎FB |
| low | `app/app.py:4933` | ✓ | lora_generate_dataset background loop has no cancel support (no cancel key, no endpoint, no loop check). ⚠︎FB |
| low | `app/app.py:4971` | ~ | lora_generate_dataset writes an empty/broken dataset when zero samples succeed instead of failing loud. ⚠︎FB |
| low | `app/app.py:5276` | ~ | lora_preview permanently serves a partial preview_sample.wav if generation dies mid-write. ⚠︎FB |
| low | `app/app.py:5782` | ✓ | preparer_start writes the uploaded audio straight to UPLOADS_DIR/<name>, silently overwriting an existing upload; uploads are never cleaned up. ⚠︎FB |
| low | `app/find_nicknames.py:426` | ✓ | No checkpoint/incremental persistence: alias results are only written once at the end of main(), so a crash/cancel/power-loss mid-run discards every already-completed evidence chunk (each a full LLM call). ⚠︎FB |
| low | `app/generate_personas.py:923` | ~ | Basic persona path has no final ref_text fallback: a speaker whose sample entries are all empty strings (main l.731 appends text even when empty) reaches generate_voice_design with sample_text="", unlike _compile_persona which guards with a synthetic sentence (l.659-660). |
| low | `app/hf_utils.py:42` | ~ | fetch_builtin_manifest never validates that the fetched/loaded manifest.json is a list of dicts before caching and returning it. |
| low | `app/lmstudio_settings.py:78` | ✓ | THUNDER_LMS_PORT is int()-parsed from the environment at import time with no validation, so a malformed env value crashes every importer at startup with a bare ValueError. ⚠︎FB |
| low | `app/persona_prompts.py:31` | ✓ | load_persona_prompts() runs at import time and raises RuntimeError on a missing/malformed persona_prompts.txt; app.py imports this module at startup (l.35), so a bad txt file prevents the entire FastAPI server from starting, not just persona features. |
| low | `app/project.py:596` | ✓ | export_audacity writes audacity_export.zip directly at its final path; an exception mid-write leaves a truncated zip that GET /api/export_audacity happily serves. |
| low | `app/project.py:1140` | ~ | When engine.generate_batch raises (line 1141) or reports a chunk failed, any temp_batch_{idx}.wav files the engine already wrote for that batch are never removed — only _finalize_completed_chunk cleans temps, and only for completed indices. |
| low | `app/review_prompts.py:33` | ✓ | The None-fallback comment promises review_script 'can re-load via load_review_prompts() to surface a clear error', but review_script never calls it — a missing review_prompts.txt instead crashes mid-run with AttributeError on None.format. |
| low | `app/review_script.py:813` | ✓ | Half-built 'mode 2': --source reads the entire source text into memory but it is never used anywhere afterward. |
| low | `app/static/index.html:2724` | ✓ | cancelBatchScript (2724) and cancelBatchReview (2909) swallow all errors with catch { /* ignore */ }, while cancelScript/cancelReview toast the failure. ⚠︎FB |
| low | `app/static/index.html:6230` | ✓ | reattachRunningPollers only re-attaches script/review/nicknames/voicelab pollers after a page reload; lora_training, preparer, batch_preparer, persona, and audio (merge) runs lose their poller and button state on refresh. ⚠︎FB |
| low | `app/test_api.py:1404` | ✓ | cleanup() edits voice_config.json directly with a hand-rolled tmp+os.replace instead of utils.atomic_json_write/file_lock, and can race the live server writing the same file; it exists only because the app has no delete-voice API. |
| low | `app/train_lora.py:296` | ~ | Teacher-forcing input hardcodes the chat-template token counts (role prefix = exactly 3 tokens, suffix = exactly 5) with no validation, so a tokenizer/template change silently trains on truncated or shifted text. |
| low | `app/tts.py:274` | ~ | Setting sub_batch_enabled=false silently bypasses the VRAM-derived max_items cap, not just the length-ratio splitting |
| low | `app/tts.py:819` | ✓ | generate_voice_design writes a preview WAV per call into designed_voices/previews/, but the previews-dir size cap only runs in app.py's designer-preview route — chunk/batch design-voice generation leaks one WAV per chunk |
| low | `app/utils.py:104` | ~ | atomic_json_write fsyncs the temp file but never fsyncs the containing directory after os.replace, so the rename itself is not durable across power loss — the very scenario the in-code comment says checkpoint-resume must survive. ⚠︎FB |
| low | `name_voices.py:232` | ✓ | A crash between os.rename of an adapter dir and the final manifest json.dump leaves the manifest stale relative to disk (the write happens once, after the whole rename loop). |
| low | `reset.js:8` | ~ | reset.js deletes only part of the generated state (7 items) and never removes app/env, so 'Reset' can neither recover a corrupted install nor fully reset app data. |
| low | `voice_analysis.py:45` | ✓ | DEFAULT_ZIPS2 hardcodes a machine-specific absolute path as the CLI default. |
| low | `voice_analysis.py:202` | ✓ | Per-WAV `except Exception: pass` in both phases swallows all extraction errors with no count or sample of what was skipped. |

## Dead code (22)

| Sev | Location | V | Summary |
|---|---|---|---|
| med | `.alexandria_audio_24k_14674.wav:1` | ✓ | Four hidden scratch WAVs at repo root (.alexandria_audio_24k_{14674,216420,2729625,371702}.wav, dated May 23-24) total ~7 GB and have NO producer, consumer, or cleanup path in the current code. |
| med | `app/app.py:4884` | ✓ | POST /api/lora/generate_dataset (the whole 'dataset_gen' background task, plus LoraGenerateDatasetRequest at L446 and the process_state entry at L534) is never called by the SPA — the Dataset tab uses the newer /api/dataset_builder/* endpoints exclusively. (Independently flagged by units 3, 10b, and 15.) ⚠︎FB |
| med | `app/test_api.py:1330` | ✓ | test_checkpoint_detect_endpoints_shape and test_readonly_subsystem_smoke (line 1346) are defined but never registered in run_all_tests(), so they never execute. |
| low | `.clinerules:1` | ✓ | Stale AI-assistant rule mirrors: .clinerules, .cursorrules, .windsurfrules (May 15), QWEN.md and AGENTS.md (May 23) are all identical 42,315-byte snapshots of an OLD CLAUDE.md; the live CLAUDE.md was rewritten Jun 20 (17,960 bytes) and GEMINI.md Jun 19, so the five mirrors now give other AI tools month-old, contradictory instructions. |
| low | `alexandria_alignment.py:31` | ✓ | Unused imports: os, sys, json. |
| low | `alexandria_batch_processor.py:56` | ✓ | get_gpu_stats computes allocated/reserved/total/allocated_percent but log_gpu_stats (the only caller) never logs them. |
| low | `app/app.py:3192` | ✓ | GET /api/annotated_script is never called by the SPA and is not documented in README's API section; only app/test_api.py:400 references it. ⚠︎FB |
| low | `app/generate_personas.py:706` | ✓ | CLI flags --alias-check, --new-only, --speakers, --narration-window have zero non-CLI callers: app.py's /api/generate_personas builds the command with only --advanced/--batch-size, so the whole Step-1/Step-2 alias-resolution feature (l.801-875) is unreachable from the app. |
| low | `app/generate_script.py:213` | ✓ | In clean_json_string's bracket scanner, `and not escape_next` on the quote check is always true (dead condition). ⚠︎FB |
| low | `app/hf_utils.py:83` | ✓ | The adapter_id -> HF-subfolder mapping (`adapter_id.replace("builtin_", "", 1)`) is duplicated verbatim in app.py instead of living in one helper, and .replace strips the first occurrence anywhere, not just a prefix. |
| low | `app/lmstudio_settings.py:657` | ✓ | Unreachable exception classes in apply_lmstudio_settings' except tuples: subprocess.CalledProcessError is never raised by subprocess.run without check=True, and FileNotFoundError is a subclass of the already-listed OSError. ⚠︎FB |
| low | `app/review_script.py:15` | ✓ | clean_json_string, repair_json_array, salvage_json_entries are imported but never used in review_script.py. |
| low | `app/static/index.html:5388` | ✓ | pollLoraTraining(totalEpochs) never uses its parameter — epoch totals are parsed from the [EPOCH]/[TRAIN] log lines instead. ⚠︎FB |
| low | `app/static/index.html:5482` | ✓ | loadLoraModels renders m.dataset_id \|\| (m.builtin ? '--' : '--') — both ternary arms identical, builtin check dead. ⚠︎FB |
| low | `app/static/index.html:5933` | ✓ | dsbPollStatus's silent parameter is never passed as true, so all three silent branches are unreachable. ⚠︎FB |
| low | `app/test_checkpoint_resume.py:201` | ✓ | test_probe_gate_allows_configured_remote(monkeypatch=None) declares a monkeypatch parameter it never uses (patching is done by manual attribute swap). (On unmerged feature work.) ⚠︎FB |
| low | `app/train_lora.py:40` | ✓ | --batch_size is parsed, forwarded from the UI/app, printed in the 'effective batch' summary, and stored in training_meta.json, but never used — training always processes exactly 1 sample per step. |
| low | `app/tts_vram_benchmark.py:24` | ✓ | ROOT_DIR module constant and print_summary's 'args' parameter are unused |
| low | `llm_enricher.py:39` | ✓ | The `if not self.llm` guard in enrich_transcript_chunk is unreachable. |
| low | `old_scripts/dedupe_zips2.py:1` | ✓ | Only file left in old_scripts/; zero programmatic callers, but it is still the ONLY producer of the zips2/_deduped folder that voice_analysis.py --phase analyze consumes, and voice_analysis.py:325 names it in a user-facing warning. |
| low | `tododo.txt:1` | ✓ | Cluster of untracked one-off personal artifacts at repo root with zero code references: tododo.txt, Screenshot_2026-07-05_14-18-28.png, pr_body.txt, pr_body2.txt, commit_msg.txt, batch_results_20260521_074227.json, CODE_REVIEW.md, code-review-20.skill (a zip of the .claude/skills packaging). |
| low | `voice_analysis.py:116` | ✓ | mfcc_mean/mfcc_std are computed per sample and pickled into the analyze cache but never consumed anywhere. |

## Refuted during verification (dropped)

- `app/app.py:849` — _stream_subprocess_to_logs opens the on-disk log file BEFORE subprocess.Popen — if Popen raises, the file handle is never closed. **Refuted:** The stated impact (fd leak accumulating per retry) cannot occur on CPython: when Popen raises, the exception propagates to run_process's `except Exception` (app.py:772-778); when that except block exits, the exception/traceback is cleared, the _stream_subprocess_to_logs frame holding log_fh is freed, its refcount hits zero, and the file object's deallocator closes the fd immediately. Remaining iss
- `pinokio.json:5` — "plugin": { "menu": [] } is not a documented pinokio.json field for an app launcher and is an empty no-op. **Refuted:** The identical structure ships in a canonical Pinokio example: /home/fakemitch/pinokio/prototype/system/examples/serverless_web_app/pinokio.json contains exactly `"plugin": { "menu": [] }` (lines 5-7). Both this repo's CLAUDE.md and the parent CLAUDE.md mandate mirroring the examples folder as the source of truth ('ALWAYS try to follow the best practices in the examples folder'), so this key is a s
