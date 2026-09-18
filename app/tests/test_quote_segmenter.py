"""Hand-checked cases for the quote-mark segmenter, including the ones it
must REJECT (Rule 21): apostrophes are not quotes, and a paragraph break
closes a span. Every fixture is text as a novel prints it."""
import unittest

from experiments.quote_segmenter import segment, score_novel


def texts(t):
    return [t[a:b] for a, b in segment(t)]


class SegmentTest(unittest.TestCase):
    def test_straight_quotes_alternate(self):
        t = '"Hello," she said. "Go away."'
        self.assertEqual(texts(t), ["Hello,", "Go away."])

    def test_curly_doubles_pair_by_shape(self):
        self.assertEqual(texts("“Yes,” he said, “now.”"), ["Yes,", "now."])

    def test_apostrophes_are_not_single_quotes(self):
        t = "‘I don’t know Tom’s plan,’ she said."
        self.assertEqual(texts(t), ["I don’t know Tom’s plan,"])

    def test_a_single_quote_inside_a_word_never_opens(self):
        self.assertEqual(texts("It was o’clock and rock ’n’ roll."), [])

    def test_paragraph_break_closes_and_the_next_mark_reopens(self):
        t = '"First paragraph of a long speech.\n\n"Second paragraph." He stopped.'
        self.assertEqual(texts(t), ["First paragraph of a long speech.", "Second paragraph."])

    def test_unclosed_quote_at_end_yields_nothing(self):
        self.assertEqual(texts('He wrote "and then'), [])


class ScoreTest(unittest.TestCase):
    def test_found_needs_95_percent_coverage_and_true_needs_half_inside(self):
        text = "x" * 100
        gold = [(10, 30), (50, 70)]
        # first predicted span covers 20/20 of the first gold; second covers
        # 10/20 of the second (not found) and is 10/25 inside gold (not true)
        r = score_novel(text, gold, [(10, 30), (60, 85)])
        self.assertEqual((r["found"], r["true"]), (1, 1))
        self.assertEqual(r["recall"], 0.5)
        self.assertEqual(r["precision"], 0.5)
