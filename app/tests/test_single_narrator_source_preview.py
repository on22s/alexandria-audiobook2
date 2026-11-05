"""Bounded, book-bound source preview for the single-narrator guide (#1724).

Synthetic fixtures only. The route takes no path input, reads only the active
book's recorded source, bounds the read, and binds the excerpt to the book
token + source digest so inspection cannot carry across books.
"""
import builtins
from contextlib import ExitStack
from pathlib import Path
import hashlib
import json
import os
import subprocess
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from routers import script as script_router


class SingleNarratorSourcePreviewTests(unittest.TestCase):
    def fixture(self, root, stack, text="Synthetic source text.\nSecond line."):
        source = root / 'book.txt'
        source.write_text(text, encoding='utf-8')
        (root / 'state.json').write_text(json.dumps(
            {"active_book_id": "book", "book_generation": "one",
             "input_file_path": str(source)}))
        stack.enter_context(patch.object(script_router, 'DATA_DIR', str(root)))
        app = FastAPI()
        app.include_router(script_router.router)
        return stack.enter_context(TestClient(app)), source, text

    def test_preview_is_bounded_and_bound_to_active_source(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, source, text = self.fixture(Path(tmp), stack, text="A" * 5000)
            response = client.get('/api/single-narrator/source-preview')
            self.assertEqual(200, response.status_code, response.text)
            body = response.json()
            self.assertEqual(5000, body['total_chars'])
            self.assertTrue(body['truncated'])
            self.assertEqual(4000, len(body['text_start']))
            self.assertEqual('book.txt', body['source_name'])
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),
                             body['source_sha256'])
            self.assertRegex(body['book_token'], r'^[0-9a-f]{64}$')
            self.assertEqual('utf-8', body['encoding'])

    def test_preview_tracks_source_and_book_changes(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, source, text = self.fixture(root, stack)
            first = client.get('/api/single-narrator/source-preview').json()
            source.write_text("Changed synthetic source.", encoding='utf-8')
            second = client.get('/api/single-narrator/source-preview').json()
            self.assertNotEqual(first['source_sha256'], second['source_sha256'])
            self.assertEqual("Changed synthetic source.", second['text_start'])
            # A different book (new generation) yields a different token binding.
            (root / 'state.json').write_text(json.dumps(
                {"active_book_id": "book", "book_generation": "two",
                 "input_file_path": str(source)}))
            third = client.get('/api/single-narrator/source-preview').json()
            self.assertNotEqual(first['book_token'], third['book_token'])

    def test_preview_refuses_missing_unsupported_and_outside_sources(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, source, text = self.fixture(root, stack)
            # No path input is accepted: a query param cannot escape the binding.
            response = client.get('/api/single-narrator/source-preview?path=/etc/passwd')
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual('book.txt', response.json()['source_name'])
            # Missing active source.
            source.unlink()
            self.assertEqual(404, client.get('/api/single-narrator/source-preview').status_code)
            # Unsupported format.
            epub = root / 'book.epub'
            epub.write_bytes(b'PK synthetic')
            (root / 'state.json').write_text(json.dumps(
                {"active_book_id": "book", "book_generation": "one",
                 "input_file_path": str(epub)}))
            self.assertEqual(409, client.get('/api/single-narrator/source-preview').status_code)

    def test_preview_refuses_empty_raw_docx_and_raw_epub(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, source, text = self.fixture(root, stack)
            # Empty source never enables an inspection gate.
            source.write_bytes(b'')
            self.assertEqual(400, client.get('/api/single-narrator/source-preview').status_code)
            # A raw .docx (zip bytes, no retained group) is never decoded as text.
            raw_docx = root / 'raw.docx'
            raw_docx.write_bytes(b'PK\x03\x04' + b'synthetic zip bytes')
            (root / 'state.json').write_text(json.dumps(
                {"active_book_id": "book", "book_generation": "one",
                 "input_file_path": str(raw_docx)}))
            self.assertEqual(409, client.get('/api/single-narrator/source-preview').status_code)
            # A raw .epub likewise; EPUB-derived extracted text is a plain file and previews.
            raw_epub = root / 'raw.epub'
            raw_epub.write_bytes(b'PK\x03\x04' + b'synthetic zip bytes')
            (root / 'state.json').write_text(json.dumps(
                {"active_book_id": "book", "book_generation": "one",
                 "input_file_path": str(raw_epub)}))
            self.assertEqual(409, client.get('/api/single-narrator/source-preview').status_code)
            derived = root / 'derived-from-epub.txt'
            derived.write_text('EPUB-derived synthetic text.', encoding='utf-8')
            (root / 'state.json').write_text(json.dumps(
                {"active_book_id": "book", "book_generation": "one",
                 "input_file_path": str(derived)}))
            body = client.get('/api/single-narrator/source-preview').json()
            self.assertEqual('EPUB-derived synthetic text.', body['text_start'])

    def test_preview_accepts_canonical_docx_group_and_refuses_foreign_root(self):
        from docx_preview import POLICY  # noqa: PLC0415  (test-only import)
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            uploads = root / 'uploads'
            uploads.mkdir()
            text = 'Canonical DOCX synthetic text.'
            text_bytes = text.encode('utf-8')
            original_bytes = b'PK\x03\x04' + b'synthetic original'
            stem = 'book-aa2docx-' + 'a' * 32
            names = {'original': stem + '.docx', 'text': stem + '.txt',
                     'receipt': stem + '.docx-receipt.json'}
            original_sha = hashlib.sha256(original_bytes).hexdigest()
            text_sha = hashlib.sha256(text_bytes).hexdigest()
            preview = {'policy': POLICY, 'policy_text': 'synthetic policy', 'warnings': [],
                       'original_sha256': original_sha, 'text_sha256': text_sha}
            digest = hashlib.sha256(json.dumps(preview, sort_keys=True, ensure_ascii=False,
                separators=(',', ':')).encode('utf-8')).hexdigest()
            receipt = dict(preview, preview_digest=digest, version=1, filenames=names,
                           uploaded_filename='book.docx', confirmed=True)
            (uploads / names['original']).write_bytes(original_bytes)
            (uploads / names['text']).write_bytes(text_bytes)
            (uploads / names['receipt']).write_text(json.dumps(receipt, sort_keys=True), encoding='utf-8')
            (root / 'state.json').write_text(json.dumps(
                {"active_book_id": "book", "book_generation": "one",
                 "input_file_path": str(uploads / names['text'])}))
            stack.enter_context(patch.object(script_router, 'DATA_DIR', str(root)))
            body = script_router._get_single_narrator_source_preview()
            self.assertEqual(text, body['text_start'])
            self.assertEqual(hashlib.sha256(text_bytes).hexdigest(), body['source_sha256'])
            # The same group outside the owning root is refused, never previewed.
            from docx_publication import capture_docx_group
            with self.assertRaisesRegex(ValueError, 'outside the owning root'):
                capture_docx_group(uploads / names['text'], owning_root=uploads.parent / 'elsewhere')

    def test_preview_refuses_swapped_source_parent(self):
        # Swapped-parent negative control: after the route's path checks but
        # before the confined read, swap the source's parent directory for
        # an alias to outside bytes (junction on Windows, symlink on POSIX —
        # the same owned fixture and the same mid-read race on both). The
        # descriptor-confined read must refuse instead of previewing outside
        # bytes. If the race cannot be established on this host the test
        # fails loudly; it never skips (Linux CI rejects skips and a skip
        # would pretend the race was exercised). The Windows junction
        # evidence is preserved by OS and by the external parent probe.
        with tempfile.TemporaryDirectory(prefix='aa2-1724-preview-parent-') as name:
            owned = Path(name)
            data, outside = owned / 'data', owned / 'outside-fixture'
            uploads, saved = data / 'uploads', data / 'uploads-original'
            uploads.mkdir(parents=True)
            outside.mkdir()
            source = uploads / 'source.txt'
            source.write_text('Inside synthetic source.', encoding='utf-8')
            sentinel = b'Outside synthetic sentinel. Must not be previewed.'
            (outside / 'source.txt').write_bytes(sentinel)
            (data / 'state.json').write_text(json.dumps(
                {'active_book_id': 'synthetic', 'book_generation': 'one',
                 'input_file_path': str(source)}), encoding='utf-8')
            (data / 'annotated_script.json').write_text(
                '[{"speaker":"NARRATOR","text":"Inside synthetic source."}]', encoding='utf-8')
            (data / 'voice_config.json').write_text('{}', encoding='utf-8')
            real_open = builtins.open
            real_os_open = os.open
            swap = {'injected': False, 'alias_created': False}

            def inject_parent_swap(file):
                try:
                    is_source = os.path.normcase(os.path.abspath(os.fspath(file))) == os.path.normcase(str(source))
                except TypeError:
                    is_source = False
                if is_source and not swap['injected']:
                    swap['injected'] = True
                    os.rename(uploads, saved)
                    if os.name == 'nt':
                        subprocess.run(['cmd.exe', '/d', '/c', 'mklink', '/J', str(uploads), str(outside)],
                                       capture_output=True, check=True)
                    else:
                        os.symlink(outside, uploads, target_is_directory=True)
                    swap['alias_created'] = True

            def inject_open(file, mode='r', *args, **kwargs):
                if mode == 'rb':
                    inject_parent_swap(file)
                return real_open(file, mode, *args, **kwargs)

            def inject_os_open(file, flags, *args, **kwargs):
                inject_parent_swap(file)
                return real_os_open(file, flags, *args, **kwargs)

            response, error = None, None
            try:
                with ExitStack() as stack:
                    stack.enter_context(patch.object(script_router, 'DATA_DIR', str(data)))
                    stack.enter_context(patch.object(builtins, 'open', inject_open))
                    stack.enter_context(patch.object(os, 'open', inject_os_open))
                    try:
                        response = script_router._get_single_narrator_source_preview()
                    except Exception as exc:
                        error = exc
            finally:
                if saved.exists() and os.path.lexists(uploads):
                    if os.path.islink(uploads):
                        os.unlink(uploads)
                    else:
                        os.rmdir(uploads)
                if saved.exists():
                    os.rename(saved, uploads)
            self.assertTrue(swap['injected'],
                            'Swap boundary was not exercised; inspect the current capture mechanism.')
            self.assertTrue(swap['alias_created'],
                            'Swapped-parent alias could not be created; no qualification.')
            escaped = isinstance(response, dict) and response.get('source_sha256') == hashlib.sha256(sentinel).hexdigest()
            self.assertFalse(escaped, 'route previewed outside sentinel bytes after parent swap')
            self.assertIsNotNone(error, 'repaired route must refuse swapped source with an error')
            self.assertIsInstance(error, HTTPException)
            self.assertEqual(409, error.status_code)
            self.assertEqual(sentinel, (outside / 'source.txt').read_bytes(), 'outside fixture must be untouched')

    def test_preview_refuses_json_source(self):
        # JSON is a retained annotated script, not original source prose:
        # refuse honestly with saved-script guidance, never relabel JSON.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, source, text = self.fixture(root, stack)
            script_json = root / 'book.json'
            script_json.write_bytes(b'[{"speaker":"ALICE","text":"Synthetic JSON source line."}]')
            (root / 'state.json').write_text(json.dumps(
                {"active_book_id": "book", "book_generation": "one",
                 "input_file_path": str(script_json)}))
            response = client.get('/api/single-narrator/source-preview')
            self.assertEqual(409, response.status_code, response.text)
            self.assertIn('saved script', response.json()['detail'])

    def test_preview_refuses_garbled_and_blank_text(self):
        # #1724 C034/C043: undecodable bytes and whitespace-only sources are
        # clear refusals, never confirmable excerpts.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, source, text = self.fixture(root, stack)
            source.write_bytes(b'hello\x81world')
            response = client.get('/api/single-narrator/source-preview')
            self.assertEqual(409, response.status_code, response.text)
            self.assertIn('could not be decoded', response.json()['detail'])
            source.write_text('  \n\n  \n', encoding='utf-8')
            response = client.get('/api/single-narrator/source-preview')
            self.assertEqual(400, response.status_code, response.text)
            self.assertIn('empty', response.json()['detail'])

    def test_preview_distinguishes_corrupt_from_missing_state(self):
        # #1724 C044: corrupt state.json is a distinct refusal from missing.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, source, text = self.fixture(root, stack)
            (root / 'state.json').write_bytes(b'{corrupt')
            response = client.get('/api/single-narrator/source-preview')
            self.assertEqual(409, response.status_code, response.text)
            self.assertIn('corrupt', response.json()['detail'])
            (root / 'state.json').unlink()
            response = client.get('/api/single-narrator/source-preview')
            self.assertEqual(400, response.status_code, response.text)
            self.assertIn('No input file selected', response.json()['detail'])

    def test_preview_refuses_hard_linked_source_as_integrity_failure(self):
        # #1724 C003: a hard-linked source is a 409 integrity refusal, not a
        # 400 upload-size misclassification.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, source, text = self.fixture(root, stack)
            linked = root / 'linked.txt'
            os.link(source, linked)
            (root / 'state.json').write_text(json.dumps(
                {"active_book_id": "book", "book_generation": "one",
                 "input_file_path": str(linked)}))
            response = client.get('/api/single-narrator/source-preview')
            self.assertEqual(409, response.status_code, response.text)
            self.assertIn('could not be verified', response.json()['detail'])


    def test_preview_refuses_outside_data_dir(self):
        # DS064: an input_file_path outside DATA_DIR is a 403 refusal.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            outside_dir = root.parent
            outside = root.parent / (root.name + '-outside.txt')
            outside.write_text('Outside synthetic source.', encoding='utf-8')
            try:
                client, source, text = self.fixture(root, stack)
                (root / 'state.json').write_text(json.dumps(
                    {"active_book_id": "book", "book_generation": "one",
                     "input_file_path": str(outside)}))
                response = client.get('/api/single-narrator/source-preview')
                self.assertEqual(403, response.status_code, response.text)
                self.assertIn('outside the book data directory', response.json()['detail'])
            finally:
                if outside.exists():
                    outside.unlink()

    def test_preview_accepts_real_canonical_docx_group(self):
        # DS065: a group produced by the real publish/extract helpers, not
        # a hand-built receipt. Original source bytes are maintained.
        from docx_preview import extract_docx_preview
        from docx_publication import publish_docx_upload
        from source_upload_publication import create_source_upload_stage
        from tests.test_docx_preview import archive as docx_archive
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            uploads = root / 'uploads'
            uploads.mkdir()
            raw = docx_archive()
            stage = create_source_upload_stage(root, limit=len(raw))
            try:
                stage.write(raw)
                path, names = publish_docx_upload(stage, extract_docx_preview(raw),
                    'book.docx', remaining=1000000)
            finally:
                stage.close()
            (root / 'state.json').write_text(json.dumps(
                {"active_book_id": "book", "book_generation": "one",
                 "input_file_path": path}))
            stack.enter_context(patch.object(script_router, 'DATA_DIR', str(root)))
            body = script_router._get_single_narrator_source_preview()
            self.assertIn('Synthetic', body['text_start'])
            self.assertTrue(body['source_sha256'])
            self.assertEqual(raw, (root / names['original']).read_bytes(),
                             'original DOCX bytes are retained untouched')

    def test_preview_bom_and_leading_whitespace_policy(self):
        # Synthetic BOM controls: UTF-8 BOM is represented by the canonical
        # decoder and previews; UTF-16 BOM / NUL refuse. Leading whitespace
        # then prose previews; blank excerpt vs empty source are distinct.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, source, text = self.fixture(root, stack)
            # UTF-8 BOM is ordinary text to the canonical decoder.
            source.write_bytes(b'\xef\xbb\xbf' + 'UTF-8 BOM text.'.encode('utf-8'))
            body = client.get('/api/single-narrator/source-preview').json()
            self.assertIn('UTF-8 BOM text', body['text_start'])
            # UTF-16LE BOM refuses.
            source.write_bytes(b'\xff\xfe' + 'UTF-16 text.'.encode('utf-16-le'))
            self.assertEqual(409, client.get('/api/single-narrator/source-preview').status_code)
            # NUL refuses.
            source.write_bytes('Contains \x00 NUL.'.encode('utf-8'))
            self.assertEqual(409, client.get('/api/single-narrator/source-preview').status_code)
            # Leading whitespace then prose previews.
            source.write_text('   \n\nLeading whitespace prose here.', encoding='utf-8')
            body = client.get('/api/single-narrator/source-preview').json()
            self.assertIn('Leading whitespace prose', body['text_start'])
            # Blank excerpt (whitespace-only) is distinct from empty file.
            source.write_text('   \n\n   ', encoding='utf-8')
            response = client.get('/api/single-narrator/source-preview')
            self.assertEqual(400, response.status_code)
            self.assertIn('empty', response.json()['detail'])
            source.write_bytes(b'')
            response = client.get('/api/single-narrator/source-preview')
            self.assertEqual(400, response.status_code)

    def test_preview_refuses_duplicate_or_nonfinite_state(self):
        # Bounded strict reader: duplicate keys / nonfinite reject as corrupt.
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            client, source, text = self.fixture(root, stack)
            (root / 'state.json').write_bytes(
                b'{"active_book_id":"book","active_book_id":"book","input_file_path":"x"}')
            response = client.get('/api/single-narrator/source-preview')
            self.assertEqual(409, response.status_code, response.text)
            self.assertIn('corrupt', response.json()['detail'])


if __name__ == '__main__':
    unittest.main()
