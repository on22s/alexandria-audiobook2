"""Displayed and remote-shell commands carry values as literal arguments."""
import json
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import lmstudio_settings as settings
from script_preflight import replacement_repair_hint


class CommandBoundaryTests(unittest.TestCase):
    def test_repair_hint_executes_literal_filename_not_extra_shell_statements(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);binary=root/'app/env/bin';binary.mkdir(parents=True)
            recorder=binary/'python'
            recorder.write_text('#!'+sys.executable+'\nimport json,pathlib,sys\npathlib.Path("argv.json").write_text(json.dumps(sys.argv[1:]))\n')
            recorder.chmod(0o755)
            sentinel=root/'injected'
            names=['ordinary name.txt',"quote'book.txt",'line\nbreak.txt','-book.txt',f'book; touch {sentinel}; #',f'book$(touch {sentinel}).txt',f'book`touch {sentinel}`.txt']
            for name in names:
                with self.subTest(name=name):
                    hint=replacement_repair_hint(name)
                    command=hint.split('it with\n',1)[1].split('\nwhich writes',1)[0].strip()
                    result=subprocess.run(['bash','-c',command],cwd=root,capture_output=True,text=True,timeout=3)
                    self.assertEqual(0,result.returncode,result.stderr)
                    argv=json.loads((root/'app/argv.json').read_text())
                    self.assertEqual(['repair_source_encoding.py','--apply','--',name],argv)
                    self.assertFalse(sentinel.exists())

    def test_invalid_ports_reject_before_transport(self):
        for port in ('1234; touch injected','$(touch injected)','-1','0','65536','1.5',True,1.5,None,{},'１２３４','9'*5000):
            with self.subTest(port=port), patch.object(settings,'_ssh_run') as transport:
                ok,message=settings.ensure_remote_server_running('fixture',port)
                self.assertFalse(ok)
                self.assertIn('port',message.lower())
                transport.assert_not_called()

    def test_decimal_ports_form_exact_safe_start_command(self):
        for port in (1,65535,1234,'1234',' 1234 ','0'*5000+'1234'):
            with self.subTest(port=port),patch.object(settings,'_remote_server_bound',return_value=False),patch.object(settings,'_ssh_run',return_value=subprocess.CompletedProcess([],0,'','')) as run:
                ok,_=settings.ensure_remote_server_running('fixture',port)
                self.assertTrue(ok)
                self.assertEqual(['lms','server','start','--port',str(int(port.strip().lstrip('0') or '0') if isinstance(port,str) else port),'--bind','0.0.0.0'],shlex.split(run.call_args.args[1]))
