# Logic review report (Fable, 2026-07-19)

Response to `FABLE_LOGIC_REVIEW.md`. Every function below was read directly
this session; line references are from today's main.

**Verdict summary:**

| Area | Verdict |
|---|---|
| 1. Retry/quality-gate flow | **Clean**, with three design notes (no bugs) |
| 2. GPU lock / task claiming | Statics clean; **one real unclaimed window** (lora checkpoint swaps) — low severity |
| 3. Bidirectional merge stats | **Clean** — `books_done` really is the only per-book key |
| 4. Checkpoint gapless invariant | **Clean** — resume re-verifies; final gate backstops content |
| 5. Speaker-identity consistency | **Finding (Rule 15)** — three independently-maintained label resolvers, two normalization schemes |
| 6. Batch collision policy | **Finding** — `replace` + duplicate-normalized stems silently overwrites the batch's own earlier output |

---

## Area 1 — Retry/quality-gate control flow (`app/generate_script.py`)

**What it does (confirmed):** `get_quality_retry_policy` returns
`increase_tokens` only when the response hit the token limit (or was
incomplete *and* within 90% of it); everything else retries on the same
budget. `is_severe_chunk_truncation` requires ratio < 0.6 plus both recall
codes. `get_chunk_retry_action` splits only after `consecutive_severe >= 2`
with `allow_early_split`. `process_chunk` runs `call(max_retries)` then, if
the *last* attempt was a near-miss (recall ≥ 0.75), one `call(0)` bonus.

**Brief's specific questions, answered:**
- *Does the `call(0)` bonus get retries if it fails?* No — `max_retries=0`
  inside `call_llm_for_entries` means one attempt; a quality failure hits
  `attempt < max_retries` = false and returns `[]`. That is the designed
  meaning of "one bonus retry", not an off-by-one. Two deliberate
  consequences worth knowing: the bonus attempt (a) carries **no retry
  feedback** (fresh `call_llm_for_entries`, `retry_feedback=None`) —
  consistent with the incident diagnosis that fresh samples succeeded 3/3
  where feedback-laden retries looped — and (b) passes `retry_decider=None`
  (the `allow_early_split and retries` guard is false at `retries=0`), so a
  bonus attempt can never trigger a split. Both look intentional; neither
  is documented in the comment. Not a bug.
- *Does recursion suppress the bonus?* No. Every recursion level of
  `process_chunk_adaptively` calls `process_chunk(allow_early_split=True)`,
  and the bonus logic lives inside `process_chunk` — each sub-part's own
  full attempt gets its own near-miss bonus. Also verified the interaction
  can't misfire: a near-miss attempt (recall ≥ 0.75 ⇒ ratio well above 0.6)
  can never simultaneously count as severe, so it *resets*
  `consecutive_severe` rather than advancing toward a split.
- *Is 0.75 the right cutoff?* It cleanly bisects the gap between the severe
  band (< 0.6 ratio) and the pass bar (0.90), so it's principled, not just
  incident-fit. There is no evidence either way for tightening it; the
  attempts telemetry now saved in `generation_quality.json` manifests
  (`attempts` per chunk) is exactly the data to revisit it with after a few
  more batch runs. Leave until then.

**One cosmetic note:** `get_quality_retry_policy` ends with
`if incomplete: return "retry_same_budget"` followed by
`return "retry_same_budget"` — a dead duplicate branch. Harmless; only
worth folding if the function is touched anyway (Rule 3).

## Area 2 — GPU lock and task claiming (`app/core.py`)

**Statics: clean.** `process_state` is one literal dict (lines 435-458)
fully populated before `GPU_TASKS` is computed at line 464; grep found no
later insertion of a new task key anywhere. Every task name is in
`GPU_TASKS` or deliberately in `NON_GPU_TASKS`.

**Call-site audit:** every background-task route pairs
`check_global_gpu_lock` (early, before validation) with `claim_gpu_task`
(immediately before `background_tasks.add_task`) — verified in
`routers/script.py` (script/review/batch_review/batch_script/nicknames),
`editor.py` (audio ×2), `dataset_builder.py`, `voicelab.py`, `preparer.py`
(both), `lora.py:638→717`, and `voice_design.py`/`system.py` (claim-only,
which is fine — claim includes the check).

**Finding — the three lora checkpoint-swap routes.** `lora_promote_candidate`
(`lora.py:883`), `lora_rollback_promotion` (`:895`), and
`lora_recover_checkpoint_swap` (`:907`) call `check_global_gpu_lock`
**only**, then perform synchronous checkpoint-file swaps via
`asyncio.to_thread` without ever claiming. Concrete scenario: promote's
check passes (nothing running) → while the file swap is mid-flight, a
`POST /api/lora/train` claims `lora_training` and the trainer starts
loading the very checkpoint files promote is moving → partially-swapped
adapter read by the trainer, or the swap journal machinery fires for a
non-crash. Severity: **low** (single user, swaps take well under a second,
training start is human-initiated), but it is precisely the TOCTOU pattern
`claim_gpu_task`'s docstring names, unfixed at exactly three call sites.
Cheap fix: `claim_gpu_task("lora_training")` + `try/finally` reset around
the swap (these are sync routes, so release is straightforward).
**Worth fixing now** — small, mechanical, closes the documented race class.

## Area 3 — Bidirectional batch-review merging (`app/core.py`)

**Clean.** Checked every key `_combine_pass_stats` sums
(`_REVIEW_SUMMARY_PATTERNS`: text/speaker/instruct/entries changed,
added/removed, narrators/speakers merged, batches failed/skipped,
total_changes): all are per-pass *event counts* — a forward-pass edit and a
backward-pass edit are distinct events even on the same book, so summing is
the correct semantics. `books_done` is genuinely the only per-book count,
exactly as the `_combine_pass_totals` comment claims. (`entries_before/
after` are set outside the pattern dict and never summed — also correct.)

The `"partial"` flag: consumed at `app/static/js/app-core.js:1423` as
`'(partial — not every pass completed)'`. The three falsy causes (crashed /
not-yet-run / cancelled) do share that one message, but the scan-time
consumer in `routers/script.py:835` already prevents the worst version of
this (single-pass runs never combine a never-populated `stats_bwd`), and
mid-run "partial" on a book whose backward pass hasn't arrived yet is
*accurate* — only one pass has contributed. Distinguishing the three causes
would add state for a tooltip nuance nobody has asked for. **Not worth
changing.**

## Area 4 — Checkpoint/resume gapless-prefix invariant

**Clean — resume does not trust the file blindly.**
`load_generation_checkpoint` (`generate_script.py:108`) rejects the whole
checkpoint (returns `[]`, forcing a fresh run) unless: fingerprint matches
exactly (source hash, settings hash, per-chunk hashes), the accepted list
is no longer than the chunk list, and **every** item positionally matches
its chunk's sha256 and has `quality.passed`. Positional verification from
index 0 makes the prefix gapless by construction. The writer holds the
invariant too: the main loop is sequential, appends+saves only after a
chunk passes validation, and `sys.exit(1)` on the first failure — no path
appends past a gap. Hand-edited/corrupt/old-version files fail
`safe_load_json` or the fingerprint match and are discarded.

One nuance, not a gap: the loader verifies each item's *source chunk* hash,
not its `entries` content — a hand-edited checkpoint with tampered entries
under a valid wrapper would resume. But the final whole-book gate
(`validate_chunk_quality(book_content, all_entries)` + `audit_script` +
`passes_final_generation_gate`) re-validates everything from scratch before
the output is marked `verified`, so tampered entries can't reach a verified
manifest. Defense in depth is already there. **No change.**

## Area 5 — Speaker-identity consistency (generation vs. review)

**Finding — the Rule 15 pattern, three ways.** The question "which real
speaker label does this proposed name refer to?" is answered by three
independently-maintained implementations with **two different
normalizations**:

1. `speaker_identity._identity_key` (generation): casefold + **strip all
   non-word characters** — "MR. SMITH", "Mr Smith", and "MR SMITH" all
   resolve to the same label.
2. `review_script.dedupe_speakers._resolve_label` (review): `.strip().lower()`
   only — punctuation/spacing variants do **not** resolve.
3. `find_nicknames._parse_alias_response`'s `label_by_norm` (nicknames):
   `.strip().lower()` only — same as 2.

Concrete divergence: a script contains the label `MR. SMITH`. Generation
happily canonicalizes a model-emitted `MR SMITH` onto it. The alias
registry (or the dedupe LLM) later proposes `"MR SMITH" -> "JOHN SMITH"` —
review's `_resolve_label("MR SMITH")` misses `MR. SMITH`, and the forced
merge is **silently dropped** (`if not actual … continue`), so a
known-confirmed alias from the registry doesn't get applied to exactly the
book that needs it. Same silent drop in find_nicknames (partially
mitigated since PR #196 at least *logs* near-misses there — but the other
two sites don't even log). Severity: moderate-low — punctuated/spaced
speaker labels genuinely occur in this corpus ("MAN 2 (VILLAIN)", initials,
honorifics), and the failure is silent by design. **Worth fixing now**: one
shared resolver (in `speaker_identity.py`, keyed on `_identity_key`) used
by all three call sites, exactly the consolidation Rule 15 prescribes.
Guard behavior (NARRATOR skip, `_is_group_label`) is already shared or
trivially string-equal — the resolver is the real drift point.

## Area 6 — Batch collision policy (`app/routers/script.py`)

**Finding — `replace` can overwrite the batch's own output.** Trace of
`generate_script_batch_start` (~line 1104-1125) with two inputs whose
names normalize to the same `safe_stem` (e.g. `My Book.epub` and
`My_Book.txt` → both `My_Book`), `collision_policy="replace"`, and no
pre-existing `My_Book.json`:

1. Task 1: no disk file, not reserved → output `My_Book.json`, reserved.
2. Task 2: `output_path in reserved_outputs` is true → enters the
   collision branch → policy is `replace` → the replace arm is
   `elif os.path.exists(output_path):` — **false** (task 1 hasn't run
   yet) → no backup, no rename, falls through → task 2 gets the **same**
   `output_path` as task 1.
3. Jobs run sequentially: task 1 generates for potentially hours, then
   task 2 **silently overwrites** `My_Book.json` (and its
   `.voice_config.json` sibling) with a different book's script. No log
   line distinguishes this from a normal run.

`cancel` handles this case correctly (skips task 2 with a message);
`version` handles it correctly (the `while output_path in
reserved_outputs` loop suffixes `_2`). Only `replace` conflates "replace
what was on disk before the batch" with "replace what this same batch is
about to produce". Severity: real but **low likelihood** (requires two
same-stem inputs *and* explicit replace policy) — however the cost when it
hits is a whole generated book, silently. **Worth fixing now**: in the
`replace` arm, when the collision is with a *reserved* (in-batch) output
rather than a pre-existing file, version-suffix it (or skip with a log
line) instead of falling through — replacing a file the same batch is
generating is never what "replace" meant.

---

## Recommendations ranked

1. **Area 6 — fix now** (small): in-batch reservation collisions under
   `replace` must version or skip, never share a path. + regression test.
2. **Area 5 — fix now** (small-medium): single shared
   `resolve_speaker_label` built on `_identity_key`, used by
   `review_script.dedupe_speakers` and `find_nicknames._parse_alias_response`.
   + tests for punctuation/spacing variants.
3. **Area 2 — fix now** (small, mechanical): `claim_gpu_task` +
   `try/finally` release around the three lora checkpoint-swap routes.
4. **Area 1 cosmetic** — fold the dead branch only if the function is
   touched anyway; document the two undocumented bonus-attempt properties
   (no feedback, no split) in its comment at the same time.
5. **Areas 3, 4 — no change.** Both are clean; the conservative structure
   is load-bearing (Rule 9) and correct.
