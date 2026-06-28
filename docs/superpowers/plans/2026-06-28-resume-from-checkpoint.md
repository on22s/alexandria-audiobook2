# Resume-from-checkpoint Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Generate Script (single + batch) and Review (single + batch) survive a power outage by checkpointing progress to disk and giving the user an explicit Resume / Start-fresh choice on the next run.

**Architecture:** One shared contract per task — a disk checkpoint writer, a read-only detect endpoint returning a uniform `{exists, done, total, label, mode}` descriptor, and a `resume` flag on the start route. `generate_script.py` gains per-chunk checkpointing (it currently writes only at the end). Review's per-batch checkpoint already exists; the new work there is persisting the *batch orchestration plan* (forward-only vs bidirectional, which pass, which books remain and in what order) so a restart rebuilds it. The frontend gets one `confirmResumeOrFresh()` helper wired into all four start buttons.

**Tech Stack:** Python 3 / FastAPI (`app/app.py`), plain-Python subprocess scripts (`app/generate_script.py`, `app/review_script.py`), vanilla-JS SPA (`app/static/index.html`), `utils.atomic_json_write` / `utils.safe_load_json` for storage. Tests are standalone runnable scripts (matching `app/test_api.py`), runnable with `python <file>` and pytest-collectable if pytest is present.

---

## File Structure

- `app/generate_script.py` — add script-checkpoint helpers + `--resume` flag + per-chunk save/clear in `main()`.
- `app/app.py` — request-model `resume` fields; four detect endpoints; `resume`/fresh wiring on the four start routes; two batch-state files + helpers; path constants.
- `app/static/index.html` — `confirmResumeOrFresh()` helper + wiring into the four start handlers.
- `app/test_checkpoint_resume.py` — NEW standalone test module for the script-checkpoint helpers and batch-state helpers.
- `.gitignore` — ignore the generated checkpoint/state files.

Path constants (add near the other `*_PATH` constants in `app/app.py`):
```python
BATCH_SCRIPT_STATE_PATH = os.path.join(SCRIPTS_DIR, ".batch_script_state.json")
BATCH_REVIEW_STATE_PATH = os.path.join(SCRIPTS_DIR, ".batch_review_state.json")
```

Uniform detect descriptor returned by every detect endpoint:
```json
{ "exists": true, "done": 40, "total": 50, "label": "40/50 chunks", "mode": {} }
```

---

## Task 1: Script-checkpoint helpers in `generate_script.py`

**Files:**
- Modify: `app/generate_script.py` (add helpers after the imports, near top; `atomic_json_write` is already imported at line 10)
- Test: `app/test_checkpoint_resume.py` (create)

- [ ] **Step 1: Write the failing test**

Create `app/test_checkpoint_resume.py`:
```python
#!/usr/bin/env python3
"""Standalone tests for resume-from-checkpoint helpers.

Run: python test_checkpoint_resume.py
(Also collectable by pytest if installed.)
"""
import os
import sys
import tempfile

import generate_script as gs


def test_script_checkpoint_roundtrip():
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "annotated_script.json")
        entries = [{"speaker": "A", "text": "hi"}]
        gs.save_script_checkpoint(out, completed_chunks=2, total_chunks=5,
                                  chunk_size=3000, input_hash="abc", all_entries=entries)
        loaded = gs.load_script_checkpoint(out, total_chunks=5, chunk_size=3000, input_hash="abc")
        assert loaded is not None
        assert loaded["completed_chunks"] == 2
        assert loaded["all_entries"] == entries


def test_script_checkpoint_mismatch_returns_none():
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "annotated_script.json")
        gs.save_script_checkpoint(out, 2, 5, 3000, "abc", [])
        # Different input_hash -> the split would differ -> must refuse to resume
        assert gs.load_script_checkpoint(out, 5, 3000, "DIFFERENT") is None
        # Different chunk_size -> must refuse
        assert gs.load_script_checkpoint(out, 5, 1000, "abc") is None
        # Different total_chunks -> must refuse
        assert gs.load_script_checkpoint(out, 99, 3000, "abc") is None


def test_clear_script_checkpoint():
    with tempfile.TemporaryDirectory() as d:
        out = os.path.join(d, "annotated_script.json")
        gs.save_script_checkpoint(out, 1, 2, 3000, "abc", [])
        assert os.path.exists(gs._script_checkpoint_path(out))
        gs.clear_script_checkpoint(out)
        assert not os.path.exists(gs._script_checkpoint_path(out))


def test_input_hash_is_stable_and_sensitive():
    assert gs.compute_input_hash("hello") == gs.compute_input_hash("hello")
    assert gs.compute_input_hash("hello") != gs.compute_input_hash("hello!")


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"FAIL {t.__name__}: {e}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    _main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd app && python test_checkpoint_resume.py`
Expected: FAIL — `AttributeError: module 'generate_script' has no attribute 'save_script_checkpoint'`

- [ ] **Step 3: Write minimal implementation**

In `app/generate_script.py`, add `import hashlib` to the imports, then add after the existing imports / before `def clean_json_string`:
```python
def _script_checkpoint_path(output_path):
    return output_path + ".script_checkpoint.json"


def compute_input_hash(book_content):
    """Stable hash of the (mojibake-fixed) source text. A changed source means
    the chunk split would differ, so a checkpoint with a different hash must not
    be resumed."""
    return hashlib.sha256(book_content.encode("utf-8")).hexdigest()


def save_script_checkpoint(output_path, completed_chunks, total_chunks,
                           chunk_size, input_hash, all_entries):
    data = {
        "completed_chunks": completed_chunks,
        "total_chunks": total_chunks,
        "chunk_size": chunk_size,
        "input_hash": input_hash,
        "all_entries": all_entries,
    }
    try:
        atomic_json_write(data, _script_checkpoint_path(output_path))
    except OSError as e:
        # Mirror review_script.py: never crash generation over a checkpoint
        # write failure (disk full, permissions) — just warn.
        print(f"WARNING: Failed to save script checkpoint: {e}. "
              f"Generation will continue but resume may not work.")


def load_script_checkpoint(output_path, total_chunks, chunk_size, input_hash):
    """Return the checkpoint dict only if it matches this run's split exactly;
    otherwise None (caller starts fresh)."""
    path = _script_checkpoint_path(output_path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if (data.get("total_chunks") != total_chunks or
            data.get("chunk_size") != chunk_size or
            data.get("input_hash") != input_hash):
        print("Found a script checkpoint but the source/split changed - starting fresh.")
        return None
    if not isinstance(data.get("all_entries"), list):
        return None
    return data


def clear_script_checkpoint(output_path):
    path = _script_checkpoint_path(output_path)
    if os.path.exists(path):
        try:
            os.remove(path)
        except OSError as e:
            print(f"WARNING: Failed to clear script checkpoint {path}: {e}.")
```
(`json` and `os` are already imported in this file.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd app && python test_checkpoint_resume.py`
Expected: PASS — `4/4 passed`

- [ ] **Step 5: Commit**

```bash
git add app/generate_script.py app/test_checkpoint_resume.py
git commit -m "feat: script-checkpoint helpers for generate_script resume"
```

---

## Task 2: Wire `--resume` + per-chunk save into `generate_script.py` main()

**Files:**
- Modify: `app/generate_script.py:413-552` (`main()`)
- Test: `app/test_checkpoint_resume.py` (add an equivalence test)

- [ ] **Step 1: Write the failing test**

Add to `app/test_checkpoint_resume.py`:
```python
def test_resume_offset_skips_completed_chunks():
    """The loop must skip already-completed chunks and keep restored context.
    We simulate the loop's resume arithmetic directly."""
    chunks = ["c1", "c2", "c3", "c4", "c5"]
    completed_chunks = 2
    restored_entries = [{"i": 0}, {"i": 1}]
    processed = list(restored_entries)
    for i, chunk in enumerate(chunks, 1):
        if i <= completed_chunks:
            continue
        # context for chunk i is everything accumulated so far
        assert len(processed) >= 2  # restored context is present
        processed.append({"i": i - 1, "chunk": chunk})
    # Only chunks 3,4,5 were processed; 1,2 came from the checkpoint
    assert [p.get("chunk") for p in processed if "chunk" in p] == ["c3", "c4", "c5"]
    assert len(processed) == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd app && python test_checkpoint_resume.py`
Expected: FAIL — `test_resume_offset_skips_completed_chunks` not yet present passes only after added; run shows it included. (If you added it correctly it will PASS immediately since it tests arithmetic; this test documents the loop contract Step 3 must satisfy.)

> Note: this is a contract/arithmetic test (no new production symbol), so it passes once added. Its purpose (Rule 12) is to lock the resume-loop semantics that Step 3 implements in `main()`.

- [ ] **Step 3: Write minimal implementation**

In `app/generate_script.py` `main()`:

(a) Add the `--resume` argument after the `--output` argument (line 416):
```python
    parser.add_argument("--resume", action="store_true",
                        help="Resume from a saved checkpoint if one matches this source.")
```

(b) Replace the block from `output_path = ...` (line 487) through the start of the loop with resume bootstrapping. After `output_path` is computed and before `all_entries = []`:
```python
    output_path = args.output or os.path.join(os.path.dirname(__file__), "..", "annotated_script.json")

    input_hash = compute_input_hash(book_content)
    all_entries = []
    completed_chunks = 0
    if args.resume:
        ckpt = load_script_checkpoint(output_path, total_chunks, chunk_size, input_hash)
        if ckpt:
            all_entries = ckpt["all_entries"]
            completed_chunks = ckpt["completed_chunks"]
            print(f"Resuming from checkpoint: {completed_chunks}/{total_chunks} chunks already done.")
        else:
            print("No usable checkpoint - starting fresh.")
    else:
        clear_script_checkpoint(output_path)
```
Delete the now-duplicated `all_entries = []` that was at line 489.

(c) In the chunk loop (line 506), skip completed chunks and save after each:
```python
    for i, chunk in enumerate(chunks, 1):
        if i <= completed_chunks:
            continue
        print(f"Processing chunk {i}/{total_chunks} ({len(chunk)} chars)...")

        chunk_start = time.monotonic()
        previous = all_entries if len(all_entries) > 0 else None
        entries = process_chunk(
            client, model_name, chunk, i, total_chunks, gen_params,
            previous_entries=previous,
        )
        chunk_elapsed = time.monotonic() - chunk_start
        chunk_times.append(chunk_elapsed)

        all_entries.extend(entries)
        save_script_checkpoint(output_path, i, total_chunks, chunk_size,
                               input_hash, all_entries)
        print(f"  Got {len(entries)} entries (chunk took {chunk_elapsed:.0f}s)")
        # ... existing ETA block unchanged ...
```

(d) After the final `atomic_json_write(all_entries, output_path)` (line 539), clear the checkpoint:
```python
    atomic_json_write(all_entries, output_path)
    clear_script_checkpoint(output_path)
```

- [ ] **Step 4: Run test + a smoke import to verify no syntax errors**

Run: `cd app && python test_checkpoint_resume.py && python -c "import generate_script"`
Expected: PASS — `5/5 passed`, and the import prints nothing (no error).

- [ ] **Step 5: Commit**

```bash
git add app/generate_script.py app/test_checkpoint_resume.py
git commit -m "feat: per-chunk checkpoint + --resume in generate_script main loop"
```

---

## Task 3: Generate Script single — detect endpoint + `resume` flag

**Files:**
- Modify: `app/app.py` (add `GenerateScriptRequest` model; add detect endpoint; edit `/api/generate_script` at 2137-2156)

- [ ] **Step 1: Add the request model and a checkpoint summary helper**

Near the other request models, add:
```python
class GenerateScriptRequest(BaseModel):
    resume: bool = False
```

Add a summary helper near `_summarize_review_checkpoint` (app.py:3475):
```python
def _summarize_script_checkpoint(path: str) -> Optional[dict]:
    """Uniform detect descriptor for a *.script_checkpoint.json. None if unusable."""
    data = safe_load_json(path)
    if not isinstance(data, dict) or "completed_chunks" not in data:
        return None
    done = data.get("completed_chunks", 0) or 0
    total = data.get("total_chunks", 0) or 0
    return {
        "exists": True,
        "done": done,
        "total": total,
        "label": f"{done}/{total} chunks",
        "mode": {},
        "mtime": os.path.getmtime(path) if os.path.exists(path) else None,
    }
```

- [ ] **Step 2: Add the detect endpoint**

After the `/api/generate_script` route, add:
```python
@app.get("/api/generate_script/checkpoint")
async def generate_script_checkpoint():
    """Detect an unfinished single-script generation (read-only)."""
    path = SCRIPT_PATH + ".script_checkpoint.json"
    s = _summarize_script_checkpoint(path)
    return s or {"exists": False, "done": 0, "total": 0, "label": "", "mode": {}}
```

- [ ] **Step 3: Add the `resume` flag to the start route**

Edit `/api/generate_script` (2137-2156) to accept an optional body and forward `--resume`:
```python
@app.post("/api/generate_script")
async def generate_script(background_tasks: BackgroundTasks,
                          request: Optional[GenerateScriptRequest] = None):
    if request is None:
        request = GenerateScriptRequest()
    # ... existing state.json / input_file / check_global_gpu_lock / claim_gpu_task unchanged ...
    cmd = [sys.executable, "-u", "generate_script.py", input_file]
    if request.resume:
        cmd.append("--resume")
    background_tasks.add_task(run_process, cmd, "script")
    return {"status": "started", "resume": request.resume}
```

- [ ] **Step 4: Verify the app imports cleanly**

Run: `cd app && python -c "import app"`
Expected: no output, no error.

- [ ] **Step 5: Commit**

```bash
git add app/app.py
git commit -m "feat: generate_script single resume flag + detect endpoint"
```

---

## Task 4: Generate Script batch — state file + detect + resume/fresh

**Files:**
- Modify: `app/app.py` (`BatchScriptRequest`; `generate_script_batch_start` at 2607-2680; add helpers + detect endpoint)
- Test: `app/test_checkpoint_resume.py` (batch-state helper roundtrip)

- [ ] **Step 1: Write the failing test**

Add to `app/test_checkpoint_resume.py`:
```python
def test_batch_state_roundtrip(tmp_path_factory=None):
    import json as _json
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, ".batch_script_state.json")
        files = [{"filename": "a.txt", "status": "done", "output_stem": "a"},
                 {"filename": "b.txt", "status": "pending", "output_stem": "b"}]
        # mirror what _save_batch_script_state writes
        from utils import atomic_json_write
        atomic_json_write({"files": files, "current_idx": 1}, path)
        loaded = _json.load(open(path))
        done = [f for f in loaded["files"] if f["status"] == "done"]
        assert [f["filename"] for f in done] == ["a.txt"]
        assert loaded["current_idx"] == 1
```

- [ ] **Step 2: Run test to verify it passes (storage contract)**

Run: `cd app && python test_checkpoint_resume.py`
Expected: PASS — this locks the on-disk shape `generate_script_batch_start` must write.

- [ ] **Step 3: Implement state persistence, fresh-clear, and resume**

(a) Add `resume` to the request model:
```python
class BatchScriptRequest(BaseModel):
    tasks: List[ScriptTask]
    resume: bool = False
```
(Keep the existing fields; only add `resume`.)

(b) Add helpers near the batch state path constants:
```python
def _save_batch_script_state(state: dict) -> None:
    try:
        atomic_json_write({
            "files": [dict(t) for t in state.get("tasks", [])],
            "current_idx": state.get("current_task_idx", 0),
        }, BATCH_SCRIPT_STATE_PATH)
    except OSError as e:
        state["logs"].append(f"WARNING: could not save batch state: {e}")


def _clear_batch_script_state(tasks) -> None:
    """Fresh start: drop the batch plan and every per-file script checkpoint."""
    if os.path.exists(BATCH_SCRIPT_STATE_PATH):
        try:
            os.remove(BATCH_SCRIPT_STATE_PATH)
        except OSError:
            pass
    for t in tasks:
        stem = secure_filename(os.path.splitext(t.filename)[0]) or ""
        if not stem:
            continue
        ckpt = os.path.join(SCRIPTS_DIR, f"{stem}.json.script_checkpoint.json")
        if os.path.exists(ckpt):
            try:
                os.remove(ckpt)
            except OSError:
                pass
```

(c) In `generate_script_batch_start`, before `def _run()`, compute the resume set:
```python
    resume = bool(request.resume)
    done_filenames = set()
    if resume:
        prev = safe_load_json(BATCH_SCRIPT_STATE_PATH)
        if isinstance(prev, dict):
            done_filenames = {f["filename"] for f in prev.get("files", [])
                              if isinstance(f, dict) and f.get("status") == "done"}
    else:
        _clear_batch_script_state(request.tasks)
```

(d) Inside `_run()`, seed each task's initial status from the resume set, and within the loop skip done files, pass `--resume`, and persist after each status change:
```python
        _init_batch_state(state,
                          [f"Starting batch of {len(request.tasks)} file(s)..."],
                          [{"filename": t.filename,
                            "status": "done" if t.filename in done_filenames else "pending"}
                           for t in request.tasks])
        _save_batch_script_state(state)
```
In the loop, immediately after `state["current_task_idx"] = i`:
```python
            if state["tasks"][i]["status"] == "done":
                state["logs"].append(f"[{i+1}/{len(request.tasks)}] Skipping — already done: {task.filename}")
                continue
```
Change the cmd to forward resume:
```python
            cmd = [
                sys.executable, "-u",
                os.path.join(BASE_DIR, "generate_script.py"),
                input_path, "--output", output_path,
            ]
            if resume:
                cmd.append("--resume")
```
After every line that sets `state["tasks"][i]["status"] = ...` inside the loop, follow it with `_save_batch_script_state(state)`. After the loop, on clean finish:
```python
        state["running"] = False
        if not state["cancel"]:
            _clear_batch_script_state(request.tasks)
        state["logs"].append("Batch script generation finished.")
```

(e) Add the detect endpoint after `generate_script_batch_start`:
```python
@app.get("/api/generate_script/batch/checkpoint")
async def generate_script_batch_checkpoint():
    prev = safe_load_json(BATCH_SCRIPT_STATE_PATH)
    if not isinstance(prev, dict) or not prev.get("files"):
        return {"exists": False, "done": 0, "total": 0, "label": "", "mode": {}}
    files = prev["files"]
    done = sum(1 for f in files if f.get("status") == "done")
    total = len(files)
    return {"exists": done < total, "done": done, "total": total,
            "label": f"{done}/{total} files", "mode": {"files": files}}
```

- [ ] **Step 4: Verify import**

Run: `cd app && python -c "import app" && python test_checkpoint_resume.py`
Expected: import clean; tests `6/6 passed` (or current count) PASS.

- [ ] **Step 5: Commit**

```bash
git add app/app.py app/test_checkpoint_resume.py
git commit -m "feat: generate_script batch resume state + detect endpoint"
```

---

## Task 5: Review single — detect endpoint + `resume` flag

**Files:**
- Modify: `app/app.py` (`ReviewRequest`/`ContextualReviewRequest`; routes at 2217-2235; add detect endpoint). `clear_checkpoint` from `review_script` is already imported (used at 2447).

- [ ] **Step 1: Add `resume` to the review request models**
```python
class ReviewRequest(BaseModel):
    dedupe_speakers: bool = True
    resume: bool = False

class ContextualReviewRequest(BaseModel):
    # ... existing fields ...
    resume: bool = False
```

- [ ] **Step 2: Clear the checkpoint on fresh start in both routes**

In `/api/review_script`, after the `check_global_gpu_lock("review")` line:
```python
    if not request.resume:
        clear_checkpoint(SCRIPT_PATH)
```
In `/api/review_script_contextual`, after its `check_global_gpu_lock("review")` line:
```python
    if not request.resume:
        clear_checkpoint(SCRIPT_PATH)
```
(`review_script.py` auto-resumes when a checkpoint is present, so resume needs no extra flag — only fresh must clear it.)

- [ ] **Step 3: Add the detect endpoint**

After the single review routes, add:
```python
@app.get("/api/review_script/checkpoint")
async def review_script_checkpoint():
    """Detect an unfinished single-book review (read-only)."""
    s = _summarize_review_checkpoint(SCRIPT_PATH + ".review_checkpoint.json")
    if not s:
        return {"exists": False, "done": 0, "total": 0, "label": "", "mode": {}}
    return {"exists": True, "done": s["completed_batches"], "total": s["total_batches"],
            "label": f"{s['completed_batches']}/{s['total_batches']} batches", "mode": {}}
```

- [ ] **Step 4: Verify import**

Run: `cd app && python -c "import app"`
Expected: no error.

- [ ] **Step 5: Commit**

```bash
git add app/app.py
git commit -m "feat: review single resume flag + detect endpoint"
```

---

## Task 6: Review batch — persist pass/order plan + detect + resume/fresh

**Files:**
- Modify: `app/app.py` (`BatchReviewRequest`; `review_script_batch_start` at 2351-2564; add helpers + detect endpoint)

- [ ] **Step 1: Add `resume` to `BatchReviewRequest` and state helpers**
```python
class BatchReviewRequest(BaseModel):
    # ... existing fields ...
    resume: bool = False
```
Add helpers near the batch state constants:
```python
def _save_batch_review_state(state: dict, names: list, settings: dict) -> None:
    try:
        atomic_json_write({
            "names": names,
            "current_pass": state.get("current_pass"),
            "current_task_idx": state.get("current_task_idx", 0),
            "bidirectional": settings.get("bidirectional", False),
            "window": settings.get("window", 0),
            "dedupe": settings.get("dedupe", False),
            "discover": settings.get("discover", False),
            "tasks": [dict(t) for t in state.get("tasks", [])],
        }, BATCH_REVIEW_STATE_PATH)
    except OSError as e:
        state["logs"].append(f"WARNING: could not save batch review state: {e}")


def _clear_batch_review_state(names: list) -> None:
    """Fresh start: drop the plan and every per-book review checkpoint."""
    if os.path.exists(BATCH_REVIEW_STATE_PATH):
        try:
            os.remove(BATCH_REVIEW_STATE_PATH)
        except OSError:
            pass
    for name in names:
        safe = secure_filename(name)
        if not safe:
            continue
        clear_checkpoint(os.path.join(SCRIPTS_DIR, f"{safe}.json"))
```

- [ ] **Step 2: Compute the resume plan before `_run()`**

After `names = request.script_names` / `total = len(names)` in `review_script_batch_start`:
```python
    resume = bool(request.resume)
    resume_pass = "fwd"
    resume_idx = 0
    if resume:
        prev = safe_load_json(BATCH_REVIEW_STATE_PATH)
        if isinstance(prev, dict) and prev.get("names") == names:
            resume_pass = prev.get("current_pass") or "fwd"
            resume_idx = prev.get("current_task_idx", 0) or 0
        else:
            resume = False  # plan changed (different books/order) -> fresh
    if not resume:
        _clear_batch_review_state(names)
    settings = {"bidirectional": bidirectional, "window": window,
                "dedupe": dedupe, "discover": discover}
```

- [ ] **Step 3: Persist state through the run and honor the resume offsets**

Inside `_run()`, after `_init_batch_state(...)` and the `state[...]` seeding, add `_save_batch_review_state(state, names, settings)`.

Inside `_process_book`, after each `state["tasks"][i]["status"] = ...` assignment, follow with `_save_batch_review_state(state, names, settings)`. Also after setting `state["current_pass"] = ...`.

Change the forward-pass loop to honor `resume_pass`/`resume_idx`:
```python
        state["current_pass"] = "fwd"
        _save_batch_review_state(state, names, settings)
        if resume_pass == "fwd":
            if bidirectional:
                state["logs"].append("=== Forward pass (reading order) ===")
            for i, name in enumerate(names):
                if i < resume_idx:
                    continue
                if state["cancel"]:
                    state["logs"].append("Batch review cancelled.")
                    break
                if not _process_book(i, name, tag=" [fwd]" if bidirectional else ""):
                    break
            state["logs"].append(_format_pass_summary(
                "Forward pass" if bidirectional else "Batch review",
                state["totals_fwd"], state["aliases_fwd"], show_aliases=discover))
```
Change the backward pass to start at `resume_idx` when resuming directly into bwd:
```python
        if bidirectional and not state["cancel"]:
            state["logs"].append("=== Backward pass (hindsight: re-scanning from the end) ===")
            state["current_pass"] = "bwd"
            _save_batch_review_state(state, names, settings)
            bwd_start = resume_idx if resume_pass == "bwd" else total - 1
            for i in range(bwd_start, -1, -1):
                if state["cancel"]:
                    state["logs"].append("Batch review cancelled.")
                    break
                if not _process_book(i, names[i], tag=" [bwd]"):
                    break
            # ... existing backward summary block unchanged ...
```
On clean finish (after the report write), clear the plan:
```python
        state["running"] = False
        if not state["cancel"]:
            _clear_batch_review_state(names)
        state["logs"].append("Batch review finished.")
```
The existing "preserve forward checkpoint during backward pass" block (2432-2447) is left exactly as-is — Rule 9.

- [ ] **Step 4: Add the detect endpoint**

```python
@app.get("/api/review_script/batch/checkpoint")
async def review_script_batch_checkpoint():
    prev = safe_load_json(BATCH_REVIEW_STATE_PATH)
    if not isinstance(prev, dict) or not prev.get("names"):
        return {"exists": False, "done": 0, "total": 0, "label": "", "mode": {}}
    tasks = prev.get("tasks", [])
    done = sum(1 for t in tasks if t.get("status") == "done")
    total = len(prev["names"])
    pass_label = ("backward" if prev.get("current_pass") == "bwd" else "forward")
    kind = "front-to-back" if prev.get("bidirectional") else "forward-only"
    return {
        "exists": True, "done": done, "total": total,
        "label": f"{kind}, {pass_label} pass, {done}/{total} books",
        "mode": {
            "bidirectional": prev.get("bidirectional", False),
            "current_pass": prev.get("current_pass"),
            "current_task_idx": prev.get("current_task_idx", 0),
            "names": prev["names"], "tasks": tasks,
        },
    }
```

- [ ] **Step 5: Verify import + commit**

Run: `cd app && python -c "import app"`
Expected: no error.
```bash
git add app/app.py
git commit -m "feat: review batch pass/order resume state + detect endpoint"
```

---

## Task 7: Frontend — `confirmResumeOrFresh()` + wire into four start handlers

**Files:**
- Modify: `app/static/index.html` (add helper; wire into the four start handlers). Vanilla JS, braces on all blocks (Rule 18).

- [ ] **Step 1: Add the shared helper**

Add near the other top-level helpers (e.g. by `confirmIfRemote`):
```javascript
// Returns 'resume' | 'fresh' | null(cancel). Single source of the resume
// prompt so the four start buttons cannot drift (Rule 15).
async function confirmResumeOrFresh(detectUrl) {
    let info;
    try {
        info = await API.get(detectUrl);
    } catch (e) {
        return 'fresh'; // detect failed — behave like a normal clean start
    }
    if (!info || !info.exists) {
        return 'fresh';
    }
    const label = info.label || `${info.done}/${info.total}`;
    const msg = `⚠ Unfinished run found (${label}).\n\n` +
                `OK = Resume where it left off.\n` +
                `Cancel = Start fresh (discards saved progress).`;
    if (window.confirm(msg)) {
        return 'resume';
    }
    return 'fresh';
}
```
> A two-button native `confirm` maps OK→Resume, Cancel→Start-fresh. (There is no Cancel-the-launch path here because the user explicitly clicked Start; both answers start a run, they only differ in resume vs fresh.)

- [ ] **Step 2: Wire Generate Script (single)**

In the single generate-script start handler, before the POST that starts generation:
```javascript
            const choice = await confirmResumeOrFresh('/api/generate_script/checkpoint');
            await API.post('/api/generate_script', { resume: choice === 'resume' });
```
(Replace the existing bodyless `API.post('/api/generate_script')` call.)

- [ ] **Step 3: Wire the other three handlers**

Batch generate (before `POST /api/generate_script/batch/start`):
```javascript
            const choice = await confirmResumeOrFresh('/api/generate_script/batch/checkpoint');
            payload.resume = choice === 'resume';
```
Review single (before `POST /api/review_script` or `/api/review_script_contextual`):
```javascript
            const choice = await confirmResumeOrFresh('/api/review_script/checkpoint');
            body.resume = choice === 'resume';
```
Batch review (before `POST /api/review_script/batch/start`):
```javascript
            const choice = await confirmResumeOrFresh('/api/review_script/batch/checkpoint');
            payload.resume = choice === 'resume';
```
For each, add `resume` into the existing request body object (`payload`/`body`) that the handler already sends.

- [ ] **Step 4: Verify in the running app**

Run: start the app (`python app/app.py` or the launcher), open the UI. With no prior run, click each Start — no prompt appears, generation/review starts normally. To exercise the prompt: start a generation, kill it mid-run (leaving a `*.script_checkpoint.json`), reload, click Start — the **⚠ Unfinished run found** prompt appears; OK resumes from the saved chunk, Cancel starts fresh.
Expected: prompt only when a checkpoint exists; Resume continues, Start-fresh restarts.

- [ ] **Step 5: Commit**

```bash
git add app/static/index.html
git commit -m "feat: explicit Resume/Start-fresh prompt wired into 4 start buttons"
```

---

## Task 8: Gitignore the generated checkpoint/state files

**Files:**
- Modify: `.gitignore`

- [ ] **Step 1: Add ignore entries**

Append to `.gitignore`:
```
# Resume checkpoints / batch plans (generated at run time)
*.script_checkpoint.json
*.review_checkpoint.json
scripts/.batch_script_state.json
scripts/.batch_review_state.json
```
(If `*.review_checkpoint.json` is already ignored, skip that line.)

- [ ] **Step 2: Verify nothing stray is tracked**

Run: `git status --short && git check-ignore -v annotated_script.json.script_checkpoint.json`
Expected: the checkpoint pattern is reported as ignored; `git status` shows no checkpoint/state files staged.

- [ ] **Step 3: Commit**

```bash
git add .gitignore
git commit -m "chore: gitignore resume checkpoint and batch-state files"
```

---

## Final verification

- [ ] Run the test module: `cd app && python test_checkpoint_resume.py` → all PASS.
- [ ] Import smoke: `cd app && python -c "import app, generate_script"` → no error.
- [ ] Manual interrupt/resume of a real generation (Task 7 Step 4) → Resume continues from the saved chunk; output matches a full run.
- [ ] Manual batch-review interrupt mid-backward-pass → detect reports "front-to-back, backward pass, k/N books"; Resume continues the backward pass at the right book; Start-fresh clears all per-book checkpoints.
