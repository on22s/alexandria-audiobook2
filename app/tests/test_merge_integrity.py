import contextlib
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import editor
from tests.test_chapter_export import _project


class MergeIntegrityAdmission(unittest.TestCase):
    WN_FRONT = (
        'Manifesto.\n'
        'Translator note for synthetic test.\n'
        'Original Web Novel Chapter - Complete.\n'
        'Original Translation by Synthetic.\n\n'
    )
    WN_STORY = 'The morning mist cleared over the quiet valley.\n'

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.project, rows = _project(self.temp.name)
        self.rows = [dict(rows[0], text='alpha beta')]
        self.project.save_chunks(self.rows)
        self.rows = self.project.load_chunks()
        self.source = self.root / 'source.txt'
        self.source.write_text('alpha beta')
        self.script = self.root / 'annotated_script.json'
        self.script.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
        self.state = {'active_book_id': 'fixture', 'book_generation': 'generation-a',
                      'input_file_path': str(self.source)}
        self.write_state()
        self.scheduled = []
        self.tasks = {'audio': {'cancel': False, 'running': False, 'logs': []}}
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for name, value in {'DATA_DIR': self.temp.name, 'SCRIPT_PATH': str(self.script),
                            'project_manager': self.project, 'process_state': self.tasks}.items():
            stack.enter_context(patch.object(editor, name, value))
        stack.enter_context(patch.object(editor, 'schedule_claimed_background_task',
                                        side_effect=lambda *args: self.scheduled.append(args)))
        app = FastAPI()
        app.include_router(editor.router)
        self.client = stack.enter_context(TestClient(app))

    def write_state(self):
        (self.root / 'state.json').write_text(json.dumps(self.state))

    def change_rows(self, text):
        self.project.save_chunks([dict(self.rows[0], text=text)])

    def write_saved_copy(self, name, source_path, text='alpha beta', *, speaker='A', entries=None, meta=None):
        from book_state_transaction import get_book_operation_token
        from script_selection_binding import make_script_selection_binding
        scripts = self.root / 'scripts'
        scripts.mkdir(exist_ok=True)
        stored = scripts / f'{name}.json'
        payload = entries if entries is not None else [{'speaker': speaker, 'text': text}]
        stored.write_text(json.dumps(payload))
        binding = make_script_selection_binding(
            get_book_operation_token(self.temp.name),
            hashlib.sha256(Path(source_path).read_bytes()).hexdigest(),
            stored.read_bytes(), source_path=str(source_path))
        (scripts / f'{name}.selection_binding.json').write_text(json.dumps(binding))
        if meta is not None:
            (scripts / f'{name}.meta.json').write_text(json.dumps(meta))
        return stored

    def admit_run(self, source_path, options, *, run_id):
        """Record an admitted attempt's receipt without publishing it."""
        import run_spec_receipt
        from book_state_transaction import get_book_operation_token
        receipt = run_spec_receipt.build_receipt(
            book_token=get_book_operation_token(self.temp.name, state=self.state),
            source_path=str(source_path),
            source_sha256=hashlib.sha256(Path(source_path).read_bytes()).hexdigest(),
            prepared_sha256='0' * 64, options=dict(options), settings={},
            fingerprint='0' * 64, chunk_count=1, run_id=run_id)
        Path(run_spec_receipt.receipt_path(self.temp.name)).write_bytes(
            run_spec_receipt.receipt_bytes(receipt))
        self.state.update({
            'script_generation_input_file': str(source_path),
            'script_generation_options': dict(options),
            'script_generation_run_id': run_id,
        })
        self.state[run_spec_receipt.STATE_DIGEST] = receipt['sha256']
        self.write_state()
        return receipt

    def publish_run(self, source_path, options, *, run_id='a' * 32):
        """Record a published generation attempt's receipt for save-time policy."""
        receipt = self.admit_run(source_path, options, run_id=run_id)
        self.state['script_generation_published_run_id'] = run_id
        self.state.pop('script_generation_run_id', None)
        self.write_state()
        return receipt

    def library_client(self):
        """Wire the real save/load routes beside the editor routes."""
        import core
        from routers import scripts_library as library
        (self.root / 'uploads').mkdir(exist_ok=True)
        (self.root / 'scripts').mkdir(exist_ok=True)
        stack = contextlib.ExitStack()
        self.addCleanup(stack.close)
        for module in (core, library):
            for name, value in (
                ('DATA_DIR', str(self.root)),
                ('SCRIPTS_DIR', str(self.root / 'scripts')),
                ('SCRIPT_PATH', str(self.script)),
                ('VOICE_CONFIG_PATH', str(self.root / 'voice_config.json')),
                ('UPLOADS_DIR', str(self.root / 'uploads')),
                ('CHUNKS_PATH', str(self.root / 'chunks.json')),
                ('AUDIOBOOK_PATH', str(self.root / 'book.mp3')),
                ('M4B_PATH', str(self.root / 'book.m4b')),
            ):
                if hasattr(module, name):
                    stack.enter_context(patch.object(module, name, value))
        for module in (core, library):
            stack.enter_context(patch.object(module, 'process_state', self.tasks))
        for name in ('_task_claims', '_gpu_leases', '_saved_book_publications'):
            stack.enter_context(patch.object(core, name, {}))
        app = FastAPI()
        app.include_router(editor.router)
        app.include_router(library.router)
        return stack.enter_context(TestClient(app))

    def load_saved(self, api, name):
        response = api.post('/api/scripts/load', json={'name': name})
        self.assertEqual(200, response.status_code, response.text)

    def use_saved_book(self, stored, *, write_active_binding=False):
        from book_state_transaction import get_book_operation_token
        from script_selection_binding import BINDING_NAME, make_script_selection_binding
        self.state['input_file_path'] = str(stored)
        self.write_state()
        if write_active_binding:
            stored_bytes = Path(stored).read_bytes()
            self.script.write_bytes(stored_bytes)
            active = make_script_selection_binding(
                get_book_operation_token(self.temp.name, state=self.state),
                hashlib.sha256(stored_bytes).hexdigest(),
                stored_bytes, source_path=str(stored))
            (self.root / BINDING_NAME).write_text(json.dumps(active))
            self.state['script_binding_required'] = True
            self.write_state()

    def test_known_word_replacement_refuses_merge_with_locations(self):
        self.change_rows('alpha gamma')
        self.script.write_text(json.dumps([{'text': 'alpha gamma'}]))
        response = self.client.post('/api/merge', json={})
        self.assertEqual(409, response.status_code, response.text)
        detail = response.json()['detail']
        self.assertEqual('differences', detail['status'])
        self.assertEqual(('beta', 'gamma', 0),
                         (detail['hunks'][0]['source_words'], detail['hunks'][0]['script_words'],
                          detail['hunks'][0]['entry_index']))
        self.assertTrue(detail['snapshot'])
        self.assertEqual([], self.scheduled)

    def test_clean_chunks_schedule_as_before_without_body(self):
        response = self.client.post('/api/merge')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('audio', self.scheduled[0][1])

    def test_clean_annotation_does_not_hide_editor_change(self):
        self.change_rows('alpha gamma')
        response = self.client.get('/api/editor/integrity')
        self.assertEqual(200, response.status_code, response.text)
        self.assertEqual('differences', response.json()['status'])
        self.assertEqual('gamma', response.json()['hunks'][0]['script_words'])
        self.assertEqual(409, self.client.post('/api/merge', json={}).status_code)

    def test_saved_script_json_is_unavailable_not_verified_source(self):
        self.state['script_generation_input_file'] = str(self.source)
        self.state['script_generation_options'] = {'strip_front_matter': False}
        self.state['input_file_path'] = str(self.script)
        self.write_state()
        result = self.client.post('/api/merge', json={})
        self.assertEqual(409, result.status_code, result.text)
        self.assertEqual('unavailable', result.json()['detail']['status'])
        self.assertEqual([], self.scheduled)

    def test_loaded_saved_script_resolves_original_source_through_copies(self):
        uploads = self.root / 'uploads'
        uploads.mkdir()
        source = uploads / 'book.txt'
        source.write_text('alpha beta')
        # The reporter's flow saved a copy, loaded it and saved another copy.
        first = self.write_saved_copy('first', source)
        second = self.write_saved_copy('second', first)
        self.state['script_generation_input_file'] = str(self.source)
        self.state['script_generation_options'] = {'strip_front_matter': True}
        self.use_saved_book(second)
        report = self.client.get('/api/editor/integrity').json()
        self.assertEqual('verified', report['status'], report)
        self.assertEqual('book.txt', report['source_name'])
        self.assertEqual(200, self.client.post('/api/merge').status_code)

    def test_loaded_saved_script_without_original_source_explains_restore(self):
        uploads = self.root / 'uploads'
        uploads.mkdir()
        source = uploads / 'book.txt'
        source.write_text('alpha beta')
        scripts = self.root / 'scripts'
        for damage in ('missing-parent', 'missing-binding', 'changed-source'):
            with self.subTest(damage=damage):
                source.write_text('alpha beta')
                first = self.write_saved_copy('first', source)
                second = self.write_saved_copy('second', first)
                if damage == 'missing-parent':
                    first.unlink()
                    Path(scripts, 'first.selection_binding.json').unlink()
                elif damage == 'missing-binding':
                    Path(scripts, 'second.selection_binding.json').unlink()
                else:
                    source.write_text('alpha gamma')
                self.use_saved_book(second)
                report = self.client.get('/api/editor/integrity').json()
                self.assertEqual('unavailable', report['status'], report)
                if damage == 'missing-binding':
                    self.assertIn('generate the script again', report['reason'])
                    self.assertIn('Re-import', report['reason'])
                    self.assertNotIn('Save the script again', report['reason'])
                elif damage == 'missing-parent':
                    self.assertIn('missing or was renamed', report['reason'])
                    self.assertIn('Re-import', report['reason'])
                else:
                    self.assertIn('has changed', report['reason'])
                refusal = self.client.post('/api/merge', json={})
                self.assertEqual(409, refusal.status_code, refusal.text)
                self.assertEqual([], self.scheduled)

    def test_overwritten_intermediate_saved_parent_is_unavailable(self):
        uploads = self.root / 'uploads'
        uploads.mkdir()
        book_a = uploads / 'bookA.txt'
        book_b = uploads / 'bookB.txt'
        book_c = uploads / 'bookC.txt'
        book_a.write_text('alpha beta')
        book_b.write_text('alpha beta')
        book_c.write_text('completely different prose')
        draft = self.write_saved_copy('draft', book_a, text='alpha beta', speaker='A')
        final = self.write_saved_copy('final', draft, text='alpha beta', speaker='A')
        self.use_saved_book(final)
        self.assertEqual('verified', self.client.get('/api/editor/integrity').json()['status'])
        for replacement_book, replacement_text in ((book_b, 'alpha beta'), (book_c, 'completely different prose')):
            with self.subTest(replacement=replacement_book.name):
                self.write_saved_copy('draft', replacement_book, text=replacement_text, speaker='B')
                report = self.client.get('/api/editor/integrity').json()
                self.assertEqual('unavailable', report['status'], report)
                self.assertIn('has changed', report['reason'])
                self.assertNotEqual(replacement_book.name, report['source_name'])
                refusal = self.client.post('/api/merge', json={})
                self.assertEqual(409, refusal.status_code, refusal.text)
                self.assertEqual([], self.scheduled)

    def test_saved_script_confinement_rejects_outside_and_non_library_json(self):
        from book_state_transaction import get_book_operation_token
        from script_selection_binding import BINDING_NAME, make_script_selection_binding
        uploads = self.root / 'uploads'
        uploads.mkdir()
        book = uploads / 'book.txt'
        book.write_text('alpha beta')
        book_sha = hashlib.sha256(book.read_bytes()).hexdigest()
        token = get_book_operation_token(self.temp.name)

        # B1: annotated_script.json with an active binding must stay unavailable.
        active_binding = make_script_selection_binding(
            token, book_sha, self.script.read_bytes(), source_path=str(book))
        (self.root / BINDING_NAME).write_text(json.dumps(active_binding))
        self.use_saved_book(self.script)
        b1 = self.client.get('/api/editor/integrity').json()
        self.assertEqual('unavailable', b1['status'], b1)
        self.assertIn('outside the saved books library', b1['reason'])
        self.assertEqual(409, self.client.post('/api/merge', json={}).status_code)
        (self.root / BINDING_NAME).unlink()

        # B2: elsewhere/carried.json inside data_dir but outside scripts/.
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        carried = elsewhere / 'carried.json'
        carried.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
        (elsewhere / 'carried.selection_binding.json').write_text(json.dumps(
            make_script_selection_binding(token, book_sha, carried.read_bytes(), source_path=str(book))))
        self.use_saved_book(carried)
        b2 = self.client.get('/api/editor/integrity').json()
        self.assertEqual('unavailable', b2['status'], b2)
        self.assertIn('outside the saved books library', b2['reason'])
        self.assertEqual(409, self.client.post('/api/merge', json={}).status_code)

        # B3: scripts/sub/nested.json inside a subdirectory of scripts/.
        sub = self.root / 'scripts' / 'sub'
        sub.mkdir(parents=True)
        nested = sub / 'nested.json'
        nested.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
        (sub / 'nested.selection_binding.json').write_text(json.dumps(
            make_script_selection_binding(token, book_sha, nested.read_bytes(), source_path=str(book))))
        self.use_saved_book(nested)
        b3 = self.client.get('/api/editor/integrity').json()
        self.assertEqual('unavailable', b3['status'], b3)
        self.assertIn('outside the saved books library', b3['reason'])
        self.assertEqual(409, self.client.post('/api/merge', json={}).status_code)

        # B4: scripts/hop.json -> uploads/injected.json -> uploads/book.txt.
        injected = uploads / 'injected.json'
        injected.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
        (uploads / 'injected.selection_binding.json').write_text(json.dumps(
            make_script_selection_binding(token, book_sha, injected.read_bytes(), source_path=str(book))))
        hop = self.write_saved_copy('hop', injected)
        self.use_saved_book(hop)
        b4 = self.client.get('/api/editor/integrity').json()
        self.assertEqual('unavailable', b4['status'], b4)
        self.assertIn('outside the saved books library', b4['reason'])
        self.assertEqual(409, self.client.post('/api/merge', json={}).status_code)

        # B5: /outside/evil.json with corrupt binding outside data_dir must never be opened.
        with tempfile.TemporaryDirectory() as outside_tmp:
            evil = Path(outside_tmp) / 'evil.json'
            evil.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
            (Path(outside_tmp) / 'evil.selection_binding.json').write_bytes(b'{corrupt')
            self.use_saved_book(evil)
            b5 = self.client.get('/api/editor/integrity').json()
            self.assertEqual('unavailable', b5['status'], b5)
            self.assertIn('outside the saved books library', b5['reason'])
            self.assertNotIn('Invalid script binding', b5['reason'])
            self.assertNotIn('damaged', b5['reason'])
            self.assertEqual(409, self.client.post('/api/merge', json={}).status_code)
            self.assertEqual([], self.scheduled)

    def test_loaded_saved_script_uses_recorded_front_matter_policy_not_stale_state(self):
        import core
        from literal_policy import bind_literal_policy
        from routers import scripts_library as library
        from tests.test_publisher_matter import StripPublisherMatterTest

        wn_front = (
            'Manifesto.\n'
            'Translator note for synthetic test.\n'
            'Original Web Novel Chapter - Complete.\n'
            'Original Translation by Synthetic.\n\n'
        )
        wn_story = 'The morning mist cleared over the quiet valley.\n'
        uploads = self.root / 'uploads'
        uploads.mkdir()
        wn_book = uploads / 'wn_book.txt'
        wn_book.write_text(wn_front + wn_story)
        other_book = uploads / 'other.txt'
        other_book.write_text('Completely separate synthetic book.')
        (self.root / 'scripts').mkdir(exist_ok=True)

        # Wire up the real save/load routes alongside editor routes.
        with contextlib.ExitStack() as stack:
            for module in (core, library):
                for name, value in (
                    ('DATA_DIR', str(self.root)),
                    ('SCRIPTS_DIR', str(self.root / 'scripts')),
                    ('SCRIPT_PATH', str(self.script)),
                    ('VOICE_CONFIG_PATH', str(self.root / 'voice_config.json')),
                    ('UPLOADS_DIR', str(uploads)),
                    ('CHUNKS_PATH', str(self.root / 'chunks.json')),
                    ('AUDIOBOOK_PATH', str(self.root / 'book.mp3')),
                    ('M4B_PATH', str(self.root / 'book.m4b')),
                ):
                    if hasattr(module, name):
                        stack.enter_context(patch.object(module, name, value))
            for module in (core, library):
                stack.enter_context(patch.object(module, 'process_state', self.tasks))
            for name in ('_task_claims', '_gpu_leases', '_saved_book_publications'):
                stack.enter_context(patch.object(core, name, {}))
            app = FastAPI()
            app.include_router(editor.router)
            app.include_router(library.router)
            api = stack.enter_context(TestClient(app))

            # Repro 1 (merge bypass): full_book saved with strip_front_matter=False,
            # loaded, front matter removed from Editor chunks, stale state sets
            # script_generation_input_file=wn_book + strip_front_matter=True.
            from book_state_transaction import get_book_operation_token
            from script_selection_binding import BINDING_NAME, make_script_selection_binding
            full_entries = [{'speaker': 'A', 'text': wn_front + wn_story}]
            self.script.write_text(json.dumps(full_entries))
            self.state.update({
                'input_file_path': str(wn_book),
                'script_generation_input_file': str(wn_book),
                'script_generation_options': {'strip_front_matter': False},
            })
            self.write_state()
            (self.root / BINDING_NAME).write_text(json.dumps(make_script_selection_binding(
                get_book_operation_token(self.temp.name, state=self.state),
                hashlib.sha256(wn_book.read_bytes()).hexdigest(),
                self.script.read_bytes(), source_path=str(wn_book))))
            self.publish_run(wn_book, {'strip_front_matter': False}, run_id='1' * 32)
            self.assertEqual(200, api.post('/api/scripts/save', json={'name': 'full_book'}).status_code)
            self.assertFalse(json.loads((self.root / 'scripts' / 'full_book.meta.json').read_text())['strip_front_matter'])
            self.assertEqual(200, api.post('/api/scripts/load', json={'name': 'full_book'}).status_code)
            self.change_rows(wn_story)
            state_after_load = json.loads((self.root / 'state.json').read_text())
            state_after_load['script_generation_input_file'] = str(wn_book)
            state_after_load['script_generation_options'] = {'strip_front_matter': True}
            (self.root / 'state.json').write_text(json.dumps(state_after_load))
            bypass_report = api.get('/api/editor/integrity').json()
            self.assertEqual('differences', bypass_report['status'], bypass_report)
            self.assertGreaterEqual(len(bypass_report['hunks']), 1)
            self.assertEqual(409, api.post('/api/merge', json={}).status_code)
            self.assertEqual([], self.scheduled)

            # Same merge-bypass refusal even when full_book has no recorded .meta.json choice.
            (self.root / 'scripts' / 'full_book.meta.json').write_text(json.dumps({'book_id': 'full_book'}))
            bypass_unrecorded = api.get('/api/editor/integrity').json()
            self.assertEqual('differences', bypass_unrecorded['status'], bypass_unrecorded)
            self.assertEqual(409, api.post('/api/merge', json={}).status_code)
            self.assertEqual([], self.scheduled)

            # Repro 2 (false differences): stripped_book generated with strip_front_matter=True,
            # saved, Save As copy-1, loaded, then another book's stale state options written.
            stripped_entries = [{'speaker': 'A', 'text': wn_story}]
            self.script.write_text(json.dumps(stripped_entries))
            self.state.update({
                'input_file_path': str(wn_book),
                'script_generation_input_file': str(wn_book),
                'script_generation_options': {'strip_front_matter': True},
            })
            self.write_state()
            (self.root / BINDING_NAME).write_text(json.dumps(make_script_selection_binding(
                get_book_operation_token(self.temp.name, state=self.state),
                hashlib.sha256(wn_book.read_bytes()).hexdigest(),
                self.script.read_bytes(), source_path=str(wn_book))))
            self.publish_run(wn_book, {'strip_front_matter': True}, run_id='2' * 32)
            self.assertEqual(200, api.post('/api/scripts/save', json={'name': 'stripped_book'}).status_code)
            self.assertTrue(json.loads((self.root / 'scripts' / 'stripped_book.meta.json').read_text())['strip_front_matter'])
            self.assertEqual(200, api.post('/api/scripts/load', json={'name': 'stripped_book'}).status_code)
            self.assertEqual(200, api.post('/api/scripts/save', json={'name': 'stripped_copy'}).status_code)
            self.assertTrue(json.loads((self.root / 'scripts' / 'stripped_copy.meta.json').read_text())['strip_front_matter'])
            self.assertEqual(200, api.post('/api/scripts/load', json={'name': 'stripped_copy'}).status_code)
            self.change_rows(wn_story)
            state_after_copy = json.loads((self.root / 'state.json').read_text())
            state_after_copy['script_generation_input_file'] = str(other_book)
            state_after_copy['script_generation_options'] = {'strip_front_matter': False}
            (self.root / 'state.json').write_text(json.dumps(state_after_copy))
            stripped_report = api.get('/api/editor/integrity').json()
            self.assertEqual('verified', stripped_report['status'], stripped_report)
            self.assertEqual('current source after generation preprocessing', stripped_report['source_scope'])
            self.assertEqual(200, api.post('/api/merge', json={}).status_code)
            self.scheduled.clear()

            # Publisher colophon (StripPublisherMatterTest.FRONT + STORY) verifies even with no .meta.json.
            pub_book = uploads / 'pub_book.txt'
            pub_book.write_text(StripPublisherMatterTest.FRONT + StripPublisherMatterTest.STORY)
            pub_saved = self.write_saved_copy('pub_saved', pub_book, text=StripPublisherMatterTest.STORY)
            self.change_rows(StripPublisherMatterTest.STORY)
            self.state['script_generation_input_file'] = str(other_book)
            self.state['script_generation_options'] = {'strip_front_matter': False}
            (self.root / BINDING_NAME).unlink(missing_ok=True)
            self.state.pop('script_binding_required', None)
            self.use_saved_book(pub_saved)
            pub_report = api.get('/api/editor/integrity').json()
            self.assertEqual('verified', pub_report['status'], pub_report)
            self.assertEqual(200, api.post('/api/merge', json={}).status_code)
            self.scheduled.clear()

            # Literal-policy saved script uses its bound strip_front_matter=True even without .meta.json.
            literal_entries = bind_literal_policy(
                wn_story, [{'speaker': 'A', 'text': wn_story}],
                input_sha256=hashlib.sha256(wn_book.read_bytes()).hexdigest(),
                strip_front_matter=True)
            literal_saved = self.write_saved_copy('literal_saved', wn_book, entries=literal_entries)
            self.change_rows(wn_story)
            self.state['script_generation_input_file'] = str(other_book)
            self.state['script_generation_options'] = {'strip_front_matter': False}
            self.use_saved_book(literal_saved)
            lit_report = api.get('/api/editor/integrity').json()
            self.assertEqual('verified', lit_report['status'], lit_report)
            self.assertEqual(200, api.post('/api/merge', json={}).status_code)

    def test_active_binding_catches_replaced_saved_script_without_blocking_editor_diffs(self):
        uploads = self.root / 'uploads'
        uploads.mkdir()
        book_a = uploads / 'bookA.txt'
        book_b = uploads / 'bookB.txt'
        book_a.write_text('alpha beta')
        book_b.write_text('alpha beta')
        saved_a = self.write_saved_copy('savedA', book_a, text='alpha beta', speaker='A')
        self.use_saved_book(saved_a, write_active_binding=True)

        verified = self.client.get('/api/editor/integrity').json()
        self.assertEqual('verified', verified['status'], verified)
        self.assertEqual('bookA.txt', verified['source_name'])

        # Live Editor edits to annotated_script.json + chunks still show differences, not unavailable.
        self.script.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha gamma'}]))
        self.change_rows('alpha gamma')
        edited = self.client.get('/api/editor/integrity').json()
        self.assertEqual('differences', edited['status'], edited)
        self.assertEqual('bookA.txt', edited['source_name'])

        # Overwriting scripts/savedA.json + binding after load must become unavailable, not verified against bookB.
        self.change_rows('alpha beta')
        self.write_saved_copy('savedA', book_b, text='alpha beta', speaker='B')
        replaced = self.client.get('/api/editor/integrity').json()
        self.assertEqual('unavailable', replaced['status'], replaced)
        self.assertIn('has changed', replaced['reason'])
        self.assertNotEqual('bookB.txt', replaced['source_name'])
        self.assertEqual(409, self.client.post('/api/merge', json={}).status_code)
        self.assertEqual([], self.scheduled)

    def test_binding_mismatch_cycles_hop_limit_and_structured_damage_hints(self):
        from script_selection_binding import MAX_BINDING_BYTES, MAX_SOURCE_BINDING_HOPS
        uploads = self.root / 'uploads'
        uploads.mkdir()
        book = uploads / 'book.txt'
        book.write_text('alpha beta')
        scripts = self.root / 'scripts'
        scripts.mkdir(exist_ok=True)

        # L5: saved_binding_matches mismatch (saved script tampered after binding).
        tampered = self.write_saved_copy('tampered', book, text='alpha beta')
        tampered.write_text(json.dumps([{'speaker': 'B', 'text': 'alpha beta'}]))
        self.use_saved_book(tampered)
        rep = self.client.get('/api/editor/integrity').json()
        self.assertEqual('unavailable', rep['status'], rep)
        self.assertIn('record is damaged', rep['reason'])
        self.assertEqual(409, self.client.post('/api/merge', json={}).status_code)

        # L5: self-loop and 2-node valid-hash cycle.
        self_loop = scripts / 'self_loop.json'
        self_loop.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
        self.write_saved_copy('self_loop', self_loop, text='alpha beta')
        self.use_saved_book(self_loop)
        rep_self = self.client.get('/api/editor/integrity').json()
        self.assertEqual('unavailable', rep_self['status'], rep_self)
        self.assertIn('chain loops', rep_self['reason'])
        self.assertEqual(409, self.client.post('/api/merge', json={}).status_code)

        node_a = scripts / 'cycle_a.json'
        node_b = scripts / 'cycle_b.json'
        node_a.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
        node_b.write_text(json.dumps([{'speaker': 'B', 'text': 'alpha beta'}]))
        self.write_saved_copy('cycle_a', node_b, text='alpha beta', speaker='A')
        self.write_saved_copy('cycle_b', node_a, text='alpha beta', speaker='B')
        self.use_saved_book(node_a)
        rep_two = self.client.get('/api/editor/integrity').json()
        self.assertEqual('unavailable', rep_two['status'], rep_two)
        self.assertIn('chain loops', rep_two['reason'])
        self.assertEqual(409, self.client.post('/api/merge', json={}).status_code)

        # L5: hop limit (8 hops verified, 9 hops refused).
        chain = [scripts / f'hop_{i}.json' for i in range(MAX_SOURCE_BINDING_HOPS + 1)]
        for i in range(len(chain) - 1, -1, -1):
            parent = book if i == len(chain) - 1 else chain[i + 1]
            self.write_saved_copy(f'hop_{i}', parent, text='alpha beta')
        self.use_saved_book(chain[1])  # 8 hops: hop_1 -> ... -> hop_8 -> book.txt
        rep_8 = self.client.get('/api/editor/integrity').json()
        self.assertEqual('verified', rep_8['status'], rep_8)
        self.assertEqual('book.txt', rep_8['source_name'])
        self.assertEqual(200, self.client.post('/api/merge', json={}).status_code)
        self.scheduled.clear()

        self.use_saved_book(chain[0])  # 9 hops: hop_0 -> ... -> hop_8 -> book.txt
        rep_9 = self.client.get('/api/editor/integrity').json()
        self.assertEqual('unavailable', rep_9['status'], rep_9)
        self.assertIn('longer than allowed', rep_9['reason'])
        self.assertEqual(409, self.client.post('/api/merge', json={}).status_code)

        # L6: corrupt binding, oversized binding, missing saved script, malformed script JSON, deep RecursionError JSON.
        damage_cases = (
            ('corrupt_binding', 'record is damaged', 'Invalid script binding'),
            ('oversized_binding', 'record is damaged', 'Scope'),
            ('missing_script', 'missing or was renamed', 'Selected source is unavailable'),
            ('malformed_script', 'record is damaged', 'Invalid script publication'),
            ('deep_script', 'record is damaged', 'RecursionError'),
        )
        for kind, expected_hint, forbidden_raw in damage_cases:
            with self.subTest(kind=kind):
                saved = self.write_saved_copy(f'l6_{kind}', book, text='alpha beta')
                binding_file = scripts / f'l6_{kind}.selection_binding.json'
                if kind == 'corrupt_binding':
                    binding_file.write_bytes(b'{corrupt')
                elif kind == 'oversized_binding':
                    binding_file.write_bytes(b'x' * (MAX_BINDING_BYTES + 1))
                elif kind == 'missing_script':
                    saved.unlink()
                elif kind == 'malformed_script':
                    saved.write_bytes(b'{corrupt')
                else:
                    saved.write_bytes(b'[' * 2000 + b']' * 2000)
                self.use_saved_book(saved)
                report = self.client.get('/api/editor/integrity').json()
                self.assertEqual('unavailable', report['status'], report)
                self.assertIn(expected_hint, report['reason'])
                self.assertNotIn(forbidden_raw, report['reason'])
                if kind == 'missing_script':
                    self.assertIn('Re-import', report['reason'])
                self.assertEqual(409, self.client.post('/api/merge', json={}).status_code)
                self.assertEqual([], self.scheduled)

    def test_unrecorded_legacy_saved_book_tries_both_preparations(self):
        api = self.library_client()
        source = self.root / 'uploads' / 'wn_book.txt'
        source.write_text(self.WN_FRONT + self.WN_STORY)
        self.write_saved_copy(
            'legacy', source, entries=[{'speaker': 'A', 'text': self.WN_STORY}],
            meta={'book_id': 'legacy'})
        self.write_saved_copy(
            'legacy_full', source,
            entries=[{'speaker': 'A', 'text': self.WN_FRONT + self.WN_STORY}],
            meta={'book_id': 'legacy_full'})

        # A book generated with the default (checked) strip is verified, not a
        # false difference against the unstripped preparation.
        self.load_saved(api, 'legacy')
        self.change_rows(self.WN_STORY)
        report = api.get('/api/editor/integrity').json()
        self.assertEqual('verified', report['status'], report)
        self.assertIs(True, report['resolved_strip_front_matter'])
        self.assertIn('no front-matter preparation choice', report['preparation_note'])
        self.assertIn('front matter stripped', report['preparation_note'])
        self.assertEqual('current source after generation preprocessing', report['source_scope'])
        self.assertEqual(200, api.post('/api/merge', json={}).status_code)
        self.scheduled.clear()

        self.load_saved(api, 'legacy_full')
        self.change_rows(self.WN_FRONT + self.WN_STORY)
        report = api.get('/api/editor/integrity').json()
        self.assertEqual('verified', report['status'], report)
        self.assertIs(False, report['resolved_strip_front_matter'])
        self.assertIn('front matter kept', report['preparation_note'])
        self.assertEqual(200, api.post('/api/merge', json={}).status_code)
        self.scheduled.clear()

        # The script selects the preparation, exactly as Review does: rows
        # edited to the other preparation must not borrow its verification.
        self.load_saved(api, 'legacy')
        self.change_rows(self.WN_FRONT + self.WN_STORY)
        divergent = api.get('/api/editor/integrity').json()
        self.assertEqual('differences', divergent['status'], divergent)
        self.assertIs(True, divergent['resolved_strip_front_matter'])
        self.assertEqual(409, api.post('/api/merge', json={}).status_code)

        # Re-saving the unrecorded book records the preparation it matched, so
        # the loaded copy no longer depends on trying both.
        self.load_saved(api, 'legacy')
        self.change_rows(self.WN_STORY)
        self.assertEqual(200, api.post('/api/scripts/save', json={'name': 'legacy_resaved'}).status_code)
        resaved = json.loads((self.root / 'scripts' / 'legacy_resaved.meta.json').read_text())
        self.assertIs(True, resaved['strip_front_matter'])
        self.load_saved(api, 'legacy_resaved')
        self.change_rows(self.WN_STORY)
        self.assertEqual('verified', api.get('/api/editor/integrity').json()['status'])

    def test_cancelled_regeneration_does_not_record_its_preparation(self):
        api = self.library_client()
        source = self.root / 'uploads' / 'direct.txt'
        source.write_text(self.WN_FRONT + self.WN_STORY)
        self.script.write_text(json.dumps([{'speaker': 'A', 'text': self.WN_STORY}]))
        self.change_rows(self.WN_STORY)
        self.state.update({
            'input_file_path': str(source),
            'script_generation_input_file': str(source),
            'script_generation_options': {'strip_front_matter': True},
        })
        from book_state_transaction import get_book_operation_token
        from script_selection_binding import BINDING_NAME, make_script_selection_binding
        (self.root / BINDING_NAME).write_text(json.dumps(make_script_selection_binding(
            get_book_operation_token(self.temp.name, state=self.state),
            hashlib.sha256(source.read_bytes()).hexdigest(),
            self.script.read_bytes(), source_path=str(source))))
        self.publish_run(source, {'strip_front_matter': True}, run_id='c' * 32)
        self.assertEqual(200, api.post('/api/scripts/save', json={'name': 'published'}).status_code)
        self.assertIs(True, json.loads(
            (self.root / 'scripts' / 'published.meta.json').read_text())['strip_front_matter'])

        # Admission writes the new options before the run; cancelling never
        # reverts them. The published run's receipt no longer exists, so the
        # cancelled choice must not be recorded against the published script.
        self.admit_run(source, {'strip_front_matter': False}, run_id='d' * 32)
        self.state.pop('script_generation_run_id', None)
        self.write_state()
        self.assertEqual(200, api.post('/api/scripts/save', json={'name': 'after_cancel'}).status_code)
        after_cancel = json.loads((self.root / 'scripts' / 'after_cancel.meta.json').read_text())
        self.assertNotIn('strip_front_matter', after_cancel)

        # With nothing recorded, the read path tries both preparations and the
        # genuinely stripped book still verifies.
        self.load_saved(api, 'after_cancel')
        self.change_rows(self.WN_STORY)
        report = api.get('/api/editor/integrity').json()
        self.assertEqual('verified', report['status'], report)
        self.assertIs(True, report['resolved_strip_front_matter'])
        self.assertEqual(200, api.post('/api/merge', json={}).status_code)

    def test_unrecorded_saved_book_reports_fewer_differences_preparation(self):
        api = self.library_client()
        source = self.root / 'uploads' / 'wn_book.txt'
        source.write_text(self.WN_FRONT + self.WN_STORY)
        self.write_saved_copy(
            'legacy', source, entries=[{'speaker': 'A', 'text': self.WN_STORY}],
            meta={'book_id': 'legacy'})
        self.load_saved(api, 'legacy')
        edited = self.WN_STORY.replace('valley', 'mountain')
        self.change_rows(edited)
        report = api.get('/api/editor/integrity').json()
        self.assertEqual('differences', report['status'], report)
        self.assertEqual(1, len(report['hunks']), report)
        self.assertIs(True, report['resolved_strip_front_matter'])
        self.assertIn('no front-matter preparation choice', report['preparation_note'])
        self.assertEqual(409, api.post('/api/merge', json={}).status_code)

        # When the script itself matches neither preparation, the rows being
        # compared pick the one with fewer differences, and the report says so.
        edited_script = [{'speaker': 'A', 'text': edited}]
        self.script.write_text(json.dumps(edited_script))
        fallback = api.get('/api/editor/integrity').json()
        self.assertEqual('differences', fallback['status'], fallback)
        self.assertEqual(1, len(fallback['hunks']), fallback)
        self.assertIs(True, fallback['resolved_strip_front_matter'])
        self.assertIn('no front-matter preparation choice', fallback['preparation_note'])
        self.assertEqual(409, api.post('/api/merge', json={}).status_code)

    def test_saved_book_snapshot_survives_a_cancelled_runs_option_change(self):
        api = self.library_client()
        source = self.root / 'uploads' / 'wn_book.txt'
        source.write_text(self.WN_FRONT + self.WN_STORY)
        self.write_saved_copy(
            'legacy', source, entries=[{'speaker': 'A', 'text': self.WN_STORY}],
            meta={'book_id': 'legacy'})
        self.load_saved(api, 'legacy')
        self.change_rows(self.WN_STORY)
        verified = api.get('/api/editor/integrity').json()
        self.assertEqual('verified', verified['status'], verified)

        # A cancelled re-generation rewrites these run fields but not the
        # comparison, so an issued confirmation must keep working.
        state_after = json.loads((self.root / 'state.json').read_text())
        state_after['script_generation_input_file'] = str(source)
        state_after['script_generation_options'] = {'strip_front_matter': False}
        (self.root / 'state.json').write_text(json.dumps(state_after))
        repeated = api.get('/api/editor/integrity').json()
        self.assertEqual(verified['snapshot'], repeated['snapshot'])
        self.assertEqual(200, api.post(
            '/api/merge', json={'integrity_confirmation': verified['snapshot']}).status_code)
        self.scheduled.clear()

        # A real comparison change still invalidates the confirmation.
        self.change_rows(self.WN_STORY.replace('valley', 'mountain'))
        self.assertEqual(409, api.post(
            '/api/merge', json={'integrity_confirmation': verified['snapshot']}).status_code)

    def test_saved_book_comparison_reads_each_hop_once(self):
        import merge_integrity
        import project_scope_files
        from script_selection_binding import _validated_script_entries
        (self.root / 'uploads').mkdir(exist_ok=True)
        source = self.root / 'uploads' / 'book.txt'
        source.write_text('alpha beta')
        draft = self.write_saved_copy('draft', source, text='alpha beta')
        final = self.write_saved_copy('final', draft, text='alpha beta')
        self.state['input_file_path'] = str(final)
        self.write_state()

        parses, reads = [], []
        real_parse, real_read = _validated_script_entries, project_scope_files.capture_scope_file
        def counting_parse(raw, **kwargs):
            parses.append(1)
            return real_parse(raw, **kwargs)
        def counting_read(root, relative, **kwargs):
            reads.append(relative)
            return real_read(root, relative, **kwargs)
        with patch.object(project_scope_files, 'capture_scope_file', counting_read), \
                patch('script_selection_binding._validated_script_entries', counting_parse):
            details = merge_integrity._get_source_preparation_details(
                self.state, data_dir=str(self.root))
        self.assertEqual(str(source), details['input_path'])
        self.assertEqual(2, len(parses))  # final.json and draft.json, once each
        for name in ('final.json', 'draft.json'):
            self.assertEqual(1, sum(1 for relative in reads if relative.endswith(name)), reads)

    def test_binding_to_uploads_script_json_is_refused_at_merge(self):
        api = self.library_client()
        source = self.root / 'uploads' / 'book.txt'
        source.write_text('alpha beta')
        injected = self.root / 'uploads' / 'injected.json'
        injected.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
        self.write_saved_copy('hop', injected, text='alpha beta')
        self.load_saved(api, 'hop')
        self.change_rows('alpha beta')
        report = api.get('/api/editor/integrity').json()
        self.assertEqual('unavailable', report['status'], report)
        self.assertIn('outside the saved books library', report['reason'])
        self.assertEqual(409, api.post('/api/merge', json={}).status_code)
        self.assertEqual([], self.scheduled)

    def test_foreign_source_settings_cannot_verify_even_identical_prose(self):
        other = self.root / 'other-source.txt'
        other.write_text(self.source.read_text())
        self.state['script_generation_input_file'] = str(other)
        self.state['script_generation_options'] = {'strip_front_matter': False}
        self.write_state()
        report = self.client.get('/api/editor/integrity').json()
        self.assertEqual('unavailable', report['status'])
        self.assertIn('another source', report['reason'])
        self.assertEqual(409, self.client.post('/api/merge').status_code)
        self.assertEqual([], self.scheduled)
        self.state['script_generation_input_file'] = str(self.source)
        self.write_state()
        self.assertEqual('verified', self.client.get('/api/editor/integrity').json()['status'])
        self.assertEqual(200, self.client.post('/api/merge').status_code)

    def test_explicit_confirmation_is_bound_to_source_book_and_chunks(self):
        self.change_rows('alpha gamma')
        snapshot = self.client.post('/api/merge', json={}).json()['detail']['snapshot']
        for kind in ('source', 'book', 'chunks'):
            with self.subTest(kind=kind):
                self.source.write_text('alpha beta' if kind != 'source' else 'alpha delta')
                self.state['book_generation'] = 'generation-b' if kind == 'book' else 'generation-a'
                self.write_state()
                self.change_rows('alpha epsilon' if kind == 'chunks' else 'alpha gamma')
                refusal = self.client.post('/api/merge', json={'integrity_confirmation': snapshot})
                self.assertEqual(409, refusal.status_code, refusal.text)
                self.assertEqual([], self.scheduled)
        self.source.write_text('alpha beta'); self.state['book_generation'] = 'generation-a'
        self.write_state(); self.change_rows('alpha gamma')
        snapshot = self.client.post('/api/merge', json={}).json()['detail']['snapshot']
        self.assertEqual(200, self.client.post('/api/merge', json={'integrity_confirmation': snapshot}).status_code)
        self.assertEqual(1, len(self.scheduled))

    def test_worker_rechecks_and_exports_checked_rows(self):
        self.assertEqual(200, self.client.post('/api/merge', json={}).status_code)
        self.change_rows('alpha gamma')
        with patch.object(self.project, 'merge_audio') as export:
            self.scheduled[0][2]()
            export.assert_not_called()
        self.assertTrue(any('changed' in msg.lower() for msg in self.tasks['audio']['logs']))
        self.scheduled.clear(); self.change_rows('alpha beta')
        self.assertEqual(200, self.client.post('/api/merge', json={}).status_code)
        with patch.object(self.project, 'merge_audio', return_value=(True, 'fixture.mp3')) as export:
            self.scheduled[0][2]()
            self.assertEqual('alpha beta', export.call_args.kwargs['chunks'][0]['text'])


class SourceIntegritySnapshotTests(unittest.TestCase):
    def test_dialogue_boundaries_verify_but_real_word_changes_still_fail(self):
        from merge_integrity import get_source_integrity
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / 'source.txt'
            source.write_text('He said,"Hello"—then left.', encoding='utf-8')
            state = {'input_file_path': str(source), 'book_generation': 'fixture'}
            rows = [{'text': 'He said,'}, {'text': 'Hello'}, {'text': '—then left.'}]
            before = source.read_bytes()
            result = get_source_integrity(state, b'annotation', rows)
            self.assertEqual('verified', result['status'])
            self.assertEqual([], result['hunks'])
            self.assertEqual(5, result['totals']['source_words'])
            changed = [dict(row, text='Goodbye') if index == 1 else dict(row)
                       for index, row in enumerate(rows)]
            rejected = get_source_integrity(state, b'changed annotation', changed)
            self.assertEqual('differences', rejected['status'])
            self.assertNotEqual(result['snapshot'], rejected['snapshot'])
            self.assertEqual(('hello', 'goodbye', 1),
                             tuple(rejected['hunks'][0][key] for key in
                                   ('source_words', 'script_words', 'entry_index')))
            source.write_text('nowhere', encoding='utf-8')
            self.assertEqual('differences', get_source_integrity(
                state, b'split', [{'text': 'now here'}])['status'])
            self.assertEqual('He said,"Hello"—then left.', before.decode('utf-8'))

    def test_real_owned_http_admission_does_not_deadlock_and_releases_failed_registration(self):
        import subprocess
        import sys
        command=[sys.executable,'-c',
                 'import ci_env,unittest; '
                 's=unittest.defaultTestLoader.loadTestsFromName("tests.test_task_claim_ownership.TaskClaimOwnershipTests.test_actual_editor_and_script_http_registration_failures_release_only_their_claim"); '
                 'r=unittest.TextTestRunner().run(s); raise SystemExit(not r.wasSuccessful())']
        try:
            result=subprocess.run(command,cwd=Path(__file__).resolve().parent.parent,
                                  capture_output=True,text=True,timeout=10)
        except subprocess.TimeoutExpired:
            self.fail('Native owned HTTP task admission deadlocked or exceeded its 10-second bound')
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)

    def test_native_preparation_accepts_publisher_cleanup_and_rejects_wrong_settings(self):
        from merge_integrity import get_source_integrity
        from tests.test_publisher_matter import StripPublisherMatterTest
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.txt'
            source.write_text(StripPublisherMatterTest.FRONT+StripPublisherMatterTest.STORY)
            state={'input_file_path':str(source),'script_generation_input_file':str(source),
                   'script_generation_options':{'strip_front_matter':False}}
            rows=[{'text':StripPublisherMatterTest.STORY}]
            self.assertEqual('verified',get_source_integrity(state,b'fixture',rows)['status'])
            changed=[{'text':StripPublisherMatterTest.STORY.replace('snow','rain',1)}]
            rejected=get_source_integrity(state,b'fixture',changed)
            self.assertEqual('differences',rejected['status'])
            self.assertEqual(('snow','rain'),(rejected['hunks'][0]['source_words'],rejected['hunks'][0]['script_words']))
            state['script_generation_options']['strip_front_matter']='false'
            self.assertEqual('unavailable',get_source_integrity(state,b'fixture',rows)['status'])
            state['script_generation_input_file']='other-book.txt'
            self.assertEqual('unavailable',get_source_integrity(state,b'fixture',rows)['status'])

    def test_cache_is_bounded_copy_safe_and_invalidates_source_book_annotation_and_rows(self):
        import merge_integrity
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.txt';source.write_text('alpha beta')
            state={'input_file_path':str(source),'book_generation':'one'};rows=[{'text':'alpha beta'}]
            merge_integrity._get_cached_diff.cache_clear()
            with patch.object(merge_integrity,'word_diff',wraps=merge_integrity.word_diff) as compare:
                first=merge_integrity.get_source_integrity(state,b'annotation',rows)
                first['hunks'].append({'kind':'fake'})
                second=merge_integrity.get_source_integrity(state,b'annotation',rows)
                self.assertEqual([],second['hunks']);self.assertEqual(1,compare.call_count)
                previous=second['snapshot']
                for kind in ('source','book','annotation','rows'):
                    if kind=='source':source.write_text('alpha gamma')
                    if kind=='book':state['book_generation']='two'
                    if kind=='rows':rows=[{'text':'beta alpha'}]
                    current=merge_integrity.get_source_integrity(state,b'new annotation' if kind in ('annotation','rows') else b'annotation',rows)
                    self.assertNotEqual(previous,current['snapshot']);previous=current['snapshot']
                self.assertEqual(5,compare.call_count)
                self.assertEqual(1,merge_integrity._get_cached_diff.cache_info().currsize)
                with patch.object(merge_integrity.copy, 'deepcopy', side_effect=AssertionError('full report copy')):
                    page=merge_integrity.get_source_integrity(state,b'new annotation',rows,review_offset=0)
                self.assertEqual(current['snapshot'],page['snapshot'])
                self.assertEqual(current['status'],page['status'])
                self.assertNotIn('hunks',page)
                self.assertEqual(5,compare.call_count)

    def test_paged_comparison_preserves_full_text_and_bounds_real_json_wire(self):
        from merge_integrity import get_source_integrity_page, SOURCE_REVIEW_PAGE_CHARS
        from starlette.responses import JSONResponse
        hunk=dict(source_before='Before',source_words='old',source_after='After',script_words='new')
        cases=[dict(status='differences',snapshot='a'*64,hunks=[hunk]),
               dict(status='differences',snapshot='a'*64,hunks=[{**hunk,'source_words':'old '*250000,'script_words':'new '*250000}]),
               dict(status='differences',snapshot='a'*64,hunks=[hunk]*100),
               dict(status='unavailable',snapshot='a'*64,reason='Unavailable synthetic reason. '*50000),
               dict(status='unavailable',snapshot='a'*64,reason=None),
               dict(status='unavailable',snapshot='a'*64,reason='\x00'*8192+'雪😀'*8192)]
        for report in cases:
            with self.subTest(status=report['status'],hunks=len(report.get('hunks',[]))):
                expected=(report.get('reason') or 'Original source comparison is unavailable.') if report['status']=='unavailable' else '\n\n'.join(
                    f"Source: {h['source_before']} [{h['source_words']}] {h['source_after']}\nMixed dialogue: {h['script_words']}" for h in report['hunks'])
                pieces=[];offset=0
                while True:
                    page=get_source_integrity_page(report,offset)
                    self.assertEqual(expected[offset:offset+SOURCE_REVIEW_PAGE_CHARS],page['page'])
                    self.assertEqual(len(expected),page['total'])
                    bound={**page,'candidate_revision':'b'*64,'approval_revision':'c'*64}
                    self.assertLessEqual(len(JSONResponse({'detail':bound},status_code=409).body),65536)
                    pieces.append(page['page'])
                    if page['next_offset'] is None:break
                    self.assertEqual(offset+len(page['page']),page['next_offset'])
                    offset=page['next_offset']
                self.assertEqual(expected,''.join(pieces))
                self.assertEqual('',get_source_integrity_page(report,len(expected))['page'])
                with self.assertRaises(ValueError):get_source_integrity_page(report,len(expected)+1)
        # Many regions exercise bounded presentation at both ends and a middle
        # range without an all-pages quadratic scan in the routine unit suite.
        report=dict(status='differences',snapshot='a'*64,hunks=[hunk]*100000)
        unit='Source: Before [old] After\nMixed dialogue: new'
        expected='\n\n'.join([unit]*100000)
        for offset in (0,len(expected)//2,len(expected)-23):
            page=get_source_integrity_page(report,offset)
            self.assertEqual(expected[offset:offset+SOURCE_REVIEW_PAGE_CHARS],page['page'])
            self.assertEqual(len(expected),page['total'])
        for offset in (True,-1,1.5,'0',2**53):
            with self.assertRaises(ValueError):get_source_integrity_page(report,offset)
        clean=get_source_integrity_page(dict(status='verified',snapshot='a'*64),0)
        self.assertEqual(('',0,None),(clean['page'],clean['total'],clean['next_offset']))


if __name__ == '__main__':
    unittest.main()
