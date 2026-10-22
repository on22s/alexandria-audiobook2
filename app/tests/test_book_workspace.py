"""Workspace facts must reject corruption and never initialize/repair book data."""
import contextlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from book_workspace import get_book_workspace
from chunk_status_journal import ChunkStatusJournal
from project import ProjectManager
from routers import scripts_library
from routers.voices import get_voice_rows


class BookWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.manager = ProjectManager(str(self.root))

    def write(self, name, value):
        (self.root/name).write_text(json.dumps(value))

    def prepare(self, voice=None):
        self.write('state.json', {'active_book_id': 'Book A', 'book_generation': 'one'})
        self.write('annotated_script.json', [{'type': 'dialogue', 'speaker': 'ALICE', 'text': 'Synthetic line.'}])
        self.write('voice_config.json', {'ALICE': voice or {'type': 'custom', 'description': 'Synthetic voice'}})

    def read(self):
        return get_book_workspace(str(self.root), {'MP3': self.root/'out.mp3', 'M4B': self.root/'out.m4b'},
                                  get_voice_rows, self.manager.get_chunk_audio_path)

    def files(self):
        return {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file() and not p.name.endswith('.lock')}

    def test_empty_and_missing_chunks_are_distinct_from_completed_audio_and_do_not_write(self):
        before = self.files()
        empty = self.read()
        self.assertEqual(before, self.files())
        self.assertEqual(empty['next']['tab'], 'script')
        self.assertEqual(empty['audio']['status'], 'not_started')
        self.prepare()
        (self.root/'state.json').unlink()
        self.assertEqual(self.read()['name'], 'Current book (unnamed)')
        self.prepare()
        before = self.files()
        state = self.read()
        self.assertEqual(before, self.files())
        self.assertEqual(state['script']['entries'], 1)
        self.assertEqual(state['cast'], {'total': 1, 'pending': 0})
        self.assertEqual(state['next']['tab'], 'editor')
        self.assertFalse((self.root/'chunks.json').exists())

    def test_existing_voice_eligibility_and_pending_state_versions_are_used(self):
        self.prepare({'type': 'custom'})
        self.assertEqual(self.read()['next']['tab'], 'voices')
        # Consume the canonical row builder's pending-state signal; file
        # presence alone must not mark persona state cards prepared.
        state = get_book_workspace(str(self.root), {}, lambda script, voices: [
            {'persona_pending': False, 'persona_states_pending': True}], self.manager.get_chunk_audio_path)
        self.assertEqual(state['cast']['pending'], 1)
        self.assertEqual(state['next']['tab'], 'voices')

    def test_saved_speech_evidence_distinguishes_current_absent_and_changed_inputs_without_audio_reads(self):
        from book_state_transaction import get_book_operation_token
        from editor_review_facts import get_editor_review_facts
        from speech_verification_receipt import get_speech_verification_receipt
        from tests.test_speech_verification_receipt import get_report

        self.prepare()
        audio = self.root/'one.wav'; audio.write_bytes(b'synthetic artifact presence')
        row = dict(id=0, uid='one', status='done', text='Synthetic line.', speaker='ALICE',
                   instruct='', audio_revision='take', audio_path='one.wav')
        self.write('chunks.json', [row])
        self.assertEqual(1, self.read()['audio']['review']['speech_not_checked'])
        report = get_report(audio, row['text'])
        speech_input = dict(version=1, text=row['text'], language='English', provenance='known_leaf',
                            audio=report['audio'])
        token = get_book_operation_token(str(self.root))
        receipt = get_speech_verification_receipt(report, row, token, 'take', speech_input)
        info = audio.stat()
        receipt['audio_file_version'] = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
        row['speech_verification'] = receipt
        self.write('chunks.json', [row])
        original_open = Path.open
        def metadata_only(path, *args, **kwargs):
            self.assertNotEqual(audio, path, 'Workspace must not open audio bytes')
            return original_open(path, *args, **kwargs)
        before = self.files()
        with patch.object(Path, 'open', metadata_only), patch(
                'speech_verification_receipt.get_verifier_file_identity',
                side_effect=AssertionError('Workspace must not hash audio')):
            facts = self.read()['audio']['review']
            self.assertEqual('metadata_only', facts['identity_check'])
            self.assertEqual(1, facts['speech_current_metadata'])
            self.assertEqual(0, facts['speech_attention'])
            # The existing Editor response shape remains unchanged.
            self.assertNotIn('speech_evidence', get_editor_review_facts(
                [row], self.manager.get_chunk_audio_path, token)[0])
        self.assertEqual(before, self.files())
        for changes in ({'text': 'Changed authored line.'}, {'audio_revision': 'new take'},
                        {'speech_verification': {}}):
            with self.subTest(changes=changes):
                self.write('chunks.json', [{**row, **changes}])
                before = self.files()
                with patch.object(Path, 'open', metadata_only):
                    facts = self.read()['audio']['review']
                self.assertEqual(0, facts['speech_current_metadata'])
                self.assertEqual(1, facts['speech_not_current'])
                self.assertEqual(before, self.files())

    def test_saved_script_recovery_uses_shared_candidate_policy_without_resuming_or_proving_source(self):
        from script_recovery_facts import three_pass_manifest_path

        self.prepare()
        source = self.root/'source.txt'; source.write_text('Synthetic original source.')
        state = {'active_book_id': 'Book A', 'book_generation': 'one',
                 'input_file_path': str(source), 'script_generation_input_file': str(source)}
        self.write('state.json', state)
        manifest = Path(three_pass_manifest_path(str(self.root/'annotated_script.json')))
        for status in ('failed', 'incomplete', 'complete'):
            with self.subTest(status=status):
                manifest.write_text(json.dumps({'status': status, 'fingerprint': {'source_sha256': 'old'}}))
                before = self.files()
                facts = self.read()
                self.assertEqual(before, self.files())
                candidate = status != 'complete'
                self.assertEqual('candidate' if candidate else 'not_reported', facts['script']['recovery']['status'])
                self.assertEqual('not_rechecked', facts['script']['recovery']['eligibility'])
                self.assertEqual('script' if candidate else 'editor', facts['next']['tab'])
        manifest.write_text(json.dumps({'status': 'failed'}))
        source.write_text('Changed bytes at the same path.')
        facts = self.read()
        self.assertEqual('candidate', facts['script']['recovery']['status'])
        self.assertIn('must be rechecked', facts['next']['reason'])
        self.write('state.json', {**state, 'input_file_path': 'different.txt'})
        self.assertEqual('not_reported', self.read()['script']['recovery']['status'])

    def test_export_session_checks_remain_historical_after_edits_and_changed_files_are_refused(self):
        from book_state_transaction import get_book_operation_token
        from export_quality import get_export_file_version
        from export_quality_session import ExportQualityAttempt, TASKS

        self.prepare()
        path = self.root/'out.mp3'; path.write_bytes(b'synthetic completed export')
        token = get_book_operation_token(str(self.root))
        states = {name: {} for name in TASKS}
        attempt = ExportQualityAttempt(states['audio'], token)
        attempt.publish(dict(status='verified', file='out.mp3', file_version=get_export_file_version(path)))
        attempt.finish(True, 'done')
        def read():
            return get_book_workspace(str(self.root), {'MP3': path}, get_voice_rows,
                                      self.manager.get_chunk_audio_path, export_states=states)
        before = self.files()
        self.assertEqual(1, read()['export_review']['verified_at_export'])
        self.assertEqual(before, self.files())
        self.write('annotated_script.json', [{'speaker': 'ALICE', 'text': 'Edited within same book.'}])
        self.write('voice_config.json', {'ALICE': {'type': 'custom', 'description': 'Changed voice'}})
        self.assertEqual(token, get_book_operation_token(str(self.root)))
        facts = read()
        self.assertEqual(1, facts['export_review']['verified_at_export'])
        self.assertEqual('not_rechecked', facts['export_review']['current_inputs'])
        self.assertEqual('present_unverified', facts['outputs'][0]['status'])
        path.write_bytes(b'changed output with different size')
        facts = read()['export_review']
        self.assertEqual(0, facts['verified_at_export'])
        self.assertEqual(1, facts['unavailable'])
        self.write('state.json', {'active_book_id': 'Book B', 'book_generation': 'two'})
        self.assertEqual(0, read()['export_review']['verified_at_export'])

    def test_journal_progress_is_read_without_compaction_and_outputs_are_unverified(self):
        self.prepare()
        self.write('chunks.json', [{'uid': 'one', 'id': 0, 'status': 'pending', 'audio_path': None}])
        (self.root/'one.wav').write_bytes(b'synthetic audio presence')
        journal = ChunkStatusJournal(self.root/'chunks.json', {'active_book_id': 'Book A', 'book_generation': 'one'})
        journal.apply_update('one', {'status': 'done', 'audio_path': 'one.wav'})
        (self.root/'out.mp3').write_bytes(b'synthetic global output')
        before = self.files()
        state = self.read()
        self.assertEqual(state['audio']['present'], 1)
        self.assertEqual(state['audio']['status'], 'files_present')
        self.assertEqual(state['outputs'][0]['status'], 'present_unverified')
        self.assertEqual(state['next']['tab'], 'audio')
        self.assertIn('verify', state['next']['reason'])
        self.assertEqual(before, self.files())
        self.assertNotIn(self.directory.name, json.dumps(state))
        self.assertNotIn('Synthetic line.', json.dumps(state))

    def test_missing_empty_and_escaping_audio_do_not_count_as_present(self):
        self.prepare()
        self.write('chunks.json', [{'uid': str(i), 'id': i, 'status': 'done', 'audio_path': path}
                                 for i, path in enumerate(['missing.wav', 'empty.wav', '../outside.wav'])])
        (self.root/'empty.wav').touch()
        state = self.read()
        self.assertEqual(state['audio']['present'], 0)
        self.assertEqual(state['audio']['missing'], 3)
        self.assertEqual(state['next']['tab'], 'editor')

    def test_wrong_book_journal_and_corrupt_chunk_data_remain_unavailable_and_untouched(self):
        self.prepare()
        self.write('chunks.json', [{'uid': 'one', 'id': 0, 'status': 'pending'}])
        wrong = ChunkStatusJournal(self.root/'chunks.json', {'active_book_id': 'Other', 'book_generation': 'two'})
        wrong.apply_update('one', {'status': 'done'})
        before = self.files()
        self.assertEqual(self.read()['audio']['status'], 'unavailable')
        self.assertEqual(before, self.files())
        (self.root/'chunks.json').write_text('{')
        before = self.files()
        self.assertEqual(self.read()['audio']['status'], 'unavailable')
        self.assertEqual(before, self.files())

    def test_actual_route_returns_safe_conflict_for_corrupt_script_without_repair(self):
        self.prepare()
        (self.root/'annotated_script.json').write_text('{')
        application = FastAPI(); application.include_router(scripts_library.router)
        before = self.files()
        with contextlib.ExitStack() as stack:
            for key, value in [('DATA_DIR', str(self.root)), ('AUDIOBOOK_PATH', str(self.root/'out.mp3')),
                               ('M4B_PATH', str(self.root/'out.m4b'))]:
                stack.enter_context(patch.object(scripts_library, key, value))
            stack.enter_context(patch('core.project_manager', self.manager))
            with TestClient(application) as client:
                response = client.get('/api/workspace')
        self.assertEqual(response.status_code, 409)
        self.assertIn('No new chunks or audio', response.json()['detail'])
        self.assertEqual(before, self.files())

    def test_home_reads_refuse_library_recovery_without_touching_saved_books(self):
        import hashlib
        from book_state_transaction import JOURNAL, ensure_book_state

        self.prepare()
        library = self.root/'scripts'; library.mkdir()
        old = b'[{"text":"Original synthetic text."}]'
        replacement = b'[{"text":"New synthetic text."}]'
        application = FastAPI(); application.include_router(scripts_library.router)
        with contextlib.ExitStack() as stack:
            for key, value in [('DATA_DIR', str(self.root)), ('SCRIPTS_DIR', str(library)),
                               ('AUDIOBOOK_PATH', str(self.root/'out.mp3')), ('M4B_PATH', str(self.root/'out.m4b'))]:
                stack.enter_context(patch.object(scripts_library, key, value))
            stack.enter_context(patch('core.project_manager', self.manager))
            with TestClient(application) as client:
                for phase in ('publishing', 'committed', 'recovered', 'malformed'):
                    with self.subTest(phase=phase):
                        script = library/'Book.json'; script.write_bytes(replacement)
                        workspace = library/f'.book-switch-{phase}'; workspace.mkdir()
                        backup = workspace/'old-0'; backup.write_bytes(old)
                        journal = library/JOURNAL
                        journal.write_text('{' if phase == 'malformed' else json.dumps({
                            'version': 1, 'phase': phase, 'workspace': workspace.name,
                            'entries': [{'path': 'Book.json', 'existed': True,
                                'old_sha256': hashlib.sha256(old).hexdigest(),
                                'new_sha256': hashlib.sha256(replacement).hexdigest()}]}))
                        def files():
                            return {str(p.relative_to(self.root)): p.read_bytes()
                                    for p in self.root.rglob('*') if p.is_file() and not p.name.endswith('.lock')}
                        before = files()
                        self.assertEqual(200, client.get('/api/workspace').status_code)
                        self.assertEqual(409, client.get('/api/scripts').status_code)
                        self.assertEqual(before, files())
                        if phase == 'malformed':
                            journal.unlink(); backup.unlink(); workspace.rmdir()
                        else:
                            with ensure_book_state(str(library)):
                                pass
                            self.assertFalse(journal.exists())
                            self.assertFalse(workspace.exists())
                            self.assertEqual(old if phase == 'publishing' else replacement, script.read_bytes())
                            self.assertEqual(200, client.get('/api/scripts').status_code)

    def test_optional_deeply_corrupt_manifest_does_not_hide_the_book_or_repair_it(self):
        self.prepare()
        self.write('state.json', {'input_file_path': 'source.txt', 'script_generation_input_file': 'source.txt'})
        manifest = self.root/'annotated_script.json.threepass_manifest.json'
        manifest.write_text('[' * 2000 + ']' * 2000)
        before = self.files()
        application = FastAPI(); application.include_router(scripts_library.router)
        with patch.object(scripts_library, 'DATA_DIR', str(self.root)), patch('core.project_manager', self.manager):
            with TestClient(application) as client:
                response = client.get('/api/workspace')
        self.assertEqual(200, response.status_code)
        self.assertEqual('not_reported', response.json()['script']['recovery']['status'])
        self.assertEqual(before, self.files())

    def test_actual_route_maps_admission_timeout_without_reading_or_changing_book(self):
        self.prepare()
        application = FastAPI(); application.include_router(scripts_library.router)
        before = self.files()
        with patch.object(scripts_library, 'DATA_DIR', str(self.root)), patch(
                'core.project_manager', self.manager), patch.object(
                scripts_library, 'ensure_book_state', side_effect=TimeoutError('busy')), patch(
                'book_workspace.get_book_workspace', side_effect=AssertionError('Not admitted')):
            with TestClient(application) as client:
                response = client.get('/api/workspace')
        self.assertEqual(409, response.status_code)
        self.assertEqual(before, self.files())

    def test_actual_route_refuses_terminal_and_malformed_journals_without_cleanup(self):
        from book_state_transaction import JOURNAL, ensure_book_state

        self.prepare()
        application = FastAPI(); application.include_router(scripts_library.router)
        with contextlib.ExitStack() as stack:
            stack.enter_context(patch.object(scripts_library, 'DATA_DIR', str(self.root)))
            stack.enter_context(patch('core.project_manager', self.manager))
            with TestClient(application) as client:
                for phase in ('committed', 'recovered', 'unknown', 'malformed'):
                    with self.subTest(phase=phase):
                        workspace = self.root/f'.book-switch-{phase}'; workspace.mkdir()
                        backup = workspace/'old-0'; backup.write_bytes(b'preserved backup')
                        journal = self.root/JOURNAL
                        journal.write_text('{' if phase == 'malformed' else json.dumps({
                            'version': 1, 'phase': phase, 'workspace': workspace.name,
                            'entries': [{'path': 'annotated_script.json', 'existed': True}]}))
                        before = self.files()
                        response = client.get('/api/workspace')
                        self.assertEqual(409, response.status_code)
                        self.assertEqual(before, self.files())
                        self.assertEqual(b'preserved backup', backup.read_bytes())
                        if phase in ('committed', 'recovered'):
                            # Explicit recovering admission retains canonical terminal cleanup.
                            with ensure_book_state(str(self.root)):
                                pass
                            self.assertFalse(journal.exists())
                            self.assertFalse(workspace.exists())
                            self.assertEqual(200, client.get('/api/workspace').status_code)
                        else:
                            journal.unlink(); backup.unlink(); workspace.rmdir()

    def test_actual_route_refuses_pending_book_publication_without_recovering_it(self):
        import hashlib
        from book_state_transaction import JOURNAL

        self.prepare()
        original = (self.root/'annotated_script.json').read_bytes()
        workspace = self.root/'.book-switch-workspace-fixture'; workspace.mkdir()
        (workspace/'old-0').write_bytes(original)
        self.write('annotated_script.json', [{'speaker': 'ALICE', 'text': 'New pending script.'}])
        replacement = (self.root/'annotated_script.json').read_bytes()
        self.write(JOURNAL, {'version': 1, 'phase': 'publishing', 'workspace': workspace.name,
            'entries': [{'path': 'annotated_script.json', 'existed': True,
                         'old_sha256': hashlib.sha256(original).hexdigest(),
                         'new_sha256': hashlib.sha256(replacement).hexdigest()}]})
        application = FastAPI(); application.include_router(scripts_library.router)
        before = self.files()
        with contextlib.ExitStack() as stack:
            for key, value in [('DATA_DIR', str(self.root)), ('AUDIOBOOK_PATH', str(self.root/'out.mp3')),
                               ('M4B_PATH', str(self.root/'out.m4b'))]:
                stack.enter_context(patch.object(scripts_library, key, value))
            stack.enter_context(patch('core.project_manager', self.manager))
            with TestClient(application) as client:
                response = client.get('/api/workspace')
        observed = {'status': response.status_code,
                    'script_changed': (self.root/'annotated_script.json').read_bytes() != replacement,
                    'journal_removed': not (self.root/JOURNAL).exists(),
                    'workspace_removed': not workspace.exists()}
        self.assertEqual(409, response.status_code, repr(observed))
        self.assertEqual(before, self.files())
        self.assertEqual(original, (workspace/'old-0').read_bytes())
        from book_state_transaction import ensure_book_state
        with ensure_book_state(str(self.root)):
            pass
        self.assertEqual(original, (self.root/'annotated_script.json').read_bytes())
        self.assertFalse((self.root/JOURNAL).exists())
        self.assertFalse(workspace.exists())
