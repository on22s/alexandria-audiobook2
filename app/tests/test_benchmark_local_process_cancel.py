"""Native owned local worker cancellation; no GPU/model/SSH commands."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

import benchmark_core as core
import benchmark_execution as execution
import benchmark_runner as runner


def is_alive(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().rsplit(')',1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


class BenchmarkLocalProcessTests(unittest.TestCase):
    def test_native_stdin_stdout_and_stderr_survive_polling(self):
        state = {'cancel':False}
        token = execution.BENCHMARK_STATE.set(state)
        payload = '音声🙂-' * 30000
        try:
            result = execution.run_benchmark_subprocess([sys.executable,'-c',
                'import sys,time; time.sleep(.2); data=sys.stdin.read();print(data,end="");print("stderr-marker",file=sys.stderr)'],
                input=payload,capture_output=True,text=True,timeout=5)
            self.assertEqual(0,result.returncode)
            self.assertEqual(payload,result.stdout)
            self.assertEqual('stderr-marker\n',result.stderr)
            self.assertEqual([],state['processes'])
        finally:
            execution.BENCHMARK_STATE.reset(token)

    @unittest.skipUnless(sys.platform=='linux','native Linux ownership proof')
    def test_cancel_reaps_detached_descendant_and_keeps_unrelated_process_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker = Path(tmp)/'pids.json'
            worker = '''import json,os,pathlib,subprocess,sys,time
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],start_new_session=True)
pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(),child.pid]))
time.sleep(30)
'''
            state = {'cancel':False}
            def cancel():
                deadline=time.monotonic()+5
                while not marker.exists() and time.monotonic()<deadline:time.sleep(.01)
                state['cancel']=True
            monitor=threading.Thread(target=cancel);monitor.start()
            sentinel=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
            token=execution.BENCHMARK_STATE.set(state)
            try:
                with self.assertRaises(execution.BenchmarkCancelled):
                    execution.run_benchmark_subprocess([sys.executable,'-c',worker,str(marker)],capture_output=True,text=True,timeout=10)
                self.assertTrue(marker.exists(),'worker must actually start before cancellation')
                self.assertTrue(all(not is_alive(pid) for pid in json.loads(marker.read_text())))
                self.assertIsNone(sentinel.poll())
                self.assertEqual([],state['processes'])
            finally:
                execution.BENCHMARK_STATE.reset(token)
                monitor.join(timeout=6)
                sentinel.terminate();sentinel.wait(timeout=5)

    def test_native_timeout_is_retained_and_context_does_not_leak(self):
        state={'cancel':False};token=execution.BENCHMARK_STATE.set(state)
        try:
            with self.assertRaises(subprocess.TimeoutExpired):
                execution.run_benchmark_subprocess([sys.executable,'-c','import time;time.sleep(30)'],capture_output=True,text=True,timeout=.2)
            self.assertEqual([],state['processes'])
        finally:
            execution.BENCHMARK_STATE.reset(token)
        self.assertIsNone(execution.BENCHMARK_STATE.get())

    def test_real_stage_decorator_persists_cancelled_report_after_owned_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            marker=Path(tmp)/'started'
            state={'cancel':False,'status':'running','tasks':[{'fixture_id':'one','status':'pending'}]}
            manifest={'schema_version':1,'stage':'voicelab_naming','targets':['local'],'repetitions':2,
                      'fixtures':[{'id':'one','sha256':'fixture'}]}
            environment=core.build_environment_fingerprint('local',{'hostname':'fixture','gpu_name':'none','backend':'cpu','python_version':'fixture','git_commit':'fixture'})
            path=str(Path(tmp)/'report.json')
            def work(*args):
                execution.run_benchmark_subprocess([sys.executable,'-c',
                    'import pathlib,sys,time;pathlib.Path(sys.argv[1]).write_text("started");time.sleep(30)',str(marker)],
                    capture_output=True,text=True,timeout=10)
                raise AssertionError('cancelled worker cannot become a measured case')
            def cancel():
                deadline=time.monotonic()+5
                while not marker.exists() and time.monotonic()<deadline:time.sleep(.01)
                state['cancel']=True
            monitor=threading.Thread(target=cancel);monitor.start()
            try:
                with patch.object(runner,'load_app_config',return_value={}),patch.object(runner,'_run_naming_worker',side_effect=work) as dispatch:
                    report=runner.run_naming_benchmark(manifest,environment,path,state,'unused',tmp)
                self.assertEqual(1,dispatch.call_count)
                self.assertEqual([],report['cases'])
                self.assertEqual(report,json.loads(Path(path).read_text()))
                self.assertEqual('cancelled',state['status'])
                self.assertEqual('cancelled',state['tasks'][0]['status'])
                self.assertEqual([],state['processes'])
                self.assertIsNone(execution.BENCHMARK_STATE.get())
            finally:
                monitor.join(timeout=6)

    @unittest.skipUnless(sys.platform=='linux','native Linux descriptor ownership proof')
    def test_command_owner_retains_gpu_and_task_descriptors_until_work_finishes(self):
        import core as runtime
        import fcntl
        with tempfile.TemporaryDirectory() as tmp, open(Path(tmp)/"gpu.lock", "w+") as gpu, tempfile.TemporaryFile() as task:
            fcntl.flock(gpu.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            state={'cancel':False};token=execution.BENCHMARK_STATE.set(state)
            original=execution.start_owned_subprocess
            observed=[]
            def start(*args,**kwargs):
                process=original(*args,**kwargs)
                self.assertEqual(gpu.fileno(),kwargs['gpu_lease_fd'])
                self.assertEqual(task.fileno(),kwargs['task_lease_fd'])
                self.assertTrue(Path(f'/proc/{process.pid}/fd/{gpu.fileno()}').exists())
                self.assertTrue(Path(f'/proc/{process.pid}/fd/{task.fileno()}').exists())
                observed.append(process)
                return process
            try:
                environment={**os.environ,'GPU_LOCK':str(Path(tmp)/'gpu.lock'),'ALEXANDRIA_GPU_LOCK_HELD':'1',
                    'ALEXANDRIA_GPU_LOCK_PID':str(os.getpid()),'ALEXANDRIA_GPU_LOCK_FD':str(gpu.fileno())}
                with patch.object(runtime,'get_gpu_task_environment',return_value=environment), \
                     patch.object(runtime,'get_task_lease_descriptor',return_value=task.fileno()), \
                     patch.object(execution,'start_owned_subprocess',side_effect=start):
                    result=execution.run_benchmark_subprocess([sys.executable,'-c','import time;time.sleep(.15);print("finished")'],capture_output=True,text=True,timeout=5)
                self.assertEqual('finished\n',result.stdout)
                self.assertEqual(1,len(observed))
                self.assertFalse(Path(f'/proc/{observed[0].pid}').exists())
            finally:
                execution.BENCHMARK_STATE.reset(token)
