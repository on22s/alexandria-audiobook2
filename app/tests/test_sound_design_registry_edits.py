"""Immutable cue editing controls; no active book or media is published."""
import copy
import unittest
from unittest.mock import patch

from sound_design_registry import (get_sound_design_registry_bytes,
    get_sound_design_registry_edit, get_sound_design_registry_selection,
    get_selected_sound_design_versions)
from sound_design_cues import get_sound_design_cue_plan


def recipe():
    return dict(book=dict(book_id='synthetic-book', source_sha256='a'*64, script_sha256='b'*64),
        selection=dict(chunk_uid='synthetic-chunk', generation_sha256='c'*64,
                       audio_sha256='d'*64, audio_revision='e'*32),
        boundary='start', offset_frames=0, frame_rate=24000, kind='effect',
        asset=dict(asset_id='synthetic-asset', sha256='f'*64, frames=3,
                   frame_rate=24000, channels=1, sample_width=4),
        duration=dict(mode='native', frames=None, loop_crossfade_frames=None),
        mix=dict(gain_db=-12, duck_db=0, fade_in_frames=0, fade_out_frames=0))


class SoundDesignRegistryEditTests(unittest.TestCase):
    def empty(self):
        return dict(schema=1, book=recipe()['book'], versions=[], selected={})

    def test_append_edit_select_and_deselect_retain_detached_history(self):
        empty = self.empty(); authored = recipe()
        first = get_sound_design_registry_edit(empty, 'cue', authored)
        original = get_sound_design_registry_bytes(first); parent = first['selected']['cue']
        authored['mix']['gain_db'] = -24
        second = get_sound_design_registry_edit(first, 'cue', authored, parent)
        self.assertEqual(original, get_sound_design_registry_bytes(first))
        self.assertEqual([], empty['versions']); self.assertEqual(2, len(second['versions']))
        self.assertEqual(parent, second['versions'][1]['parent_version'])
        previous = get_sound_design_registry_selection(second, 'cue', parent)
        self.assertEqual(parent, previous['selected']['cue'])
        removed = get_sound_design_registry_selection(previous, 'cue', None)
        self.assertEqual({}, removed['selected']); self.assertEqual(second['versions'], removed['versions'])
        restored = get_sound_design_registry_selection(removed, 'cue', second['selected']['cue'])
        self.assertEqual(second, restored)
        restored['versions'][0]['recipe']['mix']['gain_db'] = -50
        self.assertEqual(original, get_sound_design_registry_bytes(first))

    def test_explicit_historical_parent_allows_branch_without_erasing_other_revisions(self):
        first = get_sound_design_registry_edit(self.empty(), 'cue', recipe()); parent = first['selected']['cue']
        changed = recipe(); changed['offset_frames'] = 1
        second = get_sound_design_registry_edit(first, 'cue', changed, parent)
        changed['offset_frames'] = 2
        branch = get_sound_design_registry_edit(second, 'cue', changed, parent)
        self.assertEqual(3, len(branch['versions']))
        self.assertEqual(second['versions'], branch['versions'][:2])
        self.assertEqual(parent, branch['versions'][-1]['parent_version'])

    def test_new_origins_wrong_parent_other_book_and_invalid_recipe_refused(self):
        current = get_sound_design_registry_edit(self.empty(), 'one', recipe())
        parent = current['selected']['one']; original = get_sound_design_registry_bytes(current)
        wrong_book = recipe(); wrong_book['book']['book_id'] = 'other-book'
        invalid = recipe(); invalid['mix']['gain_db'] = float('nan')
        for cue, value, ancestor in (('one',recipe(),None), ('two',recipe(),parent),
                                     ('one',recipe(),'0'*64), ('one',wrong_book,parent),
                                     ('one',invalid,parent)):
            with self.subTest(cue=cue, parent=ancestor), self.assertRaises(ValueError):
                get_sound_design_registry_edit(current,cue,value,ancestor)
            self.assertEqual(original,get_sound_design_registry_bytes(current))

    def test_unknown_or_cross_cue_selection_never_changes_history(self):
        current = get_sound_design_registry_edit(self.empty(),'one',recipe())
        current = get_sound_design_registry_edit(current,'two',recipe())
        original = get_sound_design_registry_bytes(current)
        for cue, version in (('missing',None), ('one',current['selected']['two']),
                             ('one','0'*64), ('one',False), ([],None)):
            with self.subTest(cue=cue), self.assertRaises(ValueError):
                get_sound_design_registry_selection(current,cue,version)
            self.assertEqual(original,get_sound_design_registry_bytes(current))

    def test_selected_versions_use_stable_cue_order_and_do_not_reanchor_stale_data(self):
        current = get_sound_design_registry_edit(self.empty(),'zeta',recipe())
        current = get_sound_design_registry_edit(current,'alpha',recipe())
        selected = get_selected_sound_design_versions(current)
        self.assertEqual(['alpha','zeta'],[row['cue_id'] for row in selected])
        changed_book = {**current['book'], 'script_sha256':'0'*64}
        plan = dict(frames=10,frame_rate=24000,channels=1,sample_width=4,
                    source_frame_ranges=[[0,10]],intentional_pause_frames=[])
        projection = get_sound_design_cue_plan(selected,changed_book,[recipe()['selection']],plan)
        self.assertFalse(projection['can_project'])
        self.assertEqual(['stale','stale'],[row['status'] for row in projection['cues']])
        selected[0]['recipe']['mix']['gain_db'] = -40
        self.assertEqual(-12,current['versions'][1]['recipe']['mix']['gain_db'])

    def test_version_and_joint_byte_limits_precede_new_revision_hashing(self):
        current = get_sound_design_registry_edit(self.empty(),'one',recipe())
        limit = len(get_sound_design_registry_bytes(current))
        for options in ({'MAX_VERSIONS':1},{'MAX_REGISTRY_BYTES':limit}):
            with self.subTest(options=options), patch.multiple('sound_design_registry',**options), \
                    patch('sound_design_cues.get_sound_design_cue_version',side_effect=AssertionError('new revision hashed too soon')):
                # Existing history validation uses this function too. Replace
                # only its already-validated fingerprint check for this control.
                with patch('sound_design_cues.validate_sound_design_cue_version'):
                    with self.assertRaises(ValueError):
                        get_sound_design_registry_edit(current,'two',recipe())

    def test_selected_count_limit_preserves_unselected_history(self):
        current = get_sound_design_registry_edit(self.empty(),'one',recipe())
        current = get_sound_design_registry_edit(current,'two',recipe())
        removed = get_sound_design_registry_selection(current,'two',None)
        with patch('sound_design_cues.MAX_CUES',1):
            with self.assertRaises(ValueError):
                get_sound_design_registry_selection(removed,'two',current['selected']['two'])
            with self.assertRaises(ValueError):get_sound_design_registry_edit(removed,'three',recipe())
        self.assertEqual(current['versions'],removed['versions'])

    def test_invalid_existing_history_refuses_even_deselection(self):
        current = get_sound_design_registry_edit(self.empty(),'one',recipe())
        damaged = copy.deepcopy(current); damaged['versions'][0]['recipe']['mix']['gain_db'] = -30
        with self.assertRaisesRegex(ValueError,'fingerprint'):
            get_sound_design_registry_selection(damaged,'one',None)
