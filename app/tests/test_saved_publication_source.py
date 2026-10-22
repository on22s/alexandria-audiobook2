from contextlib import contextmanager, ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import core
from epub_publication import get_epub_publication_candidate
from publication_source import (get_upload_publication_update, apply_publication_source_update,
    get_publication_source_capture, get_publication_source_document, get_publication_source_path)
from routers import scripts_library as routes
from tests import test_saved_book_publication as fixtures
from tests.test_upload_publication_source import epub, TEXT
from tests.test_m4b_cover_admission import image_bytes


class SavedPublicationSourceTests(unittest.TestCase):
    @contextmanager
    def fixture(self):
        with fixtures.SavedBookPublicationTests().fixture() as (root, api), ExitStack() as stack:
            source = root / 'uploads/source.txt'; source.write_bytes(TEXT)
            candidate = get_epub_publication_candidate(epub(), filename='origin.epub',
                                                       text_sha256=hashlib.sha256(TEXT).hexdigest())
            apply_publication_source_update(get_upload_publication_update(
                source, candidate, 48 * 1024**2, source_limit=512 * 1024**2))
            state = {'active_book_id': 'new-book', 'book_generation': 'generation-a', 'input_file_path': str(source)}
            (root / 'state.json').write_text(json.dumps(state))
            for module in (routes, core):
                for key, name in (('AUDIOBOOK_PATH', 'book.mp3'), ('M4B_PATH', 'book.m4b'), ('CHUNKS_PATH', 'chunks.json')):
                    stack.enter_context(patch.object(module, key, str(root / name)))
            with TestClient(api, raise_server_exceptions=False) as client:
                yield root, client

    def test_save_load_list_and_delete_keep_exact_origin_and_script_binding(self):
        with self.fixture() as (root, client):
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
            saved = root / 'scripts/book.json'
            captured = get_publication_source_capture(saved)
            self.assertEqual('current', captured['binding_status'])
            self.assertEqual({'kind': 'script', 'sha256': hashlib.sha256(saved.read_bytes()).hexdigest()},
                             captured['document']['binding'])
            self.assertEqual('origin.epub', captured['document']['candidates'][0]['origin']['filename'])
            self.assertEqual(['book'], [row['name'] for row in client.get('/api/scripts').json()])
            self.assertEqual(400, client.post('/api/scripts/save', json={'name': 'bad.json.publication'}).status_code)
            response = client.post('/api/scripts/load', json={'name': 'book'})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(str(saved), json.loads((root / 'state.json').read_bytes())['input_file_path'])
            self.assertEqual('current', get_publication_source_capture(saved)['binding_status'])
            self.assertEqual(200, client.delete('/api/scripts/book').status_code)
            self.assertEqual({}, fixtures.SavedBookPublicationTests().artifacts(root))

    def test_stale_input_hints_are_preserved_without_certifying_the_new_script(self):
        with self.fixture() as (root, client):
            source = root / 'uploads/source.txt'
            original = Path(get_publication_source_path(source)).read_bytes()
            source.write_bytes(b'Changed source words.')
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
            saved = root / 'scripts/book.json'
            self.assertEqual(original, Path(get_publication_source_path(saved)).read_bytes())
            self.assertEqual('stale', get_publication_source_capture(saved)['binding_status'])

    def test_corrupt_hint_blocks_save_or_load_before_book_files_change(self):
        for corrupt in (b'not JSON', b'[' * 2000 + b']' * 2000):
            with self.subTest(size=len(corrupt)), self.fixture() as (root, client):
                before = fixtures.SavedBookPublicationTests().artifacts(root)
                source = root / 'uploads/source.txt'
                companion = Path(get_publication_source_path(source)); original = companion.read_bytes()
                companion.write_bytes(corrupt)
                self.assertEqual(409, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
                self.assertEqual(before, fixtures.SavedBookPublicationTests().artifacts(root))
                companion.write_bytes(original)
                self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
                Path(get_publication_source_path(root / 'scripts/book.json')).write_bytes(corrupt)
                state = (root / 'state.json').read_bytes(); active = (root / 'annotated_script.json').read_bytes()
                self.assertEqual(409, client.post('/api/scripts/load', json={'name': 'book'}).status_code)
                self.assertEqual(state, (root / 'state.json').read_bytes())
                self.assertEqual(active, (root / 'annotated_script.json').read_bytes())

    def test_provenance_publication_failure_rolls_back_entire_saved_family(self):
        with self.fixture() as (root, client):
            before = fixtures.SavedBookPublicationTests().artifacts(root)
            replace = os.replace; failed = False
            def refuse(source, destination, *args, **kwargs):
                nonlocal failed
                if not failed and str(destination) == get_publication_source_path(root / 'scripts/book.json'):
                    failed = True
                    raise OSError('fixture source provenance publication failure')
                return replace(source, destination, *args, **kwargs)
            with patch('os.replace', side_effect=refuse):
                self.assertEqual(500, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
            self.assertTrue(failed)
            self.assertEqual(before, fixtures.SavedBookPublicationTests().artifacts(root))

    def test_speaker_repair_keeps_provenance_but_marks_its_script_binding_stale(self):
        with self.fixture() as (root, client):
            self.assertEqual(200, client.post('/api/scripts/save', json={'name': 'book'}).status_code)
            saved = root / 'scripts/book.json'; provenance = Path(get_publication_source_path(saved))
            before = provenance.read_bytes()
            response = client.post('/api/scripts/book/repair/speakers/apply', json={
                'expected_sha256': hashlib.sha256(saved.read_bytes()).hexdigest(),
                'selections': [{'entry_number': 1, 'expected_speaker': 'Alice', 'new_speaker': 'Bob'}]})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual(before, provenance.read_bytes())
            self.assertEqual('stale', get_publication_source_capture(saved)['binding_status'])

    def test_reader_rejects_forged_identity_invalid_shapes_and_cover_hashes(self):
        with self.fixture() as (root, _):
            original = json.loads(Path(get_publication_source_path(root / 'uploads/source.txt')).read_bytes())
            invalid = []
            doc = copy.deepcopy(original); doc['candidates'][0]['id'] = '0' * 64; invalid.append(doc)
            doc = copy.deepcopy(original); doc['binding']['sha256'] = '0' * 64; invalid.append(doc)
            doc = copy.deepcopy(original); doc['candidates'][0]['fields']['title'] = ['x'] * 33; invalid.append(doc)
            doc = copy.deepcopy(original); doc['candidates'][0]['cover_status'] = 'available'; invalid.append(doc)
            doc = copy.deepcopy(original); doc['candidates'] *= 2; invalid.append(doc)
            cover_doc = copy.deepcopy(original)
            cover_doc['candidates'] = [get_epub_publication_candidate(epub(cover=image_bytes('PNG')),
                filename='cover.epub', text_sha256=hashlib.sha256(TEXT).hexdigest())]
            self.assertEqual('available', get_publication_source_document(
                json.dumps(cover_doc).encode())['candidates'][0]['cover_status'])
            doc = copy.deepcopy(cover_doc); doc['candidates'][0]['cover']['sha256'] = '0' * 64; invalid.append(doc)
            doc = copy.deepcopy(cover_doc); doc['candidates'][0]['cover']['data_base64'] = 'invalid'; invalid.append(doc)
            for doc in invalid:
                with self.subTest(document=invalid.index(doc)), self.assertRaises(ValueError):
                    get_publication_source_document(json.dumps(doc).encode())
