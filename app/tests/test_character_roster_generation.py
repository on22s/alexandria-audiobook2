"""Roster projections reach real generation scheduling/frozen-cast code, no model."""
import copy
import hashlib
from pathlib import Path
import unittest
from unittest.mock import patch

from cast_list import get_cast_list_path
from character_roster_apply import (ensure_character_roster_admission, apply_character_roster,
    get_character_roster_undo_status, discard_character_roster_undo)
from character_roster_snapshot import get_character_roster_preview
from tests import test_script_cast_routes as generation
from tests.test_character_roster import SOURCE1, A, entry, decision, proposal
import three_pass_generate as three_pass


class CharacterRosterGenerationTests(unittest.TestCase):
    setUp = generation.CastRoutesTest.setUp
    tearDown = generation.CastRoutesTest.tearDown
    write_list = generation.CastRoutesTest.write_list
    start = generation.CastRoutesTest.start
    stored_options = generation.CastRoutesTest.stored_options

    def test_evidence_only_keeps_dependency_and_material_alias_change_affects_new_runs_only(self):
        root = Path(self.dir)
        Path(self.source).write_text(SOURCE1, encoding='utf-8')
        self.list_path = get_cast_list_path(self.source, self.dir)
        self.write_list([{'name': 'ALICE', 'aliases': []}])
        self.start()
        frozen = Path(generation.script_module.get_run_cast_path())
        initial = frozen.read_bytes(); initial_hash = hashlib.sha256(initial).hexdigest()
        value = proposal(SOURCE1, [entry(SOURCE1, A, 'ALICE', 'Alice')], [decision('add', [], [A])])
        with patch.object(generation.core, 'DATA_DIR', self.dir), patch.object(generation.core, 'process_state', {}):
            with ensure_character_roster_admission(root):
                plan = get_character_roster_preview(root, value)
                self.assertFalse(plan['cast_changed'])
                apply_character_roster(root, value, plan['revision'])
                discard_character_roster_undo(root, get_character_roster_undo_status(root)['revision'])
            self.start()
            self.assertEqual(initial, frozen.read_bytes())
            self.assertEqual(initial_hash, self.stored_options()['cast_sha256'])
            changed = copy.deepcopy(value)
            changed['entries'][0]['aliases'] = ['ALLY']
            changed['decisions'] = [decision('update', [A], [A])]
            with ensure_character_roster_admission(root):
                plan = get_character_roster_preview(root, changed)
                self.assertTrue(plan['cast_changed'])
                apply_character_roster(root, changed, plan['revision'])
            current = Path(self.list_path).read_bytes()
            self.assertNotEqual(initial, current)
            self.assertEqual(initial, frozen.read_bytes())
            self.start(recovery=True)
            self.assertEqual(initial, frozen.read_bytes())
            self.assertEqual(initial_hash, self.stored_options()['cast_sha256'])
            self.start()
            self.assertEqual(current, frozen.read_bytes())
            updated_hash = hashlib.sha256(current).hexdigest()
            self.assertEqual(updated_hash, self.stored_options()['cast_sha256'])
            fingerprint = lambda digest: three_pass.three_pass_fingerprint(SOURCE1, 'fixture-model', 6000, cast_sha256=digest)
            self.assertNotEqual(fingerprint(initial_hash), fingerprint(updated_hash))
        self.assertEqual(SOURCE1, Path(self.source).read_text(encoding='utf-8'))
