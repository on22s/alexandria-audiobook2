"""Blind-listener harness and clip builder for the #1547 validation."""
import json
import struct
import tempfile
import unittest
import wave
from pathlib import Path

from experiments import build_listening_clips as clips
from experiments import listening_critic_harness as harness

FIXTURE = Path(__file__).resolve().parent.parent / "fixtures" / "alice_pd_listening.json"


def write_wav(path, ms=100, rate=16000):
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(rate)
        handle.writeframes(struct.pack("<h", 0) * int(rate * ms / 1000))


def make_run(root, fixture):
    """Synthetic chunks.json whose texts match the committed fixture."""
    root = Path(root)
    (root / "audio").mkdir(parents=True, exist_ok=True)
    rows = []
    for line in fixture["lines"]:
        name = f"{line['id']}.wav"
        write_wav(root / "audio" / name)
        rows.append({"speaker": line["speaker"], "text": line["text"],
                     "audio_path": f"audio/{name}", "uid": line["id"]})
    for scene in fixture["scenes"]:
        for index, line in enumerate(scene["lines"]):
            name = f"{scene['id']}_{index}.wav"
            write_wav(root / "audio" / name)
            rows.append({"speaker": line["speaker"], "text": line["text"],
                         "audio_path": f"audio/{name}", "uid": f"{scene['id']}_{index}"})
    (root / "chunks.json").write_text(json.dumps(rows), encoding="utf-8")
    (root / "config.json").write_text(json.dumps({"tts": {"pause_between_speakers_ms": 500,
                                                          "pause_same_speaker_ms": 250}}), encoding="utf-8")
    return rows


class FixtureTests(unittest.TestCase):
    def test_committed_fixture_shape(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        clips.validate_fixture(fixture)
        self.assertEqual(30, len(fixture["lines"]))
        self.assertEqual(5, len(fixture["scenes"]))
        self.assertEqual("public-domain", fixture["license"])

    def test_bad_fixtures_are_refused(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with self.assertRaisesRegex(ValueError, "license"):
            clips.validate_fixture({**fixture, "license": "copyrighted"})
        with self.assertRaisesRegex(ValueError, "30 lines and 5 scenes"):
            clips.validate_fixture({**fixture, "lines": fixture["lines"][:10]})
        duplicate = dict(fixture["lines"][0])
        with self.assertRaisesRegex(ValueError, "unique"):
            clips.validate_fixture({**fixture, "lines": fixture["lines"][:-1] + [duplicate]})


class MatchTests(unittest.TestCase):
    def test_unique_and_ambiguous_matches(self):
        rows = [{"speaker": "A", "text": "hello"}, {"speaker": "A", "text": "hello"},
                {"speaker": "B", "text": "bye"}]
        index = clips.index_chunks(rows)
        with self.assertRaisesRegex(ValueError, "found 2"):
            clips.find_unique(index, "A", "hello")
        self.assertEqual(rows[2], clips.find_unique(index, "B", "bye"))
        with self.assertRaisesRegex(ValueError, "found 0"):
            clips.find_unique(index, "A", "missing")

    def test_scene_requires_a_unique_contiguous_match(self):
        rows = [{"speaker": "A", "text": "x"}, {"speaker": "B", "text": "y"},
                {"speaker": "A", "text": "x"}, {"speaker": "B", "text": "y"}]
        scene = {"id": "s", "lines": [{"speaker": "A", "text": "x"}, {"speaker": "B", "text": "y"}]}
        with self.assertRaisesRegex(ValueError, "found 2"):
            clips.find_scene_in_rows(rows, scene)


class BuildTests(unittest.TestCase):
    def test_build_writes_35_clips_and_scene_pauses(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as root:
            make_run(root, fixture)
            manifest = clips.build(FIXTURE, Path(root) / "chunks.json", root,
                                   Path(root) / "clips", Path(root) / "manifest.json")
            self.assertEqual(35, len(manifest["clips"]))
            scene = manifest["clips"]["scene_01"]
            self.assertGreater(scene["duration_ms"], 2 * 100)
            self.assertEqual(500, manifest["pause_ms"])
            for item_id in manifest["clips"]:
                data = (Path(root) / "clips" / f"{item_id}.wav").read_bytes()
                self.assertEqual(b"RIFF", data[:4])


class TrialTests(unittest.TestCase):
    def items(self, root):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        for item in fixture["lines"] + fixture["scenes"]:
            write_wav(Path(root) / f"{item['id']}.wav")
        return harness.load_items(FIXTURE, root)

    def test_38_trials_with_three_spaced_repeats_and_public_page(self):
        with tempfile.TemporaryDirectory() as root:
            items = self.items(root)
            trials = harness.build_trials(items, seed=1547)
            self.assertEqual(38, len(trials))
            self.assertEqual(3, sum(trial["_repeat"] for trial in trials))
            positions = {trial["_item_id"]: index for index, trial in enumerate(trials) if not trial["_repeat"]}
            for index, trial in enumerate(trials):
                if trial["_repeat"]:
                    self.assertGreaterEqual(abs(index - positions[trial["_item_id"]]), harness.MIN_REPEAT_GAP)
            again = harness.build_trials(items, seed=1547)
            self.assertEqual([t["_item_id"] for t in trials], [t["_item_id"] for t in again])
            result = harness.build_page(FIXTURE, root, Path(root) / "page.html", Path(root) / "key.json", 1547)
            html = (Path(root) / "page.html").read_text(encoding="utf-8")
            key = json.loads((Path(root) / "key.json").read_text(encoding="utf-8"))
            self.assertEqual(38, result["trials"])
            self.assertNotIn("line_01", html)
            self.assertNotIn("_repeat", html)
            self.assertEqual(38, len(key["trials"]))
            self.assertEqual(3, len(key["repeats"]))
            self.assertIn("data:audio/wav;base64,", html)
            self.assertIn("Clearly consistent", html)


class AnalyzeTests(unittest.TestCase):
    def responses_for(self, key, fixture):
        dims = {"line": ["naturalness", "instruct_fit", "content_correct"],
                "scene": ["voice_consistency", "pause_fit", "emotion_fit"]}
        lines = {item["id"]: item for item in fixture["lines"]}
        answers = {}
        for entry in key["trials"]:
            item_id = entry["item_id"]
            keys = dims["line" if item_id.startswith("line_") else "scene"]
            if item_id in lines and not lines[item_id].get("instruct"):
                keys = [key for key in keys if key != "instruct_fit"]
            answers[entry["trial_id"]] = {"ratings": {key: 3 for key in keys}, "notes": ""}
        return {"version": 1, "seed": key["seed"], "responses": answers}

    def test_repeat_consistency_and_validation(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        with tempfile.TemporaryDirectory() as root:
            root = Path(root)
            for item in fixture["lines"] + fixture["scenes"]:
                write_wav(root / f"{item['id']}.wav")
            harness.build_page(FIXTURE, root, root / "page.html", root / "key.json", 1547)
            key = json.loads((root / "key.json").read_text(encoding="utf-8"))
            payload = self.responses_for(key, fixture)
            item_id = harness.REPEAT_ITEM_IDS[2]
            trial_ids = key["repeats"][item_id]
            dim = "naturalness"
            payload["responses"][trial_ids[1]]["ratings"][dim] = 4
            path = root / "responses.json"
            path.write_text(json.dumps(payload), encoding="utf-8")
            summary = harness.analyze_responses(path, root / "key.json", FIXTURE)
            repeat = summary["repeats"][item_id][dim]
            self.assertEqual(False, repeat["exact"])
            self.assertEqual(True, repeat["within_one"])
            self.assertEqual(1, repeat["mean_abs_diff"])
            del payload["responses"][key["trials"][0]["trial_id"]]
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "missing"):
                harness.analyze_responses(path, root / "key.json", FIXTURE)
            payload = self.responses_for(key, fixture)
            payload["seed"] = 1
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "different study seed"):
                harness.analyze_responses(path, root / "key.json", FIXTURE)
            payload = self.responses_for(key, fixture)
            first = key["trials"][0]["trial_id"]
            payload["responses"][first]["ratings"]["naturalness"] = 6
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "naturalness=6"):
                harness.analyze_responses(path, root / "key.json", FIXTURE)


if __name__ == "__main__":
    unittest.main()
