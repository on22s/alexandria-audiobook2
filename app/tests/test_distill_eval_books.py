"""A book named twice is scored twice, and the artifact still says four.

WHAT HAPPENED. An overnight command on tnr-0 read

    --books grimgar03 index18 mushoku16 owarimonogatari3 grimgar03

and nothing noticed. grimgar03 carries 385 of the 772 scoreable lines across
the four gold light novels - about as many as the other three combined - so
scoring it twice would pull the pooled accuracy most of the way toward
grimgar03's own number while the run still named four books.

Separately, every distill_eval artifact from 2026-09-01 and -09-04 carries a
note reading "scored on four gold books" and scored THREE: grimgar03 is absent
from all of them. The note is prose copied between runs. `gold_files` was
correct the whole time and is easy to miss beside a sentence that says
otherwise, so what was scored is now recorded as data by the loop itself.
"""
import os
import sys
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
sys.path.insert(0, os.path.join(REPO, "app", "experiments"))


class BookArgumentTests(unittest.TestCase):

    def test_the_default_covers_all_four_gold_books(self):
        from experiments.distill_eval import BOOKS
        self.assertEqual(
            sorted(BOOKS),
            ["grimgar03", "index18", "mushoku16", "owarimonogatari3"],
            "the largest gold book must not fall out of the default set")

    def test_every_default_book_has_a_gold_fixture(self):
        """A book in the default that cannot load crashes the run, not skips it."""
        from experiments.distill_eval import BOOKS
        for book in BOOKS:
            path = os.path.join(REPO, "app", "fixtures",
                                f"attribution_gold_{book}.json")
            self.assertTrue(os.path.exists(path), f"missing gold for {book}")

    def test_grimgar03_is_the_largest_and_so_the_costliest_to_drop(self):
        """Pins WHY the duplicate and the omission both mattered.

        If this ever stops being true the reasoning in the refusal message
        needs revisiting, so it is asserted rather than described.
        """
        import json
        def rows(b):
            with open(os.path.join(REPO, "app", "fixtures",
                                   f"attribution_gold_{b}.json"),
                      encoding="utf-8") as fh:
                d = json.load(fh)
            return len(d["entries"] if isinstance(d, dict) else d)
        g = rows("grimgar03")
        others = sum(rows(b) for b in ("index18", "mushoku16", "owarimonogatari3"))
        self.assertGreater(g, others * 0.9,
                           "grimgar03 should be comparable to the other three "
                           "combined; if not, the weighting argument changes")

    def test_duplicate_books_are_refused_not_deduplicated(self):
        """Silently deduplicating would hide the typo from whoever wrote it."""
        import experiments.distill_eval as m
        src = open(m.__file__, encoding="utf-8").read()
        self.assertIn("names {', '.join(dupes)} more than once", src,
                      "the duplicate check must refuse and name the books")
        self.assertIn("weighted twice", src,
                      "the refusal must say why a repeat matters")

    def test_what_was_scored_is_recorded_as_data(self):
        import experiments.distill_eval as m
        src = open(m.__file__, encoding="utf-8").read()
        self.assertIn('record.meta["books_scored"].append(book)', src,
                      "books_scored must be appended inside the loop, so it "
                      "cannot disagree with what actually ran")
        self.assertIn('record.meta["books_requested"]', src)


if __name__ == "__main__":
    unittest.main()
