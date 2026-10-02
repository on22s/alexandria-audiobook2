"""Run complete chains in a disposable Git checkout with CPU worker stand-ins."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO=Path(__file__).resolve().parents[2]
PUBLIC=('TheGambler','TheSignOfTheFour','TheMysteriousAffairAtStyles','AHandfulOfDust')
PRIVATE=('index18','mushoku16','owarimonogatari3')


class ChainRequiredInputsTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        self.root=Path(tmp.name)/'repository with spaces'
        for directory in ('run_chains/lib','app/env/bin','app/fixtures','app/experiments',
                          'bin','ab_test_runtime/experiments'):
            (self.root/directory).mkdir(parents=True)
        for relative in ('run_chains/dialogue_map_5_3_20260826.sh','run_chains/longref_arm_20260826.sh',
                         'run_chains/lib/stage.sh','run_chains/lib/server_cleanup.sh'):
            shutil.copyfile(REPO/relative,self.root/relative)
        self.write('gpu_job.sh','#!/bin/bash\nshift\nexec "$@"\n')
        self.write('bin/pgrep','#!/bin/sh\nexit 1\n')
        self.write('app/env/bin/python','#!'+sys.executable+'\n'+r'''
import json,os,pathlib,sys
args=sys.argv[1:]
if args[0]=='-u':args=args[1:]
name=pathlib.Path(args[0]).name
if name=='check_artifact_shrinkage.py':sys.exit(0)
with open(os.environ['FIXTURE_CALLS'],'a') as stream:stream.write(json.dumps(args)+'\n')
if name=='ljspeech_generate.py' and os.environ.get('FAIL_JA')=='1' and 'kokoro_eval' in args[args.index('--build')+1]:sys.exit(4)
if '--out' in args:
 output=pathlib.Path(args[args.index('--out')+1]);output.parent.mkdir(parents=True,exist_ok=True);output.write_text(json.dumps({'fixture':'current generation'}))
''')
        self.env=dict(os.environ,PATH=str(self.root/'bin')+os.pathsep+os.environ['PATH'],
                      FIXTURE_CALLS=str(self.root/'calls'))
        for script in ('dialogue_map_compare.py','script_text_fidelity.py','retrofit_dialogue_map.py'):
            self.write('app/experiments/'+script,'# fixture dispatch')
        self.write('app/three_pass_generate.py','# dialogue_spans fixture')
        for book in PUBLIC:
            self.write('ab_test_runtime/pdnc/data/'+book+'/novel_text.txt','Current public source '+book)
            self.write('app/fixtures/attribution_gold_pdnc_'+book.lower()+'.json','{}')
        for book in PRIVATE:self.write('ab_test_runtime/tpvs_inputs/'+book+'.txt','Current private source '+book)
        for name in ('ljspeech_eval','kokoro_eval','aishell3_eval'):
            self.write('ab_test_runtime/'+name+'/build_longref.json','{"fixture":"current build"}')
        for lang in ('en','ja','zh'):
            self.write('ab_test_runtime/experiments/longref__'+lang+'_generate.json','{"fixture":"STALE generation"}')
        for args in (('init','-q','-b','main'),('config','user.name','Fixture'),
                     ('config','user.email','fixture@example.com'),('config','core.hooksPath',str(self.root/'no-hooks')),
                     ('add','.'),('commit','-qm','fixture baseline')):
            subprocess.run(['git','-C',str(self.root),*args],check=True,capture_output=True)

    def write(self, relative, text):
        path=self.root/relative;path.parent.mkdir(parents=True,exist_ok=True)
        path.write_text(text);path.chmod(0o755)
        return path

    def run_chain(self, name):
        result=subprocess.run(['bash',str(self.root/'run_chains'/name)],env=self.env,cwd=self.root,
                              capture_output=True,text=True,timeout=15)
        calls=self.root/'calls'
        return result,[json.loads(line) for line in calls.read_text().splitlines()] if calls.exists() else []

    def test_missing_public_source_refuses_even_with_stale_staged_copy(self):
        missing=self.root/'ab_test_runtime/pdnc/data/TheGambler/novel_text.txt';missing.unlink()
        staged=self.write('ab_test_runtime/dialogue_map_5_3_inputs/pdnc_thegambler.txt','STALE staged source')
        result,calls=self.run_chain('dialogue_map_5_3_20260826.sh')
        self.assertNotEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual([],calls)
        self.assertIn(str(missing),result.stdout+result.stderr)
        self.assertEqual('STALE staged source',staged.read_text())

    def test_missing_private_source_refuses_without_using_stale_staged_copy(self):
        missing=self.root/'ab_test_runtime/tpvs_inputs/mushoku16.txt';missing.unlink()
        staged=self.write('ab_test_runtime/dialogue_map_5_3_inputs/mushoku16.txt','STALE private source')
        result,calls=self.run_chain('dialogue_map_5_3_20260826.sh')
        self.assertNotEqual(0,result.returncode,result.stdout+result.stderr);self.assertEqual([],calls)
        self.assertIn('mushoku16',result.stdout+result.stderr)
        self.assertEqual('STALE private source',staged.read_text())

    def test_copy_failure_refuses_before_worker_and_preserves_source(self):
        self.write('bin/cp','#!/bin/sh\necho "fixture copy failure" >&2\nexit 9\n')
        original=(self.root/'ab_test_runtime/pdnc/data/TheGambler/novel_text.txt').read_bytes()
        result,calls=self.run_chain('dialogue_map_5_3_20260826.sh')
        self.assertNotEqual(0,result.returncode,result.stdout+result.stderr);self.assertEqual([],calls)
        self.assertEqual(original,(self.root/'ab_test_runtime/pdnc/data/TheGambler/novel_text.txt').read_bytes())

    def test_valid_inputs_stage_exactly_seven_current_books(self):
        result,calls=self.run_chain('dialogue_map_5_3_20260826.sh')
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertTrue(any(Path(args[0]).name=='three_pass_vs_single.py' for args in calls))
        staged=self.root/'ab_test_runtime/dialogue_map_5_3_inputs'
        for book in PUBLIC:
            self.assertEqual((self.root/'ab_test_runtime/pdnc/data'/book/'novel_text.txt').read_bytes(),
                             (staged/('pdnc_'+book.lower()+'.txt')).read_bytes())
        for book in PRIVATE:
            self.assertEqual((self.root/'ab_test_runtime/tpvs_inputs'/(book+'.txt')).read_bytes(),(staged/(book+'.txt')).read_bytes())

    def test_missing_longref_build_refuses_all_workers_and_preserves_stale_manifest(self):
        missing=self.root/'ab_test_runtime/kokoro_eval/build_longref.json';missing.unlink()
        stale=self.root/'ab_test_runtime/experiments/longref__ja_generate.json';before=stale.read_bytes()
        result,calls=self.run_chain('longref_arm_20260826.sh')
        self.assertNotEqual(0,result.returncode,result.stdout+result.stderr);self.assertEqual([],calls)
        self.assertIn(str(missing),result.stdout+result.stderr);self.assertEqual(before,stale.read_bytes())

    def test_failed_longref_generation_cannot_score_old_manifest_or_return_success(self):
        self.env['FAIL_JA']='1'
        result,calls=self.run_chain('longref_arm_20260826.sh')
        self.assertNotEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertEqual(3,sum(Path(args[0]).name=='ljspeech_generate.py' for args in calls))
        self.assertFalse(any(Path(args[0]).name=='pitch_quality_probe.py' for args in calls))

    def test_all_longref_generations_succeed_before_quality_reads_all_three(self):
        result,calls=self.run_chain('longref_arm_20260826.sh')
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        names=[Path(args[0]).name for args in calls]
        self.assertEqual(['ljspeech_generate.py']*3,names[:3])
        quality=next(args for args in calls if Path(args[0]).name=='pitch_quality_probe.py')
        self.assertEqual(['en=longref__en_generate.json','ja=longref__ja_generate.json','zh=longref__zh_generate.json'],
                         [quality[i+1] for i,arg in enumerate(quality) if arg=='--manifest'])
        for lang in ('en','ja','zh'):
            self.assertEqual('current generation',json.loads((self.root/'ab_test_runtime/experiments'/('longref__'+lang+'_generate.json')).read_text())['fixture'])
