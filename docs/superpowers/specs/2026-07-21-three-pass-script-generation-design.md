# Three-Pass Script Generation — Design

**Date:** 2026-07-21
**Status:** Approved design, pending implementation plan
**Related:** near-miss/0.82-floor calibration (PR #207, branch `agent/fable-attempt-calibration`); chunk-size A/B (`ab_test_cs3000/`)

## Problem

Single-pass script generation asks the model to do ~7 things in one LLM call
over a 6000-char chunk: segment narrator-vs-dialogue, attribute speakers,
extract actions from attribution tags, **reproduce the author's text
near-verbatim** (the trigram gate at 0.90), TTS-normalize, invent a 1–2 sentence
emotional `instruct` per line, and emit valid JSON. On hard chunks (dense
stylized dialogue with intentional misspellings, e.g. Roswaal's "Iii"/"buuut" or
rapid `Subaru "Gu… Gul…"` exchanges) the model collapses or truncates partway,
failing the whole chunk — and because completion needs a gapless accepted
prefix, one unrecoverable chunk fails the entire book.

Two mitigations already shipped (exhaustion-only trigram near-miss acceptance,
0.82 floor) and a chunk-size A/B is running, but the failures are fundamentally
"too much cognitive load per call." This design reduces per-call load by
splitting the work into three focused passes.

## Goals

- **A (primary): fewer book failures / retry storms.** Make the fidelity-critical
  pass lighter so it collapses less on hard chunks.
- **C (co-primary): output quality.** Let attribution and `instruct` generation
  specialize with wider context.
- Speed is secondary (acceptable to trade for A and C).

## Verified assumptions (grounded in current code)

1. **The trigram gate judges only `text`.** `chunk_quality.validate_chunk_quality`
   computes recall/ordered-trigram metrics purely from `entry["text"]`; `speaker`
   and `instruct` are checked only for *presence* (`missing_fields`), never
   fidelity. So the gate that fails books is a text-reproduction gate — the
   `instruct` and naming work can be moved out of it without weakening it.
2. **Attribution and repair are already standalone modules.**
   `stabilize_speaker_identities` (`speaker_identity.py`) and
   `build_deterministic_repair` (`script_repair.py`) are reusable directly.
3. **A text-preservation guard already exists, softly.** `review_script.py`'s
   `check_text_loss` (+`normalize_text`) rejects a review batch whose text
   drifts below a 0.95 ratio. The new passes tighten this into a hard per-entry
   freeze (below).

## Architecture

A new **`three_pass` generation mode**, selectable and side-by-side; the
single-pass path stays the default and untouched. Data flows pass 1 → 2 → 3 and
the final output is the **identical `[{speaker, text, instruct}]` shape**, so
review, resume-assembly, and TTS downstream need zero changes.

### Pass 1 — Segment (fidelity-critical)
Source chunk → `[{type: "NARRATOR"|"SPOKEN", text}]`. Segments narrator vs
spoken, reproduces verbatim, TTS-normalizes, emits JSON. **No speaker names, no
`instruct`** — a stripped-down system prompt dropping the naming and instruct
rule-blocks. Chunked by source (keeps the `--chunk-size` knob). Reuses the
existing **trigram gate + near-miss/0.82-floor + adaptive split + per-chunk
checkpoint** unchanged, judging `text` reproduction. Segmentation needs
quote-tracking, not names, so pass 1 genuinely sheds the character-roster /
consistent-naming burden.

### Pass 2 — Attribute (labeling)
`[{type, text}]` → assign a **name** to each `SPOKEN` span (`NARRATOR`
unchanged). Entry-batched wide (~25–50 entries, reusing the `review_batch`
pattern) so it sees a wide roster for consistent naming. Reuses
`stabilize_speaker_identities`. Assigns labels, does not reproduce text.

### Pass 3 — Instruct (creative)
`[{speaker, text}]` → add `instruct` to each. Entry-batched wide, carrying tone
across the batch for emotional-arc continuity.

### Keystone: hard per-entry text freeze
After pass 1, `text` is frozen. Passes 2 & 3 may **only add fields**
(`speaker`, then `instruct`); if a batch's output alters any `text` (compared
via `normalize_text`, per-entry equality — a tightening of `check_text_loss`)
or drops/adds entries, that batch is rejected. This quarantines *all* fidelity
risk in pass 1, so moving work out of pass 1 can only help completion.

## Error handling & gating

- **Pass 1 — same failure model as today.** Trigram gate + near-miss/0.82-floor
  + adaptive split + per-chunk checkpoint, unchanged. The only pass that fails a
  book in production, and now lighter.
- **Pass 2 — retry, then a configurable outcome.** Per entry-batch: LLM names →
  enforce the hard freeze → on drift/drop/add, reject and retry (bounded). On
  exhaustion, behavior is gated by `pass2_on_exhaustion`:
  - **`"fail"` (DEFAULT during testing):** hard-fail the book like pass 1 and
    record the failing batch, so the real pass-2 failure rate is visible instead
    of masked.
  - **`"fallback"` (deferred; see below):** keep frozen text, run
    `stabilize_speaker_identities`, label unresolved `SPOKEN` spans `UNKNOWN` for
    the review pass, and proceed. Never fails the book.
- **Pass 3 — retry then graceful default.** LLM adds `instruct` → text+speaker
  freeze → retry batch → on exhaustion, default instruct
  (`"Neutral, even narration."` for narrator, a generic character default
  otherwise). Never fails the book.
- **Checkpoint/resume — per pass.** Pass 1 per-chunk (existing shape); passes 2
  & 3 per entry-batch. A resumed run knows which pass and batch it is in. Final
  assembly runs only after pass 3 completes. Checkpoint fingerprint is distinct
  from single-pass.

During the testing phase, passes 1 and 2 can fail a book (visible signal); pass
3 cannot. Post-testing, only pass 1 can.

## Components

- **New `three_pass_generate.py`** — orchestrator + CLI mirroring
  `generate_script.py` (`input`, `--output`, `--chunk-size`) so the A/B runners
  invoke it identically. Reuses `split_into_chunks` + source prep,
  `call_llm_for_entries` (already accepts a pluggable `validate_entries` +
  `near_miss_sink`), `stabilize_speaker_identities`, `build_deterministic_repair`,
  and `check_text_loss`/`normalize_text`.
- **New prompts (3 files, existing `prompt_loader` pattern, config-overridable):**
  `default_prompts_segment.txt`, `_attribute.txt`, `_instruct.txt`.
- **New `pass_quality.py` validators:** `validate_segment_quality`
  (`{type, text}` presence + shared recall/trigram core), `validate_attribution`
  (hard text-freeze + every `SPOKEN` named + count preserved), `validate_instruct`
  (text+speaker freeze + non-empty `instruct`).
- **Targeted refactor (serves the goal):** extract the recall/trigram core
  (`_tokens`, `_ngrams`, `_counter_recall`) from `chunk_quality.py` into a shared
  helper so the segment validator uses the *exact same* fidelity scoring as the
  single-pass gate (Rule 15 — no duplicated decision logic). Existing
  `chunk_quality` tests must stay green afterward.
- **New checkpoint shape:** pass-and-batch-aware.
- **Unchanged:** `generate_script.py` default path, review, batch orchestration,
  TTS. The runner picks which generator script to call.

## Testing & acceptance

- **Unit** (`pass_quality.py`): segment (reuses trigram tests + `{type,text}`
  shape); attribution (freeze catches text drift, names required, count
  preserved, loud-fail fires in testing mode); instruct (freeze catches
  text/speaker drift, `instruct` required, default fires on exhaustion).
- **Regression guard:** existing `chunk_quality` tests green after the recall-core
  extraction.
- **Behavioral:** mock the LLM through all three passes → correct assembled
  shape; a pass-2/3 that alters `text` is rejected; pass-2 loud-fails in testing
  mode; pass-3 default-instruct fires.
- **Acceptance A/B (the real test):** run **10wn** (most problematic book)
  through `three_pass` vs single-pass at the same model/context; compare
  completion + storms + wall-clock via the existing `summarize` approach, and
  eyeball attribution/instruct quality for C.
- Regenerate `unit_test_inventory.json`. No FastAPI route change → no
  api_contract regen (CLI/experiment path only for now).

## Scope boundaries (YAGNI)

Out of scope for this build:
- **Multi-model per pass** — all three passes use the one loaded model (local
  VRAM is one-model-at-a-time; per-pass swapping would thrash). Future work.
- **Scene detection** — rejected; entry-batched wide instead.
- **UI/route wiring** — CLI/experiment path only; the app's Script tab keeps
  using single-pass until three-pass is proven.
- **Deleting single-pass** — a later "flip to replace" step, only after proof.

## Deferred changes (write into the plan as follow-ups)

- After the 3-pass is validated, flip `pass2_on_exhaustion` default `"fail"` →
  `"fallback"` (graceful degradation so a naming issue never aborts a book).
- If proven strictly better, consider promoting three-pass to the default and
  retiring the single-pass prompt path (scope option B).

## Sequencing note

Implementation can be written immediately, but the acceptance A/B needs the GPU,
which is currently running the chunk-size experiment (`ab_test_cs3000/`). Real
testing waits for that to free up.
