"""Reviewed discard permits further reversible scopes without rewriting origins."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from literal_policy import bind_literal_policy
from merge_integrity import get_source_integrity
from project import group_into_chunks, get_chunk_generation_inputs, ProjectManager
from project_scope_admission import ensure_scope_admission
from project_scope_apply import (apply_source_scope, apply_source_scope_undo, discard_source_scope_undo,
                                 get_source_scope_undo_status, FILES, UNDO)
from project_scope_plan import get_scope_preview


class ScopeLifecycleTests(unittest.TestCase):
    def test_legacy_undo_without_binding_still_restores(self):
        from script_selection_binding import BINDING_NAME
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            script = [{'speaker':'A','text':'First line.','instruct':''},
                      {'speaker':'B','text':'Second line.','instruct':''}]
            source = root / 'source.txt'; source.write_text('First line.\nSecond line.')
            rows = [dict(row,id=i,uid=f'uid-{i}',status='pending',audio_path=None)
                    for i,row in enumerate(group_into_chunks(script))]
            for name,value in [('state.json',{'active_book_id':'fixture','book_generation':'one',
                                                'input_file_path':str(source)}),
                               ('annotated_script.json',script),('voice_config.json',{}),('chunks.json',rows)]:
                (root/name).write_text(json.dumps(value,indent=2))
            original = (root/'annotated_script.json').read_bytes()
            with patch('core.DATA_DIR',str(root)), patch('core.process_state',{}), ensure_scope_admission(root):
                apply_source_scope(root,1,get_scope_preview(root,1)['revision'])
                undo = json.loads((root/UNDO).read_text()); undo['version'] = 1
                for key in ('before','before_sha256','after'):
                    undo[key].pop(BINDING_NAME)
                (root/UNDO).write_text(json.dumps(undo))
                (root/BINDING_NAME).write_text('newer binding')
                refused = get_source_scope_undo_status(root)
                self.assertFalse(refused['can_undo'])
                self.assertTrue((root/UNDO).exists())
                (root/BINDING_NAME).unlink()
                status = get_source_scope_undo_status(root)
                self.assertTrue(status['can_undo'])
                apply_source_scope_undo(root, status['revision'])
            self.assertEqual(original, (root/'annotated_script.json').read_bytes())

    def test_bound_scope_and_undo_keep_script_identity_consistent(self):
        import hashlib
        from book_state_transaction import get_book_operation_token
        from script_selection_binding import BINDING_NAME, make_script_selection_binding
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            script = [{'speaker':'NARRATOR','text':'First line.','instruct':''},
                      {'speaker':'B','text':'Second line.','instruct':''}]
            source = root / 'source.txt'; source.write_text('First line.\nSecond line.')
            rows = [dict(row,id=i,uid=f'uid-{i}',status='pending',audio_path=None)
                    for i,row in enumerate(group_into_chunks(script))]
            state = {'active_book_id':'fixture','book_generation':'one','input_file_path':str(source),
                     'script_binding_required':True}
            for name,value in [('state.json',state),('annotated_script.json',script),
                               ('voice_config.json',{}),('chunks.json',rows)]:
                (root/name).write_text(json.dumps(value,indent=2))
            script_before = (root/'annotated_script.json').read_bytes()
            binding = make_script_selection_binding(get_book_operation_token(str(root)),
                hashlib.sha256(source.read_bytes()).hexdigest(), script_before, source_path=str(source))
            (root/BINDING_NAME).write_text(json.dumps(binding))
            binding_before = (root/BINDING_NAME).read_bytes()
            with patch('core.DATA_DIR',str(root)), patch('core.process_state',{}), ensure_scope_admission(root):
                apply_source_scope(root,1,get_scope_preview(root,1)['revision'])
                current = (root/'annotated_script.json').read_bytes()
                self.assertEqual(hashlib.sha256(current).hexdigest(),
                                 json.loads((root/BINDING_NAME).read_text())['published_script_sha256'])
                revision = get_source_scope_undo_status(root)['revision']
                apply_source_scope_undo(root, revision)
            self.assertEqual(script_before,(root/'annotated_script.json').read_bytes())
            self.assertEqual(binding_before,(root/BINDING_NAME).read_bytes())

    def test_repeated_plain_and_literal_scopes_keep_absolute_ranges_and_exact_undo(self):
        for literal in (False,True):
            with self.subTest(literal=literal),tempfile.TemporaryDirectory() as tmp:
                root=Path(tmp).resolve()
                script=[{'speaker':speaker,'text':text,'instruct':''} for speaker,text in
                        [('A','First exact line.'),('B','Second exact line.'),
                         ('A','Third exact line.'),('B','Fourth exact line.')]]
                source='\n'.join(row['text'] for row in script);path=root/'source.txt';path.write_text(source)
                if literal:script=bind_literal_policy(source,script)
                rows=[dict(row,id=i,uid=f'uid-{i}',status='pending',audio_path=None)
                      for i,row in enumerate(group_into_chunks(script))]
                original_inputs={row['uid']:get_chunk_generation_inputs(row) for row in rows}
                state={'active_book_id':'fixture','book_generation':'unchanged','input_file_path':str(path)}
                for name,value in [('state.json',state),('annotated_script.json',script),
                                   ('voice_config.json',{}),('chunks.json',rows)]:
                    (root/name).write_text(json.dumps(value,indent=2))
                with patch('core.DATA_DIR',str(root)),patch('core.process_state',{}),ensure_scope_admission(root):
                    self.assertFalse(get_source_scope_undo_status(root)['available'])
                    apply_source_scope(root,1,get_scope_preview(root,1)['revision'])
                    status=get_source_scope_undo_status(root);self.assertTrue(status['can_undo'])
                    with self.assertRaisesRegex(ValueError,'undo changed'):
                        discard_source_scope_undo(root,'0'*64)
                    discard_source_scope_undo(root,status['revision'])
                    before={name:(root/name).read_bytes() if (root/name).exists() else None for name in FILES}
                    apply_source_scope(root,1,get_scope_preview(root,1)['revision'])
                    current=json.loads((root/'state.json').read_bytes())
                    self.assertEqual((6,12,2),(current['_source_scope']['retained_start_word'],
                        current['_source_scope']['retained_end_word'],current['_source_scope']['script_start_entry']))
                    retained=json.loads((root/'chunks.json').read_bytes())
                    self.assertEqual(['uid-2','uid-3'],[row['uid'] for row in retained])
                    for row in retained:self.assertEqual(original_inputs[row['uid']],get_chunk_generation_inputs(row))
                    saved=(root/'annotated_script.json').read_bytes()
                    self.assertEqual('verified',get_source_integrity(current,saved,retained)['status'])
                    if literal:ProjectManager(str(root)).ensure_literal_render_inputs(retained,[0,1])
                    apply_source_scope_undo(root,get_source_scope_undo_status(root)['revision'])
                    self.assertEqual(before,{name:(root/name).read_bytes() if (root/name).exists() else None for name in FILES})
                    self.assertFalse((root/UNDO).exists())
