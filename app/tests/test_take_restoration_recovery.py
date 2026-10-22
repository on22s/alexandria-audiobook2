"""Hard exits across real restore persistence, with declared CPU identity only."""
import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from audio_take_history import AudioTakeHistory
from book_state_transaction import ensure_book_state
from project import ProjectManager
from synthesis_recovery_serving import NativeRecoveryServingIdentity
from synthesis_workspace import (SynthesisWorkspaceRegistry, is_synthesis_workspace_supported,
                                 apply_abandoned_synthesis_workspace_cleanup)
from task_ownership import ensure_startup_recovery
from take_restoration import capture_restore_target, restore_editor_take
from tests import test_take_restoration as restore_cases


_CHILD = r'''
import json,os,sys
from pathlib import Path
from unittest.mock import patch
from task_ownership import acquire_task_lease
from tests.test_take_restoration import TakeRestorationTests
from take_restoration import restore_editor_take
import take_restoration,take_publication
phase=sys.argv[1];journal=sys.argv[2]=='journal'
with TakeRestorationTests().prepared(journal) as args:
 root,manager,engine,request,origin,original,current,displaced,saved=args
 lease=acquire_task_lease(root,'audio',set())
 (root/'restore-test-before.json').write_text(json.dumps(dict(request=vars(request),current=current,origin=origin,
     original_sha256=__import__('hashlib').sha256(original).hexdigest(),
     displaced_sha256=__import__('hashlib').sha256(displaced).hexdigest(),saved=saved)))
 def exit_now(*args,**kwargs):os._exit(76)
 if phase=='held':manager.get_engine=exit_now
 elif phase=='staged':take_restoration._read_audio=exit_now
 elif phase=='installed':
  validate=take_publication.validate_published_take_audio
  def installed(*args,**kwargs):
   validate(*args,**kwargs);os._exit(76)
  take_publication.validate_published_take_audio=installed
 elif phase=='selected':
  update=manager._update_chunk_fields_by_uid
  def selected(*args,**kwargs):
   result=update(*args,**kwargs)
   if kwargs.get('publication_audio') is not None:os._exit(76)
   return result
  manager._update_chunk_fields_by_uid=selected
 restore_editor_take(manager,0,request,current)
 raise AssertionError('restore did not reach expected hard-exit cutpoint')
'''


@unittest.skipUnless(is_synthesis_workspace_supported(), 'Registered restore staging currently qualifies Linux')
class TakeRestorationRecoveryTests(unittest.TestCase):
    def test_hard_exits_preserve_selected_and_archived_bytes_then_allow_idle_cleanup_and_retry(self):
        from types import SimpleNamespace
        for journal in (False,True):
            for phase in ('held','staged','installed','selected'):
                with self.subTest(journal=journal,phase=phase),tempfile.TemporaryDirectory() as tmp:
                    environment={**os.environ,'TMPDIR':tmp,'TEMP':tmp,'TMP':tmp}
                    child=subprocess.run([sys.executable,'-c',_CHILD,phase,'journal' if journal else 'json'],
                        cwd=Path(__file__).resolve().parents[1],env=environment,capture_output=True,text=True,timeout=45)
                    self.assertEqual(child.returncode,76,child.stderr)
                    controls=list(Path(tmp).glob('*/restore-test-before.json'));self.assertEqual(len(controls),1)
                    root=controls[0].parent;before=json.loads(controls[0].read_text());manager=ProjectManager(str(root))
                    row=manager.get_saved_chunks()[0]
                    selected=before['origin'] if phase=='selected' else before['current']
                    selected_sha=before['original_sha256'] if phase=='selected' else before['displaced_sha256']
                    for key in ('take','audio_revision'):self.assertEqual(row[key],selected[key])
                    if phase=='selected':self.assertNotEqual(row['audio_path'],before['current']['audio_path'])
                    else:self.assertEqual(row['audio_path'],before['current']['audio_path'])
                    self.assertEqual(hashlib.sha256((root/row['audio_path']).read_bytes()).hexdigest(),selected_sha)
                    with AudioTakeHistory(root)._locked() as document:
                        records=copy.deepcopy(document['records'])
                    saved=next(record for record in records if record['version']==before['saved']['version'])
                    self.assertEqual(saved,before['saved'])
                    displaced=next(record for record in records if record['audio_revision']==before['current']['audio_revision'])
                    for record,expected in ((saved,before['original_sha256']),(displaced,before['displaced_sha256'])):
                        self.assertEqual(hashlib.sha256((root/'take_history'/f"{record['version']}{record['suffix']}").read_bytes()).hexdigest(),expected)
                    with ensure_startup_recovery(root,timeout=0) as idle:
                        self.assertTrue(idle,'dead child retained task ownership')
                        with ensure_book_state(root):
                            self.assertEqual(apply_abandoned_synthesis_workspace_cleanup(root),0 if phase=='held' else 1)
                    if phase!='held':
                        with SynthesisWorkspaceRegistry(root) as registry:self.assertEqual(registry.body['rows'],{})
                    request=SimpleNamespace(**before['request'])
                    if phase=='selected':
                        with self.assertRaises(ValueError):capture_restore_target(manager,0,request)
                    else:
                        fixture=restore_cases.TakeRestorationTests();engine=fixture.engine()
                        with fixture.owner(),patch.object(manager,'get_engine',return_value=engine), \
                                patch('synthesis_recovery_serving.capture_native_custom_serving_identity',
                                    return_value=NativeRecoveryServingIdentity('b'*64,'c'*64,'d'*64)):
                            expected=capture_restore_target(manager,0,request)
                            restored=restore_editor_take(manager,0,request,expected)
                        self.assertEqual(hashlib.sha256((root/restored['audio_path']).read_bytes()).hexdigest(),before['original_sha256'])
                    with AudioTakeHistory(root)._locked() as document:self.assertEqual(document['records'],records)


if __name__=='__main__':unittest.main()
