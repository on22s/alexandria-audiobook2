import os
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import core
import subprocess_ownership as ownership
from tests.test_subprocess_finally import is_live


class SharedShutdownPolicyTests(unittest.TestCase):
    def test_app_delegates_after_resuming_and_retains_forced_tail_policy(self):
        process=Mock();state={'paused':True};order=[]
        with patch.object(core,'_resume_if_paused',side_effect=lambda *_:order.append('resume')), \
             patch.object(core,'stop_owned_subprocess',side_effect=lambda *_args,**_kwargs:order.append('stop')) as stop:
            core.ensure_failed_subprocess_stopped(process,state)
        self.assertEqual(['resume','stop'],order)
        stop.assert_called_once_with(process,timeout=core.CANCEL_TERMINATE_GRACE_SECONDS,force_after_grace=True)

    def test_forced_tail_runs_even_when_graceful_inspection_fails(self):
        process=Mock()
        with patch.object(ownership,'is_subprocess_tree_running',side_effect=RuntimeError('fixture inspection fault')), \
             patch.object(ownership,'send_subprocess_signal') as send:
            with self.assertRaisesRegex(RuntimeError,'fixture inspection fault'):
                ownership.stop_owned_subprocess(process,force_after_grace=True)
        send.assert_called_once_with(process,signal.SIGKILL)

    @unittest.skipUnless(sys.platform=='linux','Native Linux owned worker fixture')
    def test_paused_term_ignoring_worker_is_forced_before_return(self):
        with tempfile.TemporaryDirectory() as tmp:
            pid_path=Path(tmp,'worker.pid')
            code="import os,pathlib,signal,sys,time;signal.signal(signal.SIGTERM,signal.SIG_IGN);pathlib.Path(sys.argv[1]).write_text(str(os.getpid()));print('ready',flush=True);time.sleep(30)"
            process=ownership.start_owned_subprocess([sys.executable,'-c',code,str(pid_path)],
                cwd=tmp,stdout=subprocess.PIPE,stderr=subprocess.STDOUT,text=True,start_new_session=True)
            unrelated=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
            pid=None
            try:
                self.assertEqual('ready',process.stdout.readline().strip());pid=int(pid_path.read_text())
                ownership.send_subprocess_signal(process,signal.SIGSTOP)
                state={'paused':True}
                with patch.object(core,'CANCEL_TERMINATE_GRACE_SECONDS',.1):
                    core.ensure_failed_subprocess_stopped(process,state)
                self.assertFalse(state['paused']);self.assertFalse(is_live(pid))
                self.assertIsNotNone(process.poll());self.assertIsNone(unrelated.poll())
            finally:
                if process.poll() is None:
                    ownership.send_subprocess_signal(process,signal.SIGKILL)
                process.wait(timeout=3);process.stdout.close()
                control=getattr(process,'_alexandria_control',None)
                if control is not None:control.close()
                unrelated.terminate();unrelated.wait(timeout=3)
