"""The speaker alias-merge stage ("Merge duplicate character names") runs after
the review's last batch, makes its own model call, and must report that call's
progress instead of leaving the finished "Reviewing batch N/N" line as the
newest marker for however long the merge takes (#2037).

Synthetic labels and lines only.
"""
import contextlib
import io
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import core
import generate_script
import review_script
from routers import script


def _entries(*speakers):
    return [{"speaker": speaker, "text": "A line of dialogue."} for speaker in speakers]


def _client(content):
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=MagicMock(return_value=SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content),
                                      finish_reason="stop")],
            usage=None)))))


def _merge_lines(printed):
    return [line.strip() for line in printed
            if line.strip().startswith(core.SPEAKER_MERGE_MARKER)]


def _index(lines, fragment):
    return next(index for index, line in enumerate(lines) if fragment in line)


class DedupeStageLoggingTests(unittest.TestCase):
    """dedupe_speakers logs what it is doing, in order, on the review's own
    stdout path (which the task log and the downloadable review log carry)."""

    def _run(self, client, entries, **kwargs):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), \
             patch.object(generate_script, "get_response_log_path",
                          return_value=str(Path(self._tmp.name, "object.log"))):
            # What the review loop leaves as the newest marker before this stage.
            print("\nReviewing batch 5/5 (3 entries)...")
            result = review_script.dedupe_speakers(client, "model", entries, **kwargs)
        return result, printed.getvalue().splitlines()

    def _run_raising(self, error):
        printed = io.StringIO()
        with contextlib.redirect_stdout(printed), \
             patch.object(review_script, "call_llm_for_object", side_effect=error):
            print("\nReviewing batch 5/5 (3 entries)...")
            result = review_script.dedupe_speakers(_client("{}"), "model",
                                                  _entries("ALICE", "BETTY"))
        return result, printed.getvalue().splitlines()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def test_stage_call_and_summary_lines_print_in_order_after_the_last_batch(self):
        result, printed = self._run(_client('{"ALICE": "BETTY"}'),
                                    _entries("ALICE", "BETTY", "CAROL"))
        self.assertEqual({"ALICE": "BETTY"}, result[0])
        merge = _merge_lines(printed)
        self.assertEqual(4, len(merge), merge)
        self.assertEqual(0, _index(merge, "3 speaker label(s), 1 model call"))
        self.assertEqual(1, _index(merge, "call 1/1 - asking the model"))
        self.assertEqual(2, _index(merge, "call 1/1 finished in"))
        self.assertEqual(3, _index(merge, "1 label(s) merged, 1 entries updated in"))
        # Every merge line comes after the batch count it replaces.
        last_batch = _index(printed, "Reviewing batch 5/5")
        for fragment in ("asking the model", "finished in", "entries updated"):
            self.assertGreater(_index(printed, fragment), last_batch, printed)

    def test_exhausted_call_is_not_reported_as_an_empty_success(self):
        client = _client("{}")
        client.chat.completions.create.side_effect = RuntimeError("provider down")
        result, printed = self._run(client, _entries("ALICE", "BETTY"))
        self.assertEqual(({}, 0, []), result)
        merge = _merge_lines(printed)
        self.assertEqual(1, _index(merge, "call 1/1 - asking the model"))
        self.assertEqual(2, _index(merge, "call 1/1 finished in"))
        self.assertIn("no usable response - applying known aliases only", merge[2])
        self.assertNotIn("0 label(s) proposed", merge[2])
        self.assertEqual(3, _index(merge, "no duplicate names found"))

    def test_raising_call_reports_the_failure_and_falls_back_to_known_aliases(self):
        result, printed = self._run_raising(RuntimeError("unexpected payload"))
        self.assertEqual(({}, 0, []), result)
        merge = _merge_lines(printed)
        self.assertEqual(1, _index(merge, "call 1/1 - asking the model"))
        self.assertEqual(2, _index(merge, "call 1/1 failed after"))
        self.assertIn("RuntimeError: unexpected payload", merge[2])
        self.assertIn("applying known aliases only", merge[2])
        self.assertEqual(3, _index(merge, "no duplicate names found"))

    def test_no_duplicates_still_reports_the_outcome_and_duration(self):
        result, printed = self._run(_client("{}"), _entries("ALICE", "BETTY"))
        self.assertEqual(({}, 0, []), result)
        merge = _merge_lines(printed)
        self.assertEqual(4, len(merge), merge)
        self.assertIn("call 1/1 finished in", merge[2])
        self.assertIn("0 label(s) proposed", merge[2])
        self.assertIn("no duplicate names found in", merge[3])

    def test_one_label_prints_no_call_and_never_asks_the_model(self):
        client = _client("{}")
        result, printed = self._run(client, _entries("ALICE", "ALICE"))
        self.assertEqual(({}, 0, []), result)
        client.chat.completions.create.assert_not_called()
        merge = _merge_lines(printed)
        self.assertEqual(2, len(merge), merge)
        self.assertIn("1 speaker label(s), no model call needed", merge[0])
        self.assertIn("no duplicate names found", merge[1])
        self.assertFalse(any("asking the model" in line for line in merge), merge)


class ComputeEtaMergeStageTests(unittest.TestCase):
    def _eta(self, logs, **state):
        return core._compute_eta({"start_time": time.time() - 60, "logs": logs, **state})

    def test_merge_marker_wins_over_the_finished_review_batch_count(self):
        eta = self._eta(["Reviewing batch 5/5 (3 entries)...",
                         f"{core.SPEAKER_MERGE_MARKER} call 1/1 - asking the model"])
        self.assertEqual(f"{core.SPEAKER_MERGE_MARKER} call 1/1 - asking the model",
                         eta["progress"])
        self.assertEqual(1.0, eta["fraction"])
        # One model call of unknown length: no rate to estimate from, and a
        # 1.0 fraction would otherwise read as "about 0s left".
        self.assertIsNone(eta["eta_seconds"])

    def test_merge_stage_outcome_line_stays_current_progress(self):
        eta = self._eta(["Reviewing batch 5/5 (3 entries)...",
                         f"{core.SPEAKER_MERGE_MARKER} 2 label(s) merged, 9 entries "
                         "updated in 41.2s"])
        self.assertIn("2 label(s) merged", eta["progress"])
        self.assertIsNone(eta["eta_seconds"])

    def test_review_still_in_its_batch_loop_is_unaffected(self):
        eta = self._eta(["Reviewing batch 3/5 (3 entries)..."])
        self.assertEqual("3/5", eta["progress"])
        self.assertAlmostEqual(0.6, eta["fraction"])
        self.assertIsNotNone(eta["eta_seconds"])

    def test_batch_review_log_prefix_does_not_hide_the_merge_stage(self):
        eta = self._eta(["[2] Reviewing batch 5/5 (3 entries)...",
                         f"[2] {core.SPEAKER_MERGE_MARKER} call 1/1 - asking the model"],
                        tasks=["one", "two"], current_task_idx=1)
        self.assertIn(f"{core.SPEAKER_MERGE_MARKER} call 1/1", eta["progress"])
        self.assertNotIn("5/5", eta["progress"])

    def test_batch_review_keeps_book_progress_and_its_eta(self):
        eta = self._eta(["--- [2/3] Reviewing 'book-two' ---",
                         "Reviewing batch 5/5 (3 entries)...",
                         f"{core.SPEAKER_MERGE_MARKER} call 1/1 - asking the model"],
                        tasks=["one", "two", "three"], current_task_idx=1)
        self.assertIn("item 2/3", eta["progress"])
        self.assertIn(f"{core.SPEAKER_MERGE_MARKER} call 1/1", eta["progress"])
        self.assertIsNotNone(eta["eta_seconds"])


class MergeStageHintTests(unittest.TestCase):
    def test_stage_hint_picks_the_merge_line_over_the_finished_batch(self):
        self.assertEqual(
            f"{core.SPEAKER_MERGE_MARKER} call 1/1 - asking the model"
            " · Retrying... (attempt 2 of 3)",
            script._stage_hint(["Reviewing batch 5/5 (3 entries)...",
                                f"{core.SPEAKER_MERGE_MARKER} call 1/1 - asking the model",
                                "Retrying... (attempt 2 of 3)"]))

    def test_stage_hint_drops_the_review_batch_behind_the_merge_stage(self):
        self.assertEqual(f"{core.SPEAKER_MERGE_MARKER} 1 label(s) merged, 4 entries updated",
                         script._stage_hint(["Reviewing batch 5/5 (3 entries)...",
                                             f"{core.SPEAKER_MERGE_MARKER} 1 label(s) merged, "
                                             "4 entries updated"]))

    def test_stage_hint_recognises_the_merge_line_under_a_batch_log_prefix(self):
        self.assertEqual(f"{core.SPEAKER_MERGE_MARKER} call 1/1 - asking the model",
                         script._stage_hint(["[2] Reviewing batch 5/5 (3 entries)...",
                                             f"[2] {core.SPEAKER_MERGE_MARKER} call 1/1 "
                                             "- asking the model"]))

    def test_stage_hint_still_prefers_the_review_batch_while_reviewing(self):
        self.assertEqual("Reviewing batch 3/5 (3 entries)...",
                         script._stage_hint(["Reviewing batch 2/5 (3 entries)...",
                                             "Reviewing batch 3/5 (3 entries)..."]))


class FrontendActivityMarkerParityTests(unittest.TestCase):
    """One definition of the marker text: the JS regex literal is the one place
    it has to be repeated, so pin it to the Python constant (Rule 15)."""

    def test_activity_marker_literal_carries_the_python_marker(self):
        source = (Path(__file__).resolve().parent.parent / "static/js/app-core.js").read_text(
            encoding="utf-8")
        line = next(line for line in source.splitlines() if "const ACTIVITY_MARKER" in line)
        self.assertIn(core.SPEAKER_MERGE_MARKER + " ", line)


if __name__ == "__main__":
    unittest.main()
