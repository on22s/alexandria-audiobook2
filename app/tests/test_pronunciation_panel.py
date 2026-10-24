"""Lexicon preservation, real API/audio cleanup, and worker-local audition isolation."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient
import core
import pronunciation
from project import ProjectManager
from routers import editor, pronunciation_panel as panel
from tests.test_support import use_private_cpu_fixture_lock


class PronunciationPanelTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.path = self.root/'pronunciation.json'
        self.document = {'_comment': 'retain this metadata', 'names': {'Rom': 'Rohm', 'Rom Vale': 'Full', 'Felt': ''}}
        self.path.write_text(json.dumps(self.document))
        self.pm = ProjectManager(self.tmp.name)
        self.pm.save_chunks([{'id': 0, 'uid': 'a', 'speaker': 'Hero', 'text': "Rom Vale met Rom. Romance. rom. Rom's Felt felt."}])
        (self.root/'voice_config.json').write_text(json.dumps({'Hero': {'type': 'custom', 'voice': 'Ryan'}}))
        for obj, name, value in [(pronunciation, 'DEFAULT_PATH', str(self.path)), (panel, 'project_manager', self.pm),
                                 (editor, 'project_manager', self.pm), (core, 'DATA_DIR', self.tmp.name),
                                 (panel, 'SCRIPT_PATH', str(self.root/'annotated_script.json')),
                                 (editor, 'SCRIPT_PATH', str(self.root/'annotated_script.json'))]:
            active = patch.object(obj, name, value); active.start(); self.addCleanup(active.stop)
        self.token = editor.get_snapshot_digest(editor.get_editor_book_identity(self.tmp.name))
        app = FastAPI(); app.include_router(panel.router)
        self.client = TestClient(app)
        self.request = {'expected_book_token': self.token, 'index': 0, 'expected_uid': 'a', 'text': 'Rom'}

    def test_date_order_choice_is_saved_in_book_state_and_refused_when_stale_or_busy(self):
        # #1546: the reader's choice is what ambiguous slash dates follow.
        (self.root/'annotated_script.json').write_text(json.dumps([{'text': 'She wrote on 1/2/1900.'}]))
        (self.root/'state.json').write_text(json.dumps({'active_book_id': 'book'}))
        view = self.client.get('/api/pronunciation/date_order').json()
        self.assertEqual(('auto', None, 1), (view['choice'], view['order'], view['evidence']['detail']['ambiguous_dates']))
        saved = self.client.post('/api/pronunciation/date_order', json={'choice': 'dmy', 'expected_book_token': view['book_token']})
        self.assertEqual(200, saved.status_code, saved.text)
        self.assertEqual(('dmy', 'dmy'), (saved.json()['choice'], saved.json()['order']))
        self.assertEqual({'active_book_id': 'book', 'speech_date_order': 'dmy'}, json.loads((self.root/'state.json').read_text()))
        self.assertEqual(409, self.client.post('/api/pronunciation/date_order',
                                               json={'choice': 'mdy', 'expected_book_token': '0' * 64}).status_code)
        with patch.object(panel, 'is_task_running', return_value=True):
            self.assertEqual(409, self.client.post('/api/pronunciation/date_order',
                                                   json={'choice': 'mdy', 'expected_book_token': view['book_token']}).status_code)
        back = self.client.post('/api/pronunciation/date_order', json={'choice': 'auto', 'expected_book_token': view['book_token']})
        self.assertEqual({'active_book_id': 'book'}, json.loads((self.root/'state.json').read_text()))
        self.assertEqual('auto', back.json()['choice'])
        self.assertEqual(422, self.client.post('/api/pronunciation/date_order',
                                               json={'choice': 'ymd', 'expected_book_token': view['book_token']}).status_code)
        # A corrupt state.json is reported as repairable and left exactly as found.
        for corrupt in ('{broken', '[]'):
            (self.root/'state.json').write_text(corrupt)
            for response in (self.client.get('/api/pronunciation/date_order'),
                             self.client.post('/api/pronunciation/date_order',
                                              json={'choice': 'dmy', 'expected_book_token': view['book_token']})):
                self.assertEqual(422, response.status_code, (corrupt, response.text))
                self.assertIn('state.json', response.json()['detail'])
            self.assertEqual(corrupt, (self.root/'state.json').read_text())
        # A book switch attempted between reading the order and the token cannot
        # pair book A's order with book B's token: it waits for the view's lock.
        (self.root/'state.json').write_text(json.dumps({'active_book_id': 'a', 'speech_date_order': 'dmy'}))
        token_a = panel.get_book_operation_token(self.tmp.name)
        read_status, switched = panel.get_book_date_order_status, []

        def switch_book():
            try:
                with panel.ensure_book_state(self.tmp.name, timeout=0.2):
                    (self.root/'state.json').write_text(json.dumps({'active_book_id': 'b'}))
                    switched.append(True)
            except TimeoutError:
                switched.append(False)

        def status_then_switch(data_dir):
            status = read_status(data_dir)
            worker = threading.Thread(target=switch_book); worker.start(); worker.join()
            return status
        with patch.object(panel, 'get_book_date_order_status', side_effect=status_then_switch):
            view = self.client.get('/api/pronunciation/date_order').json()
        self.assertEqual([False], switched)
        self.assertEqual(('dmy', token_a), (view['choice'], view['book_token']))

    def test_save_preserves_metadata_and_refuses_stale_or_malformed_file(self):
        loaded = self.client.get('/api/pronunciation').json()
        result = self.client.post('/api/pronunciation/save', json={'names': {'Rom': 'New'}, 'revision': loaded['revision']})
        self.assertEqual(200, result.status_code, result.text)
        self.assertEqual({'_comment': 'retain this metadata', 'names': {'Rom': 'New'}}, json.loads(self.path.read_text()))
        self.assertEqual('New', pronunciation.apply_pronunciation('Rom')[0])
        self.assertEqual(409, self.client.post('/api/pronunciation/save', json={'names': {}, 'revision': loaded['revision']}).status_code)
        self.path.write_text('{broken')
        self.assertEqual(422, self.client.post('/api/pronunciation/save', json={'names': {}, 'revision': result.json()['revision']}).status_code)
        self.assertEqual('{broken', self.path.read_text())

    def test_atomic_failure_preserves_file_and_preview_uses_production_matching(self):
        loaded = self.client.get('/api/pronunciation').json(); before = self.path.read_bytes()
        with patch.object(panel, 'atomic_json_write', side_effect=OSError('synthetic write failure')):
            with self.assertRaises(OSError):
                panel.save_pronunciation_document(panel.PronunciationSave(names={}, revision=loaded['revision']))
        self.assertEqual(before, self.path.read_bytes())

        response = self.client.post('/api/pronunciation/preview', json={'names': self.document['names'], 'expected_book_token': self.token})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual({'Rom Vale': 1, 'Rom': 1}, response.json()['counts'])
        self.assertEqual('Hero', response.json()['lines'][0]['speaker'])
        self.assertEqual(409, self.client.post('/api/pronunciation/preview', json={'names': {}, 'expected_book_token': 'f'*64}).status_code)
        self.assertEqual(before, self.path.read_bytes())

    def test_preview_refuses_invalid_chunk_ids_with_repair_guidance_without_rewriting_progress(self):
        chunk_path = Path(self.pm.chunks_path)
        audio_path = self.root/'retained.wav';audio_path.write_bytes(b'retained audio bytes')
        for row in ({'uid':'a','speaker':'Hero','text':'Rom'},
                    {'id':-1,'uid':'a','speaker':'Hero','text':'Rom'}):
            with self.subTest(row=row):
                chunk_path.write_text(json.dumps([row]))
                token = editor.get_snapshot_digest(editor.get_editor_book_identity(self.tmp.name))
                before = {path:path.read_bytes() for path in (chunk_path,audio_path,self.path)}
                response = self.client.post('/api/pronunciation/preview',json={
                    'names':{'Rom':'Rohm'},'expected_book_token':token})
                self.assertEqual(409,response.status_code,response.text)
                self.assertIn('Repair chunks.json',response.json()['detail'])
                self.assertEqual(before,{path:path.read_bytes() for path in before})

    def test_empty_missing_flat_and_invalid_documents(self):
        for doc in ([], {'names': []}, {'names': {'a': 1}}, {'names': {'': 'x'}}):
            self.path.write_text(json.dumps(doc))
            self.assertEqual(422, self.client.get('/api/pronunciation').status_code)
        self.path.write_text(json.dumps({'A': 'Ay'}))
        result = self.client.get('/api/pronunciation').json()
        self.assertEqual(200, self.client.post('/api/pronunciation/save', json={**result, 'names': {'B': 'Bee'}}).status_code)
        self.assertEqual({'B': 'Bee'}, json.loads(self.path.read_text()))
        self.path.unlink()
        self.assertEqual({}, self.client.get('/api/pronunciation').json()['names'])
        self.assertEqual(422, self.client.post('/api/pronunciation/save', json={'names': {'a': 'x'*257}, 'revision': '0'*64}).status_code)

    @use_private_cpu_fixture_lock()
    def test_audition_produces_playable_audio_without_rewriting_script_or_lexicon(self):
        (self.root/'voice_config.json').write_text(json.dumps({'Hero': {'type': 'custom', 'voice': 'Ryan',
            'versions': {'later': {'type': 'custom', 'voice': 'Vivian'}},
            'version_timeline': [{'from_index': 0, 'version_id': 'later'}],
            'style_timeline': [{'from_index': 0, 'character_style': 'quiet'}]}}))
        paths = [self.path, Path(self.pm.chunks_path), self.root/'voice_config.json']
        before = [path.read_bytes() for path in paths]; outputs = []
        class Engine:
            def generate_voice(inner, text, instruct, speaker, voices, output):
                from speech_text import normalize_for_speech
                self.assertEqual(text + '.', normalize_for_speech(text))
                self.assertEqual('Hero', speaker); self.assertEqual('Vivian', voices['Hero']['voice'])
                self.assertEqual('quiet', voices['Hero']['character_style'])
                outputs.append(Path(output)); sf.write(output, np.ones(800)*0.1, 8000); return True
        with patch.object(self.pm, 'get_engine', return_value=Engine()):
            for text in ['Rom', 'Rohm']:
                response = self.client.post('/api/pronunciation/audition', json={**self.request, 'text': text})
                self.assertEqual(200, response.status_code, response.text if response.status_code != 200 else '')
                self.assertTrue(response.content.startswith(b'RIFF'))
                self.assertEqual('no-store', response.headers['cache-control'])
                self.assertEqual('done', core.process_state['audio']['status'])
                self.assertIsNone(core.process_state['audio']['error'])
        self.assertEqual(before, [path.read_bytes() for path in paths])
        self.assertTrue(all(not path.parent.exists() for path in outputs))
        self.assertEqual('Rohm', pronunciation.apply_pronunciation('Rom')[0])
        self.assertFalse(core.process_state['audio']['running'])

    @use_private_cpu_fixture_lock()
    def test_failed_engine_stale_row_and_busy_task_never_publish_audio(self):
        outputs = []
        class Engine:
            def generate_voice(inner, *args):
                outputs.append(Path(args[-1])); outputs[-1].write_bytes(b'broken'); raise RuntimeError('synthetic')
        with patch.object(self.pm, 'get_engine', return_value=Engine()) as get_engine:
            response = self.client.post('/api/pronunciation/audition', json={**self.request, 'expected_uid': 'stale'})
            self.assertEqual(409, response.status_code); get_engine.assert_not_called()
            core.process_state['audio']['running'] = True
            try:
                self.assertEqual(400, self.client.post('/api/pronunciation/audition', json=self.request).status_code)
            finally:
                core.process_state['audio']['running'] = False
            self.assertEqual(500, self.client.post('/api/pronunciation/audition', json=self.request).status_code)
            self.assertEqual('failed', core.process_state['audio']['status'])
        self.assertTrue(all(not path.parent.exists() for path in outputs))
        self.assertEqual('Rohm', pronunciation.apply_pronunciation('Rom')[0])
        self.assertFalse(core.process_state['audio']['running'])
        class CancelledEngine:
            def generate_voice(inner, *args):
                outputs.append(Path(args[-1])); sf.write(outputs[-1], np.ones(800)*0.1, 8000)
                core.process_state['audio']['cancel'] = True
                return True
        try:
            with patch.object(self.pm, 'get_engine', return_value=CancelledEngine()):
                response = self.client.post('/api/pronunciation/audition', json=self.request)
                self.assertEqual(409, response.status_code, response.text)
                self.assertEqual('cancelled', core.process_state['audio']['status'])
                self.assertTrue(all(not path.parent.exists() for path in outputs))
        finally:
            core.process_state['audio']['cancel'] = False

    def test_literal_context_is_nested_thread_local_and_resets_after_error(self):
        ready, release = threading.Event(), threading.Event()
        def audition():
            try:
                with pronunciation.ensure_literal_pronunciation_audition():
                    ready.set(); self.assertTrue(release.wait(3))
                    self.assertEqual(('Rom', []), pronunciation.apply_pronunciation('Rom'))
                    with pronunciation.ensure_literal_pronunciation_audition():
                        self.assertEqual(('Rom', []), pronunciation.apply_pronunciation('Rom'))
                    raise RuntimeError('fixture')
            except RuntimeError:
                return pronunciation.apply_pronunciation('Rom')[0]
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(audition)
            try:
                self.assertTrue(ready.wait(3)); self.assertEqual('Rohm', pronunciation.apply_pronunciation('Rom')[0])
            finally: release.set()
            self.assertEqual('Rohm', future.result(3))

    @use_private_cpu_fixture_lock()
    def test_request_cancellation_retains_claim_until_worker_exits_and_cleans_files(self):
        started, release = threading.Event(), threading.Event(); outputs = []
        class Engine:
            def generate_voice(inner, *args):
                outputs.append(Path(args[-1])); started.set()
                if not release.wait(3): raise AssertionError('fixture did not release')
                sf.write(outputs[-1], np.ones(800)*0.1, 8000); return True
        async def run():
            task = asyncio.create_task(panel.generate_pronunciation_audition_endpoint(panel.PronunciationAudition(**self.request)))
            self.assertTrue(await asyncio.to_thread(started.wait, 3))
            task.cancel()
            try: await task
            except asyncio.CancelledError: pass
            self.assertTrue(core.process_state['audio']['running'])
            release.set()
        try:
            with patch.object(self.pm, 'get_engine', return_value=Engine()): asyncio.run(run())
        finally: release.set()
        self.assertTrue(all(not path.parent.exists() for path in outputs))
        self.assertFalse(core.process_state['audio']['running'])

    def test_flat_reserved_name_and_multiline_entries_round_trip_without_data_loss(self):
        for existing in ({}, {'A': 'Ay'}, {'names': 'nayms'}):
            with self.subTest(existing=existing):
                self.path.write_text(json.dumps(existing))
                loaded = self.client.get('/api/pronunciation')
                self.assertEqual(200, loaded.status_code, loaded.text)
                names = {'names': 'nayms', 'A\r\nB': 'Ay\nBee'}
                saved = self.client.post('/api/pronunciation/save', json={
                    'names': names, 'revision': loaded.json()['revision']})
                self.assertEqual(200, saved.status_code, saved.text)
                self.assertEqual(names, self.client.get('/api/pronunciation').json()['names'])
                self.assertEqual(names, json.loads(self.path.read_text())['names'])
                self.assertEqual('nayms', pronunciation.apply_pronunciation('names')[0])

    @use_private_cpu_fixture_lock()
    def test_audition_uses_real_dispatch_with_draft_at_production_substitution_stage(self):
        from tts import TTSEngine
        captured = []
        class Engine(TTSEngine):
            def __init__(inner):
                pass
            def get_voice_backend(inner, category):
                return 'local'
            def _local_generate_custom(inner, text, instruct, speaker, voices, output):
                captured.append((text, instruct, speaker))
                sf.write(output, np.full(800, 0.1), 8000)
                return True
        engine = Engine()
        for replacement in ['Soo~bah~roo', 'Soo...bah...roo', 'Ay Tee & Tee', 'Rohm·Vayl']:
            with self.subTest(replacement=replacement):
                self.path.write_text(json.dumps({'names': {'Rom': replacement}}))
                engine.generate_voice('Rom', 'quiet', 'Hero', {'Hero': {'type': 'custom', 'voice': 'Ryan'}}, str(self.root/'control.wav'))
                expected = captured[-1]
                rows = self.pm.load_chunks(); rows[0]['instruct'] = 'quiet'; self.pm.save_chunks(rows)
                with patch.object(self.pm, 'get_engine', return_value=engine):
                    response = self.client.post('/api/pronunciation/audition', json={**self.request, 'replacement': replacement})
                self.assertEqual(200, response.status_code, response.text if response.status_code != 200 else '')
                self.assertEqual(expected, captured[-1])
        self.assertFalse(core.process_state['audio']['running'])

    @use_private_cpu_fixture_lock()
    def test_invalid_voice_config_releases_claim_before_engine_or_output(self):
        for data in ('{broken', '[]', 'null'):
            with self.subTest(data=data), patch.object(self.pm, 'get_engine') as engine:
                (self.root/'voice_config.json').write_text(data)
                response = self.client.post('/api/pronunciation/audition', json=self.request)
                self.assertEqual(422, response.status_code, response.text)
                engine.assert_not_called()
                self.assertFalse(core.process_state['audio']['running'])
                self.assertEqual('failed', core.process_state['audio']['status'])
