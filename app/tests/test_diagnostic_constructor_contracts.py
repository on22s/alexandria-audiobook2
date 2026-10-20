"""Run diagnostic entry points with real inputs/params and CPU-only providers."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import tempfile
import unittest
from unittest.mock import patch

import three_pass_generate as tp
from experiments import quote_fallthrough, three_pass_chunk_probe

APP = Path(__file__).resolve().parent.parent


class DiagnosticConstructorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_chunk_probe_constructs_auto_params_and_publishes_real_quality(self):
        text = '"This is the complete spoken fixture."'
        source = self.root / "book.txt"
        source.write_text(text, encoding="utf-8")
        (self.root / "config.json").write_text(json.dumps({
            "generation": {"chunk_size": 6000},
            "llm": {"base_url": "http://127.0.0.1:9/v1", "model_name": "fixture"}}))
        out = self.root / "result.json"
        seen = []

        def run(client, model, chunk, params, *args, **kwargs):
            seen.append((chunk, params))
            return [{"text": text, "speaker": "ALICE", "instruct": "Neutral, even narration."}]

        with patch.object(three_pass_chunk_probe, "APP", str(self.root)), \
             patch("openai.OpenAI"), patch.object(tp, "run_three_pass", side_effect=run), \
             patch("sys.argv", ["probe", "--source", str(source), "--chunk", "1", "--out", str(out)]), \
             contextlib.redirect_stdout(io.StringIO()):
            three_pass_chunk_probe.main()
        self.assertEqual(1, len(seen))
        self.assertEqual("auto", seen[0][1].segmentation)
        self.assertEqual(0.0, seen[0][1].segment_temperature)
        doc = json.loads(out.read_text())
        self.assertTrue(doc["passed"], doc)
        self.assertEqual(1, doc["entry_count"])
        self.assertEqual(1, doc["total_chunks"])

    def test_quote_fallthrough_constructs_llm_params_and_scores_one_native_job(self):
        text = '"This is the complete spoken fixture."'
        matrix = self.root / "matrix"
        inputs = matrix / "inputs"
        inputs.mkdir(parents=True)
        (inputs / "book.txt").write_text(text, encoding="utf-8")
        checkpoint = matrix / quote_fallthrough.INPUT_RUN / "book"
        checkpoint.mkdir(parents=True)
        (checkpoint / "result.json.threepass_checkpoint.json").write_text("{}")
        fixtures = self.root / "ab_test_runtime" / "fixtures_draft"
        fixtures.mkdir(parents=True)
        (fixtures / "labelling_bundle__book.json").write_text(json.dumps({"entries": [
            {"line": "This is the complete spoken fixture.", "expected_speaker": "ALICE"}]}))
        out = self.root / "quote.json"
        seen = []

        def segment(client, model, chunk, params):
            seen.append(params)
            return [{"text": "This is the complete spoken fixture.", "type": "SPOKEN"}]

        with patch.object(quote_fallthrough, "REPO", str(self.root)), \
             patch.object(quote_fallthrough, "M", str(matrix) + "/"), \
             patch.object(quote_fallthrough, "OpenAI"), \
             patch.object(quote_fallthrough, "load_generation_delta_checkpoint",
                          return_value={"resolutions": ["quote_presegmented_repaired"]}), \
             patch.object(quote_fallthrough, "segment_chunk_adaptively", side_effect=segment), \
             patch("sys.argv", ["probe", "--out", str(out)]), \
             contextlib.redirect_stdout(io.StringIO()):
            quote_fallthrough.main()
        self.assertEqual(1, len(seen))
        self.assertEqual("llm", seen[0].segmentation)
        self.assertEqual("none", seen[0].reasoning_effort)
        doc = json.loads(out.read_text())
        self.assertEqual(1, doc["chunks"])
        self.assertEqual([1, 1], doc["score"]["model"])
        self.assertEqual([1, 1], doc["score"]["presegmented"])

    def test_two_by_two_initializes_paths_and_executes_all_four_one_item_arms(self):
        from experiments.manifest import ExperimentRecord
        from tests.test_experiment_manifest import LOADED
        text = "This is the complete spoken fixture."
        gold = self.root / "gold.json"
        gold.write_text(json.dumps({"entries": [{"line": text, "expected_speaker": "ALICE"}]}))
        source = self.root / "book.txt"
        source.write_text(text, encoding="utf-8")
        opened = []
        real_open = open
        out = self.root / "two_by_two.json"
        root = self.root

        class FixtureRecord(ExperimentRecord):
            def __init__(self, *args, **kwargs):
                super().__init__("fixture", str(root), "fixture", "http://127.0.0.1:9/v1",
                                 str(gold), {}, environment=LOADED)

            def write(self, path):
                return super().write(str(out))

        def open_input(path, *args, **kwargs):
            name = str(path)
            if name.endswith("fixtures/attribution_gold_random.json"):
                opened.append(name)
                return real_open(gold, *args, **kwargs)
            if name.endswith("inputs/mushoku16.txt"):
                opened.append(name)
                return real_open(source, *args, **kwargs)
            return real_open(path, *args, **kwargs)

        calls = []

        def attribute(client, model, frozen, params, **kwargs):
            calls.append(kwargs.get("neighbor_contexts"))
            return [{"text": entry["text"], "speaker": "ALICE"} for entry in frozen]

        with patch("builtins.open", side_effect=open_input), patch("openai.OpenAI"), \
             patch("generation_checkpoint_deltas.load_generation_delta_checkpoint", return_value={
                 "segmented": [{"type": "SPOKEN", "text": text}],
                 "named": [{"speaker": "ALICE", "text": text}]}), \
             patch.object(tp, "attribute_batch", side_effect=attribute), \
             patch("experiments.manifest.ExperimentRecord", FixtureRecord), \
             patch.dict("os.environ", {"EXPERIMENT_BOOK": "mushoku16",
                                        "EXPERIMENT_GOLD": "fixtures/attribution_gold_random.json"}), \
             contextlib.redirect_stdout(io.StringIO()):
            namespace = runpy.run_path(str(APP / "experiments" / "two_by_two.py"), run_name="__main__")
        self.assertEqual(str(APP.parent), namespace["REPO"])
        self.assertEqual(2, len(opened))
        self.assertTrue(all(name.startswith(str(APP.parent)) for name in opened))
        self.assertEqual(4, len(calls))
        self.assertEqual([None, None], calls[:2])
        self.assertTrue(all(context is not None for context in calls[2:]))
        doc = json.loads(out.read_text())
        self.assertEqual({"A", "B", "C", "D"}, {row["arm"] for row in doc["rows"]})
        self.assertTrue(all(row["correct"] for row in doc["rows"]))


if __name__ == "__main__":
    unittest.main()
