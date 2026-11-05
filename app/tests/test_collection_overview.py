"""Collection overview projects durable saved-book state without switching books."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from collection_overview import get_collection_overview
from completed_review_receipt import (get_review_entries_fingerprint,
                                      save_completed_review_receipt)
from project import ProjectManager
from routers import scripts_library
from routers.voices import get_voice_rows

ENTRIES = [{'type': 'dialogue', 'speaker': 'ALICE', 'text': 'Synthetic line.'}]
READY_VOICES = {'ALICE': {'type': 'custom', 'description': 'Synthetic voice'}}
PENDING_VOICES = {'ALICE': {'type': 'custom'}}


class CollectionOverviewTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.library = self.root / 'scripts'
        self.library.mkdir()
        self.manager = ProjectManager(str(self.root))

    def save_book(self, name, entries=ENTRIES, voices=READY_VOICES, book_id=None):
        path = self.library / f'{name}.json'
        path.write_text(json.dumps(entries))
        (self.library / f'{name}.meta.json').write_text(json.dumps({'book_id': book_id or name}))
        if voices is not None:
            (self.library / f'{name}.voice_config.json').write_text(json.dumps(voices))
        return path

    def complete_review(self, path, entries=ENTRIES):
        digest = get_review_entries_fingerprint(entries)
        save_completed_review_receipt(str(path), str(path), digest, digest, 'synthetic-settings')

    def overview(self, names, **kwargs):
        return get_collection_overview(str(self.library), names, get_voice_rows, **kwargs)

    def files(self):
        return {str(p.relative_to(self.root)): p.read_bytes()
                for p in self.root.rglob('*') if p.is_file() and not p.name.endswith('.lock')}

    def test_mixed_stage_collection_explains_each_stage(self):
        ready = self.save_book('Ready')
        self.complete_review(ready)
        self.save_book('Unreviewed')
        pending = self.save_book('PendingVoices', voices=PENDING_VOICES)
        self.complete_review(pending)
        failed = self.save_book('FailedRun')
        (self.library / 'FailedRun.json.threepass_manifest.json').write_text(json.dumps({'status': 'failed'}))
        incomplete = self.save_book('IncompleteRun')
        (self.library / 'IncompleteRun.json.generation_checkpoint.json').write_text(json.dumps({'fingerprint': 'x'}))
        before = self.files()
        rows = {row['name']: row for row in self.overview(
            ['Ready', 'Unreviewed', 'PendingVoices', 'FailedRun', 'IncompleteRun'])}
        self.assertEqual(before, self.files())
        # Recorded receipts satisfy planning readiness (never complete:
        # current settings were not rechecked in this read-only view).
        self.assertEqual('recorded', rows['Ready']['review']['status'])
        self.assertEqual('not_rechecked', rows['Ready']['review']['current_settings'])
        self.assertEqual('ready', rows['Ready']['voices']['status'])
        # Saved books carry no chunk/export binding: never inferred from
        # global output files or session state. Render stays unknown
        # while overall reaches ready_for_audio (navigation guidance).
        self.assertEqual('unknown', rows['Ready']['render']['status'])
        self.assertEqual('ready_for_audio', rows['Ready']['overall'])
        self.assertEqual('needs_review', rows['Unreviewed']['overall'])
        self.assertEqual('not_started', rows['Unreviewed']['review']['status'])
        # Recorded review unblocks voices (per-stage progress visible).
        self.assertEqual('needs_voices', rows['PendingVoices']['overall'])
        self.assertEqual('needs_decisions', rows['PendingVoices']['voices']['status'])
        self.assertEqual(1, rows['PendingVoices']['voices']['pending'])
        self.assertEqual('failed', rows['FailedRun']['overall'])
        self.assertEqual('incomplete', rows['IncompleteRun']['overall'])
        self.assertEqual('incomplete', rows['IncompleteRun']['script']['status'])
        for row in rows.values():
            self.assertRegex(row['revision'], r'^[0-9a-f]{64}$')
        for row in rows.values():
            self.assertTrue(row['script']['evidence'])
            self.assertTrue(row['next']['reason'])
            self.assertRegex(row['script']['script_sha256'], r'^[0-9a-f]{64}$')

    def test_source_edit_invalidates_review_receipt(self):
        path = self.save_book('Edited')
        self.complete_review(path)
        self.assertEqual('recorded', self.overview(['Edited'])[0]['review']['status'])
        path.write_text(json.dumps([{**ENTRIES[0], 'text': 'Edited synthetic line.'}]))
        row = self.overview(['Edited'])[0]
        self.assertEqual('stale', row['review']['status'])
        self.assertIn('edited', row['review']['evidence'])
        self.assertEqual('needs_review', row['overall'])

    def test_review_restart_after_stale_receipt_completes_again(self):
        path = self.save_book('Restarted')
        self.complete_review(path)
        edited = [{**ENTRIES[0], 'text': 'Revised synthetic line.'}]
        path.write_text(json.dumps(edited))
        self.assertEqual('stale', self.overview(['Restarted'])[0]['review']['status'])
        self.complete_review(path, edited)
        self.assertEqual('recorded', self.overview(['Restarted'])[0]['review']['status'])

    def test_rename_preserves_book_identity_and_revision(self):
        old = self.save_book('OldName', book_id='StableBook')
        self.complete_review(old)
        before = self.overview(['OldName'])[0]
        for suffix in ('.json', '.meta.json', '.voice_config.json', '.json.review_completed.json'):
            (self.library / f'OldName{suffix}').rename(self.library / f'NewName{suffix}')
        after = self.overview(['NewName'])[0]
        self.assertEqual(before['book_id'], after['book_id'])
        self.assertEqual(before['script']['script_sha256'], after['script']['script_sha256'])
        # The receipt names the old absolute path, so it cannot certify the
        # moved file as reviewed.
        self.assertEqual('stale', after['review']['status'])

    def test_missing_book_is_unknown_not_completed(self):
        row = self.overview(['Absent'])[0]
        self.assertEqual('missing', row['script']['status'])
        self.assertEqual('unknown', row['overall'])

    def test_corrupt_saved_script_fails_loud_without_writing(self):
        path = self.save_book('Corrupt')
        path.write_text('{')
        before = self.files()
        with self.assertRaises(ValueError):
            self.overview(['Corrupt'])
        self.assertEqual(before, self.files())

    def _activate(self, name, path):
        (self.root / 'state.json').write_text(json.dumps({
            'active_book_id': name, 'book_generation': 'one',
            'input_file_path': str(path)}))
        (self.root / 'annotated_script.json').write_bytes(path.read_bytes())
        (self.root / 'voice_config.json').write_text(json.dumps(READY_VOICES))

    def _workspace(self):
        from book_workspace import get_book_workspace
        return get_book_workspace(str(self.root), {'MP3': self.root / 'cloned_audiobook.mp3',
                                                   'M4B': self.root / 'audiobook.m4b'},
                                  get_voice_rows, self.manager.get_chunk_audio_path,
                                  export_states=self._states)

    def test_active_cancel_restart_and_replaced_export(self):
        from book_state_transaction import get_book_operation_token
        from chunk_status_journal import ChunkStatusJournal
        from export_quality import get_export_file_version
        from export_quality_session import ExportQualityAttempt, TASKS
        path = self.save_book('ActiveBook')
        self.complete_review(path)
        self._activate('ActiveBook', path)
        (self.root / 'one.wav').write_bytes(b'synthetic audio')
        (self.root / 'chunks.json').write_text(json.dumps(
            [{'uid': 'one', 'id': 0, 'status': 'done', 'text': 'Synthetic line.',
              'speaker': 'ALICE', 'instruct': '', 'audio_revision': 'take',
              'audio_path': 'one.wav'}]))
        journal = ChunkStatusJournal(self.root / 'chunks.json',
                                      {'active_book_id': 'ActiveBook', 'book_generation': 'one'})
        journal.apply_update('one', {'status': 'done', 'audio_path': 'one.wav'})
        output = self.root / 'cloned_audiobook.mp3'
        output.write_bytes(b'synthetic completed export')
        self._states = {name: {} for name in TASKS}
        token = get_book_operation_token(str(self.root))

        def overview():
            return self.overview(['ActiveBook'], active_workspace=self._workspace(),
                                 active_book_id='ActiveBook')[0]

        row = overview()
        self.assertTrue(row['is_active'])
        # A present output file alone is not a completed export.
        self.assertEqual('export_needed', row['render']['status'])

        attempt = ExportQualityAttempt(self._states['audio'], token)
        attempt.finish(False, 'Export cancelled')
        row = overview()
        self.assertEqual('cancelled', row['render']['status'])
        self.assertEqual('cancelled', row['overall'])

        attempt = ExportQualityAttempt(self._states['audio'], token)
        attempt.publish(dict(status='verified', file='cloned_audiobook.mp3',
                             file_version=get_export_file_version(output)))
        attempt.finish(True, 'done')
        row = overview()
        # Unbound session evidence stays recorded, never complete.
        self.assertEqual('recorded', row['render']['status'])
        self.assertEqual('export_recorded', row['overall'])
        self.assertNotEqual('completed', row['overall'])

        output.write_bytes(b'replaced output with a different size')
        row = overview()
        self.assertNotEqual('recorded', row['render']['status'])
        self.assertEqual('export_needed', row['render']['status'])

    def test_route_is_read_only_and_conflicts_on_corrupt_book(self):
        good = self.save_book('Good')
        self.complete_review(good)
        bad = self.save_book('Bad')
        bad.write_text('{')
        application = FastAPI()
        application.include_router(scripts_library.router)
        before = self.files()
        with contextlib.ExitStack() as stack:
            for key, value in [('DATA_DIR', str(self.root)), ('SCRIPTS_DIR', str(self.library)),
                               ('AUDIOBOOK_PATH', str(self.root / 'out.mp3')),
                               ('M4B_PATH', str(self.root / 'out.m4b'))]:
                stack.enter_context(patch.object(scripts_library, key, value))
            stack.enter_context(patch('core.project_manager', self.manager))
            with TestClient(application) as client:
                response = client.post('/api/collection/overview', json={'names': ['Good']})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertEqual('recorded', response.json()[0]['review']['status'])
                self.assertEqual(409, client.post(
                    '/api/collection/overview', json={'names': ['Bad']}).status_code)
                self.assertEqual(400, client.post(
                    '/api/collection/overview', json={'names': ['../escape']}).status_code)
        self.assertEqual(before, self.files())


class CollectionOverviewEvidenceTests(unittest.TestCase):
    """Substantial repairs: bounded reads, fencing, chronology, proof."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.library = self.root / 'scripts'
        self.library.mkdir()
        self.manager = ProjectManager(str(self.root))
        self._states = None

    def save_book(self, name, entries=ENTRIES, voices=READY_VOICES, book_id=None):
        path = self.library / f'{name}.json'
        path.write_text(json.dumps(entries))
        (self.library / f'{name}.meta.json').write_text(json.dumps({'book_id': book_id or name}))
        if voices is not None:
            (self.library / f'{name}.voice_config.json').write_text(json.dumps(voices))
        return path

    def complete_review(self, path, entries=ENTRIES):
        digest = get_review_entries_fingerprint(entries)
        save_completed_review_receipt(str(path), str(path), digest, digest, 'synthetic-settings')

    def overview(self, names, **kwargs):
        return get_collection_overview(str(self.library), names, get_voice_rows, **kwargs)

    def active_hashes(self, name):
        import hashlib
        script = hashlib.sha256((self.library / f'{name}.json').read_bytes()).hexdigest()
        voice_path = self.library / f'{name}.voice_config.json'
        voice = hashlib.sha256(voice_path.read_bytes()).hexdigest() if voice_path.exists() else None
        return script, voice

    def test_same_book_id_only_named_row_attaches(self):
        self.save_book('A', book_id='Shared')
        self.save_book('B', book_id='Shared')
        # Activate B: DATA_DIR bytes identical to B's saved bytes.
        (self.root / 'state.json').write_text(json.dumps({
            'active_book_id': 'Shared', 'book_generation': 'one',
            'input_file_path': str(self.library / 'B.json')}))
        (self.root / 'annotated_script.json').write_bytes((self.library / 'B.json').read_bytes())
        (self.root / 'voice_config.json').write_text(json.dumps(READY_VOICES))
        from book_workspace import get_book_workspace
        from export_quality_session import TASKS
        self._states = {name: {} for name in TASKS}
        workspace = get_book_workspace(str(self.root), {'MP3': self.root / 'out.mp3'},
                                       get_voice_rows, self.manager.get_chunk_audio_path,
                                       export_states=self._states)
        script_hash, voice_hash = self.active_hashes('B')
        rows = {row['name']: row for row in self.overview(
            ['A', 'B'], active_workspace=workspace, active_saved_name='B',
            active_script_sha256=script_hash, active_voice_sha256=voice_hash)}
        self.assertFalse(rows['A']['is_active'])
        self.assertEqual('unknown', rows['A']['render']['status'])
        self.assertTrue(rows['B']['is_active'])
        self.assertNotEqual('unknown', rows['B']['render']['status'])

    def test_active_bytes_mismatch_fences_render(self):
        path = self.save_book('ActiveBook')
        self.complete_review(path)
        (self.root / 'state.json').write_text(json.dumps({
            'active_book_id': 'ActiveBook', 'book_generation': 'one',
            'input_file_path': str(path)}))
        (self.root / 'annotated_script.json').write_bytes(path.read_bytes())
        (self.root / 'voice_config.json').write_text(json.dumps(READY_VOICES))
        from book_workspace import get_book_workspace
        from export_quality_session import TASKS
        self._states = {name: {} for name in TASKS}
        workspace = get_book_workspace(str(self.root), {'MP3': self.root / 'out.mp3'},
                                       get_voice_rows, self.manager.get_chunk_audio_path,
                                       export_states=self._states)
        script_hash, voice_hash = self.active_hashes('ActiveBook')
        # Mutate the active copy after the snapshot was taken.
        (self.root / 'annotated_script.json').write_text(
            json.dumps([{**ENTRIES[0], 'text': 'Mutated active copy.'}]))
        import hashlib
        mutated = hashlib.sha256((self.root / 'annotated_script.json').read_bytes()).hexdigest()
        row = self.overview(['ActiveBook'], active_workspace=workspace,
                            active_saved_name='ActiveBook',
                            active_script_sha256=mutated,
                            active_voice_sha256=voice_hash)[0]
        self.assertTrue(row['is_active'])
        self.assertEqual('unknown', row['render']['status'])
        self.assertIn('differs', row['render']['evidence'])
        # Matching bytes attach normally.
        row = self.overview(['ActiveBook'], active_workspace=workspace,
                            active_saved_name='ActiveBook',
                            active_script_sha256=script_hash,
                            active_voice_sha256=voice_hash)[0]
        self.assertNotEqual('unknown', row['render']['status'])

    def test_corrupt_manifest_and_voice_are_unavailable(self):
        bad_manifest = self.save_book('BadManifest')
        (self.library / 'BadManifest.json.threepass_manifest.json').write_text('{')
        row = self.overview(['BadManifest'])[0]
        self.assertEqual('unavailable', row['script']['status'])
        self.assertEqual('unknown', row['overall'])
        for name in ('CorruptVoice', 'NullVoice', 'ArrayVoice'):
            self.save_book(name)
        (self.library / 'CorruptVoice.voice_config.json').write_text('{')
        (self.library / 'NullVoice.voice_config.json').write_text('null')
        (self.library / 'ArrayVoice.voice_config.json').write_text('[]')
        rows = {row['name']: row for row in self.overview(
            ['CorruptVoice', 'NullVoice', 'ArrayVoice'])}
        for row in rows.values():
            self.assertEqual('unavailable', row['voices']['status'])
            self.assertEqual('unknown', row['overall'])
        # Meta book_id wrong type must not 500.
        self.save_book('BadMeta')
        (self.library / 'BadMeta.meta.json').write_text(json.dumps({'book_id': 123}))
        row = self.overview(['BadMeta'])[0]
        self.assertEqual('unavailable', row['meta']['status'])
        self.assertEqual('BadMeta', row['book_id'])
        # Voice value wrong shape must not 500.
        self.save_book('StrVoice')
        (self.library / 'StrVoice.voice_config.json').write_text(json.dumps({'ALICE': 'x'}))
        row = self.overview(['StrVoice'])[0]
        self.assertEqual('unavailable', row['voices']['status'])

    def test_stem_jsonl_shard_and_per_book_isolation(self):
        self.save_book('StemGen')
        (self.library / 'StemGen.generation_checkpoint.json').write_text(json.dumps({'fingerprint': 'x'}))
        self.save_book('StemManifest')
        (self.library / 'StemManifest.threepass_manifest.json').write_text(json.dumps({'status': 'failed'}))
        self.save_book('JsonlBook')
        (self.library / 'JsonlBook.checkpoint.jsonl').write_text('{"x": 1}\n')
        rows = {row['name']: row for row in self.overview(
            ['StemGen', 'StemManifest', 'JsonlBook'])}
        self.assertEqual('incomplete', rows['StemGen']['script']['status'])
        self.assertEqual('failed', rows['StemManifest']['script']['status'])
        self.assertEqual('incomplete', rows['JsonlBook']['script']['status'])
        # Valid UUID shard epoch + header.
        self.save_book('Sharded')
        header = self.library / 'Sharded.json.generation_checkpoint.json'
        header.write_text(json.dumps({'fingerprint': 'x'}))
        (self.library / 'Sharded.json.generation_checkpoint.json.parts-aabbccddeeff00112233445566778899').mkdir()
        row = self.overview(['Sharded'])[0]
        self.assertEqual('incomplete', row['script']['status'])
        # Invalid ownership (regular file with shard name) is per-book.
        self.save_book('Good')
        self.save_book('BadShard')
        (self.library / 'BadShard.json.generation_checkpoint.json.parts-11223344556677889900112233445566').write_text('stray')
        rows = {row['name']: row for row in self.overview(['Good', 'BadShard'])}
        self.assertEqual('unavailable', rows['BadShard']['script']['status'])
        self.assertNotEqual('unavailable', rows['Good']['script']['status'])

    def test_empty_script_retains_failed_evidence(self):
        for name, suffix, content, status in (
                ('EmptyFailed', '.threepass_manifest.json', json.dumps({'status': 'failed'}), 'failed'),
                ('EmptyCorrupt', '.threepass_manifest.json', '{', 'unavailable'),
                ('EmptyCkpt', '.generation_checkpoint.json', json.dumps({'fingerprint': 'x'}), 'incomplete')):
            path = self.library / f'{name}.json'
            path.write_text('[]')
            (self.library / f'{name}.meta.json').write_text(json.dumps({'book_id': name}))
            (self.library / f'{name}.voice_config.json').write_text(json.dumps(READY_VOICES))
            (self.library / f'{name}.json{suffix}').write_text(content)
            row = self.overview([name])[0]
            self.assertEqual(status, row['script']['status'], name)
        self.save_book('EmptyPlain', entries=[])
        self.assertEqual('not_started', self.overview(['EmptyPlain'])[0]['script']['status'])

    def test_receipt_bool_version_rejected(self):
        path = self.save_book('BoolVersion')
        digest = get_review_entries_fingerprint(ENTRIES)
        save_completed_review_receipt(str(path), str(path), digest, digest, 'synthetic-settings')
        receipt_path = self.library / 'BoolVersion.json.review_completed.json'
        receipt = json.loads(receipt_path.read_text())
        receipt['version'] = True
        receipt_path.write_text(json.dumps(receipt))
        row = self.overview(['BoolVersion'])[0]
        self.assertEqual('stale', row['review']['status'])

    def test_binding_mismatch_is_unavailable(self):
        from script_selection_binding import make_script_selection_binding
        path = self.save_book('Bound')
        raw = path.read_bytes()
        binding = make_script_selection_binding('a' * 64, 'b' * 64, raw,
                                                source_path=str(path))
        (self.library / 'Bound.selection_binding.json').write_text(
            json.dumps(binding, sort_keys=True, separators=(',', ':')))
        row = self.overview(['Bound'])[0]
        self.assertNotEqual('unavailable', row['script']['status'])
        edited = [{**ENTRIES[0], 'text': 'Drifted without rebinding.'}]
        path.write_text(json.dumps(edited))
        row = self.overview(['Bound'])[0]
        self.assertEqual('unavailable', row['script']['status'])
        self.assertIn('binding', row['script']['evidence'])
        # Missing binding stays loadable.
        path.write_text(json.dumps(ENTRIES))
        (self.library / 'Bound.selection_binding.json').unlink()
        self.assertNotEqual('unavailable', self.overview(['Bound'])[0]['script']['status'])

    def test_partial_and_unavailable_audio(self):
        from book_state_transaction import get_book_operation_token
        from chunk_status_journal import ChunkStatusJournal
        from export_quality_session import TASKS
        path = self.save_book('ActiveAudio')
        self.complete_review(path)
        (self.root / 'state.json').write_text(json.dumps({
            'active_book_id': 'ActiveAudio', 'book_generation': 'one',
            'input_file_path': str(path)}))
        (self.root / 'annotated_script.json').write_bytes(path.read_bytes())
        (self.root / 'voice_config.json').write_text(json.dumps(READY_VOICES))
        (self.root / 'one.wav').write_bytes(b'synthetic audio')
        (self.root / 'chunks.json').write_text(json.dumps([
            {'uid': 'one', 'id': 0, 'status': 'done', 'text': 'Synthetic line.',
             'speaker': 'ALICE', 'instruct': '', 'audio_revision': 'take',
             'audio_path': 'one.wav'},
            {'uid': 'two', 'id': 1, 'status': 'done', 'text': 'Synthetic line.',
             'speaker': 'ALICE', 'instruct': '', 'audio_revision': 'take',
             'audio_path': 'missing.wav'}]))
        journal = ChunkStatusJournal(self.root / 'chunks.json',
                                      {'active_book_id': 'ActiveAudio', 'book_generation': 'one'})
        journal.apply_update('one', {'status': 'done', 'audio_path': 'one.wav'})
        journal.apply_update('two', {'status': 'done', 'audio_path': 'missing.wav'})
        from book_workspace import get_book_workspace
        self._states = {name: {} for name in TASKS}
        workspace = get_book_workspace(str(self.root), {'MP3': self.root / 'out.mp3'},
                                       get_voice_rows, self.manager.get_chunk_audio_path,
                                       export_states=self._states)
        script_hash, voice_hash = self.active_hashes('ActiveAudio')
        row = self.overview(['ActiveAudio'], active_workspace=workspace,
                            active_saved_name='ActiveAudio',
                            active_script_sha256=script_hash,
                            active_voice_sha256=voice_hash)[0]
        # Partial audio outranks the unverified review (Editor).
        self.assertEqual('incomplete', row['render']['status'])
        # Unavailable audio is never ready_for_audio.
        (self.root / 'chunks.json').write_text('{')
        workspace = get_book_workspace(str(self.root), {'MP3': self.root / 'out.mp3'},
                                       get_voice_rows, self.manager.get_chunk_audio_path,
                                       export_states=self._states)
        row = self.overview(['ActiveAudio'], active_workspace=workspace,
                            active_saved_name='ActiveAudio',
                            active_script_sha256=script_hash,
                            active_voice_sha256=voice_hash)[0]
        self.assertEqual('unavailable', row['render']['status'])
        self.assertNotEqual('ready_for_audio', row['overall'])

    def test_export_chronology_running_and_format_scope(self):
        from book_state_transaction import get_book_operation_token
        from export_quality import get_export_file_version
        from export_quality_session import ExportQualityAttempt, TASKS
        path = self.save_book('ActiveExport')
        self.complete_review(path)
        (self.root / 'state.json').write_text(json.dumps({
            'active_book_id': 'ActiveExport', 'book_generation': 'one',
            'input_file_path': str(path)}))
        (self.root / 'annotated_script.json').write_bytes(path.read_bytes())
        (self.root / 'voice_config.json').write_text(json.dumps(READY_VOICES))
        (self.root / 'one.wav').write_bytes(b'synthetic audio')
        (self.root / 'chunks.json').write_text(json.dumps(
            [{'uid': 'one', 'id': 0, 'status': 'done', 'text': 'Synthetic line.',
              'speaker': 'ALICE', 'instruct': '', 'audio_revision': 'take',
              'audio_path': 'one.wav'}]))
        journal = __import__('chunk_status_journal', fromlist=['ChunkStatusJournal']).ChunkStatusJournal(
            self.root / 'chunks.json',
            {'active_book_id': 'ActiveExport', 'book_generation': 'one'})
        journal.apply_update('one', {'status': 'done', 'audio_path': 'one.wav'})
        output = self.root / 'out.mp3'
        output.write_bytes(b'synthetic completed export')
        self._states = {name: {} for name in TASKS}
        token = get_book_operation_token(str(self.root))

        def overview_row():
            from book_workspace import get_book_workspace
            workspace = get_book_workspace(str(self.root), {'MP3': output, 'M4B': self.root / 'out.m4b'},
                                           get_voice_rows, self.manager.get_chunk_audio_path,
                                           export_states=self._states)
            script_hash, voice_hash = self.active_hashes('ActiveExport')
            return self.overview(['ActiveExport'], active_workspace=workspace,
                                 active_saved_name='ActiveExport',
                                 active_script_sha256=script_hash,
                                 active_voice_sha256=voice_hash)[0]

        attempt = ExportQualityAttempt(self._states['audio'], token)
        attempt.finish(False, 'Export cancelled')
        self.assertEqual('cancelled', overview_row()['render']['status'])
        later = ExportQualityAttempt(self._states['m4b_export'], token)
        later.finish(False, 'boom')
        cancelled_at = self._states['audio']['export_quality_attempt']['started_at']
        self._states['m4b_export']['export_quality_attempt']['started_at'] = cancelled_at + 10
        self.assertEqual('failed', overview_row()['render']['status'])
        running = ExportQualityAttempt(self._states['audio'], token)
        self._states['audio']['export_quality_attempt']['started_at'] = cancelled_at + 20
        self.assertEqual('in_progress', overview_row()['render']['status'])
        # Verified receipt cannot hide the newer failed attempt above;
        # clear attempts and verify audacity-only scope.
        self._states = {name: {} for name in TASKS}
        attempt = ExportQualityAttempt(self._states['audacity_export'], token)
        attempt.publish(dict(status='verified', file='project.zip',
                             file_version=get_export_file_version(self.root / 'chunks.json')))
        attempt.finish(True, 'done')
        # Only audacity verified; MP3/M4B outputs stay export_needed.
        self.assertEqual('export_needed', overview_row()['render']['status'])

    def test_deep_nesting_and_wrong_shapes_are_per_book(self):
        self.save_book('GoodDeep')
        self.save_book('DeepMeta')
        deep = '[' * 3000 + ']' * 3000
        (self.library / 'DeepMeta.meta.json').write_text(deep)
        rows = {row['name']: row for row in self.overview(['GoodDeep', 'DeepMeta'])}
        self.assertEqual('unavailable', rows['DeepMeta']['meta']['status'])
        self.assertNotEqual('unavailable', rows['GoodDeep']['script']['status'])
        self.save_book('DeepVoice')
        (self.library / 'DeepVoice.voice_config.json').write_text('{"x": ' + deep + '}')
        rows = {row['name']: row for row in self.overview(['GoodDeep', 'DeepVoice'])}
        self.assertEqual('unavailable', rows['DeepVoice']['voices']['status'])
        self.assertNotEqual('unavailable', rows['GoodDeep']['script']['status'])

    def test_bounded_capture_rejects_oversize_without_huge_alloc(self):
        import collection_overview
        self.save_book('Oversize')
        with patch.object(collection_overview, 'MAX_SCRIPT_BYTES', 10):
            with self.assertRaises(ValueError):
                self.overview(['Oversize'])

    def test_real_active_route_and_load_proof(self):
        from collection_overview import compute_saved_book_revision
        path = self.save_book('Routed')
        self.complete_review(path)
        (self.root / 'state.json').write_text(json.dumps({
            'active_book_id': 'Routed', 'book_generation': 'one',
            'input_file_path': str(path)}))
        (self.root / 'annotated_script.json').write_bytes(path.read_bytes())
        (self.root / 'voice_config.json').write_text(json.dumps(READY_VOICES))
        application = FastAPI()
        application.include_router(scripts_library.router)
        with contextlib.ExitStack() as stack:
            for key, value in [('DATA_DIR', str(self.root)), ('SCRIPTS_DIR', str(self.library)),
                               ('AUDIOBOOK_PATH', str(self.root / 'out.mp3')),
                               ('M4B_PATH', str(self.root / 'out.m4b')),
                               ('SCRIPT_PATH', str(self.root / 'annotated_script.json')),
                               ('VOICE_CONFIG_PATH', str(self.root / 'voice_config.json')),
                               ('CHUNKS_PATH', str(self.root / 'chunks.json'))]:
                stack.enter_context(patch.object(scripts_library, key, value))
            stack.enter_context(patch('core.project_manager', self.manager))
            with TestClient(application) as client:
                response = client.post('/api/collection/overview', json={'names': ['Routed']})
                self.assertEqual(response.status_code, 200, response.text)
                self.assertTrue(response.json()[0]['is_active'])
                revision = response.json()[0]['revision']
                # Stale proof is rejected before any active mutation.
                before_script = (self.root / 'annotated_script.json').read_bytes()
                stale = client.post('/api/scripts/load',
                                    json={'name': 'Routed', 'expected_revision': '0' * 64})
                self.assertEqual(409, stale.status_code)
                self.assertEqual(before_script, (self.root / 'annotated_script.json').read_bytes())
                # Correct proof loads.
                ok = client.post('/api/scripts/load',
                                 json={'name': 'Routed', 'expected_revision': revision})
                self.assertEqual(ok.status_code, 200, ok.text)
                self.assertEqual(path.read_bytes(), (self.root / 'annotated_script.json').read_bytes())
                # Ordinary load without proof stays valid.
                again = client.post('/api/scripts/load', json={'name': 'Routed'})
                self.assertEqual(again.status_code, 200, again.text)


class CollectionOverviewCompletionTests(unittest.TestCase):
    """Bounded completion layer: recorded review, bound export, load proof."""

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.library = self.root / 'scripts'
        self.library.mkdir()
        self.manager = ProjectManager(str(self.root))
        self._states = None

    def save_book(self, name, entries=ENTRIES, voices=READY_VOICES, book_id=None):
        path = self.library / f'{name}.json'
        path.write_text(json.dumps(entries))
        (self.library / f'{name}.meta.json').write_text(json.dumps({'book_id': book_id or name}))
        if voices is not None:
            (self.library / f'{name}.voice_config.json').write_text(json.dumps(voices))
        return path

    def complete_review(self, path, entries=ENTRIES):
        digest = get_review_entries_fingerprint(entries)
        save_completed_review_receipt(str(path), str(path), digest, digest, 'synthetic-settings')

    def overview(self, names, **kwargs):
        return get_collection_overview(str(self.library), names, get_voice_rows, **kwargs)

    def activate(self, name, voices=READY_VOICES):
        path = self.library / f'{name}.json'
        (self.root / 'state.json').write_text(json.dumps({
            'active_book_id': name, 'book_generation': 'one',
            'input_file_path': str(path)}))
        (self.root / 'annotated_script.json').write_bytes(path.read_bytes())
        (self.root / 'voice_config.json').write_text(json.dumps(voices))

    def _setup_audio(self, missing=False):
        from chunk_status_journal import ChunkStatusJournal
        (self.root / 'one.wav').write_bytes(b'synthetic audio')
        second_missing = missing
        rows = [{'uid': 'one', 'id': 0, 'status': 'done', 'text': 'Synthetic line.',
                 'speaker': 'ALICE', 'instruct': '', 'audio_revision': 'take',
                 'audio_path': 'one.wav'}]
        if second_missing:
            rows.append({'uid': 'two', 'id': 1, 'status': 'done', 'text': 'Synthetic line.',
                         'speaker': 'ALICE', 'instruct': '', 'audio_revision': 'take',
                         'audio_path': 'missing.wav'})
        (self.root / 'chunks.json').write_text(json.dumps(rows))
        journal = ChunkStatusJournal(self.root / 'chunks.json',
                                      {'active_book_id': self._active_name, 'book_generation': 'one'})
        journal.apply_update('one', {'status': 'done', 'audio_path': 'one.wav'})
        if second_missing:
            journal.apply_update('two', {'status': 'done', 'audio_path': 'missing.wav'})
        output = self.root / 'cloned_audiobook.mp3'
        output.write_bytes(b'synthetic completed export')
        m4b = self.root / 'audiobook.m4b'
        if m4b.exists():
            m4b.unlink()
        return output

    def _active_row(self, name):
        import hashlib
        from book_workspace import get_book_workspace
        from export_quality_session import TASKS
        if self._states is None:
            self._states = {task_name: {} for task_name in TASKS}
        workspace = get_book_workspace(
            str(self.root),
            {'MP3': self.root / 'cloned_audiobook.mp3', 'M4B': self.root / 'audiobook.m4b'},
            get_voice_rows, self.manager.get_chunk_audio_path, export_states=self._states)
        script_hash = hashlib.sha256((self.library / f'{name}.json').read_bytes()).hexdigest()
        voice_path = self.library / f'{name}.voice_config.json'
        voice_hash = hashlib.sha256(voice_path.read_bytes()).hexdigest() if voice_path.exists() else None
        return self.overview([name], active_workspace=workspace, active_saved_name=name,
                             active_script_sha256=script_hash,
                             active_voice_sha256=voice_hash)[0]

    def test_a_recorded_review_readiness_inactive_and_active_without_audio(self):
        ready = self.save_book('ReadyA')
        self.complete_review(ready)
        self.save_book('UnreviewedA')
        pending = self.save_book('PendingA', voices=PENDING_VOICES)
        self.complete_review(pending)
        rows = {row['name']: row for row in self.overview(['ReadyA', 'UnreviewedA', 'PendingA'])}
        self.assertEqual('recorded', rows['ReadyA']['review']['status'])
        self.assertEqual('not_rechecked', rows['ReadyA']['review']['current_settings'])
        self.assertEqual('ready_for_audio', rows['ReadyA']['overall'])
        self.assertEqual('unknown', rows['ReadyA']['render']['status'])
        self.assertEqual('needs_review', rows['UnreviewedA']['overall'])
        self.assertEqual('needs_voices', rows['PendingA']['overall'])
        # Active, same bytes, no chunk audio: ready, never fake audio.
        self._active_name = 'ReadyA'
        self.activate('ReadyA')
        row = self._active_row('ReadyA')
        self.assertTrue(row['is_active'])
        self.assertEqual('recorded', row['review']['status'])
        self.assertEqual('ready_for_audio', row['overall'])
        self.assertEqual('not_started', row['render']['status'])
        self.assertNotIn('complete', row['render']['status'])
        self.assertNotEqual('completed', row['overall'])

    def _build_completed(self):
        from export_quality import get_export_file_version
        from routers import editor
        path = self.save_book('Whole')
        self.complete_review(path)
        self._active_name = 'Whole'
        self.activate('Whole')
        output = self._setup_audio()
        self._states = None
        from export_quality_session import TASKS
        self._states = {task_name: {} for task_name in TASKS}
        with patch.object(editor, 'DATA_DIR', str(self.root)), \
                patch.object(editor, 'SCRIPT_PATH', str(self.root / 'annotated_script.json')):
            attempt = editor.get_export_quality_attempt(None, self._states['audio'])
            attempt.publish(dict(status='verified', file='cloned_audiobook.mp3',
                                 file_version=get_export_file_version(output)))
            attempt.finish(True, 'done')
        return output

    def test_b_bound_full_export_via_editor_constructor_completes(self):
        output = self._build_completed()
        row = self._active_row('Whole')
        self.assertEqual('complete', row['render']['status'])
        self.assertEqual('completed', row['overall'])
        self.assertIn('metadata-only', row['render']['evidence'])
        # Bound report carries current binding to the done attempt.
        report = self._states['audio']['export_quality_reports'][0]
        self.assertEqual('current', report['input_binding'])
        self.assertEqual(report['attempt_started_at'],
                         self._states['audio']['export_quality_attempt']['started_at'])
        # Full MP3 needs no unrequested M4B.
        self.assertFalse((self.root / 'audiobook.m4b').exists())
        self.assertTrue(output.exists())

    def test_input_revision_refuses_unavailable_capture(self):
        from book_state_transaction import get_book_operation_token
        from export_quality_session import get_export_input_revision
        from project_scope_files import capture_scope_file
        self._build_completed()
        token = get_book_operation_token(str(self.root))
        for unreadable in ('annotated_script.json', 'voice_config.json',
                           'annotated_script.selection_binding.json'):
            with self.subTest(unreadable=unreadable):
                def capture(relative, limit, include_bytes):
                    if relative == unreadable:
                        raise OSError('Synthetic unreadable input')
                    return capture_scope_file(str(self.root), relative, limit=limit,
                                              include_bytes=include_bytes)
                proof = get_export_input_revision(str(self.root), token, capture)
                self.assertIsNone(proof['revision'])
                self.assertEqual('unknown', proof['scope'])

    def test_c_any_input_mutation_invalidates_completion(self):
        for kind in ('script', 'voice', 'chunk', 'take', 'output', 'scope'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                library = root / 'scripts'
                library.mkdir()
                # Rebuild a completed fixture in this fresh root.
                saved = library / 'Whole.json'
                saved.write_text(json.dumps(ENTRIES))
                (library / 'Whole.meta.json').write_text(json.dumps({'book_id': 'Whole'}))
                (library / 'Whole.voice_config.json').write_text(json.dumps(READY_VOICES))
                digest = get_review_entries_fingerprint(ENTRIES)
                save_completed_review_receipt(str(saved), str(saved), digest, digest, 'synthetic-settings')
                (root / 'state.json').write_text(json.dumps({
                    'active_book_id': 'Whole', 'book_generation': 'one',
                    'input_file_path': str(saved)}))
                (root / 'annotated_script.json').write_bytes(saved.read_bytes())
                (root / 'voice_config.json').write_text(json.dumps(READY_VOICES))
                (root / 'one.wav').write_bytes(b'synthetic audio')
                chunks = [{'uid': 'one', 'id': 0, 'status': 'done', 'text': 'Synthetic line.',
                           'speaker': 'ALICE', 'instruct': '', 'audio_revision': 'take',
                           'audio_path': 'one.wav'}]
                (root / 'chunks.json').write_text(json.dumps(chunks))
                from chunk_status_journal import ChunkStatusJournal
                ChunkStatusJournal(root / 'chunks.json',
                                   {'active_book_id': 'Whole', 'book_generation': 'one'}).apply_update(
                    'one', {'status': 'done', 'audio_path': 'one.wav'})
                output = root / 'cloned_audiobook.mp3'
                output.write_bytes(b'synthetic completed export')
                from export_quality import get_export_file_version
                from export_quality_session import TASKS
                from routers import editor
                states = {task_name: {} for task_name in TASKS}
                with patch.object(editor, 'DATA_DIR', str(root)), \
                        patch.object(editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')):
                    attempt = editor.get_export_quality_attempt(None, states['audio'])
                    attempt.publish(dict(status='verified', file='cloned_audiobook.mp3',
                                         file_version=get_export_file_version(output)))
                    attempt.finish(True, 'done')
                # Mutate the chosen input.
                if kind == 'script':
                    edited = [{**ENTRIES[0], 'text': 'Edited synthetic line.'}]
                    saved.write_text(json.dumps(edited))
                    (root / 'annotated_script.json').write_bytes(saved.read_bytes())
                    edited_digest = get_review_entries_fingerprint(edited)
                    save_completed_review_receipt(str(saved), str(saved), edited_digest, edited_digest,
                                                  'synthetic-settings')
                elif kind == 'voice':
                    changed = {'ALICE': {'type': 'custom', 'description': 'Changed synthetic voice'}}
                    (library / 'Whole.voice_config.json').write_text(json.dumps(changed))
                    (root / 'voice_config.json').write_text(json.dumps(changed))
                elif kind == 'chunk':
                    chunks[0]['text'] = 'Chunk changed synthetic line.'
                    (root / 'chunks.json').write_text(json.dumps(chunks))
                elif kind == 'take':
                    (root / 'one.wav').write_bytes(b'replaced take with a different size')
                elif kind == 'output':
                    output.write_bytes(b'replaced output with a different size')
                elif kind == 'scope':
                    state = json.loads((root / 'state.json').read_text())
                    state['_source_scope'] = {'version': 1, 'word_policy': 'AA2_text_diff_v1'}
                    (root / 'state.json').write_text(json.dumps(state))
                import hashlib
                from book_workspace import get_book_workspace
                workspace = get_book_workspace(
                    str(root),
                    {'MP3': root / 'cloned_audiobook.mp3', 'M4B': root / 'audiobook.m4b'},
                    get_voice_rows, ProjectManager(str(root)).get_chunk_audio_path,
                    export_states=states)
                row = get_collection_overview(
                    str(library), ['Whole'], get_voice_rows, active_workspace=workspace,
                    active_saved_name='Whole',
                    active_script_sha256=hashlib.sha256((library / 'Whole.json').read_bytes()).hexdigest(),
                    active_voice_sha256=hashlib.sha256(
                        (library / 'Whole.voice_config.json').read_bytes()).hexdigest())[0]
                self.assertNotEqual('complete', row['render']['status'], kind)
                self.assertNotEqual('completed', row['overall'], kind)
                if kind == 'output':
                    # Earlier output file is replaced only by the mutation,
                    # never restored/silently kept as current evidence.
                    self.assertEqual('export_needed', row['render']['status'])

    def test_c_old_unbound_receipt_stays_historical(self):
        from book_state_transaction import get_book_operation_token
        from export_quality import get_export_file_version
        from export_quality_session import ExportQualityAttempt, TASKS
        path = self.save_book('Whole')
        self.complete_review(path)
        self._active_name = 'Whole'
        self.activate('Whole')
        output = self._setup_audio()
        self._states = {task_name: {} for task_name in TASKS}
        token = get_book_operation_token(str(self.root))
        attempt = ExportQualityAttempt(self._states['audio'], token)
        attempt.publish(dict(status='verified', file='cloned_audiobook.mp3',
                             file_version=get_export_file_version(output)))
        attempt.finish(True, 'done')
        row = self._active_row('Whole')
        self.assertEqual('recorded', row['render']['status'])
        self.assertNotEqual('completed', row['overall'])

    def test_c_failed_capture_is_explicit_unbound(self):
        from book_state_transaction import get_book_operation_token
        from export_quality import get_export_file_version
        from export_quality_session import ExportQualityAttempt, TASKS
        path = self.save_book('Whole')
        self.complete_review(path)
        self._active_name = 'Whole'
        self.activate('Whole')
        output = self._setup_audio()
        self._states = {task_name: {} for task_name in TASKS}
        token = get_book_operation_token(str(self.root))

        def broken(relative, limit, include_bytes):
            raise ValueError('synthetic capture failure')
        attempt = ExportQualityAttempt(self._states['audio'], token,
                                       data_dir=str(self.root), capture=broken)
        self.assertEqual('unavailable', attempt.attempt['input_binding'])
        attempt.publish(dict(status='verified', file='cloned_audiobook.mp3',
                             file_version=get_export_file_version(output)))
        attempt.finish(True, 'done')
        report = self._states['audio']['export_quality_reports'][0]
        self.assertEqual('unavailable', report['input_binding'])
        self.assertIsNone(report['input_revision'])
        row = self._active_row('Whole')
        self.assertEqual('recorded', row['render']['status'])
        self.assertNotEqual('completed', row['overall'])

    def test_d_latest_full_attempt_blocks_and_partial_scope_do_not_complete(self):
        from export_quality import get_export_file_version
        from routers import editor
        self._build_completed()
        with patch.object(editor, 'DATA_DIR', str(self.root)), \
                patch.object(editor, 'SCRIPT_PATH', str(self.root / 'annotated_script.json')):
            running = editor.get_export_quality_attempt(None, self._states['audio'])
        self.assertEqual('in_progress', self._active_row('Whole')['render']['status'])
        running.finish(False, 'Export cancelled')
        row = self._active_row('Whole')
        self.assertEqual('cancelled', row['render']['status'])
        self.assertEqual('cancelled', row['overall'])
        with patch.object(editor, 'DATA_DIR', str(self.root)), \
                patch.object(editor, 'SCRIPT_PATH', str(self.root / 'annotated_script.json')):
            failed = editor.get_export_quality_attempt(None, self._states['audio'])
        failed.finish(False, 'boom')
        self.assertEqual('failed', self._active_row('Whole')['render']['status'])
        # Successful earlier output/receipt are preserved on disk.
        self.assertTrue((self.root / 'cloned_audiobook.mp3').exists())
        self.assertTrue(self._states['audio']['export_quality_reports'])
        # Chapter-only verified evidence cannot certify the whole book.
        self._states = {task_name: {} for task_name in __import__(
            'export_quality_session', fromlist=['TASKS']).TASKS}
        chapter = (self.root / 'chapter_exports' / '0001.mp3')
        chapter.parent.mkdir(exist_ok=True)
        chapter.write_bytes(b'synthetic chapter audio')
        from export_quality_session import ExportQualityAttempt
        token = __import__('book_state_transaction', fromlist=['get_book_operation_token']).get_book_operation_token(str(self.root))
        chapter_attempt = ExportQualityAttempt(self._states['chapter_export'], token)
        chapter_attempt.publish(dict(status='verified', file='chapter_exports/0001.mp3',
                                     file_version=get_export_file_version(chapter)))
        chapter_attempt.finish(True, 'done')
        row = self._active_row('Whole')
        self.assertNotEqual('complete', row['render']['status'])
        # Scoped export cannot certify the whole book either.
        state = json.loads((self.root / 'state.json').read_text())
        state['_source_scope'] = {'version': 1, 'word_policy': 'AA2_text_diff_v1'}
        (self.root / 'state.json').write_text(json.dumps(state))
        row = self._active_row('Whole')
        self.assertNotEqual('complete', row['render']['status'])
        self.assertNotEqual('completed', row['overall'])

    def test_e_checkpoint_content_edit_forces_inlock_load_409_and_zero_apply(self):
        from collection_overview import compute_saved_book_revision
        path = self.save_book('ProofBook')
        self.complete_review(path)
        revision = compute_saved_book_revision(str(self.library), 'ProofBook')
        self.assertRegex(revision, r'^[0-9a-f]{64}$')
        header = self.library / 'ProofBook.json.generation_checkpoint.json'
        header.write_text(json.dumps({'fingerprint': 'x'}))
        self.assertNotEqual(revision,
                            compute_saved_book_revision(str(self.library), 'ProofBook'))
        header.write_text(json.dumps({'fingerprint': 'y'}))
        self.assertNotEqual(revision,
                            compute_saved_book_revision(str(self.library), 'ProofBook'))
        # Manifest/publication/sound byte edits also change the proof.
        manifest = self.library / 'ProofBook.json.threepass_manifest.json'
        manifest.write_text(json.dumps({'status': 'complete'}))
        base = compute_saved_book_revision(str(self.library), 'ProofBook')
        manifest.write_text(json.dumps({'status': 'complete', 'extra': 1}))
        self.assertNotEqual(base,
                            compute_saved_book_revision(str(self.library), 'ProofBook'))
        (self.library / '.ProofBook.sound_design.json').write_text(json.dumps({'cues': []}))
        changed = compute_saved_book_revision(str(self.library), 'ProofBook')
        (self.library / '.ProofBook.sound_design.json').write_text(json.dumps({'cues': [1]}))
        self.assertNotEqual(changed,
                            compute_saved_book_revision(str(self.library), 'ProofBook'))
        # Remove the synthetic sound companion: load validates sound
        # companions strictly, and this proof change is already shown.
        (self.library / '.ProofBook.sound_design.json').unlink()
        # Route: stale proof with edited header 409s with zero applies.
        (self.root / 'state.json').write_text(json.dumps({
            'active_book_id': 'ProofBook', 'book_generation': 'one',
            'input_file_path': str(path)}))
        (self.root / 'annotated_script.json').write_bytes(path.read_bytes())
        (self.root / 'voice_config.json').write_text(json.dumps(READY_VOICES))
        application = FastAPI()
        application.include_router(scripts_library.router)
        with contextlib.ExitStack() as stack:
            for key, value in [('DATA_DIR', str(self.root)), ('SCRIPTS_DIR', str(self.library)),
                               ('AUDIOBOOK_PATH', str(self.root / 'cloned_audiobook.mp3')),
                               ('M4B_PATH', str(self.root / 'audiobook.m4b')),
                               ('SCRIPT_PATH', str(self.root / 'annotated_script.json')),
                               ('VOICE_CONFIG_PATH', str(self.root / 'voice_config.json')),
                               ('CHUNKS_PATH', str(self.root / 'chunks.json'))]:
                stack.enter_context(patch.object(scripts_library, key, value))
            stack.enter_context(patch('core.project_manager', self.manager))
            with patch.object(scripts_library, 'apply_book_state_locked') as apply_spy:
                with TestClient(application) as client:
                    stale = client.post('/api/scripts/load',
                                        json={'name': 'ProofBook', 'expected_revision': revision})
                    self.assertEqual(409, stale.status_code)
                    self.assertEqual(0, apply_spy.call_count)
                    ordinary = client.post('/api/scripts/load', json={'name': 'ProofBook'})
                    self.assertEqual(200, ordinary.status_code, ordinary.text)
                    self.assertEqual(1, apply_spy.call_count)

    def test_f_overview_reading_changes_no_book_bytes(self):
        self._build_completed()
        before = {str(p.relative_to(self.root)): p.read_bytes()
                  for p in self.root.rglob('*') if p.is_file() and not p.name.endswith('.lock')}
        self._active_row('Whole')
        self.overview(['Whole'])
        after = {str(p.relative_to(self.root)): p.read_bytes()
                 for p in self.root.rglob('*') if p.is_file() and not p.name.endswith('.lock')}
        self.assertEqual(before, after)


if __name__ == '__main__':
    unittest.main()
