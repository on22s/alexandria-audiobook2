"""Idle, book-bound clearing removes only healthy index-owned cache artifacts."""
from contextlib import nullcontext
import hashlib
from pathlib import Path
import unittest

import soundfile as sf
import core
from synthesis_recovery_cache import (RecoveryCacheStorage, CACHE_DIRECTORY,
                                      is_recovery_storage_platform_supported)
from tests import test_synthesis_window_settings_admission as windows
from tests.test_support import use_private_cpu_fixture_lock


@unittest.skipUnless(is_recovery_storage_platform_supported(), 'Linux durability required')
class RecoveryClearTests(unittest.TestCase):
    setUp = windows.SynthesisWindowSettingsAdmissionTests.setUp

    def clear(self, token=None):
        return self.client.post('/api/tts/recovery/clear', json={
            'expected_book_token': token or self.request['expected_book_token']})

    def populate(self):
        source = self.root / self.pm.get_saved_chunks()[0]['audio_path']
        info = sf.info(source)
        with RecoveryCacheStorage(self.root, nullcontext) as storage:
            self.assertTrue(storage.save_window('a' * 64, 'b' * 64, 0, 'c' * 64, source,
                dict(size=source.stat().st_size, sha256=hashlib.sha256(source.read_bytes()).hexdigest(),
                     frames=info.frames, rate=info.samplerate, channels=info.channels), budget_bytes=1024 * 1024))
        return self.root / CACHE_DIRECTORY

    def test_clear_keeps_selected_takes_unknown_files_and_persistent_lock(self):
        cache = self.populate()
        unknown = cache / 'unknown-file'
        unknown.write_bytes(b'keep')
        rows = self.pm.get_saved_chunks()
        audio = {row['audio_path']: (self.root / row['audio_path']).read_bytes() for row in rows}
        lock = cache / 'index.json.lock'
        inode = lock.stat().st_ino
        response = self.clear()
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(1, response.json()['removed_requests'])
        self.assertEqual([], list(cache.glob('*.wav')))
        self.assertEqual(b'keep', unknown.read_bytes())
        self.assertEqual(inode, lock.stat().st_ino)
        self.assertEqual(rows, self.pm.get_saved_chunks())
        self.assertEqual(audio, {path: (self.root / path).read_bytes() for path in audio})
        self.assertEqual(0, self.clear().json()['removed_requests'])

    def test_corrupt_index_refuses_without_reset_or_payload_deletion(self):
        cache = self.populate()
        index = cache / 'index.json'
        index.write_bytes(b'unknown authority')
        files = {path.name: path.read_bytes() for path in cache.iterdir() if path.is_file()}
        response = self.clear()
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(files, {path.name: path.read_bytes() for path in cache.iterdir() if path.is_file()})

    @use_private_cpu_fixture_lock()
    def test_pending_task_refuses_clearing_and_preserves_cache(self):
        cache = self.populate()
        index = (cache / 'index.json').read_bytes()
        claim = core.reserve_background_task('audio', cpu_only=True)
        try:
            response = self.clear()
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(index, (cache / 'index.json').read_bytes())
            self.assertEqual(1, len(list(cache.glob('*.wav'))))
        finally:
            core.release_gpu_task_claim('audio', claim)

    def test_stale_book_token_refuses_before_cache_initialization(self):
        response = self.clear('f' * 64)
        self.assertEqual(409, response.status_code, response.text)
        self.assertFalse((self.root / CACHE_DIRECTORY).exists())
        self.assertEqual(422, self.client.post('/api/tts/recovery/clear', json={}).status_code)

    def test_cache_directory_alias_is_refused_without_touching_target(self):
        target = self.root / 'outside'
        target.mkdir()
        (target / 'keep').write_bytes(b'keep')
        (self.root / CACHE_DIRECTORY).symlink_to(target, target_is_directory=True)
        response = self.clear()
        self.assertEqual(409, response.status_code, response.text)
        self.assertEqual(['keep'], [path.name for path in target.iterdir()])
