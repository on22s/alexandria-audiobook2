"""Known bidi override failures and legitimate multilingual format characters."""
import copy
import unittest

from persona_validation import validate_persona_payload
from script_preflight import audit_script, audit_unicode_text
from three_pass_generate import prepare_source_text
from utils import get_unsafe_text_controls


class UnicodeFormatPolicyTests(unittest.TestCase):
    def test_explicit_overrides_are_reported_even_when_already_in_source(self):
        for char in ('\u202d', '\u202e'):
            with self.subTest(char=repr(char)):
                text = 'Visible ' + char + 'abc.txt'
                report = audit_unicode_text(text, text)
                self.assertEqual([f'U+{ord(char):04X}'], report['unsafe_controls'])
                self.assertEqual([f'U+{ord(char):04X}'], get_unsafe_text_controls(text))

    def test_joiners_marks_and_isolates_are_described_without_blanket_rejection(self):
        texts = ('👩\u200d🚀', 'می\u200cروم', '\u061cسلام', '\u200fשלום',
                 '\u2067سلام\u2069', '\u202bسلام\u202c', 'soft\u00adhyphen', 'zero\u200bspace')
        formats = (['U+200D'], ['U+200C'], ['U+061C'], ['U+200F'],
                   ['U+2067', 'U+2069'], ['U+202B', 'U+202C'], ['U+00AD'], ['U+200B'])
        for text, expected in zip(texts, formats):
            with self.subTest(text=repr(text)):
                report = audit_unicode_text(text, text)
                self.assertEqual([], report['unsafe_controls'])
                self.assertEqual(expected, report['format_controls'])
                payload = {'description': text, 'ref_text': text}
                self.assertEqual(payload, validate_persona_payload(payload))
                entry = {'text': text, 'speaker': 'NARRATOR', 'instruct': 'neutral'}
                audited = audit_script([entry], text)
                self.assertNotIn('unsafe_unicode_character', {f['code'] for f in audited['findings']})

    def test_each_script_field_reports_overrides_without_mutating_entries(self):
        for field in ('text', 'speaker', 'instruct'):
            with self.subTest(field=field):
                entry = {'text': 'A known sentence.', 'speaker': 'ALICE', 'instruct': 'neutral'}
                entry[field] += '\u202e'
                original = copy.deepcopy(entry)
                report = audit_script([entry], entry['text'])
                findings = [f for f in report['findings'] if f['code'] == 'unsafe_unicode_character']
                self.assertEqual(1, len(findings))
                self.assertEqual('blocking', findings[0]['severity'])
                self.assertEqual([1], findings[0]['entry_numbers'])
                if field != 'text':
                    self.assertEqual(field, findings[0]['details']['field'])
                self.assertEqual(original, entry)

    def test_source_gate_and_persona_boundary_share_override_rejection(self):
        for control in ('\u202d', '\u202e'):
            with self.subTest(control=repr(control)):
                with self.assertRaisesRegex(ValueError, 'unsafe control'):
                    prepare_source_text('A known sentence.' + control)
                for field in ('description', 'ref_text'):
                    payload = {'description': 'A warm voice.', 'ref_text': 'Known words.'}
                    payload[field] += control
                    before = copy.deepcopy(payload)
                    with self.assertRaisesRegex(ValueError, 'unsafe controls'):
                        validate_persona_payload(payload)
                    self.assertEqual(before, payload)
