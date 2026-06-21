# CLAUDE.md Rule-Compliance Audit — Findings Log

Plan: `docs/superpowers/plans/2026-06-19-claude-md-rule-compliance-audit.md`
Progress: `docs/audits/claude_md_rule_audit/PROGRESS.md`

Findings use a single incrementing `F-001, F-002, ...` counter across the whole audit. Use the Finding Template and Fix-now criteria from the plan's Task 1.

---

All findings resolved. See FIXED.md.

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
