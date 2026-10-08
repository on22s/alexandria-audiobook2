import copy
import hashlib
import json
import ntpath
import posixpath
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import unittest
import benchmark_runner
import benchmark_core
import benchmark_execution as be
import benchmark_remote_execution as remote


class BenchmarkRecoveryTests(unittest.TestCase):
    def test_remote_supervisor_path_independent_of_local_path_semantics(self):
        class Captured(BaseException):
            pass
        with tempfile.TemporaryDirectory(prefix='remote space ') as tmp:
            app = Path(tmp, 'app'); app.mkdir()
            supervisor = app / 'benchmark_remote_command.py'; supervisor.write_text('pass')
            for paths in (ntpath, posixpath):
                calls = []
                def capture(command, **kwargs):
                    calls.append(command)
                    raise Captured()
                with patch.object(remote, 'os', SimpleNamespace(path=paths)), \
                     patch.object(be, 'get_benchmark_process_options', side_effect=lambda state, options: options), \
                     patch.object(remote, 'start_owned_subprocess', side_effect=capture):
                    with self.assertRaises(Captured):
                        remote.run_remote_benchmark_subprocess(['ssh','host','unused'],
                            ['python3',str(app/'worker.py')],{},timeout=10,check=True,input='',capture_output=True,text=True)
                self.assertEqual(str(supervisor), shlex.split(calls[0][2])[1])
                self.assertTrue(Path(shlex.split(calls[0][2])[1]).exists())

    def test_batch_publishes_active_state_and_preserves_finished_cases_on_error(self):
        for mode in ('success','failure','cancel'):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                manifest={'stage':'script_generation','repetitions':1,'fixtures':[{'id':'done'},{'id':'one'},{'id':'two'}]}
                initial={'cases':[{'fixture_id':'done','repetition':1,'status':'passed'}]}
                state={'cancel':False,'logs':[],'current_task_idx':-1,'tasks':[{'status':'done'},{'status':'pending'},{'status':'pending'}]}
                path=str(Path(tmp,'report.json'))
                def execute(pending):
                    self.assertEqual(1,state['current_task_idx'])
                    self.assertEqual(['done','running','running'],[t['status'] for t in state['tasks']])
                    self.assertEqual(['one','two'],[f['id'] for f in pending])
                    yield {'fixture_id':'one','repetition':1,'status':'passed'}
                    if mode=='failure': raise ValueError('worker failed')
                    if mode=='cancel':
                        state['cancel']=True
                        raise be.BenchmarkCancelled('cancelled')
                    yield {'fixture_id':'two','repetition':1,'status':'passed'}
                if mode=='failure':
                    with self.assertRaisesRegex(ValueError,'worker failed'):
                        benchmark_runner.run_benchmark_batch(manifest,{},path,state,execute,report=initial)
                    self.assertEqual(['done','done','failed'],[t['status'] for t in state['tasks']])
                elif mode=='cancel':
                    with self.assertRaises(be.BenchmarkCancelled):
                        benchmark_runner.run_benchmark_batch(manifest,{},path,state,execute,report=initial)
                    self.assertEqual(['done','done','cancelled'],[t['status'] for t in state['tasks']])
                else:
                    benchmark_runner.run_benchmark_batch(manifest,{},path,state,execute,report=initial)
                    self.assertEqual(['done']*3,[t['status'] for t in state['tasks']])
                saved=json.loads(Path(path).read_text())
                self.assertEqual(3 if mode=='success' else 2,len(saved['cases']))
                self.assertEqual(1,len(initial['cases']))

    def test_resume_loads_only_pending_sources_and_rejects_pending_drift(self):
        from contextlib import ExitStack
        for stage in ('script_generation','script_review'):
            for mode in ('valid','missing','changed','outside'):
                with self.subTest(stage=stage,mode=mode),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp);paths=[root/'done.txt',root/'pending.txt']
                    raw='example source' if stage=='script_generation' else json.dumps([{'speaker':'ALICE','text':'Hello.'}])
                    fixtures=[]
                    for i,path in enumerate(paths):
                        path.write_text(raw)
                        digest=hashlib.sha256(path.read_bytes()).hexdigest()
                        fixture={'id':str(i),'path':str(path),'sha256':digest}
                        if stage=='script_review': fixture.update(source_sha256=digest,entry_start=1,entry_count=1,sha256=benchmark_runner._hash_entries(json.loads(raw)))
                        fixtures.append(fixture)
                    manifest={'schema_version':1,'stage':stage,'targets':['local'],'repetitions':1,'fixtures':fixtures}
                    env=benchmark_core.build_environment_fingerprint('local',{'hostname':'fixture','gpu_name':'none','backend':'cpu','python_version':'fixture','git_commit':'fixture'})
                    report=benchmark_core.build_benchmark_report(manifest,env)
                    report['cases']=[{'fixture_id':'0','repetition':1,'status':'passed'}]
                    path=root/'report.json';benchmark_core.save_benchmark_report(str(path),report)
                    paths[0].unlink()
                    if mode=='missing': paths[1].unlink()
                    if mode=='changed': paths[1].write_text('changed source')
                    if mode=='outside': allowed=root/'allowed';allowed.mkdir()
                    else: allowed=root
                    state={'cancel':False,'logs':[],'tasks':[{'status':'done'},{'status':'pending'}]}
                    worker_name='_run_script_generation_case' if stage=='script_generation' else '_run_script_review_case'
                    run=benchmark_runner.run_script_generation_benchmark if stage=='script_generation' else benchmark_runner.run_script_review_benchmark
                    with ExitStack() as stack:
                        stack.enter_context(patch.object(benchmark_runner,'load_app_config',return_value={}))
                        stack.enter_context(patch.object(benchmark_runner,'_get_llm_benchmark_target',return_value=({'model_name':'fixture'},{'context_length':4096})))
                        stack.enter_context(patch.object(benchmark_runner,'make_llm_client',return_value=object()))
                        stack.enter_context(patch.object(benchmark_runner,'_measure_llm_network_rtt',return_value=0))
                        worker=stack.enter_context(patch.object(benchmark_runner,worker_name,return_value={'status':'passed'}))
                        if mode=='valid':
                            result=run(manifest,env,str(path),state,'unused',str(allowed))
                            self.assertEqual(2,len(result['cases']))
                            self.assertEqual(1,worker.call_count)
                            self.assertEqual('1',worker.call_args.args[0]['id'])
                        else:
                            with self.assertRaises(ValueError): run(manifest,env,str(path),state,'unused',str(allowed))
                            worker.assert_not_called()


class RemoteDedupCleanupTests(unittest.TestCase):
    def test_actual_staging_files_removed_after_success_transfer_worker_timeout_and_cancel(self):
        import copy
        import shlex
        import shutil
        import subprocess
        from types import SimpleNamespace
        from benchmark_execution import BenchmarkCancelled
        actual_run=subprocess.run
        created=[]
        for mode in ('success','transfer','worker','timeout','cancel'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);dataset=root/'dataset';dataset.mkdir()
                (dataset/'metadata.jsonl').write_text('synthetic metadata')
                fixture={'sha256':'a'*64,'dataset_path':'dataset','audio_sha256':{}}
                original=copy.deepcopy(fixture);paths=[];cleanup=[]
                def stage(command,**kwargs):
                    if command[0]=='scp':
                        target=command[-1].split(':',1)[1]
                        shutil.copyfile(command[1],target)
                        return SimpleNamespace(returncode=int(mode=='transfer'),stderr='transfer refused')
                    args=shlex.split(command[2])
                    result=actual_run(args,capture_output=True,text=True,check=True)
                    if args[0]=='mktemp':
                        path=result.stdout.strip();created.append(path);paths.append(path)
                        result.stdout='SSH banner\n'+result.stdout
                    return result
                def remove(command,**kwargs):
                    args=shlex.split(command[2]);self.assertEqual(['rm','-rf','--'],args[:3])
                    self.assertEqual(paths[0],args[3]);self.assertEqual(30,kwargs['timeout'])
                    self.assertTrue(Path(args[3],'metadata.jsonl').exists())
                    cleanup.append(args[3]);shutil.rmtree(args[3])
                    return SimpleNamespace(returncode=0)
                failures={'transfer':RuntimeError,'worker':ValueError,'timeout':subprocess.TimeoutExpired,'cancel':BenchmarkCancelled}
                error={'worker':ValueError('worker refused'),'timeout':subprocess.TimeoutExpired('worker',7200),
                       'cancel':BenchmarkCancelled('cancelled')}.get(mode)
                worker_result={'status':'passed'}
                try:
                    with patch.object(benchmark_runner,'run_benchmark_subprocess',side_effect=stage), \
                         patch.object(benchmark_runner.subprocess,'run',side_effect=remove), \
                         patch.object(benchmark_runner,'run_benchmark_worker',side_effect=error,return_value=worker_result) as worker:
                        if mode=='success':
                            self.assertEqual(worker_result,benchmark_runner._run_dedup_worker(fixture,'thunder',{'remote_root':'/remote','remote_python':'python3'},str(root),'fixture-host'))
                        else:
                            with self.assertRaises(failures[mode]) as caught:
                                benchmark_runner._run_dedup_worker(fixture,'thunder',{'remote_root':'/remote','remote_python':'python3'},str(root),'fixture-host')
                            if error is not None:self.assertIs(error,caught.exception)
                        if mode=='transfer':worker.assert_not_called()
                    self.assertEqual(paths,cleanup)
                    self.assertFalse(Path(paths[0]).exists())
                    self.assertEqual(original,fixture)
                finally:
                    for path in paths:
                        if Path(path).exists():shutil.rmtree(path)
        self.assertEqual(len(created),len(set(created)))

    def test_cleanup_errors_do_not_replace_primary_failure_and_are_reported(self):
        import subprocess
        from types import SimpleNamespace
        errors=(OSError('SSH unavailable'),subprocess.TimeoutExpired('cleanup',30),None)
        for cleanup_error in errors:
            for primary in (False,True):
                with self.subTest(cleanup_error=cleanup_error,primary=primary):
                    original=ValueError('original worker failure')
                    stage=SimpleNamespace(returncode=0,stdout='/tmp/alexandria-dedup.abcdefghij\n',stderr='')
                    with patch.object(benchmark_runner,'run_benchmark_subprocess',return_value=stage), \
                         patch.object(benchmark_runner,'run_benchmark_worker',side_effect=original if primary else None,return_value={}), \
                         patch.object(benchmark_runner.subprocess,'run',side_effect=cleanup_error,return_value=SimpleNamespace(returncode=1)), \
                         self.assertLogs('benchmark_runner',level='WARNING') as logs:
                        with self.assertRaises(ValueError if primary else RuntimeError) as caught:
                            benchmark_runner._run_dedup_worker({'sha256':'a'*64,'dataset_path':'dataset','audio_sha256':{}},'thunder',{'remote_root':'/remote','remote_python':'python3'},'/fixture','fixture-host')
                    if primary:self.assertIs(original,caught.exception)
                    self.assertIn('cleanup failed',logs.output[0])

    def test_local_dedup_and_unvalidated_remote_paths_are_never_deleted(self):
        from types import SimpleNamespace
        fixture={'sha256':'a'*64,'dataset_path':'dataset','audio_sha256':{}}
        with patch.object(benchmark_runner,'run_benchmark_worker',return_value={'status':'passed'}), \
             patch.object(benchmark_runner.subprocess,'run') as cleanup:
            self.assertEqual({'status':'passed'},benchmark_runner._run_dedup_worker(fixture,'local',{'local_python':sys.executable},'/fixture',None))
            cleanup.assert_not_called()
        for path in ('/tmp','/tmp/alexandria-dedup.abcdefghij/other',''):
            with self.subTest(path=path), \
                 patch.object(benchmark_runner,'run_benchmark_subprocess',return_value=SimpleNamespace(returncode=0,stdout=path,stderr='')), \
                 patch.object(benchmark_runner.subprocess,'run') as cleanup:
                with self.assertRaises(ValueError):benchmark_runner._run_dedup_worker(fixture,'thunder',{'remote_root':'/remote','remote_python':'python3'},'/fixture','fixture-host')
                cleanup.assert_not_called()
