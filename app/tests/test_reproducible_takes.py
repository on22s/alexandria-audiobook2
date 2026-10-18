import copy
import json
import os
import tempfile
import unittest
import wave
from unittest import mock

import project
import tts


class ChunkSeedTests(unittest.TestCase):
    def test_seed_is_stable_and_whitespace_normalized(self):
        self.assertEqual(tts.get_chunk_seed("  Hello   world  "), tts.get_chunk_seed("Hello world"))
        self.assertEqual(tts.get_chunk_seed("same", 2), tts.get_chunk_seed("same", 2))

    def test_take_changes_seed_and_stays_in_range(self):
        self.assertNotEqual(tts.get_chunk_seed("same", 0), tts.get_chunk_seed("same", 1))
        for take in (0, 1, 999_999):
            seed = tts.get_chunk_seed("Line", take)
            self.assertGreaterEqual(seed, 0)
            self.assertLess(seed, 2 ** 31)


class ReproducibleVoiceConfigTests(unittest.TestCase):
    def test_unpinned_voice_seeds_are_replaced_without_mutation(self):
        resolved = {
            "alice": {"seed": -1, "style": ["calm"]},
            "bob": {"seed": 7},
            "carol": {"seed": "not-a-number"},
            "dave": {},
            "note": "keep",
            "items": [1, 2],
        }
        original = copy.deepcopy(resolved)

        result = tts.get_reproducible_voice_config(resolved, 123)

        self.assertEqual(result["alice"], {"seed": 123, "style": ["calm"]})
        self.assertEqual(result["bob"], {"seed": 7})
        self.assertEqual(result["carol"], {"seed": 123})
        self.assertEqual(result["dave"], {"seed": 123})
        self.assertEqual(result["note"], "keep")
        self.assertEqual(result["items"], [1, 2])
        self.assertEqual(resolved, original)
        self.assertIsNot(result["alice"], resolved["alice"])


class CapturingEngine:
    def __init__(self, success):
        self.success = success
        self.args = None

    def generate_voice(self, text, instruct, speaker, voice_config, output_path):
        self.args = (text, instruct, speaker, voice_config, output_path)
        with wave.open(output_path, "wb") as handle:
            handle.setnchannels(1)
            handle.setsampwidth(2)
            handle.setframerate(8000)
            handle.writeframes(bytes([0, 0]) * 8)
        return self.success


class ProjectReproducibleTakeTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.tempdir.cleanup()

    def make_manager(self, reproducible, engine):
        manager = object.__new__(project.ProjectManager)
        manager.root_dir = self.tempdir.name
        manager.voice_config_path = os.path.join(self.tempdir.name, "voices.json")
        with open(manager.voice_config_path, "w", encoding="utf-8") as handle:
            json.dump({}, handle)
        manager._resolve_alias = lambda speaker, voice_config: speaker
        manager._load_tts_config = lambda: {"reproducible_takes": reproducible}
        manager.get_engine = lambda: engine

        def update_fields_by_uid(uid, expected_chunk=None, status=None, error=None, **kwargs):
            target = expected_chunk if expected_chunk is not None else {"uid": uid}
            if status is not None:
                target["status"] = status
            if error is not None or status == "error":
                target["error"] = error
            return target

        manager._update_chunk_fields_by_uid = update_fields_by_uid
        return manager

    def render_chunk(self, reproducible, take):
        engine = CapturingEngine(success=False)
        manager = self.make_manager(reproducible, engine)
        chunk = {
            "id": 1,
            "uid": "uid-1",
            "speaker": "Alice",
            "text": " Hello   world ",
            "instruct": "flat",
            "take": take,
            "status": "pending",
            "audio_path": None,
        }

        def resolve_narrator(speaker, voice_config, chunk):
            return voice_config

        def voice_config(voice_config, speaker, index):
            return {"alice": {"seed": -1}}

        with mock.patch.object(project, "resolve_narrator_voice_config", resolve_narrator):
            with mock.patch.object(project, "voice_config_for_chunk", voice_config):
                success, _ = manager._generate_captured_chunk_audio(0, chunk)
        return engine, success

    def test_reproducible_takes_passes_derived_seed_to_engine(self):
        engine, success = self.render_chunk(reproducible=True, take=3)
        self.assertFalse(success)
        self.assertIsNotNone(engine.args)
        self.assertEqual(
            engine.args[3]["alice"]["seed"],
            tts.get_chunk_seed(" Hello   world ", 3))

    def test_reproducible_takes_off_keeps_voice_seed(self):
        engine, success = self.render_chunk(reproducible=False, take=3)
        self.assertFalse(success)
        self.assertIsNotNone(engine.args)
        self.assertEqual(engine.args[3]["alice"]["seed"], -1)

    def test_changing_take_marks_chunk_stale(self):
        manager = object.__new__(project.ProjectManager)
        base = {
            "text": "Hello",
            "instruct": "",
            "speaker": "Alice",
            "take": 0,
            "status": "done",
            "audio_path": "old.wav",
            "drift": {"score": 1},
        }

        def modify_chunk(index, mutator):
            chunk = dict(base)
            mutator(chunk)
            return chunk

        manager._modify_chunk = modify_chunk
        result = manager.update_chunk(0, {"take": 1})

        self.assertEqual(result["take"], 1)
        self.assertEqual(result["status"], "pending")
        self.assertIsNone(result["audio_path"])
        self.assertIsNone(result["drift"])

    def test_same_take_keeps_render(self):
        manager = object.__new__(project.ProjectManager)
        base = {
            "text": "Hello",
            "instruct": "",
            "speaker": "Alice",
            "take": 0,
            "status": "done",
            "audio_path": "old.wav",
        }

        def modify_chunk(index, mutator):
            chunk = dict(base)
            mutator(chunk)
            return chunk

        manager._modify_chunk = modify_chunk
        result = manager.update_chunk(0, {"take": 0})

        self.assertEqual(result["take"], 0)
        self.assertEqual(result["status"], "done")
        self.assertEqual(result["audio_path"], "old.wav")


if __name__ == "__main__":
    unittest.main()
