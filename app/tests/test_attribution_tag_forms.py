"""Exact source tags resolve names without inventing or merging characters."""
import unittest
from pass_quality import get_attribution_tag_evidence, validate_attribution


class TagFormsTests(unittest.TestCase):
    entry = [{'type': 'SPOKEN', 'text': 'Bring the lantern.'}]

    def evidence(self, tag, names, extra=''):
        return get_attribution_tag_evidence(self.entry,
            '“Bring the lantern.” ' + tag + ' ' + extra, names)

    def test_titled_first_and_surname_forms(self):
        for tag, expected in [('said Lord Henry.', 'LORD HENRY WOTTON'),
                              ('Hallward whispered.', 'BASIL HALLWARD')]:
            with self.subTest(tag=tag):
                self.assertEqual({0: expected}, self.evidence(tag,
                    ['LORD HENRY WOTTON', 'BASIL HALLWARD']))

    def test_ambiguous_title_and_surname_abstain_even_if_attested(self):
        for tag, names in [('said Lord Henry.', ['LORD HENRY WOTTON', 'LORD HENRY JONES']),
                           ('said Hallward.', ['BASIL HALLWARD', 'MARY HALLWARD'])]:
            with self.subTest(tag=tag):
                self.assertEqual({}, self.evidence(tag, names, tag * 3))

    def test_aliases_of_one_identity_do_not_create_ambiguity(self):
        source = '“Bring the lantern.” said Hallward.'
        self.assertEqual({0: 'BASIL HALLWARD'}, get_attribution_tag_evidence(
            self.entry, source, ['BASIL HALLWARD', 'HALLWARD'],
            {'HALLWARD': 'BASIL HALLWARD'}))

    def test_new_source_attested_name_with_empty_or_partial_roster(self):
        for names in ([], ['LORD HENRY WOTTON']):
            self.assertEqual({0: 'LADY AGATHA'}, self.evidence(
                'whispered Lady Agatha.', names, 'Lady Agatha waited.'))

    def test_unattested_long_source_abstains(self):
        self.assertEqual({}, self.evidence('said Imaginary.', [], 'ordinary prose ' * 2000))

    def test_pronouns_and_title_alone_do_not_become_names(self):
        for tag in ('said He.', 'said She.', 'said Lord.'):
            self.assertEqual({}, self.evidence(tag, ['LORD HENRY WOTTON'], tag * 3))

    def test_tag_contradiction_rejects_known_wrong_speaker(self):
        source = '“Bring the lantern.” said Lord Henry. Dorian Gray listened.'
        report = validate_attribution(self.entry, [{'n': 0, 'speaker': 'DORIAN GRAY'}],
            source, known_names=['LORD HENRY WOTTON', 'DORIAN GRAY'])
        self.assertFalse(report['passed'])
        self.assertEqual('speaker_contradicts_source_tag', report['findings'][0]['code'])

    def test_repeated_quote_remains_uncheckable(self):
        self.assertEqual({}, self.evidence('said Hallward.', ['BASIL HALLWARD'],
            '“Bring the lantern.” said Hallward.'))
