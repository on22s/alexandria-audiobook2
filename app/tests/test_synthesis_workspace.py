"""Real process death, filesystem faults and live leases for disposable spill."""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
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
note=Path(sys.argv[1]);phase=sys.argv[2]
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
 manager.engine=engine
 Path(manager.voice_config_path).write_text(json.dumps({'NARRATOR':{'type':'custom','voice':'Ryan','seed':42}}))
 original=manager._publish_generated_chunk_audio
 def publish(*args,**kwargs):
  if phase=='parent':pause()
  result=original(*args,**kwargs)
  if phase=='published':
   proof=json.loads(note.read_text());selected=root/result
   proof.update(published_path=str(selected),published_sha256=hashlib.sha256(selected.read_bytes()).hexdigest())
   note.write_text(json.dumps(proof));pause()
  return result
 manager._publish_generated_chunk_audio=publish
 success,payload=manager.generate_chunk_audio(0)
 if not success:raise RuntimeError(payload)
 raise RuntimeError('Expected parent barrier was not reached')
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
        for phase in ('zero','window','parent','published'):
            with self.subTest(phase=phase),tempfile.TemporaryDirectory() as tmp:
                bound=Path(tmp);note=bound/'note.json'
                env={**os.environ,'TMPDIR':tmp,'CUDA_VISIBLE_DEVICES':'',
                     'HIP_VISIBLE_DEVICES':'','ROCR_VISIBLE_DEVICES':''}
                child=subprocess.Popen([sys.executable,'-c',_PROJECT_CHILD,str(note),phase],
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
                    self.assertEqual(2 if phase in ('zero','window') else 1,len(rows))
                    outer=[root/workspace.DIRECTORY/name for name,row in rows.items() if row['windows']==0]
                    self.assertEqual(1,len(outer));parent=outer[0]/'assembled-parent.wav'
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
                    self.assertEqual(proof['previous_sha256'],hashlib.sha256(Path(proof['previous']).read_bytes()).hexdigest())
                    if phase=='published':
                        self.assertEqual(proof['published_sha256'],hashlib.sha256(Path(proof['published_path']).read_bytes()).hexdigest())
                finally:
                    if child.poll() is None:child.kill();child.wait(timeout=10)
                    child.stdin.close();child.stderr.close()

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

    def test_project_parent_publication_cancel_replacement_and_capacity(self):
        import numpy as np
        from contextlib import ExitStack
        from audio_generation_owner import AudioGenerationCancelled
        from tests.test_take_publication import TakePublicationTests
        class Model:
            def generate_custom_voice(self,**kwargs):return [np.sin(np.arange(2400)*.1)*.2],24000
        case=TakePublicationTests()
        for outcome in ('success','cancel','replace','edit','capacity','defer'):
            with self.subTest(outcome=outcome),case.fixture(False,enabled=False) as (root,manager,previous,_),ExitStack() as stack:
                state={'cancelled':False,'replaced':False};observed=[]
                @contextmanager
                def current_owner(allow_cancelled=False):
                    if state['replaced'] or (state['cancelled'] and not allow_cancelled):
                        raise AudioGenerationCancelled()
                    yield
                stack.enter_context(ensure_audio_generation_owner(AudioGenerationOwner('e'*32,current_owner)))
                engine=case.engine();engine._synthesis_recovery=None;engine._local_custom_model=Model();manager.engine=engine
                Path(manager.voice_config_path).write_text(json.dumps({'NARRATOR':{'type':'custom','voice':'Ryan','seed':42}}))
                original=manager._publish_generated_chunk_audio
                def publish(chunk,path,*args,**kwargs):
                    observed.append(Path(path));self.assertTrue(Path(path).is_file())
                    with workspace.SynthesisWorkspaceRegistry(root) as store:
                        self.assertEqual(2 if outcome=='defer' else 1,len(store.body['rows']))
                    if outcome in ('cancel','replace'):
                        state['cancelled']=True;state['replaced']=outcome=='replace'
                        raise AudioGenerationCancelled()
                    if outcome=='edit':
                        manager._update_chunk_fields_by_uid(chunk['uid'],text='Changed synthetic input.')
                    result=original(chunk,path,*args,**kwargs)
                    self.assertTrue(Path(path).is_file());return result
                stack.enter_context(patch.object(manager,'_publish_generated_chunk_audio',side_effect=publish))
                removed=stack.enter_context(patch.object(manager,'_remove_temp_file',wraps=manager._remove_temp_file))
                if outcome=='capacity':stack.enter_context(patch.object(workspace,'MAX_ROWS',1))
                if outcome=='defer':stack.enter_context(patch.object(workspace.SynthesisWorkspaceRegistry,'retire',side_effect=OSError('defer owned cleanup')))
                success,payload=manager.generate_chunk_audio(0)
                self.assertEqual(outcome in ('success','defer'),success,payload)
                self.assertFalse(list(root.glob('chunk_*.wav')))
                for path in observed:self.assertNotIn(str(path),[call.args[0] for call in removed.call_args_list])
                with workspace.SynthesisWorkspaceRegistry(root) as store:
                    self.assertEqual(2 if outcome=='defer' else 1 if outcome=='replace' else 0,len(store.body['rows']))
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

    def test_actual_dispatch_only_known_owned_supported_custom_with_recovery_disabled(self):
        import tts
        from synthesis_recovery_context import ensure_synthesis_recovery_parent
        from synthesis_settings import get_synthesis_window_settings
        engine=tts.TTSEngine.__new__(tts.TTSEngine);engine._mode='local';engine._synthesis_recovery=None
        engine._synthesis_windows=get_synthesis_window_settings({'enabled':True,'budgets':{
            'local.custom':{'max_chars':5},'local.clone':{'max_chars':5},'external.custom':{'max_chars':5}}})
        def render(kind='custom'):
            self.assertTrue(engine._render_synthesis_windows('One. Two.',kind,None,self.root/'parent.wav'))
        with ensure_audio_generation_owner(self.owner),ensure_synthesis_recovery_parent(self.parent):
            with patch('tts.render_synthesis_windows') as dispatched:
                render();self.assertIs(self.parent,dispatched.call_args.kwargs['workspace_parent'])
                render('clone');self.assertNotIn('workspace_parent',dispatched.call_args.kwargs)
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
        with patch('tts.render_synthesis_windows') as dispatched:
            render();self.assertNotIn('workspace_parent',dispatched.call_args.kwargs)

    def test_project_capture_when_recovery_disabled_still_renews_book_and_inputs(self):
        from types import SimpleNamespace
        from tests.test_audio_generation_cancellation import AudioGenerationCancellationTests
        with AudioGenerationCancellationTests().fixture(1) as (root,manager,before,state):
            with ensure_audio_generation_owner(self.owner):
                engine=SimpleNamespace(_synthesis_windows={'enabled':True})
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
