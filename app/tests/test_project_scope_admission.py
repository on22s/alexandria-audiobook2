"""Scope ownership excludes local work and an actual foreign task lease."""
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import copy
import core
import unittest
from unittest.mock import patch
from fastapi import HTTPException
from project_scope_admission import ensure_scope_admission, is_scope_admitted


class ScopeAdmissionTests(unittest.TestCase):
    def test_local_activity_and_foreign_lease_refuse_admission_then_release(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            with patch('core.DATA_DIR', str(root)), patch('core.process_state', {'audio':{'running':True}}):
                with self.assertRaises(HTTPException):
                    with ensure_scope_admission(root):self.fail('Local task was not excluded')
            code = ('from task_ownership import acquire_task_lease; import sys; '
                    'lease=acquire_task_lease(sys.argv[1],"fixture",set()); '
                    'print("owned",flush=True); sys.stdin.readline(); lease.close()')
            child = subprocess.Popen([sys.executable,'-c',code,str(root)], stdin=subprocess.PIPE,
                                     stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,
                                     cwd=Path(__file__).resolve().parents[1])
            try:
                self.assertEqual('owned', child.stdout.readline().strip())
                self.assertIsNone(child.poll())
                with patch('core.DATA_DIR', str(root)), patch('core.process_state', {}):
                    with self.assertRaises(HTTPException):
                        with ensure_scope_admission(root):self.fail('Foreign owner was not excluded')
            finally:
                _stdout, stderr = child.communicate('\n',timeout=5)
            self.assertEqual(0, child.returncode, stderr)
            with patch('core.DATA_DIR', str(root)), patch('core.process_state', {}):
                with ensure_scope_admission(root):
                    self.assertTrue(is_scope_admitted(root))
                    observations = []
                    thread = threading.Thread(target=lambda: observations.append(is_scope_admitted(root)))
                    thread.start(); thread.join(timeout=5)
                    self.assertFalse(thread.is_alive()); self.assertEqual([False], observations)
                self.assertFalse(is_scope_admitted(root))
            self.assertFalse((root/'state.json').exists())

    def test_slow_scope_keeps_task_fence_without_blocking_request_mutex(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            states = copy.deepcopy(core.process_state)
            for state in states.values():state['running'] = False
            entered, release, done = threading.Event(), threading.Event(), threading.Event()
            outcomes = []
            def hold_scope():
                with ensure_scope_admission(root):
                    entered.set()
                    release.wait(5)
            def claim_task():
                try:
                    claim = core.reserve_background_task('audio', cpu_only=True)
                except HTTPException:
                    outcomes.append('busy')
                else:
                    core.release_gpu_task_claim('audio', claim)
                    outcomes.append('claimed')
                finally:
                    done.set()
            with patch.object(core, 'DATA_DIR', str(root)), patch.object(core, 'process_state', states), \
                    patch.object(core, '_task_claims', {}), patch.object(core, '_gpu_leases', {}):
                holder = threading.Thread(target=hold_scope)
                contender = threading.Thread(target=claim_task)
                holder.start()
                try:
                    self.assertTrue(entered.wait(5))
                    contender.start()
                    completed_while_active = done.wait(1)
                finally:
                    release.set(); holder.join(5)
                    if contender.ident is not None:contender.join(5)
                self.assertFalse(holder.is_alive());self.assertFalse(contender.is_alive())
                self.assertTrue(completed_while_active, 'Task admission blocked behind long scope I/O')
                self.assertEqual(['busy'], outcomes)
                claim = core.reserve_background_task('audio', cpu_only=True)
                self.assertTrue(core.release_gpu_task_claim('audio', claim))
