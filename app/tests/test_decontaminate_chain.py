"""Disposable native Bash runs; no queue, training, model calls, or live artifacts."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO = Path(__file__).resolve().parents[2]
SOURCE = Path(os.environ.get('DECONTAMINATE_SOURCE', REPO/'run_chains/decontaminate_library.sh'))


class DecontaminateChainTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)/'selected checkout'
        self.root.mkdir()
        (self.root/'run_chains').mkdir()
        source = SOURCE.read_text().replace('REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git',
                                             'REPO="'+str(self.root)+'"')
        # The saved baseline's other hard-coded summary paths deliberately stay intact.
        self.chain = self.root/'run_chains/decontaminate_library.sh'
        self.chain.write_text(source)
        app = self.root/'app'
        (app/'env/bin').mkdir(parents=True)
        (app/'env/bin/python').symlink_to(sys.executable)
        (app/'experiments').mkdir()
        (self.root/'gpu_job.sh').write_text('#!/bin/bash\n[ "$1" = --check-lock-owner ]\n')
        runtime = self.root/'ab_test_runtime'
        (runtime/'experiments').mkdir(parents=True)
        (runtime/'logs').mkdir()
        self.list = runtime/'contaminated_adapters.txt'
        self.calls = self.root/'calls.jsonl'
        self.names = [f'adapter_{index}' for index in range(12)]
        for name in self.names:
            (self.root/'lora_models'/name).mkdir(parents=True)
        self.list.write_text('\n'.join(self.names)+'\n')
        self.baseline = runtime/'experiments/library_voice_fidelity_n10.json'
        self.baseline.write_text(json.dumps({'results':[{'adapter':name,'ecapa':0.1} for name in self.names]}))
        (app/'experiments/retrain_honest.py').write_text('''import argparse,json,os,sys
from pathlib import Path
p=argparse.ArgumentParser()
p.add_argument('--adapters',nargs='+');p.add_argument('--use-medoid',action='store_true')
p.add_argument('--resume',action='store_true');p.add_argument('--work');p.add_argument('--out');p.add_argument('--check-artifact')
a=p.parse_args()
with open(os.environ['CHAIN_CALLS'],'a') as f:f.write(json.dumps(vars(a))+'\\n')
if a.check_artifact:
 d=json.loads(Path(a.check_artifact).read_text())
 sys.exit(0 if d.get('complete') and [r['adapter'] for r in d.get('results',[])]==a.adapters else 1)
if os.environ.get('FAIL_BATCH')=='all' or os.environ.get('FAIL_BATCH')==Path(a.out).stem:sys.exit(124)
if os.environ.get('FORGE_BATCH')==Path(a.out).stem:
 Path(a.out).write_text(json.dumps({'complete':False,'results':[]}));sys.exit(0)
if a.resume and Path(a.out).exists():sys.exit(0)
Path(a.out).write_text(json.dumps({'complete':True,'results':[{'adapter':n,'new_ecapa_heldout':0.8} for n in a.adapters]}))
''')
        self.env = {**os.environ,'PYTHONPATH':str(REPO/'app'),'ALEXANDRIA_GPU_LOCK_HELD':'1',
                    'ALEXANDRIA_GPU_LOCK_PID':'fixture','CHAIN_CALLS':str(self.calls)}

    def run_chain(self, **env):
        return subprocess.run(['bash',str(self.chain)],cwd=self.root,env={**self.env,**env},
            capture_output=True,text=True,timeout=15)

    def dispatched(self):
        return [json.loads(line) for line in self.calls.read_text().splitlines()] if self.calls.exists() else []

    def test_failures_and_timeouts_attempt_every_batch_and_cannot_report_done(self):
        result = self.run_chain(FAIL_BATCH='all')
        self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        calls = self.dispatched()
        self.assertEqual([self.names[:10],self.names[10:]],[r['adapters'] for r in calls])
        self.assertTrue(all(r['resume'] and r['use_medoid'] for r in calls))
        self.assertIn('INCOMPLETE',result.stdout)
        self.assertNotIn('DECONTAMINATION DONE',result.stdout)

    def test_selected_checkout_summary_ignores_stale_extra_batch_and_resume_keeps_bytes(self):
        extra = self.root/'ab_test_runtime/experiments/decontaminate_batch99.json'
        extra.write_text(json.dumps({'results':[{'adapter':'unrequested','new_ecapa_heldout':0.99}]}))
        result = self.run_chain()
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertIn('retrained: 12',result.stdout)
        self.assertIn('DECONTAMINATION DONE',result.stdout)
        self.assertNotIn('unrequested',result.stdout)
        reports = list((self.root/'ab_test_runtime/experiments').glob('decontaminate_batch*.json'))
        before = {p.name:p.read_bytes() for p in reports}
        self.calls.unlink()
        result = self.run_chain()
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(before,{p.name:p.read_bytes() for p in reports})
        calls = self.dispatched()
        self.assertEqual(2,len([r for r in calls if r['check_artifact']]))
        self.assertTrue(all(r['resume'] for r in calls))

    def test_lists_filter_comments_and_blanks_and_invalid_inputs_fail_before_training(self):
        self.list.write_text('  # header\n\n  adapter_0  \n\t\nadapter_1\n')
        result = self.run_chain()
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(['adapter_0','adapter_1'],self.dispatched()[0]['adapters'])
        for content in (None,'# no adapters\n\n','../escape\n','missing_adapter\n','adapter_0\nadapter_0\n'):
            with self.subTest(content=content):
                self.calls.unlink(missing_ok=True)
                if content is None:self.list.unlink(missing_ok=True)
                else:self.list.write_text(content)
                result = self.run_chain()
                self.assertEqual(1,result.returncode,result.stdout+result.stderr)
                self.assertIn('Invalid adapter list',result.stderr)
                self.assertFalse(self.dispatched())

    def test_zero_exit_incomplete_artifact_and_summary_failure_are_fatal(self):
        result = self.run_chain(FORGE_BATCH='decontaminate_batch1')
        self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        self.assertEqual(2,len([r for r in self.dispatched() if not r['check_artifact']]))
        self.assertIn('retrained: 2',result.stdout)
        self.baseline.unlink()
        result = self.run_chain()
        self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        self.assertIn('INCOMPLETE',result.stdout)
