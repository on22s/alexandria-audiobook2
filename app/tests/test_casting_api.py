"""Casting library routes (#1532) on a temporary designed_voices tree."""
from contextlib import ExitStack
from pathlib import Path
import json
import tempfile
import unittest
from unittest.mock import patch


class CastingApiTests(unittest.TestCase):
    def setUp(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from routers import casting
        from routers import voice_library
        import core
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.dir = root / 'designed_voices'
        voice = self.dir / 'persona' / 'dh_1' / 'abel_1'
        voice.mkdir(parents=True)
        (voice / 'preview.wav').write_bytes(b'RIFF')
        (voice / 'meta.json').write_text(json.dumps({
            'book_id': 'DH', 'speaker': 'ABEL', 'description': 'Young male; calm.', 'ref_text': 'Hello there.',
            'preview': 'designed_voices/persona/dh_1/abel_1/preview.wav'}), encoding='utf-8')
        mute = self.dir / 'persona' / 'dh_1' / 'mute_1'
        mute.mkdir()
        (mute / 'meta.json').write_text(json.dumps({'speaker': 'MUTE', 'description': 'female'}), encoding='utf-8')
        self.marks = root / 'casting_marks.json'
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(casting, 'DESIGNED_VOICES_DIR', str(self.dir)))
        stack.enter_context(patch.object(casting, 'CASTING_MARKS_PATH', str(self.marks)))
        self.library = root / 'voice_library.json'
        self.config = root / 'voice_config.json'
        self.script = root / 'annotated_script.json'
        self.config.write_text(json.dumps({'ADAM': {'type': 'custom', 'voice': 'old'}, 'EVE': {'voice': 'preserve'}}))
        self.script.write_text(json.dumps([{'speaker': 'ADAM', 'text': 'A synthetic test line.'}]))
        (root / 'state.json').write_text(json.dumps({'active_book_id': 'test-book'}))
        for module in (core, voice_library):
            for key, value in [('VOICE_LIBRARY_PATH', self.library), ('VOICE_CONFIG_PATH', self.config),
                               ('SCRIPT_PATH', self.script), ('SCRIPTS_DIR', root / 'scripts')]:
                stack.enter_context(patch.object(module, key, str(value)))
        stack.enter_context(patch.object(core, 'DATA_DIR', str(root)))
        stack.enter_context(patch.object(voice_library, 'DESIGNED_VOICES_DIR', str(self.dir)))
        stack.enter_context(patch.object(voice_library, 'CHARACTER_ALIASES_PATH', str(root / 'aliases.json')))
        counts = core._script_line_counts
        stack.enter_context(patch.object(voice_library, '_script_line_counts', side_effect=lambda path=None: counts(path or str(self.script))))
        app = FastAPI()
        app.include_router(casting.router)
        app.include_router(voice_library.router)
        self.client = stack.enter_context(TestClient(app))

    def test_collection_saves_clone_and_applies_through_existing_book_fence(self):
        created = self.client.post('/api/voice_library/casts', json={'name': 'Collection'})
        self.assertEqual(200, created.status_code, created.text)
        saved = self.client.post('/api/voice_library/catalog_member', json={
            'cast': 'Collection', 'voice_id': 'persona/dh_1/abel_1', 'name': 'ADAM'})
        self.assertEqual(200, saved.status_code, saved.text)
        member = json.loads(self.library.read_text())['casts']['Collection']['members']['adam']
        built = self.client.get('/api/casting/clone_config', params={'voice_id': 'persona/dh_1/abel_1', 'speaker': 'ADAM'}).json()['config']
        self.assertEqual(built, member['config'])
        matched = self.client.post('/api/voice_library/match', json={'name': 'Collection'})
        self.assertEqual(200, matched.status_code, matched.text)
        before = self.config.read_bytes()
        stale = self.client.post('/api/voice_library/apply', json={
            'cast': 'Collection', 'mapping': {'ADAM': 'adam'}, 'book_token': '0' * 64})
        self.assertEqual(409, stale.status_code, stale.text)
        self.assertEqual(before, self.config.read_bytes())
        applied = self.client.post('/api/voice_library/apply', json={
            'cast': 'Collection', 'mapping': {'ADAM': 'adam'}, 'book_token': matched.json()['book_token']})
        self.assertEqual(200, applied.status_code, applied.text)
        self.assertEqual(['ADAM'], applied.json()['applied'])
        config = json.loads(self.config.read_text())
        self.assertEqual(built, config['ADAM'])
        self.assertEqual({'voice': 'preserve'}, config['EVE'])

    def test_collection_refusals_preserve_library(self):
        self.client.post('/api/voice_library/casts', json={'name': 'Collection'})
        request = {'cast': 'Collection', 'voice_id': 'persona/dh_1/abel_1', 'name': 'ADAM'}
        self.assertEqual(200, self.client.post('/api/voice_library/catalog_member', json=request).status_code)
        before = self.library.read_bytes()
        for patch, code in [({}, 409), ({'voice_id': 'persona/dh_1/mute_1'}, 409),
                            ({'voice_id': '../../secret'}, 404), ({'cast': 'Absent'}, 404),
                            ({'name': ' '}, 400), ({'cast': '__shared__'}, 400), ({'name': ''}, 422)]:
            with self.subTest(patch=patch):
                response = self.client.post('/api/voice_library/catalog_member', json={**request, **patch})
                self.assertEqual(code, response.status_code, response.text)
                self.assertEqual(before, self.library.read_bytes())

    def test_library_lists_voices_and_only_their_marks(self):
        self.marks.write_text(json.dumps({'persona/dh_1/abel_1': {'status': 'shortlist'},
                                          'persona/gone/voice': {'status': 'reject'}}), encoding='utf-8')
        body = self.client.get('/api/casting/voices').json()
        self.assertEqual(['persona/dh_1/abel_1', 'persona/dh_1/mute_1'], [v['id'] for v in body['voices']])
        self.assertEqual({'persona/dh_1/abel_1': {'status': 'shortlist'}}, body['marks'])

    def test_mark_round_trip_and_refusals(self):
        ok = self.client.post('/api/casting/marks', json={'voice_id': 'persona/dh_1/abel_1', 'status': 'callback'})
        self.assertEqual(200, ok.status_code, ok.text)
        self.assertEqual({'status': 'callback'}, ok.json()['mark'])
        self.assertEqual(400, self.client.post('/api/casting/marks', json={
            'voice_id': 'persona/dh_1/abel_1', 'status': 'maybe'}).status_code)
        self.assertEqual(404, self.client.post('/api/casting/marks', json={
            'voice_id': 'persona/dh_1/nobody', 'status': 'shortlist'}).status_code)
        self.assertEqual(422, self.client.post('/api/casting/marks', json={
            'voice_id': '../../etc/passwd', 'status': 'shortlist'}).status_code)
        self.assertEqual({'persona/dh_1/abel_1': {'status': 'callback'}}, json.loads(self.marks.read_text()))

    def test_clone_config_for_a_voice_with_audio_and_refusals(self):
        body = self.client.get('/api/casting/clone_config',
                               params={'voice_id': 'persona/dh_1/abel_1', 'speaker': 'ADAM'}).json()
        self.assertEqual('clone', body['config']['type'])
        self.assertEqual('designed_voices/persona/dh_1/abel_1/preview.wav', body['config']['ref_audio'])
        self.assertEqual(409, self.client.get('/api/casting/clone_config',
                                              params={'voice_id': 'persona/dh_1/mute_1', 'speaker': 'ADAM'}).status_code)
        self.assertEqual(404, self.client.get('/api/casting/clone_config',
                                              params={'voice_id': 'persona/dh_1/nobody', 'speaker': 'ADAM'}).status_code)
        self.assertEqual(400, self.client.get('/api/casting/clone_config',
                                              params={'voice_id': '../x', 'speaker': 'ADAM'}).status_code)


if __name__ == '__main__':
    unittest.main()
