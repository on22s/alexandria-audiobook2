"""Reindexing must preserve the same native effective voice for every retained row."""
import copy
import unittest
from unittest.mock import patch
from project_scope_voices import get_scope_voice_plan
from tts import get_chunk_voice_inputs


class ScopeVoiceTests(unittest.TestCase):
    def rows(self, speaker='Hero', **context):
        return [dict(uid=f'uid-{i}', speaker=speaker, text=f'Synthetic line {i}.', **context)
                for i in range(6)]

    def test_versions_and_styles_carry_boundary_state_and_shift_future_points(self):
        voices = {'Hero': {'type': 'custom', 'voice': 'Ryan', 'character_style': 'Young',
            'versions': {'adult': {'voice': 'Vivian'}},
            'version_timeline': [{'from_index': 1, 'version_id': 'adult'}],
            'style_timeline': [{'from_index': 5, 'character_style': 'Older'},
                               {'from_index': 1, 'character_style': 'Adult'}]}}
        rows = self.rows(); original = copy.deepcopy((voices, rows))
        plan = get_scope_voice_plan(voices, rows, 2)
        new = plan['voices']['Hero']
        self.assertEqual([{'from_index': 0, 'version_id': 'adult'}], new['version_timeline'])
        self.assertEqual([{'from_index': 0, 'character_style': 'Adult'},
                          {'from_index': 3, 'character_style': 'Older'}], new['style_timeline'])
        self.assertNotEqual(get_chunk_voice_inputs(voices, rows[2], 2),
                            get_chunk_voice_inputs(voices, rows[2], 0))
        for old, mapping in zip(range(2, 6), plan['retained']):
            self.assertEqual((f'uid-{old}', old, old-2),
                (mapping['uid'], mapping['old_index'], mapping['new_index']))
            self.assertEqual(get_chunk_voice_inputs(voices, rows[old], old),
                             get_chunk_voice_inputs(plan['voices'], rows[old], old-2))
        self.assertEqual(original, (voices, rows))

    def test_aliases_ensembles_and_dynamic_narrator_preserve_native_dispatch(self):
        voices = {'Hero': {'type': 'ensemble', 'members': ['Member']},
            'Member': {'type': 'custom', 'voice': 'Ryan',
                'versions': {'adult': {'voice': 'Vivian'}},
                'version_timeline': [{'from_index': 2, 'version_id': 'adult'}]},
            'Alias': {'alias_of': 'Hero'},
            'NARRATOR': {'type': 'custom', 'voice': 'Ryan', 'narrator_strategy': 'focus'}}
        for speaker, context in [('Alias', {}), ('NARRATOR', {'focus_speaker': 'Hero'})]:
            with self.subTest(speaker=speaker):
                rows = self.rows(speaker, **context)
                plan = get_scope_voice_plan(voices, rows, 2)
                self.assertEqual('Vivian', plan['retained'][0]['effective_voices']['Member']['voice'])

    def test_equal_boundary_points_use_native_last_point_and_keep_future_changes(self):
        voices = {'Hero': {'type': 'custom', 'voice': 'Ryan', 'style_timeline': [
            {'from_index': '2', 'character_style': 'First'},
            {'from_index': 2, 'character_style': 'Last'},
            {'from_index': 4, 'character_style': ''}]}}
        plan = get_scope_voice_plan(voices, self.rows(), 2)
        self.assertEqual([{'from_index': 0, 'character_style': 'Last'},
                          {'from_index': 2, 'character_style': ''}],
                         plan['voices']['Hero']['style_timeline'])

    def test_malformed_timelines_and_alias_cycles_are_refused_without_changes(self):
        configs = [{'Hero': {'style_timeline': [None]}}, {'Hero': {'version_timeline': {}}},
                   {'Hero': {'style_timeline': [{'from_index': True}]}},
                   {'Hero': {'alias_of': 'Hero'}}, {'Hero': None}]
        for config in configs:
            before = copy.deepcopy(config)
            with self.subTest(config=config), self.assertRaises(ValueError):
                get_scope_voice_plan(config, self.rows(), 2)
            self.assertEqual(before, config)

    def test_repeated_effective_voice_metadata_refuses_expanded_preview_limit(self):
        voices = {'Hero': {'type':'custom', 'voice':'Ryan', 'description':'Synthetic description. ' * 100}}
        original = copy.deepcopy(voices)
        with patch('project_scope_voices.MAX_SCOPE_VOICE_MAPPING_BYTES', 3000):
            with self.assertRaisesRegex(ValueError, 'bounded scope preview'):
                get_scope_voice_plan(voices, self.rows(), 1)
        self.assertEqual(original, voices)
