"""Actual e-row dispatch and artifacts with isolated CPU worker providers."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

REPO=Path(__file__).resolve().parents[2]


class ERowCacheTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory();self.addCleanup(temp.cleanup)
        self.root=Path(temp.name)/'repo with spaces'
        for directory in ('run_chains/lib','app/env/bin','app/experiments','bin','ab_test_runtime/experiments'):(self.root/directory).mkdir(parents=True)
        for relative in ('run_chains/e_row_arms_20260817.sh','run_chains/e_row_replication_20260818.sh','run_chains/lib/queue.sh','app/experiments/respelling_completion.py'):
            shutil.copyfile(REPO/relative,self.root/relative)
        for name,override in (('e_row_arms_20260817.sh','E_ROW_ARMS_SOURCE'),('e_row_replication_20260818.sh','E_ROW_REPLICATION_SOURCE')):
            if os.environ.get(override):
                source=Path(os.environ[override]).read_text().replace('REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git','REPO='+shlex.quote(str(self.root)))
                (self.root/'run_chains'/name).write_text(source)
        (self.root/'app/env/bin/python').symlink_to(sys.executable)
        wrapper=self.root/'gpu_job.sh';wrapper.write_text('#!/bin/bash\necho "$1" >> "$FIXTURE_ROOT/jobs"\nshift\nexec "$@"\n');wrapper.chmod(0o755)
        waiter=self.root/'bin/pgrep';waiter.write_text('#!/bin/sh\nexit 1\n');waiter.chmod(0o755)
        (self.root/'app/experiments/measure_respellings.py').write_text('''import json,os,pathlib,sys,time
args=sys.argv;root=pathlib.Path(os.environ['FIXTURE_ROOT'])
limit=int(args[args.index('--limit')+1]);out=pathlib.Path(args[args.index('--out')+1])
mode=os.environ.get('FIXTURE_MODE','success')
if mode=='timeout':time.sleep(10)
if mode=='noop':raise SystemExit(0)
if out.exists() and json.loads(out.read_text()).get('run_identity',{}).get('limit',limit)!=limit:raise SystemExit(2)
rows=limit-1 if mode=='partial' else limit
out.write_text(json.dumps({'status':'partial' if mode=='partial' else 'complete','candidates_considered':limit,'run_identity':{'limit':limit},'results':[{'term':'term'+str(i)} for i in range(rows)]}))
if mode=='fail_full':raise SystemExit(7)
''')
        (self.root/'app/experiments/pair_e_row.py').write_text('''import json,os,pathlib,sys
root=pathlib.Path(os.environ['FIXTURE_ROOT']);d=json.loads(pathlib.Path(sys.argv[1]).read_text())
with (root/'scores').open('a') as f:f.write(str(d['candidates_considered'])+'\\n')
raise SystemExit(4 if os.environ.get('FIXTURE_MODE')=='score_fail' else 0)
''')
        self.env=dict(os.environ,FIXTURE_ROOT=str(self.root),PATH=str(self.root/'bin')+os.pathsep+os.environ['PATH'],E_ROW_DEADLINE=str(int(time.time())+20000))
        self.artifacts=self.root/'ab_test_runtime/experiments'

    def put(self,name,count):
        path=self.artifacts/name
        path.write_text(json.dumps({'status':'complete','candidates_considered':count,'run_identity':{'limit':count},'results':[{'term':'term'+str(i)} for i in range(count)]}))
        return path

    def run_chain(self,name,**env):
        return subprocess.run(['bash',str(self.root/'run_chains'/name)],cwd=self.root.parent,env=dict(self.env,**env),capture_output=True,text=True,timeout=15)

    def test_widened_run_preserves_previous_measurement_and_resumes_each_scoped_sample(self):
        old={s:self.put('respelling_e_row__'+s+'.json',400).read_bytes() for s in ('e','ay','ei')}
        result=self.run_chain('e_row_arms_20260817.sh',E_ROW_LIMIT='800')
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(3,len((self.root/'jobs').read_text().splitlines()))
        for spelling in old:
            self.assertEqual(old[spelling],(self.artifacts/('respelling_e_row__'+spelling+'.json')).read_bytes())
            scoped=self.artifacts/('respelling_e_row__'+spelling+'_n800.json')
            self.assertTrue(scoped.exists(),'widened run failed to publish its distinct sample artifact')
            new=json.loads(scoped.read_text())
            self.assertEqual(800,len(new['results']))
        before=(self.root/'jobs').read_bytes()
        result=self.run_chain('e_row_arms_20260817.sh',E_ROW_LIMIT='800')
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(before,(self.root/'jobs').read_bytes())
        self.assertIn('SKIP ay',result.stdout)
        result=self.run_chain('e_row_arms_20260817.sh',E_ROW_LIMIT='400')
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(before,(self.root/'jobs').read_bytes())
        self.assertIn('legacy artifact covers requested limit',result.stdout)

    def test_failed_or_incomplete_generation_never_scores_but_attempts_other_blocks(self):
        for mode in ('fail_full','partial','timeout','noop'):
            with self.subTest(mode=mode):
                for p in self.artifacts.glob('*.json'):p.unlink()
                for marker in ('jobs','scores'):
                    p=self.root/marker
                    if p.exists():p.unlink()
                # Disposable fixture cap only; production keeps its 4200s cap.
                chain=self.root/'run_chains/e_row_replication_20260818.sh'
                chain.write_text(chain.read_text().replace('BLOCK_SECONDS=4200','BLOCK_SECONDS=1'))
                result=self.run_chain(chain.name,FIXTURE_MODE=mode)
                self.assertFalse((self.root/'scores').exists(),'failed or incomplete generation reached scoring')
                self.assertEqual(1,result.returncode,result.stdout+result.stderr)
                self.assertEqual(3,len((self.root/'jobs').read_text().splitlines()))
                self.assertIn('REPLICATION INCOMPLETE',result.stdout)
                if mode=='timeout':self.assertIn('rc=124',result.stdout)

    def test_only_complete_successful_blocks_score_and_scoring_failure_is_not_success(self):
        result=self.run_chain('e_row_replication_20260818.sh')
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual('800\n1200\n1600\n',(self.root/'scores').read_text())
        for p in self.artifacts.glob('*.json'):p.unlink()
        (self.root/'scores').unlink()
        result=self.run_chain('e_row_replication_20260818.sh',FIXTURE_MODE='score_fail')
        self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        self.assertEqual('800\n1200\n1600\n',(self.root/'scores').read_text())
        self.assertIn('3 blocks failed',result.stdout)
