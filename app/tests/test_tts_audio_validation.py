"""tts.py must not hand back audio it has not looked at.

THE ASYMMETRY THIS CLOSES. `project.py` validated audio when assembling chunks
into an export. `tts.py`, the layer that CREATES the audio, called sf.write and
returned - eleven `os.path.exists`/`getsize` checks and no decoding. A guard in
the assembly layer while the generating layer is unguarded is worse than no
guard: the pipeline looks protected.

Every generation path - lora, clone, custom, design, and the three batch
variants - funnels through `TTSEngine._save_wav`, so the check belongs there
rather than in seven callers that would drift apart. These tests assert that
funnel property as well as the checking, because if a future path writes
directly the coverage silently disappears.
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audio_validation import GeneratedAudioError, validate_generated_audio

TTS_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tts.py")


def _wav(path, seconds=0.5, rate=24000):
    import numpy as np
    import soundfile as sf
    sf.write(path, np.zeros(int(rate * seconds), dtype="float32"), rate)
    return path


class TestClonePromptCacheInputs(unittest.TestCase):
    def test_reference_text_and_same_path_audio_bytes_invalidate_prompt(self):
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        import numpy as np
        import soundfile as sf
        from tts import TTSEngine
        with tempfile.TemporaryDirectory() as tmp:
            ref = os.path.join(tmp, "ref.wav")
            sf.write(ref, np.zeros(1200), 24000)
            voice = {"ALICE": {"ref_audio": ref, "ref_text": "Old text"}}
            engine = TTSEngine({"tts": {"mode": "local"}})
            prompts = [object(), object(), object()]
            create = Mock(side_effect=prompts)
            model = SimpleNamespace(create_voice_clone_prompt=create)
            with patch.object(engine, "_init_local_clone", return_value=model):
                first = engine._get_clone_prompt("ALICE", voice)
                self.assertIs(first, engine._get_clone_prompt("ALICE", voice))
                voice["ALICE"]["ref_text"] = "New text"
                second = engine._get_clone_prompt("ALICE", voice)
                self.assertIsNot(first, second)
                stat = os.stat(ref)
                sf.write(ref, np.full(1200, 0.25), 24000)
                self.assertEqual(stat.st_size, os.path.getsize(ref))
                os.utime(ref, ns=(stat.st_atime_ns, stat.st_mtime_ns))
                third = engine._get_clone_prompt("ALICE", voice)
                self.assertIsNot(second, third)
                self.assertIs(third, engine._get_clone_prompt("ALICE", voice))
            self.assertEqual(3, create.call_count)
            self.assertEqual("New text", create.call_args.kwargs["ref_text"])
            audio, rate = create.call_args.kwargs["ref_audio"]
            self.assertEqual(24000, rate)
            self.assertTrue(np.allclose(audio, 0.25))


class TestSaveWavIsTheOnlyFunnel(unittest.TestCase):
    """Structural: if a generation path stops using _save_wav, the validation
    added there stops covering it, and nothing else would say so."""

    def _paths(self):
        import ast
        with open(TTS_PATH, encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        return {n.name: ast.unparse(n) for n in ast.walk(tree)
                if isinstance(n, ast.FunctionDef)}

    def test_every_generation_path_writes_through_save_wav(self):
        expected = ["generate_lora_voice", "generate_voice_design",
                    "_local_generate_custom", "_local_generate_clone",
                    "_local_batch_custom", "_local_batch_clone",
                    "_local_batch_lora"]
        fns = self._paths()
        for name in expected:
            with self.subTest(path=name):
                self.assertIn(name, fns, f"{name} missing from tts.py")
                import ast
                pending = [name]
                visited = set()
                while pending:
                    method = pending.pop()
                    if method in visited:
                        continue
                    visited.add(method)
                    if method not in fns:
                        continue
                    pending.extend(node.func.attr for node in ast.walk(ast.parse(fns[method]))
                                   if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                                   and isinstance(node.func.value, ast.Name) and node.func.value.id == "self")
                self.assertIn("_save_wav", visited,
                              f"{name} no longer reaches validated _save_wav through its helper calls")

    def test_save_wav_rejects_bad_fresh_bytes_after_clearing_prior_audio(self):
        import numpy as np
        import soundfile as sf
        from unittest.mock import patch
        from tts import TTSEngine
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "render.wav")
            _wav(path)
            def malformed(output, values, rate):
                self.assertFalse(os.path.exists(output), "prior render survived into the write")
                with open(output, "wb") as handle:
                    handle.write(b"invalid fresh audio")
            with patch.object(sf, "write", side_effect=malformed), self.assertRaises(GeneratedAudioError):
                TTSEngine._save_wav(np.ones(2400), 24000, path)
            with open(path, "rb") as handle:
                self.assertEqual(b"invalid fresh audio", handle.read())


class TestValidationRejectsBadAudio(unittest.TestCase):
    """Behavioural, against the validator _save_wav now calls."""

    def test_a_truncated_wav_is_rejected(self):
        """The case a decode check alone misses.

        Truncating a real render still decodes - libsndfile returns the frames
        that happen to be present. Only the declared RIFF size catches it.
        """
        with tempfile.TemporaryDirectory() as tmp:
            good = _wav(os.path.join(tmp, "good.wav"), seconds=2.0)
            with open(good, "rb") as fh:
                whole = fh.read()
            bad = os.path.join(tmp, "bad.wav")
            with open(bad, "wb") as fh:
                fh.write(whole[:len(whole) // 4])

            # Prove the premise: it really does decode.
            import soundfile as sf
            self.assertGreater(sf.info(bad).frames, 0,
                               "premise failed - this test would pass for the "
                               "wrong reason if the file were unreadable")

            with self.assertRaises(GeneratedAudioError) as cm:
                validate_generated_audio(bad, "test")
            self.assertIn("truncated", str(cm.exception))

    def test_a_non_audio_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "a.wav")
            with open(p, "wb") as fh:
                fh.write(b"not audio at all")
            with self.assertRaises(GeneratedAudioError):
                validate_generated_audio(p, "test")

    def test_an_empty_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = os.path.join(tmp, "a.wav")
            open(p, "wb").close()
            with self.assertRaises(GeneratedAudioError) as cm:
                validate_generated_audio(p, "test")
            self.assertIn("empty", str(cm.exception))

    def test_a_missing_file_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(GeneratedAudioError):
                validate_generated_audio(os.path.join(tmp, "nope.wav"), "test")

    def test_real_audio_passes(self):
        """A validator that fails closed on everything is worse than none."""
        with tempfile.TemporaryDirectory() as tmp:
            p = _wav(os.path.join(tmp, "a.wav"))
            self.assertEqual(validate_generated_audio(p, "test"), p)





class TestDesignPreviewOwnership(unittest.TestCase):
    def test_same_clock_previews_keep_distinct_audio_and_chunk_cleanup_is_local(self):
        from pathlib import Path
        from types import SimpleNamespace
        from unittest.mock import Mock, patch
        import numpy as np
        import soundfile as sf
        from tts import TTSEngine

        engine = TTSEngine.__new__(TTSEngine)
        engine._mode = "local"
        engine._language = "English"
        engine._max_new_tokens = 64
        model = SimpleNamespace(generate_voice_design=Mock(side_effect=[
            ([np.full(2400, value, dtype="float32")], 24000)
            for value in (0.1, 0.2, 0.3)]))
        with tempfile.TemporaryDirectory() as root:
            previews = Path(root, "previews")
            chunk = Path(root, "chunk.wav")
            with patch.object(engine, "_init_local_design", return_value=model), \
                 patch("tts._resolve_asset_path", return_value=str(previews)), \
                 patch("time.time", return_value=1234.0):
                first, rate = engine.generate_voice_design("First voice", "First line.")
                first_bytes = Path(first).read_bytes()
                second, second_rate = engine.generate_voice_design("Second voice", "Second line.")
                self.assertNotEqual(first, second)
                self.assertEqual(first_bytes, Path(first).read_bytes())
                self.assertTrue(engine.generate_design_voice(
                    "Chunk line.", "", {"description": "Third voice"}, str(chunk)))
            self.assertEqual({Path(first).name, Path(second).name},
                             {path.name for path in previews.iterdir()})
            self.assertEqual((24000, 24000), (rate, second_rate))
            for path, expected in ((first, 0.1), (second, 0.2), (chunk, 0.3)):
                audio, actual_rate = sf.read(path)
                self.assertEqual((2400,), audio.shape)
                self.assertEqual(24000, actual_rate)
                self.assertTrue(np.allclose(audio, expected, atol=1 / 32768))
            self.assertEqual(first_bytes, Path(first).read_bytes())


if __name__ == "__main__":
    unittest.main()


class EmptyEnsembleMemberTests(unittest.TestCase):
    def test_zero_frame_member_is_refused_before_mixing_or_replacing_previous_output(self):
        import numpy as np
        import soundfile as sf
        from pathlib import Path
        from tts import mix_to_unison
        with tempfile.TemporaryDirectory() as tmp:
            empty, valid, output = (Path(tmp) / name for name in ('empty.wav', 'valid.wav', 'mixed.wav'))
            sf.write(empty, np.array([], dtype=np.float32), 24000)
            sf.write(valid, np.sin(np.arange(2400) / 10) * .1, 24000)
            original = b'previous ensemble render'
            output.write_bytes(original)
            for inputs in ([empty], [empty, valid], [valid, empty]):
                with self.subTest(inputs=[p.name for p in inputs]):
                    with self.assertRaisesRegex(ValueError, 'no audio frames'):
                        mix_to_unison(inputs, output)
                    self.assertEqual(original, output.read_bytes())
            self.assertEqual(0, sf.info(empty).frames)
            self.assertEqual(2400, sf.info(valid).frames)
