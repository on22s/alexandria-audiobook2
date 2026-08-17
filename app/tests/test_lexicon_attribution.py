"""The language attribution behind the pronunciation lexicon.

Two rules, each of which has been wrong once in this file's short life: which
books get asked for a translator's note at all, and how much evidence a verdict
needs before it is allowed to be one. Both are tested through the real
functions - an earlier draft of this file re-implemented the branch it was
checking, which would have passed against the broken version.
"""
import unittest
from unittest.mock import patch

from experiments import lexicon_language_attribution as attribution


class ResolveBookLanguageTests(unittest.TestCase):
    """A publisher that settles nothing must still reach the marker test.

    Asking only about `unknown` skipped every `mixed` publisher - Seven Seas,
    627 books, the largest source of Chinese titles in the corpus - so the
    corpus came back with zero Chinese verdicts.
    """

    def _resolve(self, declared, marker="zh"):
        asked = []

        def spy(path):
            asked.append(path)
            return marker, {"zh": 4, "ja": 0}

        meta = {"publisher": "Some Imprint", "language_of": declared, "creators": []}
        with patch.object(attribution, "book_metadata", lambda path: dict(meta)), \
             patch.object(attribution, "back_matter_language", spy):
            return attribution.resolve_book_language("/library/some.epub"), asked

    def test_mixed_publisher_is_asked_for_a_translator_note(self):
        resolved, asked = self._resolve("mixed")
        self.assertEqual(1, len(asked), "a `mixed` publisher must reach the marker test")
        self.assertEqual("zh", resolved["language_of"])
        self.assertEqual("back_matter", resolved["attributed_by"])

    def test_unknown_publisher_is_still_asked(self):
        resolved, asked = self._resolve("unknown")
        self.assertEqual(1, len(asked))
        self.assertEqual("zh", resolved["language_of"])

    def test_an_unambiguous_publisher_is_never_overruled_by_markers(self):
        # A Japanese-only imprint must not be relabelled by a stray "wuxia" in
        # an afterword: the publisher is the stronger claim.
        resolved, asked = self._resolve("ja", marker="zh")
        self.assertEqual([], asked)
        self.assertEqual("ja", resolved["language_of"])
        self.assertNotIn("attributed_by", resolved)

    def test_a_silent_translator_note_leaves_the_book_as_it_was(self):
        # `mixed` must stay `mixed`, not decay to `unknown` - it still carries
        # the information that the publisher exists and straddles.
        resolved, asked = self._resolve("mixed", marker="unknown")
        self.assertEqual(1, len(asked))
        self.assertEqual("mixed", resolved["language_of"])
        self.assertNotIn("attributed_by", resolved)

    def test_the_returned_metadata_is_not_the_object_it_was_given(self):
        # Rule 17: resolve_book_language reports by returning, so a caller
        # holding the original metadata does not see it mutate underneath.
        original = {"publisher": "X", "language_of": "mixed", "creators": []}
        with patch.object(attribution, "book_metadata", lambda path: original), \
             patch.object(attribution, "back_matter_language",
                          lambda path: ("zh", {"zh": 4, "ja": 0})):
            resolved = attribution.resolve_book_language("/library/x.epub")
        self.assertEqual("zh", resolved["language_of"])
        self.assertEqual("mixed", original["language_of"])


class TermVerdictTests(unittest.TestCase):
    """`dantian` was labelled Japanese on 2 books out of 37 because the rule
    asked only whether the other language was zero."""

    def test_two_books_cannot_outvote_thirty_three_unresolved_ones(self):
        self.assertEqual("unattributed", attribution.get_term_verdict(
            {"ja": 2, "unknown": 33, "mixed": 2}))

    def test_a_clear_majority_of_resolved_books_decides(self):
        self.assertEqual("zh", attribution.get_term_verdict({"zh": 20, "unknown": 3}))
        self.assertEqual("ja", attribution.get_term_verdict({"ja": 20, "unknown": 3}))

    def test_a_term_used_in_both_traditions_straddles_rather_than_picking(self):
        self.assertEqual("straddles", attribution.get_term_verdict({"ja": 9, "zh": 7}))

    def test_too_little_evidence_is_unattributed_even_when_unanimous(self):
        # Unanimous but thin: two books agreeing is not a corpus.
        self.assertEqual("unattributed", attribution.get_term_verdict({"zh": 2}))
        self.assertEqual("zh", attribution.get_term_verdict({"zh": 3}))

    def test_an_exact_half_of_the_evidence_is_accepted(self):
        # Pinning the boundary rather than assuming it: the rule is
        # `confident / total < 0.5` -> unattributed, so an exact half is
        # ACCEPTED. Defensible - the three resolved books carry real evidence
        # while the three unknowns carry none - but it is a decision, and the
        # next person to touch this should see it stated instead of
        # rediscovering it. One book fewer and it flips.
        self.assertEqual("zh", attribution.get_term_verdict({"zh": 3, "unknown": 3}))
        self.assertEqual("unattributed",
                         attribution.get_term_verdict({"zh": 3, "unknown": 4}))

    def test_a_term_nobody_saw_is_unattributed_rather_than_an_error(self):
        self.assertEqual("unattributed", attribution.get_term_verdict({}))


class ExtractorFailureTests(unittest.TestCase):
    """A missing dependency must not be reported as 5,224 unreadable books.

    The import used to sit inside `except Exception: return "unknown", {}`, so
    running this file as a script - which leaves `routers` off sys.path - made
    every book "unknown" and the corpus came back with zero Chinese verdicts.
    """

    def test_an_import_failure_is_raised_not_folded_into_a_verdict(self):
        with patch.object(attribution, "get_epub_extractor",
                          side_effect=RuntimeError("cannot import the EPUB extractor")):
            with self.assertRaisesRegex(RuntimeError, "cannot import"):
                attribution.back_matter_language("/library/any.epub")

    def test_the_extractor_is_importable_the_way_the_script_runs(self):
        # The regression itself: this resolves only because the module puts
        # app/ on sys.path at import time.
        self.assertTrue(callable(attribution.get_epub_extractor()))


class MarkerRuleTests(unittest.TestCase):
    def test_balanced_evidence_stays_unknown_rather_than_guessing(self):
        text = "the kanji here and the pinyin there, more kanji, more pinyin"
        with patch.object(attribution, "get_epub_extractor", lambda: (lambda p: text)):
            verdict, hits = attribution.back_matter_language("/library/tie.epub")
        self.assertEqual(hits["ja"], hits["zh"], "this fixture is meant to tie")
        self.assertEqual("unknown", verdict)

    def test_an_unreadable_book_is_unknown_rather_than_an_exception(self):
        def explode(path):
            raise OSError("not a zip file")

        with patch.object(attribution, "get_epub_extractor", lambda: explode):
            verdict, hits = attribution.back_matter_language("/library/broken.epub")
        self.assertEqual("unknown", verdict)
        self.assertEqual({}, hits)


if __name__ == "__main__":
    unittest.main()
