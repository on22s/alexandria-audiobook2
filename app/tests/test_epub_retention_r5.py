"""R5 bounded EPUB retention repair (combined #1720/#1779).

Real ``routers.script.upload_file`` with real ``OwnedSourceUploadStage`` /
actual publication, persisted synthetic fixtures only. Covers the four R5
defect clusters: original-bytes authority, rollback/unknown honesty,
bounded inventory, and quota/group-identity coherence.
"""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
import unittest
from unittest.mock import patch
import zipfile

from fastapi import FastAPI, UploadFile
import httpx
from routers import script

TEXT = b'Shareable source paragraph.'


def epub(title='Fixture title', *, cover=None, metadata=True, text=TEXT, extra=None):
    container = ('<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfile full-path="book.opf"/></container>')
    meta = ('<metadata><dc:title>' + title + '</dc:title>'
            '<dc:creator>Fixture Author</dc:creator></metadata>') if metadata else ''
    cover_item = '<item id="cover" href="cover.png" media-type="image/png" properties="cover-image"/>' if cover else ''
    package = ('<package xmlns="http://www.idpf.org/2007/opf" xmlns:dc="http://purl.org/dc/elements/1.1/">'
               + meta + '<manifest><item id="body" href="body.xhtml" media-type="application/xhtml+xml"/>'
               + cover_item + '</manifest><spine><itemref idref="body"/></spine></package>')
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as archive:
        for name, data in [('META-INF/container.xml', container), ('book.opf', package),
                           ('body.xhtml', '<html><body><p>' + text.decode() + '</p></body></html>')]:
            archive.writestr(zipfile.ZipInfo(name), data)
        if cover:
            archive.writestr(zipfile.ZipInfo('cover.png'), cover)
        if extra:
            for name, data in extra.items():
                archive.writestr(zipfile.ZipInfo(name), data)
    return raw.getvalue()


def _snapshot(uploads):
    return {path.name: path.read_bytes() for path in uploads.iterdir()}


class R5InventoryTests(unittest.TestCase):
    def test_inventory_is_bounded_without_sorting_all(self):
        """A bounded scan must not enumerate every directory entry."""
        from source_upload_publication import get_retained_epub_inventory
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(10):
                (root / f'book{i}.txt').write_text('x', encoding='utf-8')
            # Count how many directory entries the implementation consumes.
            real_scandir = os.scandir
            consumed = {'n': 0}
            class CountingEntries:
                def __init__(self, entries):
                    self._ctx = entries
                    self._it = iter(entries)
                def __enter__(self):
                    return self
                def __exit__(self, *exc):
                    try:
                        self._ctx.close()
                    except Exception:
                        pass
                    return False
                def __iter__(self):
                    return self
                def __next__(self):
                    consumed['n'] += 1
                    return next(self._it)
            def counting_scandir(path):
                return CountingEntries(real_scandir(path))
            with patch('source_upload_publication.os.scandir', side_effect=counting_scandir):
                result = get_retained_epub_inventory(str(root), 10 * 1024 * 1024, scan_limit=3, group_limit=10)
            # Bounded: at most scan_limit + 1 entry may be consumed to
            # determine completeness. Unchanged code sorts everything first.
            self.assertLessEqual(consumed['n'], 4,
                                 f"inventory consumed {consumed['n']} entries for scan_limit=3")
            self.assertFalse(result['scan_complete'])
            self.assertEqual(3, result['scanned_entries'])
            self.assertLessEqual(len(result['groups']), 10)

    def test_inventory_invalid_caps_rejected(self):
        from source_upload_publication import get_retained_epub_inventory
        with tempfile.TemporaryDirectory() as tmp:
            for bad in (-1, -4096):
                with self.assertRaises(ValueError, msg=f"scan_limit={bad} must be rejected"):
                    get_retained_epub_inventory(tmp, 1024, scan_limit=bad)
            for bad in (-1,):
                with self.assertRaises(ValueError, msg=f"group_limit={bad} must be rejected"):
                    get_retained_epub_inventory(tmp, 1024, group_limit=bad)
            with self.assertRaises(ValueError):
                get_retained_epub_inventory(tmp, 1024, scan_limit=1.5)
            with self.assertRaises(ValueError):
                get_retained_epub_inventory(tmp, 1024, group_limit='10')

    def test_inventory_groups_companion_orphan_and_links(self):
        from source_upload_publication import get_retained_epub_inventory
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'book.txt').write_text('Hello synthetic.', encoding='utf-8')
            (root / 'book.source.epub').write_bytes(b'PK-fake-original')
            (root / 'book.txt.publication.json').write_text('{}', encoding='utf-8')
            # Orphan companion and orphan retained.
            (root / 'lost.txt.publication.json').write_text('{}', encoding='utf-8')
            (root / 'gone.source.epub').write_bytes(b'PK-gone')
            # Collision-numbered group.
            (root / 'book_2.txt').write_text('Hello synthetic 2.', encoding='utf-8')
            (root / 'book_2.source.epub').write_bytes(b'PK-second')
            (root / 'book_2.txt.publication.json').write_text('{}', encoding='utf-8')
            # Non-matching file still counts toward scanned entries.
            (root / 'notes.bin').write_bytes(b'12345')
            # Foreign hard link: same inode under the retained name.
            target = root / 'link-target.bin'
            target.write_bytes(b'PK-link')
            try:
                os.link(str(target), str(root / 'linked.source.epub'))
                has_link = True
            except OSError:
                has_link = False
            before = {p.name: p.read_bytes() for p in root.iterdir() if not (has_link and p.name == 'linked.source.epub')}
            with patch('builtins.open', side_effect=AssertionError('No contents read')):
                result = get_retained_epub_inventory(str(root), 10 * 1024 * 1024)
            by_txt = {g['txt_filename']: g for g in result['groups']}
            # Companion joins the same group as TXT + retained.
            self.assertIn('book.txt', by_txt)
            self.assertEqual({'book.txt', 'book.source.epub', 'book.txt.publication.json'},
                             {f['filename'] for f in by_txt['book.txt']['files']})
            self.assertEqual('unverified', by_txt['book.txt']['state'])
            self.assertIn('book_2.txt', by_txt)
            self.assertEqual({'book_2.txt', 'book_2.source.epub', 'book_2.txt.publication.json'},
                             {f['filename'] for f in by_txt['book_2.txt']['files']})
            # Orphan companion groups with its TXT name, incomplete.
            self.assertIn('lost.txt', by_txt)
            self.assertEqual(['lost.txt.publication.json'],
                             [f['filename'] for f in by_txt['lost.txt']['files']])
            self.assertEqual('incomplete/unverified', by_txt['lost.txt']['state'])
            self.assertIn('gone.txt', by_txt)
            self.assertEqual(['gone.source.epub'],
                             [f['filename'] for f in by_txt['gone.txt']['files']])
            self.assertEqual('incomplete/unverified', by_txt['gone.txt']['state'])
            if has_link:
                self.assertIn('linked.txt', by_txt)
                self.assertEqual('unavailable', by_txt['linked.txt']['state'])
                # Hard link is not trusted quota bytes.
                self.assertLess(result['observed_named_bytes'],
                                sum(len(v) for v in before.values()) + 5 + len(b'PK-link'))
            # Partial byte count includes all named regular files in the
            # bounded scan (non-matching notes.bin included), excluding links.
            self.assertIn('companion', result['notice'].lower())
            self.assertIn('No automatic deletion', result['notice'])
            # No automatic deletion / no modification.
            after = {p.name for p in root.iterdir()}
            self.assertEqual(set(before) | ({'linked.source.epub'} if has_link else set()), after)

    def test_inventory_scan_limit_counts_nonmatching_entries(self):
        from source_upload_publication import get_retained_epub_inventory
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for i in range(5):
                (root / f'junk{i}.bin').write_bytes(b'x' * (i + 1))
            result = get_retained_epub_inventory(str(root), 1024, scan_limit=3)
            self.assertEqual(3, result['scanned_entries'])
            self.assertFalse(result['scan_complete'])
            # All scanned bytes are partial observed named bytes, grouped or not.
            self.assertLessEqual(result['observed_named_bytes'], sum(range(1, 6)))
            self.assertGreater(result['observed_named_bytes'], 0)


class R5UploadTests(unittest.IsolatedAsyncioTestCase):
    @asynccontextmanager
    async def environment(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); uploads = root / 'uploads'; uploads.mkdir()
            app = FastAPI(); app.include_router(script.router)
            with patch.object(script, 'UPLOADS_DIR', str(uploads)), patch.object(script, 'DATA_DIR', str(root)):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app, raise_app_exceptions=False),
                                             base_url='http://fixture') as client:
                    yield root, uploads, client

    async def upload(self, client, raw, name='book.epub', select=False):
        return await client.post('/api/upload', params={'select_active': str(select).lower()},
                                 files={'file': (name, raw, 'application/epub+zip')})

    async def test_new_upload_exact_coherence(self):
        """New upload: retained bytes == original, TXT == extracted, companion bound."""
        async with self.environment() as (root, uploads, client):
            raw = epub()
            response = await self.upload(client, raw)
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual('book.source.epub', response.json()['retained_epub'])
            self.assertEqual(raw, (uploads / 'book.source.epub').read_bytes())
            self.assertEqual(TEXT, (uploads / 'book.txt').read_bytes())
            record = json.loads((uploads / 'book.txt.publication.json').read_bytes())
            self.assertEqual(hashlib.sha256(TEXT).hexdigest(), record['binding']['sha256'])
            self.assertEqual(hashlib.sha256(raw).hexdigest(), record['candidates'][0]['origin']['sha256'])
            # Group coherence: one TXT, one retained, one companion, same stem.
            self.assertEqual(['book.source.epub', 'book.txt', 'book.txt.publication.json'],
                             sorted(p.name for p in uploads.iterdir()))

    async def test_same_bytes_renamed_duplicate_no_new_charge(self):
        async with self.environment() as (root, uploads, client):
            raw = epub()
            first = await self.upload(client, raw, 'first.epub')
            self.assertEqual(200, first.status_code, first.text)
            before = _snapshot(uploads)
            storage_before = script.get_upload_storage_bytes()
            second = await self.upload(client, raw, 'renamed.epub')
            self.assertEqual(200, second.status_code, second.text)
            self.assertTrue(second.json()['reused'])
            self.assertEqual('first.source.epub', second.json()['retained_epub'])
            self.assertEqual(before, _snapshot(uploads))
            self.assertEqual(storage_before, script.get_upload_storage_bytes())
            self.assertEqual(1, len(json.loads((uploads / 'first.txt.publication.json').read_bytes())['candidates']))

    async def test_different_original_same_text_refused_before_effects(self):
        """Different EPUB, identical extracted text: explicit 409, no effects."""
        async with self.environment() as (root, uploads, client):
            first = epub()
            second = epub('Other edition', extra={'cover2.txt': 'different cover bytes'})
            # Ensure different archive identity but identical extracted text.
            self.assertNotEqual(hashlib.sha256(first).hexdigest(), hashlib.sha256(second).hexdigest())
            response = await self.upload(client, first)
            self.assertEqual(200, response.status_code, response.text)
            before = _snapshot(uploads)
            before_companion = (uploads / 'book.txt.publication.json').read_bytes()
            response2 = await self.upload(client, second, 'book.epub')
            self.assertEqual(409, response2.status_code, response2.text)
            self.assertIn('retained', response2.json()['detail'].lower())
            self.assertEqual(before, _snapshot(uploads))
            self.assertEqual(before_companion, (uploads / 'book.txt.publication.json').read_bytes())
            self.assertEqual(first, (uploads / 'book.source.epub').read_bytes())

    async def test_retained_authority_ignores_cached_hash_after_timestamp_preserving_restore(self):
        for replacement in (False, True):
            with self.subTest(replacement=replacement):
                async with self.environment() as (_, uploads, client):
                    original = epub('Fixture title')
                    restored = epub('Fixture other')
                    self.assertEqual(len(original), len(restored))
                    self.assertNotEqual(original, restored)
                    self.assertEqual(200, (await self.upload(client, original)).status_code)
                    retained = uploads / 'book.source.epub'
                    # The shared dedupe cache may already hold the original
                    # version; authority must read bytes independently of it.
                    script._get_upload_hash(str(retained))
                    before = retained.stat()
                    if replacement:
                        staging = uploads / 'restored.epub'
                        staging.write_bytes(restored)
                        os.replace(staging, retained)
                    else:
                        retained.write_bytes(restored)
                    os.utime(retained, ns=(before.st_atime_ns, before.st_mtime_ns))
                    self.assertEqual(before.st_size, retained.stat().st_size)
                    self.assertEqual(before.st_mtime_ns, retained.stat().st_mtime_ns)
                    prior = _snapshot(uploads)
                    response = await self.upload(client, original)
                    self.assertEqual(409, response.status_code, response.text)
                    self.assertEqual(prior, _snapshot(uploads))
                    self.assertEqual(restored, retained.read_bytes())

    async def test_different_title_same_text_also_refused(self):
        async with self.environment() as (root, uploads, client):
            self.assertEqual(200, (await self.upload(client, epub())).status_code)
            before = _snapshot(uploads)
            response = await self.upload(client, epub('Reissued title'), 'other.epub')
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, _snapshot(uploads))

    async def test_legacy_txt_attach_correct_bytes_and_provenance(self):
        """Missing retained is really attached with the new original's bytes."""
        async with self.environment() as (root, uploads, client):
            raw = epub()
            self.assertEqual(200, (await self.upload(client, raw)).status_code)
            retained = uploads / 'book.source.epub'
            retained.unlink()
            self.assertFalse(retained.exists())
            before_txt = (uploads / 'book.txt').read_bytes()
            before_companion = (uploads / 'book.txt.publication.json').read_bytes()
            response = await self.upload(client, raw, 'reupload.epub')
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual('book.source.epub', response.json()['retained_epub'])
            self.assertEqual(raw, retained.read_bytes())
            self.assertEqual(before_txt, (uploads / 'book.txt').read_bytes())
            # Same origin: companion unchanged.
            self.assertEqual(before_companion, (uploads / 'book.txt.publication.json').read_bytes())

    async def test_stale_provenance_and_retained_tamper_refused(self):
        """Tampered retained bytes are never silently reused."""
        async with self.environment() as (root, uploads, client):
            raw = epub()
            self.assertEqual(200, (await self.upload(client, raw)).status_code)
            retained = uploads / 'book.source.epub'
            tampered = b'PK-tampered-bytes-differ-from-original'
            retained.write_bytes(tampered)
            before = _snapshot(uploads)
            response = await self.upload(client, raw, 'book.epub')
            self.assertEqual(409, response.status_code, response.text)
            detail = response.json()['detail'].lower()
            self.assertTrue('retained' in detail or 'provenance' in detail or 'mismatch' in detail, detail)
            self.assertEqual(before, _snapshot(uploads))
            self.assertEqual(tampered, retained.read_bytes())

    async def test_collision_foreign_same_basename_preserves_foreign(self):
        """A foreign file at the expected retained name is never overwritten/reused."""
        async with self.environment() as (root, uploads, client):
            raw = epub()
            self.assertEqual(200, (await self.upload(client, raw)).status_code)
            retained = uploads / 'book.source.epub'
            retained.unlink()
            foreign = b'foreign-bytes-not-the-original'
            retained.write_bytes(foreign)
            before = _snapshot(uploads)
            response = await self.upload(client, raw, 'book.epub')
            self.assertEqual(409, response.status_code, response.text)
            self.assertEqual(before, _snapshot(uploads))
            self.assertEqual(foreign, retained.read_bytes())

    async def test_quota_at_retained_and_companion_leaves_no_orphan(self):
        """New upload that cannot fit retained+companion+TXT is 413 with no new files."""
        async with self.environment() as (root, uploads, client):
            raw = epub(cover=b'x' * 4096)
            # MAX covers retained alone but not retained+companion+TXT.
            # No existing file, so this is the new-upload path (canonical None).
            with patch.object(script, 'MAX_UPLOAD_STORAGE_BYTES', len(raw) + len(TEXT) + 10):
                response = await self.upload(client, raw)
            self.assertEqual(413, response.status_code, response.text)
            self.assertEqual({}, _snapshot(uploads))
            self.assertFalse((Path(str(uploads / 'book.txt'))).exists())
            # No orphan retained: quota failure is before any publish.
            self.assertEqual([], [p.name for p in uploads.iterdir() if p.name.endswith('.source.epub')])

    async def test_legacy_attach_quota_refusal_leaves_existing_identical(self):
        async with self.environment() as (root, uploads, client):
            raw = epub()
            self.assertEqual(200, (await self.upload(client, raw)).status_code)
            (uploads / 'book.source.epub').unlink()
            before = _snapshot(uploads)
            storage_now = script.get_upload_storage_bytes()
            with patch.object(script, 'MAX_UPLOAD_STORAGE_BYTES', storage_now + 4):
                response = await self.upload(client, raw, 'book.epub')
            self.assertEqual(413, response.status_code, response.text)
            self.assertEqual(before, _snapshot(uploads))
            self.assertFalse((uploads / 'book.source.epub').exists())

    async def test_definitive_failure_before_txt_retains_and_reports(self):
        """Definitive pre-TXT failure keeps the confirmed retained file and says so."""
        async with self.environment() as (root, uploads, client):
            raw = epub()
            with patch.object(script, 'publish_completed_upload',
                              side_effect=ValueError('controlled definitive TXT failure')):
                response = await self.upload(client, raw)
            self.assertEqual(409, response.status_code, response.text)
            retained = uploads / 'book.source.epub'
            self.assertTrue(retained.exists(), 'confirmed retained must remain as recovery, not be unlinked')
            self.assertEqual(raw, retained.read_bytes())
            # No TXT/companion was published under that stem.
            self.assertEqual(['book.source.epub'], sorted(p.name for p in uploads.iterdir()))
            detail = response.json()['detail'].lower()
            self.assertIn('retained', detail)
            self.assertIn('book.source.epub', detail)
            # Quota still counts the retained orphan.
            self.assertEqual(len(raw), script.get_upload_storage_bytes())

    async def test_unknown_publish_after_commit_preserves(self):
        """Unknown outcome after commit keeps every published group member."""
        async with self.environment() as (root, uploads, client):
            raw = epub()
            real_publish = script.publish_completed_upload
            def unknown_after_commit(*args, **kwargs):
                # Real publish commits, then the outcome is unknown.
                result = real_publish(*args, **kwargs)
                raise OSError('unknown commit outcome')
            with patch.object(script, 'publish_completed_upload', side_effect=unknown_after_commit):
                response = await self.upload(client, raw)
            # Unknown OSError stays unknown: HTTP 500 (direct callers see
            # OSError) and every published member remains for recovery.
            # The failure state is the visible group itself, not the reply
            # body alone.
            self.assertEqual(500, response.status_code, response.text)
            # Retained, TXT and companion all remain visible for recovery.
            self.assertEqual(raw, (uploads / 'book.source.epub').read_bytes())
            self.assertEqual(TEXT, (uploads / 'book.txt').read_bytes())
            self.assertTrue((uploads / 'book.txt.publication.json').exists())
            # Inventory shows the unknown-outcome group as recovery material.
            from source_upload_publication import get_retained_epub_inventory
            inv = get_retained_epub_inventory(str(uploads), 10 * 1024 * 1024)
            by_txt = {g['txt_filename']: g for g in inv['groups']}
            self.assertIn('book.txt', by_txt)
            self.assertEqual('unverified', by_txt['book.txt']['state'])

    async def test_cancellation_preserves_ownership(self):
        """Cancellation during provenance apply preserves the visible group."""
        async with self.environment() as (root, uploads, client):
            self.assertEqual(200, (await self.upload(client, epub())).status_code)
            before = _snapshot(uploads)
            entered, release = threading.Event(), threading.Event()
            apply = script.apply_publication_source_update
            def paused(update):
                apply(update); entered.set()
                if not release.wait(3):
                    raise TimeoutError('fixture not released')
            upload = UploadFile(filename='cancelled.epub', file=io.BytesIO(epub()))
            # Same-bytes duplicate: applying a no-op update under cancellation
            # must not free or delete the existing retained group.
            with patch.object(script, 'apply_publication_source_update', side_effect=paused):
                task = asyncio.create_task(script.upload_file(upload, select_active=False))
                try:
                    self.assertTrue(await asyncio.to_thread(entered.wait, 2))
                    task.cancel(); await asyncio.sleep(.02)
                    self.assertFalse(task.done())
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 3)
            await upload.close()
            self.assertEqual(before, _snapshot(uploads))
            self.assertTrue((uploads / 'book.source.epub').exists())


if __name__ == '__main__':
    unittest.main()
