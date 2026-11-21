"""Real kernel leases around corpus inference; CPU provider stand-ins only."""
import copy
from contextlib import ExitStack
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi import HTTPException
import core
import integration_runner as runner
from experiments.gpu_guard import acquire_gpu_lock, gpu_is_busy
from tests import test_integration_corpus as fixtures


class CorpusOwnershipTests(fixtures.OwnedCorpusFixture):
    def get_fixture(self, root, stack):
        lock = root/'gpu.lock'
        text = 'one two three four five'
        manifest = {'books': [{'name': 'book.txt', 'passages': [{
            'category': 'opening', 'text': text, 'sha256': 'fixture'}]}]}
        config = {'llm_mode': 'local', 'llm_local': {'base_url': 'http://localhost:1234/v1', 'model_name': 'fixture'}}
        stack.enter_context(patch.object(core, 'acquire_gpu_lock', side_effect=lambda: acquire_gpu_lock(str(lock))))
        stack.enter_context(patch.object(runner, 'load_app_config', return_value=config))
        heal = stack.enter_context(patch.object(runner, 'ensure_ideal_settings', return_value=(False, {}, 'ready')))
        adapter = stack.enter_context(patch.object(runner, 'check_adapter', return_value=(True, 'verified')))
        provider = stack.enter_context(patch.object(runner, 'make_llm_client'))
        process = stack.enter_context(patch.object(runner, 'process_chunk', return_value=[{
            'speaker': 'NARRATOR', 'text': text, 'instruct': 'neutral'}]))
        return lock, manifest, config, heal, adapter, provider, process

    def test_foreign_kernel_lease_and_active_audio_refuse_before_mutation(self):
        for held in ('audio', 'kernel'):
            with self.subTest(held=held), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp)
                lock, manifest, config, heal, adapter, provider, process = self.get_fixture(root, stack)
                output = root/'report.json';output.write_bytes(b'prior report')
                holder = None
                try:
                    if held == 'audio':
                        core.process_state['audio']['running'] = True
                    else:
                        code = ("import sys,time;sys.path.insert(0," + repr(str(Path(__file__).resolve().parents[2])) + ");"
                                "from alexandria_file_lock import acquire_exclusive_file_lock;"
                                "h=open(sys.argv[1],'a');acquire_exclusive_file_lock(h.fileno());"
                                "print('held',flush=True);time.sleep(30)")
                        holder = subprocess.Popen([sys.executable, '-c', code, str(lock)],stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
                        ready = holder.stdout.readline().strip()
                        if ready != 'held':
                            self.fail('Native lock child failed: ' + holder.communicate(timeout=3)[1])
                    with self.assertRaises(HTTPException) as raised:
                        runner.run_manifest(manifest,str(output))
                    self.assertEqual(400,raised.exception.status_code)
                    for method in (heal,adapter,provider,process):
                        method.assert_not_called()
                    self.assertEqual(b'prior report',output.read_bytes())
                    self.assertFalse(core.process_state['script']['running'])
                finally:
                    core.process_state['audio']['running'] = False
                    if holder is not None:
                        holder.terminate();holder.wait(timeout=3);holder.stdout.close();holder.stderr.close()
                        # On Windows the child's descriptor can outlive wait() by a
                        # moment; rmtree then hits WinError 32 on gpu.lock. Poll the
                        # kernel lock (not the file's existence) so cleanup starts
                        # only once the handle is actually gone.
                        deadline = time.monotonic() + 2
                        while gpu_is_busy(str(lock)) and time.monotonic() < deadline:
                            time.sleep(0.01)
                        time.sleep(0.05)

    def test_lease_covers_self_heal_inference_and_artifact_publication(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            lock, manifest, config, heal, adapter, provider, process = self.get_fixture(root, stack)
            source = copy.deepcopy(manifest);output = root/'report.json'
            def assert_owned():
                self.assertTrue(core.is_task_running('script'))
                self.assertTrue(gpu_is_busy(str(lock)))
                with self.assertRaises(HTTPException):
                    core.claim_gpu_task('audio')
            def self_heal(*args,**kwargs):
                assert_owned();return False,{},'ready'
            heal.side_effect = self_heal
            def infer(*args,**kwargs):
                assert_owned()
                return [{'speaker': 'NARRATOR', 'text': 'one two three four five', 'instruct': 'neutral'}]
            process.side_effect = infer
            real_write = runner.atomic_json_write
            def publish(report, path, **kwargs):
                assert_owned();return real_write(report, path, **kwargs)
            with patch.object(runner,'atomic_json_write',side_effect=publish):
                report = runner.run_manifest(manifest,str(output))
            self.assertEqual(1,report['summary']['passed'])
            self.assertEqual(report,json.loads(output.read_text()))
            self.assertEqual(source,manifest)
            self.assertFalse(core.is_task_running('script'))
            self.assertFalse(gpu_is_busy(str(lock)))
            self.assertNotIn('script',core._task_claims)

    def test_failure_at_each_stage_releases_exact_owned_lease(self):
        for stage in ('heal','adapter','provider','process','write','interrupt'):
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp)
                lock, manifest, config, heal, adapter, provider, process = self.get_fixture(root, stack)
                output = root/'report.json';output.write_bytes(b'prior report')
                error = KeyboardInterrupt('owned fixture') if stage=='interrupt' else OSError('owned fixture')
                if stage in ('heal','interrupt'):
                    heal.side_effect = error
                elif stage=='adapter':
                    adapter.side_effect = error
                elif stage=='provider':
                    provider.side_effect = error
                elif stage=='process':
                    process.side_effect = error
                else:
                    stack.enter_context(patch.object(runner,'atomic_json_write',side_effect=error))
                with self.assertRaisesRegex(type(error),'owned fixture'):
                    runner.run_manifest(manifest,str(output))
                self.assertFalse(core.is_task_running('script'))
                self.assertFalse(gpu_is_busy(str(lock)))
                self.assertNotIn('script',core._task_claims)
                self.assertEqual(b'prior report',output.read_bytes())

    def test_optional_adapter_miss_report_sanitizes_url_and_retains_lease(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            lock = root / 'gpu.lock'
            text = 'one two three four five'
            manifest = {'books': [{'name': 'book.txt', 'passages': [{
                'category': 'opening', 'text': text, 'sha256': 'fixture'}]}]}
            url = 'https://fixture-user:fixture-password@host.example/v1'
            config = {'llm_mode': 'local', 'llm_local': {
                'base_url': url, 'model_name': 'fixture',
                'attribution_adapter': {'path': '/optional.gguf', 'scale': 1}}}
            stack.enter_context(patch.object(core, 'acquire_gpu_lock',
                side_effect=lambda: acquire_gpu_lock(str(lock))))
            stack.enter_context(patch.object(runner, 'load_app_config', return_value=config))
            stack.enter_context(patch.object(runner, 'ensure_ideal_settings',
                return_value=(False, {}, 'ready')))
            stack.enter_context(patch('attribution_adapter.served_adapters', return_value=None))
            stack.enter_context(patch.object(runner, 'make_llm_client'))
            stack.enter_context(patch.object(runner, 'process_chunk', return_value=[{
                'speaker': 'NARRATOR', 'text': text, 'instruct': 'neutral'}]))
            output = root / 'report.json'
            report = runner.run_manifest(manifest, str(output))
            body = output.read_text()
            self.assertFalse(gpu_is_busy(str(lock)))
            self.assertNotIn('script', core._task_claims)
            self.assertNotIn('fixture-password', body)
            self.assertNotIn('fixture-user', body)
            self.assertIn('[REDACTED]', report['attribution_adapter']['message'])
            self.assertFalse(report['attribution_adapter']['verified'])
            self.assertEqual(1, report['summary']['passed'])
            self.assertEqual(report, json.loads(body))

    def test_query_token_and_fragment_are_absent_from_report_and_log(self):
        import contextlib
        import io
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            lock = root / 'gpu.lock'
            text = 'one two three four five'
            manifest = {'books': [{'name': 'book.txt', 'passages': [{
                'category': 'opening', 'text': text, 'sha256': 'fixture'}]}]}
            url = ('https://fixture-user:fixture-password@host.example/v1'
                   '?access_token=synthetic-query-secret&note=kept#frag-secret')
            config = {'llm_mode': 'local', 'llm_local': {
                'base_url': url, 'model_name': 'fixture',
                'attribution_adapter': {'path': '/optional.gguf', 'scale': 1}}}
            stack.enter_context(patch.object(core, 'acquire_gpu_lock',
                side_effect=lambda: acquire_gpu_lock(str(lock))))
            stack.enter_context(patch.object(runner, 'load_app_config', return_value=config))
            stack.enter_context(patch.object(runner, 'ensure_ideal_settings',
                return_value=(False, {}, 'ready')))
            stack.enter_context(patch('attribution_adapter.served_adapters', return_value=None))
            stack.enter_context(patch.object(runner, 'make_llm_client'))
            stack.enter_context(patch.object(runner, 'process_chunk', return_value=[{
                'speaker': 'NARRATOR', 'text': text, 'instruct': 'neutral'}]))
            output = root / 'report.json'
            log = io.StringIO()
            with contextlib.redirect_stdout(log):
                report = runner.run_manifest(manifest, str(output))
            body = output.read_text() + log.getvalue()
            for secret in ('fixture-password', 'synthetic-query-secret', 'frag-secret'):
                self.assertNotIn(secret, body)
            self.assertIn('[REDACTED]@host.example/v1', report['base_url'])
            self.assertIn('note=kept', report['base_url'])
            self.assertFalse(report['attribution_adapter']['verified'])
            self.assertFalse(gpu_is_busy(str(lock)))
            self.assertNotIn('script', core._task_claims)

    def test_display_sanitizer_scrubs_secret_shapes_and_fails_closed(self):
        import contextlib
        import io
        from diagnostics import REDACTED, sanitize_url_for_display
        normal = 'https://host.example/v1?note=kept&page=2'
        self.assertEqual(normal, sanitize_url_for_display(normal))
        leaky = 'https://host.example/v1?note=sk-abcdefghijklmnop'
        self.assertNotIn('sk-abcdefghijklmnop', sanitize_url_for_display(leaky))
        self.assertIn(REDACTED, sanitize_url_for_display(leaky))
        with patch('diagnostics.urlsplit', side_effect=ValueError('fixture malformed url')):
            self.assertEqual(REDACTED,
                sanitize_url_for_display('https://host.example/v1?token=secret#frag'))

    def test_real_settings_message_and_corpus_report_hide_url_secrets(self):
        import contextlib
        import io
        import lmstudio_settings
        url = ('https://fixture-user:fixture-password@host.example/v1'
               '?access_token=synthetic-query-secret&note=kept#frag-secret')
        native = {'runtime': 'llama.cpp', 'available': True, 'loaded': True,
                  'context_length': 8192, 'parallel': 1, 'optimized': None}
        secrets = ('fixture-password', 'synthetic-query-secret', 'frag-secret')
        with patch.object(lmstudio_settings, 'get_llama_cpp_status', return_value=native):
            _, _, message = lmstudio_settings.ensure_ideal_settings('remote', url, 'fixture')
        for secret in secrets:
            self.assertNotIn(secret, message)
        self.assertIn('[REDACTED]@host.example/v1', message)
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            lock = root / 'gpu.lock'
            text = 'one two three four five'
            manifest = {'books': [{'name': 'book.txt', 'passages': [{
                'category': 'opening', 'text': text, 'sha256': 'fixture'}]}]}
            stack.enter_context(patch.object(core, 'acquire_gpu_lock',
                side_effect=lambda: acquire_gpu_lock(str(lock))))
            lock, manifest, config, heal, adapter, provider, process = self.get_fixture(root, stack)
            heal.side_effect = lambda *args, **kwargs: lmstudio_settings.ensure_ideal_settings(
                'remote', url, 'fixture')
            stack.enter_context(patch.object(lmstudio_settings, 'get_llama_cpp_status',
                return_value=native))
            stack.enter_context(patch.object(runner, 'make_llm_client'))
            stack.enter_context(patch.object(runner, 'process_chunk', return_value=[{
                'speaker': 'NARRATOR', 'text': text, 'instruct': 'neutral'}]))
            output = root / 'report.json'
            log = io.StringIO()
            with contextlib.redirect_stdout(log):
                report = runner.run_manifest(manifest, str(output))
            body = output.read_text() + log.getvalue()
            for secret in secrets:
                self.assertNotIn(secret, body)
            self.assertIn('[REDACTED]@host.example/v1', report['settings_message'])
            self.assertEqual(1, report['summary']['passed'])
            self.assertFalse(gpu_is_busy(str(lock)))
            self.assertNotIn('script', core._task_claims)

    def test_credentials_are_removed_from_report_but_preserved_for_provider(self):
        cases = (
            ('https://fixture-user:fixture-password@host.example/v1', 'https://[REDACTED]@host.example/v1'),
            ('https://fixture-user@host.example/v1', 'https://[REDACTED]@host.example/v1'),
            ('https://fixture-user:fixture-password@[::1]:8090/v1', 'https://[REDACTED]@[::1]:8090/v1'),
            ('https://fixture-user%40name:fixture-password%3Aextra@host.example/v1', 'https://[REDACTED]@host.example/v1'),
            ('https://host.example?note=visible@label', 'https://host.example?note=visible@label'),
        )
        for url, expected in cases:
            with self.subTest(url=url), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp)
                lock, manifest, config, heal, adapter, provider, process = self.get_fixture(root, stack)
                config['llm_local']['base_url'] = url
                before = copy.deepcopy(config);output = root/'report.json'
                report = runner.run_manifest(manifest,str(output))
                self.assertEqual(before['llm_local'],provider.call_args.args[0],
                                 'the canonical client must receive the bound active profile')
                self.assertEqual(url,provider.call_args.args[0]['base_url'])
                self.assertEqual(before,config)
                self.assertNotIn('fixture-user',output.read_text())
                self.assertNotIn('fixture-password',output.read_text())
                self.assertEqual(expected,report['base_url'])
                self.assertEqual(report,json.loads(output.read_text()))

    def test_admission_uses_the_inference_snapshot_despite_live_profile_changes(self):
        for local in (True,False):
            with self.subTest(local=local), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp)
                lock, manifest, config, heal, adapter, provider, process = self.get_fixture(root, stack)
                config['llm_local']['on_this_gpu'] = local
                before = copy.deepcopy(config)
                stack.enter_context(patch.object(core,'llm_is_on_this_gpu',return_value=not local))
                def assert_dispatch(*args,**kwargs):
                    self.assertTrue(core.is_task_running('script'))
                    self.assertIs(local,gpu_is_busy(str(lock)))
                    return False,{},'ready'
                heal.side_effect = assert_dispatch
                output = root/'report.json'
                runner.run_manifest(manifest,str(output))
                self.assertEqual(before,config)
                self.assertEqual(1,json.loads(output.read_text())['summary']['passed'])
                self.assertFalse(gpu_is_busy(str(lock)))
                self.assertFalse(core.is_task_running('script'))
