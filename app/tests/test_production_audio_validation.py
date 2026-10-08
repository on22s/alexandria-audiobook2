import json
import os
import sys
import tempfile
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from audio_validation import GeneratedAudioError, validate_generated_audio
import project as project_module
from project import ProjectManager


def _write_wav(path, frames=2400):
    sf.write(path, np.zeros(frames, dtype=np.float32), 24000, format="WAV")


class SharedAudioValidationTests(unittest.TestCase):
    def test_near_limit_riff_lengths_cannot_exempt_a_small_decodable_file(self):
        import struct
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fixture.wav'
            _write_wav(path, frames=80)
            original = path.read_bytes()
            self.assertEqual(path, validate_generated_audio(path))
            for declared in (0xffffffe8, 0xfffffff0, 0xffffffff):
                with self.subTest(declared=declared):
                    path.write_bytes(original[:4] + struct.pack('<I', declared) + original[8:])
                    with self.assertRaisesRegex(GeneratedAudioError, 'truncated audio'):
                        validate_generated_audio(path)
            # Only the file-extent boundary is simulated; no multi-GiB decoding claim.
            with patch('audio_validation.os.path.getsize', return_value=2**32 + len(original)):
                self.assertEqual(path, validate_generated_audio(path))
            sf.write(path, np.full(80, 0.25), 24000, format='RF64')
            self.assertEqual(b'RF64', path.read_bytes()[:4])
            self.assertEqual(path, validate_generated_audio(path))

    def test_nonfinite_generated_samples_refuse_before_pcm_encoding(self):
        from pathlib import Path
        from audio_validation import save_generated_wav
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'fixture.wav'
            for channels in (1, 2):
                for value in (float('nan'), float('inf'), -float('inf')):
                    with self.subTest(channels=channels, value=value):
                        samples = np.zeros((8, channels), dtype=np.float32)
                        samples[-1, -1] = value
                        _write_wav(path)
                        with patch.object(sf, 'write', wraps=sf.write) as writer:
                            with self.assertRaisesRegex(GeneratedAudioError, 'non-finite'):
                                save_generated_wav(samples, 24000, path)
                        writer.assert_not_called()
                        self.assertFalse(path.exists())
            samples = np.full((8, 2), 0.25, dtype=np.float32)
            self.assertEqual(path, save_generated_wav(samples, 24000, path))
            decoded, rate = sf.read(path, always_2d=True)
            self.assertEqual(24000, rate)
            np.testing.assert_allclose(samples, decoded, atol=1/32768)

    def test_failed_chunk_export_preserves_prior_mp3_and_wav(self):
        with tempfile.TemporaryDirectory() as root:
            manager = ProjectManager(root)
            os.makedirs(manager.voicelines_dir, exist_ok=True)
            source = os.path.join(root, "source.wav")
            _write_wav(source)
            mp3 = os.path.join(manager.voicelines_dir, "line.mp3")
            wav = os.path.join(manager.voicelines_dir, "line.wav")
            with open(mp3, "wb") as output:
                output.write(b"prior mp3")
            with open(wav, "wb") as output:
                output.write(b"prior wav")

            def partial_mp3(_segment, path, _format, **_kwargs):
                with open(path, "wb") as output:
                    output.write(b"partial")
                raise OSError("encoder failed")

            with patch.object(project_module, "_export_audio_segment", side_effect=partial_mp3), \
                 patch.object(project_module.shutil, "copy2", side_effect=OSError("copy failed")):
                with self.assertRaisesRegex(OSError, "copy failed"):
                    manager._export_chunk_audio(source, "line")
            with open(mp3, "rb") as source_file:
                self.assertEqual(b"prior mp3", source_file.read())
            with open(wav, "rb") as source_file:
                self.assertEqual(b"prior wav", source_file.read())
            self.assertEqual(["line.mp3", "line.wav"],
                             sorted(os.listdir(manager.voicelines_dir)))

    def test_missing_empty_and_zero_frame_outputs_fail_loudly(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = os.path.join(tmp, "missing.wav")
            with self.assertRaisesRegex(GeneratedAudioError, "wrote no file"):
                validate_generated_audio(missing, "test")

            empty = os.path.join(tmp, "empty.wav")
            open(empty, "wb").close()
            with self.assertRaisesRegex(GeneratedAudioError, "empty file"):
                validate_generated_audio(empty, "test")

            zero = os.path.join(tmp, "zero.wav")
            _write_wav(zero, frames=0)
            with self.assertRaisesRegex(GeneratedAudioError, "no audio frames"):
                validate_generated_audio(zero, "test")

    def test_decodable_partial_riff_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "partial.wav")
            _write_wav(path, frames=24000)
            with open(path, "rb") as handle:
                partial = handle.read(os.path.getsize(path) // 4)
            with open(path, "wb") as handle:
                handle.write(partial)
            self.assertGreater(sf.info(path).frames, 0)
            with self.assertRaisesRegex(GeneratedAudioError, "truncated audio"):
                validate_generated_audio(path, "test")

    def test_valid_audio_passes_after_full_decode(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "valid.wav")
            _write_wav(path)
            self.assertEqual(path, validate_generated_audio(path, "test"))


class _BatchEngine:
    def __init__(self, write_output):
        self.write_output = write_output
        self.saw_stale = None

    def generate_batch(self, chunks, voice_config, output_dir, batch_seed):
        idx = chunks[0]["index"]
        path = os.path.join(output_dir, f"temp_batch_{idx}.wav")
        self.saw_stale = os.path.exists(path)
        if self.write_output:
            _write_wav(path)
        return {"completed": [idx], "failed": []}


class _SingleEngine:
    def generate_voice(self, text, instruct, speaker, voice_config, output_path):
        with open(output_path, "wb") as handle:
            handle.write(b"not audio")
        return True


class ProductionCompletionBoundaryTests(unittest.TestCase):
    def test_changed_render_fields_detach_old_audio_and_exports_cannot_use_it(self):
        for field, value in (("text", "Changed."), ("speaker", "ALICE"),
                             ("instruct", "Angry.")):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as tmp:
                manager = self._manager(tmp)
                path = os.path.join(manager.voicelines_dir, "old.wav")
                _write_wav(path)
                manager._update_chunk_fields(0, status="done", audio_path="voicelines/old.wav")
                manager.update_chunk(0, {field: value})
                chunk = manager.load_chunks()[0]
                self.assertEqual("pending", chunk["status"])
                self.assertIsNone(chunk.get("audio_path"))
                self.assertEqual(([], 1), manager._load_chunks_with_audio())
                self.assertFalse(manager.merge_audio()[0])
                self.assertFalse(manager.export_chapters(fmt="wav")[0])
                self.assertEqual(path, validate_generated_audio(path), "old audio remains recoverable on disk")

    def test_unchanged_render_fields_and_pause_only_edits_keep_current_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(tmp)
            path = os.path.join(manager.voicelines_dir, "old.wav")
            _write_wav(path)
            manager._update_chunk_fields(0, status="done", audio_path="voicelines/old.wav")
            manager.update_chunk(0, {"text": "Hello.", "speaker": "NARRATOR", "instruct": ""})
            manager.update_chunk(0, {"pause_after": 500})
            chunk = manager.load_chunks()[0]
            self.assertEqual("done", chunk["status"])
            self.assertEqual("voicelines/old.wav", chunk["audio_path"])
            self.assertEqual(500, chunk["pause_after"])
            audio, skipped = manager._load_chunks_with_audio()
            self.assertEqual(1, len(audio))
            self.assertEqual(0, skipped)

    def test_large_undecodable_mp3_falls_back_to_validated_source_wav(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            source = os.path.join(tmp, "source.wav")
            _write_wav(source)
            def corrupt_export(segment, path, *args, **kwargs):
                with open(path, "wb") as handle:
                    handle.write(b"broken MP3 header" * 200)
            with patch("project._export_audio_segment", side_effect=corrupt_export):
                result = manager._export_chunk_audio(source, "line")
            self.assertEqual("voicelines/line.wav", result)
            output = os.path.join(tmp, result)
            self.assertEqual(output, validate_generated_audio(output))
            with open(source, "rb") as before, open(output, "rb") as after:
                self.assertEqual(before.read(), after.read())

    def _manager(self, root):
        manager = ProjectManager(root)
        with open(manager.chunks_path, "w", encoding="utf-8") as handle:
            json.dump([{"uid": "stable", "speaker": "NARRATOR",
                        "text": "Hello.", "instruct": "",
                        "status": "pending"}], handle)
        with open(manager.voice_config_path, "w", encoding="utf-8") as handle:
            json.dump({"NARRATOR": {"type": "custom", "voice": "Ryan"}},
                      handle)
        return manager

    def test_batch_deletes_stale_output_before_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(tmp)
            stale = os.path.join(tmp, "temp_batch_0.wav")
            _write_wav(stale)
            manager.engine = _BatchEngine(write_output=True)
            with patch("project._export_audio_segment", side_effect=RuntimeError("force WAV fallback")):
                result = manager.generate_chunks_batch([0], batch_size=1)
            self.assertFalse(manager.engine.saw_stale)
            self.assertEqual([0], result["completed"])
            chunk = manager.load_chunks()[0]
            self.assertEqual("done", chunk["status"])
            self.assertIsNone(chunk["error"])

    def test_false_batch_completion_without_fresh_file_is_an_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(tmp)
            stale = os.path.join(tmp, "temp_batch_0.wav")
            _write_wav(stale)
            manager.engine = _BatchEngine(write_output=False)
            result = manager.generate_chunks_batch([0], batch_size=1)
            self.assertFalse(manager.engine.saw_stale)
            self.assertEqual([], result["completed"])
            self.assertIn("Temp audio file not found", result["failed"][0][1])
            chunk = manager.load_chunks()[0]
            self.assertEqual("error", chunk["status"])
            self.assertIn("Temp audio file not found", chunk["error"])

    def test_single_chunk_rejects_undecodable_success_and_records_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = self._manager(tmp)
            manager.engine = _SingleEngine()
            success, error = manager.generate_chunk_audio(0)
            self.assertFalse(success)
            self.assertIn("undecodable audio", error)
            chunk = manager.load_chunks()[0]
            self.assertEqual("error", chunk["status"])
            self.assertEqual(error, chunk["error"])


class EngineInitializationTests(unittest.TestCase):
    def test_parallel_first_use_constructs_and_returns_one_engine(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            constructor_entered = threading.Event()
            release_constructor = threading.Event()
            second_started = threading.Event()
            engines, results = [], []
            def construct(config):
                engine = SimpleNamespace(mode="local")
                engines.append(engine)
                constructor_entered.set()
                if not release_constructor.wait(2):
                    raise RuntimeError("test constructor timed out")
                return engine
            def second_call():
                second_started.set()
                results.append(manager.get_engine())
            with patch("project.TTSEngine", side_effect=construct):
                first = threading.Thread(target=lambda: results.append(manager.get_engine()))
                second = threading.Thread(target=second_call)
                first.start()
                self.assertTrue(constructor_entered.wait(2))
                second.start()
                self.assertTrue(second_started.wait(2))
                try:
                    second.join(0.1)
                    self.assertEqual(1, len(engines), "concurrent callers must share initialization")
                finally:
                    release_constructor.set()
                    first.join(2)
                    second.join(2)
                self.assertFalse(first.is_alive() or second.is_alive())
                self.assertEqual(2, len(results))
                self.assertTrue(all(result is engines[0] for result in results))
                self.assertIs(engines[0], manager.get_engine())

    def test_initialization_failure_releases_lock_and_later_use_can_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ProjectManager(tmp)
            engine = SimpleNamespace(mode="local")
            with patch("project.TTSEngine", side_effect=[RuntimeError("failed"), engine]) as construct:
                self.assertIsNone(manager.get_engine())
                self.assertIs(engine, manager.get_engine())
                self.assertIs(engine, manager.get_engine())
                self.assertEqual(2, construct.call_count)


if __name__ == "__main__":
    unittest.main()


class DeclaredAudioFrameTests(unittest.TestCase):
    @staticmethod
    def _ogg_crc(data):
        value = 0
        for byte in data:
            value ^= byte << 24
            for _ in range(8):
                value = ((value << 1) ^ 0x04C11DB7 if value & 0x80000000 else value << 1) & 0xFFFFFFFF
        return value

    def test_non_riff_header_cannot_claim_more_frames_than_decoder_returns(self):
        import struct
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "incomplete.ogg"
            sf.write(path, np.sin(np.arange(80000) / 20) * 0.2, 24000,
                     format="OGG", subtype="VORBIS")
            self.assertEqual(str(path), validate_generated_audio(str(path)))
            raw = bytearray(path.read_bytes())
            offset = 0
            while offset < len(raw):
                last = offset
                segments = raw[offset + 26]
                page_size = 27 + segments + sum(raw[offset + 27:offset + 27 + segments])
                offset += page_size
            # Preserve a valid container/page checksum, but let the final
            # granule claim samples that were never encoded.
            declared = struct.unpack_from("<q", raw, last + 6)[0] + 80000
            struct.pack_into("<q", raw, last + 6, declared)
            raw[last + 22:last + 26] = bytes(4)
            struct.pack_into("<I", raw, last + 22, self._ogg_crc(raw[last:]))
            path.write_bytes(raw)
            self.assertEqual(declared, sf.info(path).frames)
            audio, _ = sf.read(path)
            self.assertGreater(len(audio), 0)
            self.assertLess(len(audio), declared)
            with self.assertRaisesRegex(GeneratedAudioError, "frame count.*declares.*decoded"):
                validate_generated_audio(str(path), "test")
            self.assertEqual(raw, path.read_bytes())

    def test_complete_mono_and_stereo_containers_pass_full_frame_check(self):
        from pathlib import Path
        for fmt, subtype in (("WAV", "PCM_16"), ("FLAC", "PCM_16"),
                             ("OGG", "VORBIS"), ("MP3", "MPEG_LAYER_III")):
            for channels in (1, 2):
                with self.subTest(format=fmt, channels=channels), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / ("complete." + fmt.lower())
                    mono = np.sin(np.arange(80000) / 20) * 0.2
                    samples = mono if channels == 1 else np.column_stack((mono, -mono))
                    sf.write(path, samples, 44100, format=fmt, subtype=subtype)
                    declared = sf.info(path).frames
                    decoded, _ = sf.read(path, always_2d=True)
                    self.assertEqual(declared, len(decoded))
                    self.assertEqual(channels, decoded.shape[1])
                    self.assertGreater(float(np.max(np.abs(decoded))), 0.1)
                    self.assertEqual(str(path), validate_generated_audio(str(path), "test"))
