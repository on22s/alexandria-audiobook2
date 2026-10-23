"""Real source/cast snapshots bind prepared coordinates and preserve exact cast bytes."""
from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from character_roster_snapshot import (get_character_roster_snapshot, get_character_roster_preview,
                                      get_roster_json_bytes, MAX_ROSTER_BYTES)
from project_scope_admission import ensure_scope_admission
from project_scope_source import get_scope_source_snapshot
from tests.test_character_roster import SOURCE1, SOURCE2, A, B, entry, decision, proposal


class CharacterRosterSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve(); self.source = self.root / 'source.txt'
        self.source.write_text(SOURCE1)
        self.state = {'active_book_id': 'fixture', 'book_generation': 'one', 'input_file_path': str(self.source)}
        (self.root/'state.json').write_text(json.dumps(self.state))
        self.proposal = proposal(SOURCE1, [entry(SOURCE1, A, 'ALICE', 'Alice')], [decision('add', [], [A])])

    @contextmanager
    def admission(self):
        with patch('core.DATA_DIR', str(self.root)), patch('core.process_state', {}), ensure_scope_admission(self.root):
            yield

    def save_proposed_files(self, preview):
        for key in ('cast', 'roster'):
            path = self.root / preview['snapshot']['paths'][key]
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(preview[key + '_bytes'])

    def test_preview_creates_no_cast_or_roster_and_binds_changed_state_source_and_proposal(self):
        with self.admission():
            first = get_character_roster_preview(self.root, self.proposal)
            self.assertTrue(first['review']['can_approve']); self.assertFalse(first['approval_current'])
            self.assertFalse((self.root/'cast_lists').exists())
            self.assertEqual(first['revision'], get_character_roster_preview(self.root, self.proposal)['revision'])
            self.state['book_generation'] = 'two'; (self.root/'state.json').write_text(json.dumps(self.state))
            second = get_character_roster_preview(self.root, self.proposal)
            self.assertNotEqual(first['revision'], second['revision'])
            self.source.write_text(SOURCE2)
            with self.assertRaisesRegex(ValueError, 'fingerprint'):
                get_character_roster_preview(self.root, self.proposal)

    def test_metadata_only_review_preserves_exact_cast_dependency_and_legacy_edits_mark_stale(self):
        with self.admission():
            preview = get_character_roster_preview(self.root, self.proposal)
            path = self.root / preview['snapshot']['paths']['cast']; path.parent.mkdir()
            raw = b'{ "cast": [{"name":"alice","aliases":[]}], "provenance":{"written":"old"}}\n'
            path.write_bytes(raw)
            unchanged = get_character_roster_preview(self.root, self.proposal)
            self.assertEqual(unchanged['cast_bytes'], raw); self.assertFalse(unchanged['cast_changed'])
            self.save_proposed_files(unchanged)
            value = proposal(SOURCE1, self.proposal['entries'], [])
            current = get_character_roster_preview(self.root, value)
            self.assertTrue(current['approval_current']); self.assertEqual(current['cast_bytes'], raw)
            path.write_bytes(raw + b' ')
            stale = get_character_roster_preview(self.root, value)
            self.assertFalse(stale['approval_current']); self.assertEqual(stale['cast_bytes'], raw + b' ')

    def test_new_raw_revision_can_explicitly_import_prior_ids_and_reconcile_offsets(self):
        with self.admission():
            old = get_character_roster_preview(self.root, self.proposal)['review']['roster']
            self.source.write_text(SOURCE2)
            imported = proposal(SOURCE2, [entry(SOURCE2, A, 'ALICE', 'Alice')], [decision('update', [A], [A])])
            result = get_character_roster_preview(self.root, imported, old)
            self.assertEqual(result['review']['roster']['entries'][0]['id'], A)
            self.assertTrue(result['review']['can_approve']); self.assertTrue(result['review']['source_changed'])
            self.save_proposed_files(result)
            with self.assertRaisesRegex(ValueError, 'mapping'):
                get_character_roster_preview(self.root, imported, old)

    def test_import_requires_reconciliation_even_when_new_raw_bytes_prepare_identically(self):
        with self.admission():
            old = get_character_roster_preview(self.root, self.proposal)['review']['roster']
            self.source.write_bytes(SOURCE1.replace('\n', '\r\n').encode())
            imported = proposal(SOURCE1, self.proposal['entries'], [])
            with self.assertRaisesRegex(ValueError, 'reconciliation'):
                get_character_roster_preview(self.root, imported, old)
            imported['decisions'] = [decision('update', [A], [A])]
            result = get_character_roster_preview(self.root, imported, old)
            self.assertTrue(result['review']['source_changed'])
            self.assertTrue(result['review']['can_approve'])

    def test_preparation_binding_changes_require_explicit_review_even_if_prepared_text_matches(self):
        with self.admission():
            initial = get_character_roster_preview(self.root, self.proposal)
            self.save_proposed_files(initial)
            self.state.update(script_generation_input_file=str(self.source), script_generation_options={'strip_front_matter': False})
            (self.root/'state.json').write_text(json.dumps(self.state))
            value = proposal(SOURCE1, self.proposal['entries'], [])
            with self.assertRaisesRegex(ValueError, 'reconciliation'):
                get_character_roster_preview(self.root, value)
            value['decisions'] = [decision('update', [A], [A])]
            result = get_character_roster_preview(self.root, value)
            self.assertFalse(result['approval_current']); self.assertFalse(result['cast_changed'])

    def test_saved_binding_must_match_its_source_keyed_cache_and_preserve_bad_record(self):
        with self.admission():
            preview = get_character_roster_preview(self.root, self.proposal)
            self.save_proposed_files(preview)
            path = self.root / preview['snapshot']['paths']['roster']
            saved = json.loads(path.read_bytes())
            saved['source_binding']['raw_sha256'] = '0' * 64
            raw = json.dumps(saved).encode(); path.write_bytes(raw)
            with self.assertRaisesRegex(ValueError, 'source-keyed cache'):
                get_character_roster_snapshot(self.root)
            self.assertEqual(raw, path.read_bytes())
            # Inspection/undo status can still bind file proofs without treating
            # the mismatched record as a current identity baseline.
            self.assertIsNone(get_character_roster_snapshot(self.root, load_saved=False)[3])

    def test_boundary_limits_and_symlinks_refuse_before_preprocessing_or_approval(self):
        with patch('project_scope_source.get_prepared_source', side_effect=AssertionError('must not prepare oversized source')):
            state = {**self.state, 'script_generation_input_file': str(self.source), 'script_generation_options': {'strip_front_matter': True}}
            with self.assertRaisesRegex(ValueError, 'character limit'):
                get_scope_source_snapshot(state, character_limit=1)
            with self.assertRaises(ValueError):
                get_scope_source_snapshot(state, byte_limit=1)
        with self.assertRaisesRegex(ValueError, 'byte limit'):
            get_roster_json_bytes({'large': 'x' * (MAX_ROSTER_BYTES + 1)})
        with self.admission():
            preview = get_character_roster_preview(self.root, self.proposal)
            path = self.root / preview['snapshot']['paths']['cast']; path.parent.mkdir()
            path.symlink_to(self.source)
            with self.assertRaises(ValueError):
                get_character_roster_snapshot(self.root)


if __name__ == '__main__':
    unittest.main()
