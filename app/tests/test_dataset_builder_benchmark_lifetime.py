"""Benchmark completion and lifetime use the actual owned worker thread."""
import array
import contextlib
import copy
import tempfile
import threading
import time
import unittest
import wave
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import core
import dataset_builder_benchmark as benchmark
from routers import dataset_builder as routes


class BuilderBenchmarkLifetimeTests(unittest.TestCase):
    @contextlib.contextmanager
    def fixture(self,render=None):
        payload={'fixture':{'description':'warm voice','samples':[{'text':'One'},{'text':'Two'}],'global_seed':42,'seeds':[-1,7]},'tts':{'device':'cpu'}}
        states={'dataset_builder':{'running':False,'logs':[],'cancel':False}}
        threads=[];real_start=routes.start_claimed_task_thread
        with tempfile.TemporaryDirectory() as tmp:
            wav=Path(tmp)/'source.wav'
            with wave.open(str(wav),'wb') as audio:
                audio.setnchannels(1);audio.setsampwidth(2);audio.setframerate(24000);audio.writeframes(array.array('h',[4096]*2400).tobytes())
            engine=SimpleNamespace(generate_voice_design=render or (lambda **kwargs:(str(wav),kwargs['sample_text'])))
            def start(*args,**kwargs):
                worker=real_start(*args,**kwargs);threads.append(worker);return worker
            with patch.object(routes,'DATASET_BUILDER_DIR',str(Path(tmp)/'production')),patch.object(routes.project_manager,'get_engine',return_value=SimpleNamespace(label='production fixture')),patch.object(core,'process_state',states),patch.object(core,'_gpu_leases',{}),patch.object(core,'_task_claims',{}),patch.object(core,'acquire_gpu_lock',return_value=None),patch.object(routes,'process_state',states),patch.object(benchmark,'TTSEngine',return_value=engine),patch.object(routes,'start_claimed_task_thread',side_effect=start):
                try:yield payload,states,wav,threads
                finally:
                    states['dataset_builder']['cancel']=True
                    for thread in threads:thread.join(3)

    def test_real_completed_worker_does_not_need_done_log_and_reports_one_batch_throughput(self):
        with self.fixture() as (payload,states,wav,threads):
            real_save=routes._save_builder_state;before=copy.deepcopy(payload)
            def save(name,state):
                result=real_save(name,state)
                # Exercise completion without depending on terminal log spelling.
                if all(s.get('status')=='done' for s in state['samples']):
                    class Logs(list):
                        def append(self,line):
                            if str(line).startswith('[DONE]'):line='Completed samples successfully'
                            super().append(line)
                    states['dataset_builder']['logs']=Logs(states['dataset_builder']['logs'])
                return result
            def reject_log_polling(_seconds):
                for worker in threads:worker.join(2)
                raise AssertionError('completion still depends on log polling')
            with patch.object(routes,'_save_builder_state',side_effect=save),patch.object(benchmark.time,'sleep',side_effect=reject_log_polling):
                result=benchmark.execute_payload(payload)
            self.assertEqual('passed',result['status']);self.assertEqual(2,result['completed']);self.assertEqual(before,payload)
            self.assertTrue(all(not worker.is_alive() for worker in threads))
            self.assertFalse(any(str(line).startswith('[DONE]') for line in result['logs']))
            for output in result['outputs']:self.assertEqual(.1,output['metrics']['duration_seconds'])

    def test_native_two_clip_result_reports_aggregate_throughput_without_per_sample_batch_times(self):
        with self.fixture() as (payload,states,wav,threads):
            result=benchmark.execute_payload(payload)
            self.assertIn('audio_duration_seconds',result)
            self.assertEqual(.2,result['audio_duration_seconds'])
            self.assertGreater(result['audio_seconds_per_second'],0)
            # Account only for the report's millisecond/four-decimal rounding.
            self.assertAlmostEqual(.2,result['audio_seconds_per_second']*result['elapsed_seconds'],delta=.0005*result['audio_seconds_per_second']+.00001)
            for output in result['outputs']:
                self.assertNotIn('elapsed_seconds',output['metrics']);self.assertNotIn('audio_seconds_per_second',output['metrics'])
                self.assertEqual(.1,output['metrics']['duration_seconds'])

    def test_timeout_keeps_globals_and_temp_project_until_worker_has_stopped(self):
        entered=threading.Event();release=threading.Event();seen=[]
        def render(**kwargs):
            entered.set();self.assertTrue(release.wait(3))
            seen.append((routes.DATASET_BUILDER_DIR,routes.project_manager.get_engine()))
            raise RuntimeError('fixture generation interrupted')
        with self.fixture(render) as (payload,states,wav,threads):
            original_dir=routes.DATASET_BUILDER_DIR;original_engine=routes.project_manager.get_engine
            real_monotonic=time.monotonic;calls=[]
            def clock():
                calls.append(None)
                return real_monotonic()+2000*len(calls)
            real_route = routes.dataset_builder_generate_batch
            async def launch(request):
                response = await real_route(request)
                self.assertTrue(entered.wait(2), 'native generation did not enter before timeout fixture')
                return response
            with patch.object(routes,'dataset_builder_generate_batch',side_effect=launch),patch.object(benchmark.time,'monotonic',side_effect=clock),ThreadPoolExecutor(1) as pool:
                future=pool.submit(benchmark.execute_payload,payload)
                try:
                    self.assertTrue(entered.wait(2))
                    # Let the timeout set cancel, then verify cleanup still waits for the native worker.
                    deadline=real_monotonic()+1
                    while not states['dataset_builder']['cancel'] and real_monotonic()<deadline:threading.Event().wait(.01)
                    self.assertTrue(states['dataset_builder']['cancel'])
                    self.assertFalse(future.done(),'benchmark restored globals while inference was still alive')
                    self.assertNotEqual(original_dir,routes.DATASET_BUILDER_DIR)
                    self.assertTrue(Path(routes.DATASET_BUILDER_DIR).is_dir())
                finally:release.set()
                with self.assertRaises(TimeoutError):future.result()
            self.assertTrue(all(not worker.is_alive() for worker in threads))
            self.assertEqual(original_dir,routes.DATASET_BUILDER_DIR);self.assertEqual(original_engine,routes.project_manager.get_engine)
            self.assertEqual(1,len(seen));self.assertNotEqual(original_dir,seen[0][0]);self.assertFalse(Path(seen[0][0]).exists())

    def test_invalid_request_cannot_leave_routing_globals_changed(self):
        with self.fixture() as (payload,states,wav,threads):
            payload['fixture']['global_seed']=-2
            original_dir=routes.DATASET_BUILDER_DIR;original_engine=routes.project_manager.get_engine
            try:
                with self.assertRaises(ValueError):benchmark.execute_payload(payload)
                self.assertEqual(original_dir,routes.DATASET_BUILDER_DIR);self.assertEqual(original_engine,routes.project_manager.get_engine)
            finally:
                # Restore explicitly on the unfixed baseline so later fixtures stay isolated.
                routes.DATASET_BUILDER_DIR=original_dir;routes.project_manager.get_engine=original_engine

    def test_failed_samples_return_failed_report_after_actual_worker_exit(self):
        def fail(**kwargs):raise RuntimeError('known rejected generation')
        with self.fixture(fail) as (payload,states,wav,threads):
            original_dir=routes.DATASET_BUILDER_DIR;original_engine=routes.project_manager.get_engine
            result=benchmark.execute_payload(payload)
            self.assertEqual('failed',result['status']);self.assertEqual(0,result['completed'])
            self.assertEqual(0,result['audio_duration_seconds']);self.assertEqual(0,result['audio_seconds_per_second'])
            self.assertTrue(all(not worker.is_alive() for worker in threads))
            self.assertEqual(['error','error'],[output['state']['status'] for output in result['outputs']])
            self.assertTrue(all(output['metrics'] is None for output in result['outputs']))
            self.assertEqual(original_dir,routes.DATASET_BUILDER_DIR);self.assertEqual(original_engine,routes.project_manager.get_engine)

    def test_launch_failure_restores_routing_and_releases_only_unstarted_claim(self):
        with self.fixture() as (payload,states,wav,threads):
            original_dir=routes.DATASET_BUILDER_DIR;original_engine=routes.project_manager.get_engine
            with patch.object(routes,'start_claimed_task_thread',side_effect=OSError('known thread launch failure')):
                with self.assertRaisesRegex(OSError,'known thread launch failure'):benchmark.execute_payload(payload)
            self.assertEqual([],threads);self.assertEqual({},core._task_claims)
            self.assertEqual(original_dir,routes.DATASET_BUILDER_DIR);self.assertEqual(original_engine,routes.project_manager.get_engine)
