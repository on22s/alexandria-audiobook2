"""Execute the campaign's actual config-edit command in a private native shell."""
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


class PdncConfigAtomicWriteTests(unittest.TestCase):
    def run_edit(self, fail=False):
        with tempfile.TemporaryDirectory() as td:
            root=Path(td);(root/'app').mkdir()
            config=root/'app/config.json'
            original=b'{"llm":{"model_name":"old remote","key":"keep"},"llm_local":{"model_name":"old local"},"other":[1,2,3]}'
            config.write_bytes(original)
            source=(REPO/'run_chains/pdnc_generation.sh').read_text()
            match=re.search(r'(?m)^"\$PY" -[^\n]*<<\x27PYEOF\x27[^\n]*\n.*?\nPYEOF',source,re.S)
            self.assertIsNotNone(match)
            command=match.group()
            wrapper=root/'python'
            bootstrap='''import json,os,sys
sys.argv=sys.argv[1:]
if os.environ.get('FAIL_SERIALIZE')=='1':
 def interrupt(value,stream,*args,**kwargs):
  stream.write('{"torn":');stream.flush()
  raise KeyboardInterrupt('fixture interruption during serialization')
 json.dump=interrupt
exec(compile(sys.stdin.read(),'actual campaign config editor','exec'))
'''
            wrapper.write_text('#!/bin/sh\nexec '+shlex.quote(sys.executable)+' -c '+shlex.quote(bootstrap)+' "$@"\n');wrapper.chmod(0o755)
            program='REPO='+shlex.quote(str(root))+'\nPY='+shlex.quote(str(wrapper))+'\n'+command+'\nprintf advanced > "$REPO/advanced"\n'
            env=dict(os.environ,PYTHONPATH=str(REPO/'app'),FAIL_SERIALIZE='1' if fail else '0')
            result=subprocess.run(['bash','-c',program],env=env,cwd=root,capture_output=True,text=True,timeout=10)
            return result,original,config.read_bytes(),(root/'advanced').exists(),list((root/'app').glob('.tmp_*'))

    def test_interrupted_serialization_preserves_original_valid_config(self):
        result,original,after,advanced,temporary=self.run_edit(True)
        self.assertEqual(original,after,result.stdout+result.stderr)
        self.assertEqual([],temporary)

    def test_interrupted_edit_stops_campaign_before_next_operation(self):
        result,original,after,advanced,temporary=self.run_edit(True)
        self.assertNotEqual(0,result.returncode,result.stdout+result.stderr)
        self.assertFalse(advanced)

    def test_success_changes_both_models_and_preserves_unrelated_settings(self):
        result,original,after,advanced,temporary=self.run_edit()
        self.assertEqual(0,result.returncode,result.stdout+result.stderr)
        expected=json.loads(original);expected['llm']['model_name']='qwen3-14b';expected['llm_local']['model_name']='qwen3-14b'
        self.assertEqual(expected,json.loads(after));self.assertTrue(advanced);self.assertEqual([],temporary)
