"""Hand-checked cases for the source-to-script coverage measure.

The measure exists because nothing else in the repository compares the script
to the BOOK: `check_text_loss` compares the review's output to the review's
input, and `norm_text` strips punctuation before pairing arms. Its first run
found coverage 0.969-0.993 across seven books - never 1.0.

A ratio that cannot be wrong is not a measurement, so these fix the behaviour
at both ends: perfect coverage must read exactly 1.0, and known losses must
read exactly the fraction lost, computed by hand rather than by running the
code and blessing whatever came out.
"""
import collections
import os
import tempfile
import unittest

from experiments.source_coverage import (coverage, pair_sources_to_scripts,
                                         script_words, words)


def entries(*texts):
    return [{"speaker": "NARRATOR", "text": t} for t in texts]


class WordSplitTests(unittest.TestCase):
    def test_punctuation_separates_rather_than_joins(self):
        self.assertEqual(words("Hello, world-wide!"), ["hello", "world", "wide"])

    def test_case_is_folded(self):
        self.assertEqual(words("The THE the"), ["the", "the", "the"])

    def test_cjk_and_kana_survive(self):
        # A Japanese or Chinese book must be measurable, not scored as total
        # loss because the splitter only knows latin.
        self.assertEqual(words("「愉快ですね」"), ["愉快ですね"])
        self.assertEqual(words("晓霞说"), ["晓霞说"])

    def test_empty_and_none_are_empty(self):
        self.assertEqual(words(""), [])
        self.assertEqual(words(None), [])


class CoverageTests(unittest.TestCase):
    def test_identical_text_is_exactly_one(self):
        ratio, missing, extra, n, m = coverage("a b c", entries("a b c"))
        self.assertEqual(ratio, 1.0)
        self.assertEqual(dict(missing), {})
        self.assertEqual(dict(extra), {})
        self.assertEqual((n, m), (3, 3))

    def test_a_hand_computed_loss(self):
        # Four source words, one absent from the script -> exactly 0.75.
        ratio, missing, _, _, _ = coverage("a b c d", entries("a b c"))
        self.assertEqual(ratio, 0.75)
        self.assertEqual(dict(missing), {"d": 1})

    def test_repeats_are_counted_by_multiset_not_by_set(self):
        # Three copies in the source, one in the script: two are lost. A
        # set-based check would call this perfect coverage.
        ratio, missing, _, _, _ = coverage("the the the", entries("the"))
        self.assertAlmostEqual(ratio, 1 / 3)
        self.assertEqual(dict(missing), {"the": 2})

    def test_order_is_deliberately_not_checked(self):
        ratio, _, _, _, _ = coverage("a b c", entries("c b a"))
        self.assertEqual(ratio, 1.0)

    def test_invented_prose_is_reported_as_extra_not_hidden(self):
        # A script LONGER than its source is a different failure, and the
        # ratio alone cannot show it.
        ratio, missing, extra, _, _ = coverage("a b", entries("a b c d"))
        self.assertEqual(ratio, 1.0)
        self.assertEqual(dict(missing), {})
        self.assertEqual(dict(extra), {"c": 1, "d": 1})

    def test_punctuation_only_change_is_not_a_loss(self):
        # The pipeline legitimately alters quote marks; that must not read as
        # missing words, or every book scores badly for the wrong reason.
        ratio, missing, _, _, _ = coverage('"Go," he said.',
                                           entries("Go he said"))
        self.assertEqual(ratio, 1.0)
        self.assertEqual(dict(missing), {})

    def test_empty_source_does_not_divide_by_zero(self):
        ratio, _, _, _, _ = coverage("", entries("a"))
        self.assertEqual(ratio, 0.0)

    def test_script_words_reads_the_text_field(self):
        self.assertEqual(script_words(entries("a b", "c")), ["a", "b", "c"])
        self.assertEqual(script_words([{"speaker": "X"}]), [])


class PairingTests(unittest.TestCase):
    def setUp(self):
        self.src = tempfile.mkdtemp()
        self.dst = tempfile.mkdtemp()

    def _source(self, name, body):
        with open(os.path.join(self.src, name), "w", encoding="utf-8") as fh:
            fh.write(body)

    def _script(self, name):
        with open(os.path.join(self.dst, name), "w", encoding="utf-8") as fh:
            fh.write("[]")

    def test_identical_sources_under_two_spellings_collapse(self):
        # AHandfulOfDust.txt and pdnc_ahandfulofdust.txt are byte-identical.
        # Counting both reported 11 books where there are 7.
        self._source("Book.txt", "same bytes")
        self._source("pdnc_book.txt", "same bytes")
        self._script("book__single.json")
        pairs, unpaired, dupes = pair_sources_to_scripts(
            self.src, self.dst, "__single.json")
        self.assertEqual(len(pairs), 1)
        self.assertEqual(sum(len(v) for v in dupes.values()), 1)
        self.assertEqual(unpaired, [])

    def test_different_books_with_similar_names_do_not_collapse(self):
        # Guards the opposite error: collapsing by NAME would merge these.
        self._source("Book.txt", "first book")
        self._source("pdnc_book.txt", "a different book entirely")
        self._script("book__single.json")
        pairs, _, dupes = pair_sources_to_scripts(
            self.src, self.dst, "__single.json")
        self.assertEqual(len(pairs), 2)
        self.assertEqual(dupes, {})

    def test_a_source_with_no_script_is_reported_not_scored_zero(self):
        self._source("Orphan.txt", "text")
        pairs, unpaired, _ = pair_sources_to_scripts(
            self.src, self.dst, "__single.json")
        self.assertEqual(pairs, [])
        self.assertEqual(unpaired, ["Orphan.txt"])
