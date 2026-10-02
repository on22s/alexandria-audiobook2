"""Repeat accuracy includes gold lines lost before attribution."""
import copy
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from generation_checkpoint_deltas import GenerationCheckpointDeltas

from tests.test_experiment_infra import stage_index_scripts

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))
from tools.audit.pipeline_repeat_scoring import get_pipeline_repeat_scores


def get_fixture():
    gold = {"aliases": [["Alice", "Al"]], "entries": [
        {"id": i, "line": line, "expected_speaker": "Alice"}
        for i, line in enumerate(("Hello, world!", "Missing line", "Repeated line",
                                  "Unnamed line", "Merged line", "Wrong voice"))]}
    texts = ("Hello world", "Repeated line", "Repeated line", "Unnamed line",
             "Merged line plus more", "Wrong voice")
    checkpoint = {"segmented": [{"text": text} for text in texts],
                  "named": [{"text": text, "speaker": "AL" if i == 0 else "Bob"}
                            for i, text in enumerate(texts) if text != "Unnamed line"]}
    return checkpoint, gold


class PipelineRepeatScoringTests(unittest.TestCase):
    def test_missing_duplicate_unnamed_merged_and_wrong_lines_are_in_denominator(self):
        checkpoint, gold = get_fixture()
        original = copy.deepcopy((checkpoint, gold))
        score = get_pipeline_repeat_scores(checkpoint, gold)
        self.assertEqual(score["scored"], {0: True, 1: False, 2: False,
                                           3: False, 4: False, 5: False})
        self.assertEqual((score["n"], score["correct"], score["matched"]), (6, 1, 2))
        self.assertEqual(score["accuracy_pct"], 16.7)
        self.assertEqual((checkpoint, gold), original)

    def test_duplicate_gold_ids_fail_loudly_and_empty_population_is_explicit(self):
        checkpoint, gold = get_fixture()
        gold["entries"][1]["id"] = 0
        with self.assertRaisesRegex(ValueError, "unique"):
            get_pipeline_repeat_scores(checkpoint, gold)
        self.assertEqual(get_pipeline_repeat_scores({}, {"entries": []})["n"], 0)
        self.assertEqual(get_pipeline_repeat_scores({}, {"entries": []})["accuracy_pct"], "")

    def test_native_collect_and_standalone_reports_use_full_gold_population(self):
        checkpoint, gold = get_fixture()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            stage_index_scripts(directory)
            fixtures = root / "app/fixtures"
            fixtures.mkdir()
            (fixtures / "attribution_gold_grimgar03_provisional.json").write_text(json.dumps(gold))
            repeats = root / "ab_test_runtime/pipeline_repeats"
            repeats.mkdir(parents=True)
            (repeats / "run1.json.threepass_checkpoint.json").write_text(json.dumps(checkpoint))
            audit = root / "ab_test_runtime/audit"
            audit.mkdir()
            for name in ("artifact_structural_audit", "legacy_attribution_audit"):
                (audit / (name + ".json")).write_text('{"artifacts": []}')
            collector = subprocess.run([sys.executable, str(root / "tools/audit/collect_results.py"),
                                        "--rescore-repeats"], cwd=root, capture_output=True, text=True)
            self.assertEqual(collector.returncode, 0, collector.stdout + collector.stderr)
            row = json.loads((repeats / "repeat_scores.json").read_text())["rows"][0]
            self.assertEqual((row["n"], row["correct"], row["accuracy_pct"]), (6, 1, 16.7))
            writer = GenerationCheckpointDeltas(repeats / "run1.json.threepass_checkpoint.json")
            writer.save_checkpoint({"segmented": [], "named": []})
            writer.save_checkpoint(checkpoint)
            replayed = subprocess.run([sys.executable, str(root / "tools/audit/collect_results.py"),
                                       "--rescore-repeats"], cwd=root, capture_output=True, text=True)
            self.assertEqual(replayed.returncode, 0, replayed.stdout + replayed.stderr)
            row = json.loads((repeats / "repeat_scores.json").read_text())["rows"][0]
            self.assertEqual((row["n"], row["correct"], row["accuracy_pct"]), (6, 1, 16.7))
            shutil.copy2(REPO / "ab_test_runtime/pipeline_repeats/score_repeats.py", repeats)
            report = subprocess.run([sys.executable, str(repeats / "score_repeats.py")],
                                    cwd=root, capture_output=True, text=True)
            self.assertEqual(report.returncode, 0, report.stdout + report.stderr)
            self.assertRegex(report.stdout, r"run1\s+6\s+6\s+1\s+16\.67%")


if __name__ == "__main__":
    unittest.main()
