"""Real process death, filesystem faults and live leases for disposable spill."""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from audio_generation_owner import AudioGenerationOwner, ensure_audio_generation_owner
from book_state_transaction import ensure_book_state
from synthesis_recovery_context import RecoveryParent
import synthesis_workspace as workspace
from task_ownership import ensure_startup_recovery


@contextmanager
def admit(**kwargs):
    yield


def get_parent(root):
    owner = AudioGenerationOwner('a' * 32, admit)
    return owner, RecoveryParent(str(root), 'b' * 64, 'c' * 64, 'd' * 64,
                                hashlib.sha256(owner.claim_id.encode()).hexdigest(), admit)


_CHILD = r'''
import os,sys
from pathlib import Path
import numpy as np,soundfile as sf
from tests.test_synthesis_workspace import get_parent
from audio_generation_owner import ensure_audio_generation_owner
from synthesis_workspace import ensure_synthesis_workspace
from task_ownership import acquire_task_lease
from synthesis_render import get_synthesis_window_plan,render_synthesis_windows
from synthesis_settings import get_synthesis_window_settings
root=Path(sys.argv[1]); phase=sys.argv[2]
lease=acquire_task_lease(root,'audio',set())
owner,parent=get_parent(root)
policy=get_synthesis_window_settings({'enabled':True,'budgets':{'local.custom':{'max_chars':5}}})
plan=get_synthesis_window_plan('One. Two.',policy,['local.custom'])
calls=0
def render(text,path):
 global calls
 calls+=1
 if calls==2:os._exit(76)
 sf.write(path,np.sin(np.arange(2400)*.1)*.2,24000)
 return True
with ensure_audio_generation_owner(owner):
 if phase=='window':
  render_synthesis_windows(plan,render,root/'parent.wav',policy,workspace_parent=parent)
 elif phase=='live':
  with ensure_synthesis_workspace(parent,2) as path:
   (path/'window-0.wav').write_bytes(b'live writer')
   print('ready',flush=True)
   sys.stdin.readline()
 elif phase in ('join','published'):
  with ensure_synthesis_workspace(parent,2) as path:
   (path/'window-0.wav').write_bytes(b'completed window')
   (path/'join'/'complete.wav').write_bytes(b'partial joined parent')
   if phase=='published':(root/'parent.wav').write_bytes(b'published new parent')
   os._exit(76)
'''


_PROJECT_CHILD = r'''
import hashlib,json,os,sys
from pathlib import Path
import numpy as np
from tests.test_take_publication import TakePublicationTests
from task_ownership import acquire_task_lease
note=Path(sys.argv[1]);phase=sys.argv[2];policies_off=phase.startswith('default-');phase=phase.removeprefix('default-')
def pause():
 note.with_suffix('.ready').touch()
 sys.stdin.readline()
case=TakePublicationTests()
class Model:
 def __init__(self):self.calls=0
 def generate_custom_voice(self,**kwargs):
  self.calls+=1
  if (phase=='zero' and self.calls==1) or (phase=='window' and self.calls==2):pause()
  return [np.sin(np.arange(2400)*.1)*.2],24000
with case.fixture(False,enabled=False) as (root,manager,previous,_),case.owner():
 lease=acquire_task_lease(root,'audio',set())
 note.write_text(json.dumps({'root':str(root),'previous':str(previous),
  'previous_sha256':hashlib.sha256(previous.read_bytes()).hexdigest()}))
 engine=case.engine();engine._synthesis_recovery=None;engine._local_custom_model=Model()
 if policies_off:engine._synthesis_windows=None
 manager.engine=engine
 Path(manager.voice_config_path).write_text(json.dumps({'NARRATOR':{'type':'custom','voice':'Ryan','seed':42}}))
 original=manager._publish_generated_chunk_audio
 def publish(*args,**kwargs):
  if policies_off and (args[0].get('_synthesis_receipt') is not None or args[0].get('_speech_input') is not None or kwargs.get('publication_admission') is not None):raise RuntimeError('Disabled optional proof/capture unexpectedly enabled')
  if phase=='parent':pause()
  result=original(*args,**kwargs)
  if phase=='published':
   proof=json.loads(note.read_text());selected=root/result
   proof.update(published_path=str(selected),published_sha256=hashlib.sha256(selected.read_bytes()).hexdigest())
   note.write_text(json.dumps(proof));pause()
  return result
 manager._publish_generated_chunk_audio=publish
 if phase in ('encoded','rendered'):
  import project
  # Restore the actual codec hidden by the generic WAV-only fixture. Observe
  # its return without replacing either writer's identity/ownership gate.
  project._export_audio_segment=project._RAW_CHUNK_CODEC_HELPERS[1]
  target=(project._export_audio_segment if phase=='encoded' else type(manager)._export_chunk_audio).__code__
  def observed(frame,event,value):
   if event!='return' or frame.f_code is not target:return
   path=Path(frame.f_locals['path']) if phase=='encoded' else (root/value if isinstance(value,str) else None)
   if path is None or not path.is_file() or path.stat().st_size<1024:return
   sys.setprofile(None)
   proof=json.loads(note.read_text());proof.update(encoded_path=str(path),encoded_sha256=hashlib.sha256(path.read_bytes()).hexdigest())
   note.write_text(json.dumps(proof));pause()
  sys.setprofile(observed)
 success,payload=manager.generate_chunk_audio(0)
 if not success:raise RuntimeError(payload)
 raise RuntimeError('Expected parent barrier was not reached')
'''


_PROJECT_BATCH_CHILD = r'''

from pathlib import Path
import json,os,sys,hashlib
import numpy as np
from tests.test_take_publication import TakePublicationTests
from synthesis_settings import get_synthesis_window_settings
from task_ownership import acquire_task_lease
note=Path(sys.argv[1]);case=TakePublicationTests();mode,_,phase=sys.argv[2].partition(':');policies_off=mode=='default'
class Model:
 def generate_custom_voice(self,**kwargs):
  text=kwargs['text']
  if type(text)is not list or len(text)!=2:raise RuntimeError('Expected one true native list call for two chunks')
  proof=json.loads(note.read_text());proof['submitted_texts']=text;proof['model_calls']=proof.get('model_calls',0)+1;note.write_text(json.dumps(proof))
  return [np.sin(np.arange(2400)*.1)*.2 for _ in text],24000
with case.fixture(False,enabled=False) as (root,manager,previous,_),case.owner():
 lease=acquire_task_lease(root,'audio',set())
 chunks=manager.load_chunks();chunks.append({**chunks[0],'id':1,'uid':'line-two','text':'Second synthetic words.'});manager.save_chunks(chunks)
 note.write_text(json.dumps({'root':str(root),'prior':str(previous),'prior_sha256':hashlib.sha256(previous.read_bytes()).hexdigest()}))
 engine=case.engine();engine._synthesis_recovery=None;engine._synthesis_windows=None if policies_off else get_synthesis_window_settings({'enabled':True,'budgets':{'local.custom':{'max_chars':10000}}});engine._local_custom_model=Model();engine._custom_warmup_needed=False;engine._sub_batch_max_items=0;engine._sub_batch_enabled=False
 manager.engine=engine;Path(manager.voice_config_path).write_text(json.dumps({'NARRATOR':{'type':'custom','voice':'Ryan','seed':42}}))
 if phase in ('encoded','rendered'):
  import project
  project._export_audio_segment=project._RAW_CHUNK_CODEC_HELPERS[1]
  target=(project._export_audio_segment if phase=='encoded' else type(manager)._export_chunk_audio).__code__
  def observed(frame,event,value):
   if event!='return' or frame.f_code is not target:return
   path=Path(frame.f_locals['path']) if phase=='encoded' else (root/value if isinstance(value,str) else None)
   if path is None or not path.is_file() or path.stat().st_size<1024:return
   sys.setprofile(None)
   proof=json.loads(note.read_text());proof.update(encoded_path=str(path),encoded_sha256=hashlib.sha256(path.read_bytes()).hexdigest());note.write_text(json.dumps(proof))
   os._exit(76)
  sys.setprofile(observed)
 else:manager._publish_generated_chunk_audio=lambda *a,**k:os._exit(76)
 result=manager.generate_chunks_batch([0,1],batch_seed=42,batch_size=2)
 raise RuntimeError('Expected prepublication hard exit; result='+repr(result))
'''

@unittest.skipUnless(workspace.is_synthesis_workspace_supported(), 'Registered spill currently qualifies Linux dirfd durability')
class SynthesisWorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name);self.owner, self.parent = get_parent(self.root)

    def create(self, windows=2):
        with self.parent.admit(), workspace.SynthesisWorkspaceRegistry(self.root, create=True) as store:
            return store.reserve(self.parent, windows)

    def cleanup_idle(self):
        with ensure_startup_recovery(self.root, timeout=0) as idle:
            if not idle:return None
            with ensure_book_state(self.root):
                return workspace.apply_abandoned_synthesis_workspace_cleanup(self.root)

    def rows(self):
        with workspace.SynthesisWorkspaceRegistry(self.root) as store:
            return copy.deepcopy(store.body['rows'])

    def test_hard_exit_window_join_and_postpublication_reclaimed_under_idle(self):
        for phase in ('window', 'join', 'published'):
            with self.subTest(phase=phase):
                (self.root/'parent.wav').write_bytes(b'prior parent')
                cache=self.root/'.synthesis-recovery-v1';cache.mkdir(exist_ok=True)
                (cache/'retained.wav').write_bytes(b'reusable evidence')
                result=subprocess.run([sys.executable,'-c',_CHILD,str(self.root),phase],
                    cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=40)
                self.assertEqual(76,result.returncode,result.stderr)
                rows=self.rows();self.assertEqual(1,len(rows))
                path=self.root/workspace.DIRECTORY/next(iter(rows))
                self.assertTrue((path/'window-0.wav').is_file())
                self.assertEqual(1,self.cleanup_idle());self.assertFalse(path.exists())
                self.assertEqual({},self.rows())
                self.assertEqual(b'published new parent' if phase=='published' else b'prior parent',
                                 (self.root/'parent.wav').read_bytes())
                self.assertEqual(b'reusable evidence',(cache/'retained.wav').read_bytes())

    def test_live_foreign_task_preserved_then_existing_process_retries_after_sigkill(self):
        child=subprocess.Popen([sys.executable,'-c',_CHILD,str(self.root),'live'],
            cwd=Path(__file__).resolve().parents[1],stdin=subprocess.PIPE,stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual('ready',child.stdout.readline().strip())
            rows=self.rows();self.assertEqual(1,len(rows))
            path=self.root/workspace.DIRECTORY/next(iter(rows))
            self.assertIsNone(self.cleanup_idle());self.assertTrue(path.exists())
            child.kill();child.wait(timeout=10)
            self.assertEqual(1,self.cleanup_idle());self.assertFalse(path.exists())
        finally:
            if child.poll() is None:child.kill();child.wait(timeout=10)
            for stream in (child.stdin,child.stdout,child.stderr):stream.close()

    def test_normal_unwind_and_nested_parallel_parent_registrations(self):
        with ensure_audio_generation_owner(self.owner):
            with workspace.ensure_synthesis_workspace(self.parent,2) as first:
                (first/'window-0.wav').write_bytes(b'first')
                with workspace.ensure_synthesis_workspace(self.parent,2) as second:
                    (second/'window-0.wav').write_bytes(b'second')
                    self.assertEqual(2,len(self.rows()));self.assertNotEqual(first,second)
                self.assertTrue(first.exists());self.assertFalse(second.exists())
            self.assertFalse(first.exists())
            with self.assertRaisesRegex(ValueError,'cancelled'):
                with workspace.ensure_synthesis_workspace(self.parent,2) as path:
                    (path/'window-0.wav').write_bytes(b'partial')
                    raise ValueError('cancelled')
            self.assertFalse(path.exists());self.assertEqual({},self.rows())

    def test_project_parent_sigkill_reclaims_zero_window_and_published_lifetimes(self):
        variants=['zero','window','parent','published','default-zero','default-parent','default-published']
        if shutil.which('ffmpeg'):
            variants += ['encoded','rendered','default-encoded','default-rendered']
        for variant in variants:
            phase=variant.removeprefix('default-');policies_off=variant.startswith('default-')
            with self.subTest(phase=variant),tempfile.TemporaryDirectory() as tmp:
                bound=Path(tmp);note=bound/'note.json'
                env={**os.environ,'TMPDIR':tmp,'CUDA_VISIBLE_DEVICES':'',
                     'HIP_VISIBLE_DEVICES':'','ROCR_VISIBLE_DEVICES':''}
                child=subprocess.Popen([sys.executable,'-c',_PROJECT_CHILD,str(note),variant],
                    cwd=Path(__file__).resolve().parents[1],env=env,stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,text=True)
                try:
                    deadline=time.monotonic()+20
                    while not note.with_suffix('.ready').exists() and child.poll() is None and time.monotonic()<deadline:
                        time.sleep(.01)
                    if child.poll() is not None:self.fail(child.stderr.read()[-4000:])
                    self.assertTrue(note.with_suffix('.ready').exists(),'Project parent barrier not reached')
                    proof=json.loads(note.read_text());root=Path(proof['root'])
                    self.assertTrue(root.resolve().is_relative_to(bound.resolve()))
                    legacy=root/'chunk_legacy.wav';legacy.write_bytes(b'unregistered preserved')
                    with workspace.SynthesisWorkspaceRegistry(root) as store:rows=copy.deepcopy(store.body['rows'])
                    self.assertEqual(2 if phase in ('zero','window') and not policies_off else 1,len(rows))
                    outer=[root/workspace.DIRECTORY/name for name,row in rows.items() if row['windows']==0]
                    self.assertEqual(1,len(outer));parent=outer[0]/'assembled-parent.wav'
                    if phase in ('encoded','rendered'):
                        self.assertTrue(next(row for row in rows.values() if row['windows']==0)['encoding'])
                        encoded=Path(proof['encoded_path'])
                        self.assertEqual(outer[0]/'encoded-parent.mp3',encoded)
                        self.assertEqual(proof['encoded_sha256'],hashlib.sha256(encoded.read_bytes()).hexdigest())
                        self.assertFalse(list((root/'voicelines').glob('.chunk-*')))
                        self.assertFalse(list((root/'voicelines').glob('.render-*')))
                    else:
                        self.assertEqual({'assembled-parent.wav','join'},{p.name for p in outer[0].iterdir()})
                    if phase in ('zero','window'):self.assertEqual(0,parent.stat().st_size)
                    else:self.assertGreater(parent.stat().st_size,44)
                    self.assertEqual([legacy],list(root.glob('chunk_*.wav')))
                    with ensure_startup_recovery(root,timeout=0) as idle:self.assertFalse(idle)
                    child.kill();self.assertEqual(-9,child.wait(timeout=10))
                    with ensure_startup_recovery(root,timeout=0) as idle:
                        self.assertTrue(idle)
                        with ensure_book_state(root):
                            self.assertEqual(len(rows),workspace.apply_abandoned_synthesis_workspace_cleanup(root))
                    with workspace.SynthesisWorkspaceRegistry(root) as store:self.assertEqual({},store.body['rows'])
                    self.assertFalse(parent.exists());self.assertEqual(b'unregistered preserved',legacy.read_bytes())
                    if phase in ('encoded','rendered'):self.assertFalse(encoded.exists())
                    self.assertEqual(proof['previous_sha256'],hashlib.sha256(Path(proof['previous']).read_bytes()).hexdigest())
                    if phase=='published':
                        self.assertEqual(proof['published_sha256'],hashlib.sha256(Path(proof['published_path']).read_bytes()).hexdigest())
                finally:
                    if child.poll() is None:child.kill();child.wait(timeout=10)
                    child.stdin.close();child.stderr.close()


    def test_project_list_batch_hard_exit_reclaims_both_registered_parents(self):
        modes=['windowed','default']
        if shutil.which('ffmpeg'):modes += ['windowed:encoded','windowed:rendered','default:encoded','default:rendered']
        for mode in modes:
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as bound:
                note=Path(bound)/'batch-note.json';env=dict(os.environ,TMPDIR=bound,PYTHONDONTWRITEBYTECODE='1')
                result=subprocess.run([sys.executable,'-B','-c',_PROJECT_BATCH_CHILD,str(note),mode],
                    cwd=Path(__file__).resolve().parents[1],env=env,capture_output=True,text=True,timeout=40)
                self.assertEqual(76,result.returncode,result.stderr)
                proof=json.loads(note.read_text());root=Path(proof['root'])
                self.assertTrue(root.resolve().is_relative_to(Path(bound).resolve()))
                self.assertEqual(1,proof['model_calls']);self.assertEqual(2,len(proof['submitted_texts']))
                self.assertFalse(list(root.glob('temp_batch_*.wav')))
                self.assertFalse((root/'.synthesis-recovery-v1').exists())
                with workspace.SynthesisWorkspaceRegistry(root) as store:rows=copy.deepcopy(store.body['rows'])
                self.assertEqual(2,len(rows));self.assertTrue(all(row['windows']==0 for row in rows.values()))
                if ':' in mode:
                    self.assertTrue(all(row['encoding'] is True for row in rows.values()))
                    encoded=Path(proof['encoded_path'])
                    self.assertTrue(encoded.is_relative_to(root/workspace.DIRECTORY))
                    self.assertEqual(proof['encoded_sha256'],hashlib.sha256(encoded.read_bytes()).hexdigest())
                    self.assertFalse(list((root/'voicelines').glob('.chunk-*')))
                    self.assertFalse(list((root/'voicelines').glob('.render-*')))
                for name in rows:self.assertGreater((root/workspace.DIRECTORY/name/'assembled-parent.wav').stat().st_size,44)
                with ensure_startup_recovery(root,timeout=0) as idle:
                    self.assertTrue(idle)
                    with ensure_book_state(root):self.assertEqual(2,workspace.apply_abandoned_synthesis_workspace_cleanup(root))
                with workspace.SynthesisWorkspaceRegistry(root) as store:self.assertEqual({},store.body['rows'])
                if ':' in mode:self.assertFalse(encoded.exists())
                self.assertEqual(proof['prior_sha256'],hashlib.sha256(Path(proof['prior']).read_bytes()).hexdigest())

    def test_batch_routes_capture_scope_and_native_writer_guards(self):
        from contextlib import ExitStack
        from contextvars import copy_context, Context
        from synthesis_recovery_context import (RecoveryAdmissionError, ensure_synthesis_batch_outputs,
            get_synthesis_batch_output_path)
        from tests.test_take_publication import TakePublicationTests
        import numpy as np
        import soundfile as sf
        class Model:
            def generate_custom_voice(self,**kwargs):
                if type(kwargs['text']) is not list:raise RuntimeError('Expected native list batching')
                return [np.full(2400,.1 if text=='Short words.' else .3) for text in kwargs['text']],24000
        engine=TakePublicationTests().engine();engine._synthesis_recovery=None
        from synthesis_settings import get_synthesis_window_settings
        engine._synthesis_windows=get_synthesis_window_settings({'enabled':True,'budgets':{'local.custom':{'max_chars':10000}}})
        engine._local_custom_model=Model();engine._custom_warmup_needed=False
        engine._sub_batch_max_items=0;engine._sub_batch_enabled=False
        with ensure_audio_generation_owner(self.owner),ExitStack() as stack:
            self.assertIs(self.parent,workspace.get_synthesis_workspace_parent(engine,'custom',self.parent,batch=True))
            for method in ('_local_batch_custom','save_batch_waveforms','save_submitted_batch_waveforms','generate_batch'):
                with self.subTest(method=method),patch.object(engine,method,return_value=None):
                    self.assertIsNone(workspace.get_synthesis_workspace_parent(engine,'custom',self.parent,batch=True))
            output=stack.enter_context(workspace.ensure_synthesis_parent_output(self.parent))
            second=stack.enter_context(workspace.ensure_synthesis_parent_output(self.parent))
            routes={0:output,1:second};stack.enter_context(ensure_synthesis_batch_outputs(self.root,routes));routes.clear()
            self.assertEqual(output,get_synthesis_batch_output_path(self.root,0))
            legacy=str(self.root/'temp_batch_0.wav')
            self.assertEqual(legacy,Context().run(get_synthesis_batch_output_path,self.root,0))
            self.assertEqual(output,copy_context().run(get_synthesis_batch_output_path,self.root,0))
            expired=copy_context()
            self.assertEqual(str(self.root/'elsewhere/temp_batch_0.wav'),get_synthesis_batch_output_path(self.root/'elsewhere',0))
            with ensure_synthesis_batch_outputs(self.root,{}):self.assertEqual(legacy,get_synthesis_batch_output_path(self.root,0))
            self.assertEqual(output,get_synthesis_batch_output_path(self.root,0))
            with self.assertRaises(ValueError):get_synthesis_batch_output_path(self.root,True)
            result=engine.generate_batch([
                {'index':0,'uid':'line','text':'Actual longer batch words.','speaker':'NARRATOR','instruct':''},
                {'index':1,'uid':'line-two','text':'Short words.','speaker':'NARRATOR','instruct':''}],
                {'NARRATOR':{'type':'custom','voice':'Ryan','seed':42}},str(self.root),42,capture_input=True)
            self.assertEqual([1,0],result['completed'],result);self.assertEqual([],result['failed'])
            self.assertEqual('Actual longer batch words.',result['speech_inputs'][0]['text'])
            self.assertEqual('Short words.',result['speech_inputs'][1]['text'])
            for path,amplitude in ((output,.3),(second,.1)):
                self.assertAlmostEqual(amplitude,float(sf.read(path)[0][0]),places=4)
            self.assertGreater(Path(output).stat().st_size,44)
        self.assertEqual(str(self.root/'temp_batch_0.wav'),get_synthesis_batch_output_path(self.root,0))
        with self.assertRaises(RecoveryAdmissionError):
            expired.run(get_synthesis_batch_output_path,self.root,0)
        self.assertEqual({},self.rows())

    def test_project_batch_publication_cancel_replacement_capacity_defer_and_oom(self):
        import numpy as np
        import soundfile as sf
        from contextlib import ExitStack
        from audio_generation_owner import AudioGenerationCancelled
        from synthesis_settings import get_synthesis_window_settings
        from tests.test_take_publication import TakePublicationTests
        case=TakePublicationTests()
        cases=[(outcome,off) for off in (False,True) for outcome in ('success','relative','mixed','cancel','replace','edit','capacity','defer','oom')]
        for outcome,policies_off in cases:
            with self.subTest(outcome=outcome,policies_off=policies_off),case.fixture(False,enabled=False) as (root,manager,previous,_),ExitStack() as stack:
                if outcome=='relative':manager=type(manager)(os.path.relpath(root))
                state={'cancelled':False,'replaced':False};calls=[];observed=[];attempt_rows=[]
                @contextmanager
                def current_owner(allow_cancelled=False):
                    if state['replaced'] or state['cancelled'] and not allow_cancelled:raise AudioGenerationCancelled()
                    yield
                stack.enter_context(ensure_audio_generation_owner(AudioGenerationOwner('e'*32,current_owner)))
                class Model:
                    def generate_custom_voice(self,**kwargs):
                        if type(kwargs['text']) is not list:raise RuntimeError('Expected native list batching')
                        calls.append(list(kwargs['text']))
                        with workspace.SynthesisWorkspaceRegistry(root) as store:attempt_rows.append(set(store.body['rows']))
                        if outcome=='oom' and len(calls)==1:raise RuntimeError('CUDA out of memory')
                        return [np.full(2400,.1 if text.startswith('Second') else .3) for text in kwargs['text']],24000
                engine=case.engine();engine._synthesis_recovery=None;engine._local_custom_model=Model()
                engine._synthesis_windows=None if policies_off else get_synthesis_window_settings({'enabled':True,'budgets':{'local.custom':{'max_chars':10000}}})
                engine._custom_warmup_needed=False;engine._sub_batch_max_items=0;engine._sub_batch_enabled=False;manager.engine=engine
                chunks=manager.load_chunks();chunks.append({**chunks[0],'id':1,'uid':'line-two','text':'Second synthetic words.'});manager.save_chunks(chunks)
                voices={'NARRATOR':{'type':'custom','voice':'Ryan','seed':42}}
                if outcome=='mixed':
                    chunks[1]['speaker']='DESIGN';manager.save_chunks(chunks)
                    voices['DESIGN']={'type':'design','description':'Synthetic voice.'}
                    def legacy_design(**kwargs):
                        sf.write(kwargs['output_path'],np.full(2400,.1),24000);return True
                    stack.enter_context(patch.object(engine,'generate_design_voice',side_effect=legacy_design))
                Path(manager.voice_config_path).write_text(json.dumps(voices))
                prior_hash=hashlib.sha256(previous.read_bytes()).hexdigest();original=manager._publish_generated_chunk_audio
                def publish(chunk,path,*args,**kwargs):
                    observed.append(Path(path));self.assertTrue(Path(path).is_file())
                    if policies_off:
                        self.assertIsNone(chunk.get('_synthesis_receipt'));self.assertIsNone(chunk.get('_speech_input'));self.assertIsNone(kwargs.get('publication_admission'));self.assertNotIn('recovery_parent',kwargs)
                    self.assertAlmostEqual(.1 if chunk['uid']=='line-two' else .3,float(sf.read(path)[0][0]),places=4)
                    if outcome=='mixed':
                        self.assertEqual(chunk['uid']=='line',Path(path).is_relative_to(root/workspace.DIRECTORY))
                    if outcome in ('cancel','replace'):
                        state['cancelled']=True;state['replaced']=outcome=='replace';raise AudioGenerationCancelled()
                    if outcome=='edit':manager._update_chunk_fields_by_uid(chunk['uid'],text='Changed '+chunk['uid'])
                    return original(chunk,path,*args,**kwargs)
                stack.enter_context(patch.object(manager,'_publish_generated_chunk_audio',side_effect=publish))
                removed=stack.enter_context(patch.object(manager,'_remove_temp_file',wraps=manager._remove_temp_file))
                if outcome=='capacity':stack.enter_context(patch.object(workspace,'MAX_ROWS',1))
                if outcome=='defer':stack.enter_context(patch.object(workspace.SynthesisWorkspaceRegistry,'retire',side_effect=OSError('defer batch cleanup')))
                if outcome=='capacity':
                    with self.assertRaises(workspace.SynthesisWorkspaceUnavailable):manager.generate_chunks_batch([0,1],42,2)
                    self.assertEqual([],calls)
                else:
                    result=manager.generate_chunks_batch([0,1],42,2)
                    if outcome in ('success','relative','mixed','defer','oom'):self.assertEqual([0,1],sorted(result['completed']))
                    if outcome in ('cancel','replace','edit'):self.assertEqual([],result['completed'])
                    self.assertEqual([2,1,1] if outcome=='oom' else [1] if outcome=='mixed' else [2],[len(row) for row in calls])
                self.assertFalse(list(root.glob('temp_batch_*.wav')))
                if policies_off:self.assertFalse((root/'.synthesis-recovery-v1').exists())
                for path in observed:
                    if path.is_relative_to(root/workspace.DIRECTORY):
                        self.assertNotIn(str(path),[call.args[0] for call in removed.call_args_list])
                    else:self.assertIn(str(path),[call.args[0] for call in removed.call_args_list])
                if outcome in ('cancel','replace','edit','capacity'):self.assertEqual(prior_hash,hashlib.sha256(previous.read_bytes()).hexdigest())
                with workspace.SynthesisWorkspaceRegistry(root) as store:
                    self.assertEqual(2 if outcome in ('replace','defer') else 0,len(store.body['rows']))
                if outcome=='oom':self.assertTrue(attempt_rows[0].isdisjoint(set().union(*attempt_rows[1:])))
                stack.close()
                with ensure_startup_recovery(root,timeout=0) as idle:
                    self.assertTrue(idle)
                    with ensure_book_state(root):workspace.apply_abandoned_synthesis_workspace_cleanup(root)
                with workspace.SynthesisWorkspaceRegistry(root) as store:self.assertEqual({},store.body['rows'])

    def test_parent_only_schema_exclusive_output_and_unknown_window_preservation(self):
        with ensure_audio_generation_owner(self.owner):
            with workspace.ensure_synthesis_parent_output(self.parent) as output:
                path=Path(output);self.assertEqual(0,path.stat().st_size)
                rows=self.rows();row=next(iter(rows.values()));self.assertEqual(0,row['windows'])
                for invalid in (True,False,-1,1,workspace.MAX_WINDOWS+1):
                    body={'version':1,'rows':copy.deepcopy(rows)}
                    next(iter(body['rows'].values()))['windows']=invalid
                    with self.subTest(invalid=invalid),self.assertRaises(workspace.SynthesisWorkspaceUnavailable):
                        workspace._validate(body)
                with workspace.SynthesisWorkspaceRegistry(self.root) as store:
                    with self.assertRaises(FileExistsError):store.create_encoded_parent(path.parent.name,'.wav')
                (path.parent/'window-0.wav').write_bytes(b'not declared for parent')
        self.assertTrue(path.exists());self.assertEqual(0,self.cleanup_idle())
        (path.parent/'window-0.wav').unlink()
        self.assertEqual(1,self.cleanup_idle());self.assertFalse(path.exists())
        # A hard exit mid-publish leaves a declared window's staging file
        # (local design writes `<window>.pending.<uuid4 hex>`): still reclaimed.
        name,_row=self.create(windows=2);space=self.root/workspace.DIRECTORY/name
        (space/'window-0.wav').write_bytes(b'member');(space/('window-1.wav.pending.'+'e'*32)).write_bytes(b'staged')
        self.assertEqual(1,self.cleanup_idle());self.assertFalse(space.exists())
        # Lookalikes are not staging of a declared window: everything is kept.
        for odd in ('window-2.wav.pending.'+'e'*32,'window-0.wav.pending.'+'E'*32,'window-0.wav.pending.'+'e'*31,
                    'window-0.wav.pending.'+'e'*32+'.wav','window-0.wav.tmp.'+'e'*32,'link'):
            with self.subTest(odd=odd):
                name,_row=self.create(windows=2);space=self.root/workspace.DIRECTORY/name
                (space/'window-0.wav').write_bytes(b'member')
                if odd=='link':(space/('window-1.wav.pending.'+'e'*32)).symlink_to(space/'window-0.wav')
                else:(space/odd).write_bytes(b'unknown')
                self.cleanup_idle();self.assertTrue((space/'window-0.wav').exists())
                for child in space.iterdir():
                    if child.is_symlink() or not child.is_dir():child.unlink()
                self.assertEqual(1,self.cleanup_idle());self.assertFalse(space.exists())

        # Design also stages a Project parent, which declares zero windows.
        name,_row=self.create(windows=0);space=self.root/workspace.DIRECTORY/name
        parent_staging=space/('assembled-parent.wav.pending.'+'f'*32)
        parent_staging.write_bytes(b'parent staging')
        for alias in ('assembled-parent.mp3.pending.'+'f'*32,
                      'assembled-parent.wav.pending.'+'g'*32,
                      'window-0.wav.pending.'+'f'*32):
            unknown=space/alias;unknown.write_bytes(b'preserved')
            with self.subTest(unknown_staging=alias):
                self.assertEqual(0,self.cleanup_idle());self.assertTrue(parent_staging.exists())
            unknown.unlink()
        self.assertEqual(1,self.cleanup_idle());self.assertFalse(space.exists())

        from contextvars import copy_context
        with ensure_audio_generation_owner(self.owner):
            with workspace.ensure_synthesis_parent_output(self.parent,encoding=True) as raw:
                stage=workspace.get_registered_encoding_stage(raw)
                original_fstat=os.fstat
                def shared_root(fd):
                    info=original_fstat(fd)
                    if (info.st_dev,info.st_ino)==stage.root_identity:
                        fields=list(info);fields[4]=os.geteuid()+1
                        return os.stat_result(fields)
                    return info
                with patch('synthesis_workspace.os.fstat',side_effect=shared_root):
                    self.assertIs(stage,workspace.get_registered_encoding_stage(raw))
                Path(raw).write_bytes(b'private raw bytes')
                expired=copy_context()
                self.assertTrue(next(iter(self.rows().values()))['encoding'])
                self.assertEqual(b'private raw bytes',Path(stage.copy_wav()).read_bytes())
                self.assertTrue(workspace.defer_registered_encoding_cleanup(raw))
                with self.assertRaises(FileExistsError):stage.create_file('encoded-parent.wav')
                for marker,windows in ((False,0),(1,0),('yes',0),(True,2)):
                    body={'version':1,'rows':copy.deepcopy(self.rows())}
                    row=next(iter(body['rows'].values()));row.update(encoding=marker,windows=windows)
                    with self.subTest(marker=marker,windows=windows),self.assertRaises(workspace.SynthesisWorkspaceUnavailable):workspace._validate(body)
                with workspace.ensure_synthesis_parent_output(self.parent,encoding=True) as second:
                    self.assertEqual(2,len(self.rows()))
                    self.assertIs(stage,workspace.get_registered_encoding_stage(raw))
                    self.assertIsNot(stage,workspace.get_registered_encoding_stage(second))
        self.assertEqual({},self.rows())
        with self.assertRaises(workspace.SynthesisWorkspaceUnavailable):expired.run(workspace.get_registered_encoding_stage,raw)
        relative_owner,relative_parent=get_parent(Path(os.path.relpath(self.root)))
        with ensure_audio_generation_owner(relative_owner):
            with workspace.ensure_synthesis_parent_output(relative_parent,encoding=True) as relative_raw:
                self.assertFalse(Path(relative_raw).is_absolute())
                relative_stage=workspace.get_registered_encoding_stage(relative_raw)
                self.assertTrue(relative_stage.directory.is_absolute())
                self.assertTrue(workspace.defer_registered_encoding_cleanup(relative_raw))
        self.assertEqual({},self.rows())

        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'book';root.mkdir(mode=0o700)
            alias=Path(tmp)/'book-link';alias.symlink_to(root,target_is_directory=True)
            owner,parent=get_parent(alias)
            with ensure_audio_generation_owner(owner),workspace.ensure_synthesis_parent_output(parent,encoding=True) as raw:
                stage=workspace.get_registered_encoding_stage(raw)
                self.assertEqual(root/workspace.DIRECTORY,stage.directory.parent)
                Path(raw).write_bytes(b'aliased root audio')
                self.assertEqual(b'aliased root audio',Path(stage.copy_wav()).read_bytes())
                self.assertTrue(workspace.defer_registered_encoding_cleanup(raw))
                other=Path(tmp)/'other-book';other.mkdir(mode=0o700)
                alias.unlink();alias.symlink_to(other,target_is_directory=True)
                with self.assertRaises(workspace.SynthesisWorkspaceUnavailable):workspace.get_registered_encoding_stage(raw)
                self.assertTrue(workspace.defer_registered_encoding_cleanup(raw))
                (other/'sentinel').write_bytes(b'preserved other root')
            with workspace.SynthesisWorkspaceRegistry(root) as store:self.assertEqual({},store.body['rows'])
            self.assertFalse(stage.directory.exists())
            self.assertEqual(b'preserved other root',(other/'sentinel').read_bytes())

        initial_cwd=os.getcwd()
        try:
            # A root-clamping ../ path can still resolve to this book after
            # chdir (notably with /var/tmp). Use a relative leaf so the fixture
            # actually changes its resolved book rather than assuming it does.
            os.chdir(self.root.parent)
            relative_owner,relative_parent=get_parent(Path(self.root.name))
            with ensure_audio_generation_owner(relative_owner),workspace.ensure_synthesis_parent_output(relative_parent,encoding=True) as raw:
                os.chdir(self.root)
                self.assertTrue(workspace.defer_registered_encoding_cleanup(raw))
                with self.assertRaises(workspace.SynthesisWorkspaceUnavailable):workspace.get_registered_encoding_stage(raw)
        finally:os.chdir(initial_cwd)
        self.assertEqual({},self.rows())

        with tempfile.TemporaryDirectory() as tmp:
            first=Path(tmp)/'first';second=Path(tmp)/'second';first.mkdir();second.mkdir();initial_cwd=os.getcwd()
            try:
                os.chdir(first);owner,parent=get_parent(Path('.'))
                with ensure_audio_generation_owner(owner),workspace.ensure_synthesis_parent_output(parent,encoding=True) as raw:
                    stage=workspace.get_registered_encoding_stage(raw);os.chdir(second)
                    _,other_parent=get_parent(Path('.'))
                    from types import SimpleNamespace
                    with patch.object(workspace.uuid,'uuid4',return_value=SimpleNamespace(hex=stage.directory.name)):
                        with self.assertRaisesRegex(workspace.SynthesisWorkspaceUnavailable,'Ambiguous'):
                            with workspace.ensure_synthesis_parent_output(other_parent,encoding=True):pass
                    with workspace.SynthesisWorkspaceRegistry(second) as store:self.assertEqual({},store.body['rows'])
                    os.chdir(first);self.assertIs(stage,workspace.get_registered_encoding_stage(raw))
            finally:os.chdir(initial_cwd)

        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'book';root.mkdir();owner,parent=get_parent(root)
            with ensure_audio_generation_owner(owner),workspace.ensure_synthesis_parent_output(parent,encoding=True) as raw:
                Path(raw).write_bytes(b'original authority')
                root.rename(Path(tmp)/'moved-book');root.mkdir();(root/'sentinel').write_bytes(b'new root')
            self.assertEqual(b'new root',(root/'sentinel').read_bytes())
            self.assertFalse((root/workspace.DIRECTORY).exists())
            with workspace.SynthesisWorkspaceRegistry(Path(tmp)/'moved-book') as store:self.assertEqual(1,len(store.body['rows']))

        with ensure_audio_generation_owner(self.owner):
            with workspace.ensure_synthesis_parent_output(self.parent,encoding=True) as raw:
                stage=workspace.get_registered_encoding_stage(raw);Path(raw).write_bytes(b'raw source')
                old_profile=sys.getprofile()
                def replace_target(frame,event,value):
                    if (event=='return' and frame.f_code is workspace.RegisteredEncodingStage.create_file.__code__
                            and frame.f_locals.get('name')=='encoded-parent.wav'):
                        sys.setprofile(old_profile)
                        target=stage.directory/'encoded-parent.wav'
                        target.rename(stage.directory/'unknown-original.wav');target.write_bytes(b'replacement')
                try:
                    sys.setprofile(replace_target)
                    with self.assertRaises(workspace.SynthesisWorkspaceUnavailable):
                        stage.copy_wav()
                finally:sys.setprofile(old_profile)
                self.assertEqual(b'replacement',(stage.directory/'encoded-parent.wav').read_bytes())
        self.assertEqual(1,len(self.rows()))

    def test_project_parent_publication_cancel_replacement_and_capacity(self):
        import numpy as np
        from contextlib import ExitStack
        from audio_generation_owner import AudioGenerationCancelled
        from tests.test_take_publication import TakePublicationTests
        class Model:
            def generate_custom_voice(self,**kwargs):return [np.sin(np.arange(2400)*.1)*.2],24000
        case=TakePublicationTests()
        cases=[(outcome,off) for off in (False,True) for outcome in ('success','cancel','replace','edit','capacity','defer')]+[('history',True)]
        for outcome,policies_off in cases:
            with self.subTest(outcome=outcome,policies_off=policies_off),case.fixture(False,enabled=outcome=='history') as (root,manager,previous,_),ExitStack() as stack:
                state={'cancelled':False,'replaced':False};observed=[]
                @contextmanager
                def current_owner(allow_cancelled=False):
                    if state['replaced'] or (state['cancelled'] and not allow_cancelled):
                        raise AudioGenerationCancelled()
                    yield
                stack.enter_context(ensure_audio_generation_owner(AudioGenerationOwner('e'*32,current_owner)))
                engine=case.engine();engine._synthesis_recovery=None;engine._local_custom_model=Model();manager.engine=engine
                if policies_off:engine._synthesis_windows=None
                Path(manager.voice_config_path).write_text(json.dumps({'NARRATOR':{'type':'custom','voice':'Ryan','seed':42}}))
                if outcome=='history':
                    captured=manager.load_chunks()[0]
                    parent=manager._capture_synthesis_recovery_parent(engine,0,captured)
                    with patch('project.ensure_audio_generation_book_work',side_effect=AssertionError('disabled proof book admission')):
                        self.assertIsNone(manager._capture_take_publication_admission(
                            engine,parent,0,captured,{'NARRATOR':{'type':'custom','voice':'Ryan','seed':42}},'NARRATOR'))
                original=manager._publish_generated_chunk_audio
                def publish(chunk,path,*args,**kwargs):
                    observed.append(Path(path));self.assertTrue(Path(path).is_file())
                    if policies_off:
                        self.assertIsNone(chunk.get('_synthesis_receipt'));self.assertIsNone(chunk.get('_speech_input'));self.assertIsNone(kwargs.get('publication_admission'));self.assertNotIn('recovery_parent',kwargs)
                    with workspace.SynthesisWorkspaceRegistry(root) as store:
                        self.assertEqual(2 if outcome=='defer' and not policies_off else 1,len(store.body['rows']))
                    if outcome in ('cancel','replace'):
                        state['cancelled']=True;state['replaced']=outcome=='replace'
                        raise AudioGenerationCancelled()
                    if outcome=='edit':
                        manager._update_chunk_fields_by_uid(chunk['uid'],text='Changed synthetic input.')
                    result=original(chunk,path,*args,**kwargs)
                    self.assertTrue(Path(path).is_file());return result
                stack.enter_context(patch.object(manager,'_publish_generated_chunk_audio',side_effect=publish))
                removed=stack.enter_context(patch.object(manager,'_remove_temp_file',wraps=manager._remove_temp_file))
                if outcome=='capacity':stack.enter_context(patch.object(workspace,'MAX_ROWS',0 if policies_off else 1))
                if outcome=='defer':stack.enter_context(patch.object(workspace.SynthesisWorkspaceRegistry,'retire',side_effect=OSError('defer owned cleanup')))
                success,payload=manager.generate_chunk_audio(0)
                self.assertEqual(outcome in ('success','history','defer'),success,payload)
                self.assertFalse(list(root.glob('chunk_*.wav')))
                if policies_off:
                    self.assertFalse((root/'.synthesis-recovery-v1').exists())
                    for selected in manager.load_chunks():
                        self.assertIsNone(selected.get('synthesis_receipt'));self.assertIsNone(selected.get('take_publication'))
                for path in observed:self.assertNotIn(str(path),[call.args[0] for call in removed.call_args_list])
                with workspace.SynthesisWorkspaceRegistry(root) as store:
                    expected=(1 if policies_off else 2) if outcome=='defer' else 1 if outcome=='replace' else 0
                    self.assertEqual(expected,len(store.body['rows']))
                if outcome in ('replace','defer'):self.assertTrue(observed[0].exists())
                elif observed:self.assertFalse(observed[0].exists())
                if success:self.assertTrue((root/payload).is_file())
                if outcome=='edit':
                    self.assertEqual('Changed synthetic input.',manager.load_chunks()[0]['text'])
                # End the synthetic owner/fault injections before idle recovery.
                stack.close()
                with ensure_startup_recovery(root,timeout=0) as idle:
                    self.assertTrue(idle)
                    with ensure_book_state(root):workspace.apply_abandoned_synthesis_workspace_cleanup(root)
                with workspace.SynthesisWorkspaceRegistry(root) as store:self.assertEqual({},store.body['rows'])
                self.assertTrue(previous.is_file())

        if shutil.which('ffmpeg'):
            import project
            for outcome in ('success','history','wav','cancel','replace','edit','save_error','override','relative','root_alias',
                            'swap_mp3','swap_wav','symlink_mp3','hardlink_mp3','root_retarget','root_assignment','cwd_drift'):
                with self.subTest(registered_codec=outcome),case.fixture(False,enabled=outcome=='history') as (root,manager,previous,_),ExitStack() as stack:
                    stack.enter_context(patch.object(project,'_export_audio_segment',project._RAW_CHUNK_CODEC_HELPERS[1]))
                    before=copy.deepcopy(manager.load_chunks()[0]);prior=previous.read_bytes()
                    if outcome=='cwd_drift':
                        stack.callback(os.chdir,os.getcwd())
                        os.chdir(root.parent)
                    if outcome in ('relative','cwd_drift'):manager.root_dir=os.path.relpath(root)
                    if outcome in ('root_alias','root_retarget'):
                        alias=root/'book-link';alias.symlink_to(root,target_is_directory=True);manager.root_dir=str(alias)
                    state={'cancelled':False,'replaced':False};encoded=[];mutex=threading.Lock()
                    @contextmanager
                    def owner(allow_cancelled=False):
                        if not mutex.acquire(timeout=.25):raise TimeoutError('encoding owner mutex reentry')
                        try:
                            if state['replaced'] or (state['cancelled'] and not allow_cancelled):raise AudioGenerationCancelled()
                            yield
                        finally:mutex.release()
                    stack.enter_context(ensure_audio_generation_owner(AudioGenerationOwner('f'*32,owner)))
                    engine=case.engine();engine._synthesis_recovery=None;engine._synthesis_windows=None;engine._local_custom_model=Model();manager.engine=engine
                    Path(manager.voice_config_path).write_text(json.dumps({'NARRATOR':{'type':'custom','voice':'Ryan','seed':42}}))
                    with patch.object(engine,'get_voice_backend',side_effect=AssertionError('encoding admission queried backend')):
                        self.assertTrue(project._can_register_chunk_encoding(manager))
                    if outcome in ('wav','swap_wav'):stack.enter_context(patch.object(project,'MP3_BITRATE','invalid-bitrate'))
                    if outcome=='override':
                        original=manager._export_chunk_audio
                        def overridden(path,*args,**kwargs):
                            self.assertEqual({},kwargs)
                            self.assertIsNone(workspace.get_registered_encoding_stage(path))
                            return original(path,*args,**kwargs)
                        stack.enter_context(patch.object(manager,'_export_chunk_audio',side_effect=overridden))
                    removed=stack.enter_context(patch.object(manager,'_remove_temp_file',wraps=manager._remove_temp_file))
                    old_profile=sys.getprofile();initial_cwd=os.getcwd();foreign=[]
                    def observe(frame,event,value):
                        if event!='return' or frame.f_code is not project._RAW_CHUNK_EXPORT.__code__ or not isinstance(value,str):return
                        sys.setprofile(old_profile);path=root/value
                        encoded.append((path,hashlib.sha256(Path(frame.f_locals['temp_path']).read_bytes()).hexdigest(),hashlib.sha256(path.read_bytes()).hexdigest()))
                        if outcome=='cancel':state['cancelled']=True
                        elif outcome=='replace':state['replaced']=True
                        elif outcome=='edit':manager._update_chunk_fields_by_uid(before['uid'],text='Edited after encoding.')
                        elif outcome=='save_error':stack.enter_context(patch.object(manager,'_save_chunks_locked',side_effect=OSError('encoded metadata failure')))
                        elif outcome in ('swap_mp3','swap_wav','symlink_mp3','hardlink_mp3'):
                            data=path.read_bytes();path.rename(path.with_name('unknown-original'+path.suffix))
                            if outcome=='symlink_mp3':path.symlink_to(previous)
                            elif outcome=='hardlink_mp3':os.link(previous,path)
                            else:path.write_bytes(data)
                        elif outcome=='root_retarget':
                            other=root/'other-book';other.mkdir();target=other/value;target.parent.mkdir(parents=True)
                            target.write_bytes(b'unrelated staged file');foreign.append(target)
                            alias.unlink();alias.symlink_to(other,target_is_directory=True)
                        elif outcome=='root_assignment':
                            other=root/'other-book';other.mkdir();sentinel=other/'sentinel';sentinel.write_bytes(b'unrelated staged file')
                            foreign.append(sentinel);manager.root_dir=str(other)
                        elif outcome=='cwd_drift':os.chdir(root)
                    try:
                        sys.setprofile(observe);success,payload=manager.generate_chunk_audio(0)
                    finally:sys.setprofile(old_profile);os.chdir(initial_cwd)
                    self.assertEqual(outcome in ('success','history','wav','override','relative','root_alias'),success,payload)
                    self.assertEqual(1,len(encoded));path,raw_hash,encoded_hash=encoded[0]
                    self.assertEqual(outcome!='override',path.is_relative_to(root/workspace.DIRECTORY))
                    self.assertEqual(outcome=='override',path in [Path(os.path.abspath(call.args[0])) for call in removed.call_args_list])
                    self.assertFalse(list((root/'voicelines').glob('.chunk-*')))
                    self.assertFalse(list((root/'voicelines').glob('.render-*')))
                    self.assertEqual(prior,previous.read_bytes())
                    for target in foreign:self.assertEqual(b'unrelated staged file',target.read_bytes())
                    kept=outcome in ('replace','swap_mp3','swap_wav','symlink_mp3','hardlink_mp3')
                    with workspace.SynthesisWorkspaceRegistry(root) as store:self.assertEqual(1 if kept else 0,len(store.body['rows']))
                    if success:
                        self.assertTrue((root/payload).is_file());self.assertNotEqual(before['audio_revision'],manager.load_chunks()[0]['audio_revision'])
                        if outcome=='wav':self.assertEqual(raw_hash,encoded_hash);self.assertEqual('.wav',Path(payload).suffix)
                    else:
                        self.assertEqual(before['audio_revision'],manager.load_chunks()[0]['audio_revision'])
                    stack.close()
                    with ensure_startup_recovery(root,timeout=0) as idle:
                        self.assertTrue(idle)
                        with ensure_book_state(root):workspace.apply_abandoned_synthesis_workspace_cleanup(root)
                    with workspace.SynthesisWorkspaceRegistry(root) as store:self.assertEqual(1 if outcome in ('swap_mp3','swap_wav','symlink_mp3','hardlink_mp3') else 0,len(store.body['rows']))

            with case.fixture(False,enabled=False) as (root,manager,previous,_),case.owner(), \
                    patch.object(project,'_export_audio_segment',project._RAW_CHUNK_CODEC_HELPERS[1]):
                manager.engine=case.engine();manager.engine._synthesis_recovery=None;manager.engine._synthesis_windows=None
                manager.engine._local_custom_model=Model()
                Path(manager.voice_config_path).write_text(json.dumps({'NARRATOR':{'type':'custom','voice':'Ryan','seed':42}}))
                before=copy.deepcopy(manager.load_chunks()[0]);prior=previous.read_bytes();swapped=[]
                old_profile=sys.getprofile()
                def replace_created_slot(frame,event,value):
                    if (event=='return' and frame.f_code is workspace.RegisteredEncodingStage.create_file.__code__
                            and frame.f_locals.get('name')=='encoded-parent.mp3'):
                        sys.setprofile(old_profile);stage=frame.f_locals['self'];path=stage.directory/'encoded-parent.mp3'
                        path.rename(stage.directory/'unknown-created-slot.mp3');path.write_bytes(b'unknown replacement slot');swapped.append(path)
                try:
                    sys.setprofile(replace_created_slot);success,payload=manager.generate_chunk_audio(0)
                finally:sys.setprofile(old_profile)
                self.assertFalse(success,payload);self.assertEqual(1,len(swapped))
                self.assertEqual(b'unknown replacement slot',swapped[0].read_bytes())
                self.assertEqual(before['audio_revision'],manager.load_chunks()[0]['audio_revision'])
                self.assertEqual(prior,previous.read_bytes())

            for race in ('directory','source','destination'):
                with self.subTest(final_rename_race=race),case.fixture(False,enabled=False) as (root,manager,previous,_),case.owner(), \
                        patch.object(project,'_export_audio_segment',project._RAW_CHUNK_CODEC_HELPERS[1]):
                    manager.engine=case.engine();manager.engine._synthesis_recovery=None;manager.engine._synthesis_windows=None
                    manager.engine._local_custom_model=Model()
                    Path(manager.voice_config_path).write_text(json.dumps({'NARRATOR':{'type':'custom','voice':'Ryan','seed':42}}))
                    before=copy.deepcopy(manager.load_chunks()[0]);prior=previous.read_bytes();triggered=[]
                    outside=root/'outside-stage';outside.mkdir();(outside/'encoded-parent.mp3').write_bytes(b'outside destination')
                    old_profile=sys.getprofile()
                    def replace_at_rename(frame,event,value):
                        if event!='c_call' or value is not os.replace:return
                        if race=='directory' and frame.f_code is workspace.RegisteredEncodingStage.finish_encoder_output.__code__:
                            sys.setprofile(old_profile);stage=frame.f_locals['self'];triggered.append(stage.directory)
                            stage.directory.rename(root/'moved-stage');stage.directory.symlink_to(outside,target_is_directory=True)
                        elif race=='source' and frame.f_code is project.publish_audio_output.__code__:
                            source=frame.f_back.f_locals.get('self')
                            if type(source) is not workspace.RegisteredEncodingSource:return
                            sys.setprofile(old_profile);path=Path(source.path);data=path.read_bytes();triggered.append(path)
                            path.rename(path.with_name('unknown-original.mp3'));path.write_bytes(data)
                        elif race=='destination' and frame.f_code is project.publish_audio_output.__code__:
                            sys.setprofile(old_profile);voicelines=root/'voicelines';triggered.append(voicelines)
                            voicelines.rename(root/'moved-voicelines');voicelines.symlink_to(outside,target_is_directory=True)
                            (outside/Path(frame.f_locals['output_path']).name).write_bytes(b'unrelated destination')
                    try:
                        sys.setprofile(replace_at_rename);success,payload=manager.generate_chunk_audio(0)
                    finally:sys.setprofile(old_profile)
                    self.assertFalse(success,payload);self.assertEqual(1,len(triggered))
                    self.assertEqual(before['audio_revision'],manager.load_chunks()[0]['audio_revision'])
                    actual_previous=root/'moved-voicelines'/previous.name if race=='destination' else previous
                    self.assertEqual(prior,actual_previous.read_bytes())
                    self.assertEqual(b'outside destination',(outside/'encoded-parent.mp3').read_bytes())
                    if race=='destination':
                        self.assertEqual([b'outside destination',b'unrelated destination'],
                            sorted((p.read_bytes() for p in outside.iterdir()),key=lambda value:value))
                    with workspace.SynthesisWorkspaceRegistry(root) as store:self.assertEqual(0 if race=='destination' else 1,len(store.body['rows']))

            for mutation in ('output_inplace','missing_report','oversize_report','duplicate_report','bad_type_report','nonfinite_report','wrong_inode_report','wav_inplace'):
                with self.subTest(child_completion=mutation),case.fixture(False,enabled=False) as (root,manager,previous,_),case.owner(), \
                        patch.object(project,'_export_audio_segment',project._RAW_CHUNK_CODEC_HELPERS[1]),ExitStack() as checks:
                    manager.engine=case.engine();manager.engine._synthesis_recovery=None;manager.engine._synthesis_windows=None
                    manager.engine._local_custom_model=Model()
                    Path(manager.voice_config_path).write_text(json.dumps({'NARRATOR':{'type':'custom','voice':'Ryan','seed':42}}))
                    if mutation=='wav_inplace':checks.enter_context(patch.object(project,'MP3_BITRATE','invalid-bitrate'))
                    before=copy.deepcopy(manager.load_chunks()[0]);prior=previous.read_bytes();triggered=[];old_profile=sys.getprofile()
                    def corrupt_completion(frame,event,value):
                        if (mutation=='wav_inplace' and event=='c_return'
                                and frame.f_code is workspace.RegisteredEncodingStage.copy_wav.__code__
                                and getattr(value,'__name__',None)=='flush'):
                            sys.setprofile(old_profile);triggered.append(True)
                            os.pwrite(frame.f_locals['writer'].fileno(),b'X',0);return
                        if mutation=='wav_inplace' or event!='return' or frame.f_code is not subprocess.Popen.wait.__code__ or value!=0:return
                        process=frame.f_locals['self']
                        if not any(isinstance(arg,str) and arg.endswith('/audio_export.py') for arg in process.args):return
                        sys.setprofile(old_profile);settings=json.loads(process.args[-1]);triggered.append(True)
                        report=Path(settings['exception_path']);output=report.parent/'output.mp3'
                        if mutation=='output_inplace':
                            with output.open('r+b') as stream:
                                data=stream.read();stream.seek(0);stream.write(bytes([data[0]^1])+data[1:])
                        elif mutation=='missing_report':report.write_bytes(b'')
                        elif mutation=='oversize_report':report.write_bytes(b'x'*4097)
                        elif mutation=='duplicate_report':
                            text=report.read_text();report.write_text('{"status":"success",'+text[1:])
                        elif mutation=='nonfinite_report':report.write_text('{"status":"success","output":"output.mp3","version":[NaN]}')
                        else:
                            body=json.loads(report.read_text())
                            if mutation=='bad_type_report':body['version'][0]=True
                            else:body['version'][1]+=1
                            report.write_text(json.dumps(body))
                    try:
                        sys.setprofile(corrupt_completion);success,payload=manager.generate_chunk_audio(0)
                    finally:sys.setprofile(old_profile)
                    self.assertFalse(success,payload);self.assertEqual([True],triggered)
                    self.assertEqual(before['audio_revision'],manager.load_chunks()[0]['audio_revision'])
                    self.assertEqual(prior,previous.read_bytes())

            for target,name in ((project,'validate_generated_audio'),(workspace,'_check_encoding_source'),
                                (workspace,'_check_encoding_destination')):
                with self.subTest(late_helper=name),case.fixture(False,enabled=False) as (root,manager,previous,_),case.owner(), \
                        patch.object(project,'_export_audio_segment',project._RAW_CHUNK_CODEC_HELPERS[1]):
                    manager.engine=case.engine();before=manager.load_chunks()[0]
                    parent=manager._capture_synthesis_recovery_parent(manager.engine,0,before)
                    with workspace.ensure_synthesis_parent_output(parent,encoding=True) as raw:
                        Path(raw).write_bytes(previous.read_bytes())
                        with patch.object(target,name,side_effect=AssertionError('unknown helper received registered input')) as changed:
                            with self.assertRaises(workspace.SynthesisWorkspaceUnavailable):manager._publish_generated_chunk_audio(before,raw,'NARRATOR')
                            changed.assert_not_called()

            import audio_validation
            for target,name in ((audio_validation,'publish_audio_output'),(None,'get_chunk_audio_path')):
                with self.subTest(late_public_helper=name),case.fixture(False,enabled=False) as (root,manager,previous,_),case.owner(), \
                        patch.object(project,'_export_audio_segment',project._RAW_CHUNK_CODEC_HELPERS[1]):
                    manager.engine=case.engine();before=manager.load_chunks()[0]
                    parent=manager._capture_synthesis_recovery_parent(manager.engine,0,before)
                    with workspace.ensure_synthesis_parent_output(parent,encoding=True) as raw:
                        Path(raw).write_bytes(previous.read_bytes())
                        with patch.object(manager if target is None else target,name,side_effect=AssertionError('unknown helper received capability')) as changed:
                            with self.assertRaises(workspace.SynthesisWorkspaceUnavailable):manager._publish_generated_chunk_audio(before,raw,'NARRATOR')
                            changed.assert_not_called()

            for alias in ('symlink','hardlink'):
                with self.subTest(raw_parent_alias=alias),case.fixture(False,enabled=False) as (root,manager,previous,_),case.owner(), \
                        patch.object(project,'_export_audio_segment',project._RAW_CHUNK_CODEC_HELPERS[1]):
                    manager.engine=case.engine();before=manager.load_chunks()[0];prior=previous.read_bytes()
                    parent=manager._capture_synthesis_recovery_parent(manager.engine,0,before)
                    with workspace.ensure_synthesis_parent_output(parent,encoding=True) as raw:
                        Path(raw).unlink()
                        if alias=='symlink':Path(raw).symlink_to(previous)
                        else:os.link(previous,raw)
                        with self.assertRaises(workspace.SynthesisWorkspaceUnavailable):manager._export_chunk_audio(raw,'.unused')
                        self.assertEqual(prior,previous.read_bytes())

            import audio_export
            from audio_export import export_audio_segment
            from pydub import AudioSegment
            for name in ('encoded-parent.mp3','encoded-parent.wav'):
                for alias in ('regular','symlink','hardlink'):
                    with self.subTest(encoded_slot=name,alias=alias),case.fixture(False,enabled=False) as (root,manager,previous,_),case.owner(), \
                            patch.object(project,'_export_audio_segment',project._RAW_CHUNK_CODEC_HELPERS[1]):
                        manager.engine=case.engine();before=manager.load_chunks()[0];prior=previous.read_bytes()
                        parent=manager._capture_synthesis_recovery_parent(manager.engine,0,before)
                        with workspace.ensure_synthesis_parent_output(parent,encoding=True) as raw:
                            stage=workspace.get_registered_encoding_stage(raw);Path(raw).write_bytes(prior)
                            target=stage.directory/name
                            if alias=='regular':target.write_bytes(b'preexisting member')
                            elif alias=='symlink':target.symlink_to(previous)
                            else:os.link(previous,target)
                            with self.assertRaises(workspace.SynthesisWorkspaceUnavailable):manager._export_chunk_audio(raw,'.unused')
                            self.assertEqual(prior,previous.read_bytes())
                            if name=='encoded-parent.mp3':self.assertFalse((stage.directory/'encoded-parent.wav').exists())
            for name in ('output.mp3','encoder-error.json'):
                for alias in ('symlink','hardlink'):
                    with self.subTest(encoder_member=name,alias=alias),tempfile.TemporaryDirectory() as tmp:
                        root=Path(tmp);outside=root/'outside';outside.write_bytes(b'preserved outside bytes')
                        owner,parent=get_parent(root)
                        with ensure_audio_generation_owner(owner),workspace.ensure_synthesis_parent_output(parent,encoding=True) as raw:
                            stage=workspace.get_registered_encoding_stage(raw)
                            fd=stage.create_file('encoded-parent.mp3');os.close(fd)
                            member=stage.directory/name
                            if alias=='symlink':member.symlink_to(outside)
                            else:os.link(outside,member)
                            with self.assertRaises(workspace.SynthesisWorkspaceUnavailable):
                                export_audio_segment(AudioSegment.silent(50),stage.directory/'encoded-parent.mp3','mp3',None,
                                    workspace=stage,bitrate='invalid-bitrate' if name=='encoder-error.json' else '128k')
                            self.assertEqual(b'preserved outside bytes',outside.read_bytes())

            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);owner,parent=get_parent(root)
                with ensure_audio_generation_owner(owner),workspace.ensure_synthesis_parent_output(parent,encoding=True) as raw:
                    stage=workspace.get_registered_encoding_stage(raw)
                    fd=stage.create_file('encoded-parent.mp3');os.close(fd)
                    (stage.directory/'join'/'pydub.py').write_text("raise RuntimeError('scratch must never supply trusted code')\n")
                    with patch.dict(os.environ,{'PYTHONPATH':'.'+os.pathsep+os.environ.get('PYTHONPATH','')}):
                        export_audio_segment(AudioSegment.silent(50),stage.directory/'encoded-parent.mp3','mp3',None,
                            workspace=stage,bitrate='128k')
                    self.assertGreater((stage.directory/'encoded-parent.mp3').stat().st_size,1024)

            with tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);owner,parent=get_parent(root);drift=root/'drift';drift.mkdir()
                (drift/'pydub.py').write_text("from pathlib import Path\nPath(__file__).with_suffix('.loaded').write_text('bad')\nraise RuntimeError('wrong cwd import')\n")
                initial_cwd=os.getcwd();old_profile=sys.getprofile();triggered=[]
                with ensure_audio_generation_owner(owner),workspace.ensure_synthesis_parent_output(parent,encoding=True) as raw:
                    stage=workspace.get_registered_encoding_stage(raw);fd=stage.create_file('encoded-parent.mp3');os.close(fd)
                    def drift_before_spawn(frame,event,value):
                        if event=='call' and frame.f_code is audio_export.start_owned_subprocess.__code__:
                            sys.setprofile(old_profile);triggered.append(True);os.chdir(drift)
                    try:
                        with patch.dict(os.environ,{'PYTHONPATH':'.'+os.pathsep+os.environ.get('PYTHONPATH','')}):
                            sys.setprofile(drift_before_spawn)
                            export_audio_segment(AudioSegment.silent(50),stage.directory/'encoded-parent.mp3','mp3',None,
                                workspace=stage,bitrate='128k')
                    finally:sys.setprofile(old_profile);os.chdir(initial_cwd)
                    self.assertEqual([True],triggered);self.assertFalse((drift/'pydub.loaded').exists())
                    self.assertGreater((stage.directory/'encoded-parent.mp3').stat().st_size,1024)

            for alias in ('symlink','directory'):
                with self.subTest(late_encoder_join=alias),tempfile.TemporaryDirectory() as tmp:
                    root=Path(tmp);outside=root/'outside';outside.mkdir();(outside/'sentinel').write_bytes(b'preserved')
                    owner,parent=get_parent(root)
                    with ensure_audio_generation_owner(owner),workspace.ensure_synthesis_parent_output(parent,encoding=True) as raw:
                        stage=workspace.get_registered_encoding_stage(raw)
                        fd=stage.create_file('encoded-parent.mp3');os.close(fd)
                        old_profile=sys.getprofile()
                        def replace_join(frame,event,value):
                            if event=='call' and frame.f_code is audio_export.start_owned_subprocess.__code__:
                                sys.setprofile(old_profile);join=stage.directory/'join';join.rename(root/'original-join')
                                if alias=='symlink':join.symlink_to(outside,target_is_directory=True)
                                else:join.mkdir(mode=0o700)
                        try:
                            sys.setprofile(replace_join)
                            with self.assertRaises(workspace.SynthesisWorkspaceUnavailable):
                                export_audio_segment(AudioSegment.silent(50),stage.directory/'encoded-parent.mp3','mp3',None,
                                    workspace=stage,bitrate='128k')
                        finally:sys.setprofile(old_profile)
                        report=json.loads((stage.directory/'encoder-error.json').read_text())
                        self.assertIn(report['type'],('NotADirectoryError','ValueError'))
                        self.assertEqual(['sentinel'],[p.name for p in outside.iterdir()])
                        self.assertEqual(b'preserved',(outside/'sentinel').read_bytes())

            for name in ('input.pcm','output.mp3','encoder-error.json'):
                for alias in ('symlink','hardlink'):
                    with self.subTest(late_encoder_alias=name,alias=alias),tempfile.TemporaryDirectory() as tmp:
                        root=Path(tmp);outside=root/'outside';outside.write_bytes(b'preserved outside bytes')
                        owner,parent=get_parent(root)
                        with ensure_audio_generation_owner(owner),workspace.ensure_synthesis_parent_output(parent,encoding=True) as raw:
                            stage=workspace.get_registered_encoding_stage(raw)
                            fd=stage.create_file('encoded-parent.mp3');os.close(fd)
                            old_profile=sys.getprofile()
                            def replace_before_child(frame,event,value):
                                if event=='call' and frame.f_code is audio_export.start_owned_subprocess.__code__:
                                    sys.setprofile(old_profile)
                                    member=stage.directory/name;member.unlink()
                                    if alias=='symlink':member.symlink_to(outside)
                                    else:os.link(outside,member)
                            try:
                                sys.setprofile(replace_before_child)
                                with self.assertRaises(workspace.SynthesisWorkspaceUnavailable):
                                    export_audio_segment(AudioSegment.silent(50),stage.directory/'encoded-parent.mp3','mp3',None,
                                        workspace=stage,bitrate='invalid-bitrate' if name=='encoder-error.json' else '128k')
                            finally:sys.setprofile(old_profile)
                            self.assertEqual(b'preserved outside bytes',outside.read_bytes())

        # Replacing a writer after parent admission must not receive the live
        # encoding scope that was granted to the original implementation.
        import project
        with case.fixture(False,enabled=False) as (root,manager,previous,_),case.owner(), \
                patch.object(project,'_export_audio_segment',project._RAW_CHUNK_CODEC_HELPERS[1]):
            manager.engine=case.engine();before=manager.load_chunks()[0]
            parent=manager._capture_synthesis_recovery_parent(manager.engine,0,before)
            with workspace.ensure_synthesis_parent_output(parent,encoding=True) as raw:
                Path(raw).write_bytes(previous.read_bytes())
                with patch.object(manager,'_export_chunk_audio',side_effect=AssertionError('late unknown writer invoked')) as changed:
                    with self.assertRaisesRegex(workspace.SynthesisWorkspaceUnavailable,'writer changed'):
                        manager._publish_generated_chunk_audio(before,raw,'NARRATOR')
                    changed.assert_not_called()

    def test_unknown_aliases_links_and_substituted_directory_preserve_all_files(self):
        kinds=('unknown','symlink','hardlink','join_alias','root_alias','changed_directory')
        for windows,kind in ((n,k) for n in (0,2) for k in kinds):
            with self.subTest(windows=windows,kind=kind),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);owner,parent=get_parent(root)
                with workspace.SynthesisWorkspaceRegistry(root,create=True) as store:name,row=store.reserve(parent,windows)
                owned='window-0.wav' if windows else 'assembled-parent.wav'
                alias='window-1.wav' if windows else 'assembled-parent.mp3'
                path=root/workspace.DIRECTORY/name;(path/owned).write_bytes(b'owned')
                outside=root/'outside.wav';outside.write_bytes(b'outside')
                if kind=='unknown':(path/'unknown.txt').write_bytes(b'preserve')
                elif kind=='symlink':(path/alias).symlink_to(outside)
                elif kind=='hardlink':os.link(outside,path/alias)
                elif kind=='join_alias':(path/'join').rmdir();(path/'join').symlink_to(root,target_is_directory=True)
                elif kind=='root_alias':path.rename(path.with_name(name+'old'));path.symlink_to(root,target_is_directory=True)
                elif kind=='changed_directory':path.rename(path.with_name(name+'old'));path.mkdir(mode=0o700)
                self.assertEqual(0,workspace.apply_abandoned_synthesis_workspace_cleanup(root))
                with workspace.SynthesisWorkspaceRegistry(root) as store:self.assertEqual(row,store.body['rows'][name])
                self.assertEqual(b'outside',outside.read_bytes())
                original=path.with_name(name+'old') if kind in ('root_alias','changed_directory') else path
                self.assertEqual(b'owned',(original/owned).read_bytes())

        for kind in ('clean','symlink','hardlink','nested','join_mode','join_replaced','unknown_root',
                     'overcount','oversize','legacy_join'):
            with self.subTest(encoding=kind),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);owner,parent=get_parent(root)
                with workspace.SynthesisWorkspaceRegistry(root,create=True) as store:
                    name,row=store.reserve(parent,0,encoding=kind!='legacy_join')
                path=root/workspace.DIRECTORY/name;raw=path/'assembled-parent.wav';raw.write_bytes(b'raw')
                outside=root/'outside.wav';outside.write_bytes(b'outside')
                if kind!='legacy_join':(path/'encoded-parent.mp3').write_bytes(b'encoded')
                join=path/'join';first=join/'tmpA';first.write_bytes(b'encoder scratch')
                (join/'tmpB').write_bytes(b'other scratch')
                if kind in ('symlink','hardlink'):
                    first.unlink()
                    if kind=='symlink':first.symlink_to(outside)
                    else:os.link(outside,first)
                elif kind=='nested':(join/'nested').mkdir(mode=0o700)
                elif kind=='join_mode':join.chmod(0o755)
                elif kind=='join_replaced':join.rename(root/'original-join');join.mkdir(mode=0o700)
                elif kind=='unknown_root':(path/'unknown.txt').write_bytes(b'unknown')
                with patch.object(workspace,'MAX_ENCODER_TEMP_FILES',1 if kind=='overcount' else 16), \
                        patch.object(workspace,'MAX_ENCODER_TEMP_BYTES',1 if kind=='oversize' else 8*1024**3):
                    self.assertEqual(1 if kind=='clean' else 0,workspace.apply_abandoned_synthesis_workspace_cleanup(root))
                self.assertEqual(b'outside',outside.read_bytes())
                if kind!='clean':
                    self.assertEqual(b'raw',raw.read_bytes())
                    with workspace.SynthesisWorkspaceRegistry(root) as store:self.assertEqual(row,store.body['rows'][name])

    def test_reserved_creation_gap_does_not_authorize_deleting_present_directory(self):
        with workspace.SynthesisWorkspaceRegistry(self.root,create=True) as store:
            with patch('synthesis_workspace.os.mkdir',side_effect=OSError('before mkdir')):
                with self.assertRaises(OSError):store.reserve(self.parent,2)
        self.assertEqual(1,self.cleanup_idle());self.assertEqual({},self.rows())
        with workspace.SynthesisWorkspaceRegistry(self.root) as store:
            original=store.save;calls=[]
            def save():
                calls.append(1)
                if len(calls)==2:raise OSError('identity commit failed')
                return original()
            with patch.object(store,'save',side_effect=save):
                with self.assertRaises(OSError):store.reserve(self.parent,2)
        rows=self.rows();name=next(iter(rows));path=self.root/workspace.DIRECTORY/name
        self.assertIsNone(rows[name]['identity'])
        self.assertEqual(['join'],sorted(p.name for p in path.iterdir()))
        self.assertEqual(0,self.cleanup_idle());self.assertTrue(path.exists());self.assertEqual(rows,self.rows())

    def test_unlink_and_directory_sync_fault_keep_charge_and_retry(self):
        for operation in ('unlink','fsync'):
            with self.subTest(operation=operation):
                name,row=self.create();path=self.root/workspace.DIRECTORY/name
                (path/'window-0.wav').write_bytes(b'working')
                with patch('synthesis_workspace.os.'+operation,side_effect=OSError('cleanup IO fault')):
                    self.assertEqual(0,self.cleanup_idle())
                self.assertIn(name,self.rows())
                self.assertEqual(1,self.cleanup_idle());self.assertEqual({},self.rows())

    def test_corrupt_missing_and_aliased_authority_never_reset_or_scan_legacy(self):
        for kind in ('corrupt','missing','alias'):
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp);owner,parent=get_parent(root)
                with workspace.SynthesisWorkspaceRegistry(root,create=True) as store:name,row=store.reserve(parent,2)
                path=root/workspace.DIRECTORY/name;(path/'window-0.wav').write_bytes(b'working')
                legacy=root/'.synthesis-render-legacy';legacy.mkdir();(legacy/'window-0.wav').write_bytes(b'legacy')
                index=root/workspace.DIRECTORY/workspace.INDEX
                if kind=='corrupt':index.write_bytes(b'corrupt authority')
                elif kind=='missing':index.unlink()
                else:index.unlink();index.symlink_to(legacy/'window-0.wav')
                self.assertEqual(0,workspace.apply_abandoned_synthesis_workspace_cleanup(root))
                self.assertEqual(b'working',(path/'window-0.wav').read_bytes())
                self.assertEqual(b'legacy',(legacy/'window-0.wav').read_bytes())
                with self.assertRaises((OSError,workspace.SynthesisWorkspaceUnavailable)):
                    with workspace.SynthesisWorkspaceRegistry(root,create=True):pass

    def test_capacity_refuses_before_mkdir_and_never_falls_back(self):
        name,row=self.create()
        with patch.object(workspace,'MAX_ROWS',1):
            with workspace.SynthesisWorkspaceRegistry(self.root) as store:
                with self.assertRaisesRegex(workspace.SynthesisWorkspaceUnavailable,'capacity'):
                    store.reserve(self.parent,2)
        self.assertEqual({name:row},self.rows())
        self.assertEqual({name,workspace.INDEX,workspace.INDEX+'.lock'},
                         {path.name for path in (self.root/workspace.DIRECTORY).iterdir()})

    def test_real_render_success_and_late_failure_preserve_selected_parent(self):
        import numpy as np
        import soundfile as sf
        from synthesis_render import get_synthesis_window_plan,render_synthesis_windows
        from synthesis_settings import get_synthesis_window_settings
        policy=get_synthesis_window_settings({'enabled':True,'budgets':{'local.custom':{'max_chars':5}}})
        plan=get_synthesis_window_plan('One. Two.',policy,['local.custom'])
        output=self.root/'parent.wav';output.write_bytes(b'prior')
        paths=[]
        def render(text,path):
            paths.append(Path(path));sf.write(path,np.sin(np.arange(2400)*.1)*.2,24000);return True
        with ensure_audio_generation_owner(self.owner):
            receipt=render_synthesis_windows(plan,render,output,policy,workspace_parent=self.parent)
        self.assertEqual(4800,sf.info(output).frames)
        self.assertEqual(4800,receipt['output_frames']);self.assertEqual({},self.rows())
        self.assertTrue(all(path.parent.parent.name==workspace.DIRECTORY for path in paths))
        self.assertFalse(list(self.root.glob('.synthesis-*render*')))
        self.assertFalse(list(self.root.glob('.synthesis-join-*')))
        prior=output.read_bytes();calls=[]
        def fail(text,path):
            calls.append(text)
            if len(calls)==2:raise RuntimeError('second provider failed')
            return render(text,path)
        with ensure_audio_generation_owner(self.owner),self.assertRaisesRegex(RuntimeError,'provider failed'):
            render_synthesis_windows(plan,fail,output,policy,workspace_parent=self.parent)
        self.assertEqual(prior,output.read_bytes());self.assertEqual({},self.rows())

    def test_after_publication_cleanup_failure_keeps_success_and_charge_until_idle(self):
        with ensure_audio_generation_owner(self.owner):
            with patch.object(workspace.SynthesisWorkspaceRegistry,'retire',side_effect=OSError('deferred')):
                with workspace.ensure_synthesis_workspace(self.parent,2) as path:
                    (path/'window-0.wav').write_bytes(b'working')
                    (self.root/'parent.wav').write_bytes(b'committed parent')
        self.assertTrue(path.exists());self.assertEqual(1,len(self.rows()))
        self.assertEqual(1,self.cleanup_idle())
        self.assertEqual(b'committed parent',(self.root/'parent.wav').read_bytes())

    def test_actual_core_preclaim_and_startup_cleanup_use_existing_idle_barrier(self):
        import core
        from tests.test_startup_task_ownership import StartupTaskOwnershipTests
        fixture=StartupTaskOwnershipTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        owner,parent=get_parent(fixture.root)
        with workspace.SynthesisWorkspaceRegistry(fixture.root,create=True) as store:name,row=store.reserve(parent,2)
        path=fixture.root/workspace.DIRECTORY/name;(path/'window-0.wav').write_bytes(b'abandoned')
        claim=core.claim_gpu_task('audio')
        try:
            self.assertFalse(path.exists())
            with ensure_startup_recovery(fixture.root,timeout=0) as idle:self.assertFalse(idle)
        finally:core.release_gpu_task_claim('audio',claim)
        with workspace.SynthesisWorkspaceRegistry(fixture.root) as store:name,row=store.reserve(parent,2)
        path=fixture.root/workspace.DIRECTORY/name;(path/'join'/'complete.wav').write_bytes(b'abandoned join')
        fixture.startup();self.assertFalse(path.exists())

    def test_unowned_or_changed_claim_refused_before_registry_creation(self):
        for owner in (None, AudioGenerationOwner('f' * 32, admit)):
            with ensure_audio_generation_owner(owner):
                with self.assertRaisesRegex(workspace.SynthesisWorkspaceUnavailable, 'captured live owner'):
                    with workspace.ensure_synthesis_workspace(self.parent,2):pass
            self.assertFalse((self.root/workspace.DIRECTORY).exists())

    def test_replaced_persistent_lock_refuses_deletion(self):
        name,row=self.create();path=self.root/workspace.DIRECTORY/name
        (path/'window-0.wav').write_bytes(b'working')
        with workspace.SynthesisWorkspaceRegistry(self.root) as store:
            lock=self.root/workspace.DIRECTORY/(workspace.INDEX+'.lock')
            lock.rename(lock.with_name('old-lock'))
            lock.write_bytes(b'');lock.chmod(0o600)
            with self.assertRaisesRegex(workspace.SynthesisWorkspaceUnavailable,'lock changed'):
                store.retire(name,row)
        self.assertEqual(b'working',(path/'window-0.wav').read_bytes())

    def test_unsafe_first_row_does_not_starve_later_abandoned_workspace(self):
        first,row=self.create();second,row=self.create()
        old=self.root/workspace.DIRECTORY/first;new=self.root/workspace.DIRECTORY/second
        (old/'unknown.txt').write_bytes(b'preserved');(new/'window-0.wav').write_bytes(b'working')
        self.assertEqual(1,self.cleanup_idle());self.assertTrue(old.exists());self.assertFalse(new.exists())
        self.assertEqual([first],list(self.rows()))

    def test_actual_dispatch_known_owned_categories_and_writer_guards(self):
        import tts
        from synthesis_recovery_context import ensure_synthesis_recovery_parent
        from synthesis_settings import get_synthesis_window_settings
        engine=tts.TTSEngine.__new__(tts.TTSEngine);engine._mode='local';engine._synthesis_recovery=None
        engine._synthesis_windows=get_synthesis_window_settings({'enabled':True,'budgets':{
            'local.custom':{'max_chars':5},'local.clone':{'max_chars':5},
            'local.design':{'max_chars':5},'local.lora':{'max_chars':5},
            'local.ensemble':{'max_chars':5},'external.custom':{'max_chars':5}}})
        def render(kind='custom'):
            self.assertTrue(engine._render_synthesis_windows('One. Two.',kind,None,self.root/'parent.wav'))
        with ensure_audio_generation_owner(self.owner),ensure_synthesis_recovery_parent(self.parent):
            with patch('tts.render_synthesis_windows') as dispatched:
                for kind in ('custom','clone','design','lora'):
                    render(kind);self.assertIs(self.parent,dispatched.call_args.kwargs['workspace_parent'])
                engine._mode='external';render();engine._mode='local'
                self.assertNotIn('workspace_parent',dispatched.call_args.kwargs)
                with patch.object(engine,'_save_wav',lambda *args:None):
                    render();self.assertNotIn('workspace_parent',dispatched.call_args.kwargs)
                with patch.object(workspace,'is_synthesis_workspace_supported',return_value=False):
                    render();self.assertNotIn('workspace_parent',dispatched.call_args.kwargs)
                class UnknownEngine(tts.TTSEngine):pass
                engine.__class__=UnknownEngine
                try:
                    render();self.assertNotIn('workspace_parent',dispatched.call_args.kwargs)
                finally:engine.__class__=tts.TTSEngine
            engine._synthesis_windows=None
            for category in ('custom','clone','design','lora','ensemble'):
                self.assertIs(self.parent,workspace.get_synthesis_workspace_parent(engine,category,self.parent))
                self.assertIs(self.parent,workspace.get_synthesis_workspace_parent(engine,category,self.parent,batch=True))
            engine._mode='external'
            for category in ('custom','clone','design','lora','ensemble'):
                self.assertIsNone(workspace.get_synthesis_workspace_parent(engine,category,self.parent))
            engine._mode='local'
            for category,method in (('custom','generate_custom_voice'),('clone','generate_clone_voice'),
                                    ('design','generate_design_voice'),('lora','generate_lora_voice'),
                                    ('ensemble','generate_ensemble_voice')):
                with patch.object(engine,method,return_value=None):
                    self.assertIsNone(workspace.get_synthesis_workspace_parent(engine,category,self.parent))
            for category,method in (('custom','_local_batch_custom'),('clone','_local_batch_clone'),
                                    ('lora','_local_batch_lora'),('design','generate_design_voice'),
                                    ('ensemble','_sequential_ensemble')):
                with patch.object(engine,method,return_value=None):
                    self.assertIsNone(workspace.get_synthesis_workspace_parent(engine,category,self.parent,batch=True))
            for method in ('generate_custom_voice','generate_clone_voice','generate_voice_design',
                           'generate_lora_voice','_local_generate_clone'):
                with self.subTest(ensemble_member_writer=method),patch.object(engine,method,return_value=None):
                    for batch in (False,True):
                        self.assertIsNone(workspace.get_synthesis_workspace_parent(engine,'ensemble',self.parent,batch=batch))
            with patch.object(engine,'get_voice_backend',side_effect=RuntimeError('must not invoke override')) as backend:
                for category in ('custom','clone','design','lora','ensemble'):
                    for batch in (False,True):
                        self.assertIsNone(workspace.get_synthesis_workspace_parent(engine,category,self.parent,batch=batch))
                backend.assert_not_called()
            with patch.object(engine,'_save_wav',lambda *args:None):
                self.assertIsNone(workspace.get_synthesis_workspace_parent(engine,'clone',self.parent))
            with patch.object(workspace,'is_synthesis_workspace_supported',return_value=False):
                self.assertIsNone(workspace.get_synthesis_workspace_parent(engine,'clone',self.parent))
            engine.__class__=UnknownEngine
            try:self.assertIsNone(workspace.get_synthesis_workspace_parent(engine,'clone',self.parent))
            finally:engine.__class__=tts.TTSEngine
        import numpy as np,soundfile as sf
        from types import MethodType
        calls=[]
        def legacy_design(_engine,description,sample_text,language=None,seed=-1):
            calls.append(sample_text);preview=self.root/'legacy-design.wav'
            sf.write(preview,np.arange(2400,dtype=np.int16),24000,subtype='PCM_16')
            return str(preview),24000
        engine.generate_voice_design=MethodType(legacy_design,engine)
        output=self.root/'legacy-design-published.wav'
        self.assertTrue(engine._generate_design_voice_prepared('Hello.','',{'description':'Clear voice'},str(output)))
        self.assertEqual(['Hello.'],calls);self.assertTrue(output.is_file())
        del engine.generate_voice_design
        # The fallback removes normal scratch and attempts best-effort cleanup
        # on IO failure without replacing success, provider errors or cancellation.
        import errno,shutil
        from contextlib import ExitStack
        from audio_generation_owner import AudioGenerationCancelled
        member_source=output
        scratch=[];cleanup_attempts=[];failure=[None]
        def cleanup_io_failure(path,ignore_errors=False,onerror=None,**kwargs):
            cleanup_attempts.append(Path(path))
            try:raise OSError(errno.EIO,'synthetic scratch cleanup failure')
            except OSError:
                if ignore_errors:return
                if onerror is not None:onerror(os.rmdir,path,sys.exc_info());return
                raise
        def member(text,instruct,speaker,voices,path):
            scratch.append(Path(path).parent)
            if failure[0] is not None:raise failure[0]
            shutil.copyfile(member_source,path);return True
        def mixed(paths,path):
            shutil.copyfile(paths[0],path);return True
        voices={'A':{'type':'custom','voice':'Ryan'},'B':{'type':'custom','voice':'Ryan'}}
        output=self.root/'ensemble-fallback.wav'
        with patch.object(tempfile,'tempdir',str(self.root)), \
                patch.object(engine,'_dispatch_prepared_voice',side_effect=member), \
                patch('tts.mix_to_unison',side_effect=mixed):
            for cleanup_failed in (False,True):
                with ExitStack() as faults:
                    if cleanup_failed:
                        faults.enter_context(patch.object(shutil,'rmtree',side_effect=cleanup_io_failure))
                    for error in (None,ValueError('synthetic provider failure'),AudioGenerationCancelled('synthetic cancellation')):
                        with self.subTest(cleanup_failed=cleanup_failed,fallback_error=type(error).__name__):
                            failure[0]=error;before=len(scratch);attempts=len(cleanup_attempts)
                            if error is None:
                                self.assertTrue(engine._generate_ensemble_voice_prepared(
                                    'Synthetic line.','',{'members':['A','B']},voices,str(output)))
                                prior=output.read_bytes()
                                self.assertEqual(member_source.read_bytes(),prior)
                            else:
                                with self.assertRaisesRegex(type(error),str(error)):
                                    engine._generate_ensemble_voice_prepared(
                                        'Synthetic line.','',{'members':['A','B']},voices,str(output))
                                self.assertEqual(prior,output.read_bytes())
                            directories=set(scratch[before:]);self.assertEqual(1,len(directories))
                            directory=directories.pop()
                            if cleanup_failed:
                                self.assertEqual([directory],cleanup_attempts[attempts:])
                            else:self.assertFalse(directory.exists())
        # Restore the window policy for the pre-existing unowned dispatch check.
        engine._synthesis_windows=get_synthesis_window_settings({'enabled':True,'budgets':{'local.custom':{'max_chars':5}}})
        with patch('tts.render_synthesis_windows') as dispatched:
            render();self.assertNotIn('workspace_parent',dispatched.call_args.kwargs)

    def test_project_capture_when_recovery_disabled_still_renews_book_and_inputs(self):
        from types import SimpleNamespace
        from tests.test_audio_generation_cancellation import AudioGenerationCancellationTests
        for engine in (SimpleNamespace(_synthesis_windows={'enabled':True}),object()):
            with self.subTest(engine=type(engine).__name__),AudioGenerationCancellationTests().fixture(1) as (root,manager,before,state):
                with ensure_audio_generation_owner(self.owner):
                    parent=manager._capture_synthesis_recovery_parent(engine,0,before[0])
                    self.assertIsNotNone(parent)
                    with parent.admit():pass
                    manager._update_chunk_fields_by_uid(before[0]['uid'],text='Changed source.')
                    with self.assertRaisesRegex(ValueError,'changed'):
                        with parent.admit():pass

    def test_core_preclaim_busy_book_defers_and_live_foreign_lease_preserved(self):
        import core
        from book_state_transaction import JOURNAL
        from task_ownership import acquire_task_lease
        from tests.test_startup_task_ownership import StartupTaskOwnershipTests
        from utils import file_lock
        fixture=StartupTaskOwnershipTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
        owner,parent=get_parent(fixture.root)
        with workspace.SynthesisWorkspaceRegistry(fixture.root,create=True) as store:name,row=store.reserve(parent,2)
        path=fixture.root/workspace.DIRECTORY/name;(path/'window-0.wav').write_bytes(b'abandoned')
        def claim():
            token=core.claim_gpu_task('audio',cpu_only=True)
            self.assertTrue(core.release_gpu_task_claim('audio',token))
        with file_lock(fixture.root/JOURNAL,timeout=0):
            claim();self.assertTrue(path.exists())
        lease=acquire_task_lease(fixture.root,'review',set())
        try:
            claim();self.assertTrue(path.exists())
        finally:lease.close()
        claim();self.assertFalse(path.exists())

    def test_active_same_claim_parallel_parents_preserved_by_live_kernel_lease(self):
        import threading
        from concurrent.futures import ThreadPoolExecutor
        from audio_generation_owner import run_with_audio_generation_owner
        from task_ownership import acquire_task_lease
        mutex=threading.RLock();barrier=threading.Barrier(3);release=threading.Event();paths=[]
        @contextmanager
        def serial_admit(**kwargs):
            with mutex:yield
        owner=AudioGenerationOwner(self.owner.claim_id,serial_admit)
        parent=RecoveryParent(str(self.root),'b'*64,'c'*64,'d'*64,self.parent.claim_sha256,serial_admit)
        lease=acquire_task_lease(self.root,'audio',set())
        def worker():
            with workspace.ensure_synthesis_workspace(parent,2) as path:
                paths.append(path);(path/'window-0.wav').write_bytes(b'active parent')
                barrier.wait(timeout=5)
                self.assertTrue(release.wait(5))
        try:
            with ThreadPoolExecutor(max_workers=2) as pool:
                futures=[pool.submit(run_with_audio_generation_owner,owner,worker) for _ in range(2)]
                try:
                    barrier.wait(timeout=5)
                    self.assertEqual(2,len(self.rows()));self.assertIsNone(self.cleanup_idle())
                    self.assertTrue(all(path.exists() for path in paths))
                finally:release.set()
                for future in futures:future.result(timeout=5)
            self.assertEqual({},self.rows());self.assertTrue(all(not path.exists() for path in paths))
        finally:lease.close()

    def test_real_process_death_registration_creation_and_retirement_boundaries(self):
        child=r'''
import os,sys
from pathlib import Path
from unittest.mock import patch
from tests.test_synthesis_workspace import get_parent
from synthesis_workspace import SynthesisWorkspaceRegistry
from task_ownership import acquire_task_lease
root=Path(sys.argv[1]);step=sys.argv[2]
lease=acquire_task_lease(root,'audio',set());owner,parent=get_parent(root)
with SynthesisWorkspaceRegistry(root,create=True) as store:
 if step.startswith('retire_'):
  name,row=store.reserve(parent,2)
  (root/'.synthesis-workspaces-v1'/name/'window-0.wav').write_bytes(b'working')
  key={'retire_unlink':'unlink','retire_rmdir':'rmdir','retire_save':'save'}[step]
  original=getattr(store,'save') if key=='save' else getattr(os,key)
  def interrupted(*args,**kwargs):
   result=original(*args,**kwargs)
   if key!='rmdir' or args[0]==name:os._exit(77)
   return result
  with patch.object(store if key=='save' else os,key,side_effect=interrupted):store.retire(name,row)
 else:
  original_save=store.save;original_mkdir=os.mkdir;calls=[]
  def save():
   result=original_save();calls.append(1)
   if (step=='reserved' and len(calls)==1) or (step=='ready' and len(calls)==2):os._exit(77)
   return result
  def mkdir(name,*args,**kwargs):
   result=original_mkdir(name,*args,**kwargs)
   if (step=='created' and name!='join') or (step=='join_created' and name=='join'):os._exit(77)
   return result
  with patch.object(store,'save',side_effect=save),patch.object(os,'mkdir',side_effect=mkdir):store.reserve(parent,2)
'''
        for step in ('reserved','created','join_created','ready','retire_unlink','retire_rmdir','retire_save'):
            with self.subTest(step=step),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp)
                result=subprocess.run([sys.executable,'-c',child,str(root),step],
                    cwd=Path(__file__).resolve().parents[1],capture_output=True,text=True,timeout=20)
                self.assertEqual(77,result.returncode,result.stderr)
                with ensure_startup_recovery(root,timeout=0) as idle:
                    self.assertTrue(idle)
                    with ensure_book_state(root):workspace.apply_abandoned_synthesis_workspace_cleanup(root)
                with workspace.SynthesisWorkspaceRegistry(root) as store:rows=copy.deepcopy(store.body['rows'])
                if step in ('created','join_created'):
                    self.assertEqual(1,len(rows));self.assertIsNone(next(iter(rows.values()))['identity'])
                    path=root/workspace.DIRECTORY/next(iter(rows));self.assertTrue(path.is_dir())
                    self.assertFalse(list(path.glob('*.wav')))
                else:self.assertEqual({},rows)

    def test_exact_owner_cancel_cleanup_succeeds_but_replaced_owner_defers(self):
        from audio_generation_owner import AudioGenerationCancelled,ensure_audio_generation_active
        for replacement in (False,True):
            with self.subTest(replacement=replacement):
                state={'cancelled':False,'current':True}
                @contextmanager
                def checked_admit(*,allow_cancelled=False):
                    if not state['current'] or state['cancelled'] and not allow_cancelled:
                        raise AudioGenerationCancelled()
                    yield
                owner=AudioGenerationOwner(self.owner.claim_id,checked_admit)
                parent=RecoveryParent(str(self.root),'b'*64,'c'*64,'d'*64,self.parent.claim_sha256,checked_admit)
                with ensure_audio_generation_owner(owner):
                    with self.assertRaises(AudioGenerationCancelled):
                        with workspace.ensure_synthesis_workspace(parent,2) as path:
                            (path/'window-0.wav').write_bytes(b'working')
                            state['cancelled']=True;state['current']=not replacement
                            ensure_audio_generation_active()
                self.assertEqual(replacement,path.exists())
                self.assertEqual(int(replacement),len(self.rows()))
                if replacement:self.assertEqual(1,self.cleanup_idle())
