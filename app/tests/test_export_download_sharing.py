"""Real app download readers must not prevent atomic export publication."""
import asyncio
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from audio_validation import publish_audio_output
from routers import editor


class ExportDownloadSharingTests(unittest.TestCase):
    def test_multiple_readers_keep_old_identity_while_new_output_keeps_source_identity(self):
        from export_file_io import open_export_reader
        with tempfile.TemporaryDirectory() as directory:
            target, pending = Path(directory) / 'export.mp3', Path(directory) / 'pending'
            original, replacement = b'original bytes' * 1000, b'new bytes'
            target.write_bytes(original)
            pending.write_bytes(replacement)
            source = pending.stat()
            with open_export_reader(target) as first, open_export_reader(target) as second:
                before = os.fstat(first.fileno())
                self.assertTrue(publish_audio_output(pending, target))
                published = target.stat()
                self.assertEqual((source.st_dev, source.st_ino, source.st_size, source.st_mtime_ns),
                                 (published.st_dev, published.st_ino, published.st_size, published.st_mtime_ns))
                for reader in (first, second):
                    pinned = os.fstat(reader.fileno())
                    self.assertEqual((before.st_dev, before.st_ino), (pinned.st_dev, pinned.st_ino))
                    self.assertNotEqual((published.st_dev, published.st_ino), (pinned.st_dev, pinned.st_ino))
                    self.assertEqual(original, reader.read())
                self.assertEqual(replacement, target.read_bytes())
            self.assertTrue(first.closed)
            self.assertTrue(second.closed)

    def test_refused_alias_or_hardlink_never_enters_native_replacement(self):
        import stat
        from types import SimpleNamespace
        from export_file_io import replace_export_output
        cases = [(mode, links, attributes, side)
                 for mode, links, attributes in ((stat.S_IFLNK, 1, 0x400),
                                                 (stat.S_IFREG, 1, 0x400), (stat.S_IFREG, 2, 0))
                 for side in ('source', 'destination')]
        for mode, links, attributes, side in cases:
            with self.subTest(mode=mode, links=links, attributes=attributes, side=side), tempfile.TemporaryDirectory() as directory:
                source, destination = Path(directory) / 'pending', Path(directory) / 'export.mp3'
                source.write_bytes(b'new')
                destination.write_bytes(b'old')
                error = PermissionError('synthetic publication refusal')
                error.winerror = 5
                info = SimpleNamespace(st_mode=mode, st_nlink=links, st_file_attributes=attributes)
                plain = source.stat()
                aliased = source if side == 'source' else destination
                def metadata(path):
                    return info if os.fspath(path) == os.fspath(aliased) else plain
                with patch('export_file_io.os.replace', side_effect=error), \
                        patch('export_file_io.os.lstat', side_effect=metadata), \
                        patch('export_file_io._windows_api', side_effect=AssertionError('Aliases must not reach native replacement')):
                    with self.assertRaises(PermissionError):
                        replace_export_output(source, destination)
                self.assertEqual(b'new', source.read_bytes())
                self.assertEqual(b'old', destination.read_bytes())

    def test_head_ranges_if_range_and_pathsend_keep_the_pinned_generation(self):
        from email.parser import BytesParser
        from export_download import ExportFileResponse
        cases = [('GET', None, 200), ('HEAD', None, 200),
                 ('GET', 'bytes=2-7', 206), ('HEAD', 'bytes=2-7', 206),
                 ('GET', 'bytes=0-2,10-12', 206), ('HEAD', 'bytes=0-2,10-12', 206),
                 ('GET', 'bytes=1000-', 416), ('GET', 'wrong=0-3', 400),
                 ('GET', 'bytes=0-1', 200)]
        async def exercise(root, method, requested_range, status, mismatch):
            path = root / 'export.mp3'
            original = b'0123456789abcdefghijklmnopqrstuvwxyz'
            path.write_bytes(original)
            pending = root / 'pending'
            pending.write_bytes(b'new generation')
            response = ExportFileResponse(path, filename=path.name, media_type='audio/mpeg')
            messages = []
            async def send(message):
                messages.append(message)
                if message['type'] == 'http.response.start':
                    self.assertTrue(await asyncio.to_thread(publish_audio_output, pending, path))
            async def receive():
                raise AssertionError('Unexpected receive')
            headers = [] if requested_range is None else [(b'range', requested_range.encode())]
            if mismatch:
                headers.append((b'if-range', b'"not-this-generation"'))
            await response({'type':'http', 'method':method, 'headers':headers,
                'extensions':{'http.response.pathsend':{}}}, receive, send)
            self.assertTrue(response.stream.closed)
            self.assertFalse(any(message['type'] == 'http.response.pathsend' for message in messages))
            self.assertEqual(status, messages[0]['status'])
            headers = dict(messages[0]['headers'])
            body = b''.join(message.get('body', b'') for message in messages[1:])
            if method == 'HEAD':
                self.assertEqual(b'', body)
            else:
                self.assertEqual(len(body), int(headers[b'content-length']))
                if status == 200:
                    self.assertEqual(original, body)
                elif requested_range == 'bytes=2-7':
                    self.assertEqual(original[2:8], body)
                    self.assertEqual(b'bytes 2-7/36', headers[b'content-range'])
                elif status == 206:
                    multipart = BytesParser().parsebytes(b'Content-Type: ' + headers[b'content-type'] + b'\r\n\r\n' + body)
                    self.assertEqual([original[:3], original[10:13]],
                                     [part.get_payload(decode=True) for part in multipart.get_payload()])
            self.assertEqual(b'new generation', path.read_bytes())
        for position, (method, requested_range, status) in enumerate(cases):
            with self.subTest(method=method, requested_range=requested_range, status=status), \
                    tempfile.TemporaryDirectory() as directory:
                asyncio.run(exercise(Path(directory), method, requested_range, status, position == len(cases) - 1))

    def test_disconnect_and_cancellation_close_the_native_descriptor(self):
        from export_download import ExportFileResponse
        async def exercise(root, fault):
            path = root / 'export.mp3'
            path.write_bytes(b'synthetic bytes' * 10000)
            response = ExportFileResponse(path)
            entered = asyncio.Event()
            async def send(message):
                if message['type'] == 'http.response.body' and message.get('body'):
                    entered.set()
                    if fault == 'disconnect':
                        raise OSError('synthetic disconnect')
                    await asyncio.Event().wait()
            async def receive():
                raise AssertionError('Unexpected receive')
            task = asyncio.create_task(response({'type':'http', 'method':'GET', 'headers':[]}, receive, send))
            try:
                await asyncio.wait_for(entered.wait(), 3)
                if fault == 'cancel':
                    task.cancel()
                result = await asyncio.gather(task, return_exceptions=True)
                self.assertIsInstance(result[0], OSError if fault == 'disconnect' else asyncio.CancelledError)
                self.assertTrue(response.stream.closed)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        for fault in ('disconnect', 'cancel'):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as directory:
                asyncio.run(exercise(Path(directory), fault))

    def test_cancelled_open_closes_the_late_native_result(self):
        import threading
        import export_download
        async def exercise(path):
            entered, release = threading.Event(), threading.Event()
            opened = []
            opener = export_download.open_export_reader
            def delayed_open(target):
                stream = opener(target)
                opened.append(stream)
                entered.set()
                if not release.wait(3):
                    stream.close()
                    raise AssertionError('Opening worker was not released')
                return stream
            async def unexpected(*args):
                raise AssertionError('Cancelled open must not send a response')
            with patch.object(export_download, 'open_export_reader', side_effect=delayed_open):
                response = export_download.ExportFileResponse(path)
                task = asyncio.create_task(response({'type':'http', 'method':'GET', 'headers':[]}, unexpected, unexpected))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 3))
                    task.cancel()
                finally:
                    release.set()
                    result = await asyncio.gather(task, return_exceptions=True)
                self.assertIsInstance(result[0], asyncio.CancelledError)
                self.assertEqual(1, len(opened))
                self.assertTrue(opened[0].closed)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'export.mp3'
            path.write_bytes(b'own bytes')
            asyncio.run(exercise(path))

    def test_failed_descriptor_wrapping_closes_its_owned_descriptor(self):
        from export_file_io import open_export_reader
        from audio_take_history import _open_regular
        from unittest.mock import Mock
        close = os.close
        closed = []
        def record_close(descriptor):
            closed.append(descriptor)
            return close(descriptor)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'export.mp3'
            path.write_bytes(b'own bytes')
            with patch('export_file_io.os.fdopen', side_effect=OSError('synthetic wrapping failure')), \
                    patch('export_file_io.os.close', side_effect=record_close):
                with self.assertRaisesRegex(OSError, 'wrapping failure'):
                    open_export_reader(path)
            self.assertEqual(1, len(closed))
            with self.assertRaises(OSError):
                os.fstat(closed[0])
            invalid_stream = Mock()
            invalid_stream.fileno.side_effect = OSError('synthetic fileno failure')
            with self.assertRaisesRegex(OSError, 'fileno failure'):
                _open_regular(path, path.parent, 1024, opener=lambda target: invalid_stream)
            invalid_stream.close.assert_called_once_with()

    def test_refused_publication_preserves_old_and_pending_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path, pending = Path(directory) / 'export.mp3', Path(directory) / 'pending'
            path.write_bytes(b'old')
            pending.write_bytes(b'new')
            with path.open('rb') as stream:
                if os.name == 'nt':
                    with self.assertRaises(OSError):
                        publish_audio_output(pending, path)
                else:
                    with patch('export_file_io.os.replace', side_effect=PermissionError('synthetic refusal')):
                        with self.assertRaises(PermissionError):
                            publish_audio_output(pending, path)
                self.assertEqual(b'old', stream.read())
                self.assertEqual(b'old', path.read_bytes())
                self.assertEqual(b'new', pending.read_bytes())
            self.assertTrue(publish_audio_output(pending, path))
            self.assertEqual(b'new', path.read_bytes())

    def test_readers_keep_original_bytes_during_replace_and_backup_publication(self):
        async def exercise(root, route, backup):
            suffix = '.m4b' if route == 'm4b' else '.mp3'
            path = root / ('audiobook' + suffix)
            original = b'original synthetic export' * 10000
            replacement = b'replacement synthetic export' * 10000
            path.write_bytes(original)
            pending = root / 'pending'
            pending.write_bytes(replacement)
            with patch.multiple(editor, AUDIOBOOK_PATH=str(path), M4B_PATH=str(path), DATA_DIR=str(root)), \
                    patch.object(editor, 'require_scope_export_current'), \
                    patch.object(editor, '_get_listed_chapter_path', return_value=(str(path), path.name)):
                if route == 'bound':
                    response = editor._serve_bound_audio(hashlib.sha256(original).hexdigest(), path.name, 'Fixture')
                elif route == 'mp3':
                    response = await editor.get_audiobook()
                elif route == 'm4b':
                    response = await editor.get_audiobook_m4b()
                else:
                    response = await editor.download_chapter(path.name)
            bodies = []
            published = False
            def publish():
                if backup:
                    os.replace(path, root / 'previous')
                self.assertTrue(publish_audio_output(pending, path))
            async def send(message):
                nonlocal published
                if message['type'] == 'http.response.body':
                    bodies.append(message.get('body', b''))
                    if message.get('body') and not published:
                        await asyncio.to_thread(publish)
                        published = True
            async def receive():
                raise AssertionError('Unexpected disconnect polling')
            try:
                await response({'type':'http', 'method':'GET', 'headers':[],
                    'asgi':{'spec_version':'2.4'}}, receive, send)
                self.assertTrue(published)
                self.assertEqual(original, b''.join(bodies))
                self.assertEqual(replacement, path.read_bytes())
                self.assertFalse(pending.exists())
                if backup:
                    self.assertEqual(original, (root / 'previous').read_bytes())
            finally:
                if route == 'bound':
                    response.stream.close()
        for route in ('bound', 'mp3', 'm4b', 'chapter'):
            for backup in (False, True):
                with self.subTest(route=route, backup=backup), tempfile.TemporaryDirectory() as directory:
                    asyncio.run(exercise(Path(directory), route, backup))
