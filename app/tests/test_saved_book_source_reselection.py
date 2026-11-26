"""Re-selecting the source a saved book came from keeps its rendered work.

Issue #2349 (part 3): a loaded saved script names its own library copy as the
active source, so re-uploading the EPUB (whose extracted text *is* the text that
saved book was generated from) was classified as a foreign book. The selection
retired ``chunks.json`` and set ``script_selection_pending``, so every rendered
line read back as pending even though the audio files were still on disk.

``script_selection_binding.resolve_saved_book_origin`` is the one place that
follows a saved binding chain. The reporter-flow tests drive the real upload,
save, load, render and chunk-status routes; they seed the completed generation
publication directly and stub only the speech engine. The other tests seed the
published script, its selection binding and the rendered chunk rows directly,
then exercise the save, load and selection paths.
"""
import copy
import hashlib
import io
import json
import tempfile
import unittest
import zipfile
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import soundfile as sf
from fastapi import FastAPI
from fastapi.testclient import TestClient

import core
from book_state_transaction import apply_book_input_selection, get_book_operation_token
from project import ProjectManager
from routers import editor, script, scripts_library
from script_selection_binding import (BINDING_NAME, _path_digest,
                                      capture_generation_selection,
                                      make_script_selection_binding,
                                      publish_generated_script)
from tests.test_support import use_private_cpu_fixture_lock

TEXT = ('Synthetic line one about a garden.\n'
        'Synthetic line two about a gate.')
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


class SelectionFixture(unittest.TestCase):
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

    def stale_chunks(self):
        return sorted(path.name for path in self.root.iterdir()
                      if path.name.startswith('chunks.json.stale-'))


class SavedBookSourceReselectionTests(SelectionFixture):
    def seed_saved_rendered_book(self):
        """Upload, save and load a rendered saved book through their routes.

        The upload runs through its route; the published script, its selection
        binding and the rendered chunk rows are written directly. Save, load,
        chunk status and selection run through their routes.
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
        self.assertTrue(state.get('script_selection_pending'))
        self.assertEqual(1, len(self.stale_chunks()))
        self.assertEqual(self.LINES, len(list((self.root / 'voicelines').iterdir())))

    def test_deleted_origin_with_identical_incoming_bytes_keeps_the_rendered_book(self):
        """The recorded origin digest is enough when the origin file is gone."""
        source = self.seed_saved_rendered_book()
        renamed = self.uploads / 'same.txt'
        renamed.write_bytes(source.read_bytes())
        source.unlink()
        apply_book_input_selection(str(self.root), str(renamed), 'book',
                                   saved_books_root=str(self.library))
        state = self.state()
        self.assertNotIn('script_selection_pending', state)
        self.assertEqual('book', state['active_book_id'])
        self.assertEqual(str(renamed), state['input_file_path'])
        self.assertEqual(self.script_bytes,
                         (self.root / 'annotated_script.json').read_bytes())
        self.assertEqual(self.LINES, self.rendered_lines())
        self.assertEqual([], self.stale_chunks())

    def test_changed_origin_bytes_still_start_a_new_book(self):
        """An origin file that still exists with different bytes is refused."""
        source = self.seed_saved_rendered_book()
        source.write_text('A rewritten source text.', encoding='utf-8')
        with self.assertLogs('book_state_transaction', level='WARNING') as logs:
            apply_book_input_selection(str(self.root), str(source), 'book',
                                       saved_books_root=str(self.library))
        self.assertTrue(self.state().get('script_selection_pending'))
        self.assertEqual(1, len(self.stale_chunks()))
        self.assertTrue(any('changed_source' in line for line in logs.output), logs.output)

    def test_changed_saved_script_bytes_start_a_new_book(self):
        """The resolver's expected digest is the bytes the book was loaded from."""
        self.seed_saved_rendered_book()
        (self.library / 'Synthetic Saved.json').write_bytes(json.dumps(
            [{'speaker': 'NARRATOR', 'text': 'A rewritten saved script.'}]).encode('utf-8'))
        with self.assertLogs('book_state_transaction', level='WARNING') as logs:
            response = self.upload()
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(self.state()['script_selection_pending'])
        self.assertEqual(1, len(self.stale_chunks()))
        self.assertTrue(any('changed_source' in line for line in logs.output), logs.output)

    def test_cycle_in_the_saved_binding_chain_starts_a_new_book(self):
        self.seed_saved_rendered_book()
        saved = self.library / 'Synthetic Saved.json'
        # A self-referential but otherwise consistent saved binding is a loop.
        self.write_binding(self.library / 'Synthetic Saved.selection_binding.json',
                           hashlib.sha256(saved.read_bytes()).hexdigest(), saved)
        with self.assertLogs('book_state_transaction', level='WARNING') as logs:
            response = self.upload()
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(self.state()['script_selection_pending'])
        self.assertEqual(1, len(self.stale_chunks()))
        self.assertTrue(any('cycle' in line for line in logs.output), logs.output)

    def test_origin_binding_outside_the_library_directory_is_not_trusted(self):
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

    def test_saved_binding_pointing_outside_the_library_logs_at_warning(self):
        """A saved book whose binding leaves the library is security-relevant."""
        self.seed_saved_rendered_book()
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        target = elsewhere / 'Carried.json'
        target.write_bytes(self.script_bytes)
        self.write_binding(self.library / 'Synthetic Saved.selection_binding.json',
                           hashlib.sha256(target.read_bytes()).hexdigest(), target)
        incoming = self.uploads / 'other.txt'
        incoming.write_text(OTHER_TEXT, encoding='utf-8')
        with self.assertLogs('book_state_transaction', level='WARNING') as logs:
            apply_book_input_selection(str(self.root), str(incoming), 'other',
                                       saved_books_root=str(self.library))
        self.assertTrue(self.state().get('script_selection_pending'))
        self.assertTrue(any('outside_library' in line for line in logs.output), logs.output)

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


class ReporterFlowTests(SelectionFixture):
    """The reporter's route, end to end, with only the speech engine stubbed."""

    def setUp(self):
        super().setUp()
        # Real rendering chunks the published script; one long line per chunk.
        self.entries = [{'speaker': 'NARRATOR',
                         'text': ('Synthetic line number %d, ' % number)
                                 + 'a quiet garden gate beside the old stone wall. ' * 6,
                         'instruct': 'Plain.'} for number in range(self.LINES)]
        self.script_bytes = json.dumps(self.entries).encode('utf-8')
        stack = ExitStack()
        self.addCleanup(stack.close)
        process_state = copy.deepcopy(core.process_state)
        for value in process_state.values():
            value['running'] = False
        stack.enter_context(patch.object(core, 'process_state', process_state))
        stack.enter_context(patch.object(editor, 'process_state', process_state))
        stack.enter_context(patch.object(core, '_task_claims', {}))
        stack.enter_context(patch.object(core, '_gpu_leases', {}))
        stack.enter_context(use_private_cpu_fixture_lock())
        # Force the WAV fallback so no encoder is involved in the stub.
        stack.enter_context(patch('project._export_audio_segment',
                                  side_effect=RuntimeError('force wav fallback')))
        self.manager = editor.project_manager
        self.manager.engine = SimpleNamespace(generate_voice=self._synthesize)
        self.addCleanup(self._release_claims)

    def _release_claims(self):
        for name, claim in list(core._task_claims.items()):
            core.release_gpu_task_claim(name, claim['id'])

    def _synthesize(self, text, instruct, speaker, voices, path):
        # A duration proportional to the line so the speech-length gate passes.
        frames = max(24000 * 4, 1200 * len(text))
        sf.write(path, np.full(frames, 0.15, dtype='float32'), 24000)
        return True

    def publish_book(self):
        """Upload the EPUB through its route, then seed its publication directly."""
        response = self.upload()
        self.assertEqual(200, response.status_code, response.text)
        source = self.uploads / 'book.txt'
        selection = capture_generation_selection(str(self.root), str(source))
        state = self.state()
        self.assertNotIn('script_generation_base_sha256', state)
        script_path = self.root / 'annotated_script.json'
        state['script_generation_run_id'] = 'f' * 32
        state['script_generation_base_sha256'] = (
            hashlib.sha256(script_path.read_bytes()).hexdigest()
            if script_path.exists() else None)
        (self.root / 'state.json').write_text(json.dumps(state))
        publish_generated_script(
            str(self.root), selection['book_token'], str(source),
            selection['source_sha256'], self.script_bytes, expected_run_id='f' * 32)
        return source

    def save_and_load(self, name):
        response = self.client.post('/api/scripts/save', json={'name': name})
        self.assertEqual(200, response.status_code, response.text)
        response = self.client.post('/api/scripts/load', json={'name': name})
        self.assertEqual(200, response.status_code, response.text)
        return self.library / (name + '.json')

    def render_chunks(self):
        """Render every chunk through the real Editor route with a stub engine."""
        status = self.chunk_status()
        self.assertEqual(self.LINES, status['total'])
        for index in range(status['total']):
            response = self.client.post('/api/chunks/%d/generate' % index)
            self.assertEqual(200, response.status_code, response.text)
        status = self.chunk_status()
        self.assertEqual(self.LINES, self.rendered_lines())
        return [row['uid'] for row in status['chunks']]

    def assert_rendered_book_kept(self, uids, source):
        state = self.state()
        self.assertNotIn('script_selection_pending', state)
        self.assertEqual('book', state['active_book_id'])
        self.assertEqual(str(source), state['input_file_path'])
        self.assertEqual(self.script_bytes,
                         (self.root / 'annotated_script.json').read_bytes())
        self.assertEqual([], self.stale_chunks())
        status = self.chunk_status()
        self.assertEqual(uids, [row['uid'] for row in status['chunks']])
        self.assertTrue(all(row['status'] == 'done' for row in status['chunks']))
        self.assertEqual(self.LINES, len(list((self.root / 'voicelines').iterdir())))

    def test_reporter_flow_keeps_rendered_rows_after_the_same_epub_is_reuploaded(self):
        source = self.publish_book()
        self.save_and_load('Synthetic Saved')
        uids = self.render_chunks()
        response = self.upload()
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()['reused'])
        self.assert_rendered_book_kept(uids, source)

    def test_two_hop_saved_chain_keeps_rendered_rows(self):
        """Saved B, loaded while A was active, still resolves to A's origin text."""
        source = self.publish_book()
        first = self.save_and_load('Synthetic A')
        second = self.save_and_load('Synthetic B')
        self.assertEqual(str(second), self.state()['input_file_path'])
        binding = json.loads(
            (self.library / 'Synthetic B.selection_binding.json').read_text())
        self.assertEqual(_path_digest(str(first)), binding['source_path_sha256'])
        uids = self.render_chunks()
        response = self.upload()
        self.assertEqual(200, response.status_code, response.text)
        self.assertTrue(response.json()['reused'])
        self.assert_rendered_book_kept(uids, source)


if __name__ == '__main__':
    unittest.main()
