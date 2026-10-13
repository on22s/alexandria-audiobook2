"""Synthetic source, settings and selected-runtime resume regressions."""
import asyncio
import copy
from contextlib import ExitStack, redirect_stdout
from io import StringIO
import json
import hashlib
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import benchmark_core as core
import benchmark_environment as env
import benchmark_environment_identity as source
import benchmark_runner as runner
import review_script as review
from routers import benchmark


def environment():
    return core.build_environment_fingerprint('local', {'hostname': 'fixture',
        'gpu_name': 'fixture', 'backend': 'cpu', 'python_version': '3.fixture', 'git_commit': 'synthetic'})


def manifest(stage='script_generation', settings=None):
    return {'schema_version': 1, 'stage': stage, 'targets': ['local'],
            'fixtures': [{'id': 'fixture', 'sha256': 'abc'}], 'settings': settings or {}}


class SourceResumeTests(unittest.TestCase):
    def test_workers_helpers_and_untracked_source_invalidate_resume(self):
        paths = ['name_voices.py', 'voice_analysis.py', 'voice_profiler.py',
            'alexandria_preparer_rocm_compatible.py', 'alexandria_alignment.py',
            'alexandria_run_manifest.py', 'gpu_stats.py', 'tools/voice_lab/name_voices.py',
            'tools/voice_lab/voice_analysis.py', 'tools/voice_lab/voice_profiler.py']
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'app').mkdir(); (root / 'app/source.py').write_text('synthetic=1\n')
            for name in paths:
                path = root / name; path.parent.mkdir(parents=True, exist_ok=True); path.write_text('synthetic=1\n')
            def git(*args):
                return subprocess.check_output(['git', '-C', tmp, *args], text=True).strip()
            git('init', '-q'); git('add', '.')
            git('-c', 'user.name=fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'fixture')
            head = git('rev-parse', 'HEAD'); baseline = source._get_local_worktree_identity(tmp)
            old = core.build_environment_fingerprint('local', {**environment()['details'], 'worktree': baseline})
            report_path = str(root / 'report.json')
            core.save_benchmark_report(report_path, core.build_benchmark_report(manifest(), old))
            for name in paths:
                with self.subTest(path=name):
                    path = root / name; path.write_text('synthetic=2\n')
                    observed = source._get_local_worktree_identity(tmp)
                    self.assertTrue(observed['dirty']); self.assertNotEqual(baseline, observed)
                    self.assertEqual(head, git('rev-parse', 'HEAD'))
                    new = core.build_environment_fingerprint('local', {**old['details'], 'worktree': observed})
                    with self.assertRaisesRegex(ValueError, 'environment changed'):
                        core.load_resumable_benchmark_report(report_path, manifest(), new)
                    path.write_text('synthetic=1\n')
            extra = root / 'new_worker.py'; extra.write_text('synthetic=1\n')
            first = source._get_local_worktree_identity(tmp); extra.write_text('synthetic=2\n')
            self.assertNotEqual(first, source._get_local_worktree_identity(tmp))
            extra.unlink(); (root / 'private_book.txt').write_text('synthetic private text')
            self.assertEqual(baseline, source._get_local_worktree_identity(tmp))


class SelectedPythonTests(unittest.TestCase):
    def test_cpu_probe_observes_selected_python_and_package_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); (root / 'app').mkdir(); repo = Path(__file__).resolve().parents[1]
            for name in ('runtime_info.py', 'benchmark_environment_identity.py'):
                shutil.copyfile(repo / name, root / 'app' / name)
            subprocess.run(['git', 'init', '-q', tmp], check=True)
            subprocess.run(['git', '-C', tmp, 'add', 'app'], check=True)
            subprocess.run(['git', '-C', tmp, '-c', 'user.name=fixture', '-c', 'user.email=fixture@example.test', 'commit', '-qm', 'fixture'], check=True)
            python = root / 'selected-python'
            def install(version):
                python.write_text('#!' + sys.executable + '\nimport sys,platform\nfrom importlib import metadata\n'
                    'platform.python_version=lambda:"3.99.fixture"\n'
                    f'metadata.version=lambda name:{version!r}\nexec(sys.argv[2])\n'); python.chmod(0o755)
            install('package-v1'); first = env.collect_cpu_environment(tmp, 'local', local_python=str(python))
            self.assertEqual(str(python), first['details']['python_executable'])
            self.assertEqual('3.99.fixture', first['details']['python_version'])
            self.assertEqual('package-v1', first['details']['packages']['torch'])
            install('package-v2'); second = env.collect_cpu_environment(tmp, 'local', local_python=str(python))
            self.assertEqual('package-v2', second['details']['packages']['torch'])
            self.assertNotEqual(first['sha256'], second['sha256'])

    def test_preflight_and_dispatch_select_same_python(self):
        selected = '/synthetic/selected-python'
        for stage in ('voicelab_naming', 'audacity_export', 'm4b_export', 'dataset_builder'):
            with self.subTest(stage=stage), ExitStack() as stack:
                for name in ('check_global_gpu_lock', 'save_environment_baseline'):
                    stack.enter_context(patch.object(benchmark, name))
                stack.enter_context(patch.object(benchmark, 'load_environment_baseline', return_value=None))
                cpu = stack.enter_context(patch.object(benchmark, 'collect_cpu_environment', return_value=environment()))
                tts = stack.enter_context(patch.object(benchmark, 'collect_local_tts_environment', return_value=environment()))
                benchmark._build_benchmark_preflight(benchmark.BenchmarkPreflightRequest(manifest=manifest(stage, {'local_python': selected})), config={})
                if stage == 'dataset_builder': self.assertEqual(selected, tts.call_args.args[1])
                else: self.assertEqual(selected, cpu.call_args.kwargs['local_python'])
                worker = stack.enter_context(patch.object(runner, 'run_benchmark_worker', return_value={}))
                settings = {'local_python': selected}
                if stage == 'voicelab_naming':
                    fixture = {'entries': []}; fixture['sha256'] = runner._hash_entries(fixture)
                    runner._run_naming_worker(fixture, 'local', settings, '/synthetic', '')
                elif stage == 'dataset_builder':
                    fixture = {'description': 'fixture', 'samples': [], 'global_seed': 1, 'seeds': []}
                    fixture['sha256'] = runner._hash_entries(fixture)
                    runner._run_dataset_builder_worker(fixture, 'local', settings, '/synthetic', '', {})
                else:
                    fixture = {'chunks': [], 'audio_sha256': {}, 'per_chunk_chapters': False}
                    fixture['sha256'] = runner._hash_entries(fixture)
                    runner._run_export_worker(stage, fixture, 'local', settings, '/synthetic', '')
                self.assertEqual(selected, worker.call_args.args[0][0])


class ExecutionConfigTests(unittest.TestCase):
    def preflight(self, cfg, stage='script_generation'):
        with ExitStack() as stack:
            for name in ('check_global_gpu_lock', 'save_environment_baseline'):
                stack.enter_context(patch.object(benchmark, name))
            stack.enter_context(patch.object(benchmark, 'load_environment_baseline', return_value=None))
            for name in ('collect_local_environment', 'collect_local_tts_environment'):
                stack.enter_context(patch.object(benchmark, name, return_value=environment()))
            return benchmark._build_benchmark_preflight(benchmark.BenchmarkPreflightRequest(manifest=manifest(stage)), config=cfg)

    def test_stage_config_invalidates_preflight_and_completed_or_partial_report(self):
        cfg = {'llm_local': {'model_name': 'fixture', 'api_key': 'synthetic-secret',
            'provider_headers': {'X-private': 'header-secret'}}, 'generation': {'temperature': .4},
            'prompts': {'system_prompt': 'private-prompt', 'review_system_prompt': 'review-private'},
            'tts': {'max_new_tokens': 512}}
        changes = [('script_generation', 'generation', 'temperature', .9),
            ('script_generation', 'prompts', 'system_prompt', 'changed'),
            ('script_review', 'prompts', 'review_system_prompt', 'changed'),
            ('tts_generation', 'tts', 'max_new_tokens', 1024), ('dataset_builder', 'tts', 'max_new_tokens', 1024)]
        for stage, section, key, value in changes:
            with self.subTest(stage=stage, key=key), tempfile.TemporaryDirectory() as tmp:
                original = self.preflight(cfg, stage); changed_cfg = copy.deepcopy(cfg)
                changed_cfg[section][key] = value; changed = self.preflight(changed_cfg, stage)
                self.assertNotEqual(original['preflight_id'], changed['preflight_id'])
                for secret in ('synthetic-secret', 'header-secret', 'private-prompt', 'review-private'):
                    self.assertNotIn(secret, json.dumps(original))
                for cases in ([], [{'fixture_id': 'fixture', 'repetition': 0, 'status': 'passed'}]):
                    report = core.build_benchmark_report(original['manifest'], original['environments']['local'])
                    report['cases'] = cases; path = str(Path(tmp) / 'report.json'); core.save_benchmark_report(path, report)
                    with self.assertRaisesRegex(ValueError, 'environment changed'):
                        core.load_resumable_benchmark_report(path, changed['manifest'], changed['environments']['local'])
                request = benchmark.BenchmarkStartRequest(manifest=manifest(stage), preflight_id=original['preflight_id'])
                with patch.object(benchmark, '_build_benchmark_start_preflight', return_value=(changed, changed_cfg)), patch.object(benchmark, 'reserve_background_task') as reserve:
                    with self.assertRaises(benchmark.HTTPException) as rejected:
                        asyncio.run(benchmark.benchmark_start(benchmark.BackgroundTasks(), request))
                self.assertEqual(409, rejected.exception.status_code); reserve.assert_not_called()

    def test_named_target_profile_is_shared_with_runner_and_ignores_setup_toggle(self):
        cfg = {'llm_mode': 'remote', 'llm': {'model_name': 'stale mirror'},
               'llm_local': {'model_name': 'local', 'base_url': 'http://local/v1'},
               'llm_remote': {'model_name': 'remote', 'base_url': 'http://remote/v1'},
               'generation': {'max_input_tokens': 512}}
        with patch.object(runner, 'get_lmstudio_status', return_value={'available': True, 'loaded': True}), \
             patch.object(runner, 'get_remote_lmstudio_status', return_value={'available': True, 'loaded': True}):
            for target, model in (('local', 'local'), ('thunder', 'remote')):
                selected = core.get_benchmark_llm_config(cfg, target)
                dispatched, _ = runner._get_llm_benchmark_target(cfg, target)
                self.assertEqual(selected, dispatched)
                self.assertEqual(model, selected['model_name'])
                self.assertEqual(model, benchmark._get_model_name(cfg, target))
                self.assertEqual(512, selected['max_input_tokens'])
        self.assertEqual('remote', cfg['llm_mode'])
        self.assertEqual('stale mirror', cfg['llm']['model_name'])
        self.assertEqual('stale mirror', core.get_benchmark_llm_config({'llm': cfg['llm']}, 'local')['model_name'])
        self.assertEqual({}, core.get_benchmark_llm_config({'llm': cfg['llm']}, 'thunder'))

    def test_inherited_input_limit_is_bound_for_persona_and_nickname_stages(self):
        cfg = {'llm_local': {'model_name': 'fixture'}, 'generation': {'max_input_tokens': 512}}
        for stage in ('persona_generation', 'nickname_detection'):
            before = self.preflight(cfg, stage)
            changed = {**cfg, 'generation': {'max_input_tokens': 1024}}
            self.assertNotEqual(before['preflight_id'], self.preflight(changed, stage)['preflight_id'])
            overridden = {**cfg, 'llm_local': {'model_name': 'fixture', 'max_input_tokens': 256}}
            self.assertEqual(self.preflight(overridden, stage)['preflight_id'],
                             self.preflight({**overridden, 'generation': changed['generation']}, stage)['preflight_id'])

    def test_generation_report_reuse_then_new_prompt_dispatches_new_run(self):
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp); text = 'First synthetic benchmark line.'
            fixture_path = root / 'fixture.txt'; fixture_path.write_text(text)
            digest = hashlib.sha256(fixture_path.read_bytes()).hexdigest()
            request = benchmark.BenchmarkPreflightRequest(manifest={
                'schema_version': 1, 'stage': 'script_generation', 'targets': ['local'],
                'fixtures': [{'id': 'fixture', 'sha256': digest, 'path': str(fixture_path)}]})
            cfg = {'llm_local': {'model_name': 'fixture', 'base_url': 'http://synthetic/v1'},
                   'generation': {'temperature': .4}, 'prompts': {'system_prompt': 'old prompt'}}
            stack.enter_context(patch.object(benchmark, 'check_global_gpu_lock'))
            stack.enter_context(patch.object(benchmark, 'collect_local_environment', return_value=environment()))
            stack.enter_context(patch.object(runner, 'load_app_config', side_effect=lambda _path: copy.deepcopy(cfg)))
            stack.enter_context(patch.object(runner, 'get_lmstudio_status', return_value={
                'available': True, 'loaded': True, 'context_length': 8192}))
            stack.enter_context(patch.object(runner, 'make_llm_client'))
            observed = []
            def generate(_client, _model, _text, _chunk, _total, params, **kwargs):
                observed.append((params.system_prompt, params.temperature))
                return [{'speaker': 'NARRATOR', 'text': text, 'instruct': 'Plain.'}]
            stack.enter_context(patch.object(runner, 'process_chunk', side_effect=generate))
            def run(preflight):
                path = str(root / (preflight['preflight_id'] + '.json'))
                state = {'cancel': False, 'logs': [], 'tasks': [{'fixture_id': 'fixture', 'status': 'pending'}]}
                report = runner.run_script_generation_benchmark(preflight['manifest'],
                    preflight['environments']['local'], path, state, '/synthetic/config.json', tmp)
                return report, Path(path)
            old = benchmark._build_benchmark_preflight(request, config=cfg)
            old_report, old_path = run(old); old_bytes = old_path.read_bytes()
            run(old); self.assertEqual([('old prompt', .4)], observed)
            cfg['prompts']['system_prompt'] = 'new prompt'; cfg['generation']['temperature'] = .9
            new = benchmark._build_benchmark_preflight(request, config=cfg)
            new_report, new_path = run(new)
            self.assertNotEqual(old_path, new_path)
            self.assertEqual([('old prompt', .4), ('new prompt', .9)], observed)
            self.assertEqual('passed', new_report['cases'][0]['status'])
            self.assertEqual(old_report['cases'], json.loads(old_path.read_text())['cases'])
            self.assertNotIn('old prompt', old_bytes.decode())

    def test_unconsumed_prompt_and_cache_do_not_change_identity(self):
        cfg = {'llm_local': {'model_name': 'fixture'}, 'prompts': {'review_system_prompt': 'review'}}
        before = self.preflight(cfg); cfg['prompts']['review_system_prompt'] = 'new review'; cfg['llm_local']['concurrency'] = 7
        self.assertEqual(before['preflight_id'], self.preflight(cfg)['preflight_id'])


class PartialReviewSettingsTests(unittest.TestCase):
    def test_native_partial_review_restarts_changed_settings_and_legacy_checkpoint(self):
        for context in (0, 1):
            for change in ('model', 'prompt', 'temperature', 'legacy', 'unchanged'):
                for force in (False, True):
                    with self.subTest(context=context, change=change, force=force), tempfile.TemporaryDirectory() as tmp:
                        root = Path(tmp); source_path = root / 'source.json'; output = root / 'reviewed.json'
                        rows = [{'speaker': 'NARRATOR', 'text': text, 'instruct': 'Plain.'} for text in ('First synthetic line.', 'Second synthetic line.')]
                        source_path.write_text(json.dumps(rows))
                        cfg = {'generation': {'review_batch_size': 1, 'temperature': .4}, 'prompts': {}, 'llm_mode': 'local'}
                        profile = {'model_name': 'old-model', 'base_url': 'http://synthetic/v1'}
                        def run(label, partial=False):
                            with ExitStack() as stack:
                                stack.enter_context(redirect_stdout(StringIO()))
                                for name, value in {'get_runtime_data_dir': tmp, 'load_app_config': cfg,
                                    'get_active_llm_config': profile, 'ensure_ideal_settings': (False, {}, 'fixture'),
                                    'get_current_status': {'loaded': False}, 'make_run_client': object(),
                                    'get_cached_or_benchmarked_concurrency': 1}.items():
                                    stack.enter_context(patch.object(review, name, return_value=value))
                                stack.enter_context(patch.object(review, 'wait_for_vram_headroom', side_effect=[True, False, False] if partial else None, return_value=True))
                                dispatched = stack.enter_context(patch.object(review, 'review_batch', side_effect=lambda _client, _model, batch, *args, **kwargs: [{**row, 'speaker': label} for row in batch]))
                                argv = ['review', '--input', str(source_path), '--output', str(output), '--context-window', str(context)]
                                if force: argv.append('--force-review')
                                stack.enter_context(patch.object(sys, 'argv', argv)); review.main(); return dispatched
                        first = run('OLD_MODEL_LABEL', partial=True); self.assertEqual(1, first.call_count)
                        checkpoint = Path(review._checkpoint_path(str(output))); self.assertTrue(checkpoint.exists())
                        if change == 'model': profile['model_name'] = 'new-model'
                        elif change == 'prompt': cfg['prompts']['review_system_prompt'] = 'new synthetic prompt'
                        elif change == 'temperature': cfg['generation']['temperature'] = .99
                        elif change == 'legacy':
                            from generation_checkpoint_deltas import load_generation_checkpoint_document
                            data = load_generation_checkpoint_document(str(checkpoint)); data.pop('execution_sha256', None)
                            review.clear_checkpoint(str(output)); checkpoint.write_text(json.dumps(data))
                        second = run('NEW_MODEL_LABEL'); self.assertEqual(1 if change == 'unchanged' else 2, second.call_count)
                        self.assertEqual(rows[1:] if change == 'unchanged' else rows[:1], second.call_args_list[0].args[2])
                        self.assertEqual(['OLD_MODEL_LABEL', 'NEW_MODEL_LABEL'] if change == 'unchanged' else ['NEW_MODEL_LABEL'] * 2, [row['speaker'] for row in json.loads(output.read_text())])
                        self.assertFalse(checkpoint.exists()); self.assertEqual(rows, json.loads(source_path.read_text()))
