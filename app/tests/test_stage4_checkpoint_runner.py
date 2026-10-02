import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
import wave
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
import run_stage4_checkpoint as runner

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from experiments.nonprose_replication import summarize
from experiments.nonprose_category_expansion import (
    summarize as category_summarize)


class Stage4CheckpointRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=runner.REPO)
        self.wavs = []

    def tearDown(self):
        self.tmp.cleanup()

    def _wav(self, name):
        path = os.path.join(self.tmp.name, name)
        with wave.open(path, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(16000)
            handle.writeframes(b"\0\0" * 160)
        self.wavs.append(path)
        return os.path.relpath(path, runner.REPO)

    def _artifact(self):
        rows = []
        for label in ("nonprose", "prose"):
            rows.append({
                "adapter": "adapter", "seed": 7, "pair": 0,
                "class": label, "uid": label + "-uid",
                "source_sha256": label + "-sha", "wav": self._wav(label),
                "transcript": "heard", "words": 2, "heard_words": 2,
                "errors": 0, "substitutions": 0, "deletions": 0,
                "insertions": 0, "failed": False, "threshold": 1,
                "non_speech": False, "possible_truncation": False,
            })
        return {
            "status": "complete",
            "provenance": {
                "script": "nonprose_replication.py", "written": "now",
                "host": "test", "git": {"commit": "x",
                "harness_sha256": "0" * 64},
                "args": {"source": "source.json", "config": "config.json",
                         "adapters": ["adapter"], "seeds": [7], "limit": 1,
                         "out_dir": "audio", "out": "artifact.json"},
            },
            "selection": {"pairs": [{
                "nonprose_uid": "nonprose-uid",
                "prose_uid": "prose-uid",
                "nonprose_sha256": "nonprose-sha",
                "prose_sha256": "prose-sha",
                "nonprose_features": {}, "prose_features": {},
                "absolute_feature_gap": {},
            }]},
            "rows": rows,
            "summary": summarize(rows),
        }

    def _write(self, doc):
        path = os.path.join(self.tmp.name, "artifact.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(doc, handle)
        return path

    def test_main_category_counts_follow_actual_category_matrices_and_keep_four_samples(self):
        import copy
        import contextlib
        import io
        from experiments import nonprose_category_expansion as expansion
        actual_validate = runner.validate_stage4_artifact
        for category_count in (5, 6, 7):
            with self.subTest(category_count=category_count):
                categories = tuple(f"category_{index}" for index in range(category_count))
                template = self._artifact()
                commands, validated = [], []
                def validate(path, expected_rows, matrix=None, **kwargs):
                    if kwargs.get("expected_script") != "nonprose_category_expansion.py":
                        return {}  # Non-category checkpoints are independent fixtures.
                    pilot = path == runner.EXPANSION_PILOT
                    limit = 1 if pilot else 4
                    adapters = [runner.DEFAULT_ADAPTERS[0]] if pilot else list(runner.DEFAULT_ADAPTERS)
                    seeds = [1234] if pilot else list(runner.DEFAULT_SEEDS)
                    doc = copy.deepcopy(template)
                    doc["provenance"]["script"] = "nonprose_category_expansion.py"
                    args = doc["provenance"]["args"]
                    args.pop("limit")
                    args.update(adapters=adapters, seeds=seeds, limit_per_category=limit)
                    pairs, rows = [], []
                    for category in categories:
                        for _ in range(limit):
                            pair = copy.deepcopy(template["selection"]["pairs"][0])
                            for suffix in ("uid", "sha256", "features"):
                                pair["probe_" + suffix] = pair.pop("nonprose_" + suffix)
                            pair["category"] = category
                            index = len(pairs)
                            pairs.append(pair)
                            for adapter in adapters:
                                for seed in seeds:
                                    for original in template["rows"]:
                                        row = dict(original, adapter=adapter, seed=seed, pair=index, category=category)
                                        if row["class"] == "nonprose":
                                            row["class"] = "probe"
                                        rows.append(row)
                    doc.update(selection={"pairs": pairs, "categories": list(categories)}, rows=rows,
                               summary=category_summarize(rows))
                    fixture_path = Path(self.tmp.name, f"category-{category_count}-{limit}.json")
                    fixture_path.write_text(json.dumps(doc))
                    before = fixture_path.read_bytes()
                    result = actual_validate(str(fixture_path), expected_rows, matrix, **kwargs)
                    self.assertEqual(before, fixture_path.read_bytes())
                    validated.append((pilot, len(result["rows"]), len(matrix)))
                    return result
                output = io.StringIO()
                actual_exists = os.path.exists
                checkpoint_paths = (runner.FULL, runner.EXPANSION_PILOT, runner.EXPANSION_FULL)
                with patch.object(expansion, "CATEGORIES", categories), \
                     patch.object(runner, "validate_stage4_artifact", side_effect=validate), \
                     patch.object(runner, "_provenance_harness_matches", return_value=True), \
                     patch.object(runner.os.path, "exists", side_effect=lambda path: False if path in checkpoint_paths else actual_exists(path)), \
                     patch.object(runner, "run_gpu_experiment", side_effect=lambda *args: commands.append(args)), \
                     patch.object(runner, "run"), patch.object(runner, "require_index_entries"), \
                     contextlib.redirect_stdout(output):
                    runner.main()
                pilot_rows = category_count * 2
                full_rows = len(runner.DEFAULT_ADAPTERS) * len(runner.DEFAULT_SEEDS) * category_count * 4 * 2
                self.assertEqual([(True, pilot_rows, pilot_rows), (False, full_rows, full_rows)], validated)
                full_command = next(args for args in commands if args[0] == "nonprose_category_expansion_full")
                flags = full_command[3]
                self.assertEqual("4", flags[flags.index("--limit-per-category") + 1])
                self.assertIn(f"({pilot_rows}/{pilot_rows} rows)", output.getvalue())
                self.assertIn(f"({full_rows}/{full_rows} rows)", output.getvalue())

    @patch.object(runner, "_provenance_harness_matches", return_value=True)
    def test_strict_validator_decodes_complete_exact_matrix(self, _match):
        path = self._write(self._artifact())
        result = runner.validate_stage4_artifact(path, 2)
        self.assertEqual(2, len(result["rows"]))

    @patch.object(runner, "_provenance_harness_matches", return_value=True)
    def test_validator_rejects_duplicate_matrix_cell(self, _match):
        doc = self._artifact()
        doc["rows"][1].update(doc["rows"][0])
        with self.assertRaisesRegex(runner.ArtifactValidationError,
                                    "input identity|matrix keys"):
            runner.validate_stage4_artifact(self._write(doc), 2)

    @patch.object(runner, "_provenance_harness_matches", return_value=True)
    def test_validator_recomputes_summary(self, _match):
        doc = self._artifact()
        doc["summary"][0]["failed"] = 99
        with self.assertRaisesRegex(runner.ArtifactValidationError,
                                    "summary does not exactly recompute"):
            runner.validate_stage4_artifact(self._write(doc), 2)

    @patch.object(runner, "_provenance_harness_matches", return_value=True)
    def test_validator_reads_entire_wav_and_rejects_truncation(self, _match):
        doc = self._artifact()
        wav = os.path.join(runner.REPO, doc["rows"][0]["wav"])
        with open(wav, "rb+") as handle:
            handle.truncate(50)
        with self.assertRaisesRegex(runner.ArtifactValidationError,
                                    "not fully decodable"):
            runner.validate_stage4_artifact(self._write(doc), 2)

    @patch.object(runner, "_provenance_harness_matches", return_value=True)
    def test_validator_requires_error_breakdown_identity(self, _match):
        doc = self._artifact()
        doc["rows"][0]["errors"] = 1
        with self.assertRaisesRegex(runner.ArtifactValidationError,
                                    "error breakdown is wrong"):
            runner.validate_stage4_artifact(self._write(doc), 2)

    @patch.object(runner, "_provenance_harness_matches", return_value=True)
    def test_same_validator_covers_category_expansion(self, _match):
        doc = self._artifact()
        doc["provenance"]["script"] = "nonprose_category_expansion.py"
        args = doc["provenance"]["args"]
        args["limit_per_category"] = args.pop("limit")
        doc["selection"]["categories"] = ["urls"]
        pair = doc["selection"]["pairs"][0]
        pair["probe_uid"] = pair.pop("nonprose_uid")
        pair["probe_sha256"] = pair.pop("nonprose_sha256")
        pair["probe_features"] = pair.pop("nonprose_features")
        pair["category"] = "urls"
        for row in doc["rows"]:
            row["category"] = "urls"
            if row["class"] == "nonprose":
                row["class"] = "probe"
        doc["summary"] = category_summarize(doc["rows"])
        matrix = {("adapter", 7, 0, "probe"),
                  ("adapter", 7, 0, "prose")}
        result = runner.validate_stage4_artifact(
            self._write(doc), 2, matrix,
            expected_script="nonprose_category_expansion.py",
            class_prefixes={"probe": "probe", "prose": "prose"},
            extra_arg_fields=("limit_per_category",),
            expected_categories=("urls",),
            summary_function=category_summarize)
        self.assertEqual(2, len(result["rows"]))

    @patch.object(runner, "_provenance_harness_matches", return_value=True)
    def test_category_validator_rejects_wrong_row_category(self, _match):
        doc = self._artifact()
        doc["provenance"]["script"] = "nonprose_category_expansion.py"
        args = doc["provenance"]["args"]
        args["limit_per_category"] = args.pop("limit")
        doc["selection"]["categories"] = ["urls"]
        pair = doc["selection"]["pairs"][0]
        pair["probe_uid"] = pair.pop("nonprose_uid")
        pair["probe_sha256"] = pair.pop("nonprose_sha256")
        pair["probe_features"] = pair.pop("nonprose_features")
        pair["category"] = "urls"
        for row in doc["rows"]:
            row["category"] = "wrong"
            if row["class"] == "nonprose":
                row["class"] = "probe"
        doc["summary"] = category_summarize(doc["rows"])
        with self.assertRaisesRegex(runner.ArtifactValidationError,
                                    "category does not match"):
            runner.validate_stage4_artifact(
                self._write(doc), 2,
                expected_script="nonprose_category_expansion.py",
                class_prefixes={"probe": "probe", "prose": "prose"},
                extra_arg_fields=("limit_per_category",),
                expected_categories=("urls",),
                summary_function=category_summarize)


    @patch.object(runner, "_provenance_harness_matches", return_value=True)
    def test_nested_bad_shapes_are_concrete_validation_errors_without_mutation(self, _match):
        import copy
        cases = (
            ("root-null", lambda d: None), ("root-list", lambda d: []),
            ("args-list", lambda d: d["provenance"].update(args=["adapters"])),
            ("args-number", lambda d: d["provenance"].update(args=7)),
            ("adapters-null", lambda d: d["provenance"]["args"].update(adapters=None)),
            ("adapters-object", lambda d: d["provenance"]["args"].update(adapters={"adapter": 1})),
            ("adapter-nested", lambda d: d["provenance"]["args"].update(adapters=[["adapter"]])),
            ("seeds-null", lambda d: d["provenance"]["args"].update(seeds=None)),
            ("seed-nested", lambda d: d["provenance"]["args"].update(seeds=[[7]])),
            ("selection-list", lambda d: d.update(selection=["pairs"])),
            ("pairs-object", lambda d: d["selection"].update(pairs={"pair": 1})),
            ("pair-number", lambda d: d["selection"].update(pairs=[7])),
            ("pair-list", lambda d: d["selection"].update(pairs=[[]])),
            ("optional-text-number", lambda d: d["selection"]["pairs"][0].update(nonprose_text=7)),
            ("optional-text-object", lambda d: d["selection"]["pairs"][0].update(prose_text={})),
            ("row-list", lambda d: d["rows"].__setitem__(0, [])),
            ("row-null", lambda d: d["rows"].__setitem__(0, None)),
            ("row-adapter-list", lambda d: d["rows"][0].update(adapter=[])),
            ("row-seed-list", lambda d: d["rows"][0].update(seed=[])),
            ("row-pair-list", lambda d: d["rows"][0].update(pair=[])),
            ("row-class-object", lambda d: d["rows"][0].update(**{"class": {}})),
            ("row-count-null", lambda d: d["rows"][0].update(substitutions=None)),
            ("row-count-list", lambda d: d["rows"][0].update(deletions=[])),
            ("row-wav-number", lambda d: d["rows"][0].update(wav=7)),
        )
        template = self._artifact()
        audio_before = {path: Path(path).read_bytes() for path in self.wavs}
        for name, damage in cases:
            with self.subTest(name=name):
                document = copy.deepcopy(template)
                result = damage(document)
                if name.startswith("root-"):
                    document = result
                path = self._write(document)
                original = Path(path).read_bytes()
                with self.assertRaises(runner.ArtifactValidationError) as error:
                    runner.validate_stage4_artifact(path, 2)
                self.assertTrue(str(error.exception))
                self.assertEqual(original, Path(path).read_bytes())
        self.assertEqual(audio_before, {path: Path(path).read_bytes() for path in self.wavs})

    @patch.object(runner, "_provenance_harness_matches", return_value=True)
    def test_valid_optional_text_hashes_and_artifact_remain_unchanged(self, _match):
        from pathlib import Path
        document = self._artifact()
        pair = document["selection"]["pairs"][0]
        for row in document["rows"]:
            prefix = row["class"]
            text = "café 日本語 " + prefix
            pair[prefix + "_text"] = text
            pair[prefix + "_sha256"] = runner._sha256_text(text)
            row["source_sha256"] = pair[prefix + "_sha256"]
        path = self._write(document)
        original = Path(path).read_bytes()
        self.assertEqual(document, runner.validate_stage4_artifact(path, 2))
        self.assertEqual(original, Path(path).read_bytes())


    @patch.object(runner, "_provenance_harness_matches", return_value=True)
    def test_category_expansion_nested_shapes_have_concrete_errors(self, _match):
        import copy
        template = self._artifact()
        template["provenance"]["script"] = "nonprose_category_expansion.py"
        args = template["provenance"]["args"]
        args["limit_per_category"] = args.pop("limit")
        template["selection"]["categories"] = ["urls"]
        pair = template["selection"]["pairs"][0]
        for field in ("uid", "sha256", "features"):
            pair["probe_" + field] = pair.pop("nonprose_" + field)
        pair["category"] = "urls"
        for row in template["rows"]:
            row["category"] = "urls"
            if row["class"] == "nonprose":
                row["class"] = "probe"
        template["summary"] = category_summarize(template["rows"])
        for name in ("limit-null", "categories-object", "category-list", "pair-range"):
            with self.subTest(name=name):
                document = copy.deepcopy(template)
                if name == "limit-null":
                    document["provenance"]["args"]["limit_per_category"] = None
                elif name == "categories-object":
                    document["selection"]["categories"] = {"urls": 1}
                elif name == "category-list":
                    document["selection"]["pairs"][0]["category"] = []
                    for row in document["rows"]:
                        row["category"] = []
                else:
                    document["rows"][0]["pair"] = 99
                path = self._write(document)
                original = Path(path).read_bytes()
                with self.assertRaises(runner.ArtifactValidationError):
                    runner.validate_stage4_artifact(path, 2,
                        expected_script="nonprose_category_expansion.py",
                        class_prefixes={"probe": "probe", "prose": "prose"},
                        extra_arg_fields=("limit_per_category",),
                        expected_categories=("urls",), summary_function=category_summarize)
                self.assertEqual(original, Path(path).read_bytes())


if __name__ == "__main__":
    unittest.main()


class Stage4PilotAdmissionTests(unittest.TestCase):
    setUp = Stage4CheckpointRunnerTest.setUp
    tearDown = Stage4CheckpointRunnerTest.tearDown
    _wav = Stage4CheckpointRunnerTest._wav
    _artifact = Stage4CheckpointRunnerTest._artifact
    _write = Stage4CheckpointRunnerTest._write

    def test_actual_main_rejects_foreign_pilot_arguments_before_any_dispatch(self):
        import contextlib
        import copy
        import io
        class AfterPilot(Exception): pass
        original = runner.validate_stage4_artifact
        template = self._artifact()
        template["provenance"]["args"].update(
            adapters=[runner.DEFAULT_ADAPTERS[0]], seeds=[runner.DEFAULT_SEEDS[0]], limit=2,
            source="ab_test_runtime/experiments/prose_vs_nonprose_v3.json", config="app/config.json",
            out_dir="../ab_test_runtime/nonprose_replication_pilot", out="../ab_test_runtime/experiments/nonprose_replication_pilot.json")
        pair = template["selection"]["pairs"][0]
        template["selection"]["pairs"] = [copy.deepcopy(pair), copy.deepcopy(pair)]
        rows = []
        for index in range(2):
            for row in template["rows"]:
                rows.append(dict(row,adapter=runner.DEFAULT_ADAPTERS[0],seed=runner.DEFAULT_SEEDS[0],pair=index))
        template["rows"] = rows;template["summary"] = summarize(rows)
        changes = [("adapters", ["foreign-adapter"]), ("seeds", [9999]),
                   ("source", "ab_test_runtime/experiments/foreign.json"),
                   ("config", "app/foreign.json"), ("out_dir", "../ab_test_runtime/foreign"),
                   ("out", "../ab_test_runtime/experiments/foreign.json"),
                   ("adapters", [runner.DEFAULT_ADAPTERS[0]] * 2),
                   ("seeds", [runner.DEFAULT_SEEDS[0]] * 2), ("limit", 1),
                   ("source", None), ("out", "")]
        for change in [None, *changes]:
            with self.subTest(change=change):
                doc = copy.deepcopy(template)
                if change:
                    field,value=change;doc["provenance"]["args"][field]=value
                    if field in ("adapters","seeds"):
                        for row in doc["rows"]:row["adapter" if field=="adapters" else "seed"]=value[0]
                        doc["summary"]=summarize(doc["rows"])
                path = self._write(doc);before=Path(path).read_bytes()
                def validate(target,*args,**kwargs):
                    if target==runner.PILOT:return original(path,*args,**kwargs)
                    raise AfterPilot()
                output=io.StringIO()
                with patch.object(runner,"validate_stage4_artifact",side_effect=validate), \
                     patch.object(runner,"_provenance_harness_matches",return_value=True), \
                     patch.object(runner,"run_gpu_experiment") as gpu,patch.object(runner,"run") as run, \
                     patch.object(runner.os.path,"exists",return_value=True),contextlib.redirect_stdout(output):
                    with self.assertRaises(runner.ArtifactValidationError if change else AfterPilot):runner.main()
                gpu.assert_not_called();run.assert_not_called()
                self.assertEqual(before,Path(path).read_bytes())
                self.assertEqual(not bool(change),"pilot validated strictly" in output.getvalue())
