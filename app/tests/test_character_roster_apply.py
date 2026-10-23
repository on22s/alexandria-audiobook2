"""Real flat-file transaction/rollback tests for roster approval and exact undo."""
import copy
from contextlib import contextmanager
import json
from pathlib import Path
import unittest
from unittest.mock import patch

import book_state_transaction as transaction
from character_roster_apply import (ensure_character_roster_admission, apply_character_roster,
    apply_character_roster_undo, get_character_roster_undo_status, discard_character_roster_undo)
from character_roster_snapshot import get_character_roster_preview, UNDO
from tests import test_character_roster_snapshot as snapshot_tests


class CharacterRosterApplyTests(unittest.TestCase):
    setUp = snapshot_tests.CharacterRosterSnapshotTests.setUp

    @contextmanager
    def admission(self):
        with patch('core.DATA_DIR', str(self.root)), patch('core.process_state', {}), ensure_character_roster_admission(self.root):
            yield

    def files(self):
        return {p.relative_to(self.root).as_posix(): p.read_bytes() for p in self.root.rglob('*')
                if p.is_file() and not p.name.endswith('.lock') and '.task_ownership' not in p.parts}

    def test_real_approval_and_exact_undo_preserve_script_voices_uid_audio_and_history(self):
        assets = {'annotated_script.json': b'[{"text":"accepted words"}]\n',
                  'voice_config.json': b'{"ALICE":"voice-one"}',
                  'chunks.json': b'[{"uid":"stable-one","audio_path":"one.wav","audio_revision":"one"}]',
                  'one.wav': b'unchanged accepted audio bytes', 'take_history/manifest.json': b'{"versions":["older-take"]}'}
        for name, raw in assets.items():
            p=self.root/name;p.parent.mkdir(exist_ok=True);p.write_bytes(raw)
        with self.admission():
            before=self.files();preview=get_character_roster_preview(self.root,self.proposal)
            result=apply_character_roster(self.root,self.proposal,preview['revision'])
            self.assertTrue(result['approved']);self.assertTrue(result['cast_changed'])
            self.assertTrue((self.root/UNDO).exists())
            for name,raw in assets.items():self.assertEqual(raw,(self.root/name).read_bytes())
            undo=get_character_roster_undo_status(self.root);self.assertTrue(undo['can_undo'])
            apply_character_roster_undo(self.root,undo['revision'])
            self.assertEqual(before,self.files())

    def test_stale_preview_unresolved_undo_and_external_cast_edit_refuse_without_clobber(self):
        with self.admission():
            preview=get_character_roster_preview(self.root,self.proposal);before=self.files()
            with self.assertRaisesRegex(ValueError,'preview changed'):
                apply_character_roster(self.root,self.proposal,'0'*64)
            self.assertEqual(before,self.files())
            apply_character_roster(self.root,self.proposal,preview['revision'])
            current={**self.proposal,'decisions':[]};next_preview=get_character_roster_preview(self.root,current)
            with self.assertRaisesRegex(ValueError,'existing exact'):
                apply_character_roster(self.root,current,next_preview['revision'])
            cast=self.root/preview['snapshot']['paths']['cast'];cast.write_bytes(cast.read_bytes()+b' ')
            before=self.files();undo=get_character_roster_undo_status(self.root)
            self.assertFalse(undo['can_undo']);self.assertTrue(undo['available'])
            with self.assertRaisesRegex(ValueError,'changed after approval'):
                apply_character_roster_undo(self.root,undo['revision'])
            self.assertEqual(before,self.files())
            discard_character_roster_undo(self.root,undo['revision'])
            self.assertFalse((self.root/UNDO).exists());self.assertIn('cast_lists',str(cast))

    def test_metadata_only_approval_preserves_cast_bytes_and_file_version(self):
        with self.admission():
            preview=get_character_roster_preview(self.root,self.proposal)
            apply_character_roster(self.root,self.proposal,preview['revision'])
            undo=get_character_roster_undo_status(self.root);discard_character_roster_undo(self.root,undo['revision'])
            cast=self.root/preview['snapshot']['paths']['cast'];before=cast.read_bytes();stat=cast.stat()
            value=copy.deepcopy(self.proposal);value['decisions']=[]
            preview=get_character_roster_preview(self.root,value)
            result=apply_character_roster(self.root,value,preview['revision'])
            self.assertFalse(result['cast_changed']);self.assertEqual(before,cast.read_bytes())
            self.assertEqual((stat.st_ino,stat.st_mtime_ns),(cast.stat().st_ino,cast.stat().st_mtime_ns))
            apply_character_roster_undo(self.root,get_character_roster_undo_status(self.root)['revision'])
            self.assertEqual(before,cast.read_bytes())

    def test_corrupt_undo_or_current_roster_can_be_inspected_and_explicitly_discarded(self):
        with self.admission():
            preview=get_character_roster_preview(self.root,self.proposal)
            apply_character_roster(self.root,self.proposal,preview['revision'])
        roster=self.root/preview['snapshot']['paths']['roster'];roster.write_bytes(b'{invalid')
        (self.root/UNDO).write_bytes(b'{invalid')
        with self.admission():
            undo=get_character_roster_undo_status(self.root);self.assertFalse(undo['can_undo'])
            before=roster.read_bytes();discard_character_roster_undo(self.root,undo['revision'])
            self.assertEqual(before,roster.read_bytes());self.assertFalse((self.root/UNDO).exists())

    def test_injected_publication_and_undo_move_failures_restore_every_prior_byte(self):
        for mode in ('approve','undo'):
            with self.subTest(mode=mode),self.admission():
                preview=get_character_roster_preview(self.root,self.proposal)
                if mode=='undo':apply_character_roster(self.root,self.proposal,preview['revision'])
                before=self.files();original=transaction._move;count=0
                def fail_once(*args):
                    nonlocal count
                    count+=1
                    if count==2:raise OSError('injected roster publication failure')
                    return original(*args)
                with patch.object(transaction,'_move',fail_once),self.assertRaises(OSError):
                    if mode=='approve':apply_character_roster(self.root,self.proposal,preview['revision'])
                    else:apply_character_roster_undo(self.root,get_character_roster_undo_status(self.root)['revision'])
                self.assertEqual(before,self.files());self.assertEqual([],list(self.root.glob('.book-switch-*')))
                if mode=='undo':apply_character_roster_undo(self.root,get_character_roster_undo_status(self.root)['revision'])

    def test_replaced_undo_during_snapshot_is_refused_and_replacement_is_preserved(self):
        import character_roster_apply as module
        with self.admission():
            preview=get_character_roster_preview(self.root,self.proposal)
            apply_character_roster(self.root,self.proposal,preview['revision'])
            undo=get_character_roster_undo_status(self.root)
            real=module.get_character_roster_snapshot;calls=0;replacement=b'keep replacement undo for inspection'
            def replace_once(*args,**kwargs):
                nonlocal calls
                calls+=1
                if calls==1:(self.root/UNDO).write_bytes(replacement)
                return real(*args,**kwargs)
            with patch.object(module,'get_character_roster_snapshot',replace_once),self.assertRaisesRegex(ValueError,'undo changed while reading'):
                apply_character_roster_undo(self.root,undo['revision'])
            self.assertEqual(replacement,(self.root/UNDO).read_bytes())

    def test_stale_undo_can_be_discarded_with_missing_source_or_invalid_book_state(self):
        with self.admission():
            preview=get_character_roster_preview(self.root,self.proposal)
            apply_character_roster(self.root,self.proposal,preview['revision'])
        self.source.unlink()
        for corrupt_state in (False,True):
            if corrupt_state:(self.root/'state.json').write_bytes(b'{invalid')
            # Recreate the same preserved record for the independent stale case.
            if not (self.root/UNDO).exists():(self.root/UNDO).write_bytes(b'{invalid undo')
            with snapshot_tests.CharacterRosterSnapshotTests.admission(self):
                undo=get_character_roster_undo_status(self.root)
                self.assertTrue(undo['available']);self.assertFalse(undo['can_undo'])
                before=self.files();discard_character_roster_undo(self.root,undo['revision'])
                self.assertEqual({n:r for n,r in before.items() if n!=UNDO},self.files())

    def test_discard_rechecks_exact_undo_revision_and_keeps_a_replacement_record(self):
        import character_roster_apply as module
        with self.admission():
            preview=get_character_roster_preview(self.root,self.proposal)
            apply_character_roster(self.root,self.proposal,preview['revision'])
            undo=get_character_roster_undo_status(self.root)
            real=module.capture_scope_file;replaced=False;replacement=b'keep newly replaced undo'
            def replace_after_capture(*args,**kwargs):
                nonlocal replaced
                result=real(*args,**kwargs)
                if args[1]==UNDO and not replaced:
                    replaced=True;(self.root/UNDO).write_bytes(replacement)
                return result
            with patch.object(module,'capture_scope_file',replace_after_capture),self.assertRaisesRegex(ValueError,'changed before discard'):
                discard_character_roster_undo(self.root,undo['revision'])
            self.assertEqual(replacement,(self.root/UNDO).read_bytes())

    def test_oversized_undo_returns_inspection_status_without_authorizing_discard(self):
        from character_roster_apply import MAX_UNDO_BYTES
        with (self.root/UNDO).open('wb') as stream:stream.truncate(MAX_UNDO_BYTES+1)
        with snapshot_tests.CharacterRosterSnapshotTests.admission(self):
            status=get_character_roster_undo_status(self.root)
            self.assertTrue(status['available']);self.assertFalse(status['can_undo'])
            self.assertIsNone(status['revision']);self.assertIn('reason',status)
            self.assertEqual(MAX_UNDO_BYTES+1,(self.root/UNDO).stat().st_size)

    def test_scope_admission_alone_cannot_publish_without_cast_lock(self):
        with snapshot_tests.CharacterRosterSnapshotTests.admission(self):
            preview=get_character_roster_preview(self.root,self.proposal)
            with self.assertRaisesRegex(ValueError,'cast admission'):
                apply_character_roster(self.root,self.proposal,preview['revision'])


if __name__ == '__main__':
    unittest.main()
