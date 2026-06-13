"""
Regression tests for the 10 findings fixed after the [high] code-review of the
night-super-night-themes branch (app.py / review_script.py / utils.py / index.html):

  1. _process_book clears any stale review checkpoint for script_path before
     invoking review_script.py for each pass, so a hindsight (backward) pass
     can't silently "resume" a forward pass's VRAM-abort checkpoint and skip
     re-reviewing entries.
  2. reattachRunningPollers() restores review/nicknames button + control state
     and re-attaches the same onDone callback used by a live run, after a page
     reload.
  3. utils.file_lock() provides an advisory cross-process lock (with stale-lock
     cleanup), used by voice_library_apply_bulk and
     review_script._remap_voice_config so they can't race on the same
     scripts/{name}.voice_config.json.
  4. _compute_eta's progress-scan keeps looking for an earlier valid "cur/tot"
     marker when the most recent match has cur == 0 or cur > tot.
  5. _write_batch_review_report's "done" bucket requires status == "done", so a
     VRAM-"incomplete" book with stats isn't counted as fully done.
  6. _combine_pass_stats sums forward + backward pass stats for the per-book
     badge tooltip ("stats" key), instead of last-pass-wins.
  7. _write_batch_review_report's "stopped early" note appears even if the run
     was cancelled before any book finished a pass (state["cancel"], not just a
     task with status == "cancelled").
  8. _apply_cast_mapping is the single shared implementation used by both
     voice_library_apply and voice_library_apply_bulk.
 10. review_script._load_resume_state centralizes main()'s checkpoint-load +
     resume_offset bookkeeping for both the contextual and non-contextual
     review loops.

Run with:
  /home/fakemitch/pinokio/api/alexandria-audiobook.git/app/env/bin/python test_high_review_fixes.py
"""
import asyncio
import json
import os
import re
import sys
import tempfile
import threading
import time
from unittest import mock

from fastapi import BackgroundTasks

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import review_script
import app as app_module
import utils

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


def _extract_js_function(html, signature_regex):
    """Return the body (between the outermost { }) of the first function whose
    declaration matches `signature_regex`, using brace-counting so nested
    blocks don't end the match early."""
    m = re.search(signature_regex, html)
    if not m:
        return None
    start = html.index("{", m.end())
    depth = 0
    for i in range(start, len(html)):
        if html[i] == "{":
            depth += 1
        elif html[i] == "}":
            depth -= 1
            if depth == 0:
                return html[start + 1:i]
    return None


# ── Fix #1: stale checkpoint cleared before each pass ───────────────────────

def test_process_book_clears_stale_checkpoint_before_pass():
    print("\n=== Fix #1: _process_book clears a stale checkpoint before each pass ===")
    with tempfile.TemporaryDirectory() as scripts_dir:
        name = "book1"
        script_path = os.path.join(scripts_dir, f"{name}.json")
        entries = [{"speaker": "NARRATOR", "text": "Hello.", "instruct": "calm"}]
        with open(script_path, "w", encoding="utf-8") as f:
            json.dump(entries, f)

        # Simulate a checkpoint left behind by a previous (e.g. VRAM-aborted) pass.
        review_script.save_checkpoint(script_path, 0, 1, 25, 0, [], _stats(), None, [], [])
        checkpoint_path = review_script._checkpoint_path(script_path)
        check("stale checkpoint exists before this pass runs", os.path.exists(checkpoint_path))

        fake_lines = [
            "Review complete: 1 -> 1 entries",
            "  Text changed:    0",
            "  Speaker changed: 0",
            "  Instruct changed:0",
            "  Entries added:   0",
            "  Entries removed: 0",
            "  Narrators merged:0",
            "  Speakers merged: 0",
            "  Batches skipped (low GPU VRAM): 0",
            "  Total changes:   0",
        ]

        def fake_stream(cmd, *a, **k):
            # By the time review_script.py would actually be invoked for this
            # pass, the stale checkpoint must already be gone.
            check("checkpoint cleared before the subprocess for this pass starts",
                  not os.path.exists(checkpoint_path))
            return 0, fake_lines

        request = app_module.BatchReviewRequest(
            script_names=[name], context_window=0, dedupe_speakers=False,
            find_nicknames=False, bidirectional=False)

        state = app_module.process_state["batch_review"]
        state["running"] = False
        app_module.process_state["review"]["running"] = False

        with mock.patch.object(app_module, "SCRIPTS_DIR", scripts_dir), \
             mock.patch.object(app_module, "_stream_subprocess_to_logs", side_effect=fake_stream), \
             mock.patch.object(app_module, "_task_log_path", return_value=os.path.join(scripts_dir, "log.txt")), \
             mock.patch.object(app_module, "_write_batch_review_report", return_value=None):
            bg = BackgroundTasks()
            asyncio.run(app_module.review_script_batch_start(request, bg))
            for task in bg.tasks:
                result = task.func(*task.args, **task.kwargs)
                if asyncio.iscoroutine(result):
                    asyncio.run(result)

        check("book marked done", state["tasks"][0]["status"] == "done", state["tasks"][0])
        check("checkpoint still absent after the run", not os.path.exists(checkpoint_path))


# ── Fix #2: reattachRunningPollers restores review/nicknames UI state ───────

def test_reattach_running_pollers_restores_review_and_nickname_controls():
    print("\n=== Fix #2: reattachRunningPollers() restores review/nicknames UI state ===")
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    body = _extract_js_function(html, r"async function reattachRunningPollers\(\)\s*")
    check("reattachRunningPollers() found", body is not None)
    body = body or ""

    check("review branch restores review controls (_showReviewControls)",
          "_showReviewControls(true)" in body, body)
    check("review branch disables review buttons (_disableReviewButtons)",
          "_disableReviewButtons(true)" in body, body)
    check("review branch re-attaches _onReviewDone as the completion callback",
          re.search(r"pollLogs\(\s*'review'\s*,\s*'script-logs'\s*,\s*_onReviewDone\s*\)", body) is not None,
          body)
    check("nicknames branch reloads character aliases on completion",
          "loadCharacterAliases(true)" in body, body)


# ── Fix #3: utils.file_lock advisory cross-process lock ─────────────────────

def test_file_lock_basic_and_mutual_exclusion():
    print("\n=== Fix #3: utils.file_lock provides a marker file + mutual exclusion ===")
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "voice_config.json")
        lock_path = target + ".lock"

        with utils.file_lock(target):
            check("lock marker file exists while held", os.path.exists(lock_path))
        check("lock marker file removed after release", not os.path.exists(lock_path))

        # A second acquirer waits for the first to release before proceeding.
        events = []

        def holder():
            with utils.file_lock(target):
                events.append("first-start")
                time.sleep(0.2)
                events.append("first-end")

        t = threading.Thread(target=holder)
        t.start()
        time.sleep(0.05)  # give the first thread time to grab the lock
        with utils.file_lock(target, timeout=5):
            events.append("second-start")
        t.join(timeout=5)
        check("second acquirer waited for the first to release",
              events == ["first-start", "first-end", "second-start"], events)


def test_file_lock_removes_stale_lock():
    print("\n=== Fix #3: utils.file_lock removes a stale leftover lock file ===")
    with tempfile.TemporaryDirectory() as d:
        target = os.path.join(d, "voice_config.json")
        lock_path = target + ".lock"
        with open(lock_path, "w", encoding="utf-8") as f:
            f.write("")
        old = time.time() - 60
        os.utime(lock_path, (old, old))

        start = time.time()
        with utils.file_lock(target, timeout=5, stale_after=1):
            pass
        elapsed = time.time() - start
        check("a stale lock (older than stale_after) is reclaimed quickly, not waited out",
              elapsed < 2, elapsed)
        check("lock marker removed after the context exits", not os.path.exists(lock_path))


# ── Fix #4: _compute_eta falls back to an earlier valid N/M marker ──────────

def test_compute_eta_skips_zero_progress_marker():
    print("\n=== Fix #4: _compute_eta skips a cur==0 marker and falls back ===")
    state = {
        "start_time": time.time() - 50,
        "logs": [
            "Reviewing batch 3/10",
            "Reviewing batch 0/5",  # cur == 0: not a valid marker
        ],
    }
    result = app_module._compute_eta(state)
    check("falls back to the earlier valid '3/10' marker",
          result["progress"] == "3/10", result["progress"])
    check("fraction derived from the valid marker", result["fraction"] == 3 / 10, result["fraction"])


def test_compute_eta_skips_cur_greater_than_tot_marker():
    print("\n=== Fix #4: _compute_eta skips a cur>tot marker and falls back ===")
    state = {
        "start_time": time.time() - 50,
        "logs": [
            "Reviewing batch 4/8",
            "Reviewing batch 9/8",  # cur > tot: not a valid marker
        ],
    }
    result = app_module._compute_eta(state)
    check("falls back to the earlier valid '4/8' marker",
          result["progress"] == "4/8", result["progress"])
    check("fraction derived from the valid marker", result["fraction"] == 4 / 8, result["fraction"])


# ── Fix #5 & #7: _write_batch_review_report "stopped early" / "done" count ──

def test_write_batch_review_report_incomplete_excluded_and_stopped_early_note():
    print("\n=== Fix #5 & #7: 'stopped early' note + 'done' count excludes incomplete books ===")
    book_one_stats = _full_stats(total_changes=2, text_changed=2)
    book_two_stats = _full_stats(total_changes=1, batches_skipped_vram=1)

    state = {
        "tasks": [
            {"name": "book_one", "status": "done", "stats_fwd": book_one_stats, "stats": book_one_stats},
            # book_two ran low on VRAM partway through; it has stats from the
            # batches that did complete, but status is "incomplete", not "done".
            {"name": "book_two", "status": "incomplete", "stats_fwd": book_two_stats, "stats": book_two_stats},
            # book_three was never reached — the run was cancelled before any
            # task got a "cancelled" status.
            {"name": "book_three", "status": "pending"},
        ],
        "cancel": True,
        "totals_fwd": app_module._new_review_totals(),
        "totals_bwd": app_module._new_review_totals(),
        "diff_pool": {"text": [], "speaker": []},
        "aliases_fwd": [], "aliases_bwd": [],
    }
    for key in state["totals_fwd"]:
        if key == "books_done":
            continue
        state["totals_fwd"][key] += book_one_stats.get(key, 0) + book_two_stats.get(key, 0)

    path = None
    try:
        with mock.patch.object(app_module, "_llm_summarize_report", return_value=None):
            path = app_module._write_batch_review_report(state, ["book_one", "book_two", "book_three"],
                                                           bidirectional=False, discover=False)
        check("report written", path is not None and os.path.exists(path or ""))
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        check("'stopped early' note appears even though no task has status=='cancelled' (Fix #7)",
              "stopped early" in content, content)
        check("'1 of 3' — book_two's VRAM-incomplete pass is not counted as done (Fix #5)",
              "1 of 3" in content, content)
        check("does NOT report '2 of 3' (the pre-fix count that included the incomplete book)",
              "2 of 3" not in content, content)
        check("separately notes book_two was only partially reviewed (VRAM)",
              "book_two" in content and "partially reviewed" in content, content)
    finally:
        if path and os.path.exists(path):
            os.remove(path)


# ── Fix #6: _combine_pass_stats ──────────────────────────────────────────────

def test_combine_pass_stats_sums_forward_and_backward():
    print("\n=== Fix #6: _combine_pass_stats sums fwd + bwd stats for the badge tooltip ===")
    fwd = _full_stats(text_changed=2, total_changes=3, entries_added=1)
    bwd = _full_stats(text_changed=1, speaker_changed=1, total_changes=2)

    combined = app_module._combine_pass_stats(fwd, bwd)
    check("text_changed summed across passes", combined["text_changed"] == 3, combined)
    check("speaker_changed summed across passes", combined["speaker_changed"] == 1, combined)
    check("total_changes summed across passes", combined["total_changes"] == 5, combined)
    check("entries_added summed across passes", combined["entries_added"] == 1, combined)

    only_fwd = app_module._combine_pass_stats(fwd, None)
    check("a not-yet-run pass (None) contributes 0, not an error",
          only_fwd["text_changed"] == 2, only_fwd)

    empty = app_module._combine_pass_stats(None, None)
    check("all-None input returns a zeroed dict",
          all(v == 0 for v in empty.values()) and "total_changes" in empty, empty)


# ── Fix #8: _apply_cast_mapping shared by apply / apply_bulk ────────────────

def test_apply_cast_mapping_resolves_and_preserves_alias():
    print("\n=== Fix #8: _apply_cast_mapping resolves cast/shared entries and preserves alias_of ===")
    lib = {
        "shared": {"narrator_key": {"name": "Narrator", "config": {"type": "designed", "voice": "narrator.wav"}}},
        "casts": {"Series A": {"members": {"hero_key": {"name": "Hero", "config": {"type": "clone", "voice": "hero.wav"}}}}},
    }
    mapping = {"NARRATOR": "narrator_key", "HERO": "hero_key", "UNKNOWN": "missing_key"}
    current_config = {"HERO": {"type": "designed", "voice": "old.wav", "alias_of": "HERO_PRIME"}}

    new_config, applied = app_module._apply_cast_mapping(lib, "Series A", mapping, current_config)
    check("shared-pool entry resolved", new_config["NARRATOR"]["voice"] == "narrator.wav", new_config)
    check("cast-specific entry resolved (overrides shared on collision)",
          new_config["HERO"]["voice"] == "hero.wav", new_config)
    check("existing alias_of preserved on the remapped character",
          new_config["HERO"].get("alias_of") == "HERO_PRIME", new_config["HERO"])
    check("an unresolvable library key is skipped", "UNKNOWN" not in new_config, new_config)
    check("applied list matches the characters actually written",
          sorted(applied) == ["HERO", "NARRATOR"], applied)


def test_apply_cast_mapping_chars_filter_for_bulk():
    print("\n=== Fix #8: _apply_cast_mapping respects the `chars` filter (per-book, bulk) ===")
    lib = {
        "shared": {"narrator_key": {"name": "Narrator", "config": {"type": "designed", "voice": "narrator.wav"}}},
        "casts": {"Series A": {"members": {}}},
    }
    mapping = {"NARRATOR": "narrator_key", "SIDEKICK": "narrator_key"}
    chars = {"NARRATOR": 50}  # SIDEKICK doesn't appear in this particular book

    new_config, applied = app_module._apply_cast_mapping(lib, "Series A", mapping, {}, chars=chars)
    check("only the character present in this book is applied", applied == ["NARRATOR"], applied)
    check("a character absent from this book is not written", "SIDEKICK" not in new_config, new_config)


def test_voice_library_apply_bulk_writes_companion_config_under_lock():
    print("\n=== Fix #3 & #8: voice_library_apply_bulk applies the shared mapping under file_lock ===")
    with tempfile.TemporaryDirectory() as scripts_dir:
        name = "book1"
        script_path = os.path.join(scripts_dir, f"{name}.json")
        entries = [
            {"speaker": "NARRATOR", "text": "Once upon a time.", "instruct": "calm"},
            {"speaker": "HERO", "text": "I will save the day!", "instruct": "brave"},
        ]
        with open(script_path, "w", encoding="utf-8") as f:
            json.dump(entries, f)

        config_path = os.path.join(scripts_dir, f"{name}.voice_config.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump({"HERO": {"type": "designed", "voice": "old.wav", "alias_of": "HERO_PRIME"}}, f)

        lib = {
            "shared": {"narrator_key": {"name": "Narrator", "config": {"type": "designed", "voice": "narrator.wav"}}},
            "casts": {"Series A": {"members": {"hero_key": {"name": "Hero", "config": {"type": "clone", "voice": "hero.wav"}}}}},
        }

        request = app_module.LibraryApplyBulkRequest(
            cast="Series A",
            mapping={"NARRATOR": "narrator_key", "HERO": "hero_key", "VILLAIN": "narrator_key"},
            script_names=[name])

        with mock.patch.object(app_module, "SCRIPTS_DIR", scripts_dir), \
             mock.patch.object(app_module, "_load_voice_library", return_value=lib):
            result = asyncio.run(app_module.voice_library_apply_bulk(request))

        check("result reports this book", result["results"][0]["name"] == name, result)
        check("only NARRATOR and HERO applied (VILLAIN doesn't appear in this book)",
              sorted(result["results"][0]["applied"]) == ["HERO", "NARRATOR"], result)

        with open(config_path, "r", encoding="utf-8") as f:
            written = json.load(f)
        check("NARRATOR written from the shared pool", written["NARRATOR"]["voice"] == "narrator.wav", written)
        check("HERO written from the cast, preserving its alias_of",
              written["HERO"]["voice"] == "hero.wav" and written["HERO"].get("alias_of") == "HERO_PRIME",
              written)
        check("VILLAIN (not in this book) not written", "VILLAIN" not in written, written)
        check("lock file cleaned up", not os.path.exists(config_path + ".lock"))


# ── Fix #9: loadSavedScripts uses the shared _loadScriptList helper ─────────

def test_load_saved_scripts_uses_shared_helper():
    print("\n=== Fix #9: loadSavedScripts() uses the shared _loadScriptList helper ===")
    html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "app", "static", "index.html")
    with open(html_path, "r", encoding="utf-8") as f:
        html = f.read()

    body = _extract_js_function(html, r"async function loadSavedScripts\([^)]*\)\s*")
    check("loadSavedScripts() found", body is not None)
    body = body or ""

    check("loadSavedScripts delegates to _loadScriptList",
          "_loadScriptList(" in body, body)
    check("loadSavedScripts no longer has its own inline fetch('/api/scripts')",
          "fetch('/api/scripts')" not in body and 'fetch("/api/scripts")' not in body, body)


# ── Fix #10: review_script._load_resume_state ───────────────────────────────

def test_load_resume_state_fresh_run():
    print("\n=== Fix #10: _load_resume_state — fresh run (no checkpoint) ===")
    with tempfile.TemporaryDirectory() as d:
        output_path = os.path.join(d, "script.json")
        (all_corrected, total_stats, previous_tail, completed_batches, batch_lengths,
         failed_batches, resume_offset, checkpoint) = review_script._load_resume_state(
            output_path, total_batches_estimate=3, batch_size=25, context_window=0,
            all_corrected=[], total_stats=_stats())

        check("no checkpoint found", checkpoint is None)
        check("completed_batches starts at 0", completed_batches == 0, completed_batches)
        check("resume_offset starts at 0", resume_offset == 0, resume_offset)
        check("previous_tail is None", previous_tail is None)
        check("batch_lengths/failed_batches start empty",
              batch_lengths == [] and failed_batches == [])


def test_load_resume_state_resumed_run():
    print("\n=== Fix #10: _load_resume_state — resumed run (checkpoint present) ===")
    with tempfile.TemporaryDirectory() as d:
        output_path = os.path.join(d, "script.json")
        prior_corrected = [{"speaker": "NARRATOR", "text": f"e{i}", "instruct": "calm"} for i in range(5)]
        review_script.save_checkpoint(output_path, 1, 2, 5, 0, prior_corrected,
                                       _stats(text_changed=2), prior_corrected[-2:], [5], [])

        (all_corrected, total_stats, previous_tail, completed_batches, batch_lengths,
         failed_batches, resume_offset, checkpoint) = review_script._load_resume_state(
            output_path, total_batches_estimate=2, batch_size=5, context_window=0,
            all_corrected=[], total_stats=_stats())

        check("checkpoint found", checkpoint is not None)
        check("completed_batches restored from checkpoint", completed_batches == 1, completed_batches)
        check("all_corrected restored from checkpoint", all_corrected == prior_corrected)
        check("resume_offset == len(all_corrected)", resume_offset == len(prior_corrected), resume_offset)
        check("total_stats restored from checkpoint", total_stats["text_changed"] == 2, total_stats)
        check("previous_tail restored from checkpoint", previous_tail == prior_corrected[-2:])
        check("batch_lengths restored from checkpoint", batch_lengths == [5], batch_lengths)


def main():
    test_process_book_clears_stale_checkpoint_before_pass()
    test_reattach_running_pollers_restores_review_and_nickname_controls()
    test_file_lock_basic_and_mutual_exclusion()
    test_file_lock_removes_stale_lock()
    test_compute_eta_skips_zero_progress_marker()
    test_compute_eta_skips_cur_greater_than_tot_marker()
    test_write_batch_review_report_incomplete_excluded_and_stopped_early_note()
    test_combine_pass_stats_sums_forward_and_backward()
    test_apply_cast_mapping_resolves_and_preserves_alias()
    test_apply_cast_mapping_chars_filter_for_bulk()
    test_voice_library_apply_bulk_writes_companion_config_under_lock()
    test_load_saved_scripts_uses_shared_helper()
    test_load_resume_state_fresh_run()
    test_load_resume_state_resumed_run()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} check(s) FAILED: {FAILURES}")
    else:
        print("RESULT: all checks passed")
    print("=" * 60)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
