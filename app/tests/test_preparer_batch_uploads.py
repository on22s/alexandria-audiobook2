from contextlib import ExitStack
import json
import core
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, BackgroundTasks, HTTPException
from fastapi.testclient import TestClient
from routers import preparer


class PreparerBatchUploadTests(unittest.TestCase):
    def setup_api(self, root, stack):
        uploads = root / 'uploads'
        outputs = root / 'outputs'
        uploads.mkdir()
        outputs.mkdir()
        state = {'batch_preparer': {'running': False, 'cancel': False, 'logs': []}}
        captures = []
        def stream(command, cwd, process, **kwargs):
            audio = Path(command[command.index('--audio') + 1])
            captures.append((audio.name, audio.read_bytes(), command))
            Path(command[command.index('--output') + 1]).write_bytes(b'CPU output fixture')
            return 0, []
        for name, value in (('UPLOADS_DIR', str(uploads)), ('PREPARER_OUTPUT_DIR', str(outputs)), ('process_state', state)):
            stack.enter_context(patch.object(preparer, name, value))
        stack.enter_context(patch.object(preparer, '_resolve_preparer_interpreter', return_value=sys.executable))
        stack.enter_context(patch.object(preparer, '_revalidate_voicelab_paths', return_value=None))
        stack.enter_context(patch.object(preparer, 'check_disk_space', return_value=(True, 50)))
        stack.enter_context(patch.object(preparer, 'check_global_gpu_lock'))
        stack.enter_context(patch.object(core, 'process_state', state))
        stack.enter_context(patch.object(core, '_task_claims', {}))
        stack.enter_context(patch.object(core, '_gpu_leases', {}))
        stack.enter_context(patch.object(core, 'acquire_gpu_lock', return_value=None))
        stack.enter_context(patch.object(core, 'llm_is_on_this_gpu', return_value=True))
        claim = stack.enter_context(patch.object(core, 'claim_gpu_task', wraps=core.claim_gpu_task))
        release = stack.enter_context(patch.object(preparer, 'release_gpu_task_claim', wraps=core.release_gpu_task_claim))
        stack.enter_context(patch.object(preparer, '_stream_subprocess_to_logs', side_effect=stream))
        stack.enter_context(patch.object(preparer, '_run_claimed_background_task', side_effect=lambda name, run: run()))
        api = FastAPI()
        api.add_middleware(core.TaskClaimMiddleware)
        api.include_router(preparer.router)
        client = stack.enter_context(TestClient(api, raise_server_exceptions=False))
        return client, uploads, captures, state, claim, release

    def config(self, names):
        return {'tasks': [{'audio_filename': name, 'output_filename': f'dataset_{i}.zip'}
                          for i, name in enumerate(names)], 'lang': 'English',
                'min_confidence': .85, 'min_snr': 25}

    def post(self, client, names, contents, config=None):
        return client.post('/api/preparer/batch/upload_start',
                           data={'config_json': json.dumps(config or self.config(names))},
                           files=[('audio_files', (name, data, 'audio/wav')) for name, data in zip(names, contents)])

    def test_http_uploads_actual_selected_bytes_before_sequential_dispatch(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            client, uploads, captures, state, claim, release = self.setup_api(Path(tmp), stack)
            (uploads / 'one.wav').write_bytes(b'old unrelated file')
            payloads = [b'local one bytes', b'local two bytes']
            with patch.object(preparer, '_save_upload_limited', wraps=preparer._save_upload_limited) as save:
                result = self.post(client, ['one.wav', 'two.wav'], payloads)
            self.assertEqual(200, result.status_code, result.text)
            self.assertEqual({'status': 'started', 'task_count': 2}, result.json())
            self.assertEqual([('one.wav', payloads[0]), ('two.wav', payloads[1])],
                             [(name, data) for name, data, _ in captures])
            self.assertTrue(all(call.args[2] == 20 * 1024**3 for call in save.call_args_list))
            self.assertEqual(['done', 'done'], [task['status'] for task in state['batch_preparer']['tasks']])
            claim.assert_called_once_with('batch_preparer')
            release.assert_not_called()
            self.assertEqual(['one.wav', 'two.wav'], sorted(path.name for path in uploads.iterdir()))
            # Existing JSON clients still use already-uploaded files through the same worker.
            captures.clear()
            result = client.post('/api/preparer/batch/start', json=self.config(['one.wav']))
            self.assertEqual(200, result.status_code, result.text)
            self.assertEqual([('one.wav', payloads[0])], [(name, data) for name, data, _ in captures])

    def test_mismatch_duplicate_and_invalid_config_reject_before_upload_or_claim(self):
        cases = [(['one.wav'], self.config(['other.wav'])),
                 (['one.wav'], self.config(['one.wav', 'two.wav'])),
                 (['same.wav', 'same.wav'], self.config(['same.wav', 'same.wav']))]
        for names, config in cases:
            with self.subTest(names=names, config=config), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                client, uploads, captures, state, claim, release = self.setup_api(Path(tmp), stack)
                with patch.object(preparer, '_save_upload_limited', wraps=preparer._save_upload_limited) as save:
                    result = self.post(client, names, [b'bytes'] * len(names), config)
                self.assertEqual(400, result.status_code, result.text)
                save.assert_not_called()
                claim.assert_not_called()
                self.assertEqual([], captures)
                self.assertEqual([], list(uploads.iterdir()))

    def test_upload_claim_or_scheduling_failure_preserves_old_bytes_and_cleans_staging(self):
        for failure in ('upload', 'claim', 'schedule'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                client, uploads, captures, state, claim, release = self.setup_api(Path(tmp), stack)
                original = {'one.wav': b'old one', 'two.wav': b'old two'}
                for name, data in original.items():
                    (uploads / name).write_bytes(data)
                if failure == 'upload':
                    real_save = preparer._save_upload_limited
                    async def save(upload, path, limit):
                        if upload.filename == 'two.wav':
                            Path(path).write_bytes(b'partial')
                            raise HTTPException(413, 'fixture upload exceeds limit')
                        await real_save(upload, path, limit)
                    stack.enter_context(patch.object(preparer, '_save_upload_limited', side_effect=save))
                elif failure == 'claim':
                    claim.side_effect = HTTPException(400, 'GPU task busy')
                else:
                    stack.enter_context(patch.object(BackgroundTasks, 'add_task', side_effect=RuntimeError('schedule failed')))
                result = self.post(client, ['one.wav', 'two.wav'], [b'new one', b'new two'])
                self.assertEqual({'upload': 413, 'claim': 400, 'schedule': 500}[failure], result.status_code, result.text)
                self.assertEqual(original, {path.name: path.read_bytes() for path in uploads.iterdir()})
                self.assertEqual([], captures)
                if failure in ('upload', 'schedule'):
                    self.assertEqual(1,release.call_count)
                    self.assertEqual('batch_preparer',release.call_args.args[0])
                    self.assertIsInstance(release.call_args.args[1],str)
                    self.assertEqual({'pending_only':True},release.call_args.kwargs)
                    self.assertFalse(core.is_task_running('batch_preparer'))
                    self.assertEqual({},core._task_claims)
                else:
                    release.assert_not_called()
