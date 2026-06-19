# CLAUDE.md Rule-Compliance Audit — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking. **Run tasks sequentially, never in parallel** — every task writes to the same two shared files (`PROGRESS.md`, `FINDINGS.md`); parallel writers will clobber each other.

**Goal:** Systematically verify every tracked source file in this repo (the `app/` FastAPI application plus the root-level dataset-prep pipeline) against CLAUDE.md's 18 numbered rules — excluding Rule 3, which is paused 2026-06-19 through 2026-06-21 — producing a durable findings log and a fix backlog, across as many sessions as it takes.

**Architecture:** This is an audit plan, not a TDD feature-build plan, so it deviates from the usual "write failing test → implement → pass" task shape: the code to inspect already exists and findings can't be known in advance. Instead, each task applies one fixed, fully-specified **Standard Audit Procedure** (defined once below, in Task 1) to one **piece** from the manifest (Tasks 2–3). Progress and findings live in two repo files that survive across sessions/context resets:
- `docs/audits/claude_md_rule_audit/PROGRESS.md` — one checkbox per piece, the resumption pointer.
- `docs/audits/claude_md_rule_audit/FINDINGS.md` — append-only log of every violation found, using the Finding Template.

**Method:** Manual Read + targeted `grep`, no test execution required (this is a static-analysis-style audit of existing code, not a behavioral regression sweep).

**Fix policy (confirmed with user 2026-06-19):** Log every finding. Only fix **mechanical, zero-judgment** issues inline, in their own commit, separate from the findings-log commit. Everything else — anything touching behavior, a named safety net (Rule 9), retry semantics (Rule 10), or cross-file consolidation (Rule 15) — gets logged as `needs-decision` and held for the final approval gate (Task 5). Never bundle a code fix and a findings-log update in the same commit.

**Scope (confirmed with user 2026-06-19):** Both Phase 1 (`app/` — CLAUDE.md's "Key files") and Phase 2 (root-level dataset-prep pipeline scripts) are in scope for this single plan.

**Explicitly excluded from this audit** (assumption — flag if you disagree):
- `old_scripts/` — archived/dead code, not on a live path.
- Launcher files (`install.js`, `start.js`, `update.js`, `reset.js`, `pinokio.js`, `torch.js`, `start_llm.js`, `pinokio.json`) — governed by CLAUDE.md's separate "Launcher work" section, not the 18-rule architecture section.
- Shell runner scripts (`run_*.sh`, `build_test_corpus.sh`, `watch_subset.sh`) — bash orchestration; Rules 16/17/18 are written in Python/JS terms and don't translate cleanly.
- `app/env/` (vendored virtualenv), docs/`*.md`, `*.txt` prompt-text duplicates, `*.ipynb` — not code subject to these rules.

---

## Rule applicability matrix

CLAUDE.md's 18 rules split into three buckets. Only bucket 1 gets checked per-piece below — buckets 2 and 3 are listed here once so no future session re-litigates them or silently forgets why they're absent from the per-piece checklist (Rule 8: surface, don't hide).

**1. Code-auditable now (checked in every piece task):** Rules 2, 8, 9, 10, 12, 13, 16, 17, 18 (18 only applies to JS pieces). Rule 15 is code-auditable but is **cross-cutting** (you need to see all files to find a duplicated decision) — tag candidates while auditing each piece, resolve in the dedicated Task 4 pass, not per-piece.

**2. Process-only, not retroactively auditable from static code (skipped per-piece, on purpose):** Rules 1, 4, 5, 6, 7, 11, 14. These govern how *Claude* should run a work session (think first, checkpoint, budget tokens, plan before implementing, don't re-litigate rejected fixes) — they leave no reliable fingerprint in committed code. Compliance with them going forward is enforced by Claude following CLAUDE.md during this very sweep, not by something this audit can find in a `Read` call.

**3. Excluded per user instruction:** Rule 3 ("Surgical Changes") — paused 2026-06-19 through 2026-06-21 per CLAUDE.md's own text. **Time-sensitive note for future sessions:** if you are running any task in this plan after 2026-06-21, Rule 3 is back in force — revert to strict surgical-changes discipline even though this plan was drafted during the paused window.

---

## Task 1: Standard Audit Procedure (read this once, apply it to every piece)

For each piece, for each rule in its "Applicable rules" column, answer the rule's question(s) below. Anything that fails a question is a finding — log it with the Finding Template before moving to the next rule. Pieces marked with a reduced rule set (HTML/CSS, pure-constant modules) skip the N/A rules entirely; don't spend time hunting for things that structurally can't exist there.

**Rule 2 — Simplicity First**
- Is there a function/class/parameter/config flag in this piece with zero real callers anywhere in the repo (dead code)? Check: `grep -rn "<name>(" --include=*.py --include=*.html .` (excluding the def line and tests-only callers, which is itself worth flagging separately).
- Is there a generic mechanism (factory, registry, inheritance hierarchy, plugin system) serving only one concrete case today?
- Are there parameters or branches that exist for a hypothetical future caller rather than a current one?

**Rule 8 — Fail Loud**
- Any bare or broad `except` that swallows the error without logging, re-raising, or surfacing it into `process_state`/the caller's return value?
- Any function that returns a default/empty/placeholder value on failure such that the caller can't distinguish "succeeded empty" from "failed silently"?
- Any loop where a per-item failure is `continue`d past, then the batch is reported as fully successful regardless?

**Rule 9 — Protect Safety Nets**
- List every named safety mechanism in this piece (lock, retry, threshold/headroom check, atomic write, checkpoint/resume contract). Cross-reference CLAUDE.md's named examples: `check_global_gpu_lock`, VRAM-headroom checks (`get_vram_usage`/`wait_for_vram_headroom`), `review_batch`'s retry/`gc.collect()` loop, `file_lock`, `atomic_json_write`, the preparer's source-marker/resume contract.
- Has any of them been weakened, made conditional, or bypassed without an explicit comment/commit explaining why? **Do not fix this yourself** — log as `needs-decision` per Rule 9's "explain why before touching it and confirm first."

**Rule 10 — Decide Before You Retry**
- Is there a retry loop (`for attempt in range(...)`, `while retries`, etc.)?
- If so, is the error-interpretation logic byte-for-byte the same on every attempt, or does it change (e.g. parses the error body differently, treats a timeout as fatal on attempt 1 but retryable on attempt 2)?

**Rule 12 — Verify Functionality, Not Just Return Values** (every piece, but especially `test_api.py`)
- Does each test/verification helper assert a specific expected value or state, not just `assert resp.status_code == 200` with no body check, or `assert result is not None`?
- Does it cover a real failure/edge case (404, invalid input, partial/cancelled state), or only the happy path?

**Rule 13 — Respect Architectural Styles**
- (`index.html` pieces) Any framework idiom present — JSX, `v-if`/`v-model`/`v-on`, `useState`/`useEffect`, `React.`/`Vue.`? `grep -n "v-bind\|v-on\|v-model\|useState\|useEffect\|React\.\|Vue\."`.
- (storage-layer pieces: `app.py`, `project.py`, `utils.py`, `generate_personas.py`, etc.) Any ORM/SQL/DB client introduced where CLAUDE.md documents a flat-JSON `scripts/`/`voice_library.json` model? `grep -n "sqlite3\|sqlalchemy\|psycopg\|pymongo"`.

**Rule 15 — Single Source of Dispatch** *(tag only — resolved in Task 4)*
- Does this piece re-derive a conceptually-global decision locally (is this LLM remote? what are ideal settings? is the GPU busy? is this path safe?) instead of calling a shared helper? If yes, add a `[rule15-candidate]` entry to FINDINGS.md describing the decision — don't try to resolve it now, you can't see the other files from here.

**Rule 16 — Verb-First Names**
- For every top-level function in the piece: does its name's verb (`get_`/`is_`/`has_` = pure read; `ensure_`/`apply_`/`save_` = side-effecting; in `index.html` specifically also `render*`/`on*`/`open*`) match what its body actually does? Flag any `get_`-named function with a side effect (write, mutation of a passed-in object, network call, reload).

**Rule 17 — No Dual-Purpose Parameters**
- Does any function mutate a dict/object it was passed, to report a result back to its caller, instead of returning a new value?
- If yes, does it match one of CLAUDE.md's three documented exceptions: (a) live-shared state under Rule 9 protection (e.g. `process_state[task]` entries read concurrently by status-polling routes), (b) a callback/mutator parameter whose *documented* contract is to mutate (e.g. `project.py`'s `_modify_chunk(index, mutator)`), or (c) a function loading/mutating/saving its own local variable with no cross-function handoff? If none of the three apply, it's a finding.

**Rule 18 — Always Brace Single-Line Blocks in JS** (`index.html` JS pieces only)
- Any `if (...) doSomething();` / `for (...) doSomething();` / `while (...) doSomething();` without `{ }`? Scan every `if`/`for`/`while` in the piece's `Read` output.

### Finding Template (append to FINDINGS.md)

```markdown
### [F-###] Rule <N> — <short title>
- **Piece:** <piece ID>
- **Location:** `path:line` (or function/anchor name)
- **Severity:** low | medium | high
- **Description:** <what's wrong, 1-3 sentences>
- **Status:** logged | fixed-inline (commit `<sha>`) | needs-decision
- **Suggested fix:** <1-2 sentences, or "see needs-decision">
```

Use a single incrementing `F-001, F-002, ...` counter across the whole audit (don't restart per piece).

### Fix-now criteria (mechanical, zero-judgment only)

Safe to fix immediately, in a commit separate from the findings-log commit:
- Missing JS braces (Rule 18).
- A private helper with **zero** callers anywhere in the repo, confirmed via `grep -rn`, with no plausible external/test entrypoint.
- Closing a Rule-16 false positive (a `get_`-named function that, on inspection, really is pure) requires no code change — just mark `Status: logged` with a one-line note, don't open a fix.

Everything else (Rule 8 silent-failure fixes, Rule 9 safety-net concerns, Rule 10 retry-consistency fixes, Rule 12 test-rigor gaps, Rule 15 consolidation, Rule 17 dual-purpose-parameter refactors) → log only, `Status: needs-decision`, do not touch the code.

---

## Task 0: Set up tracking files

**Files:**
- Create: `docs/audits/claude_md_rule_audit/PROGRESS.md`
- Create: `docs/audits/claude_md_rule_audit/FINDINGS.md`

- [ ] **Step 1:** Create `docs/audits/claude_md_rule_audit/PROGRESS.md` with one `- [ ] P##` checkbox line per piece ID from the four manifest tables in Tasks 2–3 below (54 lines total), grouped under `## Phase 1: app/` and `## Phase 2: root pipeline`. Each line: `- [ ] P01 — app/utils.py`.
- [ ] **Step 2:** Create `docs/audits/claude_md_rule_audit/FINDINGS.md` with a header `# CLAUDE.md Rule-Compliance Audit — Findings Log` and a one-line pointer back to this plan's path, then leave it empty below that (findings get appended as pieces are audited).
- [ ] **Step 3:** Commit.

```bash
git add docs/audits/claude_md_rule_audit/PROGRESS.md docs/audits/claude_md_rule_audit/FINDINGS.md
git commit -m "audit: set up CLAUDE.md rule-compliance tracking files"
```

---

## Task 2: Phase 1 — audit `app/` (37 pieces)

For each row: resolve current line numbers via `grep -n` on the listed anchor function/route names (line numbers below are as of 2026-06-19 and will drift once earlier pieces' mechanical fixes land), `Read` that range, apply the Task 1 procedure for the listed rule subset, append findings to FINDINGS.md, check the box in PROGRESS.md, commit (`audit: piece P## <file> — logged to FINDINGS.md`). If a piece is too large to finish within one task's budget, stop at a function boundary, note the stopping point as a comment in PROGRESS.md next to that piece's checkbox, and resume from there next session — never leave a piece partially audited without a note.

Default rule subset (unless a row overrides it): **2, 8, 9, 10, 12, 13, 16, 17** (Rule 18 added only for `index.html` JS pieces; Rule 15 is tag-only everywhere, see Task 1).

### Group A — `app/` small & medium Python modules

| ID | File | Anchors | ~Lines | Rules | Notes |
|----|------|---------|--------|-------|-------|
| P01 | `app/utils.py` | whole file | 162 | default | Emphasize Rule 9: `file_lock`, `atomic_json_write` are named safety nets in CLAUDE.md. |
| P02 | `app/hf_utils.py` | whole file | 106 | default | |
| P03 | `app/default_prompts.py` + `app/persona_prompts.py` + `app/review_prompts.py` | whole files, audited together | ~106 | 2 only | Pure constants — no functions, so 8/9/10/12/13/16/17 are structurally N/A. Check for unused/orphaned prompt variants. |
| P04 | `app/lmstudio_settings.py` | whole file | 381 | default | Emphasize Rule 15: this is CLAUDE.md's own example of `is_remote_llm` as the canonical single-source-of-dispatch fix. |
| P05 | `app/llm_bench.py` | whole file | 251 | default | |
| P06 | `app/find_nicknames.py` | whole file | 368 | default | |
| P07 | `app/tts_vram_benchmark.py` | whole file | 279 | default | |
| P08 | `app/generate_script.py` | whole file | 556 | default | Emphasize Rule 8: CODE_REVIEW.md (2025-06-13) flagged a bare `except Exception: continue` near line ~170 — confirm current status (fixed, or still there). |
| P09 | `app/train_lora.py` | whole file | 673 | default | |
| P10 | `app/generate_personas.py` | whole file | 954 | default | |
| P11 | `app/project.py` | whole file | 1120 | default | Emphasize Rule 17: confirm `_modify_chunk(index, mutator)` matches documented exception (b), not a violation. |
| P12a | `app/review_script.py` | `get_vram_usage` → `merge_consecutive_narrators` (lines 1–~595) | ~595 | default | Emphasize Rule 9: VRAM headroom + checkpoint resume. |
| P12b | `app/review_script.py` | `review_batch` → `main` (lines ~596–1198) | ~600 | default | Emphasize Rule 9 (the named `review_batch` retry/`gc.collect()` loop), Rule 10, Rule 12 (`check_text_loss`/`diff_entries`). |
| P13a | `app/tts.py` | `voice_category` → `compute_timeline` (lines 1–118) | 118 | default | |
| P13b | `app/tts.py` | `class TTSEngine` (lines 119–end) | ~1690 | default | Large — expect to split across 2+ sessions; checkpoint at a method boundary if needed. |
| P14a | `app/test_api.py` | helpers → chunk tests (lines 1–~700) | ~700 | default, Rule 12 hard | |
| P14b | `app/test_api.py` | status/preparer/voicelab/lora/dataset-builder/audio tests + `run_all_tests`/`main` (lines ~700–1396) | ~700 | default, Rule 12 hard | |

### Group B — `app/app.py` (5,565 lines — split by route group)

| ID | Anchors | ~Lines | Rules | Notes |
|----|---------|--------|-------|-------|
| P15 | imports → `check_global_gpu_lock` (lines 1–1491): log helpers, GPU stats, `check_disk_space`, all Pydantic models, `process_state`/`GPU_TASKS`/`NON_GPU_TASKS` init | 1491 | default | Emphasize Rule 9 HARD — this piece *is* `check_global_gpu_lock`. Large; expect 2+ sessions, checkpoint at the Pydantic-models/process_state boundary if needed. |
| P16 | `/api/system/stats` → `/api/upload` (lines 1492–2050) | 558 | default | |
| P17 | `/api/generate_script` → `/api/logs/{task_name}` (lines 2051–2646): script-gen, review (+contextual), nicknames, character_aliases, batch start/cancel/pause/resume for both | 595 | default | |
| P18 | `/api/voices` → `/api/suggest_voices` (lines 2647–3011) | 364 | default | |
| P19 | `/api/audiobook` → `/api/review/checkpoints` (lines 3012–3462): chunks CRUD, merge, export, m4b, generate_batch(+fast), cancel_audio, reports | 450 | default | |
| P20 | `/api/scripts` → `/api/voice_library/apply_bulk` (lines 3463–3983) | 520 | default | |
| P21 | `/api/voice_design/preview` → `/api/clone_voices/{voice_id}` (lines 3984–4142) | 158 | default | |
| P22 | `/api/lora/upload_dataset` → `/api/lora/preview/{adapter_id}` (lines 4143–4667) | 524 | default | |
| P23 | `/api/dataset_builder/*` (lines 4668–5043) | 375 | default | |
| P24 | `/api/preparer/*` (lines 5044–5318) | 274 | default | |
| P25 | `/api/voicelab/*` (lines 5319–end, 5565) | 246 | default | |

### Group C — `app/static/index.html` (6,539 lines — split by function-group; Rule 18 applies to all JS pieces)

| ID | Anchors | ~Lines | Rules | Notes |
|----|---------|--------|-------|-------|
| P26 | HTML/CSS shell + all tab markup, before main `<script>` (lines 1–1789) | 1789 | 2, 13 only | No functions/behavior here — 8/9/10/12/16/17/18 are N/A. Check Rule 13 (no framework idioms) and Rule 2 (no dead markup). Large; this is a skim-for-structure pass, not line-by-line. |
| P27 | `showToast` → `testLlmConnection` (lines ~1790–2215) | 425 | default + 18 | |
| P28 | `loadConfig` → `_onReviewDone` (lines ~2216–2810) | 594 | default + 18 | |
| P29 | `_loadScriptList` → `pollPersonaStatus` (lines ~2811–3177) | 366 | default + 18 | |
| P30 | `createVoiceCard` → `submitCastApplyBulk` (lines ~3178–3861) | 683 | default + 18 | |
| P31 | `collectVoiceConfig` → `_runBatchRender` (lines ~3862–4663) | 801 | default + 18 | Large; checkpoint at `loadChunks`/`saveRowEdits` boundary if needed. |
| P32 | `pollLogs` → `resetDesignerForm` (lines ~4664–5147) | 483 | default + 18 | |
| P33 | `loadLoraDatasets` → `dsbStopBatch` (lines ~5148–5948) | 800 | default + 18 | Large; checkpoint at the `dsb*` (dataset-builder) boundary if needed. |
| P34 | `updateSystemStats` → `viewReport` (lines ~5949–6539) | 590 | default + 18 | |

- [ ] All 37 boxes in PROGRESS.md's `Phase 1` section checked, with one FINDINGS.md entry (or explicit "no findings") logged per piece, before moving to Task 3.

---

## Task 3: Phase 2 — audit the root-level dataset-prep pipeline (17 pieces)

Same procedure as Task 2. Default rule subset: **2, 8, 9, 10, 12, 13, 16, 17** (these are all Python — Rule 18 never applies in this group).

**Known cross-file duplication candidate — flag this explicitly when you reach P40a/P41b/P42d, don't rediscover it from scratch:** `alexandria_compare.py` defines `parse_annotated_tokens` (line 72) and `merge_annotations_with_source` (line 159). `alexandria_alignment.py` defines functions with the **identical names** again at lines 985 and 1072. Separately, the preparer's own phase-7 alignment recovery (`find_best_match`/`find_anchor_position`/`realign`) is documented by the `alexandria-preparer-architecture` skill as living in the preparer file itself ("same" script), while `alexandria_alignment.py` *also* defines `find_best_match` (685), `find_anchor_position` (754), and `realign` (829). Verify in each piece whether these are intentional per-file forks (e.g. the preparer needs a self-contained subprocess with no cross-file import) or accidental duplication, and resolve definitively in Task 4.

| ID | File | Anchors | ~Lines | Notes |
|----|------|---------|--------|-------|
| P35 | `download_model.py` | whole file | 67 | |
| P36 | `llm_enricher.py` | whole file | 159 | This is Phase 4 of the preparer pipeline (LLM enrichment) — see `alexandria-preparer-architecture` skill. |
| P37 | `name_voices.py` | whole file | 254 | |
| P38a | `voice_analysis.py` | `load_model` → `run_dedup` (lines 1–315) | 315 | |
| P38b | `voice_analysis.py` | `run_analyze` → `main` (lines 316–691) | 375 | |
| P39a | `alexandria_batch_processor.py` | `get_gpu_stats` → `check_disk_space` (lines 1–250) | 250 | |
| P39b | `alexandria_batch_processor.py` | `class BatchProcessor` → `main` (lines 251–761) | 510 | |
| P40a | `alexandria_compare.py` | `load_jsonl` → `write_output` (lines 1–482) | 482 | Contains the duplication candidate above (`parse_annotated_tokens` at 72, `merge_annotations_with_source` at 159). |
| P40b | `alexandria_compare.py` | `run` → `main` (lines 483–1089) | 606 | Note: a separate skill `alexandria-compare-review` covers reviewing *user edit patterns* in this script's output log — different purpose (UX/heuristic tuning), not a conflict with this rule audit. |
| P41a | `alexandria_alignment.py` | `_expand_honorifics` → `trim_span_to_alignment` (lines 1–486) | 486 | |
| P41b | `alexandria_alignment.py` | `_num_eq_step_trailing` → `merge_annotations_with_source` (lines 487–1169) | 682 | Contains the duplication candidate above (`find_best_match` 685, `find_anchor_position` 754, `realign` 829, `parse_annotated_tokens` 985, `merge_annotations_with_source` 1072). Emphasize Rule 9/10: drift-resistant tiered-fallback alignment is exactly the kind of logic Rule 10 cares about. |
| P42a | `alexandria_preparer_rocm_compatible.py` | `validate_inputs`, `_wav_overflow_info`, `_ffmpeg_decode_to_wav`, `_ffmpeg_decode_to_numpy`, load step in `main()` | n/a (use `grep -nE "^def "`) | Emphasize Rule 9 HARD — skill's own anti-pattern: don't evaluate the WAV-wrap path without `_wav_overflow_info` and the load-step branch in `main()` together. |
| P42b | `alexandria_preparer_rocm_compatible.py` | `choose_and_transcribe` + each `transcribe_with_*` | n/a | |
| P42c | `alexandria_preparer_rocm_compatible.py` | `_build_source_state`, `normalize`, `_COMPOUND_SPLIT`, `_find_best_cut`, `_provisional_entries_for_anchor` | n/a | |
| P42d | `alexandria_preparer_rocm_compatible.py` | `find_best_match`, `realign`, `find_anchor_position` (preparer's own copies) | n/a | Resolve the duplication-vs-`alexandria_alignment.py` question here (see callout above). Emphasize Rule 15. |
| P42e | `alexandria_preparer_rocm_compatible.py` | `_load_llm`, `annotate_chunks`, `_sanitize_annotation`, atomic write/promote in `main()` | n/a | Emphasize Rule 9 (atomic write), Rule 8. |
| P42f | `alexandria_preparer_rocm_compatible.py` | `_load_existing_checkpoint`, `_check_source_marker`, `_write_source_marker`, `_wipe_temp_dir`, `class ProgressTracker` | n/a | Emphasize Rule 9 HARDEST — this is the resume contract the skill explicitly says not to touch from outside. |

For `alexandria_preparer_rocm_compatible.py` (P42a–f): per the `alexandria-preparer-architecture` skill, **run `grep -nE "^def |^class " alexandria_preparer_rocm_compatible.py` fresh at the start of each piece** — the skill explicitly warns the file evolves and line numbers drift, so none are pre-computed here.

- [ ] All 17 boxes in PROGRESS.md's `Phase 2` section checked, with one FINDINGS.md entry (or explicit "no findings") logged per piece, before moving to Task 4.

---

## Task 4: Rule 15 cross-cutting pass

**Files:** Read `docs/audits/claude_md_rule_audit/FINDINGS.md` (all `[rule15-candidate]` tags) plus the duplication callout from Task 3.

- [ ] **Step 1:** Collect every `[rule15-candidate]` tagged finding from FINDINGS.md into a list of "decisions" (e.g. "is this LLM remote", "alignment-recovery logic", "ideal LM Studio settings").
- [ ] **Step 2:** For each decision, `grep -rn` across `app/` and the root pipeline scripts for every place that decision is computed, and confirm whether they all call one shared function or independently re-derive the answer.
- [ ] **Step 3:** For each confirmed duplication, log a finding (`Status: needs-decision` — consolidating cross-file logic is never a mechanical fix) describing every call site found and which one (if any) should become the single source of dispatch.
- [ ] **Step 4:** Commit FINDINGS.md updates.

```bash
git add docs/audits/claude_md_rule_audit/FINDINGS.md
git commit -m "audit: Rule 15 cross-cutting pass — duplicate-dispatch findings"
```

---

## Task 5: Synthesis report + approval gate

- [ ] **Step 1:** Read the full FINDINGS.md. Tally findings by rule and by severity.
- [ ] **Step 2:** Produce a summary (in FINDINGS.md or a short follow-up message to the user, not a new file) — counts per rule, and an ordered fix backlog for every `needs-decision` finding (high severity first).
- [ ] **Step 3:** Present the backlog to the user and get explicit approval before implementing any `needs-decision` fix (per Rule 9 and Rule 14 — these are exactly the non-trivial, judgment-requiring changes those rules gate). Do not fix anything in this task yourself.

---

## Session pacing & resumption

- **Budget heuristic:** small pieces (~100–400 lines, no findings) cost roughly 1.5–3k tokens each; large pieces (800+ lines, or several findings) can run 4–8k. Against the 30k/session budget (Rule 5), expect **roughly 5–8 small pieces or 2–4 large pieces per session** — but checkpoint after *every single piece* regardless of remaining budget; never leave a piece half-audited without a note.
- **Resuming in a fresh session:** read `docs/audits/claude_md_rule_audit/PROGRESS.md`, find the first unchecked box, re-read this plan's Task 1 (Standard Audit Procedure) and the manifest row for that piece, then proceed. You do not need any memory of prior sessions — everything required is in this plan plus the two tracking files.
- **Total scope:** 54 audit pieces (Task 2 + Task 3) + 1 setup task + 1 cross-cutting pass + 1 synthesis task = 57 tasks. At the pacing above, expect on the order of 10–20 sessions.
