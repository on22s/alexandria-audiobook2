"""Actual saved/journal/source snapshots for cue editing; no model admission."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import wave

import book_state_transaction as books
from chunk_status_journal import ChunkStatusJournal
import sound_design_context as context


class SoundDesignContextTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(); self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.source = self.root/'source.txt'; self.source.write_text('Synthetic spoken line.')
        self.state = dict(active_book_id='synthetic-book',book_generation='first',input_file_path=str(self.source))
        self.row = dict(uid='synthetic-uid',id=0,speaker='NARRATOR',text='Synthetic spoken line.',
                        status='done',audio_path='audio.wav',audio_revision=None)
        for name,data in (('state.json',self.state),('annotated_script.json',[self.row]),
                           ('chunks.json',[self.row]),('voice_config.json',{}),('config.json',{})):
            self.write(name,data)
        self.audio = self.root/'audio.wav'
        with wave.open(str(self.audio),'wb') as stream:
            stream.setparams((1,2,24000,3,'NONE','not compressed')); stream.writeframes(b'\x01\x00'*3)

    def write(self,name,data):
        (self.root/name).write_text(json.dumps(data))

    @contextmanager
    def admitted(self):
        # The fixture grants task admission, retaining the actual native book
        # guard. Route tests must separately exercise configured task exclusion.
        with books.ensure_book_state(self.root,recover=False), \
                patch.object(context,'is_scope_admitted',side_effect=books.is_book_state_admitted):
            yield

    def files(self):
        return {str(path.relative_to(self.root)):path.read_bytes()
                for path in self.root.rglob('*') if path.is_file()}

    def test_context_and_anchor_are_read_only_and_bound_to_actual_encoded_bytes(self):
        with self.admitted():
            before = self.files(); first = context.capture_sound_design_context(self.root)
            second = context.capture_sound_design_context(self.root)
            self.assertEqual(first,second)
            self.assertEqual(books.get_book_operation_token(self.root),first['book_token'])
            self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(),first['book']['source_sha256'])
            anchor = context.capture_sound_design_anchor(self.root,first,self.row['uid'])
            self.assertEqual(hashlib.sha256(self.audio.read_bytes()).hexdigest(),anchor['selection']['audio_sha256'])
            self.assertIsNone(anchor['selection']['audio_revision'])  # legacy selected take remains usable
            self.assertEqual(before,self.files()); self.assertEqual([],first['registry']['versions'])

    def test_source_script_state_voice_settings_and_rows_change_context_identity(self):
        with self.admitted():
            baseline = context.capture_sound_design_context(self.root)['context_revision']
            for name,data in (('state.json',{**self.state,'book_generation':'other'}),
                               ('annotated_script.json',[{**self.row,'text':'Changed script.'}]),
                               ('voice_config.json',{'NARRATOR':{'speaker':'Ryan'}}),
                               ('config.json',{'tts':{'language':'English'}}),
                               ('chunks.json',[{**self.row,'audio_revision':'explicit-revision'}])):
                baseline = context.capture_sound_design_context(self.root)['context_revision']
                path = self.root/name; original = path.read_bytes(); self.write(name,data)
                self.assertNotEqual(baseline,context.capture_sound_design_context(self.root)['context_revision'])
                path.write_bytes(original)
            self.source.write_text('Changed original source.')
            self.assertNotEqual(baseline,context.capture_sound_design_context(self.root)['context_revision'])

    def test_native_journal_replay_uses_latest_selected_take_without_compacting(self):
        journal = ChunkStatusJournal(self.root/'chunks.json',
            {key:self.state[key] for key in ('active_book_id','book_generation')})
        journal.apply_update(self.row['uid'],{'audio_revision':'selected-second-take'})
        with self.admitted():
            before = self.files(); snapshot = context.capture_sound_design_context(self.root)
            anchor = context.capture_sound_design_anchor(self.root,snapshot,self.row['uid'])
            self.assertEqual('selected-second-take',anchor['selection']['audio_revision'])
            self.assertEqual(before,self.files())

    def test_existing_book_and_audio_revision_limits_are_preserved(self):
        self.write('state.json',{**self.state,'active_book_id':'b'*1024})
        self.write('chunks.json',[{**self.row,'audio_revision':'r'*128}])
        with self.admitted():
            snapshot = context.capture_sound_design_context(self.root)
            anchor = context.capture_sound_design_anchor(self.root,snapshot,self.row['uid'])
            self.assertEqual('b'*1024,snapshot['book']['book_id'])
            self.assertEqual('r'*128,anchor['selection']['audio_revision'])

    def test_changed_audio_payload_changes_anchor_even_with_unchanged_chunk_metadata(self):
        with self.admitted():
            snapshot = context.capture_sound_design_context(self.root)
            first = context.capture_sound_design_anchor(self.root,snapshot,self.row['uid'])
            raw = bytearray(self.audio.read_bytes()); raw[-1] ^= 1; self.audio.write_bytes(raw)
            second = context.capture_sound_design_anchor(self.root,snapshot,self.row['uid'])
            self.assertNotEqual(first['anchor_revision'],second['anchor_revision'])
            self.assertNotEqual(first['selection']['audio_sha256'],second['selection']['audio_sha256'])

    def test_aliases_inside_project_work_and_outside_source_is_refused(self):
        with self.admitted():
            for path in (str(self.audio),'nested/../audio.wav'):
                (self.root/'nested').mkdir(exist_ok=True)
                self.write('chunks.json',[{**self.row,'audio_path':path}])
                snapshot = context.capture_sound_design_context(self.root)
                actual = context.capture_sound_design_anchor(self.root,snapshot,self.row['uid'])
                self.assertEqual('audio.wav',actual['audio']['path'])
            self.write('chunks.json',[{**self.row,'audio_path':str(self.root.parent/'external.wav')}])
            snapshot = context.capture_sound_design_context(self.root)
            with self.assertRaisesRegex(ValueError,'escapes'):
                context.capture_sound_design_anchor(self.root,snapshot,self.row['uid'])

    def test_missing_uid_pending_take_missing_empty_and_nonregular_audio_refuse(self):
        with self.admitted():
            for fields in ({'uid':''},{'status':'pending'},{'audio_path':'missing.wav'},
                           {'audio_path':'empty.wav'},{'audio_path':'nested'}):
                (self.root/'empty.wav').write_bytes(b''); (self.root/'nested').mkdir(exist_ok=True)
                self.write('chunks.json',[{**self.row,**fields}])
                with self.subTest(fields=fields), self.assertRaises(ValueError):
                    snapshot = context.capture_sound_design_context(self.root)
                    context.capture_sound_design_anchor(self.root,snapshot,self.row['uid'])

    def test_no_admission_and_joint_limits_fail_before_original_source_scan(self):
        with self.assertRaisesRegex(ValueError,'admission'):context.capture_sound_design_context(self.root)
        with self.admitted(), patch.object(context,'MAX_CONTEXT_BYTES',32), \
                patch.object(context,'get_scope_source_snapshot') as source:
            with self.assertRaises(ValueError):context.capture_sound_design_context(self.root)
            source.assert_not_called()

    def test_joint_budget_counts_captured_files_and_detached_rows(self):
        raw = sum((self.root/name).stat().st_size for name in context._FILES if (self.root/name).exists())
        rows = len(context.get_metadata_json_bytes([self.row],100000,'fixture'))
        with self.admitted(), patch.object(context,'MAX_CONTEXT_BYTES',raw+rows-1), \
                patch.object(context,'get_scope_source_snapshot') as source:
            with self.assertRaisesRegex(ValueError,'Cue context rows exceeds'):
                context.capture_sound_design_context(self.root)
            source.assert_not_called()
        with self.admitted(), patch.object(context,'MAX_CONTEXT_BYTES',raw+rows):
            self.assertEqual(1,len(context.capture_sound_design_context(self.root)['chunks']))

    def test_duplicate_dialogue_identities_refuse_before_source_scan(self):
        self.write('chunks.json',[self.row,dict(self.row)])
        with self.admitted(), patch.object(context,'get_scope_source_snapshot') as source:
            with self.assertRaisesRegex(ValueError,'unique nonempty UIDs'):
                context.capture_sound_design_context(self.root)
            source.assert_not_called()

    def test_malformed_script_refuses_before_source_scan(self):
        for raw in (b'{invalid',b'{}',b'[]',b'[null]',b'[{"text":3}]',
                    b'[{"text":"one","text":"two"}]',b'[{"text":"line","speaker":null}]'):
            (self.root/'annotated_script.json').write_bytes(raw)
            with self.subTest(raw=raw), self.admitted(), \
                    patch.object(context,'get_scope_source_snapshot') as source:
                with self.assertRaises(ValueError):context.capture_sound_design_context(self.root)
                source.assert_not_called()

    def test_external_metadata_mutation_mid_capture_is_refused(self):
        source = context.get_scope_source_snapshot
        def change_after_read(*args,**kwargs):
            result = source(*args,**kwargs)
            self.write('chunks.json',[{**self.row,'audio_revision':'changed-during-source-read'}])
            return result
        with self.admitted(), patch.object(context,'get_scope_source_snapshot',side_effect=change_after_read):
            with self.assertRaisesRegex(ValueError,'changed while capturing'):
                context.capture_sound_design_context(self.root)

    def test_foreign_registry_and_nonfinite_saved_rows_are_preserved(self):
        with self.admitted():
            foreign = dict(schema=1,book=dict(book_id='foreign',source_sha256='a'*64,script_sha256='b'*64),
                           versions=[],selected={})
            self.write('.sound_design_cues.json',foreign); before = self.files()
            with self.assertRaisesRegex(ValueError,'another book'):context.capture_sound_design_context(self.root)
            self.assertEqual(before,self.files()); (self.root/'.sound_design_cues.json').unlink()
            self.write('chunks.json',[{**self.row,'pause_after':float('nan')}]); before = self.files()
            with self.assertRaises(ValueError):context.capture_sound_design_context(self.root)
            self.assertEqual(before,self.files())
