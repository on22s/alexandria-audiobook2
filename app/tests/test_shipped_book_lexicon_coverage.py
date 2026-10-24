"""Goal 5.5's coverage scan must count the shipped books, not the report text.

The first version of this scan parsed the discovery script's STDOUT with awk
and produced three "terms" that were words from the report's own prose -
`candidate`, `only;`, `scripts,` - inflating the uncovered set by 3 and the
term count by 2. These tests pin the parts that failed.
"""
import importlib.util
import json
import os
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "app", "experiments",
                      "shipped_book_lexicon_coverage.py")


def _mod():
    spec = importlib.util.spec_from_file_location("shipcov", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class Parsing(unittest.TestCase):
    def setUp(self):
        self.m = _mod()

    def test_words_are_whole_words_not_substrings(self):
        """`same` inside `sameness` is not an occurrence of `same`. Substring
        matching would inflate every number in this scan."""
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "b.json")
            with open(p, "w", encoding="utf-8") as handle:
                json.dump([{"speaker": "A", "text": "Sameness and samurai.",
                            "instruct": ""}], handle)
            words = self.m.terms_in_script(p)
        self.assertIn("samurai", words)
        self.assertIn("sameness", words)
        self.assertNotIn("same", words)

    def test_sidecars_are_not_mistaken_for_books(self):
        """scripts/ holds generation checkpoints and voice configs beside the
        books; counting them would multiply the book count."""
        with tempfile.TemporaryDirectory() as tmp:
            for n in ("Book One.json",
                      "Book One.json.generation_quality.json",
                      "Book One.voice_config.json"):
                with open(os.path.join(tmp, n), "w", encoding="utf-8") as handle:
                    json.dump([], handle)
            found = self.m.shipped_scripts(tmp)
        self.assertEqual(["Book One.json"],
                         [os.path.basename(f) for f in found])

if __name__ == "__main__":
    unittest.main()
