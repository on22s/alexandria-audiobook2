"""A scope preview must never guess chunk provenance or change authored rows."""
import copy
import unittest
from project import group_into_chunks
from project_scope_partition import get_scope_partition


class ScopePartitionTests(unittest.TestCase):
    def fixture(self, entries=None):
        script = entries or [{'speaker': 'NARRATOR', 'text': 'Chapter One', 'instruct': ''},
            {'speaker': 'ALICE', 'text': 'First synthetic line.', 'instruct': ''},
            {'speaker': 'NARRATOR', 'text': 'Chapter Two', 'instruct': ''},
            {'speaker': 'ALICE', 'text': 'Retained synthetic line.', 'instruct': ''}]
        chunks = [{**row, 'id': i, 'uid': f'uid-{i}', 'status': 'done',
                   'audio_path': f'{i}.wav', 'take': 4} for i, row in enumerate(group_into_chunks(script))]
        return script, chunks

    def test_exact_suffix_provenance_preserves_all_caller_bytes_and_take_fields(self):
        script, chunks = self.fixture(); before = copy.deepcopy((script, chunks))
        plan = get_scope_partition(script, chunks, 2)
        self.assertEqual({'script_start': 2, 'chunk_start': 2, 'source_entry_indices': [[0], [1]],
                          'literal_policy_rebind_required': False}, plan)
        self.assertEqual(before, (script, chunks))

    def test_merged_entry_boundary_is_refused_instead_of_regenerating_saved_audio(self):
        script, chunks = self.fixture([{'speaker': 'ALICE', 'text': text, 'instruct': ''}
                                      for text in ['First line.', 'Second line.', 'Third line.']])
        with self.assertRaisesRegex(ValueError, 'splits a saved chunk'):
            get_scope_partition(script, chunks, 1)

    def test_identity_or_authored_drift_and_invalid_selection_are_refused(self):
        for edit in ({'uid': 'uid-0'}, {'id': 0}, {'id': True}, {'text': 'Edited words.'},
                     {'instruct': 'Changed direction'}, {'pause_after': 900}):
            with self.subTest(edit=edit):
                script, chunks = self.fixture(); chunks[2].update(edit)
                with self.assertRaises(ValueError):get_scope_partition(script, chunks, 2)
        script, chunks = self.fixture()
        for value in (0, -1, True, 4, '2'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                get_scope_partition(script, chunks, value)

    def test_malformed_script_text_is_a_refusal_before_grouping(self):
        for value in (None, 7, ['unexpected'], {'text': 'nested'}):
            with self.subTest(value=value):
                script, chunks = self.fixture(); script[2]['text'] = value
                with self.assertRaises(ValueError):get_scope_partition(script, chunks, 2)

    def test_literal_boundary_requires_a_new_scope_proof_and_retains_original_policy(self):
        from literal_policy import bind_literal_policy, CHUNK_POLICY_KEY
        script, _ = self.fixture()
        source = ' '.join(row['text'] for row in script)
        script = bind_literal_policy(source, script)
        script, chunks = self.fixture(script)
        before = copy.deepcopy((script, chunks))
        plan = get_scope_partition(script, chunks, 2)
        self.assertTrue(plan['literal_policy_rebind_required'])
        self.assertEqual([[0], [1]], plan['source_entry_indices'])
        self.assertEqual(before, (script, chunks))
        chunks[2][CHUNK_POLICY_KEY]['source_policy_sha256'] = '0' * 64
        with self.assertRaisesRegex(ValueError, 'no longer match'):
            get_scope_partition(script, chunks, 2)

    def test_long_entry_splits_keep_each_exact_source_owner(self):
        script, _ = self.fixture(); script[3]['text'] = 'Long synthetic sentence. ' * 100
        script, chunks = self.fixture(script)
        plan = get_scope_partition(script, chunks, 2)
        self.assertGreater(len(plan['source_entry_indices']), 2)
        self.assertEqual([0], plan['source_entry_indices'][0])
        self.assertTrue(all(indices == [1] for indices in plan['source_entry_indices'][1:]))

    def test_scene_split_and_narrator_context_are_retained_and_checked(self):
        script, _ = self.fixture()
        script[3].update(text='First sentence. <Second sentence.> ■ Last sentence.',
                         focus_speaker='ALICE', narrator_gender='female')
        script, chunks = self.fixture(script)
        before = copy.deepcopy((script, chunks))
        plan = get_scope_partition(script, chunks, 2)
        self.assertGreater(len(plan['source_entry_indices']), 2)
        self.assertTrue(all(indices == [1] for indices in plan['source_entry_indices'][1:]))
        self.assertEqual(before, (script, chunks))
        chunks[-1]['focus_speaker'] = 'BOB'
        with self.assertRaisesRegex(ValueError, 'no longer match'):
            get_scope_partition(script, chunks, 2)
