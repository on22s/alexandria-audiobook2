import contextlib
import io
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import alexandria_alignment as alignment
import alexandria_compare as compare


class ComparePrescanReuseTests(unittest.TestCase):
    entries = [{'text': text} for text in (
        'lunar silver harbor quiet lantern drifts',
        'velvet copper meadow amber falcon rests',
        'crystal ocean forest purple river bends')]

    def run_main(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            source = root / 'book.txt'
            source.write_text(' '.join(row['text'] for row in self.entries))
            metadata = root / 'metadata.jsonl'
            metadata.write_text(''.join(json.dumps(row) + '\n' for row in self.entries))
            output = root / 'corrected.jsonl'
            with patch.object(sys, 'argv', ['compare', '--jsonl', str(metadata), '--source', str(source), '--output', str(output), '--no-auto-anchor', '--threshold', '.9']), patch.object(alignment, 'find_best_match', wraps=alignment.find_best_match) as pre, patch.object(compare, 'find_best_match', wraps=compare.find_best_match) as live, contextlib.redirect_stdout(io.StringIO()):
                compare.main()
            rows = compare.load_jsonl(output)
            self.assertEqual(self.entries, rows)
            self.assertFalse(compare.review_log_path(str(output)).exists())
            log = []  # Existing auto-keep decisions use checkpoint journal, not manual review log.
            return rows, pre.call_count + live.call_count, log

    def test_real_cli_prescan_and_review_align_each_entry_once(self):
        _, calls, _ = self.run_main()
        self.assertEqual(3, calls)

    def run_review(self, change=None, manual=False):
        source = alignment.to_words(' '.join(row['text'] for row in self.entries))
        prescan = alignment.get_alignment_quality_prescan(self.entries, source, 0, threshold=.9)
        entries = [dict(row) for row in self.entries]
        threshold = .9
        nouns = frozenset()
        cursor = 0
        if change == 'source':
            source.append('trailer')
        elif change == 'threshold':
            threshold = .8
        elif change == 'nouns':
            nouns = frozenset({'lunar'})
        elif change == 'cursor':
            cursor = 6
        elif change == 'text':
            entries[0]['text'] = entries[0]['text'].replace('lunar', 'cosmic')
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            metadata = root / 'metadata.jsonl'
            metadata.write_text(''.join(json.dumps(row) + '\n' for row in entries))
            output = root / 'out.jsonl'
            decisions = {}
            with patch.object(compare, 'find_best_match', wraps=compare.find_best_match) as live, patch('builtins.input', return_value='k'), contextlib.redirect_stdout(io.StringIO()):
                compare.run(entries, source, source, decisions, cursor, threshold, manual,
                    str(metadata), str(output), root / 'log.jsonl',
                    proper_nouns=nouns, alignment_prescan=prescan)
            self.assertEqual(entries, compare.load_jsonl(output))
            return live.call_count

    def test_changed_source_threshold_and_nouns_recompute(self):
        for change in ('source', 'threshold', 'nouns'):
            with self.subTest(change=change):
                self.assertEqual(3, self.run_review(change))

    def test_cursor_and_entry_changes_recompute_and_manual_review_clears_reuse(self):
        self.assertEqual(1, self.run_review('cursor'))
        self.assertEqual(1, self.run_review('text'))
        self.assertEqual(2, self.run_review(manual=True))

    def test_shared_recovery_preserves_wide_full_and_no_match_rules(self):
        words = ['one', 'two', 'three', 'four', 'five']
        source = ['pad'] * 6 + words
        cases = [((6, 11, .7), (0, 0, 0), (False, False)),
                 ((0, 5, .1), (6, 11, .9), (False, True)),
                 ((0, 5, .1), (0, 5, .1), (True, False))]
        for wide, anchor, expected in cases:
            with self.subTest(expected=expected), patch.object(alignment, 'find_best_match', return_value=(0, 5, .1)), patch.object(alignment, 'realign', return_value=wide), patch.object(alignment, 'find_anchor_position', return_value=anchor), patch.object(alignment, 'trim_span_to_alignment', return_value=(6, 11)):
                match = alignment.get_alignment_match(words, source, 0, .9)
                self.assertEqual(expected, (match.no_source_match, match.reanchored))
                if match.reanchored:
                    self.assertEqual((6, 11, 1), match[:3])
