import tempfile
import unittest
from pathlib import Path

from integration_corpus import build_manifest, select_passages


class IntegrationCorpusTests(unittest.TestCase):
    def test_passage_selection_covers_distinct_text_features(self):
        text = ('Opening narration establishes the setting. “This dialogue passage is long '
                'enough to represent a speaking character clearly,” she said. Wait—what?! '
                + 'x' * 260 + '. café')
        categories = {item["category"] for item in select_passages(text, target_chars=200)}
        self.assertIn("dialogue", categories)
        self.assertIn("expressive_punctuation", categories)
        self.assertIn("non_ascii", categories)
        self.assertIn("long_sentence", categories)

    def test_manifest_is_deterministic_and_records_bad_books(self):
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "b.txt").write_text("Second book text.", encoding="utf-8")
            Path(tmp, "a.txt").write_text("First book text.", encoding="utf-8")
            Path(tmp, "empty.txt").write_text("", encoding="utf-8")

            first = build_manifest(tmp, max_books=10, target_chars=200)
            second = build_manifest(tmp, max_books=10, target_chars=200)

        self.assertEqual(first, second)
        self.assertEqual(["a.txt", "b.txt"], [book["name"] for book in first["books"]])
        self.assertEqual("empty.txt", first["errors"][0]["name"])


if __name__ == "__main__":
    unittest.main()
