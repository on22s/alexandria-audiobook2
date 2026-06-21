# Two-Day Bug Retrospective (2026-06-19 → 2026-06-21)

> **Purpose:** A point-in-time record of every confirmed bug found across three review efforts, so the count/severity picture survives the next context compaction. This is a retrospective, not an implementation plan — everything listed here is already fixed (or, where marked, deferred).

**Window covered:**
1. **2026-06-19** — CLAUDE.md rule-compliance audit (systematic sweep, 54 pieces of the codebase)
2. **2026-06-20** — Code-review round 1, `/code-review-20` vs `main`
3. **2026-06-21 (morning)** — Code-review round 2, `/code-review-20` vs round 1's own commits (catching round-1-introduced regressions)
4. **2026-06-21 (this session)** — A 24-hour-scoped review, a broader dead-code/bug sweep, and full-file deep reads of `app/app.py`, `voice_analysis.py`, `app/train_lora.py`, `app/utils.py`, `app/test_api.py`, and several smaller scripts

Efforts 1-3 are documented in their own plan docs in this directory and were fully committed. **Everything in effort 4 is uncommitted** — the user's standing instruction this session was "stop committing as you go" — so none of it appears in `git log`.

---

## Totals by effort

| Effort | Confirmed findings | Confidence |
|---|---|---|
| 06-19 rule-compliance audit | 120 | Count is solid (synthesis commit `cb59497`: "54/54 pieces, 120 findings"). Per-finding detail is **not recoverable** — FINDINGS.md/FIXED.md were working files, never committed. Only 3 have a recoverable description (F-092/F-115/F-119, called "high-severity" in commit `5008a85`). |
| 06-20 round 1 | 36 | Solid — full itemized index in `2026-06-20-code-review-findings-remediation.md`. |
| 06-21 round 2 (+ 4 cut) | 24 | Solid — full itemized index in `2026-06-21-remediation-regressions-fix.md`, plus 4 more findings from the same review pass documented only in commit `04e0656`'s message (cut from that skill's 20-output cap). |
| 06-21 this session — 24-hour-scoped review | 5 | **Count only.** This batch's itemized detail was compacted out of context before this doc was written; not reconstructable without the original transcript. |
| 06-21 this session — broader sweep + deep reads | 16 | Solid — I have first-hand detail on all of these (below). |
| **Total** | **~201** | |

---

## Severity breakdown

| Severity | 06-19 | 06-20 | 06-21 round 2 | 06-21 this session | **Total** |
|---|---|---|---|---|---|
| Critical | unknown (likely a handful; only 3 named, and those were called "high" not "critical") | 4 | 1 | 0 | **5 confirmed** + unknown from 06-19 |
| High | unknown (3 named) | 7 | 8 | 4 | **22 confirmed** + unknown from 06-19 |
| Medium | unknown, likely the bulk | 16 | 11 | 3 | **30 confirmed** + unknown from 06-19 |
| Low | unknown | 9 | 4 | 6 (+ 5 uncounted) | **19 confirmed** + unknown from 06-19 |

**On the 06-19 row:** the audit doc assigns no per-finding severity scale, and its working files (FINDINGS.md/FIXED.md) were never committed, so the 120 findings can't be split with real data. The commit-group names that survive ("Group A — log before swallowing exceptions," "Group D — self-heal/canonical-dispatch fixes," "Rule 16 verb-first renames," dead-code removal, doc/citation corrections) suggest the mix skews heavily medium/low — rule-compliance and logging-discipline issues rather than crash- or security-grade bugs — with a small high/critical tail. Treat any precise split for this row as a guess, not data.

---

## Critical findings (5 confirmed, all security/RCE-class, all from 06-20/06-21 round 2)

1. **Voicelab RCE** — `rocm_python`/`pipeline_repo`/`profiler_model` only existence-checked at save time, not provenance-checked (06-20).
2. **Same RCE, actually closed** — round 1's existence check didn't stop an attacker uploading a malicious script via the dataset-upload endpoint and pointing `pipeline_repo` at it; round 2 added a denylist against every upload/generated-content directory (06-21).
3. **`dataset_builder_generate_sample` path traversal** via unsanitized `dataset_name` (06-20).
4. **`dataset_builder_status` path traversal / info disclosure**, same pattern, GET endpoint (06-20).
5. **`train_lora.py` `audio_filepath`/`ref_audio_path` traversal** — manifest-controlled fields unsanitized before file load; round 1 fixed the per-entry loop, round 2 found a pre-loop resolution of the same field that bypassed it (06-20 + 06-21).

## High findings (22 confirmed)

**From 06-20 (7):** `torch` NameError crashing all TTS generation; `dedupe_speakers` tuple-unpack crash on single-narrator books; `atomic_json_write` chmod'ing secrets-bearing files world-readable; a VRAM-abort book silently marked "done"; a TOCTOU GPU-lock window in `lora_preview`/`lora_test_model`; combined fwd/bwd review stats hiding a crashed pass; backward-pass ETA showing the wrong book number.

**From 06-21 round 2 (8):** unbounded `sample_index` → memory-exhaustion DoS; `secure_filename` with no length cap → uncaught `OSError`; `lora_preview`'s adapter download still running before `claim_gpu_task`; `dataset_builder_generate_sample`'s `get_engine()` same TOCTOU class; shared `run_rocm_smi_json` losing its returncode check + all per-failure logging when centralized; `_combine_pass_stats`'s `partial` flag always `True` for non-bidirectional reviews; remote-status cache wrong-keyed/never-invalidated/racy (3-in-1); `progress_callback` undercounting in-flight OOM chunks.

**From this session (4):**
1. **`dataset_builder_generate_batch` GPU-lock deadlock** (`app/app.py`) — an out-of-range `indices` value crashed the background thread with an uncaught `IndexError` before any cleanup ran, leaving `process_state["dataset_builder"]["running"]` stuck `True` forever. Since `dataset_builder` is a `GPU_TASKS` member, this permanently deadlocked every other GPU task (audio gen, review, persona gen, voicelab, LoRA train/test) until server restart. Fixed: validate indices up front (clean 400), plus a defensive `finally` around the whole task body.
2. **Voice Lab "dedup" stage never created `_deduped/`** (`voice_analysis.py`) — it only printed analysis/suggestions. The "train" stage's error message ("run the dedup stage first") was actively misleading; re-running dedup alone could never satisfy it. Fixed by actually copying one representative zip per cluster (largest file) into `_deduped/`.
3. **`train_lora.py` final-save overwrote the protected best/safe checkpoint** — both when `target_loss` early-stop overshoots `GARBLE_FLOOR` (the safety feature's own stated purpose, defeated by the unconditional save right after) and in the general case where the last epoch's loss regressed vs. an earlier best epoch. Fixed: only save at the end if the last epoch run actually is the tracked best/safe checkpoint.
4. **Format-spec crash in `llm_enricher.py`** — `f"{chunk.get('start', 'N/A'):.2f}"` raises `ValueError` whenever `start`/`end` is missing, since `:.2f` can't format the string `'N/A'`. Fixed to use `0.0` numeric defaults, matching the file's own `_create_prompt` convention.

## Medium findings, this session (3 of 30 total; 27 from earlier efforts not re-listed)

- **LoRA `character_style` cross-contamination** (`app/tts.py`'s `_local_batch_lora`) — adapter-grouped batches shared one `voice_data`/`character_style` across all chunks in the group instead of per-chunk, so a batch mixing styles under the same adapter could apply the wrong style.
- **`get_config()` never backfilled new `TTSConfig` fields** into old on-disk `config.json` (`app/app.py`) — explains why `pause_between_speakers_ms`/`pause_same_speaker_ms` (and any other newer TTS field) could be silently missing from `GET /api/config` on a config saved before those fields existed. Fixed by deriving defaults from `TTSConfig().model_dump()` and backfilling.
- **`atomic_json_write`'s Windows retry check used `e.errno` instead of `e.winerror`** (`app/utils.py`) — `ERROR_ACCESS_DENIED`(5)/`ERROR_SHARING_VIOLATION`(32) are raw Windows codes that only ever appear on `.winerror`; `.errno` holds the CRT-translated POSIX equivalent (`EACCES`=13). The check was a silent no-op, currently masked by an `or`'d string-matching fallback.

## Low findings, this session (6 confirmed + 5 from the uncounted 24-hour review)

- Dead heading-detection branch in `project.py`'s `_build_m4b_chapters` (provably unreachable: a `.search()` re-check of the same `^`-anchored pattern a `.match()` had already failed on the same string).
- `_safe_extractall` realpath-per-ZIP-member efficiency regression — self-introduced earlier this session during a refactor, self-caught before reporting it.
- `alexandria_preparer_rocm_compatible.py`'s scratch-audio cleanup ran after the `enrich` phase too (not just `asr`), forcing a wasted re-decode/resample on the next phase whenever `--enrich-with-llm` is used. Not a crash — a recreate-if-missing fallback already existed — but contradicted the code's own comment.
- 4x `test_api.py` tests (`save_config_roundtrip`, `save_pause_config_roundtrip`, `save_review_prompts_roundtrip`, `save_persona_prompts_roundtrip`) omitted `llm_local`/`llm_mode` from their `POST /api/config` payload, 400-ing against validation added when the Local/Remote LLM toggle shipped. Root-caused and fixed via a shared `llm_mode_fields()` test helper.

---

## Net effect

- `test_api.py` suite: **56 passed / 5 failed / 21 skipped → 61 passed / 0 failed / 21 skipped.**
- Working tree: 14 modified files + 1 new file (`voice_analysis.py`), all uncommitted per standing instruction.

## Honest gaps (don't treat these as resolved just because they're listed)

- The 24-hour review's 5 specific findings — count only, no recoverable description.
- 06-19's 120 findings' severity split — inferred from commit-group names, not real per-finding data.
- `batch_train_lora.py`/`voice_profiler.py` (the Voice Lab "train"/"profile" stages) live in a sibling repo outside this working tree and have never been reviewed in any of these efforts.
