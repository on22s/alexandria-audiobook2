"""Real HTTP transport of a maximum Unicode quote, without model inference."""
import json
import socket
import threading
import time
import unittest
from unittest.mock import patch

import httpx
import uvicorn
from fastapi import FastAPI
from routers import character_roster as api
from tests import test_character_roster_snapshot as fixtures


class CharacterRosterTransportTests(unittest.TestCase):
    setUp = fixtures.CharacterRosterSnapshotTests.setUp

    def test_maximum_unicode_quote_crosses_real_fragmented_http_body(self):
        quote = '😀' * 4000
        self.source.write_text('prefix ' + quote + ' suffix', encoding='utf-8')
        application = FastAPI(); application.include_router(api.router)
        listener = socket.socket()
        listener.bind(('127.0.0.1', 0)); listener.listen(10)
        port = listener.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(application, host='127.0.0.1', port=0,
                                              http='h11', log_level='error'))
        with patch.object(api.core, 'DATA_DIR', str(self.root)), patch.object(api.core, 'process_state', {}):
            thread = threading.Thread(target=server.run, kwargs={'sockets': [listener]}, daemon=True)
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started and thread.is_alive() and time.monotonic() < deadline:
                    time.sleep(.01)
                self.assertTrue(server.started, 'HTTP fixture did not become ready')
                with httpx.Client(base_url=f'http://127.0.0.1:{port}', timeout=5) as client:
                    metadata = client.get('/api/character_roster')
                    self.assertEqual(200, metadata.status_code, metadata.text)
                    body = json.dumps({'context_revision': metadata.json()['context_revision'],
                                       'quote': quote, 'offset': 0}, ensure_ascii=False).encode('utf-8')
                    # Deliberately split UTF-8 sequences and use chunked transfer,
                    # reaching the real server/body validator rather than ASGITransport.
                    result = client.post('/api/character_roster/source',
                        content=(body[i:i+997] for i in range(0, len(body), 997)),
                        headers={'Content-Type': 'application/json'})
                    self.assertEqual(200, result.status_code, result.text)
                    match = result.json()['matches'][0]
                    self.assertEqual((7, 4007, quote),
                                     (match['start_char'], match['end_char'], match['source_quote']))
                    stale = client.post('/api/character_roster/source', json={
                        'context_revision': '0' * 64, 'quote': quote, 'offset': 0})
                    self.assertEqual(409, stale.status_code)
            finally:
                server.should_exit = True
                thread.join(10); listener.close()
                self.assertFalse(thread.is_alive(), 'HTTP fixture did not shut down')
