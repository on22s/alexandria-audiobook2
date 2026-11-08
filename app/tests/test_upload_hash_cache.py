"""Native upload versions must not accumulate unbounded cached digests."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import io
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from fastapi import FastAPI
import httpx

from routers import script


class UploadHashCacheTests(unittest.TestCase):
    def setUp(self):
        with script._upload_hash_lock:
            self.saved = script._upload_hash_cache.copy()
            script._upload_hash_cache.clear()
        self.addCleanup(self.restore_cache)

    def restore_cache(self):
        with script._upload_hash_lock:
            script._upload_hash_cache.clear()
            script._upload_hash_cache.update(self.saved)

    def test_native_versions_replace_historical_digest_for_the_same_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'source.txt'
            for index in range(24):
                data = b'source' * (index + 1)
                path.write_bytes(data)
                self.assertEqual(hashlib.sha256(data).hexdigest(), script._get_upload_hash(str(path)))
                self.assertEqual(1, len(script._upload_hash_cache))

    def test_capacity_and_access_recency_evict_oldest_without_changing_file_bytes(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(script, 'MAX_UPLOAD_HASH_CACHE_ENTRIES', 3, create=True):
            paths = [Path(tmp) / f'{index}.txt' for index in range(4)]
            for index, path in enumerate(paths):
                path.write_bytes(bytes([index]) * (index + 1))
            for path in paths[:3]:
                script._get_upload_hash(str(path))
            # A hit refreshes recency without reopening the native file.
            with patch('builtins.open', side_effect=AssertionError('cache hit reopened file')):
                script._get_upload_hash(str(paths[0]))
            script._get_upload_hash(str(paths[3]))
            self.assertEqual(3, len(script._upload_hash_cache))
            self.assertEqual([str(paths[2]), str(paths[0]), str(paths[3])], list(script._upload_hash_cache))
            real_open = open
            opened = []
            def observe(path, *args, **kwargs):
                opened.append(str(path))
                return real_open(path, *args, **kwargs)
            with patch('builtins.open', side_effect=observe):
                digest = script._get_upload_hash(str(paths[1]))
            self.assertEqual([str(paths[1])], opened)
            self.assertEqual(hashlib.sha256(b'\x01\x01').hexdigest(), digest)
            self.assertEqual(3, len(script._upload_hash_cache))
            for index, path in enumerate(paths):
                self.assertEqual(bytes([index]) * (index + 1), path.read_bytes())


def _rewrite_inplace(path, data, before):
    path.write_bytes(data)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert after.st_size == before.st_size and after.st_mtime_ns == before.st_mtime_ns


def _rewrite_replacement(path, data, before):
    staging = path.with_name(path.name + '.staging')
    staging.write_bytes(data)
    os.replace(staging, path)
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    after = path.stat()
    assert after.st_size == before.st_size and after.st_mtime_ns == before.st_mtime_ns


def _synthetic_epub(text):
    container = ('<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfile full-path="book.opf"/></container>')
    package = ('<package xmlns="http://www.idpf.org/2007/opf" '
               'xmlns:dc="http://purl.org/dc/elements/1.1/">'
               '<metadata><dc:title>Fixture title</dc:title>'
               '<dc:creator>Fixture Author</dc:creator></metadata>'
               '<manifest><item id="body" href="body.xhtml" '
               'media-type="application/xhtml+xml"/></manifest>'
               '<spine><itemref idref="body"/></spine></package>')
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as archive:
        for name, data in [('META-INF/container.xml', container),
                           ('book.opf', package),
                           ('body.xhtml', '<html><body><p>' + text.decode() + '</p></body></html>')]:
            archive.writestr(zipfile.ZipInfo(name), data)
    return raw.getvalue()


class UploadHashDedupeFreshTests(unittest.TestCase):
    """Dedupe must freshly verify current bytes before discarding an upload.

    The primed size/mtime cache is advisory only: a same-size,
    restored-mtime rewrite (in place or by replacement) must not make a
    changed canonical look like the incoming bytes, and a changed
    incoming file must not be discarded against a stale cached digest.
    """

    ORIGINAL = b'AAAA' * 10
    CHANGED = b'BBBB' * 10

    def setUp(self):
        with script._upload_hash_lock:
            self.saved = script._upload_hash_cache.copy()
            script._upload_hash_cache.clear()
        self.addCleanup(self.restore_cache)

    def restore_cache(self):
        with script._upload_hash_lock:
            script._upload_hash_cache.clear()
            script._upload_hash_cache.update(self.saved)

    def _setup_uploads(self, tmp, canonical_name='canonical.txt'):
        uploads = Path(tmp)
        canonical = uploads / canonical_name
        canonical.write_bytes(self.ORIGINAL)
        before = canonical.stat()
        primed = script._get_upload_hash(str(canonical))
        self.assertEqual(hashlib.sha256(self.ORIGINAL).hexdigest(), primed)
        return uploads, canonical, before

    def test_same_size_restored_mtime_inplace_canonical_not_reused(self):
        for suffix in ('.txt', '.md'):
            with self.subTest(suffix=suffix), tempfile.TemporaryDirectory() as tmp:
                with script._upload_hash_lock:
                    script._upload_hash_cache.clear()
                uploads, canonical, before = self._setup_uploads(
                    tmp, f'canonical{suffix}')
                _rewrite_inplace(canonical, self.CHANGED, before)
                incoming = uploads / f'incoming{suffix}'
                incoming.write_bytes(self.ORIGINAL)
                with patch.object(script, 'UPLOADS_DIR', str(uploads)):
                    selected, reused = script._reuse_duplicate_upload(str(incoming))
                self.assertFalse(reused, 'changed canonical must not be selected')
                self.assertEqual(str(incoming), selected)
                self.assertEqual(self.ORIGINAL, incoming.read_bytes(),
                                 'incoming upload must be retained, not discarded')
                self.assertEqual(self.CHANGED, canonical.read_bytes())

    def test_same_size_restored_mtime_replacement_canonical_not_reused(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploads, canonical, before = self._setup_uploads(tmp)
            _rewrite_replacement(canonical, self.CHANGED, before)
            incoming = uploads / 'incoming.txt'
            incoming.write_bytes(self.ORIGINAL)
            with patch.object(script, 'UPLOADS_DIR', str(uploads)):
                selected, reused = script._reuse_duplicate_upload(str(incoming))
            self.assertFalse(reused)
            self.assertEqual(str(incoming), selected)
            self.assertEqual(self.ORIGINAL, incoming.read_bytes())
            self.assertEqual(self.CHANGED, canonical.read_bytes())

    def test_true_duplicate_is_reused_and_incoming_removed(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploads, canonical, _before = self._setup_uploads(tmp)
            incoming = uploads / 'incoming.txt'
            incoming.write_bytes(self.ORIGINAL)
            with patch.object(script, 'UPLOADS_DIR', str(uploads)):
                selected, reused = script._reuse_duplicate_upload(str(incoming))
            self.assertTrue(reused)
            self.assertEqual(str(canonical), selected)
            self.assertFalse(incoming.exists())
            self.assertEqual(self.ORIGINAL, canonical.read_bytes())

    def test_stale_incoming_str_digest_is_not_discarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploads = Path(tmp)
            canonical = uploads / 'canonical.txt'
            canonical.write_bytes(self.ORIGINAL)
            script._get_upload_hash(str(canonical))
            incoming = uploads / 'incoming.txt'
            incoming.write_bytes(self.ORIGINAL)
            before = incoming.stat()
            script._get_upload_hash(str(incoming))
            _rewrite_inplace(incoming, self.CHANGED, before)
            # Cached incoming digest still says ORIGINAL; current bytes are
            # CHANGED, so discarding it against the ORIGINAL canonical
            # would lose the new bytes.
            with patch.object(script, 'UPLOADS_DIR', str(uploads)):
                selected, reused = script._reuse_duplicate_upload(str(incoming))
            self.assertFalse(reused)
            self.assertEqual(str(incoming), selected)
            self.assertEqual(self.CHANGED, incoming.read_bytes())
            self.assertEqual(self.ORIGINAL, canonical.read_bytes())

    def test_candidate_disappearing_before_fresh_verify_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploads, canonical, _before = self._setup_uploads(tmp)
            incoming = uploads / 'incoming.txt'
            incoming.write_bytes(self.ORIGINAL)
            real_capture = script.capture_scope_file

            def deleting_capture(root, relative, **kwargs):
                if relative == canonical.name:
                    canonical.unlink()
                return real_capture(root, relative, **kwargs)

            with patch.object(script, 'UPLOADS_DIR', str(uploads)), \
                    patch.object(script, 'capture_scope_file',
                                 side_effect=deleting_capture):
                selected, reused = script._reuse_duplicate_upload(str(incoming))
            self.assertFalse(reused)
            self.assertEqual(str(incoming), selected)
            self.assertEqual(self.ORIGINAL, incoming.read_bytes())

    def test_candidate_changed_during_fresh_verify_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploads, canonical, _before = self._setup_uploads(tmp)
            incoming = uploads / 'incoming.txt'
            incoming.write_bytes(self.ORIGINAL)
            real_capture = script.capture_scope_file

            def changed_capture(root, relative, **kwargs):
                if relative == canonical.name:
                    raise ValueError('Scope file changed while reading.')
                return real_capture(root, relative, **kwargs)

            with patch.object(script, 'UPLOADS_DIR', str(uploads)), \
                    patch.object(script, 'capture_scope_file',
                                 side_effect=changed_capture):
                selected, reused = script._reuse_duplicate_upload(str(incoming))
            self.assertFalse(reused)
            self.assertEqual(str(incoming), selected)
            self.assertEqual(self.ORIGINAL, incoming.read_bytes())
            self.assertEqual(self.ORIGINAL, canonical.read_bytes())

    def test_candidate_fresh_oserror_does_not_discard_incoming(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploads, canonical, _before = self._setup_uploads(tmp)
            incoming = uploads / 'incoming.txt'
            incoming.write_bytes(self.ORIGINAL)
            real_capture = script.capture_scope_file

            def oserror_capture(root, relative, **kwargs):
                if relative == canonical.name:
                    raise OSError('simulated fresh candidate read failure')
                return real_capture(root, relative, **kwargs)

            with patch.object(script, 'UPLOADS_DIR', str(uploads)), \
                    patch.object(script, 'capture_scope_file',
                                 side_effect=oserror_capture):
                selected, reused = script._reuse_duplicate_upload(str(incoming))
            self.assertFalse(reused)
            self.assertEqual(str(incoming), selected)
            self.assertEqual(self.ORIGINAL, incoming.read_bytes(),
                             'an unverifiable candidate must not discard incoming')
            self.assertEqual(self.ORIGINAL, canonical.read_bytes())

    def test_incoming_fresh_oserror_retains_without_scanning(self):
        with tempfile.TemporaryDirectory() as tmp:
            uploads, canonical, _before = self._setup_uploads(tmp)
            incoming = uploads / 'incoming.txt'
            incoming.write_bytes(self.ORIGINAL)
            real_capture = script.capture_scope_file
            real_hash = script._get_upload_hash
            hash_calls = []

            def oserror_capture(root, relative, **kwargs):
                if relative == incoming.name:
                    raise OSError('simulated fresh incoming read failure')
                return real_capture(root, relative, **kwargs)

            def recording_hash(path):
                hash_calls.append(path)
                return real_hash(path)

            with patch.object(script, 'UPLOADS_DIR', str(uploads)), \
                    patch.object(script, 'capture_scope_file',
                                 side_effect=oserror_capture), \
                    patch.object(script, '_get_upload_hash',
                                 side_effect=recording_hash):
                selected, reused = script._reuse_duplicate_upload(str(incoming))
            self.assertFalse(reused)
            self.assertEqual(str(incoming), selected)
            self.assertEqual([], hash_calls,
                             'unverifiable incoming must return before any candidate scan')
            self.assertEqual(self.ORIGINAL, incoming.read_bytes())
            self.assertEqual(self.ORIGINAL, canonical.read_bytes())

    def test_fresh_capture_cancelled_error_propagates_exact_instance(self):
        # CancelledError is a BaseException, not ValueError/OSError: the
        # (ValueError, OSError) fail-closed catches must not convert it
        # into a false successful "no duplicate" (path, False) result.
        for target in ('candidate', 'incoming'):
            with self.subTest(target=target), tempfile.TemporaryDirectory() as tmp:
                with script._upload_hash_lock:
                    script._upload_hash_cache.clear()
                uploads, canonical, _before = self._setup_uploads(tmp)
                incoming = uploads / 'incoming.txt'
                incoming.write_bytes(self.ORIGINAL)
                real_capture = script.capture_scope_file
                sentinel = asyncio.CancelledError('fresh capture cancelled')
                blocked = canonical.name if target == 'candidate' else incoming.name

                def cancelling_capture(root, relative, **kwargs):
                    if relative == blocked:
                        raise sentinel
                    return real_capture(root, relative, **kwargs)

                with patch.object(script, 'UPLOADS_DIR', str(uploads)), \
                        patch.object(script, 'capture_scope_file',
                                     side_effect=cancelling_capture):
                    with self.assertRaises(asyncio.CancelledError) as caught:
                        script._reuse_duplicate_upload(str(incoming))
                self.assertIs(sentinel, caught.exception)
                self.assertEqual(self.ORIGINAL, incoming.read_bytes(),
                                 'propagated cancellation must not discard incoming')
                self.assertEqual(self.ORIGINAL, canonical.read_bytes())


class UploadHandlerFreshDedupeTests(unittest.IsolatedAsyncioTestCase):
    """Real upload handlers must persist the incoming bytes, not a stale
    canonical whose current bytes differ from the cached digest."""

    ORIGINAL = b'AAAA' * 10
    CHANGED = b'BBBB' * 10

    @asynccontextmanager
    async def environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploads = root / 'uploads'
            uploads.mkdir()
            app = FastAPI()
            app.include_router(script.router)
            with patch.object(script, 'UPLOADS_DIR', str(uploads)), \
                    patch.object(script, 'DATA_DIR', str(root)):
                async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app, raise_app_exceptions=False),
                        base_url='http://fixture') as client:
                    yield root, uploads, client

    async def _upload_txt(self, client, raw, name='book.txt'):
        return await client.post(
            '/api/upload', params={'select_active': 'false'},
            files={'file': (name, raw, 'text/plain')})
    async def _upload_epub(self, client, raw, name='book.epub'):
        return await client.post(
            '/api/upload', params={'select_active': 'false'},
            files={'file': (name, raw, 'application/epub+zip')})

    async def test_txt_handler_same_size_restored_mtime_canonical_not_reused(self):
        for suffix in ('.txt', '.md'):
            with self.subTest(suffix=suffix):
                async with self.environment() as (_root, uploads, client):
                    first = await self._upload_txt(
                        client, self.ORIGINAL, f'book{suffix}')
                    self.assertEqual(200, first.status_code, first.text)
                    self.assertFalse(first.json()['reused'])
                    canonical = uploads / f'book{suffix}'
                    self.assertEqual(self.ORIGINAL, canonical.read_bytes())
                    before = canonical.stat()
                    script._get_upload_hash(str(canonical))
                    _rewrite_inplace(canonical, self.CHANGED, before)
                    second = await self._upload_txt(
                        client, self.ORIGINAL, f'again{suffix}')
                    self.assertEqual(200, second.status_code, second.text)
                    self.assertFalse(second.json()['reused'])
                    persisted = Path(second.json()['path'])
                    self.assertEqual(self.ORIGINAL, persisted.read_bytes(),
                                     'persisted selected file must hold incoming bytes')
                    self.assertEqual(self.CHANGED, canonical.read_bytes())

    async def test_txt_handler_true_duplicate_reused(self):
        async with self.environment() as (_root, uploads, client):
            first = await self._upload_txt(client, self.ORIGINAL)
            self.assertEqual(200, first.status_code, first.text)
            second = await self._upload_txt(client, self.ORIGINAL, 'again.txt')
            self.assertEqual(200, second.status_code, second.text)
            self.assertTrue(second.json()['reused'])
            self.assertEqual(self.ORIGINAL, Path(second.json()['path']).read_bytes())
            self.assertEqual(['book.txt'], sorted(p.name for p in uploads.iterdir()))

    async def test_epub_handler_same_size_restored_mtime_canonical_txt_not_reused(self):
        async with self.environment() as (_root, uploads, client):
            raw = _synthetic_epub(self.ORIGINAL)
            first = await self._upload_epub(client, raw)
            self.assertEqual(200, first.status_code, first.text)
            canonical = uploads / 'book.txt'
            self.assertEqual(self.ORIGINAL, canonical.read_bytes())
            before = canonical.stat()
            script._get_upload_hash(str(canonical))
            _rewrite_inplace(canonical, self.CHANGED, before)
            second = await self._upload_epub(client, raw, 'again.epub')
            self.assertEqual(200, second.status_code, second.text)
            self.assertFalse(second.json()['reused'])
            persisted = Path(second.json()['path'])
            self.assertEqual(self.ORIGINAL, persisted.read_bytes(),
                             'persisted EPUB canonical TXT must hold extracted bytes')
            self.assertEqual(self.CHANGED, canonical.read_bytes(),
                             'changed canonical must be left untouched')
            retained = Path(second.json()['retained_epub'])
            retained_path = retained if retained.is_absolute() else uploads / retained.name
            self.assertEqual(raw, retained_path.read_bytes())

    async def test_epub_handler_replacement_canonical_txt_not_reused(self):
        async with self.environment() as (_root, uploads, client):
            raw = _synthetic_epub(self.ORIGINAL)
            first = await self._upload_epub(client, raw)
            self.assertEqual(200, first.status_code, first.text)
            canonical = uploads / 'book.txt'
            self.assertEqual(self.ORIGINAL, canonical.read_bytes())
            before = canonical.stat()
            script._get_upload_hash(str(canonical))
            _rewrite_replacement(canonical, self.CHANGED, before)
            second = await self._upload_epub(client, raw, 'again.epub')
            self.assertEqual(200, second.status_code, second.text)
            self.assertFalse(second.json()['reused'])
            self.assertEqual(self.ORIGINAL, Path(second.json()['path']).read_bytes())
            self.assertEqual(self.CHANGED, canonical.read_bytes())

    async def test_upload_cancellation_waits_for_paused_fresh_canonical_read(self):
        """Cancelling a real upload while the new fresh canonical read is
        paused in its worker: await_upload_operation must wait for the
        admitted I/O, then cancellation propagates; no publication,
        selection (the cancelled request is admitted with
        select_active=True and would otherwise select) or wrong-bytes
        mutation may happen, and stage cleanup must leave only the
        pre-existing canonical."""
        from fastapi import UploadFile

        async with self.environment() as (root, uploads, client):
            first = await self._upload_txt(client, self.ORIGINAL)
            self.assertEqual(200, first.status_code, first.text)
            canonical = uploads / 'book.txt'
            script._get_upload_hash(str(canonical))
            uploads_before = {path.name: path.read_bytes()
                              for path in uploads.iterdir()}
            root_before = {path.name: path.read_bytes()
                           for path in root.iterdir() if path.is_file()}
            storage_before = script.get_upload_storage_bytes()
            real_capture = script.capture_scope_file
            entered, release = threading.Event(), threading.Event()

            def paused_capture(capture_root, relative, **kwargs):
                if relative == canonical.name:
                    entered.set()
                    # Finite wait: never hang the worker/cleanup forever.
                    release.wait(5)
                return real_capture(capture_root, relative, **kwargs)

            upload = UploadFile(filename='again.txt',
                                file=io.BytesIO(self.ORIGINAL))
            with patch.object(script, 'capture_scope_file',
                              side_effect=paused_capture):
                task = asyncio.create_task(
                    script.upload_file(upload, select_active=True))
                try:
                    self.assertTrue(
                        await asyncio.to_thread(entered.wait, 3),
                        'fresh canonical read was not entered')
                    task.cancel()
                    await asyncio.sleep(0.05)
                    self.assertFalse(
                        task.done(),
                        'cancellation must wait for admitted fresh-read I/O')
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
            await upload.close()
            self.assertEqual(uploads_before,
                             {path.name: path.read_bytes()
                              for path in uploads.iterdir()},
                             'no new/wrong file may be published on cancellation')
            self.assertEqual(root_before,
                             {path.name: path.read_bytes()
                              for path in root.iterdir() if path.is_file()},
                             'no book-state/provenance mutation on cancellation')
            self.assertEqual(self.ORIGINAL, canonical.read_bytes())
            self.assertEqual(storage_before, script.get_upload_storage_bytes(),
                             'cancelled stage must not remain quota-accounted')
