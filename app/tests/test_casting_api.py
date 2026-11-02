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
        app = FastAPI()
        app.include_router(casting.router)
        self.client = stack.enter_context(TestClient(app))

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
