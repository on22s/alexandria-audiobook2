"""Interval sweeping must retain strict quotation endpoints."""
import random
import re
import unittest
from unittest.mock import patch
from dialogue_spans import spoken_spans
from generate_script import _safe_cut_points


class SafeCutSweepTests(unittest.TestCase):
    def test_span_endpoints_are_safe_and_interior_newlines_are_not(self):
        with patch('dialogue_spans.spoken_spans', return_value=[(2, 6), (8, 12)]):
            self.assertEqual([1, 2, 6, 7, 8, 12, 13, 14], _safe_cut_points('\n' * 15, 'paired_quotes'))

    def test_cuts_match_exhaustive_span_predicate_across_dialogue_conventions(self):
        rng = random.Random(699)
        for convention in ('paired_quotes', 'dash_lines', 'label_lines'):
            for _ in range(60):
                lines = []
                for i in range(rng.randint(1, 30)):
                    speech = ' '.join(rng.choice(['Yes.', 'No!', 'Maybe?', 'word', 'é']) for _ in range(rng.randint(1, 12)))
                    lines.append({'paired_quotes': f'Narration. “{speech}” More.', 'dash_lines': f'—{speech}', 'label_lines': f'Alice: {speech}'}[convention])
                text = '\n'.join(lines)
                spans = spoken_spans(text, convention)
                points = {m.start() for m in re.finditer(r'\n', text)}
                points.update(m.end() for m in re.finditer(r"[.!?][\"'\u201d\u2019)]*(?=\s)", text))
                expected = sorted(o for o in points if 0 < o < len(text) and not any(a < o < b for a, b in spans))
                self.assertEqual(expected, _safe_cut_points(text, convention))
