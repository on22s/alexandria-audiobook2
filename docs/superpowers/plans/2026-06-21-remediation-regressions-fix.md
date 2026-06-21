# Remediation-Pass Regressions — Implementation Plan

> **For agentic workers:** Execute task-by-task directly in this session (no subagent dispatch — implementer + 2 reviewers per task was found too slow/costly for a fully-specified plan like this one; the author verifies each diff directly via Read + live test instead). Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Fix the 20 findings from a `/code-review-20` pass on `git diff dceca41...HEAD` — bugs the *previous* remediation pass (30 commits fixing 30 earlier findings) itself introduced, mostly via the same "patch the named site, miss the sibling" pattern that pass was trying to close.

**Architecture:** Each task is a standalone fix scoped to one finding (or 2-3 findings that share one mechanism, e.g. the lmstudio remote-status cache). Where a fix touches a shared helper this branch already created (`extract_json_object`, `cancelTask`, `_startPolling`), the task extends that helper correctly rather than reverting to a one-off. One genuinely new piece of infrastructure is added: a path-denylist check closing the voicelab RCE finding.

**Tech Stack:** Python 3 / FastAPI (`app/app.py` + friends), vanilla JS in `app/static/index.html`, standalone scripts at repo root.

**Verification convention:** Same as the previous plan — `python3 -c '...'` repro scripts for pure-Python logic, a live `app/env/bin/python3 app.py` instance + `requests`/`app/test_api.py` for HTTP endpoints, `node --check` on the extracted `<script>` block for `index.html` JS syntax.

---

## Findings Index

| # | Finding | File | Task |
|---|---|---|---|
| 1 | `run_analyze` checkpoint moved entirely outside loop; commit's own justification factually backwards | voice_analysis.py:400 | [Task 1](#task-1-restore-per-group-checkpointing-in-run_analyze) |
| 2 | `train_lora.py` traversal guard bypassed via `ref_audio_path` (resolved before the loop) | app/train_lora.py | [Task 2](#task-2-close-the-ref_audio_path-traversal-bypass) |
| 3 | Voicelab RCE: `pipeline_repo`/`rocm_python` validated for existence, not provenance | app/app.py | [Task 3](#task-3-close-the-voicelab-rce-via-a-path-denylist) |
| 4 | `dataset_builder_generate_sample`'s `sample_index` unbounded → memory-exhaustion DoS | app/app.py | [Task 4](#task-4-bound-sample_index) |
| 5 | `voicelab_save_config` rejects `""` (intended as "clear field") as an invalid path | app/app.py | folded into [Task 3](#task-3-close-the-voicelab-rce-via-a-path-denylist) (same lines) |
| 6 | Remote-status cache keyed wrong, never invalidated, unguarded check-then-act race | app/lmstudio_settings.py | [Task 6](#task-6-fix-the-remote-status-cache-key-invalidation-and-race) |
| 10 | (same cache, same task) | app/lmstudio_settings.py | — |
| 11 | (same cache, same task) | app/lmstudio_settings.py | — |
| 7 | `_combine_pass_stats`'s `partial` flag always `True` for non-bidirectional reviews | app/app.py | [Task 7](#task-7-fix-partial-flag-for-non-bidirectional-reviews) |
| 8 | `lora_preview`'s adapter download still runs before `claim_gpu_task` | app/app.py | [Task 8](#task-8-close-lora_previews-remaining-toctou-window) |
| 9 | `dataset_builder_generate_sample`'s `get_engine()` runs before `claim_gpu_task` | app/app.py | [Task 9](#task-9-close-dataset_builder_generate_samples-toctou-window) |
| 12 | `secure_filename` has no length cap → uncaught `OSError` | app/utils.py | [Task 10](#task-10-cap-secure_filenames-output-length) |
| 13 | Shared `run_rocm_smi_json` lost the returncode check and all per-failure logging | gpu_stats.py | [Task 11](#task-11-restore-returncode-check-and-logging-in-run_rocm_smi_json) |
| 14 | `extract_balanced`'s escape-scoping diverges from `clean_json_string`'s original | app/utils.py | [Task 12](#task-12-document-extract_balanceds-escape-scoping) |
| 15 | `safe_load_json` itself (root of ~20 call sites) still has no logging | app/utils.py | [Task 13](#task-13-add-logging-to-safe_load_json-itself) |
| 16 | `review_script_contextual` has 2 more silent JSON-swallow sites | app/app.py | [Task 14](#task-14-log-review_script_contextuals-silent-json-swallows) |
| 17 | `dsbCancel`/`dsbSaveForm`/`dsbSaveRows` never migrated to toast-on-failure | app/static/index.html | [Task 15](#task-15-migrate-dataset-builder-actions-to-toast-on-failure) |
| 18 | `dedupe_speakers` still uses the old greedy regex, not `extract_json_object` | app/review_script.py | [Task 16](#task-16-migrate-dedupe_speakers-and-restore-lost-diagnostics) |
| 19 | Migrating to `extract_json_object` silently dropped a diagnostic log in `find_nicknames.py` | app/find_nicknames.py | (same task) |
| 20 | `_runBatchRender`'s `onTick` isn't awaited by `_startPolling` | app/static/index.html | [Task 17](#task-17-await-ontick-in-_startpolling) |

17 tasks cover all 20 findings (3 lmstudio-cache findings share Task 6; the 2 JSON-extraction-migration findings share Task 16).

---

## File Structure

- `voice_analysis.py` — revert `run_analyze`'s checkpoint placement (Task 1).
- `app/train_lora.py` — extend the existing traversal guard to `ref_audio_path` (Task 2).
- `app/app.py` — new `_is_inside`/`_validate_voicelab_path` helpers + 2 call sites (Task 3); `sample_index` bound (Task 4); blank-field fix (Task 5); `partial`-flag call site (Task 7); `lora_preview`/`dataset_builder_generate_sample` reordering (Tasks 8-9); `review_script_contextual` logging (Task 14).
- `app/lmstudio_settings.py` — cache key, lock, invalidation function; new call from `apply_remote_lmstudio_settings` (Task 6).
- `app/utils.py` — `secure_filename` length cap (Task 10); `extract_balanced` comment (Task 12); `safe_load_json` logging (Task 13).
- `gpu_stats.py` — restore returncode check + logging (Task 11).
- `app/static/index.html` — `dsbCancel`/`dsbSaveForm`/`dsbSaveRows` (Task 15); `_startPolling`'s `onTick` await (Task 17).
- `app/review_script.py`, `app/find_nicknames.py` — migrate to `extract_json_object`, restore diagnostics (Task 16).

---

## Task 1: Restore per-group checkpointing in `run_analyze`

**Files:** Modify `voice_analysis.py:394-403`

**Context:** The previous fix moved `pickle.dump` outside the per-group loop, justified as "matching `run_dedup`'s pattern." `run_dedup`'s own checkpoint (line 216) is actually *inside* its per-folder loop — it pays the same cumulative-I/O cost this fix was trying to avoid, but nobody has flagged that as a problem, because crash-resilience for a long GPU-bound extraction job is worth more than the I/O cost. Restore `run_analyze` to the same trade-off, deliberately.

- [ ] **Step 1: Revert the checkpoint to inside the loop**

```python
    # Extract missing groups
    for group_name, zip_paths in zip_groups.items():
        if group_name in all_embs:
            continue
        print(f"\n─── Processing group: {group_name} ───")
        g_embs, g_pros, g_wavs = [], [], []

        for zp in zip_paths:
            if not os.path.exists(zp):
                print(f"  Zip not found: {zp}, skipping")
                continue
            wav_names = list_wavs_in_zip(zp)
            if ANALYZE_SAMPLES and len(wav_names) > ANALYZE_SAMPLES:
                train = [n for n in wav_names if n.startswith("train/")]
                val   = [n for n in wav_names if n.startswith("val/")]
                if train and val:
                    half      = ANALYZE_SAMPLES // 2
                    wav_names = (
                        np.random.choice(train, min(half, len(train)), replace=False).tolist()
                        + np.random.choice(val,   min(half, len(val)),   replace=False).tolist()
                    )
                else:
                    wav_names = np.random.choice(wav_names, ANALYZE_SAMPLES, replace=False).tolist()

            print(f"  Extracting {len(wav_names)} samples from {os.path.basename(zp)}...")
            for wname in tqdm(wav_names, desc=f"  {group_name}"):
                try:
                    wav, sr = load_wav_from_zip(zp, wname)
                    g_embs.append(extract_embedding(wav, sr, model, device))
                    g_pros.append(extract_prosody(wav, sr))
                    g_wavs.append((zp, wname))
                except Exception as e:
                    tqdm.write(f"  Warning: extraction failed for {wname}: {e}")

        if g_embs:
            all_embs[group_name]      = np.array(g_embs)
            all_prosody[group_name]   = g_pros
            all_wav_names[group_name] = g_wavs
            print(f"  → {len(g_embs)} embeddings extracted")
            # Checkpoint after each group. This re-serializes every
            # previously-finished group's embeddings too (same cumulative-I/O
            # cost run_dedup's own per-folder checkpoint at line 216 already
            # pays) - deliberately accepted because losing a crash/interrupt's
            # GPU-extraction progress for every group done so far is worse
            # than the extra I/O.
            pickle.dump(
                {"embeddings": all_embs, "prosody": all_prosody, "wav_names": all_wav_names},
                open(cache_file, "wb"),
            )
    print(f"\nCache saved to {cache_file}")
```

- [ ] **Step 2: Verify the indentation/placement is correct**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/.claude/worktrees/claude-md-rule-audit
python3 -c "import ast; ast.parse(open('voice_analysis.py').read())" && echo "syntax OK"
grep -n "pickle.dump" voice_analysis.py
```

Expected: the `pickle.dump(` at line ~400 is now indented 12 spaces (inside `if g_embs:`, inside the `for group_name...` loop), not 4 spaces.

- [ ] **Step 3: Commit**

```bash
git add voice_analysis.py
git commit -m "$(cat <<'EOF'
fix: restore per-group checkpointing in run_analyze

The previous fix moved pickle.dump entirely outside the loop, justified
as matching run_dedup's pattern - but run_dedup's own checkpoint (line
216) is inside its per-folder loop, paying the identical cumulative-I/O
cost. Moving run_analyze's checkpoint outside its loop didn't fix an
inconsistency, it created one: a crash now loses every group's progress
instead of just the in-flight one. Restored to match run_dedup exactly,
accepting the same I/O-vs-crash-resilience trade-off deliberately.
EOF
)"
```

---

## Task 2: Close the `ref_audio_path` traversal bypass

**Files:** Modify `app/train_lora.py:94-108`

**Context:** The per-entry loop in `load_dataset` (lines ~129-140) got a `realpath`+`commonpath` traversal guard. `ref_audio_path` — resolved *before* that loop, from the same attacker-controlled manifest fields (`ref_audio`, or a fallback to `audio_filepath`/`audio`) — has no guard at all and goes straight to `librosa.load()`.

- [ ] **Step 1: Write a reproduction script**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/.claude/worktrees/claude-md-rule-audit
python3 -c "
import os
data_dir = '/tmp/fake_dataset_dir'
os.makedirs(data_dir, exist_ok=True)
ref_rel = '../../../../etc/hostname'
ref_audio_path = os.path.join(data_dir, ref_rel)
resolved = os.path.realpath(ref_audio_path)
real_data_dir = os.path.realpath(data_dir)
escapes = not (resolved == real_data_dir or resolved.startswith(real_data_dir + os.sep))
print('escapes (should be True, currently unguarded):', escapes)
"
```

- [ ] **Step 2: Read the current code to confirm the exact lines**

```python
    ref_audio_path = None
    if entries[0].get("ref_audio"):
        ref_rel = entries[0]["ref_audio"]
        ref_audio_path = os.path.join(data_dir, ref_rel)
    elif os.path.exists(os.path.join(data_dir, "ref.wav")):
        ref_audio_path = os.path.join(data_dir, "ref.wav")

    if ref_audio_path is None:
        # Fall back to first training sample as reference
        first_audio_rel = entries[0].get("audio_filepath") or entries[0].get("audio", "")
        ref_audio_path = os.path.join(data_dir, first_audio_rel)

    if not os.path.exists(ref_audio_path):
        print(f"[ERROR] Reference audio not found: {ref_audio_path}", flush=True)
        sys.exit(1)
```

- [ ] **Step 3: Add a shared containment helper and apply it to every branch**

```python
    def _resolve_in_data_dir(rel_path):
        """Resolve `rel_path` under data_dir, or return None if it escapes."""
        resolved = os.path.realpath(os.path.join(data_dir, rel_path))
        real_data_dir = os.path.realpath(data_dir)
        if resolved == real_data_dir or resolved.startswith(real_data_dir + os.sep):
            return resolved
        return None

    ref_audio_path = None
    if entries[0].get("ref_audio"):
        ref_audio_path = _resolve_in_data_dir(entries[0]["ref_audio"])
        if ref_audio_path is None:
            print(f"[ERROR] ref_audio escapes the dataset directory: {entries[0]['ref_audio']}", flush=True)
            sys.exit(1)
    elif os.path.exists(os.path.join(data_dir, "ref.wav")):
        ref_audio_path = os.path.join(data_dir, "ref.wav")

    if ref_audio_path is None:
        # Fall back to first training sample as reference
        first_audio_rel = entries[0].get("audio_filepath") or entries[0].get("audio", "")
        ref_audio_path = _resolve_in_data_dir(first_audio_rel)
        if ref_audio_path is None:
            print(f"[ERROR] first-sample reference path escapes the dataset directory: {first_audio_rel}", flush=True)
            sys.exit(1)

    if not os.path.exists(ref_audio_path):
        print(f"[ERROR] Reference audio not found: {ref_audio_path}", flush=True)
        sys.exit(1)
```

`os.path.join(data_dir, "ref.wav")` (the middle branch) is a hardcoded literal filename, not attacker-controlled — it doesn't need the guard.

- [ ] **Step 4: Verify the helper rejects traversal and accepts normal paths**

```bash
python3 -c "
import os
data_dir = '/tmp/fake_dataset_dir'
def _resolve_in_data_dir(rel_path):
    resolved = os.path.realpath(os.path.join(data_dir, rel_path))
    real_data_dir = os.path.realpath(data_dir)
    if resolved == real_data_dir or resolved.startswith(real_data_dir + os.sep):
        return resolved
    return None
print('traversal rejected (should be None):', _resolve_in_data_dir('../../../etc/hostname'))
print('normal path accepted (should be a path):', _resolve_in_data_dir('sample_001.wav'))
"
```

- [ ] **Step 5: Check syntax**

```bash
python3 -c "import ast; ast.parse(open('app/train_lora.py').read())" && echo "syntax OK"
```

- [ ] **Step 6: Commit**

```bash
git add app/train_lora.py
git commit -m "$(cat <<'EOF'
fix: close the ref_audio_path traversal bypass in load_dataset

The per-entry loop's traversal guard (added in the previous remediation
pass) only covered audio_filepath/audio inside the loop. ref_audio_path
- resolved before the loop, from the same attacker-controlled manifest
fields (ref_audio, or a fallback to audio_filepath/audio) - had no
guard at all and went straight to librosa.load(), a direct bypass using
a different code path in the same function.
EOF
)"
```

---

## Task 3: Close the voicelab RCE via a path denylist

**Files:** Modify `app/app.py` (new helpers near `_safe_subpath`, plus calls in `voicelab_save_config` and `voicelab_start`)

**Context:** `voicelab_save_config`/`voicelab_start` validate `rocm_python`/`pipeline_repo` for existence and (for `rocm_python`) the executable bit — but never check the path isn't inside a directory this app itself writes uploaded/generated content into. An attacker can upload a dataset ZIP via `/api/lora/upload_dataset` containing a file named `batch_train_lora.py`, set `pipeline_repo` to the resulting directory (passes `os.path.isdir`), and have it executed via `/api/voicelab/start`.

- [ ] **Step 1: Read the existing `_safe_subpath` for the containment-check convention already used in this file**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/.claude/worktrees/claude-md-rule-audit
grep -n "^def _safe_subpath" app/app.py
sed -n '2020,2032p' app/app.py
```

- [ ] **Step 2: Add `_is_inside` and `_validate_voicelab_path` right after `_safe_subpath`**

```python
def _is_inside(path: str, base_dir: str) -> bool:
    """True if the realpath of `path` is base_dir itself or somewhere under it."""
    base = os.path.realpath(base_dir)
    target = os.path.realpath(path)
    return target == base or target.startswith(base + os.sep)


# Directories this app writes user/attacker-suppliable content into (uploads,
# extracted dataset ZIPs, generated samples/previews). voicelab's rocm_python/
# pipeline_repo must never resolve inside one of these - otherwise anyone who
# can upload a file (via /api/upload, /api/lora/upload_dataset, etc.) could
# point voicelab at content they just planted and have it executed as the
# "trusted" interpreter or pipeline script.
_VOICELAB_FORBIDDEN_DIRS = [
    UPLOADS_DIR, LORA_DATASETS_DIR, LORA_MODELS_DIR, BUILTIN_LORA_DIR,
    DATASET_BUILDER_DIR, DESIGNED_VOICES_DIR, CLONE_VOICES_DIR, VOICELINES_DIR,
]


def _validate_voicelab_path(path: str, what: str) -> None:
    """Raise HTTPException 400 if `path` resolves inside a directory this app
    writes uploaded/generated content into - see _VOICELAB_FORBIDDEN_DIRS."""
    for forbidden in _VOICELAB_FORBIDDEN_DIRS:
        if _is_inside(path, forbidden):
            raise HTTPException(
                status_code=400,
                detail=f"{what} cannot be inside {forbidden} - that directory holds "
                       f"uploaded/generated content, not trusted pipeline code.")
```

- [ ] **Step 3: Call it from `voicelab_save_config`, right after each existing path check** (apply this together with Task 5's blank-field fix below — read the current function body first since both edits touch the same lines)

```python
    if updates.get("rocm_python"):
        path = updates["rocm_python"]
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            raise HTTPException(status_code=400,
                                detail=f"rocm_python must be an existing, executable file: {path}")
        _validate_voicelab_path(path, "rocm_python")
    if updates.get("pipeline_repo"):
        if not os.path.isdir(updates["pipeline_repo"]):
            raise HTTPException(status_code=400,
                                detail=f"pipeline_repo must be an existing directory: {updates['pipeline_repo']}")
        _validate_voicelab_path(updates["pipeline_repo"], "pipeline_repo")
    if updates.get("profiler_model"):
        if not os.path.isfile(updates["profiler_model"]):
            raise HTTPException(status_code=400,
                                detail=f"profiler_model must be an existing file: {updates['profiler_model']}")
        _validate_voicelab_path(updates["profiler_model"], "profiler_model")
```

(This already folds in Task 5's fix — `updates.get(...)` truthiness instead of `"key" in updates` membership — so empty-string values skip validation and clear the field via the unconditional `cfg.update(updates)` below, same as `profiler_model` already did before this task.)

- [ ] **Step 4: Call it from `voicelab_start`'s pre-flight too** (defense in depth — closes the gap even if `voicelab_config.json` was hand-edited after the last save)

```python
    needs_rocm = any(s in request.stages for s in ("dedup", "train", "profile"))
    if needs_rocm and not os.path.isfile(cfg["rocm_python"]):
        raise HTTPException(status_code=400,
                            detail=f"ROCm interpreter not found: {cfg['rocm_python']}. Set it in Voice Lab settings.")
    if needs_rocm:
        _validate_voicelab_path(cfg["rocm_python"], "rocm_python")
    profiler_model = (request.profiler_model or cfg["profiler_model"] or "").strip()
    if "profile" in request.stages and profiler_model and not os.path.isfile(profiler_model):
        raise HTTPException(status_code=400,
                            detail=f"profiler_model not found: {profiler_model}. Set it in Voice Lab settings.")
    if "profile" in request.stages and profiler_model:
        _validate_voicelab_path(profiler_model, "profiler_model")
    if "dedup" in request.stages and not os.path.isdir(zips_dir):
        raise HTTPException(status_code=400, detail=f"Input folder not found: {zips_dir}")
    if "train" in request.stages and not os.path.isdir(os.path.join(zips_dir, "_deduped")) and "dedup" not in request.stages:
        raise HTTPException(status_code=400,
                            detail=f"No _deduped folder in {zips_dir}; run the dedup stage first.")
    for s, fname, base in (("train", "batch_train_lora.py", cfg["pipeline_repo"]),
                           ("profile", "voice_profiler.py", cfg["pipeline_repo"])):
        if s in request.stages and not os.path.isfile(os.path.join(base, fname)):
            raise HTTPException(status_code=400,
                                detail=f"{fname} not found in {base}. Check the pipeline repo path in Voice Lab settings.")
        if s in request.stages:
            _validate_voicelab_path(base, "pipeline_repo")
```

- [ ] **Step 5: Verify live**

```bash
cd app
ALEXANDRIA_PORT=4203 /home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python3 app.py > /tmp/app_test_4203.log 2>&1 &
echo $! > /tmp/app_test_4203.pid
sleep 5
mkdir -p lora_datasets
/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python3 -c "
import requests
r = requests.post('http://127.0.0.1:4203/api/voicelab/config', json={'pipeline_repo': '/home/fakemitch/pinokio/api/alexandria-audiobook2.git/.claude/worktrees/claude-md-rule-audit/lora_datasets'})
print('pipeline_repo inside lora_datasets:', r.status_code, r.text[:200])
"
```

Expected: 400, mentioning `lora_datasets` and "not trusted pipeline code".

- [ ] **Step 6: Run the full test suite for regressions, then clean up**

```bash
/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python3 test_api.py --url http://127.0.0.1:4203 2>&1 | tail -15
kill $(cat /tmp/app_test_4203.pid) 2>/dev/null; rm -f /tmp/app_test_4203.pid /tmp/app_test_4203.log
cd ..
rmdir lora_datasets 2>/dev/null || true
```

Expected: same 5 pre-existing unrelated failures as every previous run, no new ones.

- [ ] **Step 7: Commit**

```bash
git add app/app.py
git commit -m "$(cat <<'EOF'
fix: close voicelab RCE - deny-list upload-writable directories

voicelab_save_config/voicelab_start validated rocm_python/pipeline_repo/
profiler_model for existence (and rocm_python's executable bit), but
never checked provenance. Any directory this app writes attacker-
suppliable content into (uploads, extracted dataset ZIPs, generated
samples) could be uploaded to, then pointed at by pipeline_repo/
rocm_python, then executed via /api/voicelab/start. Added a shared
denylist check called from both endpoints, and folded in the blank-
field-rejection fix (Task 5) at the same call site since both touch
voicelab_save_config's validation block.
EOF
)"
```

---

## Task 4: Bound `sample_index`

**Files:** Modify `app/app.py:439-444` (`DatasetSampleGenRequest`)

**Context:** `sample_index: int` has no upper bound. `dataset_builder_generate_sample`'s `while len(samples) <= request.sample_index: samples.append(...)` is reachable after one successful generation call and will happily append billions of placeholder dicts for a large `sample_index` — a memory-exhaustion DoS.

- [ ] **Step 1: Check the `Field` import**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/.claude/worktrees/claude-md-rule-audit
grep -n "^from pydantic import" app/app.py
```

Expected: `from pydantic import BaseModel` (no `Field` yet).

- [ ] **Step 2: Add `Field` to the import**

```python
from pydantic import BaseModel, Field
```

- [ ] **Step 3: Bound `sample_index`**

```python
class DatasetSampleGenRequest(BaseModel):
    description: str      # full voice description (root + emotion already combined by frontend)
    text: str
    dataset_name: str     # working directory name
    sample_index: int = Field(ge=0, le=4999)  # row number
    seed: int = -1        # -1 = random, >= 0 = manual seed
```

(4999 is generous for any realistic voice dataset — typical training sets are tens to low-hundreds of rows — while fully closing the unbounded-allocation path.)

- [ ] **Step 4: Verify Pydantic rejects an out-of-range value**

```bash
/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python3 -c "
import sys
sys.path.insert(0, 'app')
from app import DatasetSampleGenRequest
try:
    DatasetSampleGenRequest(description='x', text='x', dataset_name='x', sample_index=2_000_000_000)
    print('FAIL: should have raised')
except Exception as e:
    print('OK, rejected:', type(e).__name__)
"
```

(If importing `app.py` standalone fails due to module-level setup, verify instead via a live HTTP call in Step 5.)

- [ ] **Step 5: Verify live**

```bash
cd app
ALEXANDRIA_PORT=4203 /home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python3 app.py > /tmp/app_test_4203.log 2>&1 &
echo $! > /tmp/app_test_4203.pid
sleep 5
/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python3 -c "
import requests
r = requests.post('http://127.0.0.1:4203/api/dataset_builder/generate_sample', json={
    'description': 'x', 'text': 'x', 'dataset_name': 'x', 'sample_index': 2000000000, 'seed': -1
})
print('oversized sample_index:', r.status_code)
"
kill $(cat /tmp/app_test_4203.pid) 2>/dev/null; rm -f /tmp/app_test_4203.pid /tmp/app_test_4203.log
```

Expected: 422 (Pydantic validation error), not 200/500, and no multi-second hang from list allocation.

- [ ] **Step 6: Commit**

```bash
git add app/app.py
git commit -m "$(cat <<'EOF'
fix: bound DatasetSampleGenRequest.sample_index

Unbounded sample_index reached an unguarded
'while len(samples) <= request.sample_index: samples.append(...)' loop,
letting a single request with a huge sample_index allocate billions of
placeholder dicts - a memory-exhaustion DoS. Capped at 4999, generous
for any realistic dataset.
EOF
)"
```

---

## Task 6: Fix the remote-status cache key, invalidation, and race

**Files:** Modify `app/lmstudio_settings.py:175-193` and `apply_remote_lmstudio_settings` (~line 314-342)

**Context:** Three related bugs in the cache the previous pass added: (a) keyed by `ssh_alias` alone, so changing the configured `model_name` returns stale status for the old model; (b) never invalidated after `apply_remote_lmstudio_settings` (the "optimize" toggle) succeeds, so a poll right after can show pre-change state; (c) unguarded check-then-act, so concurrent requests whose cache entries expire simultaneously each fire their own SSH call instead of one populating it for the rest.

- [ ] **Step 1: Read the current cache code**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/.claude/worktrees/claude-md-rule-audit
sed -n '1,10p' app/lmstudio_settings.py
sed -n '175,193p' app/lmstudio_settings.py
```

Confirm `import threading` and `import time` status (time is almost certainly already imported; threading likely is not).

- [ ] **Step 2: Add `threading` to the imports if missing**

```python
import threading
```

- [ ] **Step 3: Rewrite the cache with a composite key, a lock, and an invalidation function**

```python
_remote_status_cache = {}  # (ssh_alias, model_name) -> (timestamp, status_dict)
_REMOTE_STATUS_CACHE_TTL = 10  # seconds - shorter than the UI's 30s poll interval
_remote_status_cache_lock = threading.Lock()

def get_remote_lmstudio_status_cached(ssh_alias, model_name, timeout=20):
    """Like get_remote_lmstudio_status, but reuses a result younger than
    _REMOTE_STATUS_CACHE_TTL seconds instead of making a fresh SSH round-trip.

    Multiple browser tabs each poll independently every 30s; without this,
    each poll across every open tab triggers its own live SSH call to the
    remote host just to refresh a status badge. This caps it to at most one
    SSH call per TTL window regardless of how many tabs are open.

    Keyed by (ssh_alias, model_name) - not just ssh_alias - so switching the
    configured model doesn't return a stale status computed for the
    previous one. The check-then-act is lock-protected so concurrent
    requests that all see an expired entry block on one real SSH call
    instead of each firing their own (this app configures one ssh_alias at
    a time, so the lock serializing unrelated keys too is an acceptable
    trade-off, not a real contention source).
    """
    key = (ssh_alias, model_name)
    with _remote_status_cache_lock:
        now = time.time()
        cached = _remote_status_cache.get(key)
        if cached and (now - cached[0]) < _REMOTE_STATUS_CACHE_TTL:
            return cached[1]
        status = get_remote_lmstudio_status(ssh_alias, model_name, timeout=timeout)
        _remote_status_cache[key] = (now, status)
        return status


def invalidate_remote_status_cache(ssh_alias=None):
    """Drop cached remote status so the next poll makes a fresh SSH call.

    Call this after any action that changes what 'lms ps' would report
    (e.g. apply_remote_lmstudio_settings) - otherwise a poll within the TTL
    window can show pre-change status right after a successful change.
    ssh_alias=None clears every cached entry; pass a specific alias to
    clear just that one.
    """
    with _remote_status_cache_lock:
        if ssh_alias is None:
            _remote_status_cache.clear()
        else:
            for key in [k for k in _remote_status_cache if k[0] == ssh_alias]:
                del _remote_status_cache[key]
```

- [ ] **Step 4: Call the invalidation from `apply_remote_lmstudio_settings` on success**

Read the function first:

```bash
grep -n "^def apply_remote_lmstudio_settings" app/lmstudio_settings.py
sed -n '314,343p' app/lmstudio_settings.py
```

Add the invalidation call right before the success return:

```python
    label = f"best ({REMOTE_IDEAL_SETTINGS['context_length']} ctx)" if ideal else "default"
    invalidate_remote_status_cache(ssh_alias)
    return True, f"Reloaded {model_name} on '{ssh_alias}' with {label} settings"
```

- [ ] **Step 5: Verify the cache-key fix directly**

```bash
python3 -c "
import sys, time
sys.path.insert(0, 'app')
import subprocess
call_log = []
def fake_ssh_run(ssh_alias, cmd, timeout=20, connect_timeout=10):
    call_log.append(ssh_alias)
    class FakeResult:
        stdout = '{}'
    return FakeResult()
import lmstudio_settings
lmstudio_settings._ssh_run = fake_ssh_run

lmstudio_settings.get_remote_lmstudio_status_cached('tnr-0', 'modelA')
lmstudio_settings.get_remote_lmstudio_status_cached('tnr-0', 'modelB')
print('calls for two different models, same alias (should be 2):', len(call_log))
"
```

- [ ] **Step 6: Verify invalidation works**

```bash
python3 -c "
import sys
sys.path.insert(0, 'app')
import subprocess
call_log = []
def fake_ssh_run(ssh_alias, cmd, timeout=20, connect_timeout=10):
    call_log.append(ssh_alias)
    class FakeResult:
        stdout = '{}'
    return FakeResult()
import lmstudio_settings
lmstudio_settings._ssh_run = fake_ssh_run

lmstudio_settings.get_remote_lmstudio_status_cached('tnr-0', 'modelA')
lmstudio_settings.get_remote_lmstudio_status_cached('tnr-0', 'modelA')
print('calls before invalidation (should be 1, second was cached):', len(call_log))
lmstudio_settings.invalidate_remote_status_cache('tnr-0')
lmstudio_settings.get_remote_lmstudio_status_cached('tnr-0', 'modelA')
print('calls after invalidation (should be 2):', len(call_log))
"
```

- [ ] **Step 7: Check syntax**

```bash
python3 -c "import ast; ast.parse(open('app/lmstudio_settings.py').read())" && echo "syntax OK"
```

- [ ] **Step 8: Commit**

```bash
git add app/lmstudio_settings.py
git commit -m "$(cat <<'EOF'
fix: remote-status cache key, invalidation, and check-then-act race

Three bugs in the cache added by the previous pass: keyed by ssh_alias
alone (switching model_name returned the previous model's stale
status), never invalidated after apply_remote_lmstudio_settings
succeeded (a poll right after a successful optimize could show
pre-change state), and an unguarded check-then-act (concurrent requests
whose entries expire simultaneously each fired their own SSH call).
Fixed all three: composite (ssh_alias, model_name) key, a lock around
the check-then-act, and an invalidate_remote_status_cache() call after
a successful settings change.
EOF
)"
```

---

## Task 7: Fix `partial` flag for non-bidirectional reviews

**Files:** Modify `app/app.py:2448-2449`

**Context:** `_combine_pass_stats`'s `partial` flag is `True` whenever an input stat dict is falsy. The per-book combine call always passes both `stats_fwd` and `stats_bwd`, but `stats_bwd` is legitimately always `None` for a non-bidirectional review (no backward pass ever runs) — not because anything crashed. Every book in every non-bidirectional review shows "(partial — not every pass completed)" even when it succeeded fully.

- [ ] **Step 1: Read the current call site**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/.claude/worktrees/claude-md-rule-audit
grep -n 'state\["tasks"\]\[i\]\["stats"\] = _combine_pass_stats' app/app.py
```

```python
                    state["tasks"][i]["stats"] = _combine_pass_stats(
                        state["tasks"][i].get("stats_fwd"), state["tasks"][i].get("stats_bwd"))
```

- [ ] **Step 2: Only combine in `stats_bwd` when the run is actually bidirectional**

```python
                    # Only combine stats_bwd in if this run is actually
                    # bidirectional - otherwise stats_bwd is never populated
                    # by design (no backward pass ever runs), and combining
                    # it in would mark every single-pass book "partial" even
                    # when that book's one-and-only pass succeeded cleanly.
                    pass_stats = [state["tasks"][i].get("stats_fwd")]
                    if state.get("bidirectional"):
                        pass_stats.append(state["tasks"][i].get("stats_bwd"))
                    state["tasks"][i]["stats"] = _combine_pass_stats(*pass_stats)
```

- [ ] **Step 3: Verify the logic directly**

```bash
python3 -c "
def _combine_pass_stats(*stat_dicts):
    combined = {'text_changed': 0}
    combined['partial'] = False
    for stats in stat_dicts:
        if not stats:
            combined['partial'] = True
            continue
        for key in combined:
            if key != 'partial':
                combined[key] += stats.get(key, 0)
    return combined

# Non-bidirectional: only stats_fwd passed
state_bidirectional = False
stats_fwd = {'text_changed': 5}
stats_bwd = None
pass_stats = [stats_fwd]
if state_bidirectional:
    pass_stats.append(stats_bwd)
result = _combine_pass_stats(*pass_stats)
print('non-bidirectional, successful fwd pass -> partial:', result['partial'])
assert result['partial'] is False

# Bidirectional, backward pass hasn't run yet
state_bidirectional = True
pass_stats = [stats_fwd]
if state_bidirectional:
    pass_stats.append(stats_bwd)
result2 = _combine_pass_stats(*pass_stats)
print('bidirectional, bwd not run yet -> partial:', result2['partial'])
assert result2['partial'] is True
print('OK')
"
```

- [ ] **Step 4: Check syntax**

```bash
python3 -c "import ast; ast.parse(open('app/app.py').read())" && echo "syntax OK"
```

- [ ] **Step 5: Commit**

```bash
git add app/app.py
git commit -m "$(cat <<'EOF'
fix: don't mark non-bidirectional reviews partial

_combine_pass_stats' partial flag was always True for every book in
every non-bidirectional review, since stats_bwd is never populated for
those by design (no backward pass runs), not because anything failed.
Only pass stats_bwd into the combine call when the run is actually
bidirectional.
EOF
)"
```

---

## Task 8: Close `lora_preview`'s remaining TOCTOU window

**Files:** Modify `app/app.py` (`lora_preview`, ~line 4561-4587)

**Context:** The previous pass moved `claim_gpu_task` earlier in both `lora_preview` and `lora_test_model` to close a TOCTOU race — but only `lora_test_model`'s adapter-download call actually got moved inside the claimed section. `lora_preview`'s download still runs before `check_global_gpu_lock`/`claim_gpu_task`.

- [ ] **Step 1: Read the current function**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/.claude/worktrees/claude-md-rule-audit
grep -n "^async def lora_preview" app/app.py
```

Read the full function body (roughly lines 4561-4633) before editing — it includes the cache-check-before-lock logic (a cache hit returns early with no GPU work, intentionally before the lock) which must stay in front of the download/claim reordering, not get swept into it.

- [ ] **Step 2: Move the download inside the claimed section, mirroring `lora_test_model`'s existing fix**

```python
async def lora_preview(adapter_id: str):
    """Generate or return cached preview audio for a LoRA adapter."""
    builtin = _load_builtin_lora_manifest()
    user_trained = _load_manifest(LORA_MODELS_MANIFEST)
    all_adapters = builtin + user_trained
    entry = next((m for m in all_adapters if m["id"] == adapter_id), None)
    if not entry:
        raise HTTPException(status_code=404, detail="Adapter not found")

    is_builtin = entry.get("builtin", False)
    if is_builtin:
        adapter_dir = os.path.join(BUILTIN_LORA_DIR, adapter_id)
        url_prefix = f"/builtin_lora/{adapter_id}"
    else:
        adapter_dir = os.path.join(LORA_MODELS_DIR, adapter_id)
        url_prefix = f"/lora_models/{adapter_id}"

    if not os.path.isdir(adapter_dir) and not is_builtin:
        raise HTTPException(status_code=404, detail="Adapter files not found")

    preview_path = os.path.join(adapter_dir, "preview_sample.wav")

    # Return cached if exists. This check intentionally runs BEFORE the lock -
    # no GPU/download work happens on a cache hit, regardless of whether the
    # adapter directory exists yet (a cached preview implies it does).
    if os.path.exists(preview_path):
        return {"status": "cached", "audio_url": f"{url_prefix}/preview_sample.wav"}

    # Cache miss past this point. Shares the "lora_test" slot with
    # /api/lora/test since both are "try out this adapter" operations that
    # shouldn't run concurrently with each other either. See F-040.
    check_global_gpu_lock("lora_test")
    # Claim immediately after the check, before the possible adapter download
    # AND the engine load below - both can take real time and the engine
    # load allocates VRAM, so the claim has to land before either starts, not
    # after, or two concurrent preview/test requests can both pass the check
    # above and both begin downloading/loading the model.
    claim_gpu_task("lora_test")
    try:
        if not os.path.isdir(adapter_dir) and is_builtin:
            try:
                download_builtin_adapter(adapter_id, BUILTIN_LORA_DIR)
                adapter_dir = os.path.join(BUILTIN_LORA_DIR, adapter_id)
            except Exception as e:
                logger.error(f"Auto-download failed for {adapter_id}: {e}")
                raise HTTPException(status_code=500, detail="Adapter auto-download failed — see server logs for details.")

        engine = project_manager.get_engine()
        if not engine:
            raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")

        voice_data = {
            "type": "lora",
            "adapter_id": adapter_id,
            "adapter_path": adapter_dir,
        }
        voice_config = {"_lora_preview_": voice_data}
        engine.generate_voice(
            text=LORA_PREVIEW_TEXT,
            instruct_text="",
            speaker="_lora_preview_",
            voice_config=voice_config,
            output_path=preview_path,
        )
        return {"status": "generated", "audio_url": f"{url_prefix}/preview_sample.wav"}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"LoRA preview generation failed: {e}")
        raise HTTPException(status_code=500, detail="LoRA preview generation failed — see server logs for details.")
    finally:
        process_state["lora_test"]["running"] = False
```

Note the change from the original's `if not os.path.isdir(adapter_dir) and is_builtin: ... elif not os.path.isdir(adapter_dir): raise 404` — the 404-for-non-builtin-missing-dir check is hoisted above the cache check (it's cheap, structural validation that should fail fast regardless of cache state), and the builtin-download-if-missing check moves inside the claimed `try`.

- [ ] **Step 3: Verify live — confirm 404s still work and download/engine init happen after the claim**

```bash
cd app
ALEXANDRIA_PORT=4203 /home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python3 app.py > /tmp/app_test_4203.log 2>&1 &
echo $! > /tmp/app_test_4203.pid
sleep 5
/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python3 -c "
import requests
r = requests.post('http://127.0.0.1:4203/api/lora/preview/_nonexistent_test_adapter')
print('nonexistent adapter:', r.status_code, r.json())
"
```

Expected: `404 {'detail': 'Adapter not found'}`, same as before the reorder.

- [ ] **Step 4: Run the full test suite for regressions, then stop the server**

```bash
/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python3 test_api.py --url http://127.0.0.1:4203 2>&1 | tail -15
kill $(cat /tmp/app_test_4203.pid) 2>/dev/null; rm -f /tmp/app_test_4203.pid /tmp/app_test_4203.log
```

Expected: same 5 pre-existing unrelated failures, no new ones.

- [ ] **Step 5: Commit**

```bash
git add app/app.py
git commit -m "$(cat <<'EOF'
fix: claim the lora_test GPU slot before lora_preview's download too

The previous pass moved lora_test_model's adapter download inside the
claimed section to close a TOCTOU race, but lora_preview's identical
download call was left before the claim - two concurrent preview
requests for an undownloaded builtin adapter could still both pass the
lock check and both start downloading before either's claim landed.
EOF
)"
```

---

## Task 9: Close `dataset_builder_generate_sample`'s TOCTOU window

**Files:** Modify `app/app.py` (`dataset_builder_generate_sample`, ~line 4736-4800)

**Context:** `get_engine()` (lazy `TTSEngine` init, allocates VRAM) runs before `claim_gpu_task("dataset_builder")` — the identical TOCTOU pattern Tasks 8/the previous pass fixed for `lora_preview`/`lora_test_model`, missed here even though this same function was touched by the previous pass (for the path-traversal fix).

- [ ] **Step 1: Read the current function** (already shown in Task 4's research — re-read live since Task 4 changed the model two fields above, not this function body)

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/.claude/worktrees/claude-md-rule-audit
grep -n "^async def dataset_builder_generate_sample" app/app.py
```

```python
async def dataset_builder_generate_sample(request: DatasetSampleGenRequest):
    """Generate a single dataset sample using VoiceDesign."""
    safe_name = secure_filename(request.dataset_name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid dataset name")

    # Same "dataset_builder" slot as the sibling /generate_batch route -
    # fail fast before any setup work below. See F-043.
    check_global_gpu_lock("dataset_builder")
    engine = project_manager.get_engine()
    if not engine:
        raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")

    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    os.makedirs(work_dir, exist_ok=True)

    claim_gpu_task("dataset_builder")
    try:
        wav_path, sr = engine.generate_voice_design(
```

- [ ] **Step 2: Move `get_engine()` inside the claimed `try`**

```python
async def dataset_builder_generate_sample(request: DatasetSampleGenRequest):
    """Generate a single dataset sample using VoiceDesign."""
    safe_name = secure_filename(request.dataset_name)
    if not safe_name:
        raise HTTPException(status_code=400, detail="Invalid dataset name")

    # Same "dataset_builder" slot as the sibling /generate_batch route -
    # fail fast before any setup work below. See F-043.
    check_global_gpu_lock("dataset_builder")

    work_dir = os.path.join(DATASET_BUILDER_DIR, safe_name)
    os.makedirs(work_dir, exist_ok=True)

    claim_gpu_task("dataset_builder")
    try:
        engine = project_manager.get_engine()
        if not engine:
            raise HTTPException(status_code=500, detail="Failed to initialize TTS engine")

        wav_path, sr = engine.generate_voice_design(
            description=request.description,
            sample_text=request.text,
            seed=request.seed,
        )
```

The rest of the `try`/`except`/`finally` block (dest_filename, state updates, error handling) is unchanged — only `engine = project_manager.get_engine()` and its `if not engine:` check move from before `claim_gpu_task` to the top of the `try` block. Add `except HTTPException: raise` before the existing `except Exception as e:` so the new in-`try` `HTTPException` (engine-init failure) isn't re-wrapped by the generic handler:

```python
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Dataset builder sample generation failed: {e}")
```

- [ ] **Step 3: Check syntax**

```bash
python3 -c "import ast; ast.parse(open('app/app.py').read())" && echo "syntax OK"
```

- [ ] **Step 4: Verify live**

```bash
cd app
ALEXANDRIA_PORT=4203 /home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python3 app.py > /tmp/app_test_4203.log 2>&1 &
echo $! > /tmp/app_test_4203.pid
sleep 5
/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python3 test_api.py --url http://127.0.0.1:4203 2>&1 | tail -15
kill $(cat /tmp/app_test_4203.pid) 2>/dev/null; rm -f /tmp/app_test_4203.pid /tmp/app_test_4203.log
```

Expected: same 5 pre-existing unrelated failures, no new ones.

- [ ] **Step 5: Commit**

```bash
git add app/app.py
git commit -m "$(cat <<'EOF'
fix: claim the dataset_builder GPU slot before get_engine()

get_engine() (lazy TTSEngine init, allocates VRAM) ran before
claim_gpu_task - the same TOCTOU pattern fixed for lora_preview/
lora_test_model elsewhere in this branch, missed in this function even
though the previous pass touched it for an unrelated path-traversal fix.
EOF
)"
```

---

## Task 10: Cap `secure_filename`'s output length

**Files:** Modify `app/utils.py:92-106`

**Context:** No length cap — a multi-thousand-character `dataset_name`/`name` passes through unchanged and can raise an uncaught `OSError: [Errno 36] File name too long` (the ~255-byte Linux filesystem component limit) inside `os.makedirs`/`open`, surfacing as an unhandled 500 instead of the intended 400.

- [ ] **Step 1: Write a reproduction script**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/.claude/worktrees/claude-md-rule-audit
python3 -c "
import sys
sys.path.insert(0, 'app')
from utils import secure_filename
result = secure_filename('a' * 5000)
print('length (currently unbounded):', len(result))
"
```

- [ ] **Step 2: Read the current function**

```python
def secure_filename(filename: str) -> str:
    """Sanitize a filename to prevent path-traversal attacks.

    Removes path separators and null bytes, keeps only safe characters.
    Returns empty string if the result would be unsafe.
    """
    if not filename:
        return ""
    for sep in ("/", "\\", "\0"):
        filename = filename.replace(sep, "_")
    filename = filename.lstrip(". ")
    filename = re.sub(r"[^\w\-. ]", "_", filename)
    if not filename:
        return ""
    return filename
```

- [ ] **Step 3: Cap the output length**

```python
def secure_filename(filename: str) -> str:
    """Sanitize a filename to prevent path-traversal attacks.

    Removes path separators and null bytes, keeps only safe characters,
    and caps length well under the ~255-byte filesystem component limit
    (leaving room for a caller-appended suffix, e.g. "sample_001.wav").
    """
    if not filename:
        return ""
    for sep in ("/", "\\", "\0"):
        filename = filename.replace(sep, "_")
    filename = filename.lstrip(". ")
    filename = re.sub(r"[^\w\-. ]", "_", filename)
    filename = filename[:150]
    if not filename:
        return ""
    return filename
```

- [ ] **Step 4: Verify the cap is applied**

```bash
python3 -c "
import sys
sys.path.insert(0, 'app')
from utils import secure_filename
result = secure_filename('a' * 5000)
print('length (should be <= 150):', len(result))
assert len(result) <= 150
print('normal name unaffected:', secure_filename('my_dataset_v2'))
"
```

- [ ] **Step 5: Commit**

```bash
git add app/utils.py
git commit -m "$(cat <<'EOF'
fix: cap secure_filename's output length

No length cap meant a multi-thousand-character name passed through
unchanged and could raise an uncaught OSError ("File name too long")
in a caller's os.makedirs/open, surfacing as a 500 instead of the
intended 400. Capped at 150 chars, safely under the ~255-byte
filesystem limit with room for an appended suffix.
EOF
)"
```

---

## Task 11: Restore returncode check and logging in `run_rocm_smi_json`

**Files:** Modify `gpu_stats.py`

**Context:** The shared function the previous pass centralized this into dropped two things the original per-script implementations had: a check for `subprocess.run(...).returncode == 0` before parsing stdout, and per-failure-type debug logging (`FileNotFoundError`, `TimeoutExpired`, JSON parse errors, non-zero exit). It now has only `except Exception: pass` with zero logging.

- [ ] **Step 1: Read the current file**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/.claude/worktrees/claude-md-rule-audit
cat gpu_stats.py
```

- [ ] **Step 2: Rewrite with the returncode check and logging restored**

```python
"""Shared rocm-smi JSON-parsing helper.

Lives at the repo root (not inside app/) so both the FastAPI app (via
app/utils.py's re-export) and the standalone root-level scripts
(alexandria_batch_processor.py, alexandria_preparer_rocm_compatible.py)
can import it without either side needing to reach across the app/
package boundary.
"""

import json
import logging
import subprocess

logger = logging.getLogger(__name__)


def run_rocm_smi_json(args, rocm_smi_path="rocm-smi", timeout=5):
    """Run `<rocm_smi_path> <args> --json` and return the parsed per-card dict, or None.

    Filters stdout down to JSON-looking lines first, since rocm-smi sometimes
    prints warnings to stdout ahead of the JSON payload. Returns None if the
    binary is missing, times out, exits non-zero, or produces no JSON.
    """
    try:
        result = subprocess.run(
            [rocm_smi_path] + list(args) + ["--json"],
            capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        logger.debug(f"{rocm_smi_path} not found")
        return None
    except subprocess.TimeoutExpired:
        logger.debug(f"{rocm_smi_path} timed out after {timeout}s")
        return None
    except Exception as e:
        logger.debug(f"{rocm_smi_path} unexpected error: {e}")
        return None

    if result.returncode != 0:
        logger.debug(f"{rocm_smi_path} returned error: {result.returncode}, stderr: {result.stderr}")
        return None

    # rocm-smi sometimes prints warnings to stdout ahead of the JSON, and
    # the JSON payload itself may be pretty-printed across several lines.
    # Parse everything from the first line that opens the JSON object so a
    # multi-line payload isn't truncated to just "{".
    lines = result.stdout.split('\n')
    for i, line in enumerate(lines):
        if line.strip().startswith('{'):
            try:
                return json.loads('\n'.join(lines[i:]))
            except (json.JSONDecodeError, ValueError) as e:
                logger.debug(f"{rocm_smi_path} JSON parse error: {e}")
                return None
    return None
```

- [ ] **Step 3: Check syntax**

```bash
python3 -c "import ast; ast.parse(open('gpu_stats.py').read())" && echo "syntax OK"
```

- [ ] **Step 4: Verify behavior on a real GPU (or confirm graceful None on a machine without rocm-smi)**

```bash
python3 -c "
import sys
sys.path.insert(0, '.')
from gpu_stats import run_rocm_smi_json
print(run_rocm_smi_json(['--showuse'], rocm_smi_path='/opt/rocm/bin/rocm-smi'))
"
```

Expected: either a populated dict (on a machine with ROCm) or `None` with a `DEBUG` log line explaining why (not a raised exception either way) — run with `python3 -u -c "import logging; logging.basicConfig(level=logging.DEBUG); ..."` prepended if the debug line itself needs to be seen.

- [ ] **Step 5: Re-run the two callers' own verification from the previous pass to confirm no regression**

```bash
/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python3 -c "
import sys
sys.path.insert(0, '.')
from alexandria_batch_processor import get_gpu_stats
print(get_gpu_stats())
"
```

- [ ] **Step 6: Commit**

```bash
git add gpu_stats.py
git commit -m "$(cat <<'EOF'
fix: restore returncode check and per-failure logging in run_rocm_smi_json

Centralizing this into a shared function dropped two things the
original per-script implementations had: a check for a non-zero
subprocess exit before parsing stdout, and per-failure-type debug
logging (not found / timeout / parse error / non-zero exit). It had
collapsed to a bare except Exception: pass with no logging at all.
EOF
)"
```

---

## Task 12: Document `extract_balanced`'s escape-scoping

**Files:** Modify `app/utils.py:21-29`

**Context:** `extract_balanced` only treats a backslash as an escape character while inside a string (`in_string=True`). The original `clean_json_string` bracket-loop it replaced set `escape_next=True` on *any* backslash, even outside strings — meaning a stray backslash right before a closing bracket outside any string (e.g. `[1, 2\]`) used to make `clean_json_string` skip over that bracket and fall through to its salvage path, whereas `extract_balanced` now correctly closes the bracket there. This is a real behavioral divergence for malformed-JSON edge cases — but it's also the *more correct* behavior (real JSON syntax has no escaping meaning outside strings), so the fix here is documenting the decision, not reverting it.

- [ ] **Step 1: Read the current docstring**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/.claude/worktrees/claude-md-rule-audit
sed -n '21,30p' app/utils.py
```

- [ ] **Step 2: Add the rationale**

```python
def extract_balanced(text, open_char, close_char):
    """Find the first `open_char ... close_char`-balanced span in `text`,
    tracking string-escaping so a quoted brace/bracket doesn't desync the
    depth count. Returns the matched substring, or None if `open_char`
    never appears or never balances back to depth 0.

    Shared by clean_json_string ([...]) and extract_json_object ({...}) -
    both need the same escape-aware bracket-matching, just for a different
    delimiter pair.

    A backslash only escapes the next character while inside a string
    (real JSON has no escape meaning outside one) - this is stricter than
    clean_json_string's original bracket-loop, which treated any backslash
    as an escape everywhere. That's intentional: it matches actual JSON
    semantics, so a stray unescaped backslash in malformed LLM output
    outside a string no longer causes a real closing bracket to be missed.
    """
```

- [ ] **Step 3: Commit**

```bash
git add app/utils.py
git commit -m "$(cat <<'EOF'
docs: explain extract_balanced's escape-scoping vs clean_json_string's original

A code review flagged this as a behavioral divergence from the
function it replaced (clean_json_string's bracket-loop escaped any
backslash everywhere; extract_balanced only escapes inside strings).
The new behavior matches actual JSON semantics and is the more correct
one - documenting it as a deliberate choice rather than leaving it to
be rediscovered as a surprise.
EOF
)"
```

---

## Task 13: Add logging to `safe_load_json` itself

**Files:** Modify `app/utils.py:111-119`

**Context:** The previous pass added `logger.warning` to 4 named call sites in `app.py` that each hand-roll `open`+`json.load`+`except`. The actual shared `safe_load_json()` helper — used by ~20 other call sites across `app/*.py` and root-level scripts — still silently returns `default` on corrupted JSON with zero trace. This is the root-cause fix the previous pass's own context predicted was needed but didn't do.

- [ ] **Step 1: Read the current function**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/.claude/worktrees/claude-md-rule-audit
sed -n '1,10p' app/utils.py
sed -n '109,120p' app/utils.py
```

- [ ] **Step 2: Add a module logger and use it**

```python
import os
import json
import time
import tempfile
import contextlib
import re
import subprocess
import sys
import logging

logger = logging.getLogger(__name__)
```

```python
def safe_load_json(path, default=None):
    """Load JSON from `path`, returning `default` if missing, empty, or corrupted."""
    if not os.path.exists(path):
        return default
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        logger.warning(f"Corrupted/unreadable JSON at {path}, using default: {e}")
        return default
```

(Place the `import logging`/`logger = ...` lines once near the top of the file, adjacent to the existing `gpu_stats` re-export block — not duplicated per-function.)

- [ ] **Step 3: Verify the warning fires**

```bash
python3 -c "
import sys, tempfile, os
sys.path.insert(0, 'app')
import logging
logging.basicConfig(level=logging.WARNING)
from utils import safe_load_json

with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
    f.write('not valid json {{{')
    path = f.name
result = safe_load_json(path, default={})
os.remove(path)
print('result:', result)
"
```

Expected: a `WARNING:utils:Corrupted/unreadable JSON at ...` line printed, and `result: {}`.

- [ ] **Step 4: Check syntax and run the full live test suite for regressions** (this function has ~20 callers — a syntax error here would break most of the app)

```bash
python3 -c "import ast; ast.parse(open('app/utils.py').read())" && echo "syntax OK"
cd app
ALEXANDRIA_PORT=4203 /home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python3 app.py > /tmp/app_test_4203.log 2>&1 &
echo $! > /tmp/app_test_4203.pid
sleep 5
tail -10 /tmp/app_test_4203.log
/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python3 test_api.py --url http://127.0.0.1:4203 2>&1 | tail -15
kill $(cat /tmp/app_test_4203.pid) 2>/dev/null; rm -f /tmp/app_test_4203.pid /tmp/app_test_4203.log
```

Expected: clean startup, same 5 pre-existing unrelated test failures, no new ones.

- [ ] **Step 5: Commit**

```bash
git add app/utils.py
git commit -m "$(cat <<'EOF'
fix: log corrupted JSON inside safe_load_json itself

The previous pass added logging at 4 named call sites in app.py that
each hand-roll open+json.load+except, but the shared safe_load_json()
helper underlying ~20 OTHER call sites across the codebase was still
silent. Adding the log here, at the root, covers all of them at once
instead of requiring every caller to be patched individually.
EOF
)"
```

---

## Task 14: Log `review_script_contextual`'s silent JSON swallows

**Files:** Modify `app/app.py:2199-2213`

**Context:** Same silent-swallow shape Task 18 (previous pass) fixed elsewhere in this file — two `except (json.JSONDecodeError, ...)` blocks here, defaulting `total_entries = 0` and `review_batch_size = 25`, with no `logger.warning` call.

- [ ] **Step 1: Read the current code**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/.claude/worktrees/claude-md-rule-audit
grep -n "^async def review_script_contextual" app/app.py
```

```python
    total_entries = 0
    try:
        with open(SCRIPT_PATH, "r", encoding="utf-8") as f:
            total_entries = len(json.load(f))
    except (json.JSONDecodeError, ValueError, OSError):
        total_entries = 0

    review_batch_size = 25
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                review_batch_size = max(1, int((cfg.get("generation") or {}).get("review_batch_size", 25)))
        except (json.JSONDecodeError, ValueError, TypeError, OSError):
            review_batch_size = 25
```

- [ ] **Step 2: Add logging to both**

```python
    total_entries = 0
    try:
        with open(SCRIPT_PATH, "r", encoding="utf-8") as f:
            total_entries = len(json.load(f))
    except (json.JSONDecodeError, ValueError, OSError) as e:
        logger.warning(f"Corrupted script at {SCRIPT_PATH}, estimated_calls will read 0: {e}")
        total_entries = 0

    review_batch_size = 25
    if os.path.exists(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                cfg = json.load(f)
                review_batch_size = max(1, int((cfg.get("generation") or {}).get("review_batch_size", 25)))
        except (json.JSONDecodeError, ValueError, TypeError, OSError) as e:
            logger.warning(f"Corrupted config at {CONFIG_PATH}, using default review_batch_size: {e}")
            review_batch_size = 25
```

- [ ] **Step 3: Check syntax**

```bash
python3 -c "import ast; ast.parse(open('app/app.py').read())" && echo "syntax OK"
```

- [ ] **Step 4: Commit**

```bash
git add app/app.py
git commit -m "$(cat <<'EOF'
fix: log review_script_contextual's silent JSON-swallow sites

Same shape the previous pass fixed for get_voices/save_voice_config/
etc. in this same file - just two more sites it missed.
EOF
)"
```

---

## Task 15: Migrate Dataset Builder actions to toast-on-failure

**Files:** Modify `app/static/index.html` (`dsbCancel` ~line 6022, `dsbSaveForm` ~line 5715, `dsbSaveRows` ~line 5729)

**Context:** Task 19 (previous pass) added a shared `cancelTask` helper and migrated 9 `window.cancel*`-prefixed buttons onto it so every cancel action toasts on failure instead of `console.error`-only. The Dataset Builder subsystem's own actions — named `dsb*`, not `window.cancel*` — were missed and still fail silently.

- [ ] **Step 1: Read the current code**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/.claude/worktrees/claude-md-rule-audit
grep -n "function dsbSaveForm\|function dsbSaveRows\|window.dsbCancel" app/static/index.html
```

```javascript
window.dsbCancel = async () => {
    try {
        await API.post('/api/dataset_builder/cancel', {});
    } catch (e) { console.error('Cancel error:', e); }
};

function dsbSaveForm() {
    if (!dsbCurrentProject) { return; }
    clearTimeout(dsbSaveMetaTimer);
    dsbSaveMetaTimer = setTimeout(async () => {
        try {
            await API.post('/api/dataset_builder/update_meta', {
                name: dsbCurrentProject,
                description: document.getElementById('dsb-description').value,
                global_seed: document.getElementById('dsb-global-seed').value,
            });
        } catch (e) { console.error('Failed to save meta:', e); }
    }, 500);
}

function dsbSaveRows() {
    if (!dsbCurrentProject) { return; }
    clearTimeout(dsbSaveRowsTimer);
    dsbSaveRowsTimer = setTimeout(async () => {
        try {
            await API.post('/api/dataset_builder/update_rows', {
                name: dsbCurrentProject,
                rows: dsbRows.map(r => ({ emotion: r.emotion || '', text: (r.text || '').trim(), seed: r.seed ?? '' })),
            });
        } catch (e) { console.error('Failed to save rows:', e); }
    }, 500);
}
```

- [ ] **Step 2: Migrate `dsbCancel` onto `cancelTask`** (its body matches `cancelTask`'s exact shape — empty-body POST, no extra args)

```javascript
window.dsbCancel = () => cancelTask('/api/dataset_builder/cancel');
```

- [ ] **Step 3: Add a toast directly to `dsbSaveForm`/`dsbSaveRows`** (these POST a real body, which `cancelTask` doesn't support — it always posts `{}` — so add the toast inline rather than force-fitting them into that helper)

```javascript
function dsbSaveForm() {
    if (!dsbCurrentProject) { return; }
    clearTimeout(dsbSaveMetaTimer);
    dsbSaveMetaTimer = setTimeout(async () => {
        try {
            await API.post('/api/dataset_builder/update_meta', {
                name: dsbCurrentProject,
                description: document.getElementById('dsb-description').value,
                global_seed: document.getElementById('dsb-global-seed').value,
            });
        } catch (e) {
            showToast('Failed to save meta: ' + (e.message || 'unknown error'), 'warning');
        }
    }, 500);
}

function dsbSaveRows() {
    if (!dsbCurrentProject) { return; }
    clearTimeout(dsbSaveRowsTimer);
    dsbSaveRowsTimer = setTimeout(async () => {
        try {
            await API.post('/api/dataset_builder/update_rows', {
                name: dsbCurrentProject,
                rows: dsbRows.map(r => ({ emotion: r.emotion || '', text: (r.text || '').trim(), seed: r.seed ?? '' })),
            });
        } catch (e) {
            showToast('Failed to save rows: ' + (e.message || 'unknown error'), 'warning');
        }
    }, 500);
}
```

- [ ] **Step 4: Verify JS syntax**

```bash
grep -n "^    </script>$" app/static/index.html | tail -1
```

Use the line number from that grep as the upper bound in the next command (it shifts as edits accumulate):

```bash
sed -n '1906,<CLOSING_LINE_MINUS_1>p' app/static/index.html > /tmp/_index_check.js
/home/fakemitch/pinokio/bin/miniconda/bin/node --check /tmp/_index_check.js && echo "syntax OK"
rm -f /tmp/_index_check.js
```

- [ ] **Step 5: Commit**

```bash
git add app/static/index.html
git commit -m "$(cat <<'EOF'
fix: toast on failure for Dataset Builder cancel/autosave actions

Task 19 (previous pass) migrated 9 window.cancel*-prefixed buttons onto
the shared cancelTask helper specifically so cancel actions toast
instead of failing silently. The Dataset Builder subsystem's own
actions (dsbCancel, dsbSaveForm, dsbSaveRows) are named dsb*, not
window.cancel*, and were missed - same silent-failure shape, different
naming convention.
EOF
)"
```

---

## Task 16: Migrate `dedupe_speakers` and restore lost diagnostics

**Files:** Modify `app/review_script.py` (`dedupe_speakers`, ~line 412-429), `app/find_nicknames.py` (`_parse_alias_response`, ~line 134-141)

**Context:** Two related findings. `dedupe_speakers` still uses the old greedy `re.search(r"\{.*\}")` instead of the shared `extract_json_object` this same diff added (and migrated `find_nicknames.py` onto). But migrating naively would reintroduce the *other* finding: `extract_json_object` catches `JSONDecodeError` internally and returns `None`, so callers that used to rely on a raised exception to log a diagnostic ("LLM step failed...") now silently get an empty result with no trace. Fix both at once: migrate `dedupe_speakers`, and add an explicit "parse failed" log to both functions at the point `extract_json_object` returns `None`.

- [ ] **Step 1: Read `find_nicknames.py`'s current `_parse_alias_response`**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/.claude/worktrees/claude-md-rule-audit
grep -n "^def _parse_alias_response" app/find_nicknames.py
```

```python
def _parse_alias_response(raw, speakers):
    """Normalize one LLM response into (aliases, evidence) maps.

    Resolves model casing back to the real speaker label, drops self/NARRATOR/
    group mappings, and keeps only variants that actually appear as a label.
    """
    data = extract_json_object(raw) or {}
    raw_aliases = data.get("aliases", data) if isinstance(data, dict) else {}
```

- [ ] **Step 2: Restore the lost diagnostic in `find_nicknames.py`**

```python
def _parse_alias_response(raw, speakers):
    """Normalize one LLM response into (aliases, evidence) maps.

    Resolves model casing back to the real speaker label, drops self/NARRATOR/
    group mappings, and keeps only variants that actually appear as a label.
    """
    data = extract_json_object(raw)
    if data is None:
        print(f"  Warning: could not parse a JSON object from the LLM's alias "
              f"response ({len(raw)} chars); treating as no aliases found.")
        data = {}
    raw_aliases = data.get("aliases", data) if isinstance(data, dict) else {}
```

- [ ] **Step 3: Read `review_script.py`'s current `dedupe_speakers`**

```bash
grep -n "^def dedupe_speakers" app/review_script.py
grep -n "^from utils import" app/review_script.py
```

```python
        raw = response.choices[0].message.content or ""
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        mapping = json.loads(m.group(0)) if m else {}
    except Exception as e:
        # LLM unavailable — still apply the known aliases from the file
        print(f"  Speaker dedupe LLM step failed ({e}); applying known aliases only.")
        if not forced_map:
            return {}, 0, []
```

```python
from utils import file_lock, atomic_json_write, safe_load_json, run_rocm_smi_json
```

- [ ] **Step 4: Migrate and restore the diagnostic**

```python
from utils import file_lock, atomic_json_write, safe_load_json, run_rocm_smi_json, extract_json_object
```

```python
        raw = response.choices[0].message.content or ""
        mapping = extract_json_object(raw)
        if mapping is None:
            print(f"  Warning: could not parse a JSON object from the LLM's "
                  f"speaker-merge response ({len(raw)} chars); applying known aliases only.")
            mapping = {}
    except Exception as e:
        # LLM unavailable — still apply the known aliases from the file
        print(f"  Speaker dedupe LLM step failed ({e}); applying known aliases only.")
        if not forced_map:
            return {}, 0, []
```

- [ ] **Step 5: Verify both behave correctly on a malformed-JSON input**

```bash
python3 -c "
import sys
sys.path.insert(0, 'app')
from utils import extract_json_object

raw = 'Sure! Here you go: not actually json at all'
result = extract_json_object(raw)
print('extract_json_object on garbage (should be None):', result)
assert result is None
print('OK')
"
```

- [ ] **Step 6: Check syntax**

```bash
python3 -c "import ast; ast.parse(open('app/review_script.py').read()); ast.parse(open('app/find_nicknames.py').read())" && echo "syntax OK"
```

- [ ] **Step 7: Commit**

```bash
git add app/review_script.py app/find_nicknames.py
git commit -m "$(cat <<'EOF'
fix: migrate dedupe_speakers to extract_json_object, restore lost diagnostics

dedupe_speakers (review_script.py) still used the old greedy
{.*}-regex instead of the shared extract_json_object this branch
added elsewhere. Migrating it naively would reintroduce a different
bug already found in find_nicknames.py: extract_json_object swallows
JSONDecodeError internally and returns None, so a caller that used to
rely on a raised exception to log "parse failed" now silently gets an
empty result with no trace. Fixed both functions together: migrate
dedupe_speakers, and add an explicit log at the None-check in both
functions so malformed LLM output is never silently invisible.
EOF
)"
```

---

## Task 17: Await `onTick` in `_startPolling`

**Files:** Modify `app/static/index.html` (`_startPolling`, ~line 4803-4831)

**Context:** `_runBatchRender`'s `onTick` callback is `async () => { await loadChunks(false); }`. `_startPolling`'s `tick()` calls `if (onTick) { onTick(data); }` without `await`-ing it, so `doneCheck`/`onDone` can run while the previous tick's `loadChunks(false)` is still in flight — risking two overlapping DOM refreshes. Fixing the shared engine (rather than special-casing `_runBatchRender`) benefits every current and future async `onTick` caller, with no behavior change for the existing synchronous ones (`await`-ing a non-promise value just resolves on the next microtask).

- [ ] **Step 1: Read the current code**

```bash
cd /home/fakemitch/pinokio/api/alexandria-audiobook2.git/.claude/worktrees/claude-md-rule-audit
grep -n "function _startPolling" app/static/index.html
```

```javascript
        function _startPolling(key, fetchFn, { intervalMs = 1000, doneCheck, onTick, onDone } = {}) {
            const myGen = (_pollGen[key] = (_pollGen[key] || 0) + 1);
            let consecutiveErrors = 0;
            const MAX_SILENT_ERRORS = 3;
            const tick = async () => {
                if (myGen !== _pollGen[key]) { return; }
                try {
                    const data = await fetchFn();
                    if (myGen !== _pollGen[key]) { return; }
                    consecutiveErrors = 0;
                    if (onTick) { onTick(data); }
                    if (doneCheck(data)) {
                        if (onDone) { onDone(data); }
                        return;
                    }
                } catch (e) {
```

- [ ] **Step 2: Await `onTick`**

```javascript
                    if (onTick) { await onTick(data); }
```

- [ ] **Step 3: Verify JS syntax**

```bash
grep -n "^    </script>$" app/static/index.html | tail -1
```

Use that line number minus 1 as the upper bound:

```bash
sed -n '1906,<CLOSING_LINE_MINUS_1>p' app/static/index.html > /tmp/_index_check.js
/home/fakemitch/pinokio/bin/miniconda/bin/node --check /tmp/_index_check.js && echo "syntax OK"
rm -f /tmp/_index_check.js
```

- [ ] **Step 4: Commit**

```bash
git add app/static/index.html
git commit -m "$(cat <<'EOF'
fix: await onTick in _startPolling

_runBatchRender's onTick (async () => { await loadChunks(false); }) had
its promise silently dropped, since tick() called onTick(data) without
awaiting it - doneCheck/onDone could run while the previous tick's DOM
refresh was still in flight. Fixed in the shared polling engine, not
just _runBatchRender, so every current and future async onTick caller
gets the fix; no behavior change for today's synchronous callbacks.
EOF
)"
```

---

## Self-Review

1. **Spec coverage** — all 20 findings from the code review map to one of the 17 tasks above (findings #5/#10/#11 folded into Tasks 3/6 respectively, noted explicitly in the Findings Index).
2. **Placeholder scan** — every task has concrete file paths, current-state code read from the actual files, and complete before/after diffs. No "add appropriate handling"-style gaps.
3. **Type/identifier consistency** — `_validate_voicelab_path`/`_is_inside` (Task 3) are defined once and used identically in both call sites (Task 3 itself, no cross-task reference). `invalidate_remote_status_cache` (Task 6) is defined and called within the same task. `extract_json_object` (Task 16) keeps its existing signature from `app/utils.py` — no changes to it in this plan, only new callers.

One ordering note: **Task 3's Step 3 already includes Task 5's fix** (the `updates.get(...)` truthy-check rewrite) at the same lines — do not apply a separate edit for finding #5, it would conflict with Task 3's diff.

---

## Execution

Executing directly in this session (no subagent dispatch) — tasks in order 1 through 17, each: implement → verify (repro script and/or live server + `test_api.py`) → commit. Same approach as the previous remediation plan.

