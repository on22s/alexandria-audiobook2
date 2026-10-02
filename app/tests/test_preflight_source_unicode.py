"""One exact source classification per audit retains Unicode evidence."""
import unittest
from unittest.mock import patch
import script_preflight as preflight


class SourceUnicodeReuseTests(unittest.TestCase):
    def test_absent_and_empty_source_have_different_introduced_scripts(self):
        text = '“aБ”'
        absent = preflight.audit_unicode_text(text)
        empty = preflight.audit_unicode_text(text, '', source_scripts=frozenset())
        self.assertEqual(['CYRILLIC'], absent['introduced_scripts'])
        self.assertEqual(['CYRILLIC', 'LATIN'], empty['introduced_scripts'])
        self.assertEqual([{'text': 'aБ', 'scripts': ['CYRILLIC', 'LATIN'], 'offset': 1}], absent['mixed_script_words'])

    def test_shared_source_scripts_preserve_each_report_field(self):
        for text, source in [('Cafe\u0301\ufffd\x00', None), ('Latin', ''),
                             ('aб', 'ab'), ('日本語', '日本語'),
                             ('x\u200dy', 'xy'), ('عربى עברית', 'عربى')]:
            with self.subTest(text=text, source=source):
                baseline = preflight.audit_unicode_text(text, source)
                result = preflight.audit_unicode_text(text, source, source_scripts=preflight.get_unicode_scripts(source))
                self.assertEqual(baseline, result)

    def test_full_audit_classifies_source_once_and_does_not_keep_it_for_next_book(self):
        source = 'Unique source wording. 日本語.'
        entries = [{'speaker': 'NARRATOR', 'text': 'aБ', 'instruct': 'Read.'} for _ in range(3)]
        actual = preflight.get_unicode_scripts
        with patch.object(preflight, 'get_unicode_scripts', wraps=actual) as classify:
            first = preflight.audit_script(entries, source)
            self.assertEqual(1, sum(call.args == (source,) for call in classify.call_args_list))
            second = preflight.audit_script(entries, source + ' Б')
            self.assertEqual(1, sum(call.args == (source + ' Б',) for call in classify.call_args_list))
        first_unicode = [f for f in first['findings'] if f['code'] == 'introduced_unicode_script']
        second_unicode = [f for f in second['findings'] if f['code'] == 'introduced_unicode_script']
        self.assertTrue(first_unicode)
        self.assertFalse(second_unicode)
