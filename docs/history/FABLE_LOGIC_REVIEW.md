# Logic review brief (for Fable)

## Purpose

Separate ask from `FABLE_ALGORITHM_REVIEW.md` (which is about swapping
hand-rolled matching/dedup for known algorithms). This one is about
**control-flow and business-rule correctness**: conditionals, state
transitions, threshold/boundary decisions, and multi-step logic that's
correct in the common case but may have an edge the author didn't fully
reason through. Read the actual functions below — this brief gives you a
starting map with real line numbers and the reasoning already on record in
each function's docstring/comments, not a final verdict.

This is a **scouting/analysis task, not an implementation task.** Produce a
written report — see "Deliverable" below.

## Ground rules

- **This is not a diff review.** `/code-review` and `/code-review-20` (this
  repo's existing skills) do line-by-line bug-hunting against a git diff.
  This brief is the opposite scope: whole-codebase, design-level logic that
  a diff-scoped review would never see because it was never a recent change
  — code that's been sitting there working, but whose correctness depends on
  an assumption worth re-checking now that you have fresh eyes on it.
- **Read the comments before flagging something.** Several of the functions
  below already have a paragraph explaining a subtlety or a bug they fix
  (grep the file for the function name — the "why" is usually already
  written down). If you're about to flag something the comment already
  explains, that's not a finding — cite the comment and move on. A finding
  is something the comment *doesn't* already account for.
- **State your failure scenario concretely.** "This could theoretically be
  wrong" is not a finding. "Given input X / state Y / this ordering of
  concurrent calls, output is Z when it should be W" is. This repo's own
  code-review skill enforces the same bar — match it.
- **Respect Rule 9 (`app/CLAUDE.md`) — don't recommend removing a safety
  net to simplify logic.** Several of the areas below (GPU lock, checkpoint/
  resume) are deliberately conservative. A simpler version that's less safe
  is not an improvement; say so if that's what you find.
- Every area below already has test coverage — find the test file (usually
  `test_<module>.py` or `test_<module>_regressions.py`) and read it before
  concluding a scenario isn't handled; it may already be covered and you
  just haven't found the test.

## Areas to review

### 1. Retry/quality-gate control flow — `app/generate_script.py`

The single richest piece of conditional logic in the codebase, and the
place this session's two real production incidents both came from. Trace
the full decision tree across these functions, in this order:
- `get_quality_retry_policy` and `is_severe_chunk_truncation` (find via
  grep — decide `retry_same_budget` vs `increase_tokens`)
- `get_chunk_retry_action` (`~line 455`) — decides `retry` vs `split` based
  on `consecutive_severe` count and `allow_early_split`
- `_is_near_miss_recall` (`~line 474`) and its threshold
  `NEAR_MISS_RECALL_THRESHOLD = 0.75` — is 0.75 the right cutoff, or was it
  picked from one incident's specific numbers (86%, 89%) without checking
  whether it's too permissive/strict for other recall distributions?
- `process_chunk` (`~line 855`) — the `call(max_retries)` → near-miss check
  → `call(0)` bonus-attempt sequence. Specifically: the bonus attempt calls
  `call(0)`, meaning **zero retries** — if the bonus attempt itself fails
  quality validation, does it get a chance to retry, or does one bad sample
  on the bonus attempt burn the whole bonus with no recourse? Trace exactly
  what `max_retries=0` does inside `call_llm_for_entries`.
- `process_chunk_adaptively` (`~line 940`) — now recursive (added
  2026-07-19). Check the interaction between `allow_early_split=True`
  (passed to every recursive level's own full-chunk attempt) and the
  near-miss bonus-retry logic in `process_chunk` above: does a
  recursively-split sub-part that hits a near-miss on its last attempt
  still get its own bonus retry, or does something about the recursion
  suppress it?

### 2. GPU lock and task-claiming — `app/core.py`

- `check_global_gpu_lock` (`~line 466`) / `claim_gpu_task` (`~line 492`):
  the docstring on `claim_gpu_task` already documents and fixes a real
  TOCTOU race between the two functions. Check every call site (grep
  `check_global_gpu_lock` across `routers/*.py`) for one that calls
  `check_global_gpu_lock` but does **not** follow up with `claim_gpu_task`
  before scheduling its background task — that call site has the exact race
  the comment describes, just not fixed there yet.
- `NON_GPU_TASKS = {"audacity_export", "m4b_export"}` / `GPU_TASKS =
  set(process_state.keys()) - NON_GPU_TASKS` (`~line 463-464`): `GPU_TASKS`
  is computed once, from whatever's in `process_state` **at import time**.
  If any task name gets registered in `process_state` after this line runs
  (conditionally, lazily, or dynamically), it silently never joins
  `GPU_TASKS` and never gets GPU-locked. Confirm every task name is
  statically present in `process_state` before this line executes, or flag
  it if not.

### 3. Bidirectional batch-review merging — `app/core.py`

- `_combine_pass_stats` (`~line 1033`) / `_combine_pass_totals`
  (`~line 1054`): sums two passes' stats except `books_done`, which is
  `max()`'d instead (the comment explains why: summing would double-count
  since both passes touch the same books). Check every *other* key summed
  by `_combine_pass_stats` for the same double-counting risk — is
  `books_done` the *only* key where forward and backward passes overlap, or
  are there other stat keys (in `_REVIEW_SUMMARY_PATTERNS`) that also
  shouldn't be simply added?
- The `"partial"` flag: set `True` if *either* pass's stats dict is falsy.
  Trace a caller that reads `combined["partial"]` — does it distinguish
  "one pass fully failed" from "one pass hasn't started yet" from "one pass
  was cancelled partway"? All three set the same input (`None`/falsy) to
  `_combine_pass_stats`, but may deserve different messaging.

### 4. Checkpoint/resume gapless-prefix invariant — `app/generate_script.py`

Rule 9 in `app/CLAUDE.md` names this explicitly: checkpoint/resume requires
every accepted chunk to be gapless. Find the checkpoint read/write functions
(grep `generation_checkpoint` / `build_generation_quality_manifest`) and
verify the invariant actually holds in every path that writes a checkpoint
— specifically: does a **resumed** run (one that starts partway through via
an existing checkpoint) re-verify the checkpoint's own gaplessness before
trusting it, or does it trust the file blindly? If a checkpoint file were
ever hand-edited, corrupted, or written by an older/different code version,
what happens on resume?

### 5. Speaker-identity consistency across generate vs. review

`app/speaker_identity.py`'s `stabilize_speaker_identities` runs during
**generation** (per-chunk, called from `generate_script.py`'s
`process_chunk`). `app/review_script.py`'s `--dedupe-speakers` runs
**separately**, later, over the whole already-generated script. Rule 15 in
`app/CLAUDE.md` documents a real past incident where two independently
computed answers to the same question ("is this remote?") drifted apart.
Check whether generation-time stabilization and review-time dedup use the
same similarity threshold, the same notion of "established speakers," and
the same alias-resolution logic — or whether they're two independently
maintained answers to "are these the same character?" that could disagree,
the same class of bug Rule 15 already names once.

### 6. Batch script collision-policy edge cases — `app/routers/script.py`

`generate_script_batch_start`'s `collision_policy` handling (`~line
1073-1128`, and see `_get_versioned_script_path`, `~line 958`): for
`"version"`, a `while output_path in reserved_outputs` loop finds a free
`_N` suffix. Check what happens when two *different* input filenames in the
same batch normalize (via `secure_filename`) to the same `safe_stem` — does
the `reserved_outputs` set correctly prevent them from colliding into the
same output file, or could one silently overwrite the other's in-progress
output mid-batch?

## Deliverable

A written report (markdown, not code) covering, for each area above:
1. **What the code actually does** (confirm/correct the summary above by
   reading the real function — these summaries are accurate as of
   2026-07-19 but may drift).
2. **A concrete failure scenario**, if you found one: exact inputs/state/
   ordering that produces a wrong result. If you traced an area and it's
   correct, say so explicitly — a clean bill of health is a useful result,
   not a non-result.
3. **Severity and confidence**: would this actually happen in production
   given this app's real usage pattern (mostly one user, sequential/batched
   book processing), or is it a real bug that's just very unlikely to
   trigger? Say which.
4. **A recommendation**: worth fixing now, worth fixing only if the
   triggering scenario is confirmed to have happened (check logs/
   `generation_quality.json` history the way this session did for the
   chunk-67 and front-matter incidents), or not worth changing.

Do not write or modify any code as part of this task — report findings
only. A follow-up implementation task, planned and approved separately (per
Rule 14 in `app/CLAUDE.md`), is the right next step for anything that turns
out to be worth fixing.
