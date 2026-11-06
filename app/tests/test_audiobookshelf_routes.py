"""Audiobookshelf routes: secret storage, server-bound preview/send, and status (#1727)."""
import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from export_quality import get_export_file_version
from routers import editor
from tests.test_audiobookshelf_delivery import FakeKeyring, RawAbsServer
import audiobookshelf_delivery as delivery


class AbsRouteTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name
        self.app = FastAPI()
        self.app.include_router(editor.router)
        self.fake = FakeKeyring()
        self.server = RawAbsServer().start()
        self.base = f'http://127.0.0.1:{self.server.port}'
        self.book_token = 'a' * 64
        self.export_path = os.path.join(self.data_dir, 'cloned_audiobook.mp3')
        # 200 KiB synthetic payload: exercises real transport above 64 KiB.
        self.payload = (b'synthetic-audiobook-bytes-' * 1) * 8500
        Path(self.export_path).write_bytes(self.payload)
        self.file_version = get_export_file_version(self.export_path)
        self.sha256 = hashlib.sha256(self.payload).hexdigest()
        self.report = {
            'status': 'verified', 'file': 'cloned_audiobook.mp3',
            'file_version': self.file_version, 'sha256': self.sha256,
            'output_bytes': len(self.payload), 'input_revision': 'rev_1',
            'input_binding': 'current', 'book_token': self.book_token,
        }

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def _client(self):
        return TestClient(self.app), patch.object(editor, 'DATA_DIR', self.data_dir), \
            patch.object(delivery, '_get_keyring', return_value=self.fake)

    def _flow_patches(self):
        import export_quality_session
        return [
            patch.object(editor, '_abs_book_check', lambda token: None),
            patch.object(delivery, 'get_report_for_export', return_value=self.report),
            patch.object(export_quality_session, 'get_export_input_revision',
                         return_value={'revision': 'rev_1', 'scope': 'full'}),
            patch('project_scope_exports.require_scope_export_current', return_value=None),
        ]

    def _preview_body(self):
        return {'expected_book_token': self.book_token, 'export_file': 'cloned_audiobook.mp3',
                'library_id': 'lib_1', 'folder_id': 'fol_1', 'title': 'Route Book',
                'author': '', 'series': '', 'base_url': self.base}

    def test_config_stores_key_in_credential_store_only(self):
        client, patch_dir, patch_keyring = self._client()
        with patch_dir, patch_keyring, client:
            response = client.post('/api/abs/config',
                                   json={'base_url': self.base, 'api_key': 'route-secret'})
            self.assertEqual(200, response.status_code)
            body = response.json()
            self.assertTrue(body['has_key'])
            self.assertNotIn('route-secret', response.text)
            self.assertEqual(body['key_store'], 'OS credential store')
            # The config file on disk must not contain the key.
            stored = Path(self.data_dir, delivery.CONFIG_NAME).read_text()
            self.assertNotIn('route-secret', stored)
            self.assertEqual('route-secret', self.fake.get_password(
                delivery.CREDENTIAL_SERVICE, self.base))
            fetched = client.get('/api/abs/config')
            self.assertTrue(fetched.json()['has_key'])
            self.assertNotIn('route-secret', fetched.text)

    def test_config_refuses_credential_in_url(self):
        client, patch_dir, patch_keyring = self._client()
        with patch_dir, patch_keyring, client:
            response = client.post('/api/abs/config',
                                   json={'base_url': 'http://user:pass@host:13378',
                                         'api_key': 'route-secret'})
            self.assertEqual(400, response.status_code)
            self.assertNotIn('route-secret', response.text)

    def test_libraries_listing_uses_stored_key(self):
        client, patch_dir, patch_keyring = self._client()
        with patch_dir, patch_keyring, client:
            client.post('/api/abs/config',
                        json={'base_url': self.base, 'api_key': 'route-secret'})
            response = client.get('/api/abs/libraries')
            self.assertEqual(200, response.status_code)
            self.assertEqual('lib_1', response.json()['libraries'][0]['id'])
            # The key travelled in the Authorization header only.
            method, path, header = self.server.requests_seen[-1]
            self.assertEqual('Bearer route-secret', header)
            self.assertNotIn('route-secret', path)

    def test_full_preview_send_deliveries_flow(self):
        client, patch_dir, patch_keyring = self._client()
        patches = self._flow_patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch_dir, patch_keyring, client:
            saved = client.post('/api/abs/config',
                                json={'base_url': self.base, 'api_key': 'route-secret'})
            self.assertEqual(200, saved.status_code)
            preview = client.post('/api/abs/preview', json=self._preview_body())
            self.assertEqual(200, preview.status_code, preview.text)
            body = preview.json()
            self.assertEqual(self.base, body['base_url'])
            self.assertEqual(self.sha256, body['sha256'])
            self.assertEqual(len(self.payload), body['output_bytes'])
            self.assertNotIn('route-secret', preview.text)
            send_body = dict(self._preview_body())
            send_body.update({'preview_id': body['preview_id'], 'right_to_share': True,
                              'acknowledge_unknown': False})
            sent = client.post('/api/abs/send', json=send_body)
            self.assertEqual(200, sent.status_code, sent.text)
            entry = sent.json()
            self.assertEqual('unknown', entry['status'])
            self.assertTrue(entry.get('verification_pending'))
            self.assertIsInstance(entry.get('upload_accepted_at'), float)
            self.assertEqual(self.base, entry['server'])
            # Full bytes reached the server (payload is 200 KiB, above 64 KiB).
            self.assertTrue(any(self.payload in blob for blob in self.server.received_uploads))
            parsed = self.server.parsed_uploads[-1]
            self.assertIsNotNone(parsed, 'real parser must recover the file part')
            self.assertEqual(self.payload, parsed['file'])
            self.assertEqual(self.sha256, parsed['file_sha256'])
            self.assertEqual(b'Route Book', parsed['fields'][b'title'])
            deliveries = client.get('/api/abs/deliveries',
                                    params={'expected_book_token': self.book_token})
            self.assertEqual(200, deliveries.status_code)
            records = deliveries.json()['deliveries']
            self.assertEqual(1, len(records))
            self.assertEqual('unknown', records[0]['status'])
            self.assertTrue(records[0].get('verification_pending'))
            progress = client.get('/api/abs/progress',
                                  params={'preview_id': body['preview_id']})
            self.assertEqual(200, progress.status_code)
            self.assertIn('progress', progress.json())

    def test_preview_rejects_server_mismatch(self):
        client, patch_dir, patch_keyring = self._client()
        patches = self._flow_patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch_dir, patch_keyring, client:
            client.post('/api/abs/config', json={'base_url': self.base, 'api_key': 'route-secret'})
            wrong = dict(self._preview_body())
            wrong['base_url'] = 'http://127.0.0.1:65534'
            response = client.post('/api/abs/preview', json=wrong)
            self.assertEqual(409, response.status_code)

    def test_preview_rejects_unknown_library_before_upload(self):
        client, patch_dir, patch_keyring = self._client()
        patches = self._flow_patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch_dir, patch_keyring, client:
            client.post('/api/abs/config', json={'base_url': self.base, 'api_key': 'route-secret'})
            body = dict(self._preview_body())
            body['library_id'] = 'lib_missing'
            response = client.post('/api/abs/preview', json=body)
            self.assertEqual(400, response.status_code)
            uploads = [path for _m, path, _h in self.server.requests_seen
                       if path.split('?', 1)[0] == '/api/upload']
            self.assertEqual([], uploads)

    def test_send_requires_right_to_share(self):
        client, patch_dir, patch_keyring = self._client()
        patches = self._flow_patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch_dir, patch_keyring, client:
            client.post('/api/abs/config', json={'base_url': self.base, 'api_key': 'route-secret'})
            preview = client.post('/api/abs/preview', json=self._preview_body())
            body = dict(self._preview_body())
            body.update({'preview_id': preview.json()['preview_id'], 'right_to_share': False})
            response = client.post('/api/abs/send', json=body)
            self.assertEqual(400, response.status_code)

    def test_send_unknown_stays_unknown_after_reconcile(self):
        self.server.mode = 'server-error'
        client, patch_dir, patch_keyring = self._client()
        patches = self._flow_patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch_dir, patch_keyring, client:
            client.post('/api/abs/config', json={'base_url': self.base, 'api_key': 'route-secret'})
            preview = client.post('/api/abs/preview', json=self._preview_body())
            body = dict(self._preview_body())
            body.update({'preview_id': preview.json()['preview_id'], 'right_to_share': True})
            sent = client.post('/api/abs/send', json=body)
            self.assertEqual(200, sent.status_code, sent.text)
            entry = sent.json()
            self.assertEqual('unknown', entry['status'])
            self.server.items = [{'id': 'li_9', 'folderId': 'fol_1',
                                 'media': {'size': len(self.payload),
                                           'metadata': {'title': 'Route Book'}}}]
            reconciled = client.post('/api/abs/reconcile', json={'delivery_id': entry['id']})
            self.assertEqual(200, reconciled.status_code, reconciled.text)
            updated = reconciled.json()
            self.assertEqual('unknown', updated['status'])
            self.assertIn('REMAINS unknown', updated['reconciliation'])

    def test_cancel_without_active_upload_is_safe(self):
        client, patch_dir, patch_keyring = self._client()
        patches = self._flow_patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch_dir, patch_keyring, client:
            response = client.post('/api/abs/cancel', json={'preview_id': 'no-such-preview'})
            self.assertEqual(200, response.status_code)
            self.assertFalse(response.json()['cancelling'])

    def test_unexpected_fields_rejected(self):
        client, patch_dir, patch_keyring = self._client()
        patches = self._flow_patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch_dir, patch_keyring, client:
            client.post('/api/abs/config', json={'base_url': self.base, 'api_key': 'route-secret'})
            body = dict(self._preview_body())
            body['unexpected_field'] = 'boom'
            response = client.post('/api/abs/preview', json=body)
            self.assertEqual(422, response.status_code)

    def test_config_rejects_nonstring_api_key_without_echo(self):
        client, patch_dir, patch_keyring = self._client()
        patches = self._flow_patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch_dir, patch_keyring, client:
            for malicious in ({'token': 'supersecretvalue'}, ['secret', 'abc'], 12345):
                response = client.post('/api/abs/config',
                                       json={'base_url': self.base, 'api_key': malicious})
                self.assertEqual(400, response.status_code, response.text)
                self.assertNotIn('supersecretvalue', response.text)
                self.assertNotIn('secret', response.text)
                self.assertIn('must be a string', response.text)

    def test_full_preview_send_uses_real_parsed_file(self):
        client, patch_dir, patch_keyring = self._client()
        patches = self._flow_patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch_dir, patch_keyring, client:
            client.post('/api/abs/config', json={'base_url': self.base, 'api_key': 'route-secret'})
            preview = client.post('/api/abs/preview', json=self._preview_body())
            self.assertEqual(200, preview.status_code, preview.text)
            body = dict(self._preview_body())
            body.update({'preview_id': preview.json()['preview_id'], 'right_to_share': True})
            sent = client.post('/api/abs/send', json=body)
            self.assertEqual(200, sent.status_code, sent.text)
            self.assertEqual('unknown', sent.json()['status'])
            self.assertTrue(sent.json().get('verification_pending'))
            parsed = self.server.parsed_uploads[-1]
            self.assertIsNotNone(parsed)
            self.assertEqual(self.payload, parsed['file'])
            self.assertEqual(self.sha256, parsed['file_sha256'])
            self.assertEqual(len(self.payload), len(parsed['file']))
            self.assertEqual(b'Route Book', parsed['fields'][b'title'])
            self.assertEqual(b'lib_1', parsed['fields'][b'library'])
            self.assertEqual(b'fol_1', parsed['fields'][b'folder'])

    def test_reconcile_promotes_on_exact_bytes_via_api(self):
        self.server.mode = 'server-error'
        client, patch_dir, patch_keyring = self._client()
        patches = self._flow_patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch_dir, patch_keyring, client:
            client.post('/api/abs/config', json={'base_url': self.base, 'api_key': 'route-secret'})
            preview = client.post('/api/abs/preview', json=self._preview_body())
            body = dict(self._preview_body())
            body.update({'preview_id': preview.json()['preview_id'], 'right_to_share': True})
            sent = client.post('/api/abs/send', json=body)
            self.assertEqual(200, sent.status_code, sent.text)
            entry = sent.json()
            self.assertEqual('unknown', entry['status'])
            self.server.mode = 'ok'
            self.server.items = [{'id': 'li_route', 'libraryId': 'lib_1', 'folderId': 'fol_1',
                                  'media': {'size': len(self.payload),
                                            'metadata': {'title': 'Route Book'}}}]
            self.server.item_details = {'li_route': {
                'id': 'li_route', 'libraryId': 'lib_1', 'folderId': 'fol_1',
                'mediaType': 'book',
                'media': {'metadata': {'title': 'Route Book'},
                          'audioFiles': [{'ino': 'ino_route',
                                          'metadata': {'size': len(self.payload)}}]},
                'libraryFiles': [{'ino': 'ino_route',
                                  'metadata': {'filename': 'cloned_audiobook.mp3',
                                               'size': len(self.payload)}}]}}
            self.server.file_blobs = {('li_route', 'ino_route'): self.payload}
            reconciled = client.post('/api/abs/reconcile', json={'delivery_id': entry['id']})
            self.assertEqual(200, reconciled.status_code, reconciled.text)
            updated = reconciled.json()
            self.assertEqual('sent', updated['status'])
            self.assertEqual(self.sha256, updated['reconciled_sha256'])
            self.assertEqual('li_route', updated['reconciled_item_id'])
            deliveries = client.get('/api/abs/deliveries',
                                    params={'expected_book_token': self.book_token})
            self.assertEqual('sent', deliveries.json()['deliveries'][0]['status'])

    def test_accepted_upload_verify_flow_via_routes(self):
        # N6 route flow: HTTP 200 with nothing persisted must never read
        # as sent; only an explicit verify with exact server bytes promotes.
        self.server.mode = 'accepted-no-persist'
        client, patch_dir, patch_keyring = self._client()
        patches = self._flow_patches()
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        with patch_dir, patch_keyring, client:
            client.post('/api/abs/config', json={'base_url': self.base, 'api_key': 'route-secret'})
            preview = client.post('/api/abs/preview', json=self._preview_body())
            self.assertEqual(200, preview.status_code, preview.text)
            body = dict(self._preview_body())
            body.update({'preview_id': preview.json()['preview_id'], 'right_to_share': True})
            before = Path(self.export_path).read_bytes()
            sent = client.post('/api/abs/send', json=body)
            self.assertEqual(200, sent.status_code, sent.text)
            entry = sent.json()
            self.assertEqual('unknown', entry['status'])
            self.assertTrue(entry.get('verification_pending'))
            self.assertIsInstance(entry.get('upload_accepted_at'), float)
            parsed = self.server.parsed_uploads[-1]
            self.assertIsNotNone(parsed)
            self.assertEqual(self.payload, parsed['file'])
            # Verify with no candidate: still unknown, never sent.
            self.server.mode = 'ok'
            self.server.items = []
            first = client.post('/api/abs/reconcile', json={'delivery_id': entry['id']})
            self.assertEqual(200, first.status_code, first.text)
            self.assertEqual('unknown', first.json()['status'])
            self.assertIn('cannot prove', first.json()['reconciliation'])
            # Verify with the exact persisted file: only now sent, with proof.
            self.server.items = [{'id': 'li_route', 'libraryId': 'lib_1', 'folderId': 'fol_1',
                                  'media': {'size': len(self.payload),
                                            'metadata': {'title': 'Route Book'}}}]
            self.server.item_details = {'li_route': {
                'id': 'li_route', 'libraryId': 'lib_1', 'folderId': 'fol_1',
                'mediaType': 'book',
                'media': {'metadata': {'title': 'Route Book'},
                          'audioFiles': [{'ino': 'ino_route',
                                          'metadata': {'size': len(self.payload)}}]},
                'libraryFiles': [{'ino': 'ino_route',
                                  'metadata': {'filename': 'cloned_audiobook.mp3',
                                               'size': len(self.payload)}}]}}
            self.server.file_blobs = {('li_route', 'ino_route'): self.payload}
            second = client.post('/api/abs/reconcile', json={'delivery_id': entry['id']})
            self.assertEqual(200, second.status_code, second.text)
            updated = second.json()
            self.assertEqual('sent', updated['status'])
            self.assertFalse(updated.get('verification_pending'))
            self.assertEqual(self.sha256, updated['reconciled_sha256'])
            self.assertEqual(before, Path(self.export_path).read_bytes())
            deliveries = client.get('/api/abs/deliveries',
                                    params={'expected_book_token': self.book_token})
            self.assertEqual('sent', deliveries.json()['deliveries'][0]['status'])


if __name__ == '__main__':
    unittest.main()
