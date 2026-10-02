"""Stage-loop cancellation preserves native report checkpoints; workers are fixture stubs."""
from contextlib import ExitStack
import copy
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import benchmark_core as core
import benchmark_runner as runner

if os.environ.get('BENCHMARK_CANCEL_SOURCE'):
    spec = importlib.util.spec_from_file_location('saved_cancel_boundaries', os.environ['BENCHMARK_CANCEL_SOURCE'])
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)

CASES = [('voicelab_training','run_lora_training_benchmark','_run_lora_training_worker','_validate_lora_training_fixture'),
    ('voicelab_preparer','run_preparer_benchmark','_run_preparer_worker','_validate_preparer_fixture'),
    ('voicelab_dedup','run_dedup_benchmark','_run_dedup_worker','_validate_dedup_fixture'),
    ('voicelab_profiling','run_profiling_benchmark','_run_profiling_worker','_validate_profiling_fixture'),
    ('voicelab_naming','run_naming_benchmark','_run_naming_worker',None),
    ('audacity_export','run_export_benchmark','_run_export_worker','_validate_export_fixture'),
    ('dataset_builder','run_dataset_builder_benchmark','_run_dataset_builder_worker',None),
    ('persona_generation','run_persona_generation_benchmark','_run_persona_case','_validate_persona_fixture'),
    ('nickname_detection','run_nickname_detection_benchmark','_run_nickname_case','_validate_nickname_fixture'),
    ('script_generation','run_script_generation_benchmark','_run_script_generation_case',None),
    ('script_review','run_script_review_benchmark','_run_script_review_case',None)]


class BenchmarkCancelBoundaryTests(unittest.TestCase):
    def test_worker_failure_preserves_published_prefix_and_resume_skips_it(self):
        for stage, run_name, worker_name, validate_name in CASES[:7]:
            with self.subTest(stage=stage), tempfile.TemporaryDirectory() as tmp, ExitStack() as patches:
                manifest = {'schema_version': 1, 'stage': stage, 'targets': ['local'],
                            'repetitions': 1, 'fixtures': [{'id': 'first', 'sha256': 'a'},
                                                          {'id': 'second', 'sha256': 'b'}]}
                original = copy.deepcopy(manifest)
                environment = core.build_environment_fingerprint('local', {
                    'hostname': 'fixture', 'gpu_name': 'none', 'backend': 'cpu',
                    'python_version': 'fixture', 'git_commit': 'fixture'})
                path = str(Path(tmp) / 'report.json')
                state = {'cancel': False, 'status': 'running',
                         'tasks': [{'status': 'pending'}, {'status': 'pending'}]}
                if validate_name:
                    patches.enter_context(patch.object(runner, validate_name))
                patches.enter_context(patch.object(runner, 'load_app_config', return_value={}))
                first = {'status': 'passed', 'metrics': {'artifact_sha256': 'first-artifact'}}
                worker = patches.enter_context(patch.object(runner, worker_name,
                    side_effect=[first, RuntimeError('second worker crashed')]))
                with self.assertRaisesRegex(RuntimeError, 'second worker crashed'):
                    getattr(runner, run_name)(manifest, environment, path, state, 'unused', tmp)
                saved = json.loads(Path(path).read_text())
                self.assertEqual(saved['cases'], [{'fixture_id': 'first', 'repetition': 1, **first}])
                self.assertNotEqual(state['status'], 'complete')
                worker.reset_mock()
                worker.side_effect = None
                worker.return_value = {'status': 'passed', 'metrics': {'artifact_sha256': 'second-artifact'}}
                resumed = getattr(runner, run_name)(manifest, environment, path, state, 'unused', tmp)
                worker.assert_called_once()
                self.assertEqual(resumed['cases'][0], saved['cases'][0])
                self.assertEqual([case['fixture_id'] for case in resumed['cases']], ['first', 'second'])
                self.assertEqual(json.loads(Path(path).read_text()), resumed)
                self.assertEqual(state['status'], 'complete')
                self.assertEqual(manifest, original)

    def execute(self, case, queued=False, last=False):
        stage, run_name, worker_name, validate_name = case
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as patches:
            manifest = {'schema_version':1,'stage':stage,'targets':['local'], 'repetitions':1 if last else 2,
                'fixtures':[{'id':'first','sha256':'a'}] if last else [{'id':'first','sha256':'a'},{'id':'second','sha256':'b'}]}
            original = copy.deepcopy(manifest)
            environment = core.build_environment_fingerprint('local',{'hostname':'fixture','gpu_name':'none','backend':'cpu','python_version':'fixture','git_commit':'fixture'})
            report_path = str(Path(tmp)/'report.json')
            state = {'cancel':queued,'status':'running','logs':[],'tasks':[{'fixture_id':f['id'],'status':'pending'} for f in manifest['fixtures']]}
            if validate_name:
                patches.enter_context(patch.object(runner,validate_name))
            patches.enter_context(patch.object(runner,'load_app_config',return_value={}))
            patches.enter_context(patch.object(runner,'_get_llm_benchmark_target',return_value=({'model_name':'fixture'},{'context_length':4096})))
            patches.enter_context(patch.object(runner,'make_llm_client',return_value=object()))
            patches.enter_context(patch.object(runner,'_measure_llm_network_rtt',return_value=0))
            patches.enter_context(patch.object(runner,'get_text_fixture_sources',return_value={f['id']:'one two three' for f in manifest['fixtures']}))
            patches.enter_context(patch.object(runner,'_load_review_fixture',return_value=[{'speaker':'NARRATOR','text':'one two three'}]))
            def finish(*args):
                state['cancel'] = True
                result = {'status':'passed','elapsed_seconds':0.1}
                if stage == 'nickname_detection':result.update(fixture_id=args[0]['id'],repetition=args[1])
                if stage in ('script_generation','script_review'):result.update(fixture_id=args[0]['id'],repetition=args[2])
                return result
            worker = patches.enter_context(patch.object(runner,worker_name,side_effect=finish))
            report = getattr(runner,run_name)(manifest,environment,report_path,state,'unused',tmp)
            self.assertEqual(0 if queued else 1,worker.call_count)
            self.assertEqual('cancelled',state['status'])
            self.assertEqual(0 if queued else 1,len(report['cases']))
            self.assertEqual(['done' if last and not queued else 'cancelled']*len(state['tasks']),[t['status'] for t in state['tasks']])
            checkpoint = json.loads(Path(report_path).read_text())
            self.assertEqual(report,checkpoint)
            self.assertEqual(original,manifest)
            # Resume through the actual report identity loader and preserve earlier cases.
            state['cancel'] = False
            def normal(*args):
                result = {'status':'passed','elapsed_seconds':0.2}
                if stage == 'nickname_detection':result.update(fixture_id=args[0]['id'],repetition=args[1])
                if stage in ('script_generation','script_review'):result.update(fixture_id=args[0]['id'],repetition=args[2])
                return result
            worker.reset_mock();worker.side_effect=normal
            resumed = getattr(runner,run_name)(manifest,environment,report_path,state,'unused',tmp)
            self.assertEqual(len(manifest['fixtures'])*manifest['repetitions']-len(checkpoint['cases']),worker.call_count)
            self.assertEqual(checkpoint['cases'],resumed['cases'][:len(checkpoint['cases'])])
            self.assertEqual('complete',state['status'])
            self.assertTrue(all(t['status']=='done' for t in state['tasks']))

    def test_cancel_after_completed_case_stops_remaining_repetitions_and_resumes(self):
        for case in CASES:
            with self.subTest(stage=case[0]):self.execute(case)

    def test_queued_cancel_prevents_first_worker(self):
        for case in CASES:
            with self.subTest(stage=case[0]):self.execute(case,queued=True)

    def test_cancel_during_last_case_cannot_be_overwritten_as_complete(self):
        for case in CASES:
            with self.subTest(stage=case[0]):self.execute(case,last=True)
