import contextlib
import hashlib
import json
from pathlib import Path
import shlex
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import uuid
import benchmark_runner as runner
from benchmark_execution import BENCHMARK_STATE, BenchmarkCancelled


class RemoteTtsAssetCleanupTests(unittest.TestCase):
    @contextlib.contextmanager
    def owned_root(self):
        root = Path('/tmp') / ('alexandria-tts-benchmark-assets.' + uuid.uuid4().hex[:10])
        root.mkdir(mode=0o700)
        try: yield root
        finally: shutil.rmtree(root, ignore_errors=True)

    def test_worker_success_error_timeout_and_cancel_remove_only_owned_assets(self):
        native_run = subprocess.run
        for mode in ('success', 'error', 'timeout', 'cancel', 'bad-json'):
            with self.subTest(mode=mode), self.owned_root() as assets, tempfile.TemporaryDirectory() as tmp:
                foreign = Path(tmp) / 'foreign'; foreign.mkdir(); (foreign / 'keep').write_bytes(b'keep')
                (assets / 'reference.wav').write_bytes(b'owned')
                (assets / 'external').symlink_to(foreign, target_is_directory=True)
                calls = []
                def cleanup(command, **kwargs):
                    args = shlex.split(command[2]); calls.append(args)
                    self.assertEqual(['rm', '-rf', '--', str(assets)], args)
                    self.assertEqual(30, kwargs['timeout']); self.assertTrue(assets.exists())
                    return native_run(args, **kwargs)
                failure = {'error': RuntimeError('worker failed'), 'timeout': subprocess.TimeoutExpired('worker', 1),
                           'cancel': BenchmarkCancelled('cancel requested')}.get(mode)
                def worker(*args, **kwargs):
                    self.assertTrue(assets.exists())
                    if failure: raise failure
                    return SimpleNamespace(returncode=0, stdout='TTS_BENCHMARK_RESULT=' + ('invalid' if mode == 'bad-json' else '[]'), stderr='')
                state = {'cancel': mode == 'cancel', 'logs': []}; token = BENCHMARK_STATE.set(state)
                try:
                    with patch.object(runner, '_stage_remote_tts_assets', return_value={'fixtures': [], '_asset_root': str(assets)}), \
                            patch.object(runner, 'run_benchmark_subprocess', side_effect=worker), \
                            patch.object(runner.subprocess, 'run', side_effect=cleanup):
                        if mode == 'success':
                            self.assertEqual([], runner._run_tts_worker({}, 'remote', {'remote_root': '/repo', 'remote_python': '/python'}, '/local', '/output', 'fixture-host'))
                        else:
                            with self.assertRaises(type(failure) if failure else json.JSONDecodeError) as error:
                                runner._run_tts_worker({}, 'remote', {'remote_root': '/repo', 'remote_python': '/python'}, '/local', '/output', 'fixture-host')
                            if failure: self.assertIs(failure, error.exception)
                finally: BENCHMARK_STATE.reset(token)
                self.assertEqual(1, len(calls)); self.assertFalse(assets.exists())
                self.assertEqual(b'keep', (foreign / 'keep').read_bytes())

    def test_partial_transfer_failure_cleans_created_directory(self):
        native_run = subprocess.run
        with self.owned_root() as assets, tempfile.TemporaryDirectory() as tmp:
            reference = Path(tmp) / 'reference.wav'; reference.write_bytes(b'reference')
            payload = {'fixtures': [{'voice_type': 'clone', 'ref_audio': reference.name,
                                    'ref_audio_sha256': hashlib.sha256(b'reference').hexdigest()}]}
            def transfer(command, **kwargs):
                if command[0] == 'scp':
                    (assets / 'partial.wav').write_bytes(b'partial')
                    return SimpleNamespace(returncode=1, stdout='', stderr='transfer failed')
                return SimpleNamespace(returncode=0, stdout='banner\n' + str(assets) + '\n', stderr='')
            with patch.object(runner, 'run_benchmark_subprocess', side_effect=transfer), \
                    patch.object(runner.subprocess, 'run', side_effect=lambda command, **kw: native_run(shlex.split(command[2]), **kw)) as cleanup:
                with self.assertRaisesRegex(RuntimeError, 'transfer failed'):
                    runner._stage_remote_tts_assets(payload, tmp, 'fixture-host')
            cleanup.assert_called_once(); self.assertFalse(assets.exists())
            self.assertEqual(b'reference', reference.read_bytes())

    def test_cleanup_failure_is_visible_without_masking_primary_cancel_or_failure(self):
        path = '/tmp/alexandria-tts-benchmark-assets.abcdefghij'
        for failure in (None, RuntimeError('primary worker failed'), BenchmarkCancelled('primary cancel')):
            with self.subTest(failure=type(failure).__name__):
                state = {'cancel': True, 'logs': []}; token = BENCHMARK_STATE.set(state)
                try:
                    with patch.object(runner.subprocess, 'run', return_value=SimpleNamespace(returncode=1)), self.assertLogs('benchmark_runner', level='WARNING'):
                        if failure is None:
                            with self.assertRaisesRegex(RuntimeError, 'asset cleanup failed'):
                                runner.apply_remote_tts_asset_cleanup('fixture-host', path)
                        else:
                            try: raise failure
                            except BaseException:
                                runner.apply_remote_tts_asset_cleanup('fixture-host', path)
                    self.assertEqual(1, len(state['logs'])); self.assertIn(path, state['logs'][0])
                finally: BENCHMARK_STATE.reset(token)

    def test_unknown_roots_never_trigger_deletion(self):
        with patch.object(runner.subprocess, 'run') as cleanup:
            for path in ('/tmp', '/', '/tmp/alexandria-tts-benchmark-assets.*', '/tmp/alexandria-tts-benchmark-assets.abcdefghij/../other'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    runner.apply_remote_tts_asset_cleanup('fixture-host', path)
            cleanup.assert_not_called()
