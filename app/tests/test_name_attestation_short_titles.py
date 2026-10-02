"""Short sources and shared role prefixes cannot manufacture name evidence."""
import unittest
from pass_quality import is_attested_name, validate_attribution
from three_pass_generate import build_roster, attested_new_speakers


class NameAttestationShortTitleTests(unittest.TestCase):
    def test_short_source_rejects_inventions_in_both_shared_gates(self):
        source = 'Alice said, “Hello.” She smiled.'
        frozen = [{'type': 'SPOKEN', 'text': 'Hello.'}]
        for name in ('MORDRED', 'FUTURE ME', 'INVENTED'):
            with self.subTest(name=name):
                self.assertFalse(is_attested_name(name, source))
                report = validate_attribution(frozen, [{'n': 0, 'speaker': name}], source_text=source)
                self.assertFalse(report['passed'])
                self.assertEqual('speaker_not_in_source', report['findings'][0]['code'])
                self.assertEqual([], build_roster([{'speaker': name}], source))
                self.assertEqual([], attested_new_speakers([{'speaker': name}], set(), source))

    def test_once_named_real_character_in_short_source_is_allowed(self):
        source = 'Alice said, “Hello.” She smiled.'
        self.assertTrue(is_attested_name('ALICE', source))
        self.assertEqual(['ALICE'], build_roster([{'speaker': 'ALICE'}], source))
        self.assertEqual(['ALICE'], attested_new_speakers([{'speaker': 'ALICE'}], set(), source))
        self.assertFalse(is_attested_name('SMILED', source))

    def test_no_source_and_explicit_cast_keep_existing_contract(self):
        self.assertTrue(is_attested_name('CAST NAME', None))
        frozen = [{'type': 'SPOKEN', 'text': 'Hello.'}]
        self.assertTrue(validate_attribution(frozen, [{'n': 0, 'speaker': 'THE STRANGER'}],
            source_text='“Hello,” she said.', known_names={'THE STRANGER'})['passed'])

    def test_title_only_evidence_does_not_attest_a_once_written_full_name(self):
        filler = 'The road continued through the forest. ' * 200
        for title in ('King', 'Captain', 'Doctor', 'Dr.', 'Professor', 'Father'):
            with self.subTest(title=title):
                source = filler + (title + ' Henry spoke. ') * 10 + title + ' Mordred arrived.'
                name = (title + ' Mordred').upper()
                self.assertFalse(is_attested_name(name, source))
                self.assertEqual([], build_roster([{'speaker': name}], source))
                self.assertEqual([], attested_new_speakers([{'speaker': name}], set(), source))

    def test_titled_name_can_use_its_attested_personal_component(self):
        source = 'The road continued through the forest. ' * 200 + 'King Arthur arrived. ' + 'Arthur spoke. ' * 5
        self.assertTrue(is_attested_name('KING ARTHUR', source))
        self.assertEqual(['KING ARTHUR'], build_roster([{'speaker': 'KING ARTHUR'}], source))

    def test_long_book_thresholds_and_once_full_name_fallback_are_preserved(self):
        source = 'The road continued through the forest. ' * 200 + 'Ian Fairytale arrived. ' + 'Ian spoke. ' * 5 + 'Twice spoke. Twice left.'
        self.assertTrue(is_attested_name('IAN FAIRYTALE', source))
        self.assertTrue(is_attested_name('TWICE', source))
        self.assertEqual([], build_roster([{'speaker': 'TWICE'}], source))
        self.assertFalse(is_attested_name('IAN INVENTED', source))

    def test_exact_short_source_boundary_and_rejection_message(self):
        for length in (4999, 5000):
            source = 'Alice spoke. ' + 'x' * (length - len('Alice spoke. '))
            with self.subTest(length=length):
                self.assertEqual(length < 5000, is_attested_name('ALICE', source))
                frozen = [{'type': 'SPOKEN', 'text': 'Hello.'}]
                report = validate_attribution(frozen, [{'n': 0, 'speaker': 'INVENTED'}], source_text=source)
                self.assertFalse(report['passed'])
                self.assertIn('least once' if length < 5000 else 'least twice', report['findings'][0]['message'])
