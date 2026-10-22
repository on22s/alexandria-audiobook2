"""Compound scope apply/undo retains exact saved metadata and refuses stale plans."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from contextlib import contextmanager
from book_state_transaction import ensure_book_state
from project import group_into_chunks
from project_scope_apply import (apply_source_scope, apply_source_scope_undo,
                                 get_source_scope_undo_preview, UNDO)
from project_scope_plan import get_scope_preview
from project_scope_admission import ensure_scope_admission


class ScopeApplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        script = [{'speaker':speaker, 'text':text, 'instruct':''} for speaker,text in
                  [('ALICE','First synthetic line.'), ('BOB','Retained exact line.')]]
        source = self.root/'source.txt'; source.write_text('\n'.join(r['text'] for r in script))
        rows = [dict(row, id=i, uid=f'uid-{i}', status='pending', audio_path=None)
                for i,row in enumerate(group_into_chunks(script))]
        state = {'active_book_id':'fixture', 'book_generation':'unchanged', 'input_file_path':str(source)}
        for name,value in [('state.json',state), ('annotated_script.json',script),
                           ('voice_config.json',{}), ('chunks.json',rows)]:
            (self.root/name).write_text(json.dumps(value, indent=3))

    def files(self):
        return {p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file() and not p.name.endswith('.lock')}

    @contextmanager
    def admission(self):
        with patch('core.DATA_DIR', str(self.root)), patch('core.process_state', {}), ensure_scope_admission(self.root):
            yield

    def test_compound_apply_and_exact_undo_preserve_original_bytes_and_stable_uids(self):
        before = self.files()
        with self.admission():
            plan = get_scope_preview(self.root, 1)
            result = apply_source_scope(self.root, 1, plan['revision'])
            self.assertEqual(['uid-1'], result['retained_uids'])
            rows = json.loads((self.root/'chunks.json').read_bytes())
            self.assertEqual((0,'uid-1'), (rows[0]['id'],rows[0]['uid']))
            state = json.loads((self.root/'state.json').read_bytes())
            self.assertEqual('unchanged', state['book_generation'])
            self.assertTrue((self.root/UNDO).exists())
            undo = get_source_scope_undo_preview(self.root)
            apply_source_scope_undo(self.root, undo['revision'])
        self.assertEqual(before, self.files())

    def test_stale_apply_and_changed_project_undo_are_refused_without_clobbering(self):
        with self.admission():
            before = self.files(); plan = get_scope_preview(self.root, 1)
            with self.assertRaisesRegex(ValueError, 'preview changed'):
                apply_source_scope(self.root, 1, '0'*64)
            self.assertEqual(before, self.files())
            apply_source_scope(self.root, 1, plan['revision'])
            undo = get_source_scope_undo_preview(self.root)
            path = self.root/'chunks.json'; path.write_bytes(path.read_bytes()+b' ')
            changed = self.files()
            with self.assertRaisesRegex(ValueError, 'Project changed'):
                apply_source_scope_undo(self.root, undo['revision'])
            self.assertEqual(changed, self.files())

    def test_corrupt_saved_bytes_stale_undo_and_changed_source_are_refused(self):
        import base64
        with self.admission():
            plan = get_scope_preview(self.root, 1)
            apply_source_scope(self.root, 1, plan['revision'])
            undo = get_source_scope_undo_preview(self.root)
            with self.assertRaisesRegex(ValueError, 'undo changed'):
                apply_source_scope_undo(self.root, '0'*64)
            path = self.root/UNDO; raw = path.read_bytes(); value = json.loads(raw)
            value['before']['state.json'] = base64.b64encode(b'{}').decode()
            path.write_text(json.dumps(value)); changed = self.files()
            with self.assertRaisesRegex(ValueError, 'undo bytes changed'):
                apply_source_scope_undo(self.root, undo['revision'])
            self.assertEqual(changed, self.files()); path.write_bytes(raw)
            source = self.root/'source.txt'; source.write_text(source.read_text()+' Extra.')
            changed = self.files()
            with self.assertRaisesRegex(ValueError, 'artifact changed'):
                apply_source_scope_undo(self.root, undo['revision'])
            self.assertEqual(changed, self.files())

    def test_book_lock_alone_does_not_authorize_mutation(self):
        with ensure_book_state(self.root, recover=False):
            with self.assertRaisesRegex(ValueError, 'Task and book admission'):
                apply_source_scope(self.root, 1, '0'*64)

    def test_removed_prefix_file_is_preserved_and_changed_bytes_refuse_exact_undo(self):
        path = self.root/'removed.wav'; path.write_bytes(b'original removed artifact')
        chunks_path = self.root/'chunks.json'; rows = json.loads(chunks_path.read_bytes())
        rows[0].update(status='done', audio_path='removed.wav')
        chunks_path.write_text(json.dumps(rows))
        with self.admission():
            plan = get_scope_preview(self.root, 1)
            self.assertEqual('removed.wav', plan['preserved_files'][0]['path'])
            self.assertEqual([], [item for item in plan['audio'] if item['uid']=='uid-0'])
            apply_source_scope(self.root, 1, plan['revision'])
            undo = get_source_scope_undo_preview(self.root)
            self.assertEqual(b'original removed artifact', path.read_bytes())
            path.write_bytes(b'changed after scope apply'); changed = self.files()
            with self.assertRaisesRegex(ValueError, 'artifact changed'):
                apply_source_scope_undo(self.root, undo['revision'])
            self.assertEqual(changed, self.files())

    def test_exports_are_preserved_and_new_delivery_refuses_exact_undo(self):
        path = self.root/'cloned_audiobook.mp3';path.write_bytes(b'old delivery')
        with self.admission():
            plan=get_scope_preview(self.root,1)
            apply_source_scope(self.root,1,plan['revision'])
            undo=get_source_scope_undo_preview(self.root)
            self.assertEqual(b'old delivery',path.read_bytes())
            path.write_bytes(b'new delivery after scope apply');changed=self.files()
            with self.assertRaisesRegex(ValueError,'Exports changed'):
                apply_source_scope_undo(self.root,undo['revision'])
            self.assertEqual(changed,self.files())
