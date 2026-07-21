# Three-Pass Review Fixes — Implementation Plan

> **For agentic workers:** implement task-by-task, TDD where a behavior change is testable. All commands run from `app/` via `env/bin/python`. Repo root `/home/fakemitch/pinokio/api/alexandria-audiobook2.git`, branch `agent/three-pass-generation`.

**Goal:** Fix the 15 code-review-20 findings against the freeze/recombination/context-rescue work, then re-run the full three-pass A/B on corrected code.

**Context:** Findings came from reviewing `b6ee477..HEAD`. Two are bugs introduced by my own fixes (pause_after drop, recombination-floor bypass).

**Execution order (updated per user):** Task 0 (stop the rough A/B) → **Task 9
FIRST (telemetry manifest)** so every subsequent fix and the clean re-run are
analyzable from structured data instead of log-grepping → then correctness
(Tasks 1-8) → efficiency (Task 10) → cleanup (Task 11) → re-run the A/B entirely
(Task 12). Tasks keep their numbers below; only the order changed. Task 9's
per-chunk resolution fields should be added as the fixes land (e.g. once the
recombination floor is restored and context-bleed guard exists, the manifest
records those outcomes too).

---

## Task 0: Stop the current (throwaway) A/B

The running A/B (tmux `tpab`) is on buggy code and its output won't be trusted. Free the GPU before editing.

- [ ] `tmux kill-session -t tpab; pkill -f three_pass_generate; lms unload --all`
- [ ] Confirm no `three_pass_generate` process running.
- [ ] Wipe stale outputs so the re-run is clean: `rm -rf ab_test_threepass/gemma ab_test_threepass/qwen27b ab_test_threepass/qwen9b ab_test_threepass/run.log` (keep `prove_ctx153.py` / `run_three_pass_ab.sh`).

---

## CORRECTNESS FIXES

## Task 1: Restore the trigram floor on recombination-accept (finding #2)

**File:** `app/three_pass_generate.py` (segment_chunk_adaptively recombination branch, ~line 174-193)

The change to `codes == {"low_ordered_trigram_recall"}` waives the 0.82 floor (accepts any trigram value). It was based on a seam theory later disproved; real seam cases were ~0.83 (above floor). Restore floor-gating.

- [ ] Test (append to `test_three_pass_generate.py`): a recombination whose combined validates trigram-only at **0.60** (both halves echoed but heavily reordered) must NOT be accepted (returns [] when no outer near-miss); at **0.85** it IS accepted. (Construct via a mock where each half passes but the combined reorders enough to drop whole-chunk trigram below the floor — or unit-test the predicate directly.)
- [ ] Change the branch back to use the floor: replace `if codes == {"low_ordered_trigram_recall"}:` with `if is_trigram_only_near_miss(combined_quality):` (which enforces `[ACCEPT_TRIGRAM_NEAR_MISS_FLOOR, 0.90)`). Keep the diagnostic REJECTED log for the else path. This also un-deadens the `is_trigram_only_near_miss` import (finding #13).
- [ ] Run `test_three_pass_generate` + the recombination test; commit.

## Task 2: Preserve extra fields (pause_after) in freeze-reconstruction (finding #1)

**File:** `app/three_pass_generate.py` (attribute_batch ~line 86, instruct_batch ~line 114)

Reconstruction rebuilds `{speaker,text}` / `{speaker,text,instruct}` only, dropping `pause_after` (and any other field) that `build_deterministic_repair` set in pass 1. TTS reads `pause_after`.

- [ ] Test: a frozen segment entry carrying `pause_after=1000` survives pass 2 (attribute) and pass 3 (instruct) reconstruction with `pause_after` intact and `type` removed.
- [ ] attribute_batch: change reconstruction to preserve non-`type` fields:
  ```python
  return [{**{k: v for k, v in f.items() if k != "type"}, "speaker": n.get("speaker")}
          for f, n in zip(frozen_batch, named)]
  ```
- [ ] instruct_batch: preserve all prior fields, add instruct:
  ```python
  return [{**p, "instruct": a.get("instruct")} for p, a in zip(prior_batch, annotated)]
  ```
- [ ] Also fix the pass-2 fallback path to carry pause_after through (it seeds `{speaker,text}` from frozen — spread the frozen entry minus type).
- [ ] Run tests; commit.

## Task 3: Match unsupported_unicode_character finding shape (finding #6)

**File:** `app/pass_quality.py` (`_introduced_character_findings`, ~line 38)

pass_quality emits `characters` as strings; chunk_quality emits dicts `{character, codepoint, name}`. Match chunk_quality so consumers don't KeyError.

- [ ] Test: a segment output introducing a non-source non-ASCII letter yields a finding whose `characters[0]` is a dict with keys `character`, `codepoint`, `name`.
- [ ] Change the list comprehension to build the same dict shape as `chunk_quality.py:92-94`.
- [ ] Run tests; commit.

## Task 4: Guard context-rescue against context bleed (finding #3)

**File:** `app/three_pass_generate.py` (`segment_chunk_with_context` / `rescue_chunk_with_context`)

The rescue validates only target recall/trigram/ratio; a leaked context sentence can pass. Add a cheap post-check: reject a rescue result whose entries contain text that appears in the context window but NOT in the target chunk.

- [ ] Test: a mock rescue response that appends a context-only sentence to the correct target segmentation is rejected (not returned), even though target recall/ratio pass.
- [ ] Add a helper `_output_has_context_bleed(entries, chunk, before, after)`: for each entry, if `normalize_text(entry_text)` is a substring of `normalize_text(before+after)` and NOT of `normalize_text(chunk)`, flag bleed. Wire it into `segment_chunk_with_context`'s acceptance (treat bleed as a validation failure so it retries/falls through).
- [ ] Run tests; commit. (Keep it conservative — only reject clear context-only entries, to avoid false rejections on short generic lines.)

## Task 5: Cap rescue context windows by remaining token budget (finding #4)

**File:** `app/three_pass_generate.py` (`rescue_chunk_with_context`)

With `context_length` now set, a 6000+6000-char window can bust the budget at 8192 (qwen-27b), failing the most-helpful window.

- [ ] Test: with a small `params.context_length` (e.g. 8192) and a large chunk, `rescue_chunk_with_context` does not attempt a window whose prompt would exceed the budget (skips/shrinks it) rather than erroring.
- [ ] Before each window, estimate prompt tokens (reuse the pipeline's chars//3 estimate + system prompt) and skip or shrink windows that leave < a minimum output budget for `params.context_length`. When context_length is None (unknown), keep current behavior.
- [ ] Run tests; commit.

## Task 6: Anchor freeze by index to stop identical-text speaker swap (finding #5)

**File:** `app/pass_quality.py` (freeze_check / validate_attribution), `app/three_pass_generate.py` (batch prompts + reconstruction)

Two entries with identical normalized text can be reordered without freeze_check noticing, mis-binding speaker/instruct. Add a per-entry index the model must echo.

- [ ] Test: attribute batch where the LLM swaps two identical-normalized-text entries is rejected (or corrected by index), not silently mis-attributed.
- [ ] Add an `"n"` (index) field to each entry sent in the attribute/instruct batch JSON; require the response to echo `n`; reconstruct by matching `n` rather than positional zip. `freeze_check` verifies each `n` maps to the frozen entry with matching normalized text. If the model drops/reorders indices, fail the batch (retry).
- [ ] Run tests; commit. (This is the meatier one — if it proves fragile, fall back to rejecting batches that contain duplicate-normalized-text entries in the same batch and handle them singly.)

## Task 7: Make deterministic repair type-safe on segment shape (finding #7)

**File:** `app/three_pass_generate.py` (segment_chunk transform) or `app/script_repair.py`

`build_deterministic_repair`'s empty-entry merge ignores `type`, can merge across a NARRATOR/SPOKEN boundary in segment output.

- [ ] Test: a segment list with an empty-text entry between a NARRATOR and a SPOKEN entry is not merged across the type boundary.
- [ ] Simplest: for the segment pass, wrap the transform to skip the empty-entry-merge step (segment entries with empty text are already caught by `empty_text` finding), OR pass a flag so repair only merges same-type neighbors. Prefer not modifying script_repair's single-pass behavior — gate the segment-specific behavior in three_pass.
- [ ] Run tests; commit.

## Task 8: Validate config chunk_size + roster drift (findings #14, #15)

**File:** `app/three_pass_generate.py`

- [ ] chunk_size: after resolving `chunk_size` (CLI or config), validate `>= 1` and error if not (move the check below the config read so it covers both sources).
- [ ] roster: after the pass-2 fallback path returns stabilized entries, recompute the running roster from `named` (or normalize the fallback's speakers the same way) so `roster_seen` can't drift. Simplest safe fix: keep the incremental roster but rebuild it via `build_roster(named)` whenever a fallback batch was used.
- [ ] Test both; commit.

---

## OBSERVABILITY

## Task 9: Three-pass run telemetry / manifest (finding #8 — the queued logging task)

**File:** `app/three_pass_generate.py`

Persist a manifest next to the output (like `generate_script`'s `*.generation_quality.json`) recording per-chunk pass-1 resolution and rescue/near-miss counts, so results aren't hand-grepped. See memory `queued_three_pass_logging`.

- [ ] Track per source-chunk: resolution = clean / adaptive_split / near_miss(trigram) / context_rescue(window) / (fail). Track counts: near_miss_accepted, context_rescued, split_recombined.
- [ ] Record per-pass (segment/attribute/instruct) elapsed + final status.
- [ ] Write `<output>.threepass_manifest.json` on completion (and on failure, with the failing chunk).
- [ ] Test: a run that used a near-miss + a context-rescue records both in the manifest with correct counts.
- [ ] Commit.

---

## EFFICIENCY

## Task 10: Slice rescue context from offsets, not whole-book join (finding #9)

**File:** `app/three_pass_generate.py` (`rescue_chunk_with_context`)

`"".join(chunks[:index])` / `"".join(chunks[index+1:])` materialize the whole book to slice a <=6000-char window.

- [ ] Only join enough chunks to cover the largest window: walk backward from `index` accumulating chunks until >= max(_CONTEXT_RESCUE_WINDOWS) chars, same forward. Slice from that bounded string.
- [ ] Test: rescue produces the same before/after context text as the naive join for a small book (behavior unchanged, allocation bounded).
- [ ] Commit.

---

## CLEANUP / ALTITUDE

## Task 11: Share a segment-call helper + config the windows (findings #10, #11, #12)

**File:** `app/three_pass_generate.py`

- [ ] Extract the common `segment_chunk` / `segment_chunk_with_context` body (load prompt -> params override incl. **user_prompt_template** -> call_llm_for_entries with the repair transform + segment validator + near_miss_sink) into one helper parameterized by the built user prompt. This fixes the forked `user_prompt_template` override (finding #11) structurally.
- [ ] Move `_CONTEXT_RESCUE_WINDOWS` (and rescue max_retries) into generation config with a documented default, so they're tunable (finding #12).
- [ ] Note (do NOT fully merge now): the three fallback mechanisms (finding #10) stay separate but must share the near-miss floor + logging via the helper; a full unification is a follow-up.
- [ ] Run full suite; commit.

---

## Task 12: Full suite + inventory + re-run the A/B ENTIRELY

- [ ] `cd app && env/bin/python -m unittest discover -b -p "test_*.py"` — regen `unit_test_inventory.json`, confirm green.
- [ ] **Re-run the full three-pass A/B** on corrected code: `tmux new-session -d -s tpab "bash ab_test/run_three_pass_ab.sh"` (gemma with chunk-153 context-rescue + retries, then qwen-27b@8192, then qwen-9b@32768), fresh output dirs.
- [ ] Watch: gemma completes the full 3 passes; the new manifest shows how each hard chunk resolved; qwen arms' completion.

---

## Notes
- Tasks 1, 2, 3 are the must-fix correctness bugs (two are mine) — do first; small.
- Task 6 (index anchoring) is the riskiest; if it destabilizes, use the duplicate-in-batch fallback.
- Task 9 is the user's already-queued logging ask.
- After this lands and the A/B validates, open the three-pass PR (main now has the calibration deps via #208).
