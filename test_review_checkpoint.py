"""
Regression tests for the review_script.py / lmstudio_settings.py / app.py
fixes from the night-super-night-themes code review:

  1. get_vram_usage() no longer raises ZeroDivisionError when rocm-smi
     reports a total VRAM of 0.
  2. _stream_subprocess_to_logs() flushes the log file in batches of 50
     lines, but always flushes any remainder on close.
  3. apply_lmstudio_settings() explains *why* a reload failed when the
     preceding unload also failed (old settings may still be active).
  4. (covered by test_themes.py / manual review - merge_consecutive_narrators
     is unmodified and runs regardless of vram_aborted)
  5. load_checkpoint() rewinds to the earliest failed batch so it gets
     retried on resume instead of being permanently skipped.

Run with:
  /home/fakemitch/pinokio/api/alexandria-audiobook.git/app/env/bin/python test_review_checkpoint.py
"""
import json
import os
import subprocess
import sys
import tempfile
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))

import review_script
import lmstudio_settings

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


def test_checkpoint_round_trip():
    print("\n=== checkpoint round trip (batch_lengths / failed_batches persist) ===")
    with tempfile.TemporaryDirectory() as d:
        output_path = os.path.join(d, "script.json")
        all_corrected = [{"speaker": "NARRATOR", "text": f"e{i}", "instruct": ""} for i in range(6)]
        review_script.save_checkpoint(output_path, 3, 5, 2, 0, all_corrected,
                                       _stats(text_changed=1), all_corrected[-2:],
                                       [2, 2, 2], [])
        loaded = review_script.load_checkpoint(output_path, 5, 2, 0)
        check("checkpoint loads", loaded is not None)
        check("completed_batches preserved", loaded["completed_batches"] == 3)
        check("batch_lengths preserved", loaded["batch_lengths"] == [2, 2, 2], loaded["batch_lengths"])
        check("failed_batches preserved (empty)", loaded["failed_batches"] == [], loaded["failed_batches"])
        check("all_corrected preserved", loaded["all_corrected"] == all_corrected)

        review_script.clear_checkpoint(output_path)
        check("clear_checkpoint removes the file",
              review_script.load_checkpoint(output_path, 5, 2, 0) is None)


def test_rewind_single_failed_batch():
    print("\n=== resume rewinds to retry a single failed batch ===")
    with tempfile.TemporaryDirectory() as d:
        output_path = os.path.join(d, "script.json")
        # 5 batches of 2 entries completed; batch 3 (entries 4-5, 0-indexed
        # 4:6) failed and was filled with its original, unreviewed entries.
        all_corrected = [{"speaker": "NARRATOR", "text": f"e{i}", "instruct": ""} for i in range(10)]
        review_script.save_checkpoint(output_path, 5, 5, 2, 0, all_corrected,
                                       _stats(text_changed=4, batches_failed=1),
                                       all_corrected[-2:], [2, 2, 2, 2, 2], [3])

        loaded = review_script.load_checkpoint(output_path, 5, 2, 0)
        check("rewinds completed_batches to before the failed batch",
              loaded["completed_batches"] == 2, loaded["completed_batches"])
        check("truncates all_corrected to entries from batches 1-2",
              loaded["all_corrected"] == all_corrected[:4], loaded["all_corrected"])
        check("batch_lengths truncated to match",
              loaded["batch_lengths"] == [2, 2], loaded["batch_lengths"])
        check("failed_batches cleared so batch 3 is retried as a normal batch",
              loaded["failed_batches"] == [], loaded["failed_batches"])
        check("previous_tail recomputed from the truncated all_corrected",
              loaded["previous_tail"] == all_corrected[2:4], loaded["previous_tail"])
        check("batches_failed count decremented (the retried failure is undone)",
              loaded["total_stats"]["batches_failed"] == 0, loaded["total_stats"])


def test_rewind_multiple_failed_batches():
    print("\n=== resume rewinds to the EARLIEST of several failed batches ===")
    with tempfile.TemporaryDirectory() as d:
        output_path = os.path.join(d, "script.json")
        all_corrected = [{"speaker": "NARRATOR", "text": f"e{i}", "instruct": ""} for i in range(10)]
        # batches 2 and 4 failed (out of order in the list, like real appends)
        review_script.save_checkpoint(output_path, 5, 5, 2, 0, all_corrected,
                                       _stats(batches_failed=2),
                                       all_corrected[-2:], [2, 2, 2, 2, 2], [4, 2])

        loaded = review_script.load_checkpoint(output_path, 5, 2, 0)
        check("rewinds to before the earliest failed batch (2, not 4)",
              loaded["completed_batches"] == 1, loaded["completed_batches"])
        check("truncates all_corrected to entries from batch 1 only",
              loaded["all_corrected"] == all_corrected[:2], loaded["all_corrected"])
        check("both failures cleared (batch 4 is naturally re-reached)",
              loaded["failed_batches"] == [], loaded["failed_batches"])
        check("batches_failed count decremented by both",
              loaded["total_stats"]["batches_failed"] == 0, loaded["total_stats"])


def test_legacy_checkpoint_without_new_fields():
    print("\n=== legacy checkpoints (no batch_lengths/failed_batches) still load ===")
    with tempfile.TemporaryDirectory() as d:
        output_path = os.path.join(d, "script.json")
        path = review_script._checkpoint_path(output_path)
        all_corrected = [{"speaker": "NARRATOR", "text": "e0", "instruct": ""}]
        legacy_data = {
            "completed_batches": 2,
            "total_batches": 5,
            "batch_size": 2,
            "context_window": 0,
            "all_corrected": all_corrected,
            "total_stats": _stats(),
            "previous_tail": all_corrected,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(legacy_data, f)

        loaded = review_script.load_checkpoint(output_path, 5, 2, 0)
        check("legacy checkpoint without new fields still loads", loaded is not None)
        check("completed_batches unchanged (nothing to retry)",
              loaded["completed_batches"] == 2, loaded["completed_batches"])
        check("missing fields default to empty lists",
              loaded["batch_lengths"] == [] and loaded["failed_batches"] == [])


def test_legacy_checkpoint_missing_batches_skipped_vram():
    print("\n=== legacy checkpoints without total_stats['batches_skipped_vram'] don't KeyError on resume ===")
    with tempfile.TemporaryDirectory() as d:
        output_path = os.path.join(d, "script.json")
        path = review_script._checkpoint_path(output_path)
        all_corrected = [{"speaker": "NARRATOR", "text": "e0", "instruct": ""}]
        # total_stats predates the "batches_skipped_vram" key added alongside
        # the VRAM watchdog.
        old_total_stats = {
            "text_changed": 1, "speaker_changed": 0, "instruct_changed": 0,
            "entries_added": 0, "entries_removed": 0, "batches_failed": 0,
        }
        legacy_data = {
            "completed_batches": 2,
            "total_batches": 5,
            "batch_size": 2,
            "context_window": 0,
            "all_corrected": all_corrected,
            "total_stats": old_total_stats,
            "previous_tail": all_corrected,
        }
        with open(path, "w", encoding="utf-8") as f:
            json.dump(legacy_data, f)

        loaded = review_script.load_checkpoint(output_path, 5, 2, 0)
        check("legacy checkpoint loads", loaded is not None)
        check("batches_skipped_vram defaults to 0",
              loaded["total_stats"].get("batches_skipped_vram") == 0, loaded["total_stats"])
        # This is the line that used to raise KeyError on resume.
        try:
            _ = loaded["total_stats"]["batches_skipped_vram"]
            ok = True
        except KeyError:
            ok = False
        check("total_stats['batches_skipped_vram'] is subscriptable without KeyError", ok)


def test_partial_batch_lengths_skips_rewind():
    print("\n=== failed_batches with incomplete batch_lengths doesn't crash or mis-rewind ===")
    with tempfile.TemporaryDirectory() as d:
        output_path = os.path.join(d, "script.json")
        all_corrected = [{"speaker": "NARRATOR", "text": f"e{i}", "instruct": ""} for i in range(4)]
        # completed_batches=3 but batch_lengths only covers 1 batch (as if
        # this checkpoint straddles the old/new checkpoint format).
        review_script.save_checkpoint(output_path, 3, 5, 2, 0, all_corrected,
                                       _stats(batches_failed=1),
                                       all_corrected[-2:], [2], [3])

        loaded = review_script.load_checkpoint(output_path, 5, 2, 0)
        check("does not rewind when batch_lengths doesn't cover completed_batches",
              loaded["completed_batches"] == 3, loaded["completed_batches"])
        check("all_corrected left untouched", loaded["all_corrected"] == all_corrected)


def test_vram_zero_division_guard():
    print("\n=== get_vram_usage returns None instead of crashing when total is 0 ===")
    fake_zero = mock.Mock()
    fake_zero.stdout = json.dumps({
        "card0": {"VRAM Total Used Memory (B)": "0", "VRAM Total Memory (B)": "0"}
    })
    with mock.patch.object(review_script.subprocess, "run", return_value=fake_zero):
        usage = review_script.get_vram_usage()
    check("total<=0 returns None instead of raising ZeroDivisionError", usage is None, usage)

    fake_normal = mock.Mock()
    fake_normal.stdout = json.dumps({
        "card0": {"VRAM Total Used Memory (B)": "1000", "VRAM Total Memory (B)": "2000"}
    })
    with mock.patch.object(review_script.subprocess, "run", return_value=fake_normal):
        usage2 = review_script.get_vram_usage()
    check("normal totals still return (used, total)", usage2 == (1000, 2000), usage2)


def test_lmstudio_unload_failure_message():
    print("\n=== apply_lmstudio_settings explains a failed unload+load combo ===")
    unload_result = mock.Mock(returncode=1, stdout="", stderr="model busy")
    load_result = mock.Mock(returncode=1, stdout="", stderr="load failed: identifier in use")

    with mock.patch.object(lmstudio_settings, "find_lms_binary", return_value="/usr/bin/lms"), \
         mock.patch.object(lmstudio_settings.subprocess, "run", side_effect=[unload_result, load_result]):
        ok, msg = lmstudio_settings.apply_lmstudio_settings("my-model", ideal=True)

    check("apply_lmstudio_settings reports failure", ok is False)
    check("failure message explains the unload also failed",
          "unload" in msg.lower() and "previously-loaded" in msg, msg)

    # When the unload succeeds but load still fails, the message should NOT
    # claim the old settings might still be active.
    unload_ok = mock.Mock(returncode=0, stdout="", stderr="")
    load_result2 = mock.Mock(returncode=1, stdout="", stderr="load failed: out of memory")
    with mock.patch.object(lmstudio_settings, "find_lms_binary", return_value="/usr/bin/lms"), \
         mock.patch.object(lmstudio_settings.subprocess, "run", side_effect=[unload_ok, load_result2]):
        ok2, msg2 = lmstudio_settings.apply_lmstudio_settings("my-model", ideal=True)

    check("failure message omits the unload caveat when unload succeeded",
          "previously-loaded" not in msg2, msg2)


def test_stream_subprocess_log_flush():
    print("\n=== _stream_subprocess_to_logs flushes log file in batches but never drops lines ===")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
    import app as app_module

    with tempfile.TemporaryDirectory() as d:
        log_file = os.path.join(d, "run.log")
        state = {"logs": []}
        # Print 125 lines (50 + 50 + 25) so we exercise multiple flush
        # batches plus a final partial batch handled by close().
        code = "for i in range(125): print(f'line{i}')"
        rc, own_lines = app_module._stream_subprocess_to_logs(
            [sys.executable, "-c", code], cwd=d, state=state, log_file=log_file)

        check("subprocess exits cleanly", rc == 0, rc)
        check("all lines captured in state['logs']",
              len(state["logs"]) == 125, len(state["logs"]))
        check("returned own_lines also captures all lines",
              own_lines == [f"line{i}" for i in range(125)], len(own_lines))

        with open(log_file, "r", encoding="utf-8") as f:
            file_lines = f.read().splitlines()
        check("all lines (including the final partial batch) flushed to disk on close",
              file_lines == [f"line{i}" for i in range(125)],
              f"{len(file_lines)} lines on disk")


def test_stream_subprocess_own_lines_unaffected_by_cap():
    print("\n=== _stream_subprocess_to_logs returns this run's own lines even when state['logs'] is capped ===")
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "app"))
    import app as app_module

    # Pre-fill state['logs'] close to the cap so this run's output pushes it
    # over and earlier entries get popped from the front.
    state = {"logs": [f"old{i}" for i in range(15)]}
    code = "for i in range(10): print(f'line{i}')"
    rc, own_lines = app_module._stream_subprocess_to_logs(
        [sys.executable, "-c", code], cwd=".", state=state, max_logs=20)

    check("subprocess exits cleanly", rc == 0, rc)
    check("state['logs'] capped to max_logs", len(state["logs"]) == 20, len(state["logs"]))
    check("own_lines has all 10 lines from this run regardless of the cap",
          own_lines == [f"line{i}" for i in range(10)], own_lines)


def main():
    test_checkpoint_round_trip()
    test_rewind_single_failed_batch()
    test_rewind_multiple_failed_batches()
    test_legacy_checkpoint_without_new_fields()
    test_legacy_checkpoint_missing_batches_skipped_vram()
    test_partial_batch_lengths_skips_rewind()
    test_vram_zero_division_guard()
    test_lmstudio_unload_failure_message()
    test_stream_subprocess_log_flush()
    test_stream_subprocess_own_lines_unaffected_by_cap()

    print("\n" + "=" * 60)
    if FAILURES:
        print(f"RESULT: {len(FAILURES)} check(s) FAILED: {FAILURES}")
    else:
        print("RESULT: all checks passed")
    print("=" * 60)
    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
