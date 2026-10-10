"""Sentence-position capitals must not manufacture a cast (#1047)."""
import unittest

from pass_quality import is_attested_name, validate_attribution
from three_pass_generate import build_roster, attested_new_speakers


class SentenceInitialSpeakerNamesTest(unittest.TestCase):
    def assert_shared_gates(self, label, source, accepted):
        self.assertEqual(accepted, is_attested_name(label, source))
        report = validate_attribution(
            [{'type': 'SPOKEN', 'text': 'Hello.'}],
            [{'n': 0, 'speaker': label}], source_text=source)
        self.assertEqual(accepted, report['passed'], report)
        expected = [label] if accepted else []
        self.assertEqual(expected, build_roster([{'speaker': label}], source))
        self.assertEqual(expected, attested_new_speakers([{'speaker': label}], set(), source))

    def test_issue_reproduction_short_and_long_sources(self):
        source = ' '.join(
            f'However, the rain kept falling on day {i}. Mara looked up. '
            '“Ahh… not again,” Mara said. Still, the road was long. '
            f'Three… two… one. The bell rang {i} times.'
            for i in range(6))
        for book in (source, source + 'filler word here. ' * 400):
            for label in ('HOWEVER', 'HOWEVER…', 'STILL', 'AHH…', 'THE',
                          'THREE… TWO…', 'RAIN'):
                with self.subTest(label=label, length=len(book)):
                    self.assert_shared_gates(label, book, False)
            self.assert_shared_gates('MARA', book, True)

    def test_quoted_openings_and_paragraphs_are_weak_evidence(self):
        for phrase, label in (('“However we must go,” Mara said.', 'HOWEVER'),
                              ('"Still we must go," Mara said.', 'STILL'),
                              ('Ahh… not again.', 'AHH…'),
                              ('Three… two… one.', 'THREE… TWO…')):
            source = ('\n' + phrase + '\n') * 400
            with self.subTest(label=label):
                self.assert_shared_gates(label, source, False)

    def test_pronouns_and_articles_are_not_reporting_subject_names(self):
        for label, phrase in (("HE", "He said hello."),
                              ("SHE", "She asked why."),
                              ("THE", "The road was long.")):
            with self.subTest(label=label):
                self.assert_shared_gates(label, (phrase + " ") * 400, False)

    def test_ambiguous_real_names_require_name_context(self):
        for label, sentence in (
                ('STILL', 'Still said, “Hello.”'),
                ('THREE', '“Hello,” said Three.'),
                ('HOPE', 'Hope walked to the door.'),
                ('ROSE', 'Rose opened the window.'),
                ('BRI-CHAN', 'Bri-chan drew his sword.'),
                ('MARA', 'Mara looked up.')):
            with self.subTest(label=label):
                self.assert_shared_gates(label, (sentence + ' ') * 400, True)
        self.assert_shared_gates('STILL', 'We waited for Still at the door. ' * 400, True)

    def test_ratio_and_threshold_still_apply_to_ambiguous_names(self):
        source = 'Still said hello. ' + 'filler word here. ' * 400
        self.assertFalse(is_attested_name('STILL', source))
        source += 'Still said goodbye. '
        self.assertTrue(is_attested_name('STILL', source))
        self.assertEqual([], build_roster([{'speaker': 'STILL'}], source))
        source += 'we still waited. ' * 3
        self.assertFalse(is_attested_name('STILL', source))

    def test_full_name_fallback_cannot_use_weak_first_component(self):
        source = ('However, the rain fell. ' * 400) + 'However Smith arrived.'
        self.assertFalse(is_attested_name('HOWEVER SMITH', source))

    def test_explicit_cast_remains_authoritative(self):
        report = validate_attribution(
            [{'type': 'SPOKEN', 'text': 'Hello.'}],
            [{'n': 0, 'speaker': 'STILL'}], source_text='Still, it rained.',
            known_names={'STILL'})
        self.assertTrue(report['passed'], report)
