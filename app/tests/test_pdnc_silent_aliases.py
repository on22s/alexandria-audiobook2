"""A correct short name that PDNC also lists as a character who never speaks.

TheSignOfTheFour's Wooden-Legged Man speaks 100 lines and carries the alias
Jonathan Small; PDNC also lists "Small" with zero quotations. A model answering
SMALL named the right man and was scored as naming someone else. On the
nine-novel panel that single entry flipped 10 of 57 paired adapter verdicts.

What these tests pin:
  - the reviewed pairs reach the committed fixtures and the shared scorer;
  - the two look-alikes review REJECTED stay rejected ("A Small, Dark, Brisk
    Man" is Williams; nobody calls Anne "Miss Elliot");
  - the builder refuses a pair whose short name has a quotation, so a corpus
    update cannot turn a reviewed alias into a wrong merge;
  - the fixtures' OLD alias groups fail the SMALL case, so the fixtures cannot
    quietly stop discriminating (Rule 21).
"""
import csv
import json
import os
import sys
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(REPO, "app"))
from experiments.pdnc_fixture import REVIEWED_SILENT_ALIASES, build  # noqa: E402
from experiments.scoring import alias_groups, same_speaker  # noqa: E402

FIXTURES = os.path.join(REPO, "app", "fixtures")


def groups_for(book):
    with open(os.path.join(FIXTURES, f"attribution_gold_pdnc_{book}.json"),
              encoding="utf-8") as handle:
        return alias_groups(json.load(handle))


def write_novel(root, name, chars, quotes):
    folder = os.path.join(root, name)
    os.makedirs(folder)
    with open(os.path.join(folder, "character_info.csv"), "w", newline="",
              encoding="utf-8") as handle:
        w = csv.writer(handle)
        w.writerow(["Character ID", "Main Name", "Aliases", "Gender", "Category"])
        for n, (main, aliases) in enumerate(chars):
            w.writerow([n, main, repr(aliases), "X", "minor"])
    text = "x" * 50
    with open(os.path.join(folder, "quotation_info.csv"), "w", newline="",
              encoding="utf-8") as handle:
        w = csv.writer(handle)
        w.writerow(["quoteID", "quoteText", "quoteByteSpans", "speaker", "quoteType"])
        for n, speaker in enumerate(quotes):
            w.writerow([f"Q{n}", "Hello.", "[[10, 16]]", speaker, "Implicit"])
    with open(os.path.join(folder, "novel_text.txt"), "w", encoding="utf-8") as handle:
        handle.write(text)


class CommittedFixturesTest(unittest.TestCase):

    def test_small_is_the_wooden_legged_man(self):
        self.assertTrue(same_speaker("WOODEN-LEGGED MAN", "SMALL",
                                     groups_for("thesignofthefour")))

    def test_small_is_not_the_small_dark_brisk_man(self):
        self.assertFalse(same_speaker("A SMALL, DARK, BRISK MAN", "SMALL",
                                      groups_for("thesignofthefour")))

    def test_denny_and_reggie(self):
        self.assertTrue(same_speaker("MR. DENNEY", "DENNY",
                                     groups_for("prideandprejudice")))
        self.assertTrue(same_speaker("REGGIE ST CLOUD", "REGGIE",
                                     groups_for("ahandfulofdust")))

    def test_the_wide_context_fixtures_carry_the_same_groups(self):
        for book in ("thesignofthefour", "prideandprejudice"):
            self.assertEqual(groups_for(book), groups_for(book + "_w3200"), book)

    def test_rejected_pairs_stay_rejected(self):
        self.assertFalse(same_speaker("ANNE ELLIOT", "MISS ELLIOT",
                                      groups_for("persuasion")))
        self.assertFalse(same_speaker("EDMUND", "BERTRAM",
                                      groups_for("mansfieldpark")))

    def test_the_old_groups_fail_the_case(self):
        """Negative control: without the reviewed alias SMALL scores wrong."""
        old = [g - {"SMALL"} for g in groups_for("thesignofthefour")]
        self.assertFalse(same_speaker("WOODEN-LEGGED MAN", "SMALL", old))


class BuilderTest(unittest.TestCase):

    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.book = "TheSignOfTheFour"
        self.main = next(iter(REVIEWED_SILENT_ALIASES[self.book]))
        self.short = REVIEWED_SILENT_ALIASES[self.book][self.main]

    def test_a_silent_short_name_joins_the_speakers_group(self):
        write_novel(self.root, self.book,
                    [(self.main, {"Jonathan Small"}), (self.short, [self.short])],
                    [self.main, self.main])
        groups = [set(g) for g in build(self.root, self.book)["aliases"]]
        self.assertIn(self.short.upper(),
                      next(g for g in groups if self.main.upper() in g))

    def test_a_short_name_that_speaks_is_refused(self):
        write_novel(self.root, self.book,
                    [(self.main, {"Jonathan Small"}), (self.short, [self.short])],
                    [self.main, self.short])
        with self.assertRaises(ValueError):
            build(self.root, self.book)


if __name__ == "__main__":
    unittest.main()
