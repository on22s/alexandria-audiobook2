"""Native file locks serialize response blocks and rotation across processes."""
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import generate_script as gs
from utils import file_lock


def log_response(path, label, payload):
    reply=json.dumps([{'speaker':'ALICE','text':payload,'instruct':''}])
    client=SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=lambda **kw:
        SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=reply),finish_reason='stop')],usage=None))))
    params=gs.LLMGenParams('Return entries.','',8192,0.1,1)
    with patch.object(gs,'get_response_log_path',return_value=str(path)),contextlib.redirect_stdout(io.StringIO()):
        return gs.call_llm_for_entries(client,'fixture','system','user',params,'response.log',label,max_retries=0)


class ResponseLogAtomicityTests(unittest.TestCase):
    def test_open_and_rotation_are_both_inside_the_same_kernel_lock(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'response.log';path.write_text('prior response')
            observed=[];native_open=open;native_rotate=gs._rotate_log_if_large
            def probe():
                with native_open(str(path)+'.lock','a+b') as handle:
                    try:fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    except BlockingIOError:observed.append('held')
                    else:observed.append('unlocked');fcntl.flock(handle,fcntl.LOCK_UN)
            def rotating(*args,**kwargs):probe();return native_rotate(*args,max_bytes=1)
            def opening(*args,**kwargs):probe();return native_open(*args,**kwargs)
            with patch.dict(os.environ,{'ALEXANDRIA_RUN_ID':''}),patch.object(gs,'open',opening,create=True),patch.object(gs,'_rotate_log_if_large',rotating):
                self.assertEqual('native payload',log_response(path,'ONE','native payload')[0]['text'])
            self.assertEqual(['held','held'],observed)
            self.assertEqual('prior response',Path(str(path)+'.bak').read_text())
            self.assertIn('ONE | attempt 1',path.read_text())

    def test_concurrent_native_processes_leave_complete_noninterleaved_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'response.log'
            code="from tests.test_response_log_atomicity import log_response;import sys;log_response(sys.argv[1],sys.argv[2],sys.argv[2]*30000)"
            env=dict(os.environ,ALEXANDRIA_RUN_ID='')
            workers=[subprocess.Popen([sys.executable,'-c',code,str(path),f'REPLY{i}'],env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True) for i in range(4)]
            try:
                for worker in workers:
                    out,err=worker.communicate(timeout=20);self.assertEqual(0,worker.returncode,out+err)
                blocks=[block for block in path.read_text().split('='*80) if block.strip()]
                self.assertEqual(4,len(blocks))
                labels=set()
                for block in blocks:
                    label=block.strip().split(' | attempt')[0];labels.add(label)
                    payload=json.loads(block.split('─'*80+'\n',1)[1].strip())
                    self.assertEqual(label*30000,payload[0]['text'])
                self.assertEqual({f'REPLY{i}' for i in range(4)},labels)
            finally:
                for worker in workers:
                    if worker.poll() is None:worker.kill();worker.wait()

    def test_run_paths_are_pure_and_refuse_traversal_and_symlink_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);module=root/'app/generate_script.py';module.parent.mkdir()
            with patch.object(gs,'__file__',str(module)):
                for run_id in ('../escape','/tmp/escape','a/b','..','a\\b','bad\nname'):
                    with self.subTest(run_id=run_id),patch.dict(os.environ,{'ALEXANDRIA_RUN_ID':run_id}):
                        with self.assertRaises(ValueError):gs.get_response_log_path('review_responses.log')
                with patch.dict(os.environ,{'ALEXANDRIA_RUN_ID':'run_test-123'}):
                    path=gs.get_response_log_path('review_responses.log')
                    self.assertEqual(str(root/'logs/responses/run_test-123/review_responses.log'),path)
                    self.assertFalse((root/'logs').exists(),'pure getter created a directory')
                (root/'logs/responses').mkdir(parents=True)
                (root/'logs/responses/run_test-123').symlink_to(root/'app',target_is_directory=True)
                with patch.dict(os.environ,{'ALEXANDRIA_RUN_ID':'run_test-123'}):
                    with self.assertRaises(ValueError):gs.get_response_log_path('review_responses.log')
