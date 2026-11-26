import json
import hashlib
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from script_selection_binding import (make_script_selection_binding,
                                      check_script_selection_binding, publish_generated_script,
                                      capture_generation_selection, BINDING_NAME)
from book_state_transaction import ensure_book_state, get_book_operation_token
from tests.test_support import (intercept_book_publications,
                                write_three_pass_completion_manifest,
                                build_three_pass_completion_manifest)


class ScriptSelectionBindingTests(unittest.TestCase):
    def test_staged_registry_refuses_existing_non_registry_content(self):
        from script_selection_binding import _add_staged_registry
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = root / 'custom-aliases.json'
            original = b'not an alias registry'
            registry.write_bytes(original)
            replacements = {}
            with self.assertRaises(ValueError):
                _add_staged_registry(root, {'before': original, 'after': b'{}'}, registry, replacements)
            self.assertEqual({}, replacements)
            self.assertEqual(original, registry.read_bytes())

    def setUp(self):
        self.token = 'a' * 64
        self.source = 'b' * 64
        self.script = b'[{"text":"Original line."}]'
        self.binding = make_script_selection_binding(self.token, self.source, self.script,
                                                     source_path='/source/a.txt')
        self.raw = json.dumps(self.binding).encode()

    def test_same_selection_requires_independent_edit_provenance(self):
        self.assertFalse(check_script_selection_binding(
            self.raw, self.token, self.source, self.script, source_path='/source/a.txt')['edited_after_publication'])
        with self.assertRaises(ValueError):
            check_script_selection_binding(self.raw, self.token, self.source,
                                           b'[{"text":"Edited line."}]', source_path='/source/a.txt')
        self.assertTrue(check_script_selection_binding(
            self.raw, self.token, self.source, b'[{"text":"Edited line."}]',
            source_path='/source/a.txt', verified_edit=True)['edited_after_publication'])

    def test_new_selection_or_source_cannot_rebind_old_script(self):
        for token, source in [('c' * 64, self.source), (self.token, 'd' * 64)]:
            with self.subTest(token=token, source=source), self.assertRaises(ValueError):
                check_script_selection_binding(self.raw, token, source, self.script, source_path='/source/a.txt')
        with self.assertRaises(ValueError):
            check_script_selection_binding(self.raw, self.token, self.source,
                                           self.script, source_path='/source/b.txt')

    def test_corrupt_duplicate_and_oversized_bindings_are_rejected(self):
        invalid = [b'', b'{}', b'[]', b'x' * 4097,
                   b'{"version":1,"version":1}',
                   json.dumps({**self.binding, 'version': True}).encode(),
                   json.dumps({**self.binding, 'source_sha256': 'a' * 63}).encode(),
                   json.dumps({**self.binding, 'unexpected': 1}).encode()]
        for raw in invalid:
            with self.subTest(raw=raw[:100]), self.assertRaises(ValueError):
                check_script_selection_binding(raw, self.token, self.source, self.script,
                                               source_path='/source/a.txt')

    def test_binding_rejects_nonfinite_and_ambiguous_script_json(self):
        for raw in (b'[{"text":"line","pause_after":NaN}]',
                    b'[{"text":"line","pause_after":Infinity}]',
                    b'[{"text":"line","pause_after":1e999}]',
                    b'[{"text":"line","pause_after":-1e999}]',
                    b'[{"text":"first","text":"second"}]'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                make_script_selection_binding(self.token, self.source, raw,
                                              source_path='/source/a.txt')
        from script_selection_binding import _read_selection_state, _voice_remap_bytes
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            state = root / 'state.json'
            config = root / 'voice_config.json'
            for value in ('NaN', 'Infinity', '1e999', '-1e999'):
                with self.subTest(value=value):
                    state.write_text('{"nested": [' + value + ']}')
                    with self.assertRaises(ValueError):
                        _read_selection_state(root, required=True)
                    config.write_text('{"nested": [' + value + ']}')
                    with self.assertRaises(ValueError):
                        _voice_remap_bytes(root, config.name, {}, lambda *_: ({}, 0))
                    self.assertEqual('{"nested": [' + value + ']}', config.read_text())
            state.write_text('{"small": 1e-999, "zero": -0.0}')
            self.assertEqual(0.0, _read_selection_state(root, required=True)['small'])

    def test_binding_rejects_deeply_nested_script_as_invalid(self):
        raw = ('[' * 2000 + ']' * 2000).encode()
        # Python versions may reject this during parsing or after parsing when
        # the outer list is found not to contain valid script entries.
        with self.assertRaises(ValueError):
            make_script_selection_binding(self.token, self.source, raw,
                                          source_path='/source/a.txt')

    def test_capture_rejects_oversized_state_without_reading_it_into_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.txt'
            source.write_bytes(b'source')
            with (root / 'state.json').open('wb') as output:
                output.truncate(4 * 1024**2 + 1)
            with ensure_book_state(root):
                with patch.object(Path, 'read_bytes', side_effect=AssertionError('unbounded read')):
                    with self.assertRaises(ValueError):
                        capture_generation_selection(root, str(source))

    def test_capture_token_comes_from_the_bounded_selection_snapshot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.txt'
            source.write_bytes(b'source')
            state = {'active_book_id': 'a', 'book_generation': 'one', 'input_file_path': str(source)}
            (root / 'state.json').write_text(json.dumps(state))
            expected = get_book_operation_token(root)
            with ensure_book_state(root):
                with patch.object(Path, 'read_bytes', side_effect=AssertionError('unbounded reread')):
                    captured = capture_generation_selection(root, str(source))
            self.assertEqual(expected, captured['book_token'])

    def test_capture_rejects_ambiguous_state(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.txt'
            source.write_bytes(b'source')
            (root / 'state.json').write_text('{"input_file_path": "first", "input_file_path": "second"}')
            with ensure_book_state(root), self.assertRaisesRegex(ValueError, 'Duplicate'):
                capture_generation_selection(root, str(source))

    def test_selection_guards_reject_deeply_nested_state_as_invalid(self):
        from script_selection_binding import require_published_selection, saved_script_is_active_source
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'state.json').write_text('[' * 2000 + ']' * 2000)
            with self.assertRaises(ValueError):
                require_published_selection(root)
            with self.assertRaises(ValueError):
                saved_script_is_active_source(root, str(root / 'book.json'))

    def test_alias_registry_cannot_target_lock_or_transaction_metadata(self):
        from script_selection_binding import _staged_registry_path
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('.active_book_transaction.json', '.active_book_transaction.json.lock',
                         'state.json.lock', 'voice_config.json.lock',
                         'annotated_script.json.threepass_checkpoint.json',
                         'annotated_script.json.threepass_checkpoint.json.parts-example',
                         'cloned_audiobook.mp3', 'audiobook.m4b', 'voice_library.json', 'config.json',
                         '.project_scope_undo.json'):
                staged = {'path':str(root / name), 'before':None, 'after':b'{}'}
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'collides'):
                    _staged_registry_path(root, staged)

    def test_alias_registry_rejects_windows_reserved_and_trailing_names(self):
        from script_selection_binding import _staged_registry_path
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name in ('CON', 'aliases.json.', 'aliases.json '):
                staged = {'path':str(root / name), 'before':None, 'after':b'{}'}
                with self.subTest(name=name), self.assertRaisesRegex(ValueError, 'portable'):
                    _staged_registry_path(root, staged)

    def test_legacy_controlled_edit_still_validates_unbound_replacement_bytes(self):
        from script_selection_binding import publish_controlled_script_edit
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'state.json').write_text('{}')
            (root / 'annotated_script.json').write_bytes(self.script)
            with self.assertRaises((ValueError, json.JSONDecodeError)):
                publish_controlled_script_edit(root, self.script, b'{not-json')
            self.assertEqual(self.script, (root / 'annotated_script.json').read_bytes())

    def test_controlled_edit_rejects_inconsistent_entries_fingerprint(self):
        from script_selection_binding import publish_controlled_script_edit
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.txt'; source.write_bytes(b'source')
            (root / 'state.json').write_text(json.dumps({'active_book_id':'a',
                'book_generation':'one', 'input_file_path':str(source),
                'script_generation_run_id':'f' * 32, 'script_generation_base_sha256':None}))
            selected = capture_generation_selection(root, str(source))
            publish_generated_script(root, selected['book_token'], str(source),
                selected['source_sha256'], self.script, expected_run_id='f' * 32)
            binding_path = root / BINDING_NAME
            binding = json.loads(binding_path.read_text())
            binding['published_entries_sha256'] = '0' * 64
            binding_path.write_text(json.dumps(binding))
            with self.assertRaises(ValueError):
                publish_controlled_script_edit(root, self.script, b'[{"text":"edited"}]')
            self.assertEqual(self.script, (root / 'annotated_script.json').read_bytes())

    def test_publication_rejects_oversized_state_without_reading_it_into_memory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / 'source.txt'
            source.write_bytes(b'source')
            (root / 'annotated_script.json').write_bytes(b'old script')
            with (root / 'state.json').open('wb') as output:
                output.truncate(4 * 1024**2 + 1)
            with ensure_book_state(root):
                with patch.object(Path, 'read_bytes', side_effect=AssertionError('unbounded read')):
                    with self.assertRaises(ValueError):
                        publish_generated_script(root, self.token, str(source),
                            hashlib.sha256(b'source').hexdigest(), self.script, expected_run_id='a' * 32)
            self.assertEqual(b'old script', (root / 'annotated_script.json').read_bytes())

    def test_publication_checks_capture_and_publishes_script_and_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.txt'
            source.write_bytes(b'Original source')
            (root / 'state.json').write_text(json.dumps({'active_book_id': 'a',
                'book_generation': 'one', 'input_file_path': str(source), 'script_generation_run_id': 'f' * 32}))
            (root / 'annotated_script.json').write_bytes(b'[{"text":"Old"}]')
            state = json.loads((root / 'state.json').read_text())
            state['script_generation_base_sha256'] = hashlib.sha256((root / 'annotated_script.json').read_bytes()).hexdigest()
            (root / 'state.json').write_text(json.dumps(state))
            token = get_book_operation_token(tmp)
            source_hash = hashlib.sha256(source.read_bytes()).hexdigest()
            captured = capture_generation_selection(tmp, str(source))
            self.assertEqual({'book_token': token, 'source_path': str(source),
                              'source_sha256': source_hash}, captured)
            with self.assertRaises(ValueError):
                capture_generation_selection(tmp, str(root / 'other.txt'))
            with self.assertRaises(ValueError):
                publish_generated_script(tmp, token, str(source), source_hash, b'[{"value":NaN}]', expected_run_id='f' * 32)
            with self.assertRaises(ValueError):
                publish_generated_script(tmp, token, str(source), source_hash, b'[{"value":1e999}]', expected_run_id='f' * 32)
            source.write_bytes(b'Changed source')
            with self.assertRaises(ValueError):
                publish_generated_script(tmp, token, str(source), source_hash, self.script, expected_run_id='f' * 32)
            self.assertEqual(b'[{"text":"Old"}]', (root / 'annotated_script.json').read_bytes())
            self.assertFalse((root / BINDING_NAME).exists())
            source.write_bytes(b'Original source')
            with ensure_book_state(tmp):
                publish_generated_script(tmp, token, str(source), source_hash, self.script, expected_run_id='f' * 32)
            self.assertEqual(self.script, (root / 'annotated_script.json').read_bytes())
            self.assertFalse(check_script_selection_binding((root / BINDING_NAME).read_bytes(),
                token, source_hash, self.script, source_path=str(source))['edited_after_publication'])
            state = json.loads((root / 'state.json').read_text())
            state['book_generation'] = 'two'
            (root / 'state.json').write_text(json.dumps(state))
            with self.assertRaises(ValueError):
                publish_generated_script(tmp, token, str(source), source_hash, b'[{"text":"Late"}]', expected_run_id='f' * 32)
            self.assertEqual(self.script, (root / 'annotated_script.json').read_bytes())

    def test_interrupted_publication_preserves_script_and_binding(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.txt'
            source.write_bytes(b'source')
            (root / 'state.json').write_text(json.dumps({'active_book_id':'a',
                'book_generation':'one', 'input_file_path':str(source), 'script_generation_run_id': 'f' * 32}))
            (root / 'annotated_script.json').write_bytes(b'old script')
            state = json.loads((root / 'state.json').read_text())
            state['script_generation_base_sha256'] = hashlib.sha256(b'old script').hexdigest()
            (root / 'state.json').write_text(json.dumps(state))
            (root / BINDING_NAME).write_bytes(b'old binding')
            (root / 'chunks.json').write_bytes(b'old chunks')
            (root / 'chunks.json.status.jsonl').write_bytes(b'old journal')
            manifest = root / 'annotated_script.json.threepass_manifest.json'
            write_three_pass_completion_manifest(manifest, self.script, run_id='f' * 32)
            token = get_book_operation_token(tmp)
            import book_state_transaction as transaction
            binding_path = root / BINDING_NAME
            def fail_on_binding(target):
                if target == binding_path:
                    raise OSError('injected publication failure')
            with intercept_book_publications(transaction, before=fail_on_binding):
                with self.assertRaisesRegex(OSError, 'injected publication failure'):
                    publish_generated_script(tmp, token, str(source),
                        hashlib.sha256(b'source').hexdigest(), self.script, expected_run_id='f' * 32, complete_manifest=True)
            self.assertEqual(b'old script', (root / 'annotated_script.json').read_bytes())
            self.assertEqual(b'old binding', (root / BINDING_NAME).read_bytes())
            self.assertEqual(b'old chunks', (root / 'chunks.json').read_bytes())
            self.assertEqual(b'old journal', (root / 'chunks.json.status.jsonl').read_bytes())
            self.assertEqual('publishing', json.loads(manifest.read_text())['status'])
            publish_generated_script(tmp, token, str(source),
                hashlib.sha256(b'source').hexdigest(), self.script, expected_run_id='f' * 32, complete_manifest=True)
            self.assertFalse((root / 'chunks.json').exists())
            self.assertFalse((root / 'chunks.json.status.jsonl').exists())
            self.assertEqual('complete', json.loads(manifest.read_text())['status'])
            self.assertEqual({'run_id': 'f' * 32,
                              'script_sha256': hashlib.sha256(self.script).hexdigest()},
                             json.loads(manifest.read_text())['publication'])

    def test_publication_rejects_inconsistent_saved_generation_selection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source.txt'; source.write_bytes(b'source')
            (root / 'state.json').write_text(json.dumps({
                'active_book_id':'a', 'book_generation':'one',
                'input_file_path':str(source), 'script_generation_run_id':'f' * 32,
                'script_generation_base_sha256':None,
                'script_generation_selection': {
                    'book_token':'0' * 64, 'source_path':str(source),
                    'source_sha256':hashlib.sha256(b'source').hexdigest()}}))
            token = get_book_operation_token(tmp)
            with self.assertRaisesRegex(ValueError, 'selection identity'):
                publish_generated_script(tmp, token, str(source),
                    hashlib.sha256(b'source').hexdigest(), self.script,
                    expected_run_id='f' * 32)

    def test_late_generation_cannot_overwrite_a_newer_generation(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source.txt'; source.write_bytes(b'source')
            (root / 'state.json').write_text(json.dumps({'active_book_id':'a',
                'book_generation':'one', 'input_file_path':str(source), 'script_generation_run_id': 'f' * 32,
                'script_generation_base_sha256': None}))
            token = get_book_operation_token(tmp); digest = hashlib.sha256(b'source').hexdigest()
            newer = b'[{"text":"newer run"}]'; older = b'[{"text":"older run"}]'
            publish_generated_script(tmp, token, str(source), digest, newer, expected_run_id='f' * 32)
            with self.assertRaises(ValueError):
                publish_generated_script(tmp, token, str(source), digest, older, expected_run_id='e' * 32)
            self.assertEqual(newer, (root / 'annotated_script.json').read_bytes())

    def test_edit_during_generation_is_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source.txt'; source.write_bytes(b'source')
            script = root / 'annotated_script.json'; script.write_bytes(b'original')
            (root / 'state.json').write_text(json.dumps({'active_book_id':'a',
                'book_generation':'one', 'input_file_path':str(source), 'script_generation_run_id':'f'*32,
                'script_generation_base_sha256':hashlib.sha256(b'original').hexdigest()}))
            token = get_book_operation_token(tmp)
            script.write_bytes(b'human edit')
            with self.assertRaisesRegex(ValueError, 'edits were preserved'):
                publish_generated_script(tmp, token, str(source), hashlib.sha256(b'source').hexdigest(),
                                         self.script, expected_run_id='f'*32)
            self.assertEqual(b'human edit', script.read_bytes())

    def test_editor_does_not_load_previous_chunks_after_new_selection(self):
        from fastapi import HTTPException
        from routers import editor
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'annotated_script.json').write_bytes(b'old script')
            (root / 'chunks.json').write_bytes(b'old chunks')
            (root / 'state.json').write_text(json.dumps({'script_selection_pending':True}))
            with patch.object(editor, 'SCRIPT_PATH', str(root / 'annotated_script.json')), \
                 patch.object(editor, 'DATA_DIR', str(root)), \
                 patch.object(editor.project_manager, 'load_chunks') as load:
                with self.assertRaises(HTTPException) as refused:
                    editor._ensure_editor_chunks()
                with self.assertRaises(HTTPException) as audio:
                    editor.ensure_audio_generation_requests([0])
                with self.assertRaises(HTTPException) as export:
                    editor.get_export_quality_attempt(None, {'cancel':False})
            self.assertEqual(409, refused.exception.status_code)
            self.assertEqual(409, audio.exception.status_code)
            self.assertEqual(409, export.exception.status_code)
            load.assert_not_called()
            self.assertEqual(b'old chunks', (root / 'chunks.json').read_bytes())

    def test_legacy_unbound_script_remains_readable_and_reviewable(self):
        from routers import script as routes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            entries = [{'speaker': 'Narrator', 'text': 'Legacy script.'}]
            (root / 'annotated_script.json').write_text(json.dumps(entries), encoding='utf-8')
            (root / 'state.json').write_text('{}', encoding='utf-8')
            with patch.object(routes, 'DATA_DIR', tmp), \
                 patch.object(routes, 'SCRIPT_PATH', str(root / 'annotated_script.json')):
                self.assertEqual(entries, routes._get_annotated_script_sync())
                self.assertEqual('review admitted', routes._with_published_script(lambda: 'review admitted'))
            self.assertFalse((root / BINDING_NAME).exists())

    def test_review_preflight_and_owned_worker_refuse_pending_selection(self):
        from fastapi import HTTPException
        from routers import script as routes
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'annotated_script.json').write_bytes(b'old script')
            (root / 'state.json').write_text(json.dumps({'script_selection_pending':True}))
            with patch.object(routes, 'DATA_DIR', tmp), \
                 patch.object(routes, 'SCRIPT_PATH', str(root / 'annotated_script.json')), \
                 patch.object(routes, 'process_state', {'review':{}}), \
                 patch.object(routes, 'run_process') as worker:
                with self.assertRaises(HTTPException) as preflight:
                    routes._with_published_script(lambda: None)
                self.assertEqual(1, routes._run_published_script_process(['fixture'], 'review'))
                self.assertEqual('failed', routes.process_state['review']['status'])
                self.assertIn('could not start', routes.process_state['review']['logs'][0])
                with self.assertRaises(HTTPException) as direct_read:
                    routes._get_annotated_script_sync()
                with self.assertRaises(HTTPException) as diff:
                    routes._ensure_annotated_script_diff()
            self.assertEqual(409, preflight.exception.status_code)
            self.assertEqual(409, direct_read.exception.status_code)
            self.assertEqual(409, diff.exception.status_code)
            worker.assert_not_called()

    @unittest.skipUnless(hasattr(os, 'mkfifo') and hasattr(os, 'O_NONBLOCK'), 'requires POSIX FIFO')
    def test_source_swapped_to_fifo_does_not_block_or_publish(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.txt'
            source.write_bytes(b'source')
            (root / 'state.json').write_text(json.dumps({'active_book_id':'a',
                'book_generation':'one', 'input_file_path':str(source), 'script_generation_run_id': 'f' * 32}))
            (root / 'annotated_script.json').write_bytes(b'old script')
            state = json.loads((root / 'state.json').read_text())
            state['script_generation_base_sha256'] = hashlib.sha256(b'old script').hexdigest()
            (root / 'state.json').write_text(json.dumps(state))
            token = get_book_operation_token(tmp)
            real_open = os.open
            replaced = False
            def swap(path, flags, *args, **kwargs):
                nonlocal replaced
                if Path(path) == source and not replaced:
                    source.unlink()
                    os.mkfifo(source)
                    replaced = True
                return real_open(path, flags, *args, **kwargs)
            with patch('script_selection_binding.os.open', side_effect=swap):
                with self.assertRaises(ValueError):
                    publish_generated_script(tmp, token, str(source),
                        hashlib.sha256(b'source').hexdigest(), self.script, expected_run_id='f' * 32)
            self.assertTrue(replaced)
            self.assertEqual(b'old script', (root / 'annotated_script.json').read_bytes())
            self.assertFalse((root / BINDING_NAME).exists())


class LockedPublisherStaleSourceTests(unittest.TestCase):
    def test_locked_publisher_refuses_source_swapped_after_early_check(self):
        """The locked publisher re-verifies the confined source itself: a swap
        between capture (the early check) and the guarded commit is refused and
        the last script and chunks stay byte-for-byte unchanged."""
        from script_selection_binding import STALE_SOURCE_MESSAGE
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.txt'
            source.write_bytes(b'source')
            (root / 'state.json').write_text(json.dumps({
                'active_book_id': 'a', 'book_generation': 'one',
                'input_file_path': str(source), 'script_generation_run_id': 'f' * 32,
                'script_generation_base_sha256': hashlib.sha256(b'old script').hexdigest()}))
            (root / 'annotated_script.json').write_bytes(b'old script')
            (root / 'chunks.json').write_bytes(b'old chunks')
            selected = capture_generation_selection(tmp, str(source))
            source.write_bytes(b'a different source selected after capture')
            with self.assertRaisesRegex(ValueError, 'changed during generation'):
                publish_generated_script(tmp, selected['book_token'], str(source),
                                         selected['source_sha256'], b'[{"text":"new"}]',
                                         expected_run_id='f' * 32)
            self.assertIn('preserving prior output', STALE_SOURCE_MESSAGE)
            self.assertEqual(b'old script', (root / 'annotated_script.json').read_bytes())
            self.assertEqual(b'old chunks', (root / 'chunks.json').read_bytes())
            self.assertFalse((root / BINDING_NAME).exists())


class CompletionEvidencePublisherTests(unittest.TestCase):
    def test_locked_publisher_requires_valid_attempt_bound_completion_evidence(self):
        """Deleting, corrupting or contradicting the manifest at the locked
        publish read must preserve the last script, chunks and state exactly."""
        writers = {
            'deleted': None,
            'corrupt': '{bad',
            'contradictory': json.dumps({
                'status': 'publishing', 'run_id': 'f' * 32,
                'passes': {'attribute': {'unchecked_entries': 2}},
                'diagnostic_failures': []}),
            'other_attempt': json.dumps({
                'status': 'publishing', 'run_id': 'e' * 32,
                'passes': {}, 'diagnostic_failures': []}),
        }
        for label, raw in writers.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                source = root / 'source.txt'
                source.write_bytes(b'source')
                state = {'active_book_id': 'a', 'book_generation': 'one',
                         'input_file_path': str(source), 'script_generation_run_id': 'f' * 32,
                         'script_generation_base_sha256': hashlib.sha256(b'old script').hexdigest()}
                (root / 'state.json').write_text(json.dumps(state))
                (root / 'annotated_script.json').write_bytes(b'old script')
                (root / 'chunks.json').write_bytes(b'old chunks')
                manifest = root / 'annotated_script.json.threepass_manifest.json'
                if raw is not None:
                    manifest.write_text(raw)
                before_state = (root / 'state.json').read_bytes()
                token = get_book_operation_token(tmp)
                with self.assertRaisesRegex(ValueError, 'manifest|completion evidence'):
                    publish_generated_script(tmp, token, str(source),
                                             hashlib.sha256(b'source').hexdigest(),
                                             b'[{"text":"new"}]', expected_run_id='f' * 32,
                                             complete_manifest=True)
                self.assertEqual(b'old script', (root / 'annotated_script.json').read_bytes())
                self.assertEqual(b'old chunks', (root / 'chunks.json').read_bytes())
                self.assertEqual(before_state, (root / 'state.json').read_bytes())
                self.assertFalse((root / BINDING_NAME).exists())

    def test_source_swap_at_guarded_replacement_boundary_preserves_output(self):
        from script_selection_binding import require_unchanged_source
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'source.txt'
            source.write_bytes(b'source')
            (root / 'state.json').write_text(json.dumps({
                'active_book_id': 'a', 'book_generation': 'one',
                'input_file_path': str(source), 'script_generation_run_id': 'f' * 32,
                'script_generation_base_sha256': hashlib.sha256(b'old script').hexdigest()}))
            (root / 'annotated_script.json').write_bytes(b'old script')
            (root / 'chunks.json').write_bytes(b'old chunks')
            write_three_pass_completion_manifest(
                root / 'annotated_script.json.threepass_manifest.json',
                b'[{"text":"new"}]', run_id='f' * 32)
            selected = capture_generation_selection(tmp, str(source))
            checked = []

            def swap_then_check():
                checked.append(True)
                source.write_bytes(b'replaced during guarded staging')
                require_unchanged_source(str(source), selected['source_sha256'],
                                         owning_root=tmp)

            token = get_book_operation_token(tmp)
            with self.assertRaisesRegex(ValueError, 'changed during generation'):
                publish_generated_script(tmp, token, str(source),
                                         selected['source_sha256'], b'[{"text":"new"}]',
                                         expected_run_id='f' * 32, complete_manifest=True,
                                         before_replace=swap_then_check)
            self.assertTrue(checked)
            self.assertEqual(b'old script', (root / 'annotated_script.json').read_bytes())
            self.assertEqual(b'old chunks', (root / 'chunks.json').read_bytes())
            self.assertFalse((root / BINDING_NAME).exists())


class CompletionRecordSubstitutionTests(unittest.TestCase):
    """Each forged completion substitution is refused at the locked boundary."""

    def _setup_book(self, tmp, script=b'[{"text":"new"}]'):
        root = Path(tmp)
        source = root / 'source.txt'
        source.write_bytes(b'source')
        old = b'old script'
        state = {'active_book_id': 'a', 'book_generation': 'one',
                 'input_file_path': str(source), 'script_generation_run_id': 'f' * 32,
                 'script_generation_base_sha256': hashlib.sha256(old).hexdigest()}
        (root / 'state.json').write_text(json.dumps(state))
        (root / 'annotated_script.json').write_bytes(old)
        (root / 'chunks.json').write_bytes(b'old chunks')
        (root / 'chunks.json.status.jsonl').write_bytes(b'old journal')
        manifest = root / 'annotated_script.json.threepass_manifest.json'
        return root, source, old, manifest, script

    def test_substitutions_preserve_every_prior_byte(self):
        script = b'[{"text":"new"}]'
        valid = build_three_pass_completion_manifest(script, run_id='f' * 32)
        unchecked_script = json.dumps(
            [{'text': 'new', 'attribution_unchecked': ['fallback']}],
            ensure_ascii=False, indent=2).encode()
        substitutions = {
            'status_only': ({'status': 'publishing', 'run_id': 'f' * 32}, script),
            'string_unchecked': ({**valid,
                                  'passes': {'attribute': {'unchecked_entries': '2'}}}, script),
            'malformed_pass_mapping': ({**valid,
                                        'passes': {'attribute': 'not a pass record'}}, script),
            'stale_complete': (build_three_pass_completion_manifest(
                script, status='complete', run_id='e' * 32), script),
            'prior_receipt_old_script': ({**valid, 'publication': {
                'run_id': 'f' * 32,
                'script_sha256': hashlib.sha256(b'old script').hexdigest()}}, script),
            'unchecked_staged_row': (
                build_three_pass_completion_manifest(unchecked_script, run_id='f' * 32),
                unchecked_script),
            'wrong_declared_count': ({**valid, 'completion': {
                **valid['completion'], 'entries': 2}}, script),
            'empty_pass_list': ({**valid, 'passes': []}, script),
            'empty_pass_value': ({**valid, 'passes': {'attribute': []}}, script),
        }
        for label, (manifest, staged_bytes) in substitutions.items():
            with self.subTest(label=label), tempfile.TemporaryDirectory() as tmp:
                root, source, old, path, _ = self._setup_book(tmp, staged_bytes)
                path.write_text(json.dumps(manifest))
                before_state = (root / 'state.json').read_bytes()
                before_manifest = path.read_bytes()
                token = get_book_operation_token(tmp)
                with self.assertRaises(ValueError):
                    publish_generated_script(tmp, token, str(source),
                                             hashlib.sha256(b'source').hexdigest(),
                                             staged_bytes, expected_run_id='f' * 32,
                                             complete_manifest=True)
                self.assertEqual(old, (root / 'annotated_script.json').read_bytes())
                self.assertEqual(b'old chunks', (root / 'chunks.json').read_bytes())
                self.assertEqual(before_state, (root / 'state.json').read_bytes())
                self.assertEqual(before_manifest, path.read_bytes())
                self.assertFalse((root / BINDING_NAME).exists())

    def test_source_edit_at_commit_boundary_restores_script_chunks_and_state(self):
        """An edit after the last row validation but before the committed
        journal write rolls the whole publication back while rollback is still
        possible; exact old script, chunks, state and binding are restored."""
        from script_selection_binding import require_unchanged_source
        with tempfile.TemporaryDirectory() as tmp:
            root, source, old, manifest_path, staged = self._setup_book(tmp)
            write_three_pass_completion_manifest(manifest_path, staged, run_id='f' * 32)
            before_state = (root / 'state.json').read_bytes()
            before_manifest = manifest_path.read_bytes()
            selected = capture_generation_selection(tmp, str(source))

            def edit_at_commit():
                if (not (root / 'chunks.json').exists()
                        and not (root / 'chunks.json.status.jsonl').exists()):
                    source.write_bytes(b'edited during final cleanup')
                require_unchanged_source(str(source), selected['source_sha256'],
                                         owning_root=tmp)

            token = get_book_operation_token(tmp)
            with self.assertRaisesRegex(ValueError, 'changed during generation'):
                publish_generated_script(tmp, token, str(source),
                                         selected['source_sha256'], staged,
                                         expected_run_id='f' * 32,
                                         complete_manifest=True,
                                         before_replace=edit_at_commit)
            self.assertEqual(old, (root / 'annotated_script.json').read_bytes())
            self.assertEqual(b'old chunks', (root / 'chunks.json').read_bytes())
            self.assertEqual(b'old journal', (root / 'chunks.json.status.jsonl').read_bytes())
            self.assertEqual(before_state, (root / 'state.json').read_bytes())
            self.assertEqual(before_manifest, manifest_path.read_bytes())
            self.assertFalse((root / BINDING_NAME).exists())

    def test_source_edit_at_committed_journal_replace_restores_all_artifacts(self):
        import book_state_transaction as transactions
        from script_selection_binding import require_unchanged_source
        with tempfile.TemporaryDirectory() as tmp:
            root, source, old, manifest_path, staged = self._setup_book(tmp)
            write_three_pass_completion_manifest(manifest_path, staged, run_id='f' * 32)
            before_state = (root / 'state.json').read_bytes()
            before_manifest = manifest_path.read_bytes()
            selected = capture_generation_selection(tmp, str(source))
            boundary = []
            original = transactions._save_journal

            def inject(root_dir, data, **kwargs):
                callback = kwargs.get('on_publish_attempt')
                if data.get('phase') == 'committed' and callback is not None:
                    def edit_at_replace():
                        boundary.append(True)
                        source.write_bytes(b'edited at journal replace')
                        require_unchanged_source(str(source), selected['source_sha256'],
                                                 owning_root=tmp)
                    kwargs['on_publish_attempt'] = edit_at_replace
                return original(root_dir, data, **kwargs)

            token = get_book_operation_token(tmp)
            with patch.object(transactions, '_save_journal', side_effect=inject):
                with self.assertRaisesRegex(ValueError, 'changed during generation'):
                    publish_generated_script(
                        tmp, token, str(source), selected['source_sha256'], staged,
                        expected_run_id='f' * 32, complete_manifest=True,
                        before_replace=lambda: None)
            self.assertTrue(boundary)
            self.assertEqual(old, (root / 'annotated_script.json').read_bytes())
            self.assertEqual(b'old chunks', (root / 'chunks.json').read_bytes())
            self.assertEqual(b'old journal', (root / 'chunks.json.status.jsonl').read_bytes())
            self.assertEqual(before_state, (root / 'state.json').read_bytes())
            self.assertEqual(before_manifest, manifest_path.read_bytes())
            self.assertFalse((root / BINDING_NAME).exists())


class SavedBookOriginResolverTests(unittest.TestCase):
    def _bind(self, root, script_path, source_path, *, source_path_override=None):
        raw = script_path.read_bytes()
        binding = make_script_selection_binding(
            get_book_operation_token(str(root)),
            hashlib.sha256(source_path.read_bytes()).hexdigest(),
            raw, source_path=source_path_override or str(source_path))
        script_path.with_suffix('.selection_binding.json').write_text(json.dumps(binding))
        return binding

    def test_get_bound_source_path_empty_selected_skips_upload_json_and_matches_posix_library(self):
        from script_selection_binding import get_bound_source_path, _path_digest
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            uploads = root / 'uploads'
            uploads.mkdir()
            scripts = root / 'scripts'
            scripts.mkdir()
            book = uploads / 'book.txt'
            book.write_text('alpha beta')
            injected = uploads / 'injected.json'
            injected.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
            saved = scripts / 'saved.json'
            saved.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
            binding = self._bind(root, saved, book)
            with patch('core.DATA_DIR', str(root)):
                # L7: selected='' must not crash _path_digest('') before checking uploads.
                self.assertEqual(str(book), get_bound_source_path(
                    str(root), binding, active=False, selected=''))
                # B2: .json files inside uploads/ are never returned as bound sources.
                injected_binding = self._bind(root, saved, injected)
                with self.assertRaises(ValueError):
                    get_bound_source_path(str(root), injected_binding, active=False)

            # N9: library scan matches path.as_posix() as well as str(path).
            class _WindowsSpellingEntry:
                name = 'parent.json'
                def __str__(self):
                    return r'C:\data\scripts\parent.json'
                def as_posix(self):
                    return 'C:/data/scripts/parent.json'

            posix_binding = {**binding, 'source_path_sha256': _path_digest('C:/data/scripts/parent.json')}
            real_iterdir = Path.iterdir
            def fake_iterdir(path_self):
                if path_self == scripts:
                    return iter([_WindowsSpellingEntry()])
                return real_iterdir(path_self)
            with patch('core.DATA_DIR', str(root)), patch.object(Path, 'iterdir', fake_iterdir):
                self.assertEqual(r'C:\data\scripts\parent.json', get_bound_source_path(
                    str(root), posix_binding, active=False))

    def test_resolve_saved_book_origin_contract_optional_terminal_and_structured_errors(self):
        from script_selection_binding import (
            MAX_BINDING_BYTES, MAX_SOURCE_BINDING_HOPS,
            SavedSourceResolutionError, resolve_saved_book_origin)
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside_tmp:
            root = Path(tmp)
            uploads = root / 'uploads'
            uploads.mkdir()
            scripts = root / 'scripts'
            scripts.mkdir()
            book = uploads / 'book.txt'
            book.write_text('alpha beta')
            book_sha = hashlib.sha256(book.read_bytes()).hexdigest()

            first = scripts / 'first.json'
            first.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
            first_binding = self._bind(root, first, book)
            second = scripts / 'second.json'
            second.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
            self._bind(root, second, first)

            with patch('core.DATA_DIR', str(root)):
                origin = resolve_saved_book_origin(
                    str(root), str(second),
                    expected_script_sha256=hashlib.sha256(second.read_bytes()).hexdigest(),
                    require_terminal_file=True)
                self.assertEqual(str(book), origin['source_path'])
                self.assertEqual(book_sha, origin['source_sha256'])
                self.assertEqual(first_binding['source_path_sha256'], origin['source_path_sha256'])
                self.assertEqual(2, len(origin['hops']))
                self.assertEqual(str(second), origin['hops'][0]['script_path'])
                self.assertEqual(str(first), origin['hops'][1]['script_path'])

                # require_terminal_file=False succeeds across multiple hops even after upload removal.
                book.unlink()
                without_terminal = resolve_saved_book_origin(
                    str(root), str(second), require_terminal_file=False)
                self.assertIsNone(without_terminal['source_path'])
                self.assertEqual(book_sha, without_terminal['source_sha256'])
                self.assertEqual(first_binding['source_path_sha256'], without_terminal['source_path_sha256'])
                self.assertEqual(2, len(without_terminal['hops']))
                with self.assertRaises(SavedSourceResolutionError) as missing_err:
                    resolve_saved_book_origin(str(root), str(second), require_terminal_file=True)
                self.assertEqual('missing_source', missing_err.exception.code)
                book.write_text('alpha beta')

                # B2 B5: outside data_dir with corrupt binding returns outside_library without reading it.
                evil = Path(outside_tmp) / 'evil.json'
                evil.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
                (Path(outside_tmp) / 'evil.selection_binding.json').write_bytes(b'{corrupt')
                with self.assertRaises(SavedSourceResolutionError) as out_err:
                    resolve_saved_book_origin(str(root), str(evil))
                self.assertEqual('outside_library', out_err.exception.code)

                # Non-plain saved script path (e.g. symlink) is refused as outside_library.
                with patch('direct_path_admission.is_plain_path', side_effect=lambda p: str(p) != str(second)):
                    with self.assertRaises(SavedSourceResolutionError) as link_err:
                        resolve_saved_book_origin(str(root), str(second))
                self.assertEqual('outside_library', link_err.exception.code)

                # B2 B4: uploads/*.json target is refused as outside_library even when require_terminal_file=False.
                injected = uploads / 'injected.json'
                injected.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
                self._bind(root, injected, book)
                hop = scripts / 'hop.json'
                hop.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
                self._bind(root, hop, injected)
                for req_term in (True, False):
                    with self.assertRaises(SavedSourceResolutionError) as inj_err:
                        resolve_saved_book_origin(str(root), str(hop), require_terminal_file=req_term)
                    self.assertEqual('outside_library', inj_err.exception.code)

                # Missing binding, oversized binding, changed hop 0 digest, cycle, hop limit.
                unbound = scripts / 'unbound.json'
                unbound.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
                with self.assertRaises(SavedSourceResolutionError) as mb_err:
                    resolve_saved_book_origin(str(root), str(unbound))
                self.assertEqual('missing_binding', mb_err.exception.code)

                (scripts / 'unbound.selection_binding.json').write_bytes(b'x' * (MAX_BINDING_BYTES + 1))
                with self.assertRaises(SavedSourceResolutionError) as ov_err:
                    resolve_saved_book_origin(str(root), str(unbound))
                self.assertEqual('damaged_binding', ov_err.exception.code)

                with self.assertRaises(SavedSourceResolutionError) as ch_err:
                    resolve_saved_book_origin(str(root), str(second), expected_script_sha256='0' * 64)
                self.assertEqual('changed_source', ch_err.exception.code)

                loop = scripts / 'loop.json'
                loop.write_text(json.dumps([{'speaker': 'A', 'text': 'alpha beta'}]))
                self._bind(root, loop, loop)
                with self.assertRaises(SavedSourceResolutionError) as cyc_err:
                    resolve_saved_book_origin(str(root), str(loop))
                self.assertEqual('cycle', cyc_err.exception.code)

                chain = [scripts / f'chain_{i}.json' for i in range(MAX_SOURCE_BINDING_HOPS + 1)]
                for idx, node in enumerate(chain):
                    node.write_text(json.dumps([{'speaker': 'A', 'text': f'node {idx}'}]))
                for idx in range(len(chain) - 1, -1, -1):
                    target = book if idx == len(chain) - 1 else chain[idx + 1]
                    self._bind(root, chain[idx], target)
                # chain[1] -> ... -> chain[8] -> book is 8 hops (admitted); chain[0] is 9 hops (refused).
                self.assertEqual(str(book), resolve_saved_book_origin(str(root), str(chain[1]))['source_path'])
                with self.assertRaises(SavedSourceResolutionError) as hop_err:
                    resolve_saved_book_origin(str(root), str(chain[0]))
                self.assertEqual('hop_limit', hop_err.exception.code)
