"""A selection that keeps the published script keeps its same-book companions.

#2312: ``apply_book_input_selection`` put ``.sound_design_cues.json`` and
``.sound_design_renditions.json`` in the transaction's removals regardless of
whether the selection rebound the published script for the same book, so
re-selecting the active upload kept the script and its binding but destroyed
the cue history. Companions are kept only for a same-book rebind; identical
bytes under another filename change the book id and still clear them.
"""
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import httpx

import book_state_transaction as books
import core
from project_scope_admission import ensure_scope_admission
from routers import script
from script_selection_binding import (BINDING_NAME, capture_generation_selection,
                                      make_script_selection_binding,
                                      publish_generated_script)
import sound_design_current as current
from sound_design_context import (capture_sound_design_anchor, capture_sound_design_context,
                                  require_sound_design_context_files_current)
from sound_design_cues import get_sound_design_cue_plan, get_sound_design_cue_version
from sound_design_registry import (ACTIVE_CUES, get_selected_sound_design_versions,
                                   get_sound_design_registry_bytes)
from sound_design_render import save_sound_design_candidate
from sound_design_renditions import ACTIVE_RENDITIONS, get_sound_rendition_ledger_bytes
from tests import test_sound_design_capture as capture_fixtures
from tests.test_sound_design_cues import pcm_plan, recipe
from tests.test_sound_design_registry import registry
from tests.test_support import BookProtocolCases


SOURCE = b'A synthetic source used only for regression fixtures.\n'
SCRIPT = b'[{"speaker":"N","text":"She wrote on 1/2/1900."}]'
CHUNKS = [{'id': 0, 'uid': 'line-0', 'text': 'She wrote on 1/2/1900.',
           'speaker': 'N', 'status': 'done', 'audio_path': 'line-0.wav'}]


def capture_book(root):
    """Capture the real current book and its dialogue selection."""
    with patch.object(core, 'DATA_DIR', str(root)), \
            patch.object(core, 'process_state', {}), \
            ensure_scope_admission(root):
        context = capture_sound_design_context(str(root))
        anchor = capture_sound_design_anchor(str(root), context, 'line-0')
    return context, anchor


def cue_projection(context, anchor):
    versions = get_selected_sound_design_versions(context['registry'])
    return get_sound_design_cue_plan(versions, context['book'],
                                     [anchor['selection']], pcm_plan())


def published_book(root):
    """A verified published book with a real cue for its own dialogue row."""
    root = Path(root)
    uploads = root / 'uploads'; uploads.mkdir()
    source = uploads / 'source.txt'; source.write_bytes(SOURCE)
    (root / 'annotated_script.json').write_bytes(SCRIPT)
    (root / 'voice_config.json').write_bytes(b'{}')
    (root / 'config.json').write_bytes(b'{}')
    state = {'active_book_id': 'source', 'book_generation': 'generation-a',
             'input_file_path': str(source)}
    (root / 'state.json').write_text(json.dumps(state))
    selected = capture_generation_selection(str(root), str(source))
    state.update(script_generation_run_id='f' * 32,
                 script_generation_base_sha256=hashlib.sha256(SCRIPT).hexdigest())
    (root / 'state.json').write_text(json.dumps(state))
    publish_generated_script(str(root), selected['book_token'], str(source),
                             selected['source_sha256'], SCRIPT, expected_run_id='f' * 32)
    (root / 'chunks.json').write_text(json.dumps(CHUNKS, indent=2))
    (root / 'line-0.wav').write_bytes(b'RIFF' + b'\0' * 64)
    context, anchor = capture_book(root)
    authored = recipe()
    authored['book'] = dict(context['book'])
    authored['selection'] = dict(anchor['selection'])
    version = get_sound_design_cue_version('cue-a', authored)
    cues = get_sound_design_registry_bytes(dict(
        schema=1, book=dict(context['book']), versions=[version],
        selected={'cue-a': version['version']}))
    ledger = get_sound_rendition_ledger_bytes(dict(
        schema=1, book_id=context['book']['book_id'], records=[], keep=[]))
    (root / ACTIVE_CUES).write_bytes(cues)
    (root / ACTIVE_RENDITIONS).write_bytes(ledger)
    return root, source, cues, ledger


def selection_files(root):
    root = Path(root)
    return {name: (root / name).read_bytes() for name in
            ('state.json', ACTIVE_CUES, ACTIVE_RENDITIONS, 'script_selection_binding.json')
            if (root / name).exists()}


class CompanionSelectionTests(BookProtocolCases, unittest.TestCase):
    def test_reselecting_the_active_upload_keeps_valid_companions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, cues, ledger = published_book(tmp)
            before = cue_projection(*capture_book(root))
            self.assertTrue(before['can_project'])
            self.assertEqual(['current'], [cue['status'] for cue in before['cues']])
            books.apply_book_input_selection(str(root), str(source), 'source')
            self.assertEqual(cues, (root / ACTIVE_CUES).read_bytes())
            self.assertEqual(ledger, (root / ACTIVE_RENDITIONS).read_bytes())
            self.assertNotIn('script_selection_pending',
                             json.loads((root / 'state.json').read_text()))
            # The kept cue must still project through the real current path,
            # not merely stay parseable.
            after = cue_projection(*capture_book(root))
            self.assertTrue(after['can_project'])
            self.assertEqual(before['cues'], after['cues'])

    def test_identical_bytes_under_another_filename_clear_the_companions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, cues, ledger = published_book(tmp)
            renamed = root / 'uploads' / 'renamed.txt'; renamed.write_bytes(SOURCE)
            books.apply_book_input_selection(str(root), str(renamed), 'renamed')
            # The script is still rebound, but the book id changed; the kept
            # registry would be refused as another book's (#2312 evidence).
            self.assertNotIn('script_selection_pending',
                             json.loads((root / 'state.json').read_text()))
            self.assertFalse((root / ACTIVE_CUES).exists())
            self.assertFalse((root / ACTIVE_RENDITIONS).exists())
            (root / ACTIVE_CUES).write_bytes(cues)
            (root / ACTIVE_RENDITIONS).write_bytes(ledger)
            with patch.object(core, 'DATA_DIR', str(root)), \
                    patch.object(core, 'process_state', {}), \
                    ensure_scope_admission(root):
                with self.assertRaisesRegex(ValueError, 'belongs to another book'):
                    capture_sound_design_context(str(root))

    def test_changed_bytes_clear_the_companions_for_the_new_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, source, _cues, _ledger = published_book(tmp)
            source.write_bytes(b'Different synthetic source bytes.\n')
            books.apply_book_input_selection(str(root), str(source), 'source')
            self.assertTrue(json.loads(
                (root / 'state.json').read_text())['script_selection_pending'])
            self.assertFalse((root / ACTIVE_CUES).exists())
            self.assertFalse((root / ACTIVE_RENDITIONS).exists())

    def test_malformed_or_foreign_companions_refuse_both_branches_unchanged(self):
        for branch in ('same_source', 'changed_bytes'):
            for kind in ('malformed', 'foreign'):
                with self.subTest(branch=branch, kind=kind), \
                        tempfile.TemporaryDirectory() as tmp:
                    root, source, cues, ledger = published_book(tmp)
                    if kind == 'malformed':
                        cues = b'{invalid'
                        ledger = b'{invalid'
                    else:
                        cues = get_sound_design_registry_bytes(registry('other-book'))
                        ledger = get_sound_rendition_ledger_bytes(dict(
                            schema=1, book_id='other-book', records=[], keep=[]))
                    (root / ACTIVE_CUES).write_bytes(cues)
                    (root / ACTIVE_RENDITIONS).write_bytes(ledger)
                    if branch == 'changed_bytes':
                        source.write_bytes(b'Different synthetic source bytes.\n')
                    before = selection_files(root)
                    with self.assertRaises(ValueError):
                        books.apply_book_input_selection(str(root), str(source), 'source')
                    self.assertEqual(before, selection_files(root))
                    self.assertFalse((root / books.JOURNAL).exists())

    def test_interrupted_reselection_replays_with_companions_kept(self):
        worker = '''
import os,sys
import book_state_transaction as books
import source_upload_publication as publication
root,protocol=sys.argv[1],sys.argv[2]
if protocol=='v3':
 publication.probe_exchange_support=lambda _fd:False
books._finish_transaction=lambda *args,**kwargs: os._exit(77)
books.apply_book_input_selection(root,root+'/uploads/source.txt','source')
'''
        with tempfile.TemporaryDirectory() as tmp, \
                tempfile.TemporaryDirectory() as live_tmp:
            root, source, cues, ledger = published_book(tmp)
            result = subprocess.run(
                [sys.executable, '-c', worker, str(root), self._book_protocol],
                capture_output=True, text=True, timeout=15)
            self.assertEqual(77, result.returncode, result.stderr)
            journal = json.loads((root / books.JOURNAL).read_bytes())
            self.assertEqual('committed', journal['phase'])
            with books.ensure_book_state(str(root)):
                pass
            self.assertFalse((root / books.JOURNAL).exists())
            self.assertEqual(cues, (root / ACTIVE_CUES).read_bytes())
            self.assertEqual(ledger, (root / ACTIVE_RENDITIONS).read_bytes())
            replayed = json.loads((root / 'state.json').read_text())
            live_root, live_source, _live_cues, _live_ledger = published_book(live_tmp)
            books.apply_book_input_selection(str(live_root), str(live_source), 'source')
            direct = json.loads((live_root / 'state.json').read_text())
            allowed = {'book_generation', 'input_file_path'}
            self.assertEqual(set(direct), set(replayed),
                             'replay wrote a different set of state keys')
            self.assertEqual(allowed,
                             {key for key in replayed if replayed[key] != direct[key]})
            self.assertNotIn('script_selection_pending', replayed)


class GenerationBoundCandidateTests(unittest.TestCase):
    """A kept registry does not keep a candidate bound to the old generation."""

    def setUp(self):
        capture_fixtures.SoundCaptureCompositionTests.setUp(self)
        admission = patch.object(current, 'ensure_audio_scope_admission',
                                 side_effect=lambda root: self.fixture.admitted())
        admission.start()
        self.addCleanup(admission.stop)

    def revision(self):
        with self.fixture.admitted():
            return capture_sound_design_context(self.root)['context_revision']

    def test_reselection_keeps_companions_and_refuses_the_old_candidate(self):
        selected = capture_generation_selection(str(self.root), str(self.fixture.source))
        binding = make_script_selection_binding(
            selected['book_token'], selected['source_sha256'],
            (self.root / 'annotated_script.json').read_bytes(),
            source_path=str(self.fixture.source))
        (self.root / BINDING_NAME).write_bytes(json.dumps(binding).encode('utf-8'))
        with self.fixture.admitted():
            book = capture_sound_design_context(self.root)['book']
        cues = get_sound_design_registry_bytes(dict(
            schema=1, book=dict(book), versions=[], selected={}))
        ledger = get_sound_rendition_ledger_bytes(dict(
            schema=1, book_id=book['book_id'], records=[], keep=[]))
        (self.root / ACTIVE_CUES).write_bytes(cues)
        (self.root / ACTIVE_RENDITIONS).write_bytes(ledger)
        with self.fixture.admitted():
            before = capture_sound_design_context(self.root)
        ranked = save_sound_design_candidate(self.root, self.revision())
        stage = (self.root / ranked['directory']).name

        books.apply_book_input_selection(
            str(self.root), str(self.fixture.source), self.fixture.state['active_book_id'])

        self.assertEqual(cues, (self.root / ACTIVE_CUES).read_bytes())
        self.assertEqual(ledger, (self.root / ACTIVE_RENDITIONS).read_bytes())
        with self.fixture.admitted():
            after = capture_sound_design_context(self.root)
            # The book identity survives, so the kept companions stay usable.
            self.assertEqual(before['book'], after['book'])
            self.assertEqual(before['registry_revision'], after['registry_revision'])
            self.assertEqual(before['ledger_revision'], after['ledger_revision'])
            # The generation changed, so a context/prepared candidate bound to
            # the old one must be refused rather than silently re-used.
            self.assertNotEqual(before['context_revision'], after['context_revision'])
            self.assertNotEqual(before['book_token'], after['book_token'])
            with self.assertRaisesRegex(ValueError, 'changed before publication'):
                require_sound_design_context_files_current(self.root, before)
        with self.assertRaisesRegex(ValueError, 'stale'):
            with current.ensure_current_sound_candidate(
                    self.root, stage, ranked['candidate_revision']):
                self.fail('A candidate rendered before the selection was admitted')


class CompanionSelectionRouteTests(unittest.IsolatedAsyncioTestCase):
    async def test_select_route_keeps_the_active_uploads_companions(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _source, cues, ledger = published_book(tmp)
            uploads = root / 'uploads'
            app = FastAPI(); app.include_router(script.router)
            with patch.object(script, 'DATA_DIR', str(root)), \
                    patch.object(script, 'UPLOADS_DIR', str(uploads)), \
                    patch.object(core, 'DATA_DIR', str(root)), \
                    patch.object(core, 'process_state', {'script': {'running': False}}), \
                    patch.object(core, '_task_claims', {}), \
                    patch.object(core, '_gpu_leases', {}):
                transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
                async with httpx.AsyncClient(transport=transport,
                                             base_url='http://fixture') as client:
                    response = await client.post('/api/uploads/select',
                                                 json={'filename': 'source.txt'})
            self.assertEqual(200, response.status_code, response.text)
            self.assertEqual('selected', response.json()['status'])
            self.assertEqual(cues, (root / ACTIVE_CUES).read_bytes())
            self.assertEqual(ledger, (root / ACTIVE_RENDITIONS).read_bytes())


if __name__ == '__main__':
    unittest.main()
