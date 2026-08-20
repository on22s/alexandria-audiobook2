"""The new 5.3 test: compare the arms on the dialogue map, not on punctuation.

Goal 5.3's original metric pairs lines by `re.sub(r"[^0-9a-z]+", "", ...)`, so
it cannot see what either arm does to the text. Since 1f6be7a the right answer
is not to count punctuation at all: `dialogue_spans` marks each entry with
`spoken` and `source_span` from the SOURCE, before any model runs. That is a
carried fact - no normalisation deletes it, and it does not depend on either
arm's quote habits.

These tests pin the three ways the comparison could report a comfortable
non-result: pairing lines that are not the same line, counting an unlocated
line as narration, and scoring an arm that predates the feature as though it
had failed.
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(REPO / "app" / "experiments"))


class PairingTest(unittest.TestCase):
    def setUp(self):
        from experiments import dialogue_map_compare as m
        self.m = m

    def test_lines_pair_on_the_source_span(self):
        """The span is where the line sits in the book, so it identifies the
        same passage across two different segmentations - which is what the old
        alphanumeric key was only approximating."""
        self.assertEqual(("span", 10, 40),
                         self.m.key_of({"source_span": [10, 40], "text": "x"}))

    def test_an_unlocated_line_has_no_key_and_cannot_be_paired(self):
        """Falling back to text here would pair a line the source could not
        locate with some other arm's line that merely reads the same, and then
        report agreement about a passage neither arm actually found."""
        self.assertIsNone(self.m.key_of({"text": "somewhere"}))

    def test_a_malformed_span_is_not_treated_as_a_span(self):
        self.assertIsNone(self.m.key_of({"source_span": [5], "text": "x"}))
        self.assertIsNone(self.m.key_of({"source_span": "10-40", "text": "x"}))


class ProfileTest(unittest.TestCase):
    def setUp(self):
        from experiments import dialogue_map_compare as m
        self.m = m

    def _script(self, entries):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8")
        json.dump(entries, fh)
        fh.close()
        return fh.name

    def test_missing_spoken_is_unlocated_not_narration(self):
        """`spoken` absent means the line could not be found in the source.
        Counting it as `spoken: false` would turn a mapping failure into a
        confident claim that the line is narration - the single most likely way
        this probe could lie."""
        path = self._script([{"text": "a", "spoken": True, "source_span": [0, 1]},
                             {"text": "b"}])
        got = self.m.profile(path)
        self.assertEqual(2, got["entries"])
        self.assertEqual(1, got["located"])
        self.assertEqual(1, got["spoken"])
        self.assertEqual(1.0, got["spoken_rate"])   # over LOCATED, not entries

    def test_rates_are_none_rather_than_zero_when_nothing_located(self):
        """0% located and 0% spoken read as measurements. None reads as 'not
        measured', which is what a pre-1f6be7a script actually is."""
        got = self.m.profile(self._script([{"text": "a"}, {"text": "b"}]))
        self.assertEqual(0, got["located"])
        self.assertIsNone(got["spoken_rate"])

    def test_an_empty_text_entry_is_not_counted_at_all(self):
        got = self.m.profile(self._script([{"text": ""}, {"text": "a",
                                                          "spoken": False,
                                                          "source_span": [0, 1]}]))
        self.assertEqual(1, got["entries"])


class ComparisonTest(unittest.TestCase):
    def setUp(self):
        from experiments import dialogue_map_compare as m
        self.m = m

    def _script(self, entries):
        fh = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False,
                                         encoding="utf-8")
        json.dump(entries, fh)
        fh.close()
        return fh.name

    def test_agreement_is_computed_only_over_lines_both_arms_located(self):
        """An arm that locates fewer lines is not thereby more accurate. If
        agreement were taken over one arm's total, the arm that found least
        would score best."""
        single = self._script([
            {"text": "a", "spoken": True, "source_span": [0, 1]},
            {"text": "b", "spoken": False, "source_span": [2, 3]},
            {"text": "c", "spoken": True, "source_span": [4, 5]}])
        three = self._script([
            {"text": "a", "spoken": True, "source_span": [0, 1]},
            {"text": "b", "spoken": False, "source_span": [2, 3]}])
        got = self.m.compare(single, three)
        self.assertEqual(2, got["paired_entries"])
        self.assertEqual(1.0, got["agreement_rate"])

    def test_a_systematic_disagreement_is_reported_with_a_p_value(self):
        """If one arm calls speech what the other calls narration, the reader's
        next question is whether that is systematic. Answered with the shared
        McNemar rather than a second implementation."""
        single = self._script([{"text": f"l{i}", "spoken": True,
                                "source_span": [i, i + 1]} for i in range(12)])
        three = self._script([{"text": f"l{i}", "spoken": False,
                               "source_span": [i, i + 1]} for i in range(12)])
        got = self.m.compare(single, three)
        self.assertEqual(12, got["single_says_spoken_only"])
        self.assertEqual(0, got["three_pass_says_spoken_only"])
        self.assertIn("mcnemar_p", got)
        self.assertLess(got["mcnemar_p"], 0.01)

    def test_identical_arms_need_no_p_value(self):
        s = [{"text": "a", "spoken": True, "source_span": [0, 1]}]
        got = self.m.compare(self._script(s), self._script(list(s)))
        self.assertEqual(1.0, got["agreement_rate"])
        self.assertNotIn("mcnemar_p", got)


class WiringTest(unittest.TestCase):
    """Both arms must carry the map, or the comparison measures which arm got
    patched rather than which design is better."""

    def test_both_generators_import_the_dialogue_map(self):
        for name in ("generate_script.py", "three_pass_generate.py"):
            with self.subTest(generator=name):
                src = (REPO / "app" / name).read_text(encoding="utf-8")
                self.assertIn("from dialogue_spans import", src)
                self.assertIn("mark_entries", src)

    def test_three_pass_records_a_mapping_failure_instead_of_hiding_it(self):
        """A script silently missing `spoken` reads downstream as a book with
        no dialogue at all, so the except must say so rather than pass."""
        src = (REPO / "app" / "three_pass_generate.py").read_text(encoding="utf-8")
        self.assertIn("Dialogue map FAILED", src)


if __name__ == "__main__":
    unittest.main()
