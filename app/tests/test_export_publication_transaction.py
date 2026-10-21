"""Live owner leases and chapter set admission have bounded lock behavior."""
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import export_publication
import export_quality
from tests import test_project_export_quality as fixtures


class PublicationTransactionTests(unittest.TestCase):
    def run_bounded(self, code):
        result = subprocess.run([sys.executable, '-c', code], capture_output=True,
                                text=True, timeout=20)
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)

    def test_live_lease_identity_thread_lifetime_and_cleanup_are_scoped(self):
        self.run_bounded(LIFETIME_PROBE)

    def test_actual_editor_waits_outside_book_during_owner_aware_transaction(self):
        self.run_bounded(EDITOR_PROBE)

    def test_whole_set_scoped_admission_refuses_contention_and_changed_retained_file(self):
        with tempfile.TemporaryDirectory() as root:
            manager, _ = fixtures.ProjectExportQualityTests().fixture(root)
            snapshot = export_publication.ExportSourceSnapshot(manager)
            snapshot.verify_sources()
            paths = [Path(root) / name for name in ('staged.wav', 'retained.wav')]
            for path in paths:
                path.write_bytes(b'admitted regular file')
            outputs = {path: export_quality.get_export_file_identity(path) for path in paths}
            with snapshot.ensure_publication_transaction(outputs) as admit:
                with admit():
                    self.assertTrue(manager._chunks_lock.locked())
                self.assertFalse(manager._chunks_lock.locked())
                with manager._chunks_lock:
                    with self.assertRaises(TimeoutError):
                        with admit():
                            self.fail('Contended chunk mutex was admitted')
                paths[1].write_bytes(b'changed retained file')
                with self.assertRaisesRegex(ValueError, 'Finished export changed'):
                    with admit():
                        self.fail('Changed member was admitted')
            with self.assertRaisesRegex(ValueError, 'transaction has ended'):
                with admit():
                    self.fail('Escaped callback retained admission')
            self.assertFalse(manager._chunks_lock.locked())


LIFETIME_PROBE = r'''
import sys,threading,json,contextvars,hashlib
from pathlib import Path
from contextlib import contextmanager
import audio_generation_owner as a
results=[]
calls=[];revoked=False
@contextmanager
def owner(*,allow_cancelled=False):
 calls.append(allow_cancelled)
 if revoked and not allow_cancelled:raise a.AudioGenerationCancelled()
 yield
with a.ensure_audio_generation_owner(owner):
 with a.ensure_audio_generation_publication():
  with a.ensure_audio_generation_publication():pass
  with a.ensure_audio_generation_publication(allow_cancelled=True):pass
  assert calls==[False]
  copied=contextvars.copy_context()
 assert a._held_publication.get() is None
 revoked=True
 try:copied.run(a.ensure_audio_generation_active)
 except a.AudioGenerationCancelled:results.append('expired copied context performed fresh owner check and refused revocation')
 else:raise AssertionError('expired copy retained authority')
 assert calls==[False,False]
 with a.ensure_audio_generation_publication(allow_cancelled=True):
  try:a.ensure_audio_generation_active()
  except a.AudioGenerationCancelled:results.append('cleanup cannot authorize nested normal publication')
  else:raise AssertionError('cleanup upgraded')
 assert a._held_publication.get() is None
 revoked=False
 try:
  with a.ensure_audio_generation_publication():raise RuntimeError('body')
 except RuntimeError:pass
 assert a._held_publication.get() is None
 results.append('body exception resets lease and ContextVar')
 assert a.get_audio_generation_claim_id() is None
 results.append('legacy callable remains valid and has no inferred claim')
 assert a.get_audio_generation_owner() is owner
assert a.get_audio_generation_owner() is None
# Different owner is always admitted, even on same thread.
other_calls=[]
@contextmanager
def other(*,allow_cancelled=False):other_calls.append(allow_cancelled);yield
with a.ensure_audio_generation_owner(owner),a.ensure_audio_generation_publication():
 with a.ensure_audio_generation_owner(other),a.ensure_audio_generation_publication():pass
assert other_calls==[False];results.append('different owner invokes its own admission')
# Cross-thread copy cannot inherit physical ownership of a held mutex.
mutex=threading.Lock();attempted=threading.Event();done=threading.Event();count=[];errors=[];main=threading.get_ident()
@contextmanager
def locked(*,allow_cancelled=False):
 if threading.get_ident()!=main:attempted.set()
 with mutex:count.append(threading.get_ident());yield
with a.ensure_audio_generation_owner(locked):
 with a.ensure_audio_generation_publication():
  ctx=contextvars.copy_context()
  def run():
   try:ctx.run(a.ensure_audio_generation_active);done.set()
   except BaseException as e:errors.append(repr(e))
  worker=threading.Thread(target=run);worker.start();assert attempted.wait(1);assert not done.is_set()
 worker.join(1);assert not worker.is_alive();assert done.is_set();assert not errors
assert len(count)==2;results.append('cross-thread copied context waits for real admission and finishes after outer release')
'''

EDITOR_PROBE = r'''
import sys,tempfile,threading,time,json,hashlib
from pathlib import Path
from contextlib import ExitStack,contextmanager
from unittest.mock import patch
import core,export_publication
from routers import editor
from project import ProjectManager
from book_state_transaction import ensure_book_state
from audio_generation_owner import AudioGenerationOwner,ensure_audio_generation_owner,is_audio_generation_cancelled
attempted=threading.Event();edit_book_entered=threading.Event();errors=[];mainthread=threading.get_ident()
class ObservedMutex:
 def __init__(self):self.lock=threading.Lock()
 def __enter__(self):
  if threading.get_ident()!=mainthread:attempted.set()
  self.lock.acquire()
 def __exit__(self,*args):self.lock.release()
@contextmanager
def edit_book(path):
 edit_book_entered.set()
 with ensure_book_state(path,timeout=.5):yield
with tempfile.TemporaryDirectory() as tmp,ExitStack() as stack:
 manager=ProjectManager(tmp);mutex=ObservedMutex();claim='a'*32
 states={key:{'running':False,'cancel':False,'logs':[]} for key in core.process_state};states['chapter_export']['running']=True
 for module,key,value in [(core,'process_state',states),(core,'_task_claims',{'chapter_export':{'id':claim,'phase':'started'}}),(core,'_gpu_lock',mutex),(editor,'_gpu_lock',mutex),(editor,'SCRIPT_PATH',manager.script_path),(editor,'project_manager',manager),(editor,'ensure_book_state',edit_book)]:stack.enter_context(patch.object(module,key,value))
 stack.enter_context(patch.object(editor,'check_chunk_identity',return_value=None));stack.enter_context(patch.object(manager,'update_chunk',return_value=[]))
 snapshot=export_publication.ExportSourceSnapshot(manager)
 def edit():
  try:editor._apply_chunk_edit(editor.apply_chunk_update,0,{'text':'edit'},None,None)
  except BaseException as error:errors.append(repr(error))
 with ensure_audio_generation_owner(AudioGenerationOwner(claim,lambda **kw:core._ensure_audio_task_owner(claim,task_name='chapter_export',**kw))):
  with snapshot.ensure_publication_transaction({}) as admit:
   with admit():pass
   worker=threading.Thread(target=edit);worker.start();assert attempted.wait(1)
   assert not edit_book_entered.is_set()
   start=time.monotonic();assert not is_audio_generation_cancelled()
   with admit():pass
   duration=time.monotonic()-start
  worker.join(1);assert not worker.is_alive()
 assert edit_book_entered.is_set();assert not errors,errors
 result={'second_admission_and_owner_aware_callback_seconds':duration,'edit_waited_outside_book':True,'edit_completed_after_transaction':True,'edit_errors':errors}
'''
