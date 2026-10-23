"""Two shareable book revisions and explicit identity/import reconciliation controls."""
import copy
import hashlib
import unittest

from character_roster import get_character_roster_review, validate_character_roster

SOURCE1 = '“Wait,” Alice said.\nMiss Reed nodded.\nBob left.'
SOURCE2 = 'Chapter One\n“Wait,” Alice said.\nMiss Reed nodded.\nBob returned.'
A, B, C, D = ['character_' + c * 20 for c in 'abcd']


def entry(source, identity, name, quote, aliases=()):
    start = source.index(quote)
    return {'id': identity, 'canonical_name': name, 'aliases': list(aliases),
            'evidence': [{'source_quote': quote, 'start_char': start, 'end_char': start + len(quote)}]}


def decision(action, before, after):
    return {'action': action, 'from_ids': before, 'to_ids': after}


def proposal(source, entries, decisions):
    return {'source_sha256': hashlib.sha256(source.encode()).hexdigest(), 'entries': entries, 'decisions': decisions}


class CharacterRosterTests(unittest.TestCase):
    def initial(self):
        rows = [entry(SOURCE1, A, 'ALICE', 'Alice'), entry(SOURCE1, B, 'MISS REED', 'Miss Reed')]
        return get_character_roster_review(SOURCE1, None, proposal(SOURCE1, rows,
            [decision('add', [], [A]), decision('add', [], [B])]))['roster']

    def test_import_requires_explicit_adds_and_returns_detached_records(self):
        rows = [entry(SOURCE1, A, 'ALICE', 'Alice')]
        value = proposal(SOURCE1, rows, [])
        with self.assertRaisesRegex(ValueError, 'explicit decision'):
            get_character_roster_review(SOURCE1, None, value)
        value['decisions'] = [decision('add', [], [A])]
        before = copy.deepcopy(value)
        review = get_character_roster_review(SOURCE1, None, value)
        self.assertTrue(review['can_approve']); self.assertEqual(value, before)
        review['roster']['entries'][0]['aliases'].append('STRANGER')
        self.assertEqual(value, before)

    def test_revision_reconciles_same_ids_without_silent_offset_relocation(self):
        old = self.initial()
        value = proposal(SOURCE2, [entry(SOURCE2, A, 'ALICE', 'Alice'), entry(SOURCE2, B, 'MISS REED', 'Miss Reed')], [])
        with self.assertRaisesRegex(ValueError, 'reconciliation'):
            get_character_roster_review(SOURCE2, old, value)
        value['decisions'] = [decision('update', [A], [A]), decision('update', [B], [B])]
        review = get_character_roster_review(SOURCE2, old, value)
        self.assertTrue(review['source_changed']); self.assertTrue(review['can_approve'])
        self.assertEqual([e['id'] for e in review['roster']['entries']], [A, B])
        value['entries'][0]['evidence'] = copy.deepcopy(old['entries'][0]['evidence'])
        self.assertFalse(get_character_roster_review(SOURCE2, old, value)['can_approve'])

    def test_merge_preserves_survivor_and_split_allocates_new_identity(self):
        old = self.initial()
        merged = entry(SOURCE1, A, 'ALICE', 'Alice', ['MISS REED'])
        review = get_character_roster_review(SOURCE1, old, proposal(SOURCE1, [merged], [decision('merge', [A, B], [A])]))
        self.assertEqual(review['roster']['retired_ids'], [B])
        split = [entry(SOURCE1, A, 'ALICE', 'Alice'), entry(SOURCE1, C, 'MISS REED', 'Miss Reed')]
        result = get_character_roster_review(SOURCE1, review['roster'], proposal(SOURCE1, split, [decision('split', [A], [A, C])]))
        self.assertEqual([e['id'] for e in result['roster']['entries']], [A, C])
        self.assertEqual(result['roster']['retired_ids'], [B])
        split[1]['id'] = B
        with self.assertRaisesRegex(ValueError, 'retired identity'):
            get_character_roster_review(SOURCE1, review['roster'], proposal(SOURCE1, split, [decision('split', [A], [A, B])]))

    def test_split_can_explicitly_retire_original_and_allocate_only_new_ids(self):
        old = self.initial()
        rows = [entry(SOURCE1, C, 'ALICE', 'Alice'), entry(SOURCE1, D, 'MISS REED', 'Miss Reed')]
        decisions = [decision('split', [A], [C, D]), decision('retire', [B], [])]
        result = get_character_roster_review(SOURCE1, old, proposal(SOURCE1, rows, decisions))
        self.assertEqual(result['roster']['retired_ids'], [A, B])
        self.assertEqual([e['id'] for e in result['roster']['entries']], [C, D])
        self.assertTrue(result['can_approve'])

    def test_retirement_history_and_decision_identity_lists_refuse_malformed_imports(self):
        old = self.initial()
        for retired in ([C, C], ['wrong'], [None], [C] * 2001):
            with self.subTest(retired=retired[:2]), self.assertRaises(ValueError):
                validate_character_roster({**old, 'retired_ids': retired})
        for decisions in ([decision('merge', [A, A], [A])], [decision('update', [C], [C])],
                          [decision('split', [A], [A, A])], [decision('add', [], [C])]):
            with self.subTest(decisions=decisions), self.assertRaises(ValueError):
                get_character_roster_review(SOURCE1, old, proposal(SOURCE1, old['entries'], decisions))

    def test_rename_alias_and_retirement_are_explicit(self):
        old = self.initial(); rows = [entry(SOURCE1, A, 'ALICE REED', 'Alice', ['ALICE'])]
        value = proposal(SOURCE1, rows, [decision('update', [A], [A])])
        with self.assertRaisesRegex(ValueError, 'explicit decision'):
            get_character_roster_review(SOURCE1, old, value)
        value['decisions'].append(decision('retire', [B], []))
        review = get_character_roster_review(SOURCE1, old, value)
        self.assertEqual(review['cast'], [{'name': 'ALICE REED', 'aliases': ['ALICE']}])
        self.assertEqual(review['roster']['retired_ids'], [B])
        validate_character_roster(review['roster'])

    def test_competing_decisions_cannot_steal_or_replace_existing_ids(self):
        old = self.initial(); rows = copy.deepcopy(old['entries'])
        invalid = [
            [decision('update', [A], [B])],
            [decision('merge', [A, B], [A])],  # B was not removed
            [decision('add', [], [A])],
            [decision('update', [A], [A]), decision('retire', [A], [])],
            [decision('split', [A], [A, B])],
        ]
        for decisions in invalid:
            with self.subTest(decisions=decisions), self.assertRaises(ValueError):
                get_character_roster_review(SOURCE1, old, proposal(SOURCE1, rows, decisions))

    def test_unanchored_ambiguous_and_missing_claims_cannot_be_approved(self):
        for quote in ('Alice', 'Nobody'):
            value = proposal(SOURCE1, [entry(SOURCE1, A, 'ALICE', 'Alice')], [decision('add', [], [A])])
            value['entries'][0]['evidence'] = [{'source_quote': quote, 'start_char': None, 'end_char': None}]
            self.assertFalse(get_character_roster_review(SOURCE1, None, value)['can_approve'])
        source = SOURCE1 + ' Alice'
        value = proposal(source, [entry(source, A, 'ALICE', 'Alice')], [decision('add', [], [A])])
        value['entries'][0]['evidence'][0].update(start_char=None, end_char=None)
        self.assertFalse(get_character_roster_review(source, None, value)['can_approve'])

    def test_alias_conflicts_protected_labels_and_malformed_imports_are_refused(self):
        old = self.initial()
        for aliases in (['MISS REED'], ['UNKNOWN'], ['alice'], ['X'] * 33, ['X', 'x'], [chr(0xD800)]):
            rows = copy.deepcopy(old['entries']); rows[0]['aliases'] = aliases
            with self.subTest(aliases=aliases), self.assertRaises(ValueError):
                get_character_roster_review(SOURCE1, old, proposal(SOURCE1, rows, [decision('update', [A], [A])]))
        for field, bad in [('schema', True), ('source_characters', True), ('retired_ids', [A]), ('entries', [])]:
            invalid = {**old, field: bad}
            with self.subTest(field=field), self.assertRaises(ValueError):
                validate_character_roster(invalid)


if __name__ == '__main__':
    unittest.main()
