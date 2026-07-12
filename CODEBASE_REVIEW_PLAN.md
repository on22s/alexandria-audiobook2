# Whole-Codebase Review Plan (for Fable 5)

**Goal:** review the entire Alexandria Audiobook2 codebase for (1) **bugs**,
(2) **missing code** (unhandled paths, absent validation/cleanup, half-built
features, silent-failure gaps), and (3) **dead code** (unused defs/imports/
endpoints, unreachable branches, orphaned files/scripts).

**Scope:** ~29k LOC — the FastAPI web app under `app/`, the root-level Voice
Lab / dataset-prep pipeline, launcher JS (including `pinokio.json`), and
`old_scripts/`. Exclude `env/`, `app/env/`, `venv/`, `.analysis_env/`,
`.claude/` (contains a stale full worktree copy at
`.claude/worktrees/checkpoint-resume/`), `cache/`, `__pycache__/`, `logs/`,
generated data (`*.json` *state* files — NOT `pinokio.json`,
`preparer_output/`, `dataset_temp/`), and third-party clones
(`insanely-fast-whisper-rocm/`).

**Budget note (Rule 5):** this plan is its own budgeted workflow (like
`/code-review`), so it operates under this document's scope rather than the
4k/30k ad-hoc token budgets — declared here explicitly per Rule 8.

**Style (non-negotiable):** read each file **top-to-bottom in full**, not
diffs or samples — the maintainer has confirmed whole-file reads are what catch
the real bugs here (a full-file read once caught a GPU-lock deadlock a diff scan
missed). Trace every finding across callers/callees before reporting it.

---

## 0. Read these first (context, before any file review)

Skim so you don't flag intentional design as a bug:
- `CLAUDE.md` — the 18 project rules. Especially **Rule 9** (protect safety
  nets: `check_global_gpu_lock`, VRAM-headroom checks, worker-stepdown retries —
  do NOT flag these as removable/redundant), **Rule 15** (single source of
  dispatch), **Rule 16** (verb-prefix names signal side effects), **Rule 17**
  (no dual-purpose params, *with* its documented exceptions), **Rule 18** (brace
  JS blocks). A "simplification" that violates a Rule is not a valid finding.
- `AGENTS.md` / `README.md` / `PREPARER_GUIDE.md` / `BATCH_PROCESSOR_GUIDE.md` /
  `COMPARE_GUIDE.md` — declared behavior, so you can spot code that contradicts
  its own spec (a *bug*) vs. code that's just unfamiliar.
- The concurrency model: every long task has a `process_state` entry (`app.py`
  ~line 468) and must pass `check_global_gpu_lock(task_name)` before GPU/LLM
  work (`GPU_TASKS` = everything except `NON_GPU_TASKS = {audacity_export,
  m4b_export}`). A new background task that skips the lock is a real bug;
  `voices` being *in* `GPU_TASKS` is intentional.
- Storage model: flat JSON via `utils.atomic_json_write` / `safe_load_json` /
  `file_lock` / `secure_filename`. There is no DB/ORM. Re-implementing any of
  these four helpers inline is a finding.

---

## 1. What to look for (the three buckets)

### A. Bugs (highest priority)
- Inverted/wrong conditions, off-by-one, falsy-zero (`if not x` where `x==0` is
  valid), wrong-variable copy-paste, missing `await`, unescaped regex metachars.
- Null/KeyError on realistic paths: cold cache, missing optional JSON field,
  error handlers, partial/interrupted writes, corrupt on-disk state trusted
  without validation.
- Concurrency: tasks mutating shared `process_state[...]` while a status route
  reads it; missing/incorrect GPU-lock acquisition; races around checkpoint/
  state files; `file_lock` used inconsistently (some clears locked, some not).
- LLM path: retry loops that reinterpret the same error differently across
  attempts (**Rule 10**); "is remote?" / "ideal settings?" computed two
  different ways in two files and drifting (**Rule 15** — this has bitten the
  repo before between `app.py` optimize and `review_script.py` self-heal);
  finish-reason / token-count / truncation handling.
- Remote (Thunder/SSH): stdout parsing that assumes a clean line (there's always
  a decorative banner — parse the *last* non-empty line); multi-statement SSH
  passed as separate argv (must be one pre-quoted `bash -lc <shlex.quote>`);
  "running but unreachable" when LM Studio isn't bound `0.0.0.0`.
- Subprocess orchestration: return codes ignored; a child that exits non-zero
  still recorded as "done"; stream/log handling that can deadlock or drop output.
- Path safety: external input (request bodies, filenames, saved state values)
  building a filesystem path without `secure_filename` → `../` escape.

### B. Missing code
- Endpoints/functions that consume external input without validating it.
- `except: pass` / bare excepts that swallow errors that should surface (**Rule
  8** — fail loud); missing error branches for the failure the code clearly
  anticipates.
- Cleanup gaps: temp files, checkpoints, state files, subprocesses, or GPU
  claims (`claim_gpu_task`) not released on every exit path (cancel, exception).
- Half-built features: `TODO`/`FIXME`/`XXX`/`HACK`, functions that return a
  placeholder, UI controls wired to endpoints that don't exist (or vice versa).
- Frontend/back-end contract gaps: a `fetch`/`API.post` in `index.html` with no
  matching FastAPI route, or a route no frontend or script ever calls.

### C. Dead code
- Unused imports, module-level constants, and locals.
- Functions/methods with **zero references** repo-wide (grep the symbol across
  `*.py`, `*.js`, `*.html`, and string literals for subprocess/dynamic calls —
  using the canonical grep scope in §3 step 2, or a match inside a venv or the
  stale worktree copy will fake a caller).
- Unreachable branches (conditions that can't be true given surrounding
  invariants; code after unconditional `return`/`raise`/`sys.exit`).
- Orphaned files: `old_scripts/*`, scripts superseded by newer ones, endpoints
  the SPA no longer references.
- Duplicated logic that should be one function (dead-by-duplication): two copies
  where one is never exercised.

**Dead-code false-positive traps — do NOT flag these without proof they're
unreachable:**
- FastAPI routes are registered by `@app.<verb>` decorators, not called by name.
- `index.html` handlers are wired via `onclick="..."`/`addEventListener` and
  referenced inside HTML *strings* — grep the string, not just JS calls.
- Subprocess entrypoints are invoked via `sys.executable ... script.py` — grep
  the filename, not the function.
- `main()` / `if __name__ == "__main__"` blocks are CLI entrypoints.
- Something referenced only by the **sibling repo** `alexandria-audiobook.git`
  (the cross-repo Voice Lab pipeline) is not dead here — note "cross-repo" and
  keep it.

---

## 2. Review units (batched; one Fable 5 agent per unit)

Sized so an agent can read the whole unit in one pass. Big files are their own
unit or split by section. **Priority order = risk order** (do 1–5 first).

| # | Unit | Files (LOC) | Focus |
|---|------|-------------|-------|
| 1 | **Web orchestration A** | `app/app.py` lines ~1–2000 (models, config, state, upload, script/persona routes) | routes + `process_state`/GPU-lock wiring, input validation, path safety |
| 2 | **Web orchestration B** | `app/app.py` ~2000–4000 (review single/batch, nicknames, checkpoints, detect) | resume/checkpoint logic, subprocess rc handling, concurrency |
| 3 | **Web orchestration C** | `app/app.py` ~4000–6060 (TTS/audio routes, exports, voice library, LLM optimize/heal, helpers) | Rule 15 heal-policy drift, export cleanup, dead helpers |
| 4 | **LLM annotation/review** | `app/generate_script.py` (743), `app/review_script.py` (1204), `app/review_prompts.py`, `app/default_prompts.py` | checkpoint/resume, retry policy (Rule 10), VRAM watchdog, batching off-by-one |
| 5 | **LLM personas/nicknames/bench** | `app/generate_personas.py` (954), `app/find_nicknames.py` (433), `app/llm_bench.py` (258), `app/persona_prompts.py` | fail-loud on partial results, concurrency cache staleness |
| 6 | **Remote/LLM settings** | `app/lmstudio_settings.py` (858), `app/hf_utils.py` (106) | SSRF probe gate, SSH argv/banner parsing, is-remote single source |
| 7 | **TTS engine** | `app/tts.py` (1811), `app/tts_vram_benchmark.py` (279) | device/VRAM handling, timeline/combine math, external-Gradio path, resource release |
| 8 | **Export/assembly** | `app/project.py` (1120) | chunk→export correctness, M4B/Audacity build, temp cleanup |
| 9 | **Training + utils** | `app/train_lora.py` (673), `app/utils.py` (162), `download_model.py` (67, repo root — not in `app/`) | atomic-write/lock correctness, LoRA arg handling |
| 10a | **Frontend SPA A** | `app/static/index.html` first half (~1–3300: markup, styles, core JS state/API helpers, Setup→Script→Voices flow) | vanilla-JS bugs, Rule 18 unbraced blocks, API-contract gaps, escaping/XSS in `innerHTML` |
| 10b | **Frontend SPA B** | `app/static/index.html` second half (~3300–6642: Editor/Result, Designer/Dataset/Training/Preparer/Voice Lab tabs) | same focus as 10a, plus dead handlers; read any function straddling the split boundary in full |
| 11 | **Prep pipeline core** | `alexandria_preparer_rocm_compatible.py` (3448) | use the `alexandria-preparer-architecture` skill to map phases first; ASR/align/annotate/chunk/resume, scratch-dir cleanup |
| 12 | **Prep pipeline support** | `alexandria_alignment.py` (1169), `voice_analysis.py` (691), `llm_enricher.py` (159) | alignment edge cases, dedup phase, enrichment error paths |
| 13 | **Batch/compare/naming** | `alexandria_batch_processor.py` (761), `alexandria_compare.py` (1089), `name_voices.py` (254) | batch resume/idempotence, compare-log logic |
| 14 | **Launcher JS** | `pinokio.js` (144), `install.js`, `start.js`, `start_llm.js`, `reset.js`, `update.js`, `torch.js`, `pinokio.json` | against `PINOKIO.md`/examples: URL-capture pattern, port/venv/path correctness, metadata sanity, dead scripts not referenced by `pinokio.js` |
| 15 | **Legacy sweep (dead-code)** | `old_scripts/*` (649, incl. `rename_kizu_zips.sh`) + a repo-wide unused-symbol pass (use §3 step 2 grep scope) | confirm truly orphaned; list deletion candidates with evidence |
| 16 | **Tests** | `app/test_api.py` (1428), `app/test_checkpoint_resume.py` (321) | tests that assert placeholders/return-values instead of real behavior (**Rule 12**); stale tests; coverage holes for units 1–9 |

---

## 3. Method per unit (each agent)

1. Read the unit's file(s) **completely**. For split units (1–3, 10a/10b), also
   read the function bodies that straddle the boundary so nothing is reviewed
   half-open.
2. For each candidate issue, **grep the symbol repo-wide** (`*.py *.js *.html`)
   to confirm callers/contract before deciding bug vs. dead vs. intentional.
   **Canonical grep scope** (mandatory — the repo root contains three venvs, an
   HF cache, a third-party clone, and a stale full worktree copy that will fake
   callers):
   `grep -rn --include='*.py' --include='*.js' --include='*.html'
   --exclude-dir={env,venv,.analysis_env,.claude,cache,insanely-fast-whisper-rocm,__pycache__,logs,dataset_temp,preparer_output,node_modules}
   <symbol> .`
   Check `logs/` and `logs/api/*-latest.log` when behavior is unclear — the app
   writes rich runtime logs (LLM req/resp in `logs/review_responses.log`).
3. Classify each finding: `bug` | `missing` | `dead`, with a severity
   (`high`/`med`/`low`) and a one-line concrete failure/impact.
4. **Checkpoint** every ~25/50/75% of the unit to
   `logs/review_checkpoints/<unit-id>.json` (git-ignored under `logs/`; avoid
   `/tmp` in this environment) so a cut-off agent isn't a total loss.
5. Return findings as JSON (schema below). Prefer surfacing with evidence over
   staying silent; a nameable failure scenario earns a spot.

### Finding schema
```json
{
  "unit": "4-llm-annotation",
  "file": "app/review_script.py",
  "line": 812,
  "category": "bug | missing | dead",
  "severity": "high | med | low",
  "summary": "one sentence",
  "evidence": "quote the line(s) / grep result proving it",
  "failure_or_impact": "concrete input/state -> wrong result, or why it's unreachable/unused",
  "suggested_fix": "shortest correct change; cite an existing helper to reuse if any",
  "confidence": "confirmed | plausible",
  "rule_check": "none | 'consistent with Rule N' | 'verify against Rule N'"
}
```

---

## 4. Orchestration (how to run it)

1. **Fan out:** launch all units (1–16, with 10 split into 10a/10b = 17
   agents) as parallel Fable 5 agents (batch to stay within limits — e.g. 4–6
   at a time, priority order). Each gets: this plan's §0–§3 **and §5** (the
   reviewer guardrails must reach the reviewers), its file list, and the
   checkpoint instruction. Fable 5 is fast, so many short whole-file passes
   beat one giant pass.
2. **Verify:** dedup near-duplicates, then run a 1-vote verifier per surviving
   candidate (recall-biased): `CONFIRMED` (constructible from code), `PLAUSIBLE`
   (realistic state), or `REFUTED` (quote the guard/invariant, or it's
   intentional per a Rule). Drop REFUTED. This mirrors the repo's existing
   `/code-review-20` finder→verify flow.
3. **Consolidate:** merge into one ranked report grouped by category, then by
   severity. For **dead code**, produce a separate deletion-candidate list with
   grep-evidence of zero references (flag anything dynamic/cross-repo as
   "verify before deleting"). For **missing code**, group by subsystem so gaps
   in one area are visible together.
4. **Deliverable:** `CODEBASE_REVIEW_FINDINGS.md` — executive summary (counts by
   category/severity, top 10 risks), then the full table, then the dead-code and
   missing-code appendices. Tag findings that fall in files with uncommitted
   checkpoint-resume work on `feature/night-super-night-themes` (`app/app.py`,
   `app/generate_script.py`, `app/find_nicknames.py`, `app/lmstudio_settings.py`,
   `app/static/index.html`, `app/test_checkpoint_resume.py`) as "on unmerged
   feature work" so they aren't mistaken for shipped bugs. Nothing is edited;
   review-only unless the maintainer asks for fixes.

---

## 5. Guardrails for the reviewers (avoid noise)

- Do not propose removing a cache/limit/retry to "simplify" (Rule 9). If one
  looks in the way, explain why and flag `plausible`, don't assert `bug`.
- Do not flag a `get_`-named pure reader vs. `ensure_`/`apply_`/`save_`
  side-effecting name as wrong — that's the intended convention (Rule 16); in
  `index.html`, `render*`/`on*`/`open*` are also intentional side-effect names.
- Do not flag mutation of `process_state[...]` entries, documented mutator
  callbacks, or load-mutate-save locals as Rule 17 violations — those are the
  documented exceptions.
- A "duplicate" across this repo and `alexandria-audiobook.git` is cross-repo by
  design; note it, don't call it dead.
- When unsure whether something is dead: default to `plausible` + "verify" — a
  wrong deletion is worse than a missed one.
