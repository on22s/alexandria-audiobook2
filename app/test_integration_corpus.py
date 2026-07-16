import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from integration_corpus import build_manifest, select_passages
import integration_runner


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

    def test_runner_writes_case_results_incrementally(self):
        text = "one two three four five"
        manifest = {"books": [{"name": "book.txt", "passages": [{
            "category": "opening_narration", "text": text, "sha256": "abc"}]}]}

        def process(_client, _model, chunk, _index, _total, _params,
                    max_retries, attempt_observer):
            attempt_observer({"attempt": 1, "finish_reason": "stop"})
            return [{"speaker": "NARRATOR", "text": chunk, "instruct": "neutral"}]

        with tempfile.TemporaryDirectory() as tmp, \
             patch.object(integration_runner, "load_app_config", return_value={
                 "llm": {"base_url": "http://localhost:1234/v1", "model_name": "model"}}), \
             patch.object(integration_runner, "ensure_ideal_settings",
                          return_value=(False, {"context_length": 8192}, "ready")), \
             patch.object(integration_runner, "OpenAI"), \
             patch.object(integration_runner, "process_chunk", side_effect=process):
            output = str(Path(tmp, "report.json"))
            report = integration_runner.run_manifest(manifest, output)

            self.assertTrue(Path(output).is_file())
        self.assertEqual("passed", report["cases"][0]["status"])
        self.assertEqual(1, len(report["cases"][0]["attempts"]))
        self.assertEqual({"total": 1, "passed": 1},
                         report["summary"]["by_category"]["opening_narration"])
        self.assertEqual(0, report["summary"]["retry_cases"])


if __name__ == "__main__":
    unittest.main()
