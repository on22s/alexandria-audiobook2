"""Copied complete chains reject bad cached/fresh stop verdicts without inference."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

REPO=Path(__file__).resolve().parents[2]


class LibriQuoteStopGateTests(unittest.TestCase):
    def run_fixture(self,reader,cached,fresh,rc):
        temp=tempfile.TemporaryDirectory(prefix='libriquote gate ');self.addCleanup(temp.cleanup);root=Path(temp.name)
        (root/'run_chains/lib').mkdir(parents=True);(root/'app/experiments').mkdir(parents=True)
        name=f'libriquote_{reader}_20260913.sh';source=Path('/tmp/before_stopgate_'+name) if os.environ.get('LIBRIQUOTE_STOP_BASELINE') else REPO/'run_chains'/name
        shutil.copyfile(source,root/'run_chains'/name)
        for relative in ('run_chains/lib/stage.sh','run_chains/lib/server_cleanup.sh','app/experiments/stop_gate_completion.py'):shutil.copyfile(REPO/relative,root/relative)
        artifacts=[]
        for arm in ('quotes','narration'):
            corpus=root/f'ab_test_runtime/corpora/libriquote/{reader}'/arm;corpus.mkdir(parents=True)
            (corpus/'metadata.csv').write_text('CPU fixture');(corpus/'split.json').write_text('{}')
            work=root/f'ab_test_runtime/libriquote_{reader}_eval'/arm;(work/'adapter').mkdir(parents=True)
            (work/'build.json').write_text('{}');(work/'adapter/adapter_model.safetensors').write_bytes(b'not loaded')
            gate=work/'stop_check/verify_adapter_stops.json';gate.parent.mkdir()
            document=cached[arm]
            if document is not None:gate.write_text(document if isinstance(document,str) else json.dumps(document))
            artifacts.append(gate)
        config=root/'app/config.json';config.write_text('{}')
        (root/'ab_test_runtime/experiments').mkdir(parents=True)
        queue=root/'gpu_job.sh';queue.write_text('#!/bin/bash\nshift\nexec "$@"\n');queue.chmod(0o755)
        worker='''import json,os,pathlib,sys
r=pathlib.Path(os.environ['FIXTURE_ROOT']);name=pathlib.Path(__file__).name
with (r/'calls').open('a') as f:f.write(name+'\\n')
p=pathlib.Path(sys.argv[sys.argv.index('--out')+1]);p.parent.mkdir(parents=True,exist_ok=True)
if name=='verify_adapter_stops.py':
 fresh=os.environ['FRESH'];p.write_text('{broken' if fresh=='malformed' else json.dumps({'passed':fresh=='true'}));raise SystemExit(int(os.environ['GATE_RC']))
p.write_text('{}')
'''
        for name in ('verify_adapter_stops.py','ljspeech_generate.py','prosody_fidelity.py','ljspeech_score.py'):(root/'app/experiments'/name).write_text(worker)
        for args in (('init','-q','-b','main'),('config','user.name','Fixture'),('config','user.email','fixture@example.com'),('config','core.hooksPath',str(root/'no-hooks')),('add','-A'),('commit','-q','-m','fixture baseline')):subprocess.run(['git','-C',str(root),*args],check=True,capture_output=True)
        head=subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()
        env=dict(os.environ,FIXTURE_ROOT=str(root),PYTHON=sys.executable,CONFIG=str(config),FRESH=str(fresh).lower(),GATE_RC=str(rc))
        before={p:p.read_bytes() for p in artifacts if p.exists()}
        result=subprocess.run(['bash',str(root/'run_chains'/f'libriquote_{reader}_20260913.sh')],cwd=root.parent,env=env,capture_output=True,text=True,timeout=15)
        calls=(root/'calls').read_text().splitlines() if (root/'calls').exists() else []
        committed=subprocess.check_output(['git','-C',str(root),'diff','--name-only',head,'HEAD'],text=True).splitlines()
        return result,calls,artifacts,before,committed

    def test_failed_missing_malformed_and_boolean_like_verdicts_never_bypass_gate(self):
        cases=(({'passed':False},False,3,False),('{bad',False,0,False),([],False,0,False),({'passed':1},False,0,False),
            ({'passed':'true'},'malformed',0,False),(None,False,3,False),({'passed':False},True,3,False),
            ({'passed':False},True,0,True))
        for reader in (2033,4992):
            for cached,fresh,rc,allowed in cases:
                with self.subTest(reader=reader,cached=cached,fresh=fresh,rc=rc):
                    result,calls,artifacts,before,committed=self.run_fixture(reader,{'quotes':{'passed':True},'narration':cached},fresh,rc)
                    self.assertEqual(0 if allowed else 1,result.returncode,result.stdout+result.stderr)
                    self.assertEqual(1,calls.count('verify_adapter_stops.py'),calls)
                    self.assertEqual(4 if allowed else 0,calls.count('ljspeech_generate.py'),calls)
                    self.assertEqual(4 if allowed else 0,calls.count('prosody_fidelity.py'),calls)
                    self.assertEqual(4 if allowed else 0,calls.count('ljspeech_score.py'),calls)
                    self.assertEqual(before[artifacts[0]],artifacts[0].read_bytes())
                    if not allowed:self.assertIn('REFUSING',result.stdout);self.assertTrue(artifacts[1].exists())

    def test_exact_passing_caches_skip_gate_preserve_evidence_and_allow_all_four_cross_generations(self):
        for reader in (2033,4992):
            with self.subTest(reader=reader):
                result,calls,artifacts,before,committed=self.run_fixture(reader,{'quotes':{'passed':True},'narration':{'passed':True}},False,3)
                self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                self.assertNotIn('verify_adapter_stops.py',calls);self.assertEqual(4,calls.count('ljspeech_generate.py'))
                self.assertEqual(12,len(committed),committed)
                self.assertTrue(all(path.startswith('ab_test_runtime/experiments/') for path in committed))
                self.assertEqual(before,{p:p.read_bytes() for p in artifacts})
