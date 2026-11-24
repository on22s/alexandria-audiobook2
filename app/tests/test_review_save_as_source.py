"""Exercise saved-source ownership through Save As and the real review entry points."""
from contextlib import ExitStack
import hashlib
from itertools import product
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import core
from book_state_transaction import get_book_operation_token
from completed_review_receipt import get_review_receipt_path
from routers import script as routes, scripts_library as library
from script_selection_binding import _read_source_snapshot, make_script_selection_binding, updated_saved_binding
from tests import test_review_source_spans as spans
from tests import test_review_scoped_source_span as scoped


RAW = 'Rain fell.\n\nOpen the gate!'
ROWS = [spans.row(RAW, 'NARRATOR', spoken=False, source_span=[0, len(RAW)])]
REPLY = [spans.row('Rain fell.', 'NARRATOR', spoken=False),
         spans.row('Open the gate!', 'MIRA', spoken=True)]


class ReviewSaveAsSourceTests(unittest.TestCase):
    def workspace(self, root, stack, *, custom, raw=RAW, rows=ROWS):
        app_dir = root / 'app'
        data = root / 'data' if custom else root
        upload = (data if custom else app_dir) / 'uploads' / 'fixture.txt'
        script, _, _ = spans.create_book(data, raw=raw.replace('\n', '\r\n'),
                                         source=rows, upload_path=str(upload))
        (data / 'scripts').mkdir()
        (data / 'config.json').write_text('{}')
        for name, value in {'BASE_DIR': app_dir, 'ROOT_DIR': root, 'DATA_DIR': data,
                            'RUN_HISTORY_DIR': data / 'runs'}.items():
            stack.enter_context(patch.object(core, name, str(value)))
        for module in (library, routes):
            for name, value in {'DATA_DIR': data, 'SCRIPTS_DIR': data / 'scripts',
                                'SCRIPT_PATH': script, 'VOICE_CONFIG_PATH': data / 'voices.json'}.items():
                stack.enter_context(patch.object(module, name, str(value)))
        for name, value in {'CHUNKS_PATH': data / 'chunks.json', 'AUDIOBOOK_PATH': data / 'book.mp3',
                            'M4B_PATH': data / 'book.m4b'}.items():
            stack.enter_context(patch.object(library, name, str(value)))
        for name, value in {'REPORTS_DIR': data / 'reports', 'ROOT_DIR': root,
                            'CONFIG_PATH': data / 'config.json'}.items():
            stack.enter_context(patch.object(routes, name, str(value)))
        state = {name: {'running': False, 'cancel': False, 'logs': []}
                 for name in ('review', 'batch_review')}
        for module in (core, routes, library):
            stack.enter_context(patch.object(module, 'process_state', state))
        for name in ('_task_claims', '_gpu_leases', '_saved_book_publications'):
            stack.enter_context(patch.object(core, name, {}))
        stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=False))
        stack.enter_context(patch.object(routes, '_task_log_path', return_value=str(data / 'task.log')))
        stack.enter_context(patch.object(routes, '_init_task_log', return_value=str(data / 'task.log')))
        stack.enter_context(patch('review_startup.get_failure_log_path', return_value=data / 'diagnostics.jsonl'))
        stack.enter_context(patch('review_startup.emit_diagnostic_log'))
        app = FastAPI()
        app.include_router(library.router)
        app.include_router(routes.router)
        return data, script, upload, state, stack.enter_context(TestClient(app))

    def library_request(self, api, action, name):
        response = api.post('/api/scripts/' + action, json={'name': name})
        self.assertEqual(200, response.status_code, response.text)

    def save_copy(self, api, count):
        self.library_request(api, 'save', 'original')
        name = 'original'
        for index in range(count):
            self.library_request(api, 'load', name)
            name = f'copy-{index + 1}'
            self.library_request(api, 'save', name)
        return name

    def run_entry_point(self, api, data, mode, name, output, reply=REPLY):
        client, calls = spans.client_for([(reply, 'stop')] * 3)
        logs = []

        def run_cli(command):
            index = command.index('--context-window')
            self.assertEqual('18', command[index + 1])
            with spans.ReviewSourceSpanTests().cli(str(data), client) as log:
                with patch.object(spans.review.sys, 'argv', command[2:]):
                    try:
                        spans.review.main()
                    finally:
                        logs.extend(log.getvalue().splitlines())
            return 0

        def stream(command, _cwd, _state, **_kwargs):
            return run_cli(command), logs

        if mode == 'direct':
            run_cli(['python', '-u', 'review_script.py', '--input', str(output),
                     '--context-window', '18'])
        elif mode == 'loaded':
            with patch.object(routes, 'run_process', side_effect=lambda command, _task: run_cli(command)):
                response = api.post('/api/review_script_contextual', json={
                    'window_size': 18, 'dedupe_speakers': False})
            self.assertEqual(200, response.status_code, response.text)
        else:
            with patch.object(routes, '_stream_subprocess_to_logs', side_effect=stream):
                response = api.post('/api/review_script/batch/start', json={
                    'script_names': [name], 'context_window': 18, 'dedupe_speakers': False})
            self.assertEqual(200, response.status_code, response.text)
        return calls, '\n'.join(logs)

    def test_contextual_direct_and_batch_review_bound_copies_in_both_layouts(self):
        cases = [('loaded', 0), *product(('loaded', 'direct', 'batch'), (1, 2))]
        for custom, (mode, count) in product((False, True), cases):
            with self.subTest(custom=custom, mode=mode, count=count), TemporaryDirectory() as tmp, ExitStack() as stack:
                data, active, upload, state, api = self.workspace(Path(tmp), stack, custom=custom)
                upload_before = upload.read_bytes()
                name = self.save_copy(api, count)
                scripts = data / 'scripts'
                saved = scripts / f'{name}.json'
                if mode == 'loaded':
                    self.library_request(api, 'load', name)
                    output = active
                else:
                    (scripts / 'other.json').write_text(json.dumps([spans.row('A different book.')]))
                    self.library_request(api, 'load', 'other')
                    output = saved
                ancestors = {path: path.read_bytes() for path in scripts.glob('*.json')
                             if path.stem != name and path.stem != name + '.selection_binding'}
                calls, log = self.run_entry_point(api, data, mode, name, output)
                self.assertTrue(Path(get_review_receipt_path(str(output))).is_file(), log)
                self.assertEqual(1, len(calls), log)
                result = json.loads(output.read_bytes())
                self.assertEqual([[0, 10], [12, len(RAW)]], [row['source_span'] for row in result])
                self.assertEqual(['NARRATOR', 'MIRA'], [row['speaker'] for row in result])
                self.assertEqual(upload_before, upload.read_bytes())
                for path, original in ancestors.items():
                    self.assertEqual(original, path.read_bytes(), path.name)
                if mode == 'batch':
                    self.assertEqual('done', state['batch_review']['tasks'][0]['status'], log)

    def test_copy_rejects_missing_renamed_or_changed_evidence_and_modified_replies(self):
        for damage in ('missing-parent', 'renamed-parent', 'changed-parent', 'missing-upload',
                       'changed-upload', 'edited-reply', 'reordered-reply'):
            with self.subTest(damage=damage), TemporaryDirectory() as tmp, ExitStack() as stack:
                data, active, upload, _, api = self.workspace(Path(tmp), stack, custom=True)
                name = self.save_copy(api, 2)
                self.library_request(api, 'load', name)
                parent = data / 'scripts' / 'original.json'
                reply = REPLY
                if damage == 'missing-parent':
                    parent.unlink()
                elif damage == 'renamed-parent':
                    parent.rename(parent.with_name('renamed.json'))
                elif damage == 'changed-parent':
                    parent.write_bytes(parent.read_bytes() + b'\n')
                elif damage == 'missing-upload':
                    upload.unlink()
                elif damage == 'changed-upload':
                    upload.write_bytes(upload.read_bytes() + b'\r\n')
                elif damage == 'edited-reply':
                    reply = [REPLY[0], {**REPLY[1], 'text': 'Close the gate!'}]
                else:
                    reply = REPLY[::-1]
                before = active.read_bytes()
                if damage.endswith('-reply'):
                    self.assertIsNotNone(spans.review.get_review_source_snapshot(
                        str(data), str(active), before, ROWS))
                calls, log = self.run_entry_point(api, data, 'loaded', name, active, reply)
                self.assertEqual(3, len(calls), log)
                self.assertEqual(before, active.read_bytes())
                self.assertFalse(Path(get_review_receipt_path(str(active))).exists(), log)
                if not damage.endswith('-reply'):
                    self.assertIn('reason=source_unavailable', log)

    def bind(self, data, script, source):
        raw = script.read_bytes()
        binding = make_script_selection_binding(get_book_operation_token(str(data)),
            hashlib.sha256(source.read_bytes()).hexdigest(), raw, source_path=str(source))
        script.with_suffix('.selection_binding.json').write_text(json.dumps(binding))

    def test_cycles_and_excessive_saved_ancestry_end_without_reading_more_links(self):
        for cycle in (False, True):
            with self.subTest(cycle=cycle), TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp)
                _, upload, _ = spans.create_book(root, saved=True, raw=RAW, source=ROWS)
                scripts = root / 'scripts'
                chain = [scripts / f'chain-{i}.json' for i in range(12)]
                for path in chain:
                    path.write_text(json.dumps(ROWS))
                for path, parent in zip(chain, chain[1:] + [upload]):
                    self.bind(root, path, parent)
                if cycle:
                    self.bind(root, chain[2], chain[1])
                diagnostics = stack.enter_context(patch.object(spans.review, 'record_failure'))
                reads = stack.enter_context(patch('script_selection_binding._read_source_snapshot',
                                                 wraps=_read_source_snapshot))
                self.assertIsNone(spans.review.get_review_source_snapshot(
                    str(root), str(chain[0]), chain[0].read_bytes(), ROWS))
                self.assertIn('cycle' if cycle else 'hop limit', str(diagnostics.call_args_list[0].args[1]))
                limit = 2 if cycle else spans.review.MAX_REVIEW_SAVED_SOURCE_HOPS
                self.assertEqual([str(path) for path in chain[1:limit + 1]],
                                 [read.args[0] for read in reads.call_args_list])

    def test_multiple_saved_hops_cannot_drop_an_ambiguous_parent_witness(self):
        raw = 'Manifesto.\nStone bell  rang.'
        variant = 'Manifesto.\nStone bell rang.'
        parent_rows = [spans.row('Manifesto.', 'NARRATOR', source_span=[0, 10]),
                       spans.row('Stone bell rang.', 'NARRATOR')]
        child_rows = [parent_rows[0], {**parent_rows[1], 'source_span': [11, 27]}]
        with TemporaryDirectory() as tmp:
            root = Path(tmp)
            original, _, _ = spans.create_book(root, saved=True, raw=raw, source=parent_rows)
            copy = original.with_name('copy.json')
            copy.write_bytes(original.read_bytes())
            self.bind(root, copy, original)
            child = copy.with_name('child.json')
            child.write_text(json.dumps(child_rows))
            self.bind(root, child, copy)
            with patch('generate_script.get_preprocessed_source', return_value=(raw, {})), \
                 patch('three_pass_generate.prepare_source_text', return_value=(variant, {})), \
                 patch.object(spans.review, 'record_failure'):
                snapshot = spans.review.get_review_source_snapshot(str(root), str(copy),
                    copy.read_bytes(), parent_rows)
                self.assertIsNotNone(snapshot)
                self.assertEqual({raw, variant}, snapshot['candidates'])
                with self.assertRaises(spans.review.ReviewSourceContradiction):
                    spans.review.get_review_source_snapshot(str(root), str(child),
                        child.read_bytes(), child_rows)

    def test_repeated_save_as_preserves_scoped_cut_and_refuses_a_child_span_overrun(self):
        raw = scoped.RAW
        entries, _ = scoped.make_scoped_book()
        with TemporaryDirectory() as tmp, ExitStack() as stack:
            data, active, _, _, api = self.workspace(Path(tmp), stack, custom=True, raw=raw, rows=entries)
            self.assertIsNotNone(scoped.add_scope_evidence(
                active, raw, spans.mark_entries(scoped.RAW_ROWS, raw), entries))
            name = self.save_copy(api, 2)
            self.library_request(api, 'load', name)
            before = active.read_bytes()
            snapshot = spans.review.get_review_source_snapshot(str(data), str(active), before, entries)
            self.assertIsNotNone(snapshot)
            self.assertEqual(raw, snapshot['text'])
            self.assertEqual(['copy-2.json', 'copy-1.json', 'original.json', 'fixture.txt'],
                             [Path(source['source_path']).name for source in snapshot['identity']['sources']])
            changed = json.loads(before)
            changed[-1]['source_span'][0] -= 1  # Add adjacent whitespace to the frozen span.
            updated = json.dumps(changed).encode()
            binding = active.with_suffix('.selection_binding.json')
            binding.write_bytes(updated_saved_binding(binding.read_bytes(), before, updated))
            active.write_bytes(updated)
            with self.assertRaises(spans.review.ReviewSourceContradiction):
                spans.review.get_review_source_snapshot(str(data), str(active), updated, changed)

    def test_review_preserves_pre_review_key_order_on_every_entry_point(self):
        rows = [{'text': RAW, 'speaker': 'NARRATOR', 'speaker_gender': 'male',
                 'speaker_age_group': 'adult', 'speaker_ageless': False,
                 'instruct': 'Plain narration.', 'spoken': False,
                 'source_span': [0, len(RAW)]}]
        expected = list(rows[0])
        paths = (('loaded', 0), ('loaded', 2), ('direct', 2), ('batch', 2))
        for custom, (mode, count) in product((False, True), paths):
            with self.subTest(custom=custom, mode=mode, count=count), \
                    TemporaryDirectory() as tmp, ExitStack() as stack:
                data, active, _, _, api = self.workspace(
                    Path(tmp), stack, custom=custom, rows=rows)
                name = self.save_copy(api, count)
                saved = data / 'scripts' / f'{name}.json'
                if mode == 'loaded':
                    self.library_request(api, 'load', name)
                    output = active
                else:
                    (data / 'scripts' / 'other.json').write_text(
                        json.dumps([{'text': 'A different book.'}]))
                    self.library_request(api, 'load', 'other')
                    output = saved
                calls, log = self.run_entry_point(api, data, mode, name, output)
                result = json.loads(output.read_bytes())
                self.assertEqual(2, len(result), log)
                for index, row in enumerate(result):
                    # The second row is relabelled MIRA, so its stale speaker
                    # traits are cleared; the surviving keys keep their order.
                    want = expected if index == 0 else [
                        key for key in expected if key not in
                        ('speaker_gender', 'speaker_age_group', 'speaker_ageless')]
                    self.assertEqual(want, list(row), log)

    def test_publication_refuses_when_a_deep_parent_binding_changes_during_review(self):
        with TemporaryDirectory() as tmp, ExitStack() as stack:
            data, active, _, _, api = self.workspace(Path(tmp), stack, custom=True)
            name = self.save_copy(api, 2)
            self.library_request(api, 'load', name)
            before = active.read_bytes()
            parent_binding = data / 'scripts' / 'original.selection_binding.json'
            client, calls = spans.client_for([(REPLY, 'stop')])
            complete = client.chat.completions.create

            def change_parent_binding(**kwargs):
                parent_binding.write_bytes(parent_binding.read_bytes() + b'\n')
                return complete(**kwargs)

            client.chat.completions.create = change_parent_binding
            with spans.ReviewSourceSpanTests().cli(str(data), client), \
                    self.assertRaisesRegex(ValueError, 'source identity changed during review'):
                spans.review.main()
            self.assertEqual(1, len(calls))
            self.assertEqual(before, active.read_bytes())
            self.assertFalse(Path(get_review_receipt_path(str(active))).exists())
