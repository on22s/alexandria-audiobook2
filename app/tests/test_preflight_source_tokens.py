import copy
import unittest
from unittest.mock import patch
import script_preflight as p


class PreflightSourceTokenTests(unittest.TestCase):
    def test_audit_normalizes_full_source_once_and_preserves_inputs(self):
        texts = ['The captain raised the lantern before dawn.',
                 'The sailor lowered the anchor beyond noon.']
        entries = [{'speaker': 'NARRATOR', 'text': text} for text in texts]
        source = '\n'.join(texts) + '\nA quiet ending after the journey.'
        original = copy.deepcopy(entries)
        with patch.object(p, '_normalize', wraps=p._normalize) as normalize:
            p.audit_script(entries, source)
        self.assertEqual(1, sum(call.args[0] == source for call in normalize.call_args_list))
        self.assertEqual(original, entries)

    def test_direct_near_default_reuses_normalized_source_for_exact_pass(self):
        texts = ['The captain raised the lantern before dawn.',
                 'The captain raised his lantern before dawn.']
        source = texts[0] + ' A quiet ending after the journey.'
        with patch.object(p, '_normalize', wraps=p._normalize) as normalize:
            p.find_adjacent_near_duplicate_entries(texts, source)
        self.assertEqual(1, sum(call.args[0] == source for call in normalize.call_args_list))

    def test_token_views_are_immutable_and_keep_existing_unicode_rules(self):
        text = 'Straße _MYSELF_  日本語  café\n ＡＢＣ—hello!'
        tokens = p.get_normalized_word_tokens(text)
        self.assertEqual(('strasse', 'myself', '日本語', 'café', 'ａｂｃ', 'hello'), tokens)
        self.assertEqual(' '.join(tokens), p._normalize_words(text))
        first = 'The captain raised the lantern before dawn.'
        second = 'The sailor lowered the anchor beyond noon.'
        entries = [{'speaker': 'NARRATOR', 'text': value} for value in (first, second, first, second)]
        original = copy.deepcopy(entries)
        once = first + ' ' + second
        twice = once + ' ' + once
        report1 = p.audit_script(entries, once)
        report2 = p.audit_script(entries, twice)
        report3 = p.audit_script(entries, once)
        self.assertEqual(report1, report3)
        row1 = next(row for row in report1['findings'] if row['code'] == 'adjacent_duplicate_block')
        row2 = next(row for row in report2['findings'] if row['code'] == 'adjacent_duplicate_block')
        self.assertEqual('blocking', row1['severity'])
        self.assertEqual('manual_review', row2['severity'])
        self.assertEqual(original, entries)
