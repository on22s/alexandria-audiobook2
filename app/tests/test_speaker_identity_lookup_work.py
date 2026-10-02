"""Bound exact normalized lookup work without changing fuzzy suggestions."""
import copy
import unittest
from unittest.mock import patch
import speaker_identity as identity


class SpeakerIdentityLookupWorkTests(unittest.TestCase):
    def test_large_existing_roster_uses_linear_normalization_work(self):
        roster = [f'PERSON{i:04}' for i in range(200)]
        entries = [{'speaker': name.lower(), 'text': 'Hello'} for name in roster]
        before = copy.deepcopy(entries)
        with patch.object(identity, '_identity_key', wraps=identity._identity_key) as normalize:
            result = identity.stabilize_speaker_identities(entries, roster)
        self.assertEqual(roster, [row['speaker'] for row in result['entries']])
        self.assertEqual(roster, result['speakers'])
        self.assertEqual(before, entries)
        self.assertLessEqual(normalize.call_count, 20 * len(roster))

    def test_roster_identity_collisions_keep_first_match_and_both_fuzzy_candidates(self):
        result = identity.stabilize_speaker_identities(
            [{'speaker': 'alice', 'text': 'Hi'}, {'speaker': 'Alicee', 'text': 'Hello'}],
            ['A-LICE', 'ALICE'])
        self.assertEqual('A-LICE', result['entries'][0]['speaker'])
        self.assertEqual(['A-LICE', 'ALICE'],
                         [item['speaker'] for item in result['review'][0]['candidates']])
