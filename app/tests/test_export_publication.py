"""Changed source/book/UID facts cannot become a current assembled delivery."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import export_publication
import export_quality
from tests import test_project_export_quality as fixtures


class ExportPublicationTests(unittest.TestCase):
    def test_changes_after_media_validation_preserve_prior_delivery_and_source_edits(self):
        for change in ('bytes-restored-mtime', 'uid', 'text', 'pause', 'revision', 'order', 'book', 'script'):
            with self.subTest(change=change), tempfile.TemporaryDirectory() as tmp:
                manager, source = fixtures.ProjectExportQualityTests().fixture(tmp)
                prior = Path(tmp) / 'cloned_audiobook.mp3'
                prior.write_bytes(b'prior complete delivery')
                reports = []
                admit = export_quality.get_admitted_export_report
                def edit_after_check(*args, **kwargs):
                    result = admit(*args, **kwargs)
                    if change == 'bytes-restored-mtime':
                        info = source.stat()
                        raw = bytearray(source.read_bytes())
                        raw[-100] ^= 1
                        source.write_bytes(raw)
                        os.utime(source, ns=(info.st_atime_ns, info.st_mtime_ns))
                    elif change == 'book':
                        (Path(tmp) / 'state.json').write_text(json.dumps({'active_book_id': 'other', 'book_generation': 2}))
                    elif change == 'script':
                        (Path(tmp) / 'annotated_script.json').write_text('[]')
                    else:
                        rows = manager.load_chunks()
                        if change == 'order':
                            rows.reverse()
                        else:
                            field = {'uid': 'uid', 'text': 'text', 'pause': 'pause_after',
                                     'revision': 'audio_revision'}[change]
                            rows[0][field] = 2000 if change == 'pause' else 'edited'
                        manager.save_chunks(rows)
                    return result
                with patch.object(export_quality, 'get_admitted_export_report', side_effect=edit_after_check):
                    with self.assertRaisesRegex(ValueError, 'changed'):
                        manager.merge_audio(export_quality_callback=reports.append)
                self.assertEqual(b'prior complete delivery', prior.read_bytes())
                self.assertEqual([], reports)
                fixtures.ProjectExportQualityTests().assert_clean(tmp)

    def test_edit_after_final_hash_is_refused_by_short_metadata_admission(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, source = fixtures.ProjectExportQualityTests().fixture(tmp)
            prior = Path(tmp) / 'cloned_audiobook.mp3'
            prior.write_bytes(b'prior delivery')
            verify = export_publication.ExportSourceSnapshot.verify_sources
            def edit_after_hash(snapshot):
                verify(snapshot)
                raw = bytearray(source.read_bytes())
                raw[-100] ^= 1
                source.write_bytes(raw)
            with patch.object(export_publication.ExportSourceSnapshot, 'verify_sources', edit_after_hash):
                with self.assertRaisesRegex(ValueError, 'source changed before publication'):
                    manager.merge_audio()
            self.assertEqual(b'prior delivery', prior.read_bytes())
            fixtures.ProjectExportQualityTests().assert_clean(tmp)

    def test_supplied_stale_uid_and_changed_rows_during_capture_are_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = fixtures.ProjectExportQualityTests().fixture(tmp)
            stale = manager.load_chunks()
            stale[0]['text'] = 'stale text'
            with self.assertRaisesRegex(ValueError, 'after export admission'):
                manager.merge_audio(chunks=stale)
            load = manager.load_chunks
            def change_between_reads():
                captured = load()
                changed = load()
                changed[0]['pause_after'] = 50
                manager.save_chunks(changed)
                return captured
            with patch.object(manager, 'load_chunks', side_effect=change_between_reads):
                with self.assertRaisesRegex(ValueError, 'while capturing'):
                    manager.merge_audio()
            self.assertFalse((Path(tmp) / 'cloned_audiobook.mp3').exists())

    def test_hashing_is_outside_existing_chunk_lock_and_metadata_admission_does_not_rehash(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = fixtures.ProjectExportQualityTests().fixture(tmp)
            read = export_publication.read_source_identity
            seen = []
            def checked_read(*args, **kwargs):
                self.assertFalse(manager._chunks_lock.locked())
                seen.append(args[0])
                return read(*args, **kwargs)
            with patch.object(export_publication, 'read_source_identity', side_effect=checked_read):
                snapshot = export_publication.ExportSourceSnapshot(manager)
                snapshot.verify_sources()
                checked = len(seen)
                output = Path(tmp) / 'private.wav'
                output.write_bytes(b'private validated bytes')
                report = {'file_version': export_quality.get_export_file_version(output)}
                with snapshot.ensure_publication(output, report):
                    self.assertTrue(manager._chunks_lock.locked())
                    self.assertEqual(checked, len(seen))
            self.assertGreater(checked, 0)

    def test_pause_settings_change_between_tuple_and_identity_capture_refuses_export(self):
        from utils import atomic_json_write
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = fixtures.ProjectExportQualityTests().fixture(tmp)
            rows = manager.load_chunks()
            for row in rows:
                row.pop('pause_after', None)
            manager.save_chunks(rows)
            atomic_json_write({'tts': {'pause_between_speakers_ms': 10, 'pause_same_speaker_ms': 10}}, manager.config_path)
            manager.invalidate_config_cache()
            prior = Path(tmp) / 'cloned_audiobook.mp3'
            prior.write_bytes(b'prior delivery')
            read, changed = export_publication.read_source_identity, False
            def change_settings(*args, **kwargs):
                nonlocal changed
                if not changed:
                    changed = True
                    atomic_json_write({'tts': {'pause_between_speakers_ms': 1000,
                        'pause_same_speaker_ms': 1000}}, manager.config_path)
                    manager.invalidate_config_cache()
                return read(*args, **kwargs)
            with patch.object(export_publication, 'read_source_identity', side_effect=change_settings):
                with self.assertRaisesRegex(ValueError, 'pause settings changed'):
                    manager.merge_audio()
            self.assertTrue(changed)
            self.assertEqual((1000, 1000), manager._load_pause_defaults())
            self.assertEqual(b'prior delivery', prior.read_bytes())

    def test_final_admission_does_not_read_script_payload_or_retry_body_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager, _ = fixtures.ProjectExportQualityTests().fixture(tmp)
            script = Path(manager.script_path)
            script.write_text('[]')
            snapshot = export_publication.ExportSourceSnapshot(manager)
            snapshot.verify_sources()
            output = Path(tmp) / 'private.wav'
            output.write_bytes(b'admitted private file')
            report = {'file_version': export_quality.get_export_file_version(output)}
            read, entries = Path.read_bytes, []
            def no_script_read(path):
                self.assertNotEqual(script, path, 'Script hashing occurred during final admission')
                return read(path)
            with patch.object(Path, 'read_bytes', no_script_read):
                with self.assertRaisesRegex(TimeoutError, 'body exception'):
                    with snapshot.ensure_publication(output, report):
                        entries.append(1)
                        raise TimeoutError('body exception')
            self.assertEqual([1], entries)

    def test_exact_project_root_and_private_regular_file_are_required(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            manager, _ = fixtures.ProjectExportQualityTests().fixture(tmp)
            snapshot = export_publication.ExportSourceSnapshot(manager)
            snapshot.verify_sources()
            output = Path(tmp) / 'private.wav'
            output.write_bytes(b'admitted private file')
            report = {'file_version': export_quality.get_export_file_version(output)}
            manager.root_dir = other
            with self.assertRaisesRegex(ValueError, 'project paths changed'):
                with snapshot.ensure_publication(output, report):
                    self.fail('Wrong project root was admitted')
            manager.root_dir = tmp
            if os.name != 'nt':
                renamed = output.with_name('same-inode.wav')
                output.rename(renamed)
                output.symlink_to(renamed)
                with self.assertRaisesRegex(ValueError, 'regular private file'):
                    with snapshot.ensure_publication(output, report):
                        self.fail('Private symlink was admitted')

    def test_owner_aware_cancel_callback_does_not_reenter_the_owner_mutex(self):
        # Isolate the probe so a reintroduced nonreentrant-lock deadlock has a
        # hard timeout and cannot strand the parent suite's thread or file locks.
        code = '''
import tempfile
import threading
from contextlib import contextmanager
from unittest.mock import patch
import core
from audio_generation_owner import (AudioGenerationOwner, ensure_audio_generation_owner,
                                    is_audio_generation_cancelled)
from tests import test_project_export_quality as fixtures
with tempfile.TemporaryDirectory() as root:
    manager, _ = fixtures.ProjectExportQualityTests().fixture(root)
    load, pauses = manager.load_chunks, manager._load_pause_defaults
    def owner_aware_load():
        assert not is_audio_generation_cancelled()
        return load()
    def owner_aware_pauses():
        assert not is_audio_generation_cancelled()
        return pauses()
    manager.load_chunks, manager._load_pause_defaults = owner_aware_load, owner_aware_pauses
    claim = 'a' * 32
    owner = AudioGenerationOwner(claim, lambda **kwargs: core._ensure_audio_task_owner(claim, **kwargs))
    states = {'audio': {'cancel': False}}
    claims = {'audio': {'id': claim, 'phase': 'started'}}
    with patch.object(core, 'process_state', states), patch.object(core, '_task_claims', claims):
        with ensure_audio_generation_owner(owner):
            result = manager.merge_audio(cancel_check=is_audio_generation_cancelled)
            assert result[0], result
            import export_publication as ep
            import export_quality as eq
            snapshot = ep.ExportSourceSnapshot(manager)
            snapshot.verify_sources()
            private = __import__('pathlib').Path(root) / 'private.wav'
            private.write_bytes(b'admitted bytes')
            report = {'file_version': eq.get_export_file_version(private)}
            entered = threading.Event()
            manager._chunks_lock.acquire()
            original = ep._ensure_export_book
            @contextmanager
            def observed_owner(*args, **kwargs):
                with original(*args, **kwargs):
                    entered.set()
                    yield
            def release_after_owner_check():
                assert entered.wait(5)
                with core._ensure_audio_task_owner(claim):
                    manager._chunks_lock.release()
            worker = threading.Thread(target=release_after_owner_check)
            worker.start()
            with patch.object(ep, '_ensure_export_book', observed_owner):
                with snapshot.ensure_publication(private, report):
                    pass
            worker.join(5)
            assert not worker.is_alive()
print('owner-aware export completed')
'''
        result = subprocess.run([sys.executable, '-c', code], capture_output=True,
            text=True, timeout=20, check=False)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        self.assertIn('owner-aware export completed', result.stdout)


if __name__ == '__main__':
    unittest.main()
