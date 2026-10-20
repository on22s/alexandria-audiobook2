"""Real encoded takes survive failed metadata saves and interrupted publication."""
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from project import ProjectManager


class ChunkAudioSaveFailureTests(unittest.TestCase):
    def create_project(self, root, prior=True):
        manager = ProjectManager(str(root))
        previous = root / 'voicelines' / 'voiceline_stable_narrator.wav'
        fresh = root / 'fresh.wav'
        for path, frequency in ((previous, 220), (fresh, 880)):
            sf.write(path, .2 * np.sin(2 * np.pi * frequency * np.arange(12000) / 24000), 24000)
        manager.save_chunks([dict(uid='stable', id=0, speaker='NARRATOR',
            text='A short synthetic sentence.', instruct='', status='done' if prior else 'pending',
            audio_path='voicelines/' + previous.name if prior else None,
            audio_revision='approved', custom={'keep': True})])
        if not prior:
            previous.unlink()
        return manager, previous, fresh

    def get_codec_context(self, codec):
        return (patch('project._export_audio_segment', side_effect=RuntimeError('force WAV fallback'))
                if codec == 'wav' else contextlib.nullcontext())

    def test_failed_snapshot_or_append_preserves_prior_bytes_and_revision(self):
        for codec in ('wav', 'mp3'):
            for storage in ('snapshot', 'journal'):
                for prior in (False, True):
                    with self.subTest(codec=codec, storage=storage, prior=prior), \
                            tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
                        root = Path(tmp)
                        manager, previous, fresh = self.create_project(root, prior)
                        before = manager.load_chunks()
                        old_bytes = previous.read_bytes() if prior else None
                        journal = manager._get_status_journal_locked() if storage == 'journal' else None
                        if journal:
                            journal.ensure_writable()
                        owner, method = (journal, '_append') if journal else (manager, '_save_chunks_locked')
                        with self.get_codec_context(codec), patch.object(
                                owner, method, side_effect=OSError('injected metadata save failure')):
                            for attempt in range(4):
                                with self.assertRaisesRegex(OSError, 'metadata save failure'):
                                    manager._publish_generated_chunk_audio(before[0], str(fresh), 'NARRATOR')
                                self.assertEqual(before, ProjectManager(tmp).load_chunks())
                                self.assertEqual(old_bytes, previous.read_bytes() if prior else None)
                                self.assertEqual([], list((root / 'voicelines').glob('.render-*')))
                                installed = list((root / 'voicelines').glob('*.slot-*'))
                                self.assertEqual(len(installed), 1)
                                self.assertEqual(installed[0].suffix, '.' + codec)
                        with self.get_codec_context(codec):
                            path = manager._publish_generated_chunk_audio(before[0], str(fresh), 'NARRATOR')
                        saved = ProjectManager(tmp).load_chunks()[0]
                        self.assertEqual(saved['audio_path'], path)
                        self.assertEqual(saved['status'], 'done')
                        self.assertNotEqual(saved['audio_revision'], 'approved')
                        self.assertEqual(saved['custom'], before[0]['custom'])
                        self.assertEqual(old_bytes, previous.read_bytes() if prior else None)

    def test_post_commit_error_keeps_new_revision_paired_with_its_audio(self):
        for storage in ('snapshot', 'journal'):
            with self.subTest(storage=storage), tempfile.TemporaryDirectory() as tmp, \
                    contextlib.redirect_stdout(io.StringIO()):
                root = Path(tmp)
                manager, previous, fresh = self.create_project(root)
                before, old_bytes = manager.load_chunks()[0], previous.read_bytes()
                journal = manager._get_status_journal_locked() if storage == 'journal' else None
                if journal:
                    journal.ensure_writable()
                    inode, original = journal.path.stat().st_ino, os.fsync
                    def fail_after_sync(fd):
                        original(fd)
                        if os.fstat(fd).st_ino == inode:
                            raise OSError('ambiguous journal fsync failure')
                    failure = patch('os.fsync', side_effect=fail_after_sync)
                else:
                    original = manager._save_chunks_locked
                    def fail_after_save(*args, **kwargs):
                        original(*args, **kwargs)
                        raise OSError('ambiguous snapshot save failure')
                    failure = patch.object(manager, '_save_chunks_locked', side_effect=fail_after_save)
                with self.get_codec_context('wav'), failure:
                    with self.assertRaisesRegex(OSError, 'ambiguous'):
                        manager._publish_generated_chunk_audio(before, str(fresh), 'NARRATOR')
                saved = manager.load_chunks()[0]
                self.assertEqual(saved, ProjectManager(tmp).load_chunks()[0])
                self.assertNotEqual(saved['audio_revision'], before['audio_revision'])
                self.assertEqual((root / saved['audio_path']).read_bytes(), fresh.read_bytes())
                self.assertEqual(previous.read_bytes(), old_bytes)
                committed_path = root / saved['audio_path']
                committed_bytes = committed_path.read_bytes()
                sf.write(fresh, .2 * np.sin(2 * np.pi * 440 * np.arange(12000) / 24000), 24000)
                with self.get_codec_context('wav'):
                    next_path = manager._publish_generated_chunk_audio(saved, str(fresh), 'NARRATOR')
                self.assertNotEqual(next_path, saved['audio_path'])
                self.assertEqual((root / next_path).read_bytes(), fresh.read_bytes())
                self.assertEqual(committed_path.read_bytes(), committed_bytes)

    def test_successive_renders_bound_slots_across_speaker_names_and_restarts(self):
        with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(io.StringIO()):
            root = Path(tmp)
            manager, previous, fresh = self.create_project(root)
            previous_bytes, paths = previous.read_bytes(), set()
            for index in range(8):
                manager = ProjectManager(tmp)
                with self.get_codec_context('wav'):
                    path = manager._publish_generated_chunk_audio(
                        manager.load_chunks()[0], str(fresh), 'speaker-' + str(index))
                paths.add(path)
                self.assertEqual((root / path).read_bytes(), fresh.read_bytes())
            self.assertEqual(len(paths), 2)
            self.assertEqual(len(list((root / 'voicelines').glob('*.slot-*'))), 2)
            self.assertEqual(previous.read_bytes(), previous_bytes)

    def test_shared_live_paths_block_slot_replacement_in_snapshot_and_journal(self):
        for storage in ('snapshot', 'journal'):
            with self.subTest(storage=storage), tempfile.TemporaryDirectory() as tmp, \
                    contextlib.redirect_stdout(io.StringIO()):
                root = Path(tmp)
                manager, previous, fresh = self.create_project(root)
                with self.get_codec_context('wav'):
                    first = manager._publish_generated_chunk_audio(manager.load_chunks()[0], str(fresh), 'NARRATOR')
                    rows = manager.load_chunks()
                    manager.save_chunks(rows + [dict(rows[0], uid='other', id=1, status='pending',
                        audio_path=first.replace('voicelines/', 'voicelines/./'))])
                    if storage == 'journal':
                        manager._get_status_journal_locked().ensure_writable()
                    second = manager._publish_generated_chunk_audio(rows[0], str(fresh), 'NARRATOR')
                    self.assertNotEqual(first, second)
                    saved = manager.load_chunks()
                    first_bytes, second_bytes = (root / first).read_bytes(), (root / second).read_bytes()
                    with self.assertRaisesRegex(ValueError, 'slots are referenced'):
                        manager._publish_generated_chunk_audio(saved[0], str(fresh), 'NARRATOR')
                    self.assertEqual(saved, manager.load_chunks())
                    self.assertEqual(first_bytes, (root / first).read_bytes())
                    self.assertEqual(second_bytes, (root / second).read_bytes())
                    manager._update_chunk_fields_by_uid('other', audio_path=None)
                    self.assertEqual(first, manager._publish_generated_chunk_audio(saved[0], str(fresh), 'NARRATOR'))

    def test_interruption_after_install_before_metadata_preserves_prior_and_bounds_retry(self):
        code = '''import os, shutil, sys
from pathlib import Path
import project
root=Path(sys.argv[1]);m=project.ProjectManager(str(root))
def export(source,name):
 relative='voicelines/'+name+'.wav';shutil.copyfile(source,root/relative);return relative
m._export_chunk_audio=export
publish=project.publish_audio_output
def stop_after_install(*args):
 publish(*args);os._exit(93)
project.publish_audio_output=stop_after_install
m._publish_generated_chunk_audio(m.load_chunks()[0],str(root/'fresh.wav'),'NARRATOR')
'''
        for storage in ('snapshot', 'journal'):
            for prior in (False, True):
                with self.subTest(storage=storage, prior=prior), tempfile.TemporaryDirectory() as tmp, \
                        contextlib.redirect_stdout(io.StringIO()):
                    root = Path(tmp)
                    manager, previous, fresh = self.create_project(root, prior)
                    before = manager.load_chunks()
                    previous_bytes = previous.read_bytes() if prior else None
                    if storage == 'journal':
                        manager._get_status_journal_locked().ensure_writable()
                    for attempt in range(3):
                        child = subprocess.run([sys.executable, '-c', code, tmp], capture_output=True, text=True)
                        self.assertEqual(child.returncode, 93, child.stderr)
                        self.assertEqual(before, ProjectManager(tmp).load_chunks())
                        self.assertEqual(previous_bytes, previous.read_bytes() if prior else None)
                        self.assertEqual(len(list((root / 'voicelines').glob('*.slot-*'))), 1)
                        self.assertEqual([], list((root / 'voicelines').glob('.render-*')))
                    with self.get_codec_context('wav'):
                        path = ProjectManager(tmp)._publish_generated_chunk_audio(before[0], str(fresh), 'NARRATOR')
                    self.assertEqual(fresh.read_bytes(), (root / path).read_bytes())

    def test_audio_sync_failure_leaves_metadata_and_previous_take_unchanged(self):
        for target in ('os.fsync', 'project.sync_adapter_directory'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp, \
                    contextlib.redirect_stdout(io.StringIO()):
                root = Path(tmp)
                manager, previous, fresh = self.create_project(root)
                before, previous_bytes = manager.load_chunks(), previous.read_bytes()
                def export(source, name):
                    path = 'voicelines/' + name + '.wav'
                    shutil.copyfile(source, root / path)
                    return path
                with patch.object(manager, '_export_chunk_audio', side_effect=export), \
                        patch(target, side_effect=OSError('injected audio sync failure')):
                    with self.assertRaisesRegex(OSError, 'audio sync failure'):
                        manager._publish_generated_chunk_audio(before[0], str(fresh), 'NARRATOR')
                self.assertEqual(before, manager.load_chunks())
                self.assertEqual(previous_bytes, previous.read_bytes())
                self.assertEqual([], list((root / 'voicelines').glob('.render-*')))

    def test_single_parallel_and_native_callers_preserve_prior_on_save_failure(self):
        for mode in ('single', 'parallel', 'native'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp, \
                    contextlib.redirect_stdout(io.StringIO()):
                root = Path(tmp)
                manager, previous, fresh = self.create_project(root)
                prior_bytes = previous.read_bytes()
                def voice(text, instruct, speaker, config, output):
                    shutil.copyfile(fresh, output)
                    return True
                def batch(chunks, config, output, seed):
                    for chunk in chunks:
                        shutil.copyfile(fresh, Path(output) / f"temp_batch_{chunk['index']}.wav")
                    return {'completed': [chunk['index'] for chunk in chunks], 'failed': []}
                manager.engine = SimpleNamespace(generate_voice=voice, generate_batch=batch)
                save = manager._update_chunk_fields_by_uid
                def fail_publication(*args, **kwargs):
                    if kwargs.get('audio_source'):
                        journal = manager._get_status_journal_locked() if mode == 'parallel' else None
                        owner, method = (journal, '_append') if journal else (manager, '_save_chunks_locked')
                        with patch.object(owner, method, side_effect=OSError('injected metadata failure')):
                            return save(*args, **kwargs)
                    return save(*args, **kwargs)
                with self.get_codec_context('wav'), patch.object(
                        manager, '_update_chunk_fields_by_uid', side_effect=fail_publication):
                    if mode == 'single':
                        success, message = manager.generate_chunk_audio(0)
                        self.assertFalse(success)
                    else:
                        result = (manager.generate_chunks_parallel([0], max_workers=1) if mode == 'parallel'
                                  else manager.generate_chunks_batch([0], batch_size=1))
                        self.assertEqual(result['completed'], [])
                        message = result['failed'][0][1]
                self.assertIn('metadata failure', message)
                self.assertEqual(previous.read_bytes(), prior_bytes)
                saved = ProjectManager(tmp).load_chunks()[0]
                self.assertEqual(saved['audio_path'], 'voicelines/' + previous.name)
                self.assertEqual(saved['audio_revision'], 'approved')
                self.assertEqual([], list((root / 'voicelines').glob('.render-*')))
