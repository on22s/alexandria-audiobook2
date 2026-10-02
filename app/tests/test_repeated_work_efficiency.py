import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import compare_attribution_arms as compare
import speech_policy


class RepeatedWorkEfficiencyTests(unittest.TestCase):
    def test_actual_comparison_cli_aligns_once_and_preserves_saved_report(self):
        left = [None, {'speaker': 'A', 'text': 'Hello'},
                {'speaker': 'A', 'text': 'Repeated'}, {'speaker': 'B', 'text': 'Repeated'},
                {'speaker': 'B', 'text': 'Goodbye'}]
        right = [{'speaker': 'C', 'text': 'hello'}, {'speaker': 'A', 'text': 'Repeated'},
                 {'speaker': 'B', 'text': 'Repeated'}, {'speaker': 'B', 'text': 'Goodbye'},
                 {'speaker': 'C', 'text': 'Unmatched'}]
        pairs, coverage = compare.align_arms(left, right)
        rows = compare.find_disagreements(left, right, pairs=pairs)
        expected = dict(entries_arm_a=len(left), entries_arm_b=len(right),
                        aligned=len(pairs), alignment_coverage=round(coverage, 4),
                        comparison_eligible=False,
                        disagreement_count=len(rows), sample=compare.sample_disagreements(rows, 1, 11))
        with tempfile.TemporaryDirectory() as tmp:
            a, b, output = [Path(tmp, name) for name in ('a.json', 'b.json', 'report.json')]
            a.write_text(json.dumps(left)); b.write_text(json.dumps(right))
            with patch('sys.argv', ['compare', str(a), str(b), '--size', '1', '--seed', '11', '--output', str(output)]), \
                 patch.object(compare, 'align_arms', wraps=compare.align_arms) as align, contextlib.redirect_stdout(io.StringIO()):
                compare.main()
            self.assertEqual(expected, json.loads(output.read_text()))
            self.assertEqual(1, align.call_count)

    def test_emoji_replacement_and_count_use_one_regex_pass(self):
        for text, expected in [('ordinary text', ('ordinary text', 0)),
                               ('😀😀 hi 😀😀😀', (speech_policy.SCENE_BREAK_MARKER+'hi '+speech_policy.SCENE_BREAK_MARKER, 2)),
                               ('one 😀 emoji', ('one 😀 emoji', 0))]:
            with self.subTest(text=text):
                regex = Mock(wraps=speech_policy._EMOJI_RUN)
                with patch.object(speech_policy, '_EMOJI_RUN', regex):
                    observed = speech_policy.strip_emoji_dividers(text)
                self.assertEqual(expected, observed)
                self.assertEqual(1, len(regex.method_calls))
