"""Real saved-byte restoration after scope edits with declared CPU serving fixtures.

No native model, GPU, ASR, listening or other-platform qualification is claimed.
"""
import copy
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from audio_take_history import AudioTakeHistory
from book_state_transaction import get_book_operation_token
from literal_policy import bind_literal_policy
from project import group_into_chunks
from project_scope_admission import ensure_scope_admission
from project_scope_apply import apply_source_scope, apply_source_scope_undo, get_source_scope_undo_preview
from project_scope_plan import get_scope_preview
from synthesis_recovery_serving import NativeRecoveryServingIdentity
from take_publication import KEY
from take_restoration import capture_restore_target, restore_editor_take
from tests import test_take_restoration as fixtures


class ScopeRestorationTests(unittest.TestCase):
    fixture=fixtures.TakeRestorationTests.fixture
    owner=fixtures.TakeRestorationTests.owner
    engine=fixtures.TakeRestorationTests.engine
    render=fixtures.TakeRestorationTests.render

    @unittest.skipUnless(fixtures.is_synthesis_workspace_supported(), 'Registered restoration staging currently qualifies Linux')
    def test_saved_take_remains_restorable_after_plain_literal_scope_and_exact_undo(self):
        for journal in (False,True):
            for literal in (False,True):
                for undo_first in (False,True):
                    with self.subTest(journal=journal,literal=literal,undo_first=undo_first),self.fixture(journal) as args:
                        root,manager,_prior,fresh=args;root=Path(root).resolve()
                        script=[{'speaker':'PREFIX','text':'Removed synthetic prefix.','instruct':''},
                                {'speaker':'NARRATOR','text':'Original synthetic words.','instruct':''}]
                        source='\n'.join(row['text'] for row in script)
                        path=root/'source.txt';path.write_text(source)
                        if literal:script=bind_literal_policy(source,script)
                        Path(manager.script_path).write_text(json.dumps(script))
                        state=json.loads((root/'state.json').read_bytes());state['input_file_path']=str(path)
                        (root/'state.json').write_text(json.dumps(state))
                        grouped=group_into_chunks(script);target=manager.load_chunks()[0]
                        rows=[dict(grouped[0],id=0,uid='prefix',take=0,status='pending',audio_path=None),
                              {**target,**grouped[1],'id':1}]
                        manager.save_chunks(rows)
                        voices={'NARRATOR':{'type':'custom','voice':'Ryan','seed':42}}
                        Path(manager.voice_config_path).write_text(json.dumps(voices))
                        engine=self.engine()
                        Path(manager.config_path).write_text(json.dumps({'tts':{
                            **engine.get_model_settings(),'synthesis_recovery':engine._synthesis_recovery}}))
                        manager.invalidate_config_cache()
                        with self.owner():
                            chunk=manager.load_chunks()[1]
                            parent=manager._capture_synthesis_recovery_parent(engine,1,chunk)
                            admitted=manager._capture_take_publication_admission(engine,parent,1,chunk,voices,'NARRATOR')
                            self.assertIsNotNone(admitted)
                            pcm,_preparation,synthesis,observed=self.render(root,engine,parent,chunk)
                            manager._publish_generated_chunk_audio({**chunk,'_synthesis_receipt':synthesis,
                                '_speech_input':observed},str(pcm),'NARRATOR',
                                recovery_parent=parent,publication_admission=admitted)
                            origin=copy.deepcopy(manager.load_chunks()[1])
                            original=(root/origin['audio_path']).read_bytes()
                            manager.update_chunk(1,{'take':3})
                            manager._publish_generated_chunk_audio(manager.load_chunks()[1],str(fresh),'NARRATOR')
                        current=copy.deepcopy(manager.load_chunks()[1]);token=get_book_operation_token(root)
                        with AudioTakeHistory(root)._locked() as document:
                            saved=copy.deepcopy(next(row for row in document['records']
                                if row['audio_revision']==origin['audio_revision']))
                        with patch('core.DATA_DIR',str(root)),patch('core.process_state',{}),ensure_scope_admission(root):
                            apply_source_scope(root,1,get_scope_preview(root,1)['revision'])
                            if undo_first:apply_source_scope_undo(root,get_source_scope_undo_preview(root)['revision'])
                        index=1 if undo_first else 0
                        self.assertEqual(token,get_book_operation_token(root))
                        request=SimpleNamespace(expected_book_token=token,source_book_token=token,
                            expected_uid=current['uid'],expected_audio_revision=current['audio_revision'],
                            expected_audio_path=current['audio_path'],expected_take=current['take'],
                            version=saved['version'],sha256=saved['sha256'])
                        with self.owner(),patch.object(manager,'get_engine',return_value=engine),patch(
                                'synthesis_recovery_serving.capture_native_custom_serving_identity',
                                return_value=NativeRecoveryServingIdentity('b'*64,'c'*64,'d'*64)):
                            expected=capture_restore_target(manager,index,request)
                            restored=restore_editor_take(manager,index,request,expected)
                        self.assertEqual(original,(root/restored['audio_path']).read_bytes())
                        self.assertEqual(origin[KEY],restored[KEY])
                        self.assertEqual(origin['synthesis_receipt'],restored['synthesis_receipt'])
                        self.assertEqual(origin['take'],restored['take'])
                        self.assertEqual('line',restored['uid'])
                        self.assertEqual(1,restored[KEY]['index'])
