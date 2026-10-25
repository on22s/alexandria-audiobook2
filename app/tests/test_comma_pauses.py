"""Comma pauses are measured where the text has a mid-line mark, and nowhere else.

Synthetic audio: 0.3 s tones stand in for words, silences for pauses. Spans are
contiguous like whisper.cpp's, so a silence is absorbed into a neighbouring word
and only the envelope can measure it.
"""
import argparse
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf

import comma_pauses
from comma_pauses import get_clip_comma_pauses, get_envelope, get_mark_pauses, get_summary

RATE = 24000
TEXT = "One two, three four five."


def write_line(path, gaps):
    """Five tone 'words'; `gaps` maps a word index to the silence after it (seconds)."""
    t = np.arange(int(.3 * RATE)) / RATE
    pieces = []
    for index in range(5):
        pieces.append((.3 * np.sin(2 * np.pi * (220 + 40 * index) * t)).astype("float32"))
        pieces.append(np.zeros(int(gaps.get(index, 0) * RATE), dtype="float32"))
    sf.write(path, np.concatenate(pieces), RATE)


def get_spans(gaps, absorb="before"):
    """Contiguous spans over TEXT; each silence joins the word before (or after) it."""
    words = [(0, 3), (4, 7), (9, 14), (15, 19), (20, 24)]
    spans, clock = [], 0.0
    for index, (start, end) in enumerate(words):
        lead = gaps.get(index - 1, 0) if absorb == "after" else 0
        tail = gaps.get(index, 0) if absorb == "before" else 0
        span_start = clock
        clock += lead + .3 + tail
        spans.append({"from": start, "to": end, "start": round(span_start, 3), "end": round(clock, 3)})
    return spans


def naive_longest_pause(db):
    """The mutant: the longest quiet run anywhere in the clip, ignoring the text."""
    longest = run = 0
    for level in db:
        run = run + 1 if level <= comma_pauses.QUIET_DB else 0
        longest = max(longest, run)
    return longest * comma_pauses.FRAME_S


class CommaPauseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)

    def measure(self, gaps, absorb="before"):
        path = self.root / "line.wav"
        write_line(path, gaps)
        return get_mark_pauses(get_envelope(path), TEXT, get_spans(gaps, absorb))

    def test_pause_at_the_comma_is_measured_and_a_cut_one_is_not(self):
        for absorb in ("before", "after"):
            with self.subTest(absorb=absorb):
                kept = self.measure({1: .25, 3: .25}, absorb)
                cut = self.measure({3: .25}, absorb)
                # One mark only: the comma. The final full stop and the
                # unpunctuated three/four pause are not counted.
                self.assertEqual([","], [mark["mark"] for mark in kept])
                self.assertAlmostEqual(.25, kept[0]["pause_s"], delta=.03)
                self.assertEqual([","], [mark["mark"] for mark in cut])
                self.assertLess(cut[0]["pause_s"], .05)

    def test_punctuation_inside_numbers_is_not_a_mark_but_a_dash_is(self):
        text = "Pay 3,000 or 3.5 now—then wait."
        words = [(0, 3), (4, 5), (6, 9), (10, 12), (13, 14), (15, 16), (17, 20), (21, 25), (26, 30)]
        spans = [{"from": a, "to": b, "start": i * .3, "end": (i + 1) * .3} for i, (a, b) in enumerate(words)]
        marks = get_mark_pauses(np.zeros(400), text, spans)
        self.assertEqual(["—"], [mark["mark"] for mark in marks])

    def test_a_measure_that_ignores_the_text_cannot_tell_these_apart(self):
        # Both clips hold a 0.25 s silence somewhere, so the whole-clip measure
        # scores them alike. If this ever stops holding, the fixture no longer
        # tests what get_mark_pauses adds.
        kept, cut = self.root / "kept.wav", self.root / "cut.wav"
        write_line(kept, {1: .25, 3: .25})
        write_line(cut, {3: .25})
        self.assertAlmostEqual(naive_longest_pause(get_envelope(kept)),
                               naive_longest_pause(get_envelope(cut)), delta=.03)

    def test_refused_timing_is_recorded_and_never_counted_as_a_pause(self):
        path = self.root / "line.wav"
        write_line(path, {1: .25})
        with patch("word_timing.render_word_timing", side_effect=ValueError("ASR timestamp saturation.")):
            result = get_clip_comma_pauses(path, TEXT, runtime={})
        self.assertEqual({"refused": "ASR timestamp saturation."}, result)
        summary = get_summary([result])
        self.assertEqual({"clips": 1, "clips_refused": 1, "marks": 0}, summary)

    def test_summary_counts_skipped_marks_with_a_covering_interval(self):
        results = [{"pauses": [{"pause_s": value} for value in (0.0, .05, .09, .2, .25, .3, .3, .31, .4, .5)]},
                   {"refused": "too short"}]
        summary = get_summary(results)
        self.assertEqual((2, 1, 10), (summary["clips"], summary["clips_refused"], summary["marks"]))
        self.assertEqual(.3, summary["share_skipped"])
        low, high = summary["share_skipped_ci95"]
        self.assertTrue(low < .3 < high, summary)
        self.assertAlmostEqual(.275, summary["median_pause_s"], places=3)


TOOL_PATH = Path(__file__).parent.parent.parent / "tools" / "voice_lab" / "evaluate_comma_pauses.py"
spec = importlib.util.spec_from_file_location("evaluate_comma_pauses", TOOL_PATH)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)


def run(lines):
    return {"lines": [{"id": i, "prepared_text": t, "result": r} for i, t, r in lines]}


class CommaPauseToolTests(unittest.TestCase):
    def test_probe_lines_are_the_fixtures_comma_lines(self):
        lines = tool.get_comma_lines()
        self.assertTrue(lines)
        self.assertTrue(all(comma_pauses.has_mid_line_mark(line["text"]) for line in lines))
        self.assertEqual(len(lines), sum(comma_pauses.has_mid_line_mark(line["text"])
                                         for line in json.loads(tool.LINES_PATH.read_text())["lines"]))
        self.assertEqual(["Down,", "down."], tool.get_clauses("Down, down."))
        self.assertEqual(["3,000 coins"], tool.get_clauses("3,000 coins"))

    def test_variants_parse_or_refuse(self):
        self.assertEqual(("none", ""), tool.parse_variant("none"))
        self.assertEqual(("split", ""), tool.parse_variant("split"))
        self.assertEqual(("instruct", "pause at commas"), tool.parse_variant("instruct: pause at commas"))
        for bad in ("instruct:", "faster", "instruct:   "):
            with self.subTest(bad=bad), self.assertRaises(argparse.ArgumentTypeError):
                tool.parse_variant(bad)

    def test_compare_pairs_the_same_marks_and_refuses_other_lines(self):
        marks = lambda *values: {"pauses": [{"pause_s": v} for v in values]}
        first = run([("a", "A, b.", marks(.05, .3)), ("b", "B, c.", marks(.02)), ("c", "C, d.", {"refused": "x"})])
        second = run([("a", "A, b.", marks(.25, .3)), ("b", "B, c.", marks(.22)), ("c", "C, d.", marks(.2))])
        pairs, unmatched = tool.get_pairs(first, second)
        self.assertEqual([(.05, .25), (.3, .3), (.02, .22)], pairs)
        self.assertEqual(1, unmatched)  # refused in one run: left out, never treated as 0
        report = tool.compare(first, second)
        self.assertEqual(([0.667, 0.0], 3), (report["share_skipped"], report["marks_paired"]))
        with self.assertRaisesRegex(ValueError, "do not share line"):
            tool.get_pairs(first, run([("a", "Other text.", marks(.1))]))


REPORT_PATH = Path(__file__).parent.parent.parent / "tools" / "book_comma_report.py"
report_spec = importlib.util.spec_from_file_location("book_comma_report", REPORT_PATH)
book_report = importlib.util.module_from_spec(report_spec)
report_spec.loader.exec_module(book_report)


class BookCommaReportTests(unittest.TestCase):
    def test_only_chunks_with_a_mid_line_mark_are_eligible(self):
        for text, eligible in (("Down, down, down.", True), ("He paused - then went on.", False),
                               ("He paused — then went on.", True), ("It cost 3,000 or 3.5.", False),
                               ("One sentence. Another one.", True), ("No marks here.", False)):
            with self.subTest(text=text):
                self.assertIs(eligible, comma_pauses.has_mid_line_mark(text))

    def test_report_gives_rates_per_voice_and_quotes_no_text(self):
        line = {"pauses": [{"pause_s": .05}, {"pause_s": .3}]}
        by_speaker = {"NARRATOR": [line, line], "ANNA": [{"refused": "x"}]}
        overall = get_summary([line, line, {"refused": "x"}])
        markdown = book_report.get_report_markdown(
            "test_book", by_speaker, {"NARRATOR": "lora: husky", "ANNA": "custom: Vivian"}, overall, 3, 10, "/data")
        self.assertIn("Measured 3 of 10 rendered chunks", markdown)
        self.assertIn("| NARRATOR (lora: husky) | 2 | 0 | 4 | 0.17 s | 50%", markdown)
        self.assertIn("| ANNA (custom: Vivian) | 1 | 1 | 0 | – | – |", markdown)
        self.assertNotIn("said", markdown.lower().split("## how to read this")[0])

    def test_report_list_labels_comma_reports_apart_from_reviews(self):
        from routers import editor
        with tempfile.TemporaryDirectory() as directory:
            for name in ("comma_pauses_book_20261025-1200.md", "batch_review_x.md", "review_y.md", "notes.txt"):
                Path(directory, name).write_text("# report\n", encoding="utf-8")
            with patch.object(editor, "REPORTS_DIR", directory):
                kinds = {entry["filename"]: entry["type"] for entry in editor._get_reports()}
        self.assertEqual({"comma_pauses_book_20261025-1200.md": "pauses", "batch_review_x.md": "batch",
                          "review_y.md": "review"}, kinds)


if __name__ == "__main__":
    unittest.main()
