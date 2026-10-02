import asyncio
from contextlib import ExitStack
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from config_settings import load_app_config
from routers import benchmark


STAGES = {
    'script_generation': 'run_script_generation_benchmark',
    'script_review': 'run_script_review_benchmark',
    'persona_generation': 'run_persona_generation_benchmark',
    'nickname_detection': 'run_nickname_detection_benchmark',
    'tts_generation': 'run_tts_generation_benchmark',
    'dataset_builder': 'run_dataset_builder_benchmark',
    'voicelab_training': 'run_lora_training_benchmark',
    'voicelab_preparer': 'run_preparer_benchmark',
    'voicelab_dedup': 'run_dedup_benchmark',
    'voicelab_profiling': 'run_profiling_benchmark',
    'voicelab_naming': 'run_naming_benchmark',
    'audacity_export': 'run_export_benchmark',
    'm4b_export': 'run_export_benchmark',
}


class BenchmarkConfigSnapshotTests(unittest.TestCase):
    def run_deferred(self, stage, fail=False):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp); live = root / 'live.json'
            live.write_text(json.dumps({'llm_local': {'model_name': 'old-model', 'api_key': 'secret'},
                                        'tts': {'max_new_tokens': 512}}))
            live.chmod(0o600)
            expected = load_app_config(str(live))
            state = {}; queued = []; paths = []; observed = []
            manifest = {'schema_version': 1, 'stage': stage, 'targets': ['local'], 'fixtures': [{'id': 'f', 'sha256': 'abc'}]}
            stack.enter_context(patch.object(benchmark, 'CONFIG_PATH', str(live)))
            stack.enter_context(patch.object(benchmark, 'REPORTS_DIR', str(root / 'reports')))
            stack.enter_context(patch.object(benchmark, 'process_state', {'benchmark': state}))
            stack.enter_context(patch.object(benchmark, 'check_global_gpu_lock'))
            stack.enter_context(patch.object(benchmark, 'reserve_background_task', return_value='claim'))
            stack.enter_context(patch.object(benchmark, '_init_batch_state'))
            for name in ('collect_cpu_environment', 'collect_local_tts_environment', 'collect_local_environment'):
                stack.enter_context(patch.object(benchmark, name, return_value={'target': 'local', 'sha256': 'old-env'}))
            stack.enter_context(patch.object(benchmark, 'save_environment_baseline'))
            stack.enter_context(patch.object(benchmark, 'load_environment_baseline', return_value=None))
            stack.enter_context(patch.object(benchmark, 'register_claimed_background_task',
                                             side_effect=lambda *args: queued.append(args[-1])))
            preflight = benchmark._build_benchmark_preflight(benchmark.BenchmarkPreflightRequest(manifest=manifest))
            self.assertNotIn('secret', json.dumps(preflight))
            request = benchmark.BenchmarkStartRequest(manifest=manifest, preflight_id=preflight['preflight_id'])
            response = asyncio.run(benchmark.benchmark_start(benchmark.BackgroundTasks(), request))
            self.assertNotIn('secret', json.dumps(response))
            self.assertEqual(1, len(queued))
            live.write_text(json.dumps({'llm_local': {'model_name': 'new-model', 'api_key': 'new-secret'},
                                        'tts': {'max_new_tokens': 2048}}))
            changed = live.read_bytes()
            def runner(_manifest, environment, _report, _state, config_path, _directory):
                paths.append(Path(config_path)); observed.append(load_app_config(config_path))
                self.assertEqual({'target': 'local', 'sha256': 'old-env'}, environment)
                if os.name == 'posix':
                    self.assertEqual(0o600, Path(config_path).stat().st_mode & 0o777)
                    self.assertEqual(0o700, Path(config_path).parent.stat().st_mode & 0o777)
                if fail: raise RuntimeError('fixture runner failed')
            with patch.object(benchmark, STAGES[stage], side_effect=runner):
                if fail:
                    with self.assertRaisesRegex(RuntimeError, 'fixture runner failed'): queued[0]()
                else: queued[0]()
            self.assertEqual([expected], observed)
            self.assertEqual(changed, live.read_bytes())
            self.assertTrue(paths)
            self.assertTrue(all(not path.exists() and not path.parent.exists() for path in paths))

    def test_all_deferred_stages_read_captured_config_after_live_file_changes(self):
        for stage in STAGES:
            with self.subTest(stage=stage): self.run_deferred(stage)

    def test_runner_failure_cleans_private_snapshot(self):
        self.run_deferred('script_generation', fail=True)

    def test_start_preflight_uses_one_config_read_for_environment_and_snapshot(self):
        config = {'llm_local': {'model_name': 'old-model'}}
        request = benchmark.BenchmarkPreflightRequest(manifest={
            'schema_version': 1, 'stage': 'script_generation', 'targets': ['local'], 'fixtures': [{'id': 'f', 'sha256': 'abc'}]})
        with patch.object(benchmark, 'load_app_config', return_value=config) as load, \
             patch.object(benchmark, 'check_global_gpu_lock'), \
             patch.object(benchmark, 'collect_local_environment', return_value={'target': 'local', 'sha256': 'abc'}) as collect:
            preflight, captured = benchmark._build_benchmark_start_preflight(request)
        load.assert_called_once_with(benchmark.CONFIG_PATH)
        collect.assert_called_once_with(benchmark.ROOT_DIR, 'old-model')
        config['llm_local']['model_name'] = 'changed'
        self.assertEqual('old-model', captured['llm_local']['model_name'])
        self.assertNotIn('config', preflight)
