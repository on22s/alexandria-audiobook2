"""Native HTTP admission and private child artifacts for book preflight."""
from contextlib import ExitStack
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from fastapi import FastAPI
from fastapi.testclient import TestClient
from routers import script
import core

class BookPreflightApiTests(unittest.TestCase):
    def _assert_source_refused(self, tmp, detail):
        app = FastAPI(); app.include_router(script.router)
        with patch.object(script, 'DATA_DIR', tmp), patch.object(script, 'CONFIG_PATH', str(Path(tmp, 'config.json'))), \
                patch.object(script, 'get_active_book_id', return_value='fixture'), \
                patch.object(script, 'schedule_claimed_background_task', return_value='owned-claim') as schedule:
            response = TestClient(app).post('/api/generate_script/preflight', json={})
        self.assertEqual(400, response.status_code, response.text)
        self.assertIn(detail, response.text)
        schedule.assert_not_called()
        self.assertEqual([], list(Path(tmp).glob('script_preflight_*')))
        self.assertEqual(b'prior receipt', Path(tmp, 'script_preflight.json').read_bytes())

    def test_source_cap_refuses_before_claim_and_cleans_private_copy(self):
        self.assertEqual(512 * 1024**2, script.MAX_SCRIPT_UPLOAD_BYTES)
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, 'source.txt'); source.write_bytes(b'x' * 17)
            Path(tmp, 'state.json').write_text(json.dumps({'input_file_path': str(source)}))
            Path(tmp, 'script_preflight.json').write_bytes(b'prior receipt')
            # Exercise the real bounded read and refusal with a scaled limit.
            with patch.object(script, 'MAX_SCRIPT_UPLOAD_BYTES', 16):
                self._assert_source_refused(tmp, 'Source exceeds')

    def test_source_changed_during_copy_refuses_without_claim_or_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp, 'source.txt'); source.write_bytes(b'original')
            Path(tmp, 'state.json').write_text(json.dumps({'input_file_path': str(source)}))
            Path(tmp, 'script_preflight.json').write_bytes(b'prior receipt')
            native_stat = script.os.fstat
            inode = source.stat().st_ino
            changed = []
            def stat_and_change(fd):
                result = native_stat(fd)
                if result.st_ino == inode and not changed:
                    changed.append(True)
                    source.write_bytes(b'changed while the source handle is open')
                return result
            with patch.object(script.os, 'fstat', side_effect=stat_and_change):
                self._assert_source_refused(tmp, 'Source changed')
            self.assertEqual([True], changed)

    def test_bad_receipts_cannot_be_reported_complete_by_the_http_worker(self):
        for kind in ('summary-status', 'sample-status', 'native-status', 'failed-sample'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp); source = root / 'source.txt'; source.write_text('Original source.')
                (root / 'config.json').write_text('{}')
                (root / 'state.json').write_text(json.dumps({'input_file_path': str(source)}))
                active = root / 'annotated_script.json'; active.write_bytes(b'active book')
                captured = {}
                def schedule(tasks, name, callback, task, worker):
                    captured['worker'] = worker
                    return 'owned-claim'
                for name, value in [('DATA_DIR', tmp), ('CONFIG_PATH', str(root / 'config.json')),
                                    ('get_active_book_id', lambda: 'fixture'),
                                    ('schedule_claimed_background_task', schedule)]:
                    stack.enter_context(patch.object(script, name, value))
                app = FastAPI(); app.include_router(script.router); client = TestClient(app)
                started = client.post('/api/generate_script/preflight', json={})
                self.assertEqual(200, started.status_code, started.text)
                def stream(command, cwd, state, env):
                    output = command[command.index('--output') + 1]
                    sample_status = 'invalid' if kind == 'sample-status' else ('failed' if kind == 'failed-sample' else 'complete')
                    native_status = 'running' if kind == 'native-status' else sample_status
                    sample = {'label': 'first', 'chunk_index': 0, 'status': sample_status,
                              'planned_calls': {}, 'failure_codes': {}}
                    script.atomic_json_write({'status': native_status}, script.three_pass_manifest_path(output + '.preflight_first.json'))
                    summary_status = 'invalid' if kind == 'summary-status' else ('failed' if kind == 'sample-status' else 'complete')
                    script.atomic_json_write({'status': summary_status,
                                              'samples': [sample], 'planned_calls': {}}, output + '.preflight_manifest.json')
                    captured['private'] = Path(env['ALEXANDRIA_DATA_DIR'])
                    return 0, []
                with patch.object(script, '_stream_subprocess_to_logs', side_effect=stream), \
                        patch.dict(script.process_state, {'script': {'logs': [], 'cancel': False}}):
                    captured['worker']()
                final = client.get('/api/generate_script/preflight/' + started.json()['job_id']).json()
                self.assertEqual('failed', final['status'])
                self.assertTrue(final.get('error'), final)
                self.assertFalse(captured['private'].exists())
                self.assertEqual(b'active book', active.read_bytes())

    def test_http_freezes_source_and_config_without_touching_full_book(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            source = root / 'book.txt'; source.write_text('Original source.')
            config = root / 'config.json'; config.write_text('{"llm_local":{"model_name":"fixture"}}')
            (root / 'state.json').write_text(json.dumps({'input_file_path': str(source)}))
            active = [root / name for name in ('annotated_script.json', 'chunks.json', 'voice_config.json', 'annotated_script.json.threepass_checkpoint.json')]
            for path in active:
                path.write_bytes(b'full book sentinel')
            captured = {}
            def schedule(tasks, name, callback, task, worker):
                captured.update(worker=worker, task=task)
                return 'owned-claim'
            for name, value in [('DATA_DIR', tmp), ('CONFIG_PATH', str(config)),
                                ('get_active_book_id', lambda: 'book-id'),
                                ('schedule_claimed_background_task', schedule)]:
                stack.enter_context(patch.object(script, name, value))
            app = FastAPI(); app.include_router(script.router)
            client = TestClient(app)
            result = client.post('/api/generate_script/preflight', json={'strip_front_matter': False, 'first_person_narrator': 'ALICE'})
            self.assertEqual(200, result.status_code, result.text)
            receipt = result.json()
            self.assertEqual('running', receipt['status'])
            source.write_text('Changed after admission.')
            config.write_text('{"llm_local":{"model_name":"changed"}}')
            def stream(command, cwd, state, env):
                self.assertEqual('Original source.', Path(command[3]).read_text())
                self.assertIn('--no-strip-front-matter', command)
                self.assertIn('--first-person-narrator', command)
                self.assertIn('--preflight', command)
                private = Path(env['ALEXANDRIA_DATA_DIR'])
                self.assertEqual('fixture', json.loads((private / 'config.json').read_text())['llm_local']['model_name'])
                output = command[command.index('--output') + 1]
                plan = {'1': 1, '2': 1, '3': 1}
                sample = {'label': 'first', 'chunk_index': 0, 'status': 'complete', 'planned_calls': plan, 'failure_codes': {}}
                script.atomic_json_write({'status': 'complete'}, script.three_pass_manifest_path(output + '.preflight_first.json'))
                script.atomic_json_write({'status': 'complete', 'samples': [sample], 'planned_calls': plan}, output + '.preflight_manifest.json')
                captured['private'] = private
                return 0, []
            with patch.object(script, '_stream_subprocess_to_logs', side_effect=stream), patch.dict(script.process_state, {'script': {'logs': [], 'cancel': False}}):
                captured['worker']()
            final = client.get('/api/generate_script/preflight/' + receipt['job_id'])
            self.assertEqual('complete', final.json()['status'])
            self.assertFalse(final.json()['source_is_current'])
            self.assertNotIn('_source_path', final.json())
            self.assertEqual('owned-claim', final.json()['claim_id'])
            self.assertFalse(captured['private'].exists())
            for path in active:
                self.assertEqual(b'full book sentinel', path.read_bytes())
            self.assertEqual(409, client.get('/api/generate_script/preflight/' + 'f' * 32).status_code)

    def test_interrupted_workspace_limit_refuses_without_deleting_files(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(script, 'DATA_DIR', tmp):
            for number in range(4):
                path = Path(tmp, 'script_preflight_old_' + str(number))
                path.mkdir()
                (path / 'source.txt').write_text('preserve interrupted evidence')
            app = FastAPI(); app.include_router(script.router)
            response = TestClient(app).post('/api/generate_script/preflight', json={})
            self.assertEqual(409, response.status_code)
            self.assertIn('workspace limit', response.text)
            self.assertEqual(4, len(list(Path(tmp).glob('script_preflight_*'))))
            for path in Path(tmp).glob('script_preflight_*/source.txt'):
                self.assertEqual('preserve interrupted evidence', path.read_text())

    def test_missing_native_manifest_cannot_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            output = str(Path(tmp, 'sample.json'))
            script.atomic_json_write({'status': 'complete', 'planned_calls': {}, 'samples': [
                {'label': 'first', 'chunk_index': 0, 'status': 'complete', 'planned_calls': {}, 'failure_codes': {}}]}, output + '.preflight_manifest.json')
            with self.assertRaisesRegex(ValueError, 'native run manifest'):
                script.get_completed_book_preflight_summary(output)

    def test_cancel_refuses_another_script_owner(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(script, 'DATA_DIR', tmp):
            job = 'a' * 32
            script.atomic_json_write({'job_id': job, 'claim_id': 'old'}, str(Path(tmp, 'script_preflight.json')))
            app = FastAPI(); app.include_router(script.router)
            with patch.dict(core._task_claims, {'script': {'id': 'new'}}, clear=True), patch.dict(script.process_state, {'script': {'cancel': False}}):
                self.assertEqual(409, TestClient(app).post('/api/generate_script/preflight/' + job + '/cancel').status_code)
                self.assertFalse(script.process_state['script']['cancel'])
