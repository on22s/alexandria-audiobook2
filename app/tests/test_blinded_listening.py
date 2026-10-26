import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

from experiments import blinded_listening as blind
from experiments.provenance import file_sha256, input_sha256


def write_wav(path, frames=320, sample=b"\x00\x00"):
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        handle.writeframes(sample * frames)


def provenance(script):
    return {"script": script, "git": {"harness_sha256": "a" * 64}}


class BlindedListeningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=blind.REPO)
        self.root = Path(self.temp.name)
        self.audio = self.root / "source"
        self.audio.mkdir()

    def tearDown(self):
        self.temp.cleanup()

    def source(self, name, sample=None):
        path = self.audio / f"{name}.wav"
        # Distinct bytes per file: a fixture whose arms were all the same
        # silence could never show the hollow-arm refusal failing.
        write_wav(path, sample=sample or hashlib.sha256(name.encode()).digest()[:2])
        return os.path.relpath(path, blind.REPO)

    def documents(self):
        comparisons = []
        for line in range(4):
            comparisons.append({
                "arm_files": {arm: self.source(f"line{line}_{arm}")
                              for arm in ("none", "per_char", "per_line")}})
        instruction = {
            "status": "complete", "all_arms_rendered": True,
            "provenance": provenance("instruct_listening.py"),
            "comparisons": comparisons}
        casting = {
            "status": "complete", "published": True, "lines": 2,
            "provenance": provenance("casting_ab_audio.py"),
            "arms": {arm: {"path": self.source(f"cast_{arm}"),
                            "lines": [0, 1]}
                     for arm in ("current", "scene_aware")}}
        rows = []
        for row in range(3):
            controls = {}
            for arm in ("very_slow", "very_fast"):
                relative = self.source(f"control{row}_{arm}")
                controls[arm] = {
                    "file": relative,
                    "sha256": file_sha256(os.path.join(blind.REPO, relative))}
            rows.append({"duration_order_control_passes": True,
                         "instruction_controls": controls})
        controls = {"provenance": provenance("seed_instruction_controls.py"),
                    "rows": rows}
        return instruction, casting, controls

    def write_documents(self):
        paths = []
        for name, document in zip(
                ("instruction.json", "casting.json", "controls.json"),
                self.documents()):
            path = self.root / name
            path.write_text(json.dumps(document), encoding="utf-8")
            paths.append(str(path))
        return paths

    def build(self, suffix=""):
        instruction, casting, controls = self.write_documents()
        package = str(self.root / f"package{suffix}")
        public_path = str(self.root / f"public{suffix}.json")
        key_path = str(self.root / f"key{suffix}.json")
        public, key = blind.build_package(
            instruction, casting, controls, package, public_path, key_path, 42)
        return public, key, package, public_path, key_path

    def test_builds_eight_unlabeled_sets_and_strictly_revalidates(self):
        public, key, package, public_path, key_path = self.build()
        checked_public, checked_key = blind.validate_package(
            public_path, key_path, package)

        self.assertEqual(8, len(public["sets"]))
        self.assertEqual(public, checked_public)
        self.assertEqual(key, checked_key)
        serialized = json.dumps(public)
        for label in ("very_slow", "very_fast", "per_char", "per_line",
                      '"current"', '"scene_aware"'):
            self.assertNotIn(label, serialized)
        self.assertEqual(
            {sample["file"] for item in public["sets"] for sample in item["samples"]},
            set(os.listdir(package)))

    def test_dirty_git_arm_paths_stay_in_concealed_key_with_public_hashes(self):
        original = blind.provenance
        captured = []
        def get_fixture_provenance(*args, **kwargs):
            document = original(*args, **kwargs)
            document['git'].update(branch='fix/per_char', dirty=True,
                modified_tracked_files=['very_fast.wav'],
                untracked_harness_files=['very_slow.wav', 'per_line.wav'])
            captured.append(document)
            return document
        with patch.object(blind, 'provenance', side_effect=get_fixture_provenance):
            public, key, package, public_path, key_path = self.build()
        self.assertEqual(captured[0], key['provenance'])
        for name in ('commit', 'dirty', 'source_state', 'harness_sha256'):
            self.assertEqual(captured[0]['git'][name], public['provenance']['git'][name])
        self.assertEqual({'commit', 'dirty', 'source_state', 'harness_sha256'},
                         set(public['provenance']['git']))
        self.assertEqual(captured[0]['source_artifacts'], public['provenance']['source_artifacts'])
        self.assertEqual((public, key), blind.validate_package(public_path, key_path, package))
        public['provenance']['git']['branch'] = 'fix/per_char'
        Path(public_path).write_text(json.dumps(public))
        with self.assertRaisesRegex(blind.ListeningPackageError, 'leaks arm labels'):
            blind.validate_package(public_path, key_path, package)

    def test_malformed_package_shapes_raise_concrete_errors_without_writes(self):
        import copy
        public, key, package, public_path, key_path = self.build()
        cases = [
            ("public", lambda d: [], "root must"),
            ("key", lambda d: [], "root must"),
            ("public", lambda d: dict(d, provenance=[]), "no provenance"),
            ("public", lambda d: dict(d, provenance=dict(d["provenance"], git=[])), "provenance.git"),
            ("public", lambda d: dict(d, package_dir=[]), "package_dir"),
            ("public", lambda d: dict(d, provenance=dict(d["provenance"], source_artifacts=[1])), "source identities"),
            ("public", lambda d: dict(d, provenance=dict(d["provenance"], source_artifacts={"x": []})), "hashes must"),
            ("public", lambda d: dict(d, sets={}), "must contain 8 sets"),
            ("key", lambda d: dict(d, sets={}), "must contain 8 sets"),
        ]
        for side in ("public", "key"):
            cases.extend([
                (side, lambda d: dict(d, sets=[[]] + d["sets"][1:]), "set 0 must"),
                (side, lambda d: dict(d, sets=[dict(d["sets"][0], id=[])] + d["sets"][1:]), "id must"),
            ])
        cases.extend([
            ("public", lambda d: dict(d, sets=[dict(d["sets"][0], samples={})] + d["sets"][1:]), "samples"),
            ("public", lambda d: dict(d, sets=[dict(d["sets"][0], samples=[[]])] + d["sets"][1:]), "sample in"),
            ("public", lambda d: dict(d, sets=[dict(d["sets"][0], samples=[dict(d["sets"][0]["samples"][0], file=[])])] + d["sets"][1:]), "file and sha256"),
            ("public", lambda d: dict(d, sets=[dict(d["sets"][0], samples=[dict(d["sets"][0]["samples"][0], sha256=[])])] + d["sets"][1:]), "file and sha256"),
            ("key", lambda d: dict(d, sets=[dict(d["sets"][0], mapping=[])] + d["sets"][1:]), "mapping in"),
            ("key", lambda d: dict(d, sets=[dict(d["sets"][0], mapping={next(iter(d["sets"][0]["mapping"])): []})] + d["sets"][1:]), "mapping entries"),
            ("key", lambda d: dict(d, sets=[dict(d["sets"][0], mapping={next(iter(d["sets"][0]["mapping"])): {"source_sha256": []}})] + d["sets"][1:]), "source_sha256"),
        ])
        for side, mutate, message in cases:
            with self.subTest(side=side, message=message):
                p, k = copy.deepcopy(public), copy.deepcopy(key)
                if side == "key":
                    k = mutate(k)
                else:
                    p = mutate(p)
                Path(key_path).write_text(json.dumps(k), encoding="utf-8")
                if isinstance(p, dict):
                    p["concealed_key_sha256"] = file_sha256(key_path)
                Path(public_path).write_text(json.dumps(p), encoding="utf-8")
                before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
                with self.assertRaisesRegex(blind.ListeningPackageError, message):
                    blind.validate_package(public_path, key_path, package)
                self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_same_seed_produces_the_same_concealed_mapping(self):
        first_public, first_key, _, _, _ = self.build("_one")
        self.assertNotIn("randomization_seed", first_public["provenance"])
        self.assertEqual(42, first_key["randomization_seed"])
        _, second_key, _, _, _ = self.build("_two")
        self.assertEqual({k: v for k, v in first_key.items() if k != 'provenance'},
                         {k: v for k, v in second_key.items() if k != 'provenance'})
        self.assertEqual(first_key['provenance']['source_artifacts'],
                         second_key['provenance']['source_artifacts'])

    def test_validation_rejects_changed_audio_and_extra_files(self):
        _, _, package, public_path, key_path = self.build()
        public = json.loads(Path(public_path).read_text(encoding="utf-8"))
        first = Path(package, public["sets"][0]["samples"][0]["file"])
        first.write_bytes(first.read_bytes()[:-1])
        with self.assertRaises(blind.ListeningPackageError):
            blind.validate_package(public_path, key_path, package)

        write_wav(first)
        public["sets"][0]["samples"][0]["sha256"] = file_sha256(first)
        mapped = json.loads(Path(key_path).read_text(encoding="utf-8"))
        mapped["sets"][0]["mapping"][first.name]["source_sha256"] = file_sha256(first)
        Path(key_path).write_text(json.dumps(mapped), encoding="utf-8")
        public["concealed_key_sha256"] = file_sha256(key_path)
        Path(public_path).write_text(json.dumps(public), encoding="utf-8")
        write_wav(Path(package, "unexpected.wav"))
        with self.assertRaisesRegex(blind.ListeningPackageError,
                                    "inventory changed"):
            blind.validate_package(public_path, key_path, package)

    def test_validation_rejects_changed_source_manifest(self):
        _, _, package, public_path, key_path = self.build()
        source = self.root / "instruction.json"
        source.write_text(source.read_text(encoding="utf-8") + "\n",
                          encoding="utf-8")
        with self.assertRaisesRegex(blind.ListeningPackageError,
                                    "source artifact changed"):
            blind.validate_package(public_path, key_path, package)

    def test_rejects_instruction_arms_that_rendered_the_same_bytes(self):
        instruction, casting, controls = self.documents()
        same = self.source("line0_none_again", sample=b"\x07\x00")
        instruction["comparisons"][0]["arm_files"]["none"] = same
        instruction["comparisons"][0]["arm_files"]["per_char"] = self.source(
            "line0_per_char_again", sample=b"\x07\x00")
        with self.assertRaisesRegex(blind.ListeningPackageError,
                                    r"identical renders for \['none', 'per_char'\]"):
            blind._source_groups(instruction, casting, controls)

    def test_rejects_asymmetric_casting_arms(self):
        instruction, casting, controls = self.documents()
        casting["arms"]["scene_aware"]["lines"] = [0]
        with self.assertRaisesRegex(blind.ListeningPackageError,
                                    "identical lines"):
            blind._source_groups(instruction, casting, controls)

    def test_rejects_truncated_wav_during_full_decode(self):
        path = self.root / "truncated.wav"
        write_wav(path)
        path.write_bytes(path.read_bytes()[:-1])
        with self.assertRaises(blind.ListeningPackageError):
            blind._validate_wav(str(path))

    def test_rejects_source_without_provenance(self):
        path = self.root / "source.json"
        path.write_text('{"status":"complete"}', encoding="utf-8")
        with self.assertRaisesRegex(blind.ListeningPackageError, "no provenance"):
            blind._load_document(str(path))

    def test_failed_manifest_write_removes_all_new_outputs(self):
        instruction, casting, controls = self.write_documents()
        package = str(self.root / "package")
        public_path = str(self.root / "public.json")
        key_path = str(self.root / "key.json")
        from utils import atomic_json_write as real_write
        calls = 0

        def fail_second(document, path):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("injected public-manifest failure")
            return real_write(document, path)

        with patch("utils.atomic_json_write", side_effect=fail_second):
            with self.assertRaisesRegex(OSError, "injected"):
                blind.build_package(instruction, casting, controls, package,
                                    public_path, key_path, 42)
        self.assertFalse(os.path.exists(package))
        self.assertFalse(os.path.exists(public_path))
        self.assertFalse(os.path.exists(key_path))

    def test_input_hashes_fail_loudly_for_missing_files(self):
        with self.assertRaises(FileNotFoundError):
            input_sha256((str(self.root / "missing"),))

    def _responses(self, public, listeners=2):
        rows = []
        for listener_index in range(listeners):
            sets = []
            for item in public["sets"]:
                ratings = {
                    sample["file"]: {field: 3 + (sample_index % 2)
                                     for field in blind.SAMPLE_RATING_FIELDS}
                    for sample_index, sample in enumerate(item["samples"])}
                sets.append({"id": item["id"], "ratings": ratings,
                             "preference": item["samples"][0]["file"]})
            rows.append({"id": f"listener-{listener_index}", "sets": sets})
        return {"listeners": rows}

    def test_analyzes_only_complete_explicit_listener_count(self):
        public, _, package, public_path, key_path = self.build()
        responses_path = self.root / "responses.json"
        responses_path.write_text(json.dumps(self._responses(public)), encoding="utf-8")

        result = blind.analyze_responses(
            public_path, key_path, package, str(responses_path), 2)

        self.assertEqual("complete", result["status"])
        self.assertEqual(2, result["listener_count"])
        self.assertEqual(40, result["response_count"])
        self.assertIn("no production threshold", result["limitations"][0])

    def test_response_analysis_rejects_missing_listener_and_rating(self):
        public, _, package, public_path, key_path = self.build()
        responses = self._responses(public, listeners=1)
        responses_path = self.root / "responses.json"
        responses_path.write_text(json.dumps(responses), encoding="utf-8")
        with self.assertRaisesRegex(blind.ListeningPackageError, "expected 2"):
            blind.analyze_responses(
                public_path, key_path, package, str(responses_path), 2)

        import copy
        for mutate in (lambda d: d.update(listeners=[[]]),
                       lambda d: d["listeners"][0].update(sets=["bad"]),
                       lambda d: d["listeners"][0]["sets"][0].update(preference=[])):
            invalid = copy.deepcopy(responses)
            mutate(invalid)
            responses_path.write_text(json.dumps(invalid), encoding="utf-8")
            with self.assertRaises(blind.ListeningPackageError):
                blind.analyze_responses(public_path, key_path, package, str(responses_path), 1)

        first = responses["listeners"][0]["sets"][0]["ratings"]
        first.pop(next(iter(first)))
        responses_path.write_text(json.dumps(responses), encoding="utf-8")
        with self.assertRaisesRegex(blind.ListeningPackageError, "incomplete ratings"):
            blind.analyze_responses(
                public_path, key_path, package, str(responses_path), 1)

    def scene_document(self, scenes=2, **overrides):
        comparisons = [{"arm_files": {arm: self.source(f"scene{n}_{arm}")
                                      for arm in blind.SCENE_ARMS},
                        "content_regressions": [],
                        "wer": {arm: 0.0 for arm in blind.SCENE_ARMS}}
                       for n in range(scenes)]
        for comparison in comparisons:
            comparison["arm_sha256"] = {arm: blind.file_sha256(os.path.join(blind.REPO, path))
                                        for arm, path in comparison["arm_files"].items()}
        document = {"status": "complete", "published": True,
                    "provenance": provenance("scene_context_listening.py"),
                    "comparisons": comparisons}
        document.update(overrides)
        return document

    def build_scene(self, document):
        scene_path = self.root / "scene.json"
        scene_path.write_text(json.dumps(document), encoding="utf-8")
        controls_path = self.root / "controls.json"
        controls_path.write_text(json.dumps(self.documents()[2]), encoding="utf-8")
        package = str(self.root / "scene_package")
        public_path = str(self.root / "scene_public.json")
        key_path = str(self.root / "scene_key.json")
        public, key = blind.build_scene_package(
            str(scene_path), str(controls_path), package, public_path, key_path, 42)
        return public, key, package, public_path, key_path, str(scene_path), str(controls_path)

    def test_scene_package_keeps_the_controls_and_hides_scene_arm_labels(self):
        public, key, package, public_path, key_path, scene_path, controls_path = \
            self.build_scene(self.scene_document(scenes=2))
        # Two scene sets plus the three positive controls, counted from the
        # sources rather than assumed.
        self.assertEqual(5, blind.get_scene_set_count(scene_path, controls_path))
        self.assertEqual(["scene_context"] * 2 + ["positive_control"] * 3,
                         [item["kind"] for item in public["sets"]])
        self.assertEqual((public, key), blind.validate_package(
            public_path, key_path, package, expected_sets=5))
        serialized = json.dumps(public)
        for label in ('"line"', '"scene"', "wrong_scene"):
            self.assertNotIn(label, serialized)
        arms = {entry["arm"] for item in key["sets"][:2]
                for entry in item["mapping"].values()}
        self.assertEqual(set(blind.SCENE_ARMS), arms)

    def test_scene_package_is_held_to_its_own_set_count(self):
        _, _, package, public_path, key_path, _, _ = \
            self.build_scene(self.scene_document(scenes=2))
        # The legacy default is eight; a five-set scene package must not pass
        # it, and the right count must not be satisfied by a smaller package.
        with self.assertRaisesRegex(blind.ListeningPackageError, "must contain 8 sets"):
            blind.validate_package(public_path, key_path, package)
        with self.assertRaisesRegex(blind.ListeningPackageError, "must contain 6 sets"):
            blind.validate_package(public_path, key_path, package, expected_sets=6)

        # Set counts for analysis come from the bound source JSON inventory,
        # while copied package audio remains usable after source WAV cleanup.
        for source in self.audio.iterdir():
            source.unlink()
        source_paths = json.loads(Path(public_path).read_text())["provenance"]["source_artifacts"]
        scene_path = next(str(Path(blind.REPO, path)) for path in source_paths if path.endswith("scene.json"))
        control_path = next(str(Path(blind.REPO, path)) for path in source_paths if path.endswith("controls.json"))
        self.assertEqual(5, blind.get_scene_set_count(scene_path, control_path))
        blind.validate_package(public_path, key_path, package, expected_sets=5)

    def test_scene_source_refusals(self):
        for public_name, key_name in (("same.json", "same.json"),
                                      ("package/public.json", "key.json"),
                                      ("public.json", "package/key.json")):
            with self.subTest(public=public_name, key=key_name):
                with self.assertRaises(blind.ListeningPackageError):
                    blind._validate_output_paths(str(self.root / "package"),
                        str(self.root / public_name), str(self.root / key_name))
                self.assertFalse((self.root / "package").exists())
        cases = [
            (dict(status="partial"), "incomplete"),
            (dict(published=False), "incomplete"),
            (dict(comparisons=[]), "no comparisons"),
        ]
        wrong_arms = self.scene_document(scenes=1)
        wrong_arms["comparisons"][0]["arm_files"].pop("wrong_scene")
        regressed = self.scene_document(scenes=1)
        regressed["comparisons"][0]["content_regressions"] = ["scene"]
        hollow = self.scene_document(scenes=1)
        files = hollow["comparisons"][0]["arm_files"]
        files["scene"] = files["line"]
        hollow["comparisons"][0]["arm_sha256"]["scene"] = hollow["comparisons"][0]["arm_sha256"]["line"]
        # Cached flags cannot turn unavailable or failed raw scores into pass.
        for bad in (None, 1.0, float("nan"), -1, "0"):
            doc = self.scene_document(scenes=1)
            doc["comparisons"][0]["wer"]["scene"] = bad
            with self.subTest(wer=bad), self.assertRaisesRegex(blind.ListeningPackageError, "WER gate"):
                self.build_scene(doc)
        changed = self.scene_document(scenes=1)
        changed["comparisons"][0]["arm_sha256"]["line"] = "0" * 64
        with self.assertRaisesRegex(blind.ListeningPackageError, "hash"):
            self.build_scene(changed)
        for index, (overrides, message) in enumerate(cases):
            with self.subTest(message=message, index=index):
                with self.assertRaisesRegex(blind.ListeningPackageError, message):
                    self.build_scene(self.scene_document(scenes=1, **overrides))
        for document, message in ((wrong_arms, "wrong arms"),
                                  (regressed, "WER gate"),
                                  (hollow, "identical renders")):
            with self.subTest(message=message):
                with self.assertRaisesRegex(blind.ListeningPackageError, message):
                    self.build_scene(document)
        self.assertFalse((self.root / "scene_package").exists())

    def test_scene_source_from_another_script_is_refused(self):
        document = self.scene_document(
            scenes=1, provenance=provenance("instruct_listening.py"))
        with self.assertRaisesRegex(blind.ListeningPackageError, "expected"):
            self.build_scene(document)


if __name__ == "__main__":
    unittest.main()
