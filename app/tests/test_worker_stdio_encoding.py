"""A child script's stdout must not be limited by the host's ANSI code page.

#2062: a finished script review died with UnicodeEncodeError printing its
DIFF_PREVIEW_JSON line on native Windows, because the child's pipe defaulted
to cp1252 and the parent decoded the pipe with the same locale codec. The fix
is one agreement between the two sides (review_startup.WORKER_STDIO_ENCODING):
the child is launched with PYTHONIOENCODING set to it and the parent decodes
the pipe with it, and the machine-read *_JSON markers are printed ASCII-safe
so json.loads restores every character whatever the child's stdout codec is.

Synthetic strings only — no text from any book.
"""
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

import core
import review_script
import review_startup

# U+2770 is the character from the report: printable prose, absent from cp1252.
ANSI_BULLET = "\u2770test\u2771"
JAPANESE = "\u30c6\u30b9\u30c8"
LEGACY_ENCODING = "cp1252"


def _marker_payload():
    return {"text_rewrites": [{"before": ANSI_BULLET, "after": JAPANESE}],
            "speaker_changes": [{"from": JAPANESE, "to": ANSI_BULLET}]}


def _raw_child_source():
    """A child printing exactly what the app's scripts print: free text with
    the operator's characters, then a marker whose JSON is not ASCII-escaped."""
    return (
        "import json\n"
        "print('Loaded 1 script entry for review " + JAPANESE + "')\n"
        "print('DIFF_PREVIEW_JSON: ' + json.dumps(" + repr(_marker_payload()) + ", ensure_ascii=False))\n"
    )


class ChildStdoutTests(unittest.TestCase):
    def _run_task(self, tmp, source):
        """Launch a child the way every router does: core.run_process, whose
        environment starts from os.environ and goes through the real child-env
        helper."""
        path = os.path.join(tmp, "child.py")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(source)
        key = "stdio_encoding_probe"
        state = {"running": False, "logs": [], "cancel": False, "status": None,
                 "return_code": None, "process": None, "pid": None, "paused": False}
        core.process_state[key] = state
        try:
            with patch.dict(os.environ, {"PYTHONIOENCODING": LEGACY_ENCODING}), \
                    patch.object(core, "_init_task_log",
                                 return_value=os.path.join(tmp, "task.log")):
                return_code = core.run_process([sys.executable, "-u", path], key, tmp)
        finally:
            core.process_state.pop(key, None)
        return return_code, state

    def test_a_child_printing_non_ansi_text_finishes_and_the_parent_reads_it_back(self):
        """The end-to-end shape of #2062: the operator's environment asks for
        cp1252 stdout, the child prints Japanese free text and an unescaped
        marker, and the parent must see the exact characters with exit 0 —
        not UnicodeEncodeError after all the work is done."""
        with tempfile.TemporaryDirectory() as tmp:
            return_code, state = self._run_task(tmp, _raw_child_source())

        self.assertEqual(0, return_code, state["logs"])
        self.assertEqual("done", state["status"])
        self.assertEqual(_marker_payload(), core._extract_diff_highlights(state["logs"]))
        self.assertIn("Loaded 1 script entry for review " + JAPANESE, state["logs"])
        self.assertFalse([line for line in state["logs"] if "\ufffd" in line],
                         "no line may arrive as a replacement character")

    def test_a_legacy_stdio_child_dies_before_its_marker_line(self):
        """The reproduction, pinned: with the pre-fix cp1252 agreement (both
        halves move together, as they must) the child exits non-zero on the
        marker, so the review reports a failure and no preview is parsed."""
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(review_startup, "WORKER_STDIO_ENCODING", LEGACY_ENCODING):
            return_code, state = self._run_task(tmp, _raw_child_source())

        self.assertNotEqual(0, return_code, state["logs"])
        self.assertTrue(any("UnicodeEncodeError" in line for line in state["logs"]),
                        state["logs"])
        self.assertEqual({"text_rewrites": [], "speaker_changes": []},
                         core._extract_diff_highlights(state["logs"]))

    def test_child_environment_agrees_with_the_parent_decode(self):
        """The writer half: the child is told the same codec core decodes with,
        even when the operator's environment names another one, and unrelated
        variables are still passed through."""
        environment = review_startup.worker_environment(
            {"PYTHONIOENCODING": LEGACY_ENCODING, "KEEP": "value"})
        self.assertEqual(review_startup.WORKER_STDIO_ENCODING,
                         environment["PYTHONIOENCODING"])
        self.assertEqual("value", environment["KEEP"])


class ReviewMarkerTests(unittest.TestCase):
    def _emit(self, marker, payload):
        emitted = StringIO()
        with redirect_stdout(emitted):
            review_script._emit_json_marker(marker, payload)
        return emitted.getvalue().strip()

    def test_the_diff_preview_marker_round_trips_non_ansi_text(self):
        """The review path from the report: the DIFF_PREVIEW_JSON line carries
        U+2770 and CJK text, and core's parser returns those exact characters.
        The line itself is ASCII, so no stdout codec can refuse or mangle it."""
        payload = _marker_payload()
        line = self._emit("DIFF_PREVIEW_JSON", payload)
        self.assertTrue(line.startswith("DIFF_PREVIEW_JSON: "), line)
        line.encode("ascii")
        self.assertEqual(payload, core._extract_diff_highlights([line]))

    def test_the_failed_sections_marker_round_trips_non_ansi_text(self):
        """Same guarantee for the incomplete-review marker, whose reason text
        also carries whatever the operator's script contains."""
        sections = [{"category": "review_failed", "batch": 2,
                     "reason": JAPANESE + " " + ANSI_BULLET}]
        payload = {"sections": sections, "original_entries_preserved": True,
                   "checkpoint_retained": False, "retry_from_batch": 2}
        line = self._emit("FAILED_SECTIONS_JSON", payload)
        line.encode("ascii")
        parsed = core._extract_failed_sections([line])
        self.assertEqual(sections, parsed["sections"])
        self.assertTrue(parsed["original_entries_preserved"])
        self.assertFalse(parsed["checkpoint_retained"])
        self.assertEqual(2, parsed["retry_from_batch"])

    def test_review_script_prints_every_marker_through_the_helper(self):
        """The call sites, not just the helper: a marker printed with
        ensure_ascii=False would be unsafe again on a cp1252 stdout, and a new
        marker must not reintroduce that."""
        source = Path(__file__).resolve().parent.parent.joinpath("review_script.py").read_text(
            encoding="utf-8")
        self.assertIn("_emit_json_marker('FAILED_SECTIONS_JSON', failure_details)", source)
        self.assertIn("_emit_json_marker('DIFF_PREVIEW_JSON', diff_highlights)", source)
        self.assertNotIn("json.dumps(failure_details, ensure_ascii=False)", source)
        self.assertNotIn("json.dumps(diff_highlights, ensure_ascii=False)", source)


if __name__ == "__main__":
    unittest.main()
