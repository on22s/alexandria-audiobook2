"""Real bounded snapshots must refuse aliases, replacement and cancellation."""
import hashlib
import tracemalloc
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import project_scope_files as files
from audio_take_history import AudioGenerationCancelled


class ScopeFileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()

    def test_raw_and_streaming_authority_match_without_writes(self):
        path = self.root/'source.txt'; path.write_bytes(b'Synthetic source.\r\n')
        before = path.read_bytes(), path.stat()
        proof, raw = files.capture_scope_file(self.root, 'source.txt', include_bytes=True)
        streamed, omitted = files.capture_scope_file(self.root, 'source.txt')
        self.assertEqual(proof, streamed); self.assertIsNone(omitted)
        self.assertEqual(before[0], raw)
        self.assertEqual(hashlib.sha256(raw).hexdigest(), proof['sha256'])
        self.assertEqual(len(raw), proof['size']); self.assertEqual(5, len(proof['file_version']))
        self.assertEqual(before[0], path.read_bytes())
        self.assertEqual(before[1].st_mtime_ns, path.stat().st_mtime_ns)
        self.assertEqual(['source.txt'], [p.name for p in self.root.iterdir()])

    def test_missing_and_empty_are_distinct_and_do_not_initialize_anything(self):
        missing, raw = files.capture_scope_file(self.root, 'missing.json', include_bytes=True)
        self.assertFalse(missing['exists']); self.assertIsNone(raw)
        self.assertEqual([], list(self.root.iterdir()))
        (self.root/'missing.json').touch()
        empty, raw = files.capture_scope_file(self.root, 'missing.json', include_bytes=True)
        self.assertTrue(empty['exists']); self.assertEqual(b'', raw)
        self.assertNotEqual(missing, empty)

    def test_paths_size_special_files_and_aliases_are_refused(self):
        path = self.root/'source.txt'; path.write_bytes(b'bounded')
        for name in ('../source.txt', str(path), 'C:source.txt', 'a\\source.txt', '.'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                files.capture_scope_file(self.root, name)
        with self.assertRaises(ValueError):files.capture_scope_file(self.root, 'source.txt', limit=2)
        alias = self.root/'alias'; alias.symlink_to(path)
        with self.assertRaises(ValueError):files.capture_scope_file(self.root, 'alias')
        hard = self.root/'hard'; os.link(path, hard)
        with self.assertRaises(ValueError):files.capture_scope_file(self.root, 'hard')
        hard.unlink(); alias.unlink()
        (self.root/'directory').mkdir()
        with self.assertRaises(ValueError):files.capture_scope_file(self.root, 'directory')

    def test_path_replacement_after_hash_is_not_an_accepted_snapshot(self):
        path = self.root/'audio.wav'; path.write_bytes(b'original bytes')
        replacement = self.root/'replacement'; replacement.write_bytes(b'changed bytes')
        real_hash = files._hash
        def replace_after_hash(stream, limit, **kwargs):
            result = real_hash(stream, limit, **kwargs); os.replace(replacement, path); return result
        with patch.object(files, '_hash', replace_after_hash), self.assertRaises(ValueError):
            files.capture_scope_file(self.root, 'audio.wav')
        self.assertEqual(b'changed bytes', path.read_bytes())

    def test_missing_root_and_in_place_changes_are_refused(self):
        with self.assertRaises(ValueError):
            files.capture_scope_file(self.root/'absent', 'state.json')
        self.assertFalse((self.root/'absent').exists())
        path = self.root/'source.txt'; path.write_bytes(b'original')
        real_hash = files._hash
        def change_after_hash(stream, limit, **kwargs):
            result = real_hash(stream, limit, **kwargs)
            path.write_bytes(b'changed, larger bytes')
            return result
        with patch.object(files, '_hash', change_after_hash), self.assertRaises(ValueError):
            files.capture_scope_file(self.root, 'source.txt')
        self.assertEqual(b'changed, larger bytes', path.read_bytes())

    def test_cancellation_precedes_open_and_leaves_bytes_intact(self):
        path = self.root/'source.txt'; path.write_bytes(b'synthetic')
        with patch.object(files, '_open_regular', side_effect=AssertionError('must not open')):
            with self.assertRaises(AudioGenerationCancelled):
                files.capture_scope_file(self.root, 'source.txt', cancel_check=lambda: True)
        self.assertEqual(b'synthetic', path.read_bytes())

    def test_tiny_metadata_read_does_not_allocate_the_whole_file_limit(self):
        (self.root/'state.json').write_bytes(b'{}')
        tracemalloc.start()
        try:
            proof, raw = files.capture_scope_file(self.root, 'state.json', include_bytes=True)
            _current, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertEqual((b'{}', 2), (raw, proof['size']))
        self.assertLess(peak, 1024**2)

    def test_raw_read_refuses_growth_after_the_descriptor_is_admitted(self):
        path = self.root/'state.json';path.write_bytes(b'{}')
        original = files._open_regular
        def grow(*args, **kwargs):
            stream, before = original(*args, **kwargs)
            path.write_bytes(b'{"changed":true}')
            return stream, before
        with patch.object(files, '_open_regular', grow), self.assertRaisesRegex(ValueError, 'changed while reading'):
            files.capture_scope_file(self.root, 'state.json', include_bytes=True)
