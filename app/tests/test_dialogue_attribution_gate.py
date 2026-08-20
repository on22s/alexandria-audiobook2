"""The measurability gate must not outlive the guess it was built for.

`measurable()` refuses a book whose entries carry too few quotation marks,
because a quote-based detector cannot see dialogue it cannot find. That is
right, and it was paid for: the detector once found 22 spoken lines in a
6,173-entry book and reported 59.1%.

It became wrong the moment `spoken` existed. `classify()` already prefers the
recorded fact over the punctuation guess, so a book carrying a source-derived
map can be measured whatever its punctuation looks like. The gate ran first and
never consulted it, and on 2026-08-20 that refused 28 of 29 retrofitted books -
each reported as "does not mark dialogue with quotes" while carrying a map
built from a source that marks dialogue thousands of times.

A guard built for a guess, still blocking after the guess was replaced. These
tests pin both halves: the refusal still works where it should, and the
recorded fact overrides it where it should.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(REPO / "app" / "experiments"))


class GateTest(unittest.TestCase):
    def setUp(self):
        from experiments import measure_dialogue_attribution as m
        self.m = m

    def _unquoted(self, n, **extra):
        return [{"text": f"Say something plain number {i}", **extra}
                for i in range(n)]

    def test_a_book_with_no_quotes_and_no_map_is_still_refused(self):
        """The original protection. Without it the detector reports a figure
        that describes itself rather than the book."""
        ok, why = self.m.measurable(self._unquoted(200))
        self.assertFalse(ok)
        self.assertIn("quotation marks", why)

    def test_the_recorded_fact_overrides_the_punctuation_gate(self):
        """Same book, same absent punctuation, but every line mapped from the
        source. This is the case that was refused 28 times."""
        ok, why = self.m.measurable(self._unquoted(200, spoken=True))
        self.assertTrue(ok, why)
        self.assertIsNone(why)

    def test_spoken_false_counts_as_recorded_too(self):
        """`spoken: False` is an answer - this line is narration. Only an
        ABSENT key means the line could not be located."""
        ok, _ = self.m.measurable(self._unquoted(200, spoken=False))
        self.assertTrue(ok)

    def test_a_barely_mapped_book_does_not_slip_through(self):
        """Retrofitting locates 89-96% on this corpus. A book far below that
        has not really been mapped, and must fall back to the quote gate
        rather than be measured on a handful of located lines."""
        entries = self._unquoted(180) + self._unquoted(20, spoken=True)
        ok, why = self.m.measurable(entries)
        self.assertFalse(ok)
        self.assertIn("quotation marks", why)

    def test_an_empty_book_is_refused_before_any_division(self):
        ok, why = self.m.measurable([])
        self.assertFalse(ok)
        self.assertEqual("no entries", why)

    def test_classify_prefers_the_record_over_the_punctuation(self):
        """The gate and the classifier must agree about what counts as speech,
        or a book passes the gate and is then scored by the other rule."""
        entries = [{"text": "no quotes here at all", "spoken": True,
                    "speaker": "RUDI"},
                   {"text": '"quoted but narration"', "spoken": False,
                    "speaker": "NARRATOR"}]
        got = self.m.classify(entries)
        self.assertEqual(1, got["spoken_lines"])
        self.assertEqual(0, got["left_with_narrator"])


if __name__ == "__main__":
    unittest.main()
