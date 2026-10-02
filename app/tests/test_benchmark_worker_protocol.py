"""Shared wire protocol keeps stage shapes and reports setup/serialization failures."""
import base64
import contextlib
import importlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from benchmark_execution import BenchmarkCancelled
import benchmark_worker_protocol as protocol

WORKERS = ('tts_benchmark','export_benchmark','dataset_builder_benchmark','profiling_benchmark',
           'naming_benchmark','dedup_benchmark','lora_training_benchmark','preparer_benchmark')
METRICS = {'dedup_benchmark','lora_training_benchmark','preparer_benchmark'}
PAYLOAD = {'fixture':{},'python':'fixture','analysis_script':'fixture','train_script':'fixture',
           'output_root':'fixture','preparer_script':'fixture'}


class WorkerProtocolTests(unittest.TestCase):
    def test_native_structured_failure_obeys_caller_error_bound(self):
        import benchmark_runner
        marker = 'LLM_BENCHMARK_RESULT='
        script = "import json;print(%r+json.dumps({'status':'failed','error':'x'*8192+'tail'}))" % marker
        with self.assertRaises(RuntimeError) as failure:
            benchmark_runner.run_benchmark_worker([sys.executable, '-c', script], marker,
                'worker failed', timeout=5, error_limit=32, raise_failed=True)
        self.assertEqual(str(failure.exception), 'x' * 28 + 'tail')

    def invoke(self, name, result=None, error=None, encoded=None):
        module = importlib.import_module(name)
        marker = name.removesuffix('_benchmark').upper() + '_BENCHMARK_RESULT='
        argv = [name,'--payload',encoded if encoded is not None else protocol.get_encoded_worker_payload(PAYLOAD)]
        if name == 'tts_benchmark':argv += ['--output-dir','unused']
        target = 'execute_fixture' if name in METRICS else 'execute_payload'
        output = io.StringIO()
        with patch.object(sys,'argv',argv),patch.object(module,target,return_value=result,side_effect=error),contextlib.redirect_stdout(output):
            module.main()
        lines = [line for line in output.getvalue().splitlines() if line.startswith(marker)]
        self.assertEqual(1,len(lines),output.getvalue())
        return json.loads(lines[0][len(marker):])

    def test_all_worker_mains_preserve_success_shapes_and_emit_uniform_failures(self):
        for name in WORKERS:
            with self.subTest(worker=name):
                value = [{'fixture_id':'f','status':'failed','error':'case failed'}] if name=='tts_benchmark' else {'elapsed_seconds':1,'name':'日本語'}
                expected = {'status':'passed','metrics':value,'error':None} if name in METRICS else value
                self.assertEqual(expected,self.invoke(name,result=value))
                self.assertEqual({'status':'failed','metrics':{},'error':'setup failed'},self.invoke(name,error=RuntimeError('setup failed')))
                self.assertEqual('failed',self.invoke(name,encoded='!!!!')['status'])
                self.assertEqual('failed',self.invoke(name,result={'not_json':{1,2}})['status'])
                with self.assertRaises(BenchmarkCancelled):self.invoke(name,error=BenchmarkCancelled())

    def test_payload_roundtrip_and_last_marker_parser_preserve_diagnostics(self):
        payload={'text':'日本語 café','nested':{'value':[1,None,True]}}
        original=json.dumps(payload)
        self.assertEqual(payload,protocol.get_decoded_worker_payload(protocol.get_encoded_worker_payload(payload)))
        self.assertEqual(original,json.dumps(payload))
        marker='FIXTURE_RESULT='
        result=subprocess.CompletedProcess([],0,'banner\n'+marker+'{"old":true}\nnoise\n'+marker+'{"new":true}\n','')
        self.assertEqual({'new':True},protocol.get_benchmark_worker_result(result,marker,'failed'))
        result.stdout=marker+'{"status":"failed","metrics":{},"error":"setup broke"}'
        self.assertEqual('failed',protocol.get_benchmark_worker_result(result,marker,'failed')['status'])
        with self.assertRaisesRegex(RuntimeError,'setup broke'):
            protocol.get_benchmark_worker_result(result,marker,'failed',raise_failed=True)
        result.returncode=7;result.stderr='PREFIX-last-error'
        with self.assertRaisesRegex(RuntimeError,'last-error'):
            protocol.get_benchmark_worker_result(result,marker,'failed',error_limit=10)
        result.returncode=0;result.stderr='';result.stdout='banner without marker'
        with self.assertRaises(RuntimeError):protocol.get_benchmark_worker_result(result,marker,'failed')
        result.stdout=marker+'{malformed'
        with self.assertRaises(json.JSONDecodeError):protocol.get_benchmark_worker_result(result,marker,'failed')

    def test_llm_stdin_main_keeps_cancellation_marker_and_reports_decode_failures(self):
        import llm_benchmark_worker as worker
        argv=['worker','--stage','fixture','--payload-stdin']
        for value in ('{invalid', '{}'):
            output=io.StringIO()
            error=BenchmarkCancelled() if value=='{}' else AssertionError('invalid JSON reached execution')
            with patch.object(sys,'argv',argv),patch.object(sys,'stdin',io.StringIO(value)), \
                 patch.object(worker,'execute_payload',side_effect=error),contextlib.redirect_stdout(output):worker.main()
            if value=='{}':self.assertEqual('LLM_BENCHMARK_CANCELLED\n',output.getvalue())
            else:self.assertEqual('failed',json.loads(output.getvalue().split('=',1)[1])['status'])

    def test_native_worker_decode_failures_emit_one_machine_readable_result(self):
        self.native_results=[]
        with tempfile.TemporaryDirectory() as tmp:
            env=dict(os.environ,ALEXANDRIA_DATA_DIR=tmp)
            for name in (*WORKERS,'llm_benchmark_worker'):
                with self.subTest(worker=name):
                    path=Path(__file__).resolve().parent.parent/(name+'.py')
                    args=['--stage','fixture','--payload-stdin'] if name=='llm_benchmark_worker' else ['--payload','!!!!']
                    if name=='tts_benchmark':args+=['--output-dir',str(Path(tmp,'audio'))]
                    result=subprocess.run([sys.executable,str(path),*args],input='{invalid' if name=='llm_benchmark_worker' else None,
                                          capture_output=True,text=True,env=env,timeout=20)
                    self.assertEqual(0,result.returncode,result.stderr)
                    marker='LLM_BENCHMARK_RESULT=' if name=='llm_benchmark_worker' else name.removesuffix('_benchmark').upper()+'_BENCHMARK_RESULT='
                    lines=[line for line in result.stdout.splitlines() if line.startswith(marker)]
                    self.assertEqual(1,len(lines),result.stdout)
                    outcome=json.loads(lines[0][len(marker):]);self.assertEqual('failed',outcome['status']);self.assertEqual({},outcome['metrics']);self.assertTrue(outcome['error'])
                    self.native_results.append({'worker':name,'returncode':result.returncode,'result':outcome})
