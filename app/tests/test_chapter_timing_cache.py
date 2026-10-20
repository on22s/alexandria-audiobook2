import hashlib
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

import numpy as np
from pydub import AudioSegment
import soundfile as sf
from project import ProjectManager, CHAPTER_EXPORT_DIR
import chapter_timing_cache as cache
import project
import tts


class ChapterTimingCacheTests(unittest.TestCase):
    def make_project(self, root, fmt='wav'):
        manager = ProjectManager(str(root))
        Path(manager.voicelines_dir).mkdir(exist_ok=True)
        chunks = []
        for i, rate in enumerate((16000, 24000, 44100, 24000)):
            path = Path(manager.voicelines_dir, f'line{i}.{fmt}')
            raw = np.r_[np.zeros(rate // 10, dtype=np.int16),
                        np.full(rate // 5 + 1, 2000 + i, dtype=np.int16),
                        np.zeros(rate // 5, dtype=np.int16)]
            segment = AudioSegment(data=raw.tobytes(), sample_width=2, frame_rate=rate, channels=1)
            with path.open('wb') as out:
                segment.export(out, format=fmt, **({'bitrate': '32k'} if fmt == 'mp3' else {}))
            chunks.append({'id': i, 'uid': f'row{i}', 'speaker': 'A' if i % 2 else 'B',
                           'text': f'Chapter {i + 1}', 'pause_after': 17,
                           'audio_path': str(path.relative_to(root))})
        manager.save_chunks(chunks)
        manager._load_pause_defaults = lambda: (100, 50)
        return manager, chunks

    def result(self, root):
        output = root / CHAPTER_EXPORT_DIR
        row = json.loads((output / 'manifest.json').read_text())['chapters'][0]
        return row, (output / row['file']).read_bytes()

    def test_mp3_legacy_index_once_then_cold_selected_export_decodes_only_selected_audio(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); manager, chunks = self.make_project(root, 'mp3')
            original = {p: p.read_bytes() for p in Path(manager.voicelines_dir).iterdir()}
            decoded = [(chunk, manager.get_chunk_audio_segment(chunk)) for chunk in chunks]
            reference = tts.compute_timeline(decoded, *manager._load_pause_defaults())
            reader = manager.get_chunk_audio_segment
            with patch.object(manager, 'get_chunk_audio_segment', wraps=reader) as decode:
                self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=True, chapters=[2])[0])
                self.assertEqual(4, decode.call_count, 'initial indexing must decode each source only once')
                first = self.result(root)
                shutil.rmtree(root / CHAPTER_EXPORT_DIR)
                decode.reset_mock()
                self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=True, chapters=[2])[0])
                self.assertEqual([chunks[2]], [call.args[0] for call in decode.call_args_list])
                self.assertEqual(first, self.result(root))
            # Full and subset exports share the independently decoded reference clock.
            self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=True)[0])
            manifest = json.loads((root / CHAPTER_EXPORT_DIR / 'manifest.json').read_text())
            self.assertEqual(first[0], manifest['chapters'][2])
            self.assertAlmostEqual(tts.get_audio_duration_ms(reference[2][1]), first[0]['duration_ms'])
            for path, raw in original.items():
                self.assertEqual(raw, path.read_bytes())

    def test_changed_unselected_waveform_with_restored_size_and_mtime_invalidates_its_timing_fact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); manager, chunks = self.make_project(root)
            self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=True, chapters=[1])[0])
            before, _ = self.result(root)
            path = root / chunks[0]['audio_path']; stat = path.stat()
            audio, rate = sf.read(path, dtype='int16')
            audio[:] = 2000
            sf.write(path, audio, rate, subtype='PCM_16')
            self.assertEqual(stat.st_size, path.stat().st_size)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            reader = manager.get_chunk_audio_segment
            with patch.object(manager, 'get_chunk_audio_segment', wraps=reader) as decode:
                self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=True, chapters=[1])[0])
                self.assertEqual([chunks[0], chunks[1]], [call.args[0] for call in decode.call_args_list])
            after, _ = self.result(root)
            self.assertNotEqual(before['start_ms'], after['start_ms'])
            self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=True)[0])
            full = json.loads((root / CHAPTER_EXPORT_DIR / 'manifest.json').read_text())['chapters'][1]
            self.assertEqual(after, full)

    def test_corrupted_facts_and_changed_trim_policy_require_fresh_indexing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); manager, chunks = self.make_project(root)
            self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=True, chapters=[1])[0])
            expected = self.result(root)
            path = root / cache.CACHE_FILENAME
            document = json.loads(path.read_text())
            next(iter(document['body']['records'].values()))['shape']['frames'] += 1000
            path.write_text(json.dumps(document))  # A valid JSON edit with a stale integrity digest.
            reader = manager.get_chunk_audio_segment
            with patch.object(manager, 'get_chunk_audio_segment', wraps=reader) as decode:
                self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=True, chapters=[1])[0])
                self.assertEqual(4, decode.call_count)
            self.assertEqual(expected, self.result(root))
            with patch.object(tts, '_EDGE_SILENCE_STEP_MS', 5), \
                    patch.object(manager, 'get_chunk_audio_segment', wraps=reader) as decode:
                self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=True, chapters=[1])[0])
                self.assertEqual(4, decode.call_count)

    def test_cancelled_index_preserves_prior_publication_and_does_not_publish_partial_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); manager, chunks = self.make_project(root)
            self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=True, chapters=[1])[0])
            before = self.result(root)
            (root / cache.CACHE_FILENAME).unlink()
            calls = []
            reader = manager.get_chunk_audio_segment
            def decode(chunk):
                calls.append(chunk)
                return reader(chunk)
            with patch.object(manager, 'get_chunk_audio_segment', side_effect=decode), \
                    patch.object(project, '_export_audio_segment') as encode:
                result = manager.export_chapters(fmt='wav', per_chunk_chapters=True,
                    chapters=[1], cancel_check=lambda: len(calls) >= 2)
            self.assertEqual((False, 'Export cancelled'), result)
            self.assertEqual(2, len(calls))
            encode.assert_not_called()
            self.assertEqual(before, self.result(root))
            self.assertFalse((root / cache.CACHE_FILENAME).exists())
            self.assertEqual([], list(root.glob('.source-pcm-*')))

    def test_source_edit_during_staging_refuses_publication_and_preserves_prior_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); manager, chunks = self.make_project(root)
            self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=True, chapters=[1])[0])
            before = self.result(root)
            export = project._export_audio_segment
            def edit_unselected(*args, **kwargs):
                result = export(*args, **kwargs)
                path = root / chunks[0]['audio_path']
                audio, rate = sf.read(path, dtype='int16'); audio[:] = 3000
                sf.write(path, audio, rate, subtype='PCM_16')
                return result
            with patch.object(project, '_export_audio_segment', side_effect=edit_unselected):
                result = manager.export_chapters(fmt='wav', per_chunk_chapters=True, chapters=[1])
            self.assertFalse(result[0])
            self.assertIn('Audio changed', result[1])
            self.assertEqual(before, self.result(root))
            self.assertEqual([], list((root / CHAPTER_EXPORT_DIR).glob('.chapter-export-*')))

    def test_missing_corrupt_and_repaired_inputs_keep_legacy_admission_and_refresh_negative_facts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); manager, chunks = self.make_project(root)
            broken = root / chunks[0]['audio_path']; broken.write_bytes(b'broken waveform')
            (root / chunks[3]['audio_path']).unlink()
            first = manager.export_chapters(fmt='wav', per_chunk_chapters=True, chapters=[0])
            self.assertTrue(first[0]); self.assertIn('2 chunk(s) skipped', first[1])
            expected = self.result(root)
            reader = manager.get_chunk_audio_segment
            shutil.rmtree(root / CHAPTER_EXPORT_DIR)
            with patch.object(manager, 'get_chunk_audio_segment', wraps=reader) as decode:
                self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=True, chapters=[0])[0])
                self.assertEqual([chunks[0], chunks[1]], [call.args[0] for call in decode.call_args_list],
                                 'existing unreadable input needs a fresh admission attempt')
            self.assertEqual(expected, self.result(root))
            sf.write(broken, np.full(4000, 2000, dtype=np.int16), 16000, subtype='PCM_16')
            with patch.object(manager, 'get_chunk_audio_segment', wraps=reader) as decode:
                self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=True, chapters=[0])[0])
                self.assertEqual([chunks[0]], [call.args[0] for call in decode.call_args_list])
            self.assertEqual('[B] Chapter 1', self.result(root)[0]['title'])

    def test_encoded_input_hashing_is_bounded_and_matches_exact_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'compressed.input'
            raw = b'known encoded bytes' * 100000; path.write_bytes(raw)
            native_open = open
            reads = []
            class Reader:
                def __init__(self, stream): self.stream = stream
                def __enter__(self): return self
                def __exit__(self, *args): self.stream.close()
                def fileno(self): return self.stream.fileno()
                def read(self, size):
                    reads.append(size)
                    self_test.assertGreater(size, 0)
                    self_test.assertLessEqual(size, 65536)
                    return self.stream.read(size)
            self_test = self
            with patch.object(cache, 'open', side_effect=lambda *a, **k: Reader(native_open(*a, **k)), create=True):
                identity = cache.read_source_identity(path, lambda: False)
            self.assertEqual(hashlib.sha256(raw).hexdigest(), identity['sha256'])
            self.assertGreater(len(reads), 10)

    def test_changed_only_checks_content_identity_before_reusing_a_selected_chapter(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); manager, chunks = self.make_project(root)
            self.assertTrue(manager.export_chapters(fmt='wav', per_chunk_chapters=True)[0])
            before = self.result(root)
            path = root / chunks[0]['audio_path']; stat = path.stat()
            original_version = cache._version
            frozen_version = original_version(stat)
            def coarse_filesystem_version(info):
                # Simulate a platform where ctime/mtime cannot reveal this edit.
                if (info.st_dev, info.st_ino) == (stat.st_dev, stat.st_ino):
                    return frozen_version
                return original_version(info)
            audio, rate = sf.read(path, dtype='int16'); audio[:] = 3000
            sf.write(path, audio, rate, subtype='PCM_16')
            self.assertEqual(stat.st_size, path.stat().st_size)
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
            reader = manager.get_chunk_audio_segment
            with patch.object(cache, '_version', side_effect=coarse_filesystem_version), \
                    patch.object(manager, 'get_chunk_audio_segment', wraps=reader) as decode:
                expected_file = before[0]['file']
                self.assertEqual([expected_file], [row['file'] for row in
                    manager.preview_chapter_filenames(fmt='wav', per_chunk_chapters=True,
                                                       chapters=[0], changed_only=True)])
                decode.assert_not_called()
                ok, message = manager.export_chapters(fmt='wav', per_chunk_chapters=True,
                                                       chapters=[0], changed_only=True)
                self.assertTrue(ok, message)
                self.assertIn('1 chapter file(s) written', message)
                self.assertEqual([chunks[0]], [call.args[0] for call in decode.call_args_list])
            self.assertNotEqual(before, self.result(root))

    def test_transient_decoder_failure_is_retried_without_changing_source_bytes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); manager, chunks = self.make_project(root)
            reader = manager.get_chunk_audio_segment
            with patch.object(manager, 'get_chunk_audio_segment', side_effect=lambda chunk:
                              None if chunk == chunks[0] else reader(chunk)):
                ok, message = manager.export_chapters(fmt='wav', per_chunk_chapters=True, chapters=[0])
            self.assertFalse(ok, message)
            self.assertIn('could not be decoded', message)
            self.assertFalse((root / CHAPTER_EXPORT_DIR / 'manifest.json').exists())
            with patch.object(manager, 'get_chunk_audio_segment', wraps=reader) as decode:
                ok, message = manager.export_chapters(fmt='wav', per_chunk_chapters=True, chapters=[0])
                self.assertTrue(ok, message)
                self.assertEqual([chunks[0]], [call.args[0] for call in decode.call_args_list])
            self.assertNotIn('skipped', message)
            self.assertEqual('[B] Chapter 1', self.result(root)[0]['title'])


if __name__ == '__main__':
    unittest.main()
