"""Recorded review facts are bound to current chunks and do not certify quality."""
import copy
import hashlib
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from editor_review_facts import get_editor_review_facts
from project import ProjectManager
from routers import editor
from speech_verification_receipt import get_audio_file_version, get_speech_verification_receipt
from tests.test_speech_verification_receipt import get_report


class EditorReviewFactsTests(unittest.TestCase):
    def test_flags_discriminate_pending_missing_failed_unknown_and_real_drift(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pm = ProjectManager(tmp)
            (root/'ok.wav').write_bytes(b'not decoded by a presence check')
            (root/'empty.wav').write_bytes(b'')
            chunks = [
                {'id': 0, 'uid': 'pending', 'type': 'Narrator', 'status': 'pending'},
                {'id': 1, 'uid': 'unknown', 'speaker': 'UNKNOWN', 'status': 'error', 'error': 'failed'},
                {'id': 2, 'uid': 'missing', 'speaker': 'Background crowd', 'status': 'done', 'audio_path': 'missing.wav'},
                {'id': 3, 'uid': 'empty', 'speaker': 'Hero', 'status': 'done', 'audio_path': 'empty.wav'},
                {'id': 4, 'uid': 'drift', 'speaker': 'Hero', 'status': 'done', 'audio_path': 'ok.wav', 'drift': {'score': .3, 'flagged': True}},
                {'id': 5, 'uid': 'measured', 'speaker': 'Hero', 'status': 'done', 'audio_path': 'ok.wav', 'drift': {'score': .9, 'flagged': False}},
            ]
            before = copy.deepcopy(chunks)
            facts = get_editor_review_facts(chunks, pm.get_chunk_audio_path)
            self.assertEqual([[], ['unknown', 'failed'], ['missing'], ['missing'], ['drift'], []], [row['issues'] for row in facts])
            self.assertEqual(['not_rendered', 'not_rendered', 'unmeasured', 'unmeasured', 'flagged', 'measured'], [row['drift'] for row in facts])
            self.assertEqual('present', facts[4]['audio'])
            self.assertEqual(before, chunks)
            self.assertNotIn('text', json.dumps(facts))

    def test_view_only_historical_field_is_ignored_when_not_revalidated(self):
        row = {'id': 0, 'uid': 'one', 'speaker': 'A', 'status': 'done', 'audio_path': 'missing.wav'}
        for value in ('bad', [], 1, {'status': 'corroborated_content_difference'}):
            with self.subTest(value=value):
                facts = get_editor_review_facts([{**row, 'speech_verification_historical': value}],
                    lambda path: '/missing/audio')
                self.assertEqual(['missing'], facts[0]['issues'])

    def _finished_receipt(self, root, text='Exact API speech.'):
        path = Path(root) / 'audio.wav'
        path.write_bytes(b'finished artifact')
        chunk = {'id': 0, 'uid': 'one', 'speaker': 'A', 'text': 'Authored.',
                 'instruct': '', 'status': 'done', 'audio_revision': 'take',
                 'audio_path': str(path)}
        report = get_report(path, text)
        receipt = get_speech_verification_receipt(report, chunk, 'f' * 64, 'take',
            {'version': 1, 'text': text, 'language': 'English', 'provenance': 'known_leaf',
             'audio': {'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                       'size': path.stat().st_size}})
        receipt['audio_file_version'] = get_audio_file_version(path)
        return path, chunk, receipt

    def test_valid_view_historical_receipt_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, chunk, receipt = self._finished_receipt(tmp)
            receipt['audio_file_version'] = [value + 1 for value in receipt['audio_file_version']]
            row = {**chunk, 'speech_verification_historical': receipt}
            facts = get_editor_review_facts([row], lambda value: value, 'f' * 64)[0]
            self.assertIn('speech_historical', facts['issues'])

    def test_malformed_stored_receipt_does_not_hide_valid_view_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, chunk, receipt = self._finished_receipt(tmp)
            receipt['audio_file_version'] = [value + 1 for value in receipt['audio_file_version']]
            row = {**chunk, 'speech_verification': {},
                   'speech_verification_historical': receipt}
            facts = get_editor_review_facts([row], lambda value: value, 'f' * 64,
                include_readiness=True)[0]
            self.assertIn('speech_historical', facts['issues'])
            self.assertEqual('not_current', facts['speech_evidence'])

    @unittest.skipIf(sys.platform.startswith('win'), 'Windows ctime is creation time; saved receipts are historical')
    def test_current_receipt_ignores_extra_historical_field(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, chunk, receipt = self._finished_receipt(tmp)
            forged = copy.deepcopy(receipt)
            forged['audio_file_version'] = [value + 1 for value in forged['audio_file_version']]
            row = {**chunk, 'speech_verification': receipt,
                   'speech_verification_historical': forged}
            facts = get_editor_review_facts([row], lambda value: value, 'f' * 64)[0]
            self.assertEqual('no_content_candidate', facts['speech'])
            self.assertNotIn('speech_historical', facts['issues'])

    def test_deeply_nested_view_historical_receipt_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, chunk, receipt = self._finished_receipt(tmp)
            receipt['audio_file_version'] = [value + 1 for value in receipt['audio_file_version']]
            node = {}
            for _ in range(2000):
                node = {'x': node}
            receipt['deep'] = node
            row = {**chunk, 'speech_verification_historical': receipt}
            facts = get_editor_review_facts([row], lambda value: value, 'f' * 64)[0]
            self.assertNotIn('speech_historical', facts['issues'])

    @unittest.skipIf(sys.platform.startswith('win'), 'Windows ctime is creation time; saved receipts are historical')
    def test_deeply_nested_stored_receipt_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            path, chunk, receipt = self._finished_receipt(tmp)
            node = {}
            for _ in range(2000):
                node = {'x': node}
            receipt['deep'] = node
            row = {**chunk, 'speech_verification': receipt}
            facts = get_editor_review_facts([row], lambda value: value, 'f' * 64)[0]
            self.assertNotIn('speech_historical', facts['issues'])
            self.assertNotEqual('no_content_candidate', facts['speech'])

    def test_failed_invalid_and_unmeasured_voice_checks_never_become_passes(self):
        rows = [{'id': i, 'uid': str(i), 'status': 'done', 'speaker': 'A', 'audio_path': 'audio', 'drift': drift}
                for i, drift in enumerate([None, {'score': None, 'flagged': False, 'error': 'reference missing'}, {'score': float('nan'), 'flagged': False}, {'score': True, 'flagged': False}, {'score': .2, 'flagged': 'false'}, 'bad'])]
        facts = get_editor_review_facts(rows, lambda path: '/missing/audio')
        self.assertEqual(['unmeasured'] + ['unavailable'] * 5, [row['drift'] for row in facts])
        self.assertTrue(all('drift' not in row['issues'] for row in facts))
        self.assertTrue(all('missing' in row['issues'] for row in facts))

    def test_outside_paths_and_io_refusals_are_unavailable_without_reading_them(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as other:
            root = Path(tmp); outside = Path(other)/'private.wav'; outside.write_bytes(b'private')
            pm = ProjectManager(tmp)
            rows = [{'id': i, 'uid': str(i), 'speaker': '', 'status': 'done', 'audio_path': path}
                    for i, path in enumerate([str(outside), None, 4])]
            before = outside.read_bytes()
            self.assertTrue(all(row['issues'] == ['unknown', 'missing'] for row in get_editor_review_facts(rows, pm.get_chunk_audio_path)))
            self.assertEqual(before, outside.read_bytes())
            with patch('audio_validation.Path.is_file', side_effect=PermissionError):
                self.assertEqual('missing', get_editor_review_facts(rows[:1], lambda path: root/'a')[0]['audio'])

    def test_loaded_rows_are_detached_from_later_persisted_status_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            pm = ProjectManager(tmp)
            pm.save_chunks([{'id': 0, 'uid': 'one', 'speaker': 'A', 'status': 'pending'}])
            captured = pm.load_chunks()
            pm._update_chunk_fields(0, status='error', error='later failure')
            self.assertEqual('pending', captured[0]['status'])
            self.assertEqual([], get_editor_review_facts(captured, pm.get_chunk_audio_path)[0]['issues'])
            self.assertEqual(['failed'], get_editor_review_facts(pm.load_chunks(), pm.get_chunk_audio_path)[0]['issues'])

    def test_route_refuses_wrong_book_and_returns_matching_revision_without_writing_chunks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); pm = ProjectManager(tmp)
            pm.save_chunks([{'id': 0, 'uid': 'one', 'speaker': 'Hero', 'text': 'Synthetic line.', 'status': 'done', 'audio_path': 'one.wav'}])
            (root/'one.wav').write_bytes(b'synthetic')
            api = FastAPI(); api.include_router(editor.router)
            with patch.object(editor, 'project_manager', pm), patch.object(editor, 'SCRIPT_PATH', str(root/'annotated_script.json')), TestClient(api) as client:
                snapshot = client.get('/api/chunks/status').json()
                before = (root/'chunks.json').read_bytes()
                self.assertEqual(422, client.get('/api/chunks/quality').status_code)
                self.assertEqual(409, client.get('/api/chunks/quality', params={'expected_book_token': 'f'*64}).status_code)
                params = {'expected_book_token': snapshot['book_token']}
                response = client.get('/api/chunks/quality', params=params)
                self.assertEqual(200, response.status_code, response.text)
                facts = response.json(); self.assertEqual(snapshot['revision'], facts['revision'])
                self.assertEqual([], facts['rows'][0]['issues'])
                (root/'one.wav').unlink()
                self.assertEqual(['missing'], client.get('/api/chunks/quality', params=params).json()['rows'][0]['issues'])
                self.assertEqual(before, (root/'chunks.json').read_bytes())
                pm._update_chunk_fields(0, status='error', error='synthetic refusal')
                changed = client.get('/api/chunks/quality', params=params).json()
                self.assertNotEqual(snapshot['revision'], changed['revision'])
                self.assertEqual(['failed'], changed['rows'][0]['issues'])
