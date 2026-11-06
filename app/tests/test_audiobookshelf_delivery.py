"""Audiobookshelf explicit delivery (#1727): fake-HTTP transport, identity, ledger.

All network traffic goes to a real local raw-socket HTTP/1.1 server
no live Audiobookshelf server is contacted. Fixtures are synthetic bytes.
"""
import hashlib
import io
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent))
import audiobookshelf_delivery as delivery
from export_quality import get_export_file_version


class FakeKeyring:
    def __init__(self):
        self.store = {}

    def set_password(self, service, account, value):
        self.store[(service, account)] = value

    def get_password(self, service, account):
        return self.store.get((service, account))

    def delete_password(self, service, account):
        self.store.pop((service, account), None)


class AbsHandler:
    mode = 'ok'
    received_uploads = []
    requests_seen = []
    items = []


class RawAbsServer:
    """Minimal HTTP/1.1 test double on a raw socket.

    The stdlib ``ThreadingHTTPServer`` on Windows intermittently resets
    loopback connections with rapid fresh connections (~1 in 7 measured),
    which would make these tests flaky. A raw socket with explicit framing
    and a graceful close (FIN after the response drains, never an RST) is
    deterministic here and closer to a real Node server's behavior.

    Upload bodies are parsed with the real ``python_multipart`` parser and
    stored in ``parsed_uploads`` so tests can assert the exact parsed file
    SHA-256/length and form fields instead of raw substrings. Accepted
    sockets are tracked and closed in a ``finally``; ``stop()`` joins the
    accept and handler threads and raises when a handler recorded an error.
    Header and body reads are bounded. All fixtures are synthetic; no live
    Audiobookshelf server is contacted.
    """

    _MAX_HEADER_BYTES = 65536
    _MAX_BODY_BYTES = 16 * 1024 * 1024

    def __init__(self, host='127.0.0.1'):
        import socket
        self.socket_module = socket
        self.listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.listener.bind((host, 0))
        self.listener.listen(128)
        self.port = self.listener.getsockname()[1]
        self.mode = 'ok'
        self.received_uploads = []
        self.parsed_uploads = []
        self.requests_seen = []
        self.items = []
        self.item_details = {}
        self.file_blobs = {}
        self.download_mode = 'ok'
        self.item_mode = 'ok'
        self.errors = []
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._handlers = set()
        self.thread = threading.Thread(target=self._serve, daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self._stop.set()
        try:
            self.listener.close()
        except OSError:
            pass
        self.thread.join(timeout=10)
        with self._lock:
            handlers = list(self._handlers)
        for handler in handlers:
            handler.join(timeout=10)
        if self.errors:
            raise AssertionError(f'RawAbsServer errors: {self.errors}')

    def _record_error(self, label, error):
        with self._lock:
            self.errors.append(f'{label}: {type(error).__name__}: {error}')

    def _serve(self):
        try:
            self.listener.settimeout(0.5)
        except OSError:
            return
        while not self._stop.is_set():
            try:
                connection, _addr = self.listener.accept()
            except self.socket_module.timeout:
                continue
            except OSError as error:
                if self._stop.is_set():
                    break
                self._record_error('accept', error)
                continue
            try:
                connection.setsockopt(self.socket_module.IPPROTO_TCP, self.socket_module.TCP_NODELAY, 1)
            except OSError:
                pass
            handler = threading.Thread(target=self._handle, args=(connection,), daemon=True)
            with self._lock:
                self._handlers.add(handler)
            handler.start()

    def _handle(self, connection):
        response_sent = False
        try:
            connection.settimeout(10)
            raw = b''
            while b'\r\n\r\n' not in raw:
                if len(raw) > self._MAX_HEADER_BYTES:
                    self._record_error('header', ValueError(f'header exceeds {self._MAX_HEADER_BYTES} bytes'))
                    return
                block = connection.recv(65536)
                if not block:
                    return
                raw += block
            head, rest = raw.split(b'\r\n\r\n', 1)
            lines = head.decode('iso-8859-1').split('\r\n')
            method, path, _version = lines[0].split(' ', 2)
            headers = {}
            for line in lines[1:]:
                if ': ' in line:
                    key, value = line.split(': ', 1)
                    headers[key.lower()] = value
            total = int(headers.get('content-length') or 0)
            if total > self._MAX_BODY_BYTES:
                self._record_error('body', ValueError(f'body exceeds {self._MAX_BODY_BYTES} bytes'))
                return
            body = rest
            while len(body) < total:
                block = connection.recv(min(65536, total - len(body)))
                if not block:
                    break
                body += block
            body = body[:total]
            with self._lock:
                self.requests_seen.append((method, path, headers.get('authorization')))
            status, payload = self._dispatch(method, path, body, headers)
            if status is None:  # close without a response (interrupted/stall)
                return
            response = payload
            connection.sendall(response)
            response_sent = True
            connection.settimeout(0.5)
            try:
                while connection.recv(65536):
                    pass
            except OSError:
                pass
        except Exception as error:
            if not response_sent and not isinstance(error, (ConnectionResetError, BrokenPipeError)):
                self._record_error('handle', error)
        finally:
            try:
                import struct
                connection.setsockopt(self.socket_module.SOL_SOCKET, self.socket_module.SO_LINGER,
                                        struct.pack('ii', 1, 5))
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
            with self._lock:
                self._handlers.discard(threading.current_thread())

    def _json(self, code, reason, payload):
        body = json.dumps(payload).encode()
        head = (f'HTTP/1.1 {code} {reason}\r\nContent-Type: application/json\r\n'
                f'Content-Length: {len(body)}\r\nConnection: close\r\n\r\n')
        return head.encode() + body

    def _bytes_response(self, code, reason, body, extra_headers=None):
        headers = (extra_headers or [])
        head = (f'HTTP/1.1 {code} {reason}\r\nContent-Type: application/octet-stream\r\n'
                f'Content-Length: {len(body)}\r\nConnection: close\r\n')
        for header in headers:
            head += f'{header}\r\n'
        head += '\r\n'
        return head.encode('iso-8859-1') + body

    def _parse_multipart(self, body, headers):
        """Parse one upload body with the real parser; None when unparsable."""
        from python_multipart import parse_form
        content_type = headers.get('content-type') or ''
        if 'boundary=' not in content_type:
            self._record_error('multipart', ValueError('missing boundary in content-type'))
            return None
        fields = {}
        files = {}
        try:
            def on_field(field):
                fields[field.field_name] = field.value

            def on_file(file):
                file.file_object.seek(0)
                files[file.field_name] = file.file_object.read()
                file.file_object.seek(0)

            parsed_files = []
            def on_file_track(file):
                parsed_files.append(file)
                on_file(file)

            parse_form({'Content-Type': content_type.encode(), 'Content-Length': str(len(body)).encode()},
                       io.BytesIO(body), on_field, on_file_track, chunk_size=65536)
            for file in parsed_files:
                try:
                    file.close()
                except Exception:
                    pass
        except Exception as error:
            self._record_error('multipart', error)
            return None
        file_bytes = files.get(b'file')
        return {'fields': fields, 'file': file_bytes,
                'file_sha256': hashlib.sha256(file_bytes).hexdigest() if file_bytes is not None else None,
                'content_type': content_type}

    def _dispatch(self, method, path, body, headers):
        # Strip query for routing; handle paginated items listing.
        query_path = path.split('?', 1)[0]
        page = 0
        limit = 0
        if '?' in path:
            from urllib.parse import parse_qs
            params = parse_qs(path.split('?', 1)[1])
            try:
                page = int((params.get('page') or ['0'])[0])
            except ValueError:
                page = 0
            try:
                limit = int((params.get('limit') or ['0'])[0])
            except ValueError:
                limit = 0
        if method == 'GET' and query_path == '/api/libraries':
            return 200, self._json(200, 'OK', {'libraries': [
                {'id': 'lib_1', 'name': 'Main', 'folders': [
                    {'id': 'fol_1', 'fullPath': '/books'}]}]})
        if method == 'GET' and query_path == '/api/libraries/lib_1/items':
            with self._lock:
                items = list(self.items)
            if limit:
                start = page * limit
                window = items[start:start + limit]
            else:
                window = items
            return 200, self._json(200, 'OK', {'results': window, 'total': len(items),
                                              'limit': limit, 'page': page})
        if method == 'GET' and query_path.startswith('/api/items/'):
            from urllib.parse import unquote
            segments = [unquote(part) for part in query_path.split('/')]
            # ['','api','items', item_id, ...]
            if len(segments) == 4:
                item_id = segments[3]
                if self.item_mode == 'forbidden':
                    return 403, self._json(403, 'Forbidden', {'error': 'no access'})
                if self.item_mode == 'notfound':
                    return 404, self._json(404, 'Not Found', {})
                if self.item_mode == 'redirect':
                    head = ('HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:9/evil\r\n'
                            'Content-Length: 0\r\nConnection: close\r\n\r\n')
                    return 302, head.encode()
                if self.item_mode == 'server-error':
                    return 500, self._json(500, 'Internal Server Error', {'error': 'boom'})
                with self._lock:
                    detail = self.item_details.get(item_id)
                if detail is None:
                    return 404, self._json(404, 'Not Found', {})
                return 200, self._json(200, 'OK', detail)
            if len(segments) == 7 and segments[4] == 'file' and segments[6] == 'download':
                item_id, file_id = segments[3], segments[5]
                if self.download_mode == 'forbidden':
                    return 403, self._json(403, 'Forbidden', {'error': 'no download permission'})
                if self.download_mode == 'notfound':
                    return 404, self._json(404, 'Not Found', {})
                if self.download_mode == 'xaccel':
                    head = ('HTTP/1.1 204 No Content\r\nX-Accel-Redirect: /internal/file\r\n'
                            'Content-Length: 0\r\nConnection: close\r\n\r\n')
                    return 204, head.encode()
                if self.download_mode == 'redirect':
                    head = ('HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:9/evil\r\n'
                            'Content-Length: 0\r\nConnection: close\r\n\r\n')
                    return 302, head.encode()
                if self.download_mode == 'server-error':
                    return 500, self._json(500, 'Internal Server Error', {'error': 'boom'})
                if self.download_mode == 'unauthorized':
                    return 401, self._json(401, 'Unauthorized', {'error': 'unauthorized'})
                with self._lock:
                    blob = self.file_blobs.get((item_id, file_id))
                if blob is None:
                    return 404, self._json(404, 'Not Found', {})
                return 200, self._bytes_response(
                    200, 'OK', blob,
                    [f'Content-Disposition: attachment; filename="audio.mp3"'])
        if method != 'POST' or path != '/api/upload':
            return 404, self._json(404, 'Not Found', {})
        if self.mode == 'auth-fail':
            return 401, self._json(401, 'Unauthorized', {'error': 'unauthorized'})
        if self.mode == 'forbidden':
            return 403, self._json(403, 'Forbidden', {'error': 'no upload permission'})
        if self.mode == 'wrong-library':
            return 404, self._json(404, 'Not Found', {'error': 'no such library'})
        if self.mode == 'server-error':
            with self._lock:
                self.received_uploads.append(body)
                self.parsed_uploads.append(self._parse_multipart(body, headers))
            return 500, self._json(500, 'Internal Server Error', {'error': 'upload directory exists'})
        if self.mode == 'redirect':
            head = ('HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1:9/evil\r\n'
                    'Content-Length: 0\r\nConnection: close\r\n\r\n')
            return 302, head.encode()
        if self.mode == 'interrupted':
            with self._lock:
                self.received_uploads.append(body[: max(0, len(body) // 2)])
            return None, None
        if self.mode == 'stall':
            deadline = time.monotonic() + 8
            while time.monotonic() < deadline and not self._stop.is_set():
                time.sleep(0.1)
            if self._stop.is_set():
                return None, None
            with self._lock:
                self.received_uploads.append(body)
                self.parsed_uploads.append(self._parse_multipart(body, headers))
            return 200, self._json(200, 'OK', {'success': True})
        if self.mode == 'accepted-no-persist':
            # Models the pinned server's MiscController.handleUpload: it
            # receives and parses the full valid multipart, then the
            # file.mv() move fails internally (caught -> false, ignored) --
            # yet it still replies HTTP 200. Crucially it creates NO item,
            # item detail, or file blob: nothing is persisted to verify.
            with self._lock:
                self.received_uploads.append(body)
                self.parsed_uploads.append(self._parse_multipart(body, headers))
            return 200, self._json(200, 'OK', {'success': True})
        with self._lock:
            self.received_uploads.append(body)
            self.parsed_uploads.append(self._parse_multipart(body, headers))
        return 200, self._json(200, 'OK', {'success': True})


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.server = RawAbsServer().start()
        self.base = f'http://127.0.0.1:{self.server.port}'
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name
        self.book_token = 'a' * 64
        self.export_path = os.path.join(self.data_dir, 'cloned_audiobook.mp3')
        self.payload = b'synthetic-audiobook-bytes-' * 40
        Path(self.export_path).write_bytes(self.payload)
        self.file_version = get_export_file_version(self.export_path)
        self.sha256 = hashlib.sha256(self.payload).hexdigest()
        self.report = {
            'status': 'verified', 'file': 'cloned_audiobook.mp3',
            'file_version': self.file_version, 'sha256': self.sha256,
            'output_bytes': len(self.payload), 'input_revision': 'rev_1',
            'input_binding': 'current', 'book_token': self.book_token,
        }
        self.states = {'audio': {'export_quality_reports': [self.report]}}

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def _patch_identity(self):
        stack = patch.object(delivery, 'get_report_for_export', return_value=self.report)
        import export_quality_session
        stack2 = patch.object(export_quality_session, 'get_export_input_revision',
                              return_value={'revision': 'rev_1', 'scope': 'full'})
        stack.start()
        stack2.start()
        self.addCleanup(stack.stop)
        self.addCleanup(stack2.stop)

    def _preview(self, **kwargs):
        self._patch_identity()
        delivery.save_server_config(self.data_dir, self.base)
        return delivery.save_preview(self.data_dir, self.states, self.book_token,
                                     'cloned_audiobook.mp3', 'lib_1', 'fol_1',
                                     'Test Book', 'Test Author', '',
                                     base_url=self.base, token='fake-token', **kwargs)

    def _confirm(self, preview):
        return {'book_token': self.book_token, 'export_file': 'cloned_audiobook.mp3',
                'library_id': 'lib_1', 'folder_id': 'fol_1', 'title': 'Test Book',
                'author': 'Test Author', 'series': '', 'base_url': self.base}

    def _send(self, preview, **kwargs):
        return delivery.send_preview(self.data_dir, self.states, self.base,
                                     'fake-token', preview['preview_id'],
                                     self._confirm(preview), **kwargs)

    # --- credential store ----------------------------------------------------

    def test_keyring_store_roundtrip_and_no_plaintext_fallback(self):
        fake = FakeKeyring()
        with patch.object(delivery, '_get_keyring', return_value=fake):
            self.assertEqual('OS credential store', delivery.save_api_key(self.base, 'sekret'))
            self.assertEqual('sekret', delivery.get_api_key(self.base))
            delivery.clear_api_key(self.base)
            self.assertEqual('', delivery.get_api_key(self.base))

    def test_fail_closed_without_keyring_or_env(self):
        with patch.object(delivery, '_get_keyring', return_value=None), \
                patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(delivery.DeliveryError) as caught:
                delivery.save_api_key(self.base, 'sekret')
            self.assertEqual(503, caught.exception.status)
            self.assertEqual('', delivery.get_api_key(self.base))

    def test_env_fallback_is_explicit_only(self):
        with patch.object(delivery, '_get_keyring', return_value=None), \
                patch.dict(os.environ, {'ALEXANDRIA_ABS_API_KEY': 'env-secret',
                                        'ALEXANDRIA_ABS_SERVER_URL': self.base}):
            self.assertEqual('env-secret', delivery.get_api_key(self.base))

    def test_env_fallback_bound_to_one_server(self):
        other = 'http://127.0.0.1:9999'
        with patch.object(delivery, '_get_keyring', return_value=None), \
                patch.dict(os.environ, {'ALEXANDRIA_ABS_API_KEY': 'env-secret',
                                        'ALEXANDRIA_ABS_SERVER_URL': self.base}):
            self.assertEqual('', delivery.get_api_key(other))

    def test_config_file_never_contains_key(self):
        fake = FakeKeyring()
        with patch.object(delivery, '_get_keyring', return_value=fake):
            delivery.save_api_key(self.base, 'sekret')
            delivery.save_server_config(self.data_dir, self.base)
        config = Path(delivery._config_path(self.data_dir)).read_text()
        self.assertNotIn('sekret', config)
        self.assertNotIn('api_key', config)

    def test_base_url_rejects_embedded_credentials(self):
        with self.assertRaises(delivery.DeliveryError):
            delivery._validate_base_url('http://user:pass@host:13378')

    def test_redaction(self):
        self.assertTrue(delivery.is_secret_text('Bearer sekret failed', 'sekret'))
        self.assertEqual('Bearer [redacted] failed', delivery.redact_text('Bearer sekret failed', 'sekret'))

    # --- identity / preview ----------------------------------------------------

    def test_preview_binds_exact_identity(self):
        preview = self._preview()
        self.assertEqual(self.sha256, preview['sha256'])
        self.assertEqual(len(self.payload), preview['output_bytes'])
        self.assertEqual('rev_1', preview['input_revision'])
        self.assertEqual(self.file_version, preview['file_version'])

    def test_stale_artifact_rejected_before_send(self):
        preview = self._preview()
        self.server.requests_seen.clear()
        self.payload = b'replaced-bytes'
        Path(self.export_path).write_bytes(self.payload)
        self.file_version = get_export_file_version(self.export_path)
        # Report still describes the old file version -> 409, no upload bytes.
        with self.assertRaises(delivery.DeliveryError) as caught:
            self._send(preview)
        self.assertEqual(409, caught.exception.status)
        uploads = [path for _m, path, _h in self.server.requests_seen if path.split('?', 1)[0] == '/api/upload']
        self.assertEqual([], uploads)

    def test_mutated_destination_rejected(self):
        preview = self._preview()
        confirm = self._confirm(preview)
        confirm['library_id'] = 'lib_other'
        with self.assertRaises(delivery.DeliveryError) as caught:
            delivery.send_preview(self.data_dir, self.states, self.base, 'fake-token',
                                  preview['preview_id'], confirm)
        self.assertEqual(409, caught.exception.status)

    def test_replaced_book_revision_rejected(self):
        preview = self._preview()
        import export_quality_session
        with patch.object(export_quality_session, 'get_export_input_revision',
                          return_value={'revision': 'rev_2', 'scope': 'full'}):
            with self.assertRaises(delivery.DeliveryError) as caught:
                self._send(preview)
            self.assertEqual(409, caught.exception.status)

    def test_unbound_report_rejected(self):
        self.report['input_binding'] = 'unavailable'
        self._patch_identity()
        with self.assertRaises(delivery.DeliveryError) as caught:
            delivery.get_current_export_identity(self.data_dir, self.states, self.book_token,
                                                 'cloned_audiobook.mp3')
        self.assertEqual(409, caught.exception.status)

    # --- transport outcomes ------------------------------------------------------

    def test_success_upload_is_accepted_pending_verification(self):
        preview = self._preview()
        before = hashlib.sha256(Path(self.export_path).read_bytes()).hexdigest()
        entry = self._send(preview)
        # HTTP 200 is acceptance only (file.mv() failure is ignored by the
        # server and still returns 200), so the ledger must NOT say sent.
        self.assertEqual('unknown', entry['status'])
        self.assertTrue(entry.get('verification_pending'))
        self.assertIsInstance(entry.get('upload_accepted_at'), float)
        self.assertGreater(entry['upload_accepted_at'], 0)
        self.assertIn('not yet verified', entry.get('verification_note', ''))
        self.assertIn('Verify delivery', entry.get('verification_note', ''))
        self.assertEqual(1, len(self.server.received_uploads))
        self.assertEqual(1, len(self.server.parsed_uploads))
        # The file bytes travel inside the multipart body verbatim.
        self.assertIn(self.payload, self.server.received_uploads[0])
        parsed = self.server.parsed_uploads[0]
        self.assertIsNotNone(parsed, 'real parser must recover the file part')
        self.assertEqual(self.payload, parsed['file'])
        self.assertEqual(self.sha256, parsed['file_sha256'])
        self.assertEqual(len(self.payload), len(parsed['file']))
        self.assertEqual(b'Test Book', parsed['fields'][b'title'])
        self.assertEqual(b'Test Author', parsed['fields'][b'author'])
        self.assertEqual(b'lib_1', parsed['fields'][b'library'])
        self.assertEqual(b'fol_1', parsed['fields'][b'folder'])
        self.assertEqual(before, hashlib.sha256(Path(self.export_path).read_bytes()).hexdigest())
        # Credential travelled in the header only, never in a URL.
        for _method, path, header in self.server.requests_seen:
            self.assertNotIn('fake-token', path)
            self.assertEqual('Bearer fake-token', header)

    def test_auth_failure_is_failed_not_unknown(self):
        self.server.mode = 'auth-fail'
        entry = self._send(self._preview())
        self.assertEqual('failed', entry['status'])

    def test_forbidden_and_wrong_library_are_failed(self):
        for mode in ('forbidden', 'wrong-library'):
            self.server.mode = mode
            entry = self._send(self._preview())
            self.assertEqual('failed', entry['status'], mode)
        self.server.mode = 'ok'

    def test_server_error_is_unknown(self):
        self.server.mode = 'server-error'
        entry = self._send(self._preview())
        self.assertEqual('unknown', entry['status'])

    def test_interrupted_upload_is_unknown(self):
        self.server.mode = 'interrupted'
        entry = self._send(self._preview())
        self.assertEqual('unknown', entry['status'])
        self.assertEqual(len(self.payload),
                         Path(self.export_path).stat().st_size)

    def test_redirect_is_refused_and_not_followed(self):
        self.server.mode = 'redirect'
        entry = self._send(self._preview())
        self.assertEqual('unknown', entry['status'])
        with self.assertRaises(delivery.DeliveryError) as caught:
            self._send(self._preview())
        self.assertEqual(409, caught.exception.status)
        self.assertIn('redirect', entry['error'].lower())
        # Only the upload path was POSTed; nothing followed to the evil origin.
        uploads = [path for _m, path, _h in self.server.requests_seen if path.split('?', 1)[0] == '/api/upload']
        self.assertEqual(['/api/upload'], uploads)

    def test_cancel_is_unknown_but_locally_unchanged(self):
        preview = self._preview()
        cancel = threading.Event()
        cancel.set()
        with self.assertRaises(delivery.DeliveryError):
            delivery.upload_export(self.base, 'fake-token', preview,
                                   self.export_path, self.file_version, cancel_event=cancel)
        self.assertEqual(self.payload, Path(self.export_path).read_bytes())

    # --- ledger / duplicate gating ------------------------------------------------

    def test_restart_demotes_in_flight_to_unknown(self):
        # Simulate a restart: recovery runs again for a pre-process entry.
        delivery._recovered = False
        self.addCleanup(lambda: setattr(delivery, '_recovered', True))
        delivery.save_ledger_entry(self.data_dir, {'id': 'x', 'status': 'in_flight',
                                                  'created_at': 1, 'book_token': self.book_token,
                                                  'sha256': 's', 'library_id': 'lib_1',
                                                  'folder_id': 'fol_1', 'output_bytes': 1})
        ledger = delivery.get_ledger(self.data_dir)
        self.assertEqual('unknown', ledger[0]['status'])

    def test_recovery_requires_matching_byte_proof_for_legacy_sent(self):
        delivery._recovered = False
        self.addCleanup(lambda: setattr(delivery, '_recovered', True))
        base = {'status': 'sent', 'created_at': 1, 'book_token': self.book_token,
                'sha256': self.sha256, 'library_id': 'lib_1', 'folder_id': 'fol_1',
                'output_bytes': len(self.payload), 'server': self.base,
                'base_url': self.base, 'title': 'Test Book'}
        verified = dict(base, id='verified', reconciled_sha256=self.sha256,
                        reconciled_bytes=len(self.payload), reconciled_item_id='item_1',
                        reconciled_file_id='file_1', reconciled_verified_at=123)
        legacy = dict(base, id='legacy')
        wrong_hash = dict(verified, id='wrong_hash', reconciled_sha256='0' * 64)
        wrong_size = dict(verified, id='wrong_size', reconciled_bytes=len(self.payload) + 1)
        delivery.save_ledger(self.data_dir, [legacy, verified, wrong_hash, wrong_size])
        recovered = {entry['id']: entry for entry in delivery.get_ledger(self.data_dir)}
        self.assertEqual('sent', recovered['verified']['status'])
        for name in ('legacy', 'wrong_hash', 'wrong_size'):
            self.assertEqual('unknown', recovered[name]['status'])
            self.assertTrue(recovered[name]['verification_pending'])
            self.assertIn('Verify delivery', recovered[name]['verification_note'])
        persisted = json.loads(Path(delivery._ledger_path(self.data_dir)).read_text())
        self.assertEqual(recovered, {entry['id']: entry for entry in persisted})

    def test_accepted_blocks_duplicate_until_verified(self):
        self.server.mode = 'ok'
        entry = self._send(self._preview())
        self.assertEqual('unknown', entry['status'])
        self.assertTrue(entry.get('verification_pending'))
        with self.assertRaises(delivery.DeliveryError) as caught:
            self._send(self._preview())
        self.assertEqual(409, caught.exception.status)
        # Blocked as accepted-but-unverified, never as already delivered.
        self.assertIn('not yet verified', str(caught.exception))
        self.assertNotIn('already delivered', str(caught.exception))

    def test_unknown_blocks_retry_and_reconcile_stays_unknown(self):
        self.server.mode = 'server-error'
        self._send(self._preview())
        with self.assertRaises(delivery.DeliveryError) as caught:
            self._send(self._preview())
        self.assertIn('unknown', str(caught.exception))
        # Reconcile with no match: stays unknown; silent retry still blocked.
        self.server.items = []
        entry = delivery.get_latest_delivery(self.data_dir, self.book_token, self.sha256, 'lib_1', 'fol_1')
        reconciled = delivery.reconcile_delivery(self.data_dir, entry['id'], self.base, 'fake-token')
        self.assertEqual('unknown', reconciled['status'])
        # Acknowledgement can never override unresolved unknown.
        self.server.mode = 'ok'
        with self.assertRaises(delivery.DeliveryError) as caught:
            self._send(self._preview(), acknowledge_unknown=True)
        self.assertIn('unknown', str(caught.exception))

    def test_http200_without_persisted_file_never_becomes_sent(self):
        # N6 discriminating case: the fake server receives the full valid
        # multipart, replies 200, but persists nothing (file.mv() failure).
        self.server.mode = 'accepted-no-persist'
        before = Path(self.export_path).read_bytes()
        entry = self._send(self._preview())
        self.assertEqual('unknown', entry['status'])
        self.assertTrue(entry.get('verification_pending'))
        self.assertEqual(1, len(self.server.parsed_uploads))
        parsed = self.server.parsed_uploads[0]
        self.assertIsNotNone(parsed)
        self.assertEqual(self.payload, parsed['file'])
        self.assertEqual(self.sha256, parsed['file_sha256'])
        # Nothing was created server-side to verify against.
        self.assertEqual([], self.server.items)
        self.assertEqual({}, self.server.item_details)
        self.assertEqual({}, self.server.file_blobs)
        # Verify with an empty listing: stays unknown, never sent, no retry.
        self.server.requests_seen.clear()
        reconciled = delivery.reconcile_delivery(self.data_dir, entry['id'], self.base, 'fake-token')
        self.assertEqual('unknown', reconciled['status'])
        self.assertTrue(reconciled.get('verification_pending'))
        self.assertIn('cannot prove', reconciled['reconciliation'])
        uploads = [path for method, path, _ in self.server.requests_seen
                   if method == 'POST' and path.split('?', 1)[0] == '/api/upload']
        self.assertEqual([], uploads)
        self.assertEqual(before, Path(self.export_path).read_bytes())
        # Duplicate resend stays blocked (also with acknowledgement).
        with self.assertRaises(delivery.DeliveryError) as caught:
            self._send(self._preview(), acknowledge_unknown=True)
        self.assertEqual(409, caught.exception.status)

    def test_http200_then_verify_with_exact_bytes_becomes_sent(self):
        # Same accepted upload, but this time the server DID persist the
        # file: only an explicit verify with exact bytes may promote it.
        self.server.mode = 'accepted-no-persist'
        before = Path(self.export_path).read_bytes()
        entry = self._send(self._preview())
        self.assertEqual('unknown', entry['status'])
        accepted_at = entry['upload_accepted_at']
        self.server.mode = 'ok'
        self.server.items = [{'id': 'li_1', 'libraryId': 'lib_1', 'folderId': 'fol_1',
                              'media': {'size': len(self.payload),
                                        'metadata': {'title': 'Test Book'}}}]
        self.server.item_details = {'li_1': {
            'id': 'li_1', 'libraryId': 'lib_1', 'folderId': 'fol_1',
            'mediaType': 'book',
            'media': {'metadata': {'title': 'Test Book'},
                      'audioFiles': [{'ino': 'ino_1',
                                      'metadata': {'size': len(self.payload)}}]},
            'libraryFiles': [{'ino': 'ino_1',
                              'metadata': {'filename': 'cloned_audiobook.mp3',
                                           'size': len(self.payload)}}]}}
        self.server.file_blobs = {('li_1', 'ino_1'): self.payload}
        reconciled = delivery.reconcile_delivery(self.data_dir, entry['id'], self.base, 'fake-token')
        self.assertEqual('sent', reconciled['status'])
        self.assertFalse(reconciled.get('verification_pending'))
        self.assertEqual(accepted_at, reconciled.get('upload_accepted_at'))
        self.assertEqual(self.sha256, reconciled.get('reconciled_sha256'))
        self.assertIn('verified by raw bytes', reconciled['reconciliation'])
        self.assertEqual(before, Path(self.export_path).read_bytes())
        # After verified delivery the duplicate fence blocks a second send.
        with self.assertRaises(delivery.DeliveryError) as caught:
            self._send(self._preview())
        self.assertIn('already delivered', str(caught.exception))

    def test_reconcile_candidate_never_promotes_to_sent(self):
        self.server.mode = 'server-error'
        self._send(self._preview())
        self.server.items = [{'id': 'li_1', 'folderId': 'fol_1',
                             'media': {'size': len(self.payload),
                                       'metadata': {'title': 'Test Book'}}}]
        entry = delivery.get_latest_delivery(self.data_dir, self.book_token, self.sha256, 'lib_1', 'fol_1')
        reconciled = delivery.reconcile_delivery(self.data_dir, entry['id'], self.base, 'fake-token')
        self.assertEqual('unknown', reconciled['status'])
        self.assertIn('REMAINS unknown', reconciled['reconciliation'])
        self.assertIn('li_1', reconciled['reconciliation'])

    def test_reconcile_wrong_size_does_not_confirm(self):
        self.server.mode = 'server-error'
        self._send(self._preview())
        self.server.items = [{'id': 'li_1', 'folderId': 'fol_1',
                             'media': {'size': len(self.payload) + 1,
                                       'metadata': {'title': 'Test Book'}}}]
        entry = delivery.get_latest_delivery(self.data_dir, self.book_token, self.sha256, 'lib_1', 'fol_1')
        reconciled = delivery.reconcile_delivery(self.data_dir, entry['id'], self.base, 'fake-token')
        self.assertEqual('unknown', reconciled['status'])

    def test_libraries_listing_is_explicit(self):
        libraries = delivery.get_libraries(self.base, 'fake-token')
        self.assertEqual('lib_1', libraries[0]['id'])
        self.assertEqual('fol_1', libraries[0]['folders'][0]['id'])

    # --- server binding / destination validation -----------------------------------

    def test_preview_binds_server(self):
        preview = self._preview()
        self.assertEqual(self.base, preview['base_url'])

    def test_server_change_after_preview_invalidates_send(self):
        preview = self._preview()
        delivery.save_server_config(self.data_dir, 'http://127.0.0.1:65534')
        with self.assertRaises(delivery.DeliveryError) as caught:
            self._send(preview)
        self.assertEqual(409, caught.exception.status)

    def test_confirm_server_mismatch_rejected(self):
        preview = self._preview()
        confirm = self._confirm(preview)
        confirm['base_url'] = 'http://127.0.0.1:65534'
        with self.assertRaises(delivery.DeliveryError) as caught:
            delivery.send_preview(self.data_dir, self.states, self.base, 'fake-token',
                                  preview['preview_id'], confirm)
        self.assertEqual(409, caught.exception.status)

    def test_unknown_library_rejected_at_preview_before_upload(self):
        self._patch_identity()
        delivery.save_server_config(self.data_dir, self.base)
        with self.assertRaises(delivery.DeliveryError) as caught:
            delivery.save_preview(self.data_dir, self.states, self.book_token,
                                  'cloned_audiobook.mp3', 'lib_missing', 'fol_1',
                                  'Test Book', '', '', base_url=self.base, token='fake-token')
        self.assertEqual(400, caught.exception.status)
        uploads = [path for method, path, _ in self.server.requests_seen if path.startswith('/api/upload')]
        self.assertEqual([], uploads)

    def test_unknown_folder_rejected_at_preview_before_upload(self):
        self._patch_identity()
        delivery.save_server_config(self.data_dir, self.base)
        with self.assertRaises(delivery.DeliveryError) as caught:
            delivery.save_preview(self.data_dir, self.states, self.book_token,
                                  'cloned_audiobook.mp3', 'lib_1', 'fol_missing',
                                  'Test Book', '', '', base_url=self.base, token='fake-token')
        self.assertEqual(400, caught.exception.status)

    # --- ledger safety ----------------------------------------------------------------

    def test_live_in_flight_not_demoted_by_poll(self):
        delivery._recovered = True
        delivery._active_deliveries.add('live-1')
        self.addCleanup(lambda: delivery._active_deliveries.discard('live-1'))
        Path(delivery._ledger_path(self.data_dir)).write_text(json.dumps([{
            'id': 'live-1', 'status': 'in_flight', 'created_at': time.time(),
            'book_token': self.book_token, 'sha256': 's', 'library_id': 'lib_1',
            'folder_id': 'fol_1', 'output_bytes': 1}]))
        first = delivery.get_ledger(self.data_dir)
        second = delivery.get_ledger(self.data_dir)
        self.assertEqual('in_flight', first[0]['status'])
        self.assertEqual('in_flight', second[0]['status'])

    def test_corrupted_ledger_shape_is_tolerated(self):
        Path(delivery._ledger_path(self.data_dir)).write_text(json.dumps({'not': 'a list'}))
        self.assertEqual([], delivery.get_ledger(self.data_dir))
        Path(delivery._ledger_path(self.data_dir)).write_text(json.dumps([
            {'status': 'weird'}, 'junk', None,
            {'id': 'good', 'status': 'unknown', 'book_token': 'b', 'sha256': 's',
             'library_id': 'lib_1', 'folder_id': 'fol_1', 'output_bytes': 3}]))
        ledger = delivery.get_ledger(self.data_dir)
        self.assertEqual(1, len(ledger))
        self.assertEqual('good', ledger[0]['id'])

    def test_concurrent_duplicate_sends_one_wins(self):
        self.server.mode = 'ok'
        first = self._preview()
        second = self._preview()
        barrier = threading.Barrier(2)
        outcomes = []

        def attempt(preview):
            barrier.wait(timeout=5)
            try:
                entry = self._send(preview)
                outcomes.append(('accepted', entry['status']))
            except delivery.DeliveryError as error:
                outcomes.append(('error', error.status))

        threads = [threading.Thread(target=attempt, args=(p,)) for p in (first, second)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=10)
        self.assertEqual(2, len(outcomes))
        # Exactly one reservation wins; its HTTP 200 is recorded as
        # accepted-unknown (never sent), the other is refused with 409.
        accepted = [item for item in outcomes if item[0] == 'accepted']
        errors = [item for item in outcomes if item[0] == 'error']
        self.assertEqual(1, len(accepted))
        self.assertEqual('unknown', accepted[0][1])
        self.assertEqual(1, len(errors))
        self.assertEqual(409, errors[0][1])

    # --- keyring / transport hardening ---------------------------------------------------

    def test_plaintext_backend_rejected(self):
        class PlaintextKeyring:
            pass
        fake = FakeKeyring()
        fake.get_keyring = lambda: PlaintextKeyring()
        with patch.object(delivery, '_get_keyring', return_value=fake):
            with self.assertRaises(delivery.DeliveryError) as caught:
                delivery.save_api_key(self.base, 'sekret')
            self.assertEqual(503, caught.exception.status)
            self.assertNotIn('sekret', str(caught.exception))

    def test_backend_error_is_sanitized(self):
        class BrokenKeyring(FakeKeyring):
            def set_password(self, service, account, value):
                raise RuntimeError('locked: sekret would leak here')

            def get_password(self, service, account):
                raise RuntimeError('locked: sekret would leak here')
        fake = BrokenKeyring()
        fake.get_keyring = lambda: object()
        import types
        with patch.object(delivery, '_get_keyring', return_value=fake):
            with self.assertRaises(delivery.DeliveryError) as caught:
                delivery.save_api_key(self.base, 'sekret')
            self.assertEqual(503, caught.exception.status)
            self.assertNotIn('sekret', str(caught.exception))
            with self.assertRaises(delivery.DeliveryError) as caught_get:
                delivery.get_api_key(self.base)
            self.assertNotIn('sekret', str(caught_get.exception))

    def test_remote_http_refused_loopback_http_allowed(self):
        with self.assertRaises(delivery.DeliveryError):
            delivery._validate_base_url('http://example.com:13378')
        self.assertEqual('http://127.0.0.1:13378', delivery._validate_base_url('http://127.0.0.1:13378'))
        self.assertEqual('https://example.com', delivery._validate_base_url('https://example.com'))
        with self.assertRaises(delivery.DeliveryError):
            delivery._validate_base_url('http://127.0.0.1:13378?token=x')
        with self.assertRaises(delivery.DeliveryError):
            delivery._validate_base_url('http://user@127.0.0.1:13378')

    def test_session_disables_ambient_proxy(self):
        session = delivery._session('fake-token')
        try:
            self.assertFalse(session.trust_env)
        finally:
            session.close()

    def test_real_backend_structurally_qualified(self):
        import keyring
        backend = keyring.get_keyring()
        self.assertTrue(delivery._is_secure_backend(backend))

    def test_stalled_socket_is_bounded_by_timeout(self):
        self.server.mode = 'stall'
        preview = self._preview()
        started = time.time()
        with self.assertRaises(delivery.DeliveryError) as caught:
            with patch.object(delivery, '_READ_TIMEOUT', 1):
                delivery.upload_export(self.base, 'fake-token', preview,
                                       self.export_path, self.file_version,
                                       timeout=(2, 1))
        self.assertEqual(504, caught.exception.status)
        self.assertLess(time.time() - started, 7)
        self.server.mode = 'ok'

    def test_preview_growth_is_bounded(self):
        self._patch_identity()
        delivery.save_server_config(self.data_dir, self.base)
        previews = []
        for _ in range(55):
            previews.append(delivery.save_preview(
                self.data_dir, self.states, self.book_token, 'cloned_audiobook.mp3',
                'lib_1', 'fol_1', 'Test Book', '', '',
                base_url=self.base, token='fake-token'))
        self.assertEqual(50, len(delivery._previews))
        self.assertIsNone(delivery.get_preview(previews[0]['preview_id']))

    def test_reconcile_paginates_to_later_pages(self):
        self.server.mode = 'server-error'
        self._send(self._preview())
        filler = [{'id': f'li_{i}', 'folderId': 'fol_other',
                   'media': {'size': 1, 'metadata': {'title': 'Other Book'}}}
                  for i in range(150)]
        candidate = {'id': 'li_late', 'folderId': 'fol_1',
                     'media': {'size': len(self.payload),
                               'metadata': {'title': 'Test Book'}}}
        self.server.items = filler + [candidate]
        entry = delivery.get_latest_delivery(self.data_dir, self.book_token, self.sha256, 'lib_1', 'fol_1')
        reconciled = delivery.reconcile_delivery(self.data_dir, entry['id'], self.base, 'fake-token')
        self.assertEqual('unknown', reconciled['status'])
        self.assertIn('li_late', reconciled['reconciliation'])
        self.assertGreaterEqual(reconciled.get('reconciliation_pages', 0), 2)

    def test_read_json_limit_is_streamed(self):
        class FakeResponse:
            def __init__(self, chunks):
                self._chunks = chunks

            def iter_content(self, chunk_size=65536):
                yield from self._chunks

            def close(self):
                pass
        with self.assertRaises(delivery.DeliveryError):
            delivery._read_json_limited(FakeResponse([b'x' * (8 * 1024 * 1024 + 1)]))


class LargeTransportTests(unittest.TestCase):
    """Full-byte transport for >64 KiB exports against a real local server."""

    def setUp(self):
        self.server = RawAbsServer().start()
        self.base = f'http://127.0.0.1:{self.server.port}'
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name
        self.book_token = 'a' * 64
        self.export_path = os.path.join(self.data_dir, 'cloned_audiobook.mp3')

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def _write_payload(self, size):
        block = os.urandom(65536)
        digest = hashlib.sha256()
        with open(self.export_path, 'wb') as handle:
            written = 0
            while written < size:
                chunk = block[: max(0, min(len(block), size - written))]
                handle.write(chunk)
                digest.update(chunk)
                written += len(chunk)
        return digest.hexdigest()

    def _preview_for(self, size):
        sha = self._write_payload(size)
        file_version = get_export_file_version(self.export_path)
        report = {'status': 'verified', 'file': 'cloned_audiobook.mp3',
                  'file_version': file_version, 'sha256': sha,
                  'output_bytes': os.path.getsize(self.export_path),
                  'input_revision': 'rev_1', 'input_binding': 'current',
                  'book_token': self.book_token}
        import export_quality_session
        with patch.object(delivery, 'get_report_for_export', return_value=report), \
                patch.object(export_quality_session, 'get_export_input_revision',
                             return_value={'revision': 'rev_1', 'scope': 'full'}), \
                patch('project_scope_exports.require_scope_export_current', return_value=None):
            delivery.save_server_config(self.data_dir, self.base)
            preview = delivery.save_preview(self.data_dir, {}, self.book_token,
                                            'cloned_audiobook.mp3', 'lib_1', 'fol_1',
                                            'Big Book', '', '',
                                            base_url=self.base, token='fake-token')
        states = {}
        confirm = {'book_token': self.book_token, 'export_file': 'cloned_audiobook.mp3',
                   'library_id': 'lib_1', 'folder_id': 'fol_1', 'title': 'Big Book',
                   'author': '', 'series': '', 'base_url': self.base}
        return preview, confirm, sha, file_version, report

    def test_3mib_upload_is_byte_identical_with_bounded_progress(self):
        size = 3 * 1024 * 1024
        preview, confirm, sha, file_version, report = self._preview_for(size)
        import export_quality_session
        progress = []
        with patch.object(delivery, 'get_report_for_export', return_value=report), \
                patch.object(export_quality_session, 'get_export_input_revision',
                             return_value={'revision': 'rev_1', 'scope': 'full'}), \
                patch('project_scope_exports.require_scope_export_current', return_value=None):
            entry = delivery.send_preview(self.data_dir, {}, self.base, 'fake-token',
                                          preview['preview_id'], confirm,
                                          progress=progress.append)
        self.assertEqual('unknown', entry['status'])
        self.assertTrue(entry.get('verification_pending'))
        self.assertEqual(1, len(self.server.received_uploads))
        self.assertEqual(1, len(self.server.parsed_uploads))
        body = self.server.received_uploads[0]
        source = Path(self.export_path).read_bytes()
        self.assertIn(source, body)
        parsed = self.server.parsed_uploads[0]
        self.assertIsNotNone(parsed, 'real parser must recover the file part')
        self.assertEqual(source, parsed['file'])
        self.assertEqual(sha, parsed['file_sha256'])
        self.assertEqual(b'Big Book', parsed['fields'][b'title'])
        self.assertEqual(b'lib_1', parsed['fields'][b'library'])
        self.assertEqual(b'fol_1', parsed['fields'][b'folder'])
        self.assertGreater(len(progress), 10)
        increments = [b - a for a, b in zip([0] + progress[:-1], progress)]
        self.assertTrue(all(0 < inc <= 65536 for inc in increments))
        self.assertEqual(size, progress[-1])
        self.assertEqual(sha, hashlib.sha256(source).hexdigest())

    def test_transport_at_multiple_boundary_sizes(self):
        for size in (65535, 65536, 65537, 131072 + 13):
            with self.subTest(size=size):
                preview, confirm, sha, file_version, report = self._preview_for(size)
                import export_quality_session
                with patch.object(delivery, 'get_report_for_export', return_value=report), \
                        patch.object(export_quality_session, 'get_export_input_revision',
                                     return_value={'revision': 'rev_1', 'scope': 'full'}), \
                        patch('project_scope_exports.require_scope_export_current', return_value=None):
                    entry = delivery.send_preview(self.data_dir, {}, self.base, 'fake-token',
                                                  preview['preview_id'], confirm)
                self.assertEqual('unknown', entry['status'])
                self.assertTrue(entry.get('verification_pending'))
                source = Path(self.export_path).read_bytes()
                self.assertIn(source, self.server.received_uploads[-1])
                parsed = self.server.parsed_uploads[-1]
                self.assertIsNotNone(parsed, f'real parser must recover file at size {size}')
                self.assertEqual(source, parsed['file'])
                self.assertEqual(sha, parsed['file_sha256'])
                self.assertEqual(size, len(parsed['file']))
                self.assertEqual(b'Big Book', parsed['fields'][b'title'])
                # Clear ledger so the next size is a new identity, not a duplicate.
                Path(delivery._ledger_path(self.data_dir)).write_text('[]')

    def test_cancel_during_stream_aborts_without_sent(self):
        size = 3 * 1024 * 1024
        preview, confirm, sha, file_version, report = self._preview_for(size)
        cancel = threading.Event()

        def cancel_on_first(sent):
            cancel.set()

        import export_quality_session
        with patch.object(delivery, 'get_report_for_export', return_value=report), \
                patch.object(export_quality_session, 'get_export_input_revision',
                             return_value={'revision': 'rev_1', 'scope': 'full'}), \
                patch('project_scope_exports.require_scope_export_current', return_value=None):
            self.server.mode = 'interrupted'
            entry = delivery.send_preview(self.data_dir, {}, self.base, 'fake-token',
                                          preview['preview_id'], confirm,
                                          progress=cancel_on_first)
            # Cancel path through a direct upload raises rather than marking sent.
            with self.assertRaises(delivery.DeliveryError):
                delivery.upload_export(self.base, 'fake-token', preview,
                                       self.export_path, file_version,
                                       cancel_event=cancel)
        self.assertEqual('unknown', entry['status'])
        self.assertEqual(sha, hashlib.sha256(Path(self.export_path).read_bytes()).hexdigest())

    def test_source_replaced_midstream_still_uploads_bound_snapshot(self):
        size = 2 * 1024 * 1024
        preview, confirm, sha, file_version, report = self._preview_for(size)
        original = Path(self.export_path).read_bytes()
        replaced = b'R' * size
        replaced_flag = []

        def replace_once(sent):
            if not replaced_flag:
                replaced_flag.append(True)
                Path(self.export_path).write_bytes(replaced)

        import export_quality_session
        with patch.object(delivery, 'get_report_for_export', return_value=report), \
                patch.object(export_quality_session, 'get_export_input_revision',
                             return_value={'revision': 'rev_1', 'scope': 'full'}), \
                patch('project_scope_exports.require_scope_export_current', return_value=None):
            entry = delivery.send_preview(self.data_dir, {}, self.base, 'fake-token',
                                          preview['preview_id'], confirm,
                                          progress=replace_once)
        self.assertEqual('unknown', entry['status'])
        self.assertTrue(entry.get('verification_pending'))
        self.assertIn(original, self.server.received_uploads[0])
        self.assertNotIn(replaced, self.server.received_uploads[0])
        parsed = self.server.parsed_uploads[0]
        self.assertIsNotNone(parsed)
        self.assertEqual(original, parsed['file'])
        self.assertEqual(sha, parsed['file_sha256'])
        self.assertIn('WARNING', entry.get('error', ''))

    def test_source_replaced_before_stream_refuses_without_sending(self):
        size = 128 * 1024
        preview, confirm, sha, file_version, report = self._preview_for(size)
        Path(self.export_path).write_bytes(b'X' * size)
        import export_quality_session
        with patch.object(delivery, 'get_report_for_export', return_value=report), \
                patch.object(export_quality_session, 'get_export_input_revision',
                             return_value={'revision': 'rev_1', 'scope': 'full'}), \
                patch('project_scope_exports.require_scope_export_current', return_value=None):
            with self.assertRaises(delivery.DeliveryError) as caught:
                delivery.send_preview(self.data_dir, {}, self.base, 'fake-token',
                                      preview['preview_id'], confirm)
        self.assertEqual(409, caught.exception.status)
        self.assertEqual([], self.server.received_uploads)


class SocketParsedTests(unittest.TestCase):
    """Real-parsing assertions via actual socket send_preview."""

    def setUp(self):
        self.server = RawAbsServer().start()
        self.base = f'http://127.0.0.1:{self.server.port}'
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name
        self.book_token = 'a' * 64
        self.export_path = os.path.join(self.data_dir, 'cloned_audiobook.mp3')

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def _send_payload(self, payload):
        Path(self.export_path).write_bytes(payload)
        sha = hashlib.sha256(payload).hexdigest()
        file_version = get_export_file_version(self.export_path)
        report = {'status': 'verified', 'file': 'cloned_audiobook.mp3',
                  'file_version': file_version, 'sha256': sha,
                  'output_bytes': len(payload), 'input_revision': 'rev_1',
                  'input_binding': 'current', 'book_token': self.book_token}
        import export_quality_session
        with patch.object(delivery, 'get_report_for_export', return_value=report), \
                patch.object(export_quality_session, 'get_export_input_revision',
                             return_value={'revision': 'rev_1', 'scope': 'full'}), \
                patch('project_scope_exports.require_scope_export_current', return_value=None):
            delivery.save_server_config(self.data_dir, self.base)
            preview = delivery.save_preview(self.data_dir, {}, self.book_token,
                                            'cloned_audiobook.mp3', 'lib_1', 'fol_1',
                                            'Binary Book', '', '',
                                            base_url=self.base, token='fake-token')
            confirm = {'book_token': self.book_token, 'export_file': 'cloned_audiobook.mp3',
                       'library_id': 'lib_1', 'folder_id': 'fol_1', 'title': 'Binary Book',
                       'author': '', 'series': '', 'base_url': self.base}
            entry = delivery.send_preview(self.data_dir, {}, self.base, 'fake-token',
                                          preview['preview_id'], confirm)
        return entry, sha, payload

    def test_trailing_crlf_and_binary_payload_survive_socket_parse(self):
        # >64 KiB, ends with CRLF CRLF + NUL + CRLF, and contains CRLF pairs.
        size = 70000
        base_payload = (b'\x00\xff\r\nabc\r\n' * (size // 9 + 1))[:size - 5]
        payload = base_payload + b'\r\n\r\n\x00'.replace(b'\x00', b'\x00') + b'\r\n'
        # Force exact tail: ... \r\n \r\n \x00 \r\n
        self.assertTrue(payload.endswith(b'\r\n\r\n\x00\r\n'))
        entry, sha, _ = self._send_payload(payload)
        self.assertEqual('unknown', entry['status'])
        self.assertTrue(entry.get('verification_pending'))
        parsed = self.server.parsed_uploads[-1]
        self.assertIsNotNone(parsed)
        self.assertEqual(payload, parsed['file'])
        self.assertEqual(sha, parsed['file_sha256'])
        self.assertEqual(len(payload), len(parsed['file']))
        self.assertEqual(b'Binary Book', parsed['fields'][b'title'])

    def test_binary_payload_with_crlf_sequences_at_chunk_edges(self):
        for size in (65536, 131085):
            with self.subTest(size=size):
                # Payload whose 64 KiB edges land on CRLF boundaries.
                payload = (b'x' * (size - 4)) + b'\r\n\r\n'
                entry, sha, _ = self._send_payload(payload)
                self.assertEqual('unknown', entry['status'])
                self.assertTrue(entry.get('verification_pending'))
                parsed = self.server.parsed_uploads[-1]
                self.assertIsNotNone(parsed)
                self.assertEqual(payload, parsed['file'])
                self.assertEqual(sha, parsed['file_sha256'])
                # Fresh identity per size: clear ledger duplicate fence.
                Path(delivery._ledger_path(self.data_dir)).write_text('[]')


class ReconcileVerifyTests(unittest.TestCase):
    """Explicit byte verification promotes unknown->sent only on exact proof."""

    def setUp(self):
        self.server = RawAbsServer().start()
        self.base = f'http://127.0.0.1:{self.server.port}'
        self.tmp = tempfile.TemporaryDirectory()
        self.data_dir = self.tmp.name
        self.book_token = 'a' * 64
        self.export_path = os.path.join(self.data_dir, 'cloned_audiobook.mp3')
        # >64 KiB synthetic payload so verification streams real bytes.
        block = os.urandom(65536)
        digest = hashlib.sha256()
        with open(self.export_path, 'wb') as handle:
            written = 0
            size = 70000
            while written < size:
                chunk = block[:max(0, min(len(block), size - written))]
                handle.write(chunk)
                digest.update(chunk)
                written += len(chunk)
        self.payload = Path(self.export_path).read_bytes()
        self.sha256 = digest.hexdigest()
        self.file_version = get_export_file_version(self.export_path)
        self.report = {'status': 'verified', 'file': 'cloned_audiobook.mp3',
                       'file_version': self.file_version, 'sha256': self.sha256,
                       'output_bytes': len(self.payload), 'input_revision': 'rev_1',
                       'input_binding': 'current', 'book_token': self.book_token}
        self.states = {'audio': {'export_quality_reports': [self.report]}}

    def tearDown(self):
        self.server.stop()
        self.tmp.cleanup()

    def _patch_identity(self):
        stack = patch.object(delivery, 'get_report_for_export', return_value=self.report)
        import export_quality_session
        stack2 = patch.object(export_quality_session, 'get_export_input_revision',
                              return_value={'revision': 'rev_1', 'scope': 'full'})
        stack.start()
        stack2.start()
        self.addCleanup(stack.stop)
        self.addCleanup(stack2.stop)

    def _preview(self):
        self._patch_identity()
        delivery.save_server_config(self.data_dir, self.base)
        return delivery.save_preview(self.data_dir, self.states, self.book_token,
                                     'cloned_audiobook.mp3', 'lib_1', 'fol_1',
                                     'Test Book', '', '', base_url=self.base, token='fake-token')

    def _confirm(self):
        return {'book_token': self.book_token, 'export_file': 'cloned_audiobook.mp3',
                'library_id': 'lib_1', 'folder_id': 'fol_1', 'title': 'Test Book',
                'author': '', 'series': '', 'base_url': self.base}

    def _seed_unknown(self):
        self.server.mode = 'server-error'
        preview = self._preview()
        entry = delivery.send_preview(self.data_dir, self.states, self.base, 'fake-token',
                                      preview['preview_id'], self._confirm())
        self.assertEqual('unknown', entry['status'])
        self.server.mode = 'ok'
        return entry

    def _seed_accepted(self):
        self.server.mode = 'accepted-no-persist'
        preview = self._preview()
        entry = delivery.send_preview(self.data_dir, self.states, self.base, 'fake-token',
                                      preview['preview_id'], self._confirm())
        self.assertEqual('unknown', entry['status'])
        self.assertTrue(entry.get('verification_pending'))
        self.server.mode = 'ok'
        return entry

    def _listing_candidate(self, item_id='li_1'):
        self.server.items = [{'id': item_id, 'libraryId': 'lib_1', 'folderId': 'fol_1',
                              'media': {'size': len(self.payload),
                                        'metadata': {'title': 'Test Book'}}}]

    def _item_detail(self, item_id='li_1', ino='ino_1', size=None, **overrides):
        size = len(self.payload) if size is None else size
        detail = {'id': item_id, 'libraryId': 'lib_1', 'folderId': 'fol_1',
                  'mediaType': 'book',
                  'media': {'metadata': {'title': 'Test Book'},
                            'audioFiles': [{'ino': ino, 'metadata': {'size': size}}]},
                  'libraryFiles': [{'ino': ino, 'metadata': {'filename': 'cloned_audiobook.mp3',
                                                              'size': size}}]}
        detail.update(overrides)
        return detail

    def test_exact_bytes_promote_to_sent_with_proof(self):
        entry = self._seed_unknown()
        self._listing_candidate()
        self.server.item_details = {'li_1': self._item_detail()}
        self.server.file_blobs = {('li_1', 'ino_1'): self.payload}
        before = hashlib.sha256(Path(self.export_path).read_bytes()).hexdigest()
        reconciled = delivery.reconcile_delivery(self.data_dir, entry['id'], self.base, 'fake-token')
        self.assertEqual('sent', reconciled['status'])
        self.assertEqual('li_1', reconciled['reconciled_item_id'])
        self.assertEqual('ino_1', reconciled['reconciled_file_id'])
        self.assertEqual(len(self.payload), reconciled['reconciled_bytes'])
        self.assertEqual(self.sha256, reconciled['reconciled_sha256'])
        self.assertIn('verified by raw bytes', reconciled['reconciliation'])
        persisted = delivery.get_ledger(self.data_dir)[0]
        self.assertEqual('sent', persisted['status'])
        self.assertEqual(self.sha256, persisted['reconciled_sha256'])
        self.assertEqual(before, hashlib.sha256(Path(self.export_path).read_bytes()).hexdigest())
        # After promotion the duplicate fence blocks a second send.
        self._patch_identity()
        preview = delivery.save_preview(self.data_dir, self.states, self.book_token,
                                        'cloned_audiobook.mp3', 'lib_1', 'fol_1',
                                        'Test Book', '', '', base_url=self.base, token='fake-token')
        with self.assertRaises(delivery.DeliveryError) as caught:
            delivery.send_preview(self.data_dir, self.states, self.base, 'fake-token',
                                  preview['preview_id'], self._confirm())
        self.assertIn('already delivered', str(caught.exception))

    def test_wrong_bytes_stay_unknown(self):
        entry = self._seed_unknown()
        self._listing_candidate()
        self.server.item_details = {'li_1': self._item_detail()}
        wrong = bytearray(self.payload)
        wrong[0] ^= 1
        self.server.file_blobs = {('li_1', 'ino_1'): bytes(wrong)}
        reconciled = delivery.reconcile_delivery(self.data_dir, entry['id'], self.base, 'fake-token')
        self.assertEqual('unknown', reconciled['status'])
        self.assertIn('SHA-256', reconciled['reconciliation'])
        self.assertIn('REMAINS unknown', reconciled['reconciliation'])

    def test_accepted_wrong_bytes_stay_unknown(self):
        # An upload-accepted record is held to the same exact-bytes bar:
        # a one-bit file difference must not promote it.
        entry = self._seed_accepted()
        self._listing_candidate()
        self.server.item_details = {'li_1': self._item_detail()}
        wrong = bytearray(self.payload)
        wrong[0] ^= 1
        self.server.file_blobs = {('li_1', 'ino_1'): bytes(wrong)}
        reconciled = delivery.reconcile_delivery(self.data_dir, entry['id'], self.base, 'fake-token')
        self.assertEqual('unknown', reconciled['status'])
        self.assertTrue(reconciled.get('verification_pending'))
        self.assertIn('REMAINS unknown', reconciled['reconciliation'])

    def test_truncated_stays_unknown(self):
        entry = self._seed_unknown()
        self._listing_candidate()
        self.server.item_details = {'li_1': self._item_detail()}
        self.server.file_blobs = {('li_1', 'ino_1'): self.payload[:-10]}
        reconciled = delivery.reconcile_delivery(self.data_dir, entry['id'], self.base, 'fake-token')
        self.assertEqual('unknown', reconciled['status'])
        self.assertIn('REMAINS unknown', reconciled['reconciliation'])

    def test_oversize_stays_unknown(self):
        entry = self._seed_unknown()
        self._listing_candidate()
        self.server.item_details = {'li_1': self._item_detail()}
        self.server.file_blobs = {('li_1', 'ino_1'): self.payload + b'X'}
        reconciled = delivery.reconcile_delivery(self.data_dir, entry['id'], self.base, 'fake-token')
        self.assertEqual('unknown', reconciled['status'])
        self.assertIn('REMAINS unknown', reconciled['reconciliation'])

    def test_xaccel_forbidden_notfound_redirect_stay_unknown(self):
        for mode in ('xaccel', 'forbidden', 'notfound', 'redirect'):
            with self.subTest(mode=mode):
                Path(delivery._ledger_path(self.data_dir)).write_text('[]')
                self.server.mode = 'server-error'
                preview = self._preview()
                entry = delivery.send_preview(self.data_dir, self.states, self.base, 'fake-token',
                                              preview['preview_id'], self._confirm())
                self.assertEqual('unknown', entry['status'])
                Path(delivery._ledger_path(self.data_dir)).write_text(
                    json.dumps([entry]))
                self.server.mode = 'ok'
                self._listing_candidate()
                self.server.item_details = {'li_1': self._item_detail()}
                self.server.file_blobs = {('li_1', 'ino_1'): self.payload}
                self.server.download_mode = mode
                reconciled = delivery.reconcile_delivery(self.data_dir, entry['id'], self.base, 'fake-token')
                self.assertEqual('unknown', reconciled['status'], mode)
                self.assertIn('REMAINS unknown', reconciled['reconciliation'], mode)
                self.server.download_mode = 'ok'

    def test_ambiguous_multiple_matches_stay_unknown(self):
        entry = self._seed_unknown()
        self.server.items = [
            {'id': 'li_a', 'libraryId': 'lib_1', 'folderId': 'fol_1',
             'media': {'size': len(self.payload), 'metadata': {'title': 'Test Book'}}},
            {'id': 'li_b', 'libraryId': 'lib_1', 'folderId': 'fol_1',
             'media': {'size': len(self.payload), 'metadata': {'title': 'Test Book'}}},
        ]
        self.server.item_details = {'li_a': self._item_detail('li_a'), 'li_b': self._item_detail('li_b')}
        self.server.file_blobs = {('li_a', 'ino_1'): self.payload, ('li_b', 'ino_1'): self.payload}
        reconciled = delivery.reconcile_delivery(self.data_dir, entry['id'], self.base, 'fake-token')
        self.assertEqual('unknown', reconciled['status'])
        self.assertIn('ambiguous', reconciled['reconciliation'])

    def test_no_candidate_is_not_proof_and_no_retry(self):
        entry = self._seed_unknown()
        self.server.items = []
        self.server.requests_seen.clear()
        reconciled = delivery.reconcile_delivery(self.data_dir, entry['id'], self.base, 'fake-token')
        self.assertEqual('unknown', reconciled['status'])
        self.assertIn('cannot prove', reconciled['reconciliation'])
        # Reconcile made no upload POST (only listing GETs).
        uploads = [path for method, path, _ in self.server.requests_seen
                   if method == 'POST' and path.split('?', 1)[0] == '/api/upload']
        self.assertEqual([], uploads)
        # Resend is still blocked.
        self._patch_identity()
        preview = delivery.save_preview(self.data_dir, self.states, self.book_token,
                                        'cloned_audiobook.mp3', 'lib_1', 'fol_1',
                                        'Test Book', '', '', base_url=self.base, token='fake-token')
        with self.assertRaises(delivery.DeliveryError) as caught:
            delivery.send_preview(self.data_dir, self.states, self.base, 'fake-token',
                                  preview['preview_id'], self._confirm())
        self.assertIn('unknown', str(caught.exception))

    def test_bound_server_mismatch_is_refused_without_network(self):
        entry = self._seed_unknown()
        with self.assertRaises(delivery.DeliveryError) as caught:
            delivery.reconcile_delivery(self.data_dir, entry['id'], 'http://127.0.0.1:65534', 'fake-token')
        self.assertEqual(409, caught.exception.status)
        persisted = delivery.get_ledger(self.data_dir)[0]
        self.assertEqual('unknown', persisted['status'])

    def test_full_item_identity_mismatch_stays_unknown(self):
        cases = [
            self._item_detail(libraryId='lib_2'),
            self._item_detail(folderId='fol_2'),
            self._item_detail(mediaType='podcast'),
            self._item_detail(media={'metadata': {'title': 'Other Book'}, 'audioFiles': []}),
        ]
        for detail in cases:
            with self.subTest(detail=str(detail)[:60]):
                Path(delivery._ledger_path(self.data_dir)).write_text('[]')
                self.server.mode = 'server-error'
                preview = self._preview()
                entry = delivery.send_preview(self.data_dir, self.states, self.base, 'fake-token',
                                              preview['preview_id'], self._confirm())
                self.server.mode = 'ok'
                self._listing_candidate()
                self.server.item_details = {'li_1': detail}
                self.server.file_blobs = {('li_1', 'ino_1'): self.payload}
                reconciled = delivery.reconcile_delivery(self.data_dir, entry['id'], self.base, 'fake-token')
                self.assertEqual('unknown', reconciled['status'])
                self.assertIn('REMAINS unknown', reconciled['reconciliation'])

    def test_second_reconcile_after_sent_does_not_downgrade(self):
        entry = self._seed_unknown()
        self._listing_candidate()
        self.server.item_details = {'li_1': self._item_detail()}
        self.server.file_blobs = {('li_1', 'ino_1'): self.payload}
        first = delivery.reconcile_delivery(self.data_dir, entry['id'], self.base, 'fake-token')
        self.assertEqual('sent', first['status'])
        second = delivery.reconcile_delivery(self.data_dir, entry['id'], self.base, 'fake-token')
        self.assertEqual('sent', second['status'])
        persisted = delivery.get_ledger(self.data_dir)[0]
        self.assertEqual('sent', persisted['status'])


class MultipartFramingTests(unittest.TestCase):
    def test_real_parser_recovers_exact_file_at_chunk_boundaries(self):
        from python_multipart import parse_form
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / 'snapshot.mp3'
            for size in (65535, 65536, 65537, 131085, 3 * 1024 * 1024):
                with self.subTest(size=size):
                    payload = (b'synthetic-audio-\x00\xff' * (size // 18 + 1))[:size]
                    self.assertEqual(size, len(payload))
                    snapshot.write_bytes(payload)
                    preview = {'title': 'Synthetic book', 'library_id': 'lib_1',
                               'folder_id': 'fol_1', 'filename': 'snapshot.mp3',
                               'format': 'mp3'}
                    multipart = delivery._StreamingMultipart(preview, str(snapshot))
                    body = b''.join(multipart)
                    self.assertEqual(len(multipart), len(body))
                    fields, files = {}, {}
                    parsed_files = []

                    def on_field(field):
                        fields[field.field_name] = field.value

                    def on_file(file):
                        parsed_files.append(file)
                        file.file_object.seek(0)
                        files[file.field_name] = file.file_object.read()

                    parse_form({'Content-Type': (
                        'multipart/form-data; boundary=' + multipart.boundary
                    ).encode(), 'Content-Length': str(len(body)).encode()},
                        io.BytesIO(body), on_field, on_file, chunk_size=65536)
                    for file in parsed_files:
                        file.close()
                    self.assertEqual(b'Synthetic book', fields[b'title'])
                    self.assertIn(b'file', files)
                    self.assertEqual(payload, files[b'file'])
                    self.assertEqual(hashlib.sha256(payload).digest(),
                                     hashlib.sha256(files[b'file']).digest())


if __name__ == '__main__':
    unittest.main()
