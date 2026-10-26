"""The scene-context arms must be the comparison the readings assume."""
import copy
import json
import os
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from experiments import scene_context_listening as scl


def read_fixture():
    with open(scl.FIXTURE, encoding="utf-8") as handle:
        return json.load(handle)


class FixtureTests(unittest.TestCase):
    def test_every_line_is_the_pdnc_text_it_names(self):
        fixture = read_fixture()
        scenes = scl.load_scenes()
        self.assertEqual(6, len(scenes))
        for raw, scene in zip(fixture["scenes"], scenes):
            path = os.path.join(scl.REPO, fixture["source_fixture"].format(book=raw["book"]))
            with open(path, encoding="utf-8") as handle:
                rows = {row["id"]: row for row in json.load(handle)["entries"]}
            for turn in scene["turns"]:
                row = rows[turn["id"]]
                self.assertEqual(row["expected_speaker"], turn["speaker"])
                self.assertEqual(scl.get_spoken_text(row["line"]), turn["text"])
                self.assertNotIn("_", turn["text"])
            self.assertEqual(2, len({turn["speaker"] for turn in scene["turns"]}))

    def test_italics_markup_and_wrapping_are_not_spoken(self):
        self.assertEqual("he _may_ fall".replace("_", ""),
                         scl.get_spoken_text("he _may_\nfall"))

    def test_broken_fixtures_are_refused(self):
        def turn0(fixture):
            return fixture["scenes"][0]["turns"][0]
        cases = [
            (lambda f: f.update(source_fixture="{book:>10000}.json"), "literal"),
            (lambda f: f.update(scenes=f["scenes"][:2]), "different book"),
            (lambda f: f["scenes"][0].update(id="../outside"), "scene id"),
            (lambda f: f["scenes"][0].update(id="/tmp/outside"), "scene id"),
            (lambda f: f["scenes"][0].update(id="a\\outside"), "scene id"),
            (lambda f: f["scenes"][0].update(id=["bad"]), "scene id"),
            (lambda f: f.update(source_fixture="../outside_{book}.json"), "inside the repository"),
            (lambda f: turn0(f).update(id="PrideAndPrejudice-99999"), "no PDNC row"),
            (lambda f: turn0(f).update(instruct="  "), "no instruct"),
            (lambda f: f["scenes"][0].update(scene=""), "no scene description"),
            (lambda f: f["scenes"][0].update(voices={"MRS. BENNET": "female",
                                                    "MR. BENNET": "female"}),
             "one female and one male"),
            (lambda f: f["scenes"][0]["voices"].pop("MR. BENNET"), "one female and one male"),
            (lambda f: f["scenes"][1].update(id=f["scenes"][0]["id"]), "duplicate scene id"),
            (lambda f: f.update(scenes=f["scenes"][:1]), "at least two scenes"),
            (lambda f: f["scenes"][0].update(turns=f["scenes"][0]["turns"][:1]),
             "two-speaker exchange"),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fixture.json")
            for mutate, message in cases:
                with self.subTest(message=message):
                    fixture = copy.deepcopy(read_fixture())
                    mutate(fixture)
                    with open(path, "w", encoding="utf-8") as handle:
                        json.dump(fixture, handle)
                    with self.assertRaisesRegex(scl.SceneFixtureError, message):
                        scl.load_scenes(path)

    def test_an_uncast_speaker_is_refused(self):
        fixture = copy.deepcopy(read_fixture())
        fixture["scenes"][0]["voices"] = {"MRS. BENNET": "female", "MR. COLLINS": "male"}
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "fixture.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(fixture, handle)
            with self.assertRaisesRegex(scl.SceneFixtureError, "is not cast"):
                scl.load_scenes(path)


class WrongSceneTests(unittest.TestCase):
    def test_no_scene_ever_borrows_its_own_description(self):
        for count in range(2, 10):
            ids = [f"s{n}" for n in range(count)]
            mapping = scl.get_wrong_scene_map(ids)
            with self.subTest(count=count):
                self.assertTrue(all(mapping[i] != i for i in ids))
                self.assertEqual(sorted(ids), sorted(mapping.values()))
                self.assertEqual(mapping, scl.get_wrong_scene_map(list(ids)))

    def test_the_fixture_borrows_from_a_different_book(self):
        # The docstring promises a fluent but wrong description. A scene from
        # the same novel could share characters and mood, and weaken the control.
        scenes = scl.load_scenes()
        books = {scene["id"]: scene["book"] for scene in scenes}
        mapping = scl.get_wrong_scene_map([scene["id"] for scene in scenes])
        for own, borrowed in mapping.items():
            self.assertNotEqual(books[own], books[borrowed], own)


class ArmInstructionTests(unittest.TestCase):
    def test_each_arm_sends_exactly_its_stated_instruction(self):
        scenes = scl.load_scenes()
        arms = scl.get_arm_instructions(scenes)
        described = {scene["id"]: scene["scene"] for scene in scenes}
        mapping = scl.get_wrong_scene_map([scene["id"] for scene in scenes])
        for scene in scenes:
            own = arms[scene["id"]]
            self.assertEqual(set(scl.ARMS), set(own))
            for index, turn in enumerate(scene["turns"]):
                self.assertEqual(turn["instruct"], own["line"][index])
                self.assertEqual(f"{scene['scene']} {turn['instruct']}",
                                 own["scene"][index])
                self.assertEqual(
                    f"{described[mapping[scene['id']]]} {turn['instruct']}",
                    own["wrong_scene"][index])
                self.assertEqual(3, len({own[arm][index] for arm in scl.ARMS}))


class ContentGateTests(unittest.TestCase):
    def test_only_arms_worse_than_line_by_the_margin_are_flagged(self):
        self.assertEqual([], scl.get_content_regressions(
            {"line": 0.02, "scene": 0.07, "wrong_scene": 0.0}))
        self.assertEqual(["scene"], scl.get_content_regressions(
            {"line": 0.02, "scene": 0.0701, "wrong_scene": 0.0}))

    def test_an_unscored_arm_is_never_a_pass(self):
        for bad in (float("nan"), float("inf"), -0.01, True, "0"):
            with self.subTest(bad=bad):
                self.assertTrue(scl.get_content_regressions(
                    {"line": 0.0, "scene": bad, "wrong_scene": 0.0}))
        self.assertEqual(["wrong_scene"], scl.get_content_regressions(
            {"line": 0.0, "scene": 0.0, "wrong_scene": None}))
        self.assertEqual(["line", "scene", "wrong_scene"], scl.get_content_regressions(
            {"line": None, "scene": 0.0, "wrong_scene": 0.0}))


class VoiceTests(unittest.TestCase):
    CONFIG = {
        "F": {"type": "custom", "voice": "Serena", "character_style": "Warm.",
              "default_style": "Calm."},
        "M": {"type": "lora", "adapter_path": "x", "character_style": "Dry."},
        "C": {"type": "clone", "ref_audio": "a.wav"},
    }

    def test_styles_are_cleared_where_both_render_paths_look(self):
        entries, config = scl.get_arm_voices(
            self.CONFIG, {"female": "F", "male": "M"}, 99)
        for role, name in (("female", "F"), ("male", "M")):
            for entry in (entries[role], config[name]):
                self.assertNotIn("character_style", entry)
                self.assertNotIn("default_style", entry)
                self.assertEqual("99", entry["seed"])
        self.assertIn("character_style", self.CONFIG["F"])  # input untouched

    def test_clone_and_missing_voices_are_refused(self):
        for seed in (-1, 2**31, True):
            with self.subTest(seed=seed), self.assertRaisesRegex(scl.SceneFixtureError, "seed"):
                scl.get_arm_voices(self.CONFIG, {"female": "F", "male": "M"}, seed)
        with self.assertRaisesRegex(scl.SceneFixtureError, "distinct voices"):
            scl.get_arm_voices(self.CONFIG, {"female": "F", "male": "F"}, 1)
        with self.assertRaisesRegex(scl.SceneFixtureError, "ignore instruct"):
            scl.get_arm_voices(self.CONFIG, {"female": "C", "male": "M"}, 1)
        with self.assertRaisesRegex(scl.SceneFixtureError, "not in the voice config"):
            scl.get_arm_voices(self.CONFIG, {"female": "F", "male": "X"}, 1)


class MainLifecycleTests(unittest.TestCase):
    def test_fresh_outputs_and_lease_cover_failure_and_cleanup(self):
        import sys
        from experiments import gpu_guard
        with tempfile.TemporaryDirectory(dir=scl.REPO) as tmp:
            root = Path(tmp)
            voices = root / "voices.json"
            voices.write_text(json.dumps(VoiceTests.CONFIG))
            config = root / "config.json"
            config.write_text("{}")
            out = root / "manifest.json"
            audio = root / "audio"
            argv = ["scene_context_listening", "--voice-config", str(voices),
                    "--config", str(config), "--female-voice", "F", "--male-voice", "M",
                    "--out-dir", str(audio), "--out", str(out)]
            sentinel = root / "outside_line_t0.wav"
            sentinel.write_bytes(b"outside recording")
            events = []
            lease = object()
            class Engine:
                def ensure_idle_local_models_released(self):
                    events.append("cleanup")
                    return True
            def fail(*args):
                args[-1](Engine())
                events.append("render")
                raise RuntimeError("injected render interruption")
            with patch.object(sys, "argv", argv), \
                    patch.object(gpu_guard, "acquire_gpu_lock", side_effect=lambda: events.append("lease") or lease), \
                    patch.object(gpu_guard, "release_gpu_lock", side_effect=lambda value: events.append("release") if value is lease else self.fail("wrong lease")), \
                    patch.object(scl, "_render_scenes", side_effect=fail) as render:
                with self.assertRaisesRegex(RuntimeError, "interruption"):
                    scl.main()
                self.assertEqual(["lease", "render", "cleanup", "release"], events)
                self.assertEqual({"status": "incomplete", "published": False}, json.loads(out.read_text()))
                # A rerun cannot overwrite either partial or completed output.
                out.write_text('{"status":"complete","published":true}')
                before = out.read_bytes()
                with self.assertRaises(SystemExit):
                    scl.main()
                self.assertEqual(before, out.read_bytes())
                self.assertEqual(1, render.call_count)
                out.unlink()
                with self.assertRaises(FileExistsError):
                    scl.main()
                self.assertFalse(out.exists())
                self.assertEqual(1, render.call_count)
            self.assertEqual(b"outside recording", sentinel.read_bytes())
            # A manifest may not overwrite a rendered WAV, even through an alias.
            shutil = __import__("shutil")
            shutil.rmtree(audio)
            collision = audio / "pp_bennets_news_line.wav"
            for manifest_path in [collision]:
                bad_argv = argv[:]
                bad_argv[-1] = str(manifest_path)
                with patch.object(sys, "argv", bad_argv), \
                        patch.object(scl, "_render_scenes") as render:
                    with self.assertRaises(SystemExit):
                        scl.main()
                    render.assert_not_called()
                    self.assertFalse(audio.exists())
            # Contention refuses inference while preserving an incomplete run.
            import shutil
            if audio.exists():
                shutil.rmtree(audio)
            out.unlink(missing_ok=True)
            with patch.object(sys, "argv", argv), \
                    patch.object(gpu_guard, "acquire_gpu_lock", side_effect=RuntimeError("busy")), \
                    patch.object(scl, "_render_scenes") as render:
                with self.assertRaisesRegex(RuntimeError, "busy"):
                    scl.main()
                render.assert_not_called()
                self.assertFalse(json.loads(out.read_text())["published"])

            # Cleanup refusal cannot leave a completed/published receipt.
            shutil.rmtree(audio)
            out.unlink()
            class Unreleased:
                def ensure_idle_local_models_released(self):
                    return False
            def prepared(*args):
                args[-1](Unreleased())
                return {"status": "complete", "published": True}
            with patch.object(sys, "argv", argv), \
                    patch.object(gpu_guard, "acquire_gpu_lock", return_value=object()), \
                    patch.object(gpu_guard, "release_gpu_lock") as release, \
                    patch.object(scl, "_render_scenes", side_effect=prepared), \
                    patch.object(scl.os, "_exit", side_effect=SystemExit(70)):
                with self.assertRaises(SystemExit) as exit_result:
                    scl.main()
                self.assertEqual(70, exit_result.exception.code)
                release.assert_not_called()
                self.assertEqual({"status": "incomplete", "published": False}, json.loads(out.read_text()))

    def test_real_render_join_and_publication_bind_the_admitted_inputs(self):
        import hashlib
        import sys
        import wave
        import tts
        from experiments import asr_backends, gpu_guard
        with tempfile.TemporaryDirectory(dir=scl.REPO) as tmp:
            root = Path(tmp)
            fixture = read_fixture()
            fixture["scenes"] = [fixture["scenes"][0], fixture["scenes"][3]]
            fixture_path = root / "fixture.json"
            fixture_path.write_text(json.dumps(fixture))
            voices = root / "voices.json"
            voices.write_text(json.dumps({"F": {"type": "custom"}, "M": {"type": "custom"}}))
            config = root / "config.json"
            loaded = []
            class Engine:
                def __init__(self, settings):
                    loaded.append(settings)
                def ensure_idle_local_models_released(self):
                    return True
                def generate_custom_voice(self, text, instruct, speaker, voice_config, path):
                    sample = hashlib.sha256(instruct.encode()).digest()[:2]
                    with wave.open(path, "wb") as handle:
                        handle.setnchannels(1)
                        handle.setsampwidth(2)
                        handle.setframerate(16000)
                        handle.writeframes(sample * 160)
                    return True
            for variant in ("success", "changed_input"):
                config.write_text('{"setting":"admitted"}')
                admitted = hashlib.sha256(config.read_bytes()).hexdigest()
                audio, manifest = root / variant, root / (variant + ".json")
                argv = ["scene_context_listening", "--fixture", str(fixture_path),
                        "--voice-config", str(voices), "--config", str(config),
                        "--female-voice", "F", "--male-voice", "M",
                        "--out-dir", str(audio), "--out", str(manifest)]
                def transcribe(*args, **kwargs):
                    self.assertEqual(-1, kwargs["device"])
                    if variant == "changed_input":
                        config.write_text('{"setting":"never admitted"}')
                    return "synthetic transcript", []
                with patch.object(sys, "argv", argv), \
                        patch.object(tts, "TTSEngine", Engine), \
                        patch.object(asr_backends, "run_transformers_whisper", side_effect=transcribe), \
                        patch.object(asr_backends, "word_error_rate", return_value=0.0), \
                        patch.object(gpu_guard, "acquire_gpu_lock", return_value=object()), \
                        patch.object(gpu_guard, "release_gpu_lock"):
                    if variant == "changed_input":
                        with self.assertRaisesRegex(scl.SceneFixtureError, "input changed"):
                            scl.main()
                    else:
                        scl.main()
                doc = json.loads(manifest.read_text())
                self.assertEqual({"setting": "admitted"}, loaded[-1])
                if variant == "success":
                    self.assertTrue(doc["published"])
                    self.assertEqual(admitted, doc["provenance"]["input_sha256"][os.path.relpath(config, scl.REPO)])
                    for comparison in doc["comparisons"]:
                        for arm, path in comparison["arm_files"].items():
                            self.assertEqual(comparison["arm_sha256"][arm],
                                hashlib.sha256(Path(scl.REPO, path).read_bytes()).hexdigest())
                else:
                    self.assertEqual({"status": "incomplete", "published": False}, doc)
            # Safe IDs can still collide after arm suffixes are appended.
            fixture["scenes"][0]["id"] = "foo"
            fixture["scenes"][1]["id"] = "foo_wrong"
            fixture_path.write_text(json.dumps(fixture))
            argv[-3], argv[-1] = str(root / "collision"), str(root / "collision.json")
            with patch.object(sys, "argv", argv), patch.object(gpu_guard, "acquire_gpu_lock") as lease:
                with self.assertRaises(SystemExit):
                    scl.main()
                lease.assert_not_called()
                self.assertFalse((root / "collision").exists())


if __name__ == "__main__":
    unittest.main()
