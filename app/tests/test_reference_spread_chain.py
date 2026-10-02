"""Native copied driver/Git/score reader with independent CPU arm producers."""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO=Path(__file__).resolve().parents[2]
SOURCE=Path(os.environ.get('REFERENCE_SPREAD_SOURCE',str(REPO/'run_chains/reference_spread_20260821.sh')))


class ReferenceSpreadChainTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(prefix='reference spread ');self.addCleanup(temp.cleanup);self.root=Path(temp.name)
        for folder in ('run_chains/lib','app/env/bin','app/experiments','ab_test_runtime/experiments','ab_test_runtime/ljspeech_eval/adapter','bin'):(self.root/folder).mkdir(parents=True)
        for name in ('stage.sh','server_cleanup.sh'):shutil.copyfile(REPO/'run_chains/lib'/name,self.root/'run_chains/lib'/name)
        shutil.copyfile(SOURCE,self.root/'run_chains/chain.sh')
        (self.root/'ab_test_runtime/ljspeech_eval/build.json').write_text('{}')
        for name,body in (('pkill','touch "$FIXTURE_ROOT/unsafe_stop"; exit 0'),('sleep','exit 0')):
            p=self.root/'bin'/name;p.write_text('#!/bin/sh\n'+body+'\n');p.chmod(0o755)
        p=self.root/'app/env/bin/python';p.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' "$@"\n');p.chmod(0o755)
        p=self.root/'gpu_job.sh';p.write_text('#!/bin/bash\necho "${GPU_RECLAIM_VRAM:-0} ${REQUIRE_VRAM_GB:-missing}" >> "$FIXTURE_ROOT/admission"\nshift\nexec "$@"\n');p.chmod(0o755)
        worker='''import json,os,pathlib,sys
r=pathlib.Path(os.environ['FIXTURE_ROOT']);name=pathlib.Path(__file__).name;args=sys.argv;mode=os.environ.get('FIXTURE_FAIL','')
p=pathlib.Path(args[args.index('--out')+1]);p.parent.mkdir(parents=True,exist_ok=True)
if name=='reference_spread.py':
 folder=pathlib.Path(args[args.index('--out-dir')+1]);folder.mkdir(exist_ok=True)
 arms=[]
 for i in range(4):
  b=folder/('build_spread'+str(i)+'.json');b.write_text('{}');arms.append({'arm':i,'distance':float(i),'seconds':12,'measures':{}})
 p.write_text(json.dumps({'arms':arms}));raise SystemExit(7 if mode=='build' else 0)
arm=int(p.stem.split('arm')[-1]);stage='G' if name=='ljspeech_generate.py' else 'S'
with (r/'events').open('a') as f:f.write(stage+str(arm)+'\\n')
if stage=='G':p.write_text('{}');raise SystemExit(7 if mode=='gen'+str(arm) else 0)
value=None if mode=='unusable'+str(arm) else .7-.05*arm
p.write_text(json.dumps({'summary':{'clone':{'ecapa':value,'n':2}}}))
raise SystemExit(4 if mode=='score'+str(arm) or (mode=='only_one' and arm>0) else 0)
'''
        for name in ('reference_spread.py','ljspeech_generate.py','ljspeech_score.py'):(self.root/'app/experiments'/name).write_text(worker)
        compare=self.root/'app/experiments/reference_spread_compare.py';compare.write_text('''import json,os,pathlib,runpy,sys
if '--check-score' not in sys.argv:
 r=pathlib.Path(os.environ['FIXTURE_ROOT']);(r/'compared').write_text(json.dumps(sys.argv))
runpy.run_path(os.environ['REAL_COMPARE'],run_name='__main__')
''')
        old=self.root/'ab_test_runtime/experiments/reference_spread__en_score_arm1.json';old.write_text('{"summary":{"clone":{"ecapa":0.8,"n":2}}}')
        for args in (('init','-q','-b','main'),('config','user.name','Fixture'),('config','user.email','fixture@example.com'),('config','core.hooksPath',str(self.root/'no-hooks')),('add','-A'),('commit','-q','-m','fixture base')):subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True)
        self.env=dict(os.environ,FIXTURE_ROOT=str(self.root),REAL_COMPARE=str(REPO/'app/experiments/reference_spread_compare.py'),PYTHONPATH=str(REPO/'app'),PATH=str(self.root/'bin')+os.pathsep+os.environ['PATH'])

    def run_chain(self,**env):
        for name in ('events','compared','admission'):
            p=self.root/name
            if p.exists():p.unlink()
        return subprocess.run(['bash',str(self.root/'run_chains/chain.sh')],cwd=self.root.parent,env=dict(self.env,**env),capture_output=True,text=True,timeout=15)

    def test_all_generation_precedes_cpu_scoring_and_each_admission_requests_supervised_four_gb(self):
        result=self.run_chain();self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(['G0','G1','G2','G3','S0','S1','S2','S3'],(self.root/'events').read_text().splitlines())
        self.assertEqual(['1 4']*4,(self.root/'admission').read_text().splitlines());self.assertFalse((self.root/'unsafe_stop').exists())
        doc=json.loads((self.root/'ab_test_runtime/experiments/reference_spread__en_compare.json').read_text());self.assertEqual([0,1,2,3],[r['arm'] for r in doc['arms']])

    def test_failed_generation_failed_scoring_and_invalid_numeric_output_are_excluded_from_comparison(self):
        for mode in ('gen1','score1','unusable1'):
            with self.subTest(mode=mode):
                result=self.run_chain(FIXTURE_FAIL=mode);self.assertEqual(1,result.returncode,result.stdout+result.stderr)
                doc=json.loads((self.root/'ab_test_runtime/experiments/reference_spread__en_compare.json').read_text());self.assertEqual([0,2,3],[r['arm'] for r in doc['arms']])
                events=(self.root/'events').read_text().splitlines();self.assertEqual(['G0','G1','G2','G3'],events[:4])
                self.assertEqual(mode!='gen1','S1' in events)

    def test_only_one_successful_score_does_not_launch_comparison(self):
        result=self.run_chain(FIXTURE_FAIL='only_one');self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        self.assertFalse((self.root/'compared').exists())

    def test_failed_builder_does_not_consume_its_manifest_or_admit_any_gpu_worker(self):
        result=self.run_chain(FIXTURE_FAIL='build');self.assertEqual(1,result.returncode,result.stdout+result.stderr)
        self.assertFalse((self.root/'events').exists());self.assertFalse((self.root/'admission').exists())
