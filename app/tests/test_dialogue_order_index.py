"""Exact indexed matching preserves cursor fallback and raw source attribution."""
import copy
import random
import unittest
from unittest.mock import patch
import dialogue_spans as dialogue


class ObservedSource(str):
    def __new__(cls, value):
        instance = super().__new__(cls, value)
        instance.searches = []
        instance.traversals = 0
        return instance

    def find(self, needle, start=0):
        self.searches.append(start)
        return super().find(needle, start)

    def __iter__(self):
        self.traversals += 1
        return super().__iter__()


class DialogueOrderIndexTests(unittest.TestCase):
    def test_reversed_entries_share_one_scan_after_first_order_slip(self):
        texts = [f'Line {i:04d} ' + 'long distinctive words '*12 + '.' for i in range(120)]
        source = '\n\n'.join('“'+text+'”' for text in texts)
        entries = [{'speaker':'ALICE','text':text} for text in reversed(texts)]
        before = copy.deepcopy(entries)
        normalized, offsets = dialogue._normalize_with_offsets(source)
        observed = ObservedSource(normalized)
        with patch.object(dialogue, '_normalize_with_offsets', return_value=(observed, offsets)):
            marked = dialogue.mark_entries(iter(entries), source, 'paired_quotes', speaker_names=iter(['ALICE']))
        self.assertEqual(2, len(observed.searches))
        self.assertEqual(1, observed.traversals)
        for row, expected in zip(marked, reversed(texts)):
            start, end = row['source_span']
            self.assertEqual(expected, source[start:end])
            self.assertTrue(row['spoken'])
        self.assertEqual(before, entries)

    def test_excess_duplicates_are_unlocated_and_unused_backward_matches_remain_valid(self):
        source = 'He said "Oh." Then she said "Oh." and left.'
        entries = [{'text': 'Oh.'} for _ in range(4)]
        entries[-1].update(spoken=True, source_span=[0, 3], source_speaker='OLD')
        before = copy.deepcopy(entries)
        marked = dialogue.mark_entries(entries, source, 'paired_quotes')
        self.assertEqual([[9, 12], [29, 32]], [row['source_span'] for row in marked[:2]])
        for row in marked[2:]:
            self.assertNotIn('source_span', row)
            self.assertNotIn('spoken', row)
            self.assertNotIn('source_speaker', row)
        self.assertEqual(before, entries)
        source = '"First." Then "Oh." Then "Last."'
        marked = dialogue.mark_entries([{'text': text} for text in ('Last.', 'First.', 'Oh.')], source, 'paired_quotes')
        self.assertEqual(['Last.', 'First.', 'Oh.'], [source[a:b] for a, b in (row['source_span'] for row in marked)])
        self.assertTrue(all(row['spoken'] for row in marked))

    def test_ordered_entries_do_not_build_an_index(self):
        source = '“First line.” Then “Second line.”'
        with patch.object(dialogue, 'get_source_match_positions', side_effect=AssertionError('ordered source indexed')):
            marked = dialogue.mark_entries([{'text':'First line.'},{'text':'Second line.'}], source, 'paired_quotes')
        self.assertEqual(2, len(marked))
        self.assertTrue(all(row['spoken'] for row in marked))

    def test_overlap_suffix_repeated_and_unicode_positions_match_native_search(self):
        rng = random.Random(414)
        cases = [('aaaa café 日本語', ['a','aa','aaa','aaaa','café','日本語','missing','']),
                 ('ushers his hers she', ['he','she','hers','his','s'])]
        for _ in range(180):
            source = ''.join(rng.choice('abc 日本語') for _ in range(150))
            needles = [source[start:start+rng.randrange(1,12)] for start in rng.sample(range(140),12)]
            needles.extend(['zzmissing', needles[0]])
            cases.append((source, needles))
        for source, needles in cases:
            actual = dialogue.get_source_match_positions(source, iter(needles))
            expected = {}
            for needle in needles:
                if not needle:
                    continue
                positions = []; start = 0
                while (found := source.find(needle, start)) != -1:
                    positions.append(found); start = found+1
                expected[needle] = positions
            self.assertEqual(expected, actual)

    def test_cursor_fallback_unmatched_and_raw_spans_match_original_rule(self):
        source = 'ALICE “Oh.”\n\nNarration words.\n\nALICE “Hello\t wide\nworld.”\n\nALICE “Oh.”\n\nALICE “Extra one.”\n\nALICE “Extra two.”\n\nALICE “Extra three.”'
        entries = [{'speaker':'ALICE','text':text} for text in
                   ('Oh.','Oh.','Hello wide world.','Oh.','Invented missing.','Narration words.','Oh.')]
        # A direct oracle retains backward fallback for unused occurrences only.
        normalized, offsets = dialogue._normalize_with_offsets(source)
        cursor = 0; expected = []; used = {}
        for row in entries:
            needle = dialogue._normalize(row['text']); consumed = used.setdefault(needle, set())
            found = normalized.find(needle, cursor)
            while found in consumed:
                found = normalized.find(needle, found + 1)
            if found == -1:
                found = normalized.find(needle)
                while found in consumed:
                    found = normalized.find(needle, found + 1)
            if found != -1:
                consumed.add(found)
                cursor = found + len(needle)
                expected.append([offsets[found], offsets[cursor-1]+1])
            else:
                expected.append(None)
        actual = dialogue.mark_entries(entries, source, 'paired_quotes', speaker_names=['ALICE'])
        self.assertEqual(expected, [row.get('source_span') for row in actual])
        self.assertNotIn('spoken', actual[4])
        self.assertFalse(actual[5]['spoken'])
        self.assertEqual('ALICE', actual[2]['source_speaker'])
