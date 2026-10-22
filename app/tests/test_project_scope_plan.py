"""Composite previews bind real saved state and refuse concurrent changes."""
import json
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from book_state_transaction import ensure_book_state, get_book_operation_token
from project import group_into_chunks
import project_scope_plan as scope


class ScopePlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.script = [{'speaker': speaker, 'text': text, 'instruct': ''}
                       for speaker, text in [('ALICE', 'First synthetic line.'),
                                             ('BOB', 'Second retained line.')]]
        self.source = self.root/'source.txt'
        self.source.write_text('\n'.join(row['text'] for row in self.script))
        state = {'active_book_id': 'fixture', 'book_generation': 'generation-one',
                 'input_file_path': str(self.source)}
        chunks = [dict(row, id=i, uid=f'uid-{i}', status='pending', audio_path=None)
                  for i, row in enumerate(group_into_chunks(self.script))]
        for name, data in [('state.json',state), ('annotated_script.json',self.script),
                           ('chunks.json',chunks), ('voice_config.json',{})]:
            (self.root/name).write_text(json.dumps(data))

    def test_composite_preview_binds_partition_source_and_native_book_identity_without_writes(self):
        with ensure_book_state(self.root, recover=False):
            before = {p.name: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
            first = scope.get_scope_preview(self.root, 1)
            self.assertEqual(first, scope.get_scope_preview(self.root, 1))
            self.assertEqual(get_book_operation_token(self.root), first['book_token'])
            self.assertEqual((['uid-0'], ['uid-1']), (first['removed_uids'], first['retained_uids']))
            self.assertEqual((3, 6), (first['source_scope']['retained_start_word'],
                                      first['source_scope']['retained_end_word']))
            self.assertEqual(before, {p.name:p.read_bytes() for p in self.root.iterdir() if p.is_file()})
            self.assertFalse((self.root/'take_history').exists())
            self.source.write_text(self.source.read_text().replace('line.', 'line!'))
            self.assertNotEqual(first['revision'], scope.get_scope_preview(self.root, 1)['revision'])

    def test_missing_admission_and_metadata_changed_during_planning_are_refused(self):
        with self.assertRaisesRegex(ValueError, 'Book admission'):
            scope.get_scope_preview(self.root, 1)
        real = scope.get_scope_voice_plan
        def change(*args, **kwargs):
            plan = real(*args, **kwargs)
            path = self.root/'state.json'; value = json.loads(path.read_bytes())
            value['book_generation'] = 'changed'; path.write_text(json.dumps(value))
            return plan
        with ensure_book_state(self.root, recover=False), patch.object(scope, 'get_scope_voice_plan', change):
            with self.assertRaisesRegex(ValueError, 'metadata changed'):
                scope.get_scope_preview(self.root, 1)

    def test_source_integrity_uses_only_a_bound_range_and_refuses_stale_or_bad_authority(self):
        from merge_integrity import get_source_integrity
        with ensure_book_state(self.root, recover=False):
            plan = scope.get_scope_preview(self.root, 1)
        state = json.loads((self.root/'state.json').read_bytes())
        state['_source_scope'] = plan['source_scope']
        retained = self.script[1:]; raw = json.dumps(retained).encode()
        result = get_source_integrity(state, raw, retained)
        self.assertEqual('verified', result['status'])
        self.assertEqual('verified selected source word range', result['source_scope'])
        changed = [dict(retained[0], text='Invented retained line.')]
        self.assertEqual('differences', get_source_integrity(state, raw, changed)['status'])
        for key, value in [('retained_start_word', True), ('retained_end_word', 99),
                           ('source_text_sha256', '0'*64), ('input_sha256', '0'*64),
                           ('strip_front_matter', False)]:
            invalid = copy.deepcopy(state); invalid['_source_scope'][key] = value
            with self.subTest(key=key):
                self.assertEqual('unavailable', get_source_integrity(invalid, raw, retained)['status'])
        self.source.write_text(self.source.read_text()+' Extra words.')
        self.assertEqual('unavailable', get_source_integrity(state, raw, retained)['status'])

    def test_explicit_book_selection_clears_old_scope_and_scope_hides_full_book_recovery(self):
        from book_state_transaction import apply_book_input_selection
        from script_recovery_facts import get_saved_script_recovery_manifest
        path = self.root/'state.json'; state = json.loads(path.read_bytes())
        state.update(_source_scope={'stale':'scope'}, script_generation_input_file=str(self.source))
        path.write_text(json.dumps(state))
        manifest = self.root/'annotated_script.json.threepass_manifest.json'
        raw = json.dumps({'status':'incomplete','failed_chunk':1}).encode(); manifest.write_bytes(raw)
        self.assertIsNone(get_saved_script_recovery_manifest(self.root, str(manifest)))
        self.assertEqual(raw, manifest.read_bytes())
        apply_book_input_selection(self.root, str(self.source), 'new-selected-book')
        current = json.loads(path.read_bytes())
        self.assertNotIn('_source_scope', current)
        self.assertNotEqual(state['book_generation'], current['book_generation'])
        self.assertEqual('new-selected-book', current['active_book_id'])
        self.assertEqual('incomplete', get_saved_script_recovery_manifest(self.root, str(manifest))['status'])


class ScopePlanSizeTests(unittest.TestCase):
    def test_streamed_revision_matches_canonical_bytes_and_refuses_expanded_limit(self):
        import hashlib
        import project_scope_plan as scope
        plan = {'synthetic': ['Unicode café', {'row': 3}]}
        encoded = json.dumps(plan, sort_keys=True, ensure_ascii=False, allow_nan=False,
                             separators=(',', ':')).encode('utf-8')
        self.assertEqual(hashlib.sha256(encoded).hexdigest(), scope._get_plan_revision(plan))
        with patch.object(scope, 'MAX_SCOPE_PLAN_BYTES', len(encoded)):
            self.assertEqual(hashlib.sha256(encoded).hexdigest(), scope._get_plan_revision(plan))
        with patch.object(scope, 'MAX_SCOPE_PLAN_BYTES', len(encoded)-1):
            with self.assertRaisesRegex(ValueError, 'bounded scope preview'):
                scope._get_plan_revision(plan)
