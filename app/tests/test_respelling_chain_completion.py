"""Real chain guard expressions share counts and preserve unreadable refusal."""
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys
import tempfile
import unittest

REPO=Path(__file__).resolve().parents[2]


class RespellingChainCompletionTests(unittest.TestCase):
    def test_rule_b_and_morning_reject_complete_marker_without_complete_rows(self):
        for name,count in (('rule_b_2026_08_17.sh',0),('morning_20260818.sh',800)):
            source=(Path('/tmp/before320_'+name) if os.environ.get('RESPELLING_320_BASELINE') else REPO/'run_chains'/name).read_text()
            if name.startswith('rule_b'):
                a=source.index('artifact_complete() {');b=source.index('\n}',a)+2
                guard=source[a:b]+'\nartifact_complete "$python" "$out"\n'
            else:
                a=source.index('    if [ -e "$out" ]');b=source.index('; then',a)
                guard=source[a+len('    if '):b]+'\n'
            with tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'result.json'
                for actual,rows,expected in ((800,800,0),(800,799,1),(799,799,0 if count==0 else 1)):
                    with self.subTest(chain=name,count=actual,rows=rows):
                        path.write_text(json.dumps({'status':'complete','candidates_considered':actual,'results':[{'term':'term'+str(i)} for i in range(rows)]}))
                        before=path.read_bytes()
                        body='set -uo pipefail\nrepo='+shlex.quote(str(REPO))+'\nREPO="$repo"\npython='+shlex.quote(sys.executable)+'\nout='+shlex.quote(str(path))+'\nlimit=800\n'+guard
                        result=subprocess.run(['bash','-c',body],capture_output=True,text=True,timeout=5)
                        self.assertEqual(expected,result.returncode,result.stdout+result.stderr)
                        self.assertEqual(before,path.read_bytes())

    def test_shared_cli_keeps_missing_incomplete_and_unreadable_distinct(self):
        helper=REPO/'app/experiments/respelling_completion.py'
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'result.json'
            good={'status':'complete','candidates_considered':2,'results':[{'term':'A'},{'term':'B'}]}
            for raw,expected in ((None,1),('{',2),('[]',2),(json.dumps({'status':'complete'}),1),(json.dumps(good),0),(json.dumps(dict(good,status='partial')),1)):
                with self.subTest(raw=raw):
                    if raw is None:
                        if path.exists():path.unlink()
                    else:path.write_text(raw)
                    result=subprocess.run([sys.executable,str(helper),str(path),'2','--refuse-unreadable'],capture_output=True,text=True,timeout=5)
                    self.assertEqual(expected,result.returncode,result.stderr)
                    if raw is not None:self.assertEqual(raw,path.read_text())
                    ordinary=subprocess.run([sys.executable,str(helper),str(path),'2'],capture_output=True,text=True,timeout=5)
                    self.assertEqual(0 if expected==0 else 1,ordinary.returncode,ordinary.stderr)

    def test_resume_rejects_forged_complete_and_wrong_count_without_changing_checkpoint(self):
        source=(Path('/tmp/before320_resume_partial_arm_20260826.sh') if os.environ.get('RESPELLING_320_BASELINE') else REPO/'run_chains/resume_partial_arm_20260826.sh').read_text()
        a=source.index('set +e\n');b=source.index('\nesac',a)+len('\nesac')
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);path=root/'result.json'
            for document,dispatch in (({'status':'complete'},True),({'status':'complete','candidates_considered':1,'results':[{'term':'A'}]},True),({'status':'complete','candidates_considered':2,'results':[{'term':'A'},{'term':'B'}]},False)):
                with self.subTest(document=document):
                    path.write_text(json.dumps(document));before=path.read_bytes()
                    body='REPO='+shlex.quote(str(REPO))+'\npython='+shlex.quote(sys.executable)+'\nout='+shlex.quote(str(path))+'\nLIMIT=2\nSEP=space\n'+source[a:b]+'\necho dispatch\n'
                    result=subprocess.run(['bash','-c',body],capture_output=True,text=True,timeout=5)
                    self.assertEqual(0,result.returncode,result.stdout+result.stderr)
                    self.assertEqual(dispatch,'dispatch' in result.stdout,result.stdout+result.stderr)
                    self.assertEqual(before,path.read_bytes())
