"""Re-selecting the source a loaded saved book came from keeps its rendered work.

Issue #2349 (part 3): a loaded saved script names its own library copy as the
active source, so re-uploading the EPUB (whose extracted text *is* the text that
saved book was generated from) was classified as a foreign book. The selection
retired ``chunks.json`` and set ``script_selection_pending``, so every rendered
line read back as pending even though the audio files were still on disk.
"""
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import core
from book_state_transaction import get_book_operation_token
from project import ProjectManager
from routers import editor, script, scripts_library
from script_selection_binding import BINDING_NAME, make_script_selection_binding

TEXT = 'Synthetic line one about a garden.\nSynthetic line two about a gate.'
OTHER_TEXT = 'A different synthetic book about a harbour.'


def epub(text=TEXT, title='Synthetic Book'):
    container = ('<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
                 '<rootfile full-path="book.opf"/></container>')
    package = ('<package xmlns="http://www.idpf.org/2007/opf" '
               'xmlns:dc="http://purl.org/dc/elements/1.1/">'
               '<metadata><dc:title>' + title + '</dc:title></metadata>'
               '<manifest><item id="body" href="body.xhtml" '
               'media-type="application/xhtml+xml"/></manifest>'
               '<spine><itemref idref="body"/></spine></package>')
    body = '<html><body>' + ''.join(
        '<p>%s</p>' % line for line in text.splitlines()) + '</body></html>'
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, 'w') as archive:
        archive.writestr(zipfile.ZipInfo('META-INF/container.xml'), container)
        archive.writestr(zipfile.ZipInfo('book.opf'), package)
        archive.writestr(zipfile.ZipInfo('body.xhtml'), body)
    return raw.getvalue()


class SavedBookSourceReselectionTests(unittest.TestCase):
    LINES = 7

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.uploads = self.root / 'uploads'
        self.uploads.mkdir()
        self.library = self.root / 'scripts'
        self.library.mkdir()
        self.entries = [{'speaker': 'NARRATOR',
                         'text': 'Synthetic line number %d.' % number,
                         'instruct': 'Plain.'} for number in range(self.LINES)]
        self.script_bytes = json.dumps(self.entries).encode('utf-8')
        stack = ExitStack()
        self.addCleanup(stack.close)
        stack.enter_context(patch.object(core, 'DATA_DIR', str(self.root)))
        stack.enter_context(patch.object(core, 'SCRIPTS_DIR', str(self.library)))
        stack.enter_context(patch.object(script, 'DATA_DIR', str(self.root)))
        stack.enter_context(patch.object(script, 'UPLOADS_DIR', str(self.uploads)))
        stack.enter_context(patch.object(script, 'SCRIPTS_DIR', str(self.library)))
        stack.enter_context(patch.object(scripts_library, 'DATA_DIR', str(self.root)))
        stack.enter_context(patch.object(editor, 'DATA_DIR', str(self.root)))
        stack.enter_context(patch.object(editor, 'SCRIPT_PATH',
                                         str(self.root / 'annotated_script.json')))
        stack.enter_context(patch.object(editor, 'project_manager',
                                         ProjectManager(str(self.root))))
        for name, value in (('SCRIPTS_DIR', str(self.library)),
                            ('SCRIPT_PATH', str(self.root / 'annotated_script.json')),
                            ('VOICE_CONFIG_PATH', str(self.root / 'voice_config.json')),
                            ('CHUNKS_PATH', str(self.root / 'chunks.json')),
                            ('AUDIOBOOK_PATH', str(self.root / 'audiobook.mp3')),
                            ('M4B_PATH', str(self.root / 'audiobook.m4b')),
                            ('CHAPTER_EXPORT_DIR', str(self.root / 'chapter_export'))):
            stack.enter_context(patch.object(scripts_library, name, value))
        application = FastAPI()
        for router in (script.router, scripts_library.router, editor.router):
            application.include_router(router)
        self.client = TestClient(application)

    def upload(self, text=TEXT, *, name='book.epub'):
        if name.endswith('.epub'):
            files = {'file': (name, epub(text), 'application/epub+zip')}
        else:
            files = {'file': (name, text.encode('utf-8'), 'text/plain')}
        return self.client.post('/api/upload', params={'select_active': 'true'},
                                files=files)

    def state(self):
        return json.loads((self.root / 'state.json').read_text())

    def chunk_status(self):
        response = self.client.get('/api/chunks/status')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(self.LINES, response.json()['total'])
        return response.json()

    def rendered_lines(self):
        return sum(1 for row in self.chunk_status()['chunks']
                   if row['status'] == 'done')

    def write_rendered_chunks(self):
        (self.root / 'voicelines').mkdir(exist_ok=True)
        rows = []
        for number in range(self.LINES):
            uid = 'synthetic-%04d' % number
            (self.root / 'voicelines' / (uid + '.wav')).write_bytes(b'RIFF')
            rows.append({'id': number, 'uid': uid, 'text': self.entries[number]['text'],
                         'speaker': 'NARRATOR', 'status': 'done',
                         'audio_path': 'voicelines/' + uid + '.wav'})
        (self.root / 'chunks.json').write_text(json.dumps(rows, indent=2))

    def write_binding(self, path, digest, source_path):
        binding = make_script_selection_binding(
            get_book_operation_token(str(self.root)), digest,
            self.script_bytes, source_path=str(source_path))
        Path(path).write_text(json.dumps(binding, sort_keys=True, separators=(',', ':')))

    def seed_saved_rendered_book(self):
        """Upload -> publish -> save -> load, then render (real routes).

        Only the completed generation publication is seeded directly; save,
        load, chunk status and selection all run through their routes.
        """
        response = self.upload()
        self.assertEqual(200, response.status_code, response.text)
        source = self.uploads / 'book.txt'
        (self.root / 'annotated_script.json').write_bytes(self.script_bytes)
        self.write_binding(self.root / BINDING_NAME,
                           hashlib.sha256(source.read_bytes()).hexdigest(), source)
        state = self.state()
        state.pop('script_selection_pending', None)
        (self.root / 'state.json').write_text(json.dumps(state))
        response = self.client.post('/api/scripts/save', json={'name': 'Synthetic Saved'})
        self.assertEqual(200, response.status_code, response.text)
        response = self.client.post('/api/scripts/load', json={'name': 'Synthetic Saved'})
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual(str(self.library / 'Synthetic Saved.json'),
                         self.state()['input_file_path'])
        self.write_rendered_chunks()
        self.assertEqual(self.LINES, self.rendered_lines())
        return source

    def stale_chunks(self):
        return sorted(path.name for path in self.root.iterdir()
                      if path.name.startswith('chunks.json.stale-'))

    def test_identical_epub_reupload_keeps_the_rendered_book(self):
        source = self.seed_saved_rendered_book()
        before = self.state()
        response = self.upload()
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()['reused'])
        after = self.state()
        self.assertNotIn('script_selection_pending', after)
        self.assertEqual(before['active_book_id'], after['active_book_id'])
        self.assertEqual(str(source), after['input_file_path'])
        self.assertEqual(self.script_bytes,
                         (self.root / 'annotated_script.json').read_bytes())
        self.assertEqual(self.LINES, self.rendered_lines())
        self.assertEqual([], self.stale_chunks())

    def test_different_epub_starts_a_new_book_and_retains_the_old_rows(self):
        self.seed_saved_rendered_book()
        response = self.upload(OTHER_TEXT, name='other.epub')
        self.assertEqual(200, response.status_code, response.text)
        state = self.state()
        self.assertEqual('other', state['active_book_id'])
        self.assertTrue(state['script_selection_pending'])
        self.assertEqual(1, len(self.stale_chunks()))
        self.assertEqual(self.LINES, len(list((self.root / 'voicelines').iterdir())))

    def test_inconsistent_saved_binding_is_not_trusted(self):
        self.seed_saved_rendered_book()
        (self.library / 'Synthetic Saved.json').write_bytes(json.dumps(
            [{'speaker': 'NARRATOR', 'text': 'A rewritten saved script.'}]).encode('utf-8'))
        response = self.upload()
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(self.state()['script_selection_pending'])
        self.assertEqual(1, len(self.stale_chunks()))

    def test_origin_binding_outside_the_library_directory_is_not_trusted(self):
        from book_state_transaction import apply_book_input_selection
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        carried = elsewhere / 'Carried.json'
        carried.write_bytes(self.script_bytes)
        other = self.uploads / 'other.txt'
        other.write_text(OTHER_TEXT, encoding='utf-8')
        (self.root / 'annotated_script.json').write_bytes(self.script_bytes)
        (self.root / 'state.json').write_text(json.dumps({
            'active_book_id': 'carried', 'book_generation': 'generation-a',
            'input_file_path': str(carried)}))
        self.write_binding(self.root / BINDING_NAME,
                           hashlib.sha256(carried.read_bytes()).hexdigest(), carried)
        (self.library / 'Carried.json').write_bytes(self.script_bytes)
        self.write_binding(self.library / 'Carried.selection_binding.json',
                           hashlib.sha256(other.read_bytes()).hexdigest(), other)
        self.write_rendered_chunks()
        apply_book_input_selection(str(self.root), str(other), 'other',
                                   saved_books_root=str(self.library))
        self.assertTrue(self.state()['script_selection_pending'])
        self.assertEqual(1, len(self.stale_chunks()))

    def test_sibling_binding_outside_the_library_is_not_trusted(self):
        response = self.upload()
        self.assertEqual(200, response.status_code, response.text)
        source = self.uploads / 'book.txt'
        other = self.uploads / 'other.txt'
        other.write_text(OTHER_TEXT, encoding='utf-8')
        (self.root / 'annotated_script.json').write_bytes(self.script_bytes)
        self.write_binding(self.root / BINDING_NAME,
                           hashlib.sha256(source.read_bytes()).hexdigest(), source)
        self.write_binding(self.uploads / 'book.selection_binding.json',
                           hashlib.sha256(other.read_bytes()).hexdigest(), other)
        state = self.state()
        state.pop('script_selection_pending', None)
        (self.root / 'state.json').write_text(json.dumps(state))
        self.write_rendered_chunks()
        self.assertEqual(self.LINES, self.rendered_lines())
        response = self.upload(OTHER_TEXT, name='other.txt')
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(self.state()['script_selection_pending'])
        self.assertEqual(1, len(self.stale_chunks()))


if __name__ == '__main__':
    unittest.main()
