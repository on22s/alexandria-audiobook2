# Resume-from-checkpoint for Generate Script + Review

**Date:** 2026-06-28
**Status:** Design approved, pending spec review
**Scope:** Generate Script (single + batch) and Review (single + batch)

## Problem

A power outage (or crash/kill) mid-run loses work that should be recoverable:

| Area | Today | Gap on power outage |
|---|---|---|
| **Generate Script** (`generate_script.py`) | Accumulates `all_entries` in memory, single `atomic_json_write` at the very end (app/generate_script.py:539) | **Everything** lost — no on-disk progress at all |
| **Generate Script batch** (`generate_script_batch_start`) | Runs `generate_script.py` per file; no batch state on disk | Which files were already done is lost |
| **Review single** (`review_script.py`) | Per-batch checkpoint exists (`save_checkpoint`/`_load_resume_state`), auto-resumes silently | Capability is fine, but **the user has no choice** to resume vs. start fresh |
| **Review batch** (`review_script_batch_start`) | Per-book `review_checkpoint.json` survives on disk, but orchestration (`bidirectional`, `current_pass`, book order + per-book status) lives only in `process_state["batch_review"]` (in memory) | Restart loses the **plan**: which pass (forward-only vs front-to-back), which pass is done, which books remain and in what order |

## Goals / success criteria

1. Generate Script (single) writes progress to disk after every chunk; a re-run can continue from the last completed chunk with cross-chunk context preserved, producing output identical to an uninterrupted run.
2. All four areas present an **explicit "⚠ Resume / Start fresh" choice** before starting when an unfinished run is detected (not silent auto-resume, not informational-only).
3. Review's resume is **pass-aware**: it knows whether the run was forward-only or front-to-back (bidirectional), which pass is done, and which books remain in what order.
4. "Start fresh" reliably clears all relevant checkpoint state so no stale progress leaks into the new run.
5. No existing safety net (atomic writes, the "preserve forward checkpoint during backward pass" logic, VRAM-abort handling) is weakened.

## Non-goals

- No change to the actual annotation/review LLM logic or prompts.
- No new storage paradigm — flat JSON checkpoint files only (Rule 13).
- No automatic/silent resume — the user always chooses.

## Architecture — one consistent contract (Rule 15)

Every resumable task uses the same three parts so the four implementations cannot drift:

1. **Checkpoint writer** — writes progress to disk incrementally with `atomic_json_write`.
2. **Detect endpoint** — read-only (`get_`-style, Rule 16), returns a uniform descriptor:
   `{ exists: bool, done: int, total: int, label: str, ...mode }`.
3. **Explicit UI prompt** — frontend calls detect before starting; if `exists`, shows
   **⚠ Resume / Start fresh / Cancel**. The start route takes a `resume` flag:
   `resume=true` continues; `resume=false` clears checkpoint state first.

### Detect descriptor (uniform shape)

```json
{
  "exists": true,
  "done": 40,
  "total": 50,
  "label": "40/50 chunks",
  "mode": { "bidirectional": true, "current_pass": "bwd", "books": [ ... ] }
}
```

`mode` is optional/empty for the script cases; populated for review batch.

## Component design

### 1. `generate_script.py` — per-chunk checkpoint (single)

Mirror `review_script.py`'s checkpoint helpers (local to the file; it is run as a
subprocess and already imports `atomic_json_write` from `utils`):

- `_script_checkpoint_path(output_path)` → `<output_path>.script_checkpoint.json`
- `save_script_checkpoint(output_path, completed_chunks, total_chunks, chunk_size, input_hash, all_entries)`
- `load_script_checkpoint(output_path, total_chunks, chunk_size, input_hash)` →
  returns the dict only if `total_chunks`, `chunk_size`, and `input_hash` all match
  (a mismatch means the split would differ → ignore + start fresh, same guard style
  as review's `batch_size`/`context_window` check). Returns `None` otherwise.
- `clear_script_checkpoint(output_path)`

Checkpoint contents:

```json
{
  "completed_chunks": 40,
  "total_chunks": 50,
  "chunk_size": 3000,
  "input_hash": "<sha256 of mojibake-fixed book_content>",
  "all_entries": [ ... ]
}
```

`main()` changes:
- Add CLI flag `--resume` (store_true).
- Compute `input_hash` from the mojibake-fixed `book_content` after splitting.
- If `--resume`: attempt `load_script_checkpoint(...)`; on hit, restore `all_entries`
  and set the resume offset to `completed_chunks`. On miss/mismatch: print a note and
  start fresh.
- If not `--resume`: `clear_script_checkpoint(output_path)` then start clean.
- In the chunk loop: skip chunks `i <= completed_chunks` (cross-chunk context is
  preserved because `all_entries` was restored and `previous = all_entries`). After each
  `all_entries.extend(entries)`, call `save_script_checkpoint(...)`.
- After the final `atomic_json_write(all_entries, output_path)` succeeds →
  `clear_script_checkpoint(output_path)`.

### 2. Generate Script batch — `app.py`

- New batch state file: `scripts/.batch_script_state.json`:
  `{ "files": [{"name": str, "status": "pending|done|failed"}], "current_idx": int, "params": {...} }`,
  updated atomically as each file finishes.
- Pass `--resume` to each `generate_script.py` subprocess so the in-progress file resumes
  from its own `.script_checkpoint.json`; the batch file records which files are already `done`.
- Detect endpoint: `GET /api/generate_script/batch/checkpoint` → uniform descriptor.
- Start route gains a `resume` flag. `resume=false` (fresh) deletes the batch state file and
  every per-file `.script_checkpoint.json` for the listed files before starting.
- Clean finish → delete the batch state file.

### 3. Review single — `app.py`

The writer already exists in `review_script.py`. Changes are app-side only:
- Add a `resume` flag to the single-review start route(s) (`/api/review_script`,
  `/api/review_script_contextual`). `resume=true` leaves the checkpoint in place
  (review_script.py auto-resumes via `_load_resume_state`); `resume=false` calls
  `clear_checkpoint(SCRIPT_PATH)` before launching.
- Detect endpoint reports the active book's checkpoint (done/total batches), reusing the
  data `_summarize_review_checkpoint` already produces.

### 4. Review batch — `app.py` (the pass-aware piece)

- Persist orchestration to `scripts/.batch_review_state.json`:
  ```json
  {
    "names": ["BookA", "BookB", "..."],
    "bidirectional": true,
    "window": 8,
    "dedupe": true,
    "discover": true,
    "current_pass": "fwd",
    "current_task_idx": 3,
    "tasks": [{"name": "BookA", "status": "done"}, ...]
  }
  ```
  Written in `_init_batch_state` and updated after each book status change and each pass
  transition (atomic write each time).
- Detect endpoint `GET /api/review_script/batch/checkpoint` → uniform descriptor with a
  populated `mode` (pass, books done, order).
- Start route gains a `resume` flag:
  - **Resume:** load the state, rebuild the plan — skip `done` books, resume `current_pass`
    from `current_task_idx`, then run any remaining pass(es). Within-book progress comes from
    the per-book `review_checkpoint.json` that already survives. The existing "preserve forward
    checkpoint during backward pass" / VRAM-incomplete logic (app/app.py:2432-2447) is untouched.
  - **Fresh:** delete the batch state file and every per-book `review_checkpoint.json` for the
    listed scripts, then start clean.
- Clean finish → delete the batch state file.

### Cross-cutting frontend — `app/static/index.html`

- One helper `confirmResumeOrFresh(detectUrl)` → resolves to `'resume' | 'fresh' | null`
  (null = user cancelled). Renders the **⚠ Resume / Start fresh / Cancel** prompt. Used by all
  four start handlers so the UX cannot drift (Rule 15). Vanilla JS, braces on all blocks (Rule 18).
- Wire it into the existing start handlers for: generate script, batch generate, review,
  batch review. Each calls detect first; if `exists`, prompt, then call the start route with the
  chosen `resume` value (or abort on Cancel).
- Keep the existing Review checkpoints informational list (`loadCheckpoints`), now backed by the
  same detect data.

## Error handling / edge cases

- Checkpoint write failure (disk full, permission) → log a warning and continue, exactly as
  `review_script.py`'s `save_checkpoint` already does. The run is not aborted.
- Input changed between runs (different `input_hash`, `chunk_size`, or `total_chunks`) → ignore the
  checkpoint and start fresh, with a printed note.
- "Start fresh" must delete *all* relevant checkpoint state, including per-item checkpoints for the
  batch cases — verified by test, not assumed.
- A detect endpoint must never resume or mutate state (Rule 16: `get_`/detect = pure read).

## Testing (Rule 12 — verify behavior, not return values)

- **Script checkpoint roundtrip:** save → load → resume returns the stored state; a changed
  `input_hash`/`chunk_size`/`total_chunks` yields `None` (forces fresh).
- **Interrupt-and-resume equivalence:** write a checkpoint at chunk N, re-run with `--resume`, assert
  only chunks N+1.. are processed and the final output equals an uninterrupted full run.
- **Batch review reconstruction:** given a state of "forward pass done, backward partial at book k",
  the rebuilt plan skips `done` books and resumes the backward pass at the correct book and order.
- **Start-fresh clears state:** fresh start deletes the batch state file and every per-item
  checkpoint for the listed items (assert the files are gone).

## Files touched

- `app/generate_script.py` — checkpoint helpers + `--resume` + per-chunk save/clear.
- `app/app.py` — batch state files, detect endpoints, `resume` flags on start routes,
  fresh-clear logic.
- `app/static/index.html` — `confirmResumeOrFresh` helper + wiring into four start handlers.
- `.gitignore` — ignore `*.script_checkpoint.json`, `scripts/.batch_script_state.json`,
  `scripts/.batch_review_state.json` (generated at run time, Rule 13/gitignore best practice).
- Tests — new test module(s) for checkpoint roundtrip, equivalence, and batch reconstruction.
