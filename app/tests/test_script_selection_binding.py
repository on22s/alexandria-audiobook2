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
                    b'[{"text":"first","text":"second"}]'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                make_script_selection_binding(self.token, self.source, raw,
                                              source_path='/source/a.txt')

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
            manifest.write_text('{"status":"publishing"}')
            token = get_book_operation_token(tmp)
            import book_state_transaction as transaction
            original = transaction._move
            def fail_on_binding(src, dest, **kwargs):
                if Path(dest).name == BINDING_NAME and 'new-' in str(src):
                    raise OSError('injected publication failure')
                return original(src, dest, **kwargs)
            with patch.object(transaction, '_move', side_effect=fail_on_binding):
                with self.assertRaises(OSError):
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
