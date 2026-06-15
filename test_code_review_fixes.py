"""
Regression tests for the 10 findings fixed after the [max] code-review of the
night-super-night-themes branch (review_script.py / app.py):

  1. lmstudio_status()/lmstudio_optimize() wrap the blocking
     get_lmstudio_status()/apply_lmstudio_settings() calls in
     asyncio.to_thread() so they don't block the event loop.
  2. (covered indirectly via _extract_review_stats) a VRAM-aborted book's
     stats now include batches_skipped_vram so _process_book can mark it
     "incomplete" instead of "done".
  4. /api/merge's background task sets process_state["audio"]["start_time"].
  6. _write_batch_review_report's bidirectional "done" count requires both
     stats_fwd and stats_bwd, not the bare (last-pass-wins) "stats" key.
  33. _compute_eta skips per-book banner lines (---/>>>/===) that would
      otherwise be mistaken for "current/total" sub-batch progress.
  35. _extract_review_stats returns None (rather than partial/garbage stats)
      when the "Review complete: X -> Y entries" line is absent.
  37. review_script.py's checkpoint resume uses resume_offset =
      len(all_corrected) instead of completed_batches * batch_size, so
      entry-count drift (splits/merges in earlier batches) doesn't cause
      duplicated or dropped entries on resume. load_checkpoint() no longer
      discards a checkpoint just because total_batches drifted, as long as
      batch_size/context_window still match.
  38. _compute_eta clamps its fraction to [0, 1] so a stale/edge
      current_task_idx can't produce a negative ETA.

Run with:
  python test_code_review_fixes.py
  # or from project root:
  # app/env/bin/python test_code_review_fixes.py
"""
import asyncio
import json
import os
import sys
import tempfile
import time
from unittest import mock

from fastapi import BackgroundTasks

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import review_script
import app as app_module

FAILURES = []


def check(label, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {label}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        FAILURES.append(label)


def _stats(**overrides):
    base = {"text_changed": 0, "speaker_changed": 0, "instruct_changed": 0,
            "entries_added": 0, "entries_removed": 0, "batches_failed": 0,
            "batches_skipped_vram": 0}
    base.update(overrides)
    return base


def _full_stats(**overrides):
    base = {"entries_before": 10, "entries_after": 10,
            "text_changed": 0, "speaker_changed": 0, "instruct_changed": 0,
            "entries_added": 0, "entries_removed": 0,
            "narrators_merged": 0, "speakers_merged": 0,
            "batches_failed": 0, "batches_skipped_vram": 0,
            "total_changes": 0}
    base.update(overrides)
    return base


# ── Fix #33 / #38: _compute_eta ──────────────────────────────────────────────

def test_compute_eta_skips_banner_lines():
    print("\n=== _compute_eta skips per-book banner lines that look like progress ===")
    for prefix in ("---", ">>>", "==="):
        state = {
            "start_time": time.time() - 50,
            "logs": [
                "some earlier line",
                "Reviewing batch 2/50",
                f"{prefix} [4/10] Reviewing 'Some Book' {prefix}",
            ],
        }
        result = app_module._compute_eta(state)
        check(f"'{prefix}'-prefixed banner is skipped, falls back to the real sub-progress",
              result["progress"] == "2/50", result["progress"])
        check(f"fraction derived from the line before the '{prefix}' banner",
              result["fraction"] == 2 / 50, result["fraction"])


def test_compute_eta_clamps_fraction_to_unit_range():
    print("\n=== _compute_eta clamps fraction to [0, 1] (no negative ETA) ===")
    state = {
        "start_time": time.time() - 100,
        "logs": ["Reviewing batch 5/10"],
        "tasks": [{}, {}, {}],
        "current_task_idx": 3,  # one past the last valid index — edge case
    }
    result = app_module._compute_eta(state)
    check("fraction clamped to 1.0 (raw value would be >1)",
          result["fraction"] == 1.0, result["fraction"])
    check("eta_seconds is not negative once fraction is clamped",
          result["eta_seconds"] is not None and result["eta_seconds"] >= 0,
          result["eta_seconds"])


# ── Fix #35: _extract_review_stats ──────────────────────────────────────────

def test_extract_review_stats_parses_batches_skipped_vram():
    print("\n=== _extract_review_stats parses 'Batches skipped (low GPU VRAM)' ===")
    lines = [
        "Loaded 100 script entries for review",
        "Reviewing batch 1/4 (25 entries)...",
        "  WARNING: GPU VRAM at 92% (11.0/12.0 GB) - pausing to avoid an OOM crash...",
        "  VRAM still at 92% after 120s - stopping early to avoid a crash. Progress so far will be saved.",
        "============================================================",
        "Review complete: 100 -> 95 entries",
        "  Text changed:    3",
        "  Speaker changed: 1",
        "  Instruct changed:0",
        "  Entries added:   0",
        "  Entries removed: 5",
        "  Narrators merged:0",
        "  Speakers merged: 0",
        "  Batches skipped (low GPU VRAM): 2",
        "  Total changes:   9",
        "============================================================",
    ]
    stats = app_module._extract_review_stats(lines)
    check("stats parsed (not None)", stats is not None)
    check("entries_before/after parsed", stats["entries_before"] == 100 and stats["entries_after"] == 95, stats)
    check("batches_skipped_vram parsed as 2", stats["batches_skipped_vram"] == 2, stats)
    check("batches_skipped_vram > 0 (the condition _process_book checks)",
          stats.get("batches_skipped_vram", 0) > 0)


def test_extract_review_stats_returns_none_without_summary_line():
    print("\n=== _extract_review_stats returns None when the summary line is missing ===")
    lines = [
        "Loaded 10 script entries for review",
        "Reviewing batch 1/1 (10 entries)...",
        "Traceback (most recent call last):",
        "RuntimeError: boom",
    ]
    stats = app_module._extract_review_stats(lines)
    check("returns None (no 'Review complete: X -> Y entries' line)", stats is None, stats)


# ── Fix #6: _write_batch_review_report bidirectional "done" count ──────────

def test_write_batch_review_report_bidirectional_done_requires_both_passes():
    print("\n=== _write_batch_review_report: bidirectional 'done' requires stats_fwd AND stats_bwd ===")
    book_one_stats_fwd = _full_stats(total_changes=2, text_changed=2)
    book_one_stats_bwd = _full_stats(total_changes=1, speaker_changed=1)
    # book_two only completed its forward pass before the run was cancelled.
    # The bare "stats" key (last-pass-wins) is still set, exactly as
    # _process_book sets it after the forward pass.
    book_two_stats_fwd = _full_stats(total_changes=1, text_changed=1)

    state = {
        "tasks": [
            {"name": "book_one", "status": "done",
             "stats_fwd": book_one_stats_fwd, "stats_bwd": book_one_stats_bwd,
             "stats": book_one_stats_bwd},
            {"name": "book_two", "status": "cancelled",
             "stats_fwd": book_two_stats_fwd,
             "stats": book_two_stats_fwd},
        ],
        "totals_fwd": app_module._new_review_totals(),
        "totals_bwd": app_module._new_review_totals(),
        "diff_pool": {"text": [], "speaker": []},
        "aliases_fwd": [], "aliases_bwd": [],
    }
    for key in state["totals_fwd"]:
        if key == "books_done":
            continue
        state["totals_fwd"][key] += book_one_stats_fwd.get(key, 0) + book_two_stats_fwd.get(key, 0)
        state["totals_bwd"][key] += book_one_stats_bwd.get(key, 0)

    path = None
    try:
        # Avoid a real LLM call (and any contention with an in-progress run).
        with mock.patch.object(app_module, "_llm_summarize_report", return_value=None):
            path = app_module._write_batch_review_report(state, ["book_one", "book_two"],
                                                           bidirectional=True, discover=False)
        check("report written", path is not None and os.path.exists(path or ""))
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        check("reports '1 of 2 books finished' (only book_one finished both passes)",
              "1 of 2" in content, content)
        check("does NOT report '2 of 2' (the pre-fix bare-'stats' count)",
              "2 of 2" not in content, content)
    finally:
        if path and os.path.exists(path):
            os.remove(path)


# ── Fix #1: lmstudio endpoints don't block the event loop ──────────────────

def test_lmstudio_status_does_not_block_event_loop():
    print("\n=== lmstudio_status() runs the blocking call via asyncio.to_thread ===")

    def blocking_status(model_name):
        time.sleep(0.3)
        return {"available": True, "loaded": True, "context_length": 8192,
                "parallel": 1, "optimized": True}

    counter = {"n": 0}

    async def ticker():
        for _ in range(10):
            await asyncio.sleep(0.03)
            counter["n"] += 1

    async def run():
        with mock.patch.object(app_module, "_get_llm_model_name", return_value="test-model"), \
             mock.patch.object(app_module, "get_lmstudio_status", side_effect=blocking_status):
            await asyncio.gather(app_module.lmstudio_status(), ticker())

    asyncio.run(run())
    check("event loop kept ticking during the blocking lmstudio_status call",
          counter["n"] >= 5, counter["n"])


def test_lmstudio_optimize_does_not_block_event_loop():
    print("\n=== lmstudio_optimize() runs blocking calls via asyncio.to_thread ===")

    def blocking_apply(model_name, ideal):
        time.sleep(0.2)
        return True, "ok"

    def blocking_status(model_name):
        time.sleep(0.1)
        return {"available": True, "loaded": True, "context_length": 8192,
                "parallel": 1, "optimized": True}

    counter = {"n": 0}

    async def ticker():
        for _ in range(10):
            await asyncio.sleep(0.02)
            counter["n"] += 1

    async def run():
        with mock.patch.object(app_module, "_get_llm_model_name", return_value="test-model"), \
             mock.patch.object(app_module, "apply_lmstudio_settings", side_effect=blocking_apply), \
             mock.patch.object(app_module, "get_lmstudio_status", side_effect=blocking_status):
            req = app_module.LMStudioOptimizeRequest(enable=True)
            await asyncio.gather(app_module.lmstudio_optimize(req), ticker())

    asyncio.run(run())
    check("event loop kept ticking during the blocking apply/status calls",
          counter["n"] >= 5, counter["n"])


# ── Fix #4: /api/merge sets start_time ──────────────────────────────────────

def test_merge_endpoint_sets_start_time():
    print("\n=== /api/merge's background task sets process_state['audio']['start_time'] ===")
    state = app_module.process_state["audio"]
    state["start_time"] = None
    state["running"] = False

    async def run():
        with mock.patch.object(app_module.project_manager, "merge_audio", return_value=(True, "ok")):
            bg = BackgroundTasks()
            await app_module.merge_audio_endpoint(bg)
            for task in bg.tasks:
                result = task.func(*task.args, **task.kwargs)
                if asyncio.iscoroutine(result):
                    await result

    asyncio.run(run())
    check("start_time set by the merge task", state["start_time"] is not None, state["start_time"])
    check("running reset to False once the task finishes", state["running"] is False)


# ── Fix #37: checkpoint resume survives entry-count drift ───────────────────

def test_load_checkpoint_survives_total_batches_drift():
    print("\n=== load_checkpoint: total_batches drift alone doesn't discard the checkpoint ===")
    with tempfile.TemporaryDirectory() as d:
        output_path = os.path.join(d, "script.json")
        all_corrected = [{"speaker": "NARRATOR", "text": f"e{i}", "instruct": f"t{i}"} for i in range(4)]
        review_script.save_checkpoint(output_path, 2, 3, 2, 0, all_corrected,
                                       _stats(), all_corrected[-2:], [2, 2], [])

        # Caller now computes a different total_batches estimate (e.g. earlier
        # batches added entries, shifting the total). batch_size/context_window
        # still match, so the checkpoint must survive.
        loaded = review_script.load_checkpoint(output_path, 5, 2, 0)
        check("checkpoint survives a total_batches mismatch", loaded is not None)
        check("completed_batches preserved despite drift", loaded["completed_batches"] == 2, loaded)
        check("all_corrected preserved despite drift", loaded["all_corrected"] == all_corrected)


def test_load_checkpoint_discarded_on_batch_size_change():
    print("\n=== load_checkpoint: batch_size change still discards the checkpoint ===")
    with tempfile.TemporaryDirectory() as d:
        output_path = os.path.join(d, "script.json")
        all_corrected = [{"speaker": "NARRATOR", "text": f"e{i}", "instruct": f"t{i}"} for i in range(4)]
        review_script.save_checkpoint(output_path, 2, 3, 2, 0, all_corrected,
                                       _stats(), all_corrected[-2:], [2, 2], [])

        loaded = review_script.load_checkpoint(output_path, 3, 5, 0)
        check("checkpoint discarded when batch_size changes (unsafe to mix mid-run)",
              loaded is None, loaded)


def test_main_resume_avoids_duplication_with_entry_count_drift():
    print("\n=== main() resumes from len(all_corrected), avoiding duplication after drift ===")
    with tempfile.TemporaryDirectory() as d:
        script_path = os.path.join(d, "script.json")

        def entry(tag, i):
            return {"speaker": "NARRATOR", "text": f"{tag} sentence {i}.", "instruct": f"tone_{tag}_{i}"}

        # Batch 1 (originally 25 entries) was reviewed and a split brought it
        # to 26 entries. Batch 2 (25 entries) was reviewed unchanged.
        all_corrected = ([entry("b1", i) for i in range(26)] +
                         [entry("b2", i) for i in range(25)])  # 51 entries total
        unreviewed_remainder = [entry("rem", i) for i in range(10)]
        entries_on_disk = all_corrected + unreviewed_remainder  # 61 entries

        with open(script_path, "w", encoding="utf-8") as f:
            json.dump(entries_on_disk, f)

        # Checkpoint from a previous run: 2 batches completed (51 entries due
        # to the split), total_batches matches the fresh estimate
        # (ceil(61/25) == 3) so this test isolates the resume-offset fix
        # rather than the load_checkpoint gating fix.
        review_script.save_checkpoint(
            script_path, 2, 3, 25, 0, all_corrected,
            _stats(entries_added=1), all_corrected[-2:], [26, 25], [])

        argv_backup = sys.argv
        sys.argv = ["review_script.py", "--input", script_path, "--output", script_path]
        try:
            with mock.patch.object(review_script, "apply_lmstudio_settings", return_value=(True, "ok")), \
                 mock.patch.object(review_script, "get_lmstudio_status",
                                    return_value={"loaded": True, "optimized": True}), \
                 mock.patch.object(review_script, "wait_for_vram_headroom", return_value=True), \
                 mock.patch.object(review_script, "OpenAI", return_value=mock.Mock()), \
                 mock.patch.object(review_script, "review_batch", side_effect=lambda *a, **k: a[2]):
                review_script.main()
        finally:
            sys.argv = argv_backup

        with open(script_path, "r", encoding="utf-8") as f:
            output_entries = json.load(f)

        check("output has exactly 61 entries (51 already-reviewed + 10 newly-reviewed)",
              len(output_entries) == 61, len(output_entries))
        check("output matches input exactly — no duplicated/dropped entries from a drifted resume offset",
              output_entries == entries_on_disk,
              f"got {len(output_entries)} entries vs {len(entries_on_disk)} expected")
        check("checkpoint cleared after a clean completion",
              review_script.load_checkpoint(script_path, 99, 25, 0) is None)


def main():
    test_compute_eta_skips_banner_lines()
    test_compute_eta_clamps_fraction_to_unit_range()
    test_extract_review_stats_parses_batches_skipped_vram()
    test_extract_review_stats_returns_none_without_summary_line()
    test_write_batch_review_report_bidirectional_done_requires_both_passes()
    test_lmstudio_status_does_not_block_event_loop()
    test_lmstudio_optimize_does_not_block_event_loop()
    test_merge_endpoint_sets_start_time()
    test_load_checkpoint_survives_total_batches_drift()
    test_load_checkpoint_discarded_on_batch_size_change()
    test_main_resume_avoids_duplication_with_entry_count_drift()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} check(s) FAILED: {FAILURES}")
    else:
        print("RESULT: all checks passed")
    print("=" * 60)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
