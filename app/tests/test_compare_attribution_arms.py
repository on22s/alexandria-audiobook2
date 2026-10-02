import unittest

from compare_attribution_arms import find_disagreements, sample_disagreements


class FindDisagreementsTest(unittest.TestCase):

    def test_matching_arms_have_no_disagreements(self):
        arm_a = [{"speaker": "ARARAGI", "text": "Hi"},
                 {"speaker": "HACHIKUJI", "text": "Bye"}]
        self.assertEqual(find_disagreements(arm_a, list(arm_a)), [])

    def test_differing_speaker_is_reported(self):
        arm_a = [{"speaker": "ARARAGI", "text": "Hi"}]
        arm_b = [{"speaker": "HANEKAWA", "text": "Hi"}]
        found = find_disagreements(arm_a, arm_b)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["index"], 0)
        self.assertEqual(found[0]["arm_a"], "ARARAGI")
        self.assertEqual(found[0]["arm_b"], "HANEKAWA")
        self.assertEqual(found[0]["text"], "Hi")

    def test_empty_arm_yields_no_disagreements(self):
        # Previously raised on any length mismatch, which made the tool useless
        # against real arms: segmentation is not deterministic.
        self.assertEqual(find_disagreements([{"speaker": "A", "text": "x"}], []), [])

    def test_null_entries_are_skipped(self):
        arm_a = [None, {"speaker": "ARARAGI", "text": "Hi"}]
        arm_b = [None, {"speaker": "HANEKAWA", "text": "Hi"}]
        found = find_disagreements(arm_a, arm_b)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["index"], 1)


class SampleDisagreementsTest(unittest.TestCase):

    def test_sample_is_deterministic_for_a_seed(self):
        rows = [{"index": i, "arm_a": "A", "arm_b": "B", "text": str(i)}
                for i in range(200)]
        first = sample_disagreements(rows, size=50, seed=7)
        second = sample_disagreements(rows, size=50, seed=7)
        self.assertEqual(first, second)
        self.assertEqual(len(first), 50)

    def test_sample_smaller_than_size_returns_all(self):
        rows = [{"index": 0, "arm_a": "A", "arm_b": "B", "text": "x"}]
        self.assertEqual(len(sample_disagreements(rows, size=50, seed=7)), 1)


class TextAlignmentTest(unittest.TestCase):
    """Segmentation is not deterministic: two identical runs of the same book
    produced 1,995 and 2,036 entries. Index-based comparison raised outright,
    so arms could not be compared at all. Alignment has to be on text."""

    def test_arms_of_different_length_align(self):
        arm_a = [{"speaker": "ARARAGI", "text": "Hello there."},
                 {"speaker": "HANEKAWA", "text": "Good morning."},
                 {"speaker": "ARARAGI", "text": "Goodbye."}]
        # arm_b split the middle line into two entries.
        arm_b = [{"speaker": "ARARAGI", "text": "Hello there."},
                 {"speaker": "HANEKAWA", "text": "Good"},
                 {"speaker": "HANEKAWA", "text": "morning."},
                 {"speaker": "SENJOGAHARA", "text": "Goodbye."}]
        rows = find_disagreements(arm_a, arm_b)
        texts = [r["text"] for r in rows]
        self.assertIn("Goodbye.", texts)
        self.assertNotIn("Hello there.", texts)

    def test_identical_arms_of_equal_length_have_no_disagreements(self):
        arm = [{"speaker": "ARARAGI", "text": "Hi"},
               {"speaker": "HACHIKUJI", "text": "Bye"}]
        self.assertEqual(find_disagreements(arm, list(arm)), [])

    def test_whitespace_differences_do_not_count_as_disagreement(self):
        arm_a = [{"speaker": "ARARAGI", "text": "Hello  there."}]
        arm_b = [{"speaker": "ARARAGI", "text": "Hello there."}]
        self.assertEqual(find_disagreements(arm_a, arm_b), [])

    def test_alignment_reports_coverage(self):
        from compare_attribution_arms import align_arms
        arm_a = [{"speaker": "A", "text": "one"}, {"speaker": "B", "text": "two"}]
        arm_b = [{"speaker": "A", "text": "one"}, {"speaker": "C", "text": "three"}]
        pairs, coverage = align_arms(arm_a, arm_b)
        self.assertEqual(len(pairs), 1)
        self.assertLess(coverage, 1.0)
        self.assertGreater(coverage, 0.0)


if __name__ == "__main__":
    unittest.main()


class ComparisonArtifactSafetyTests(unittest.TestCase):
    def test_ambiguous_and_blank_lines_do_not_create_false_disagreements(self):
        from compare_attribution_arms import align_arms
        left = [{'text': 'Sorry.', 'speaker': 'ANN'},
                {'text': 'Unique middle.', 'speaker': 'ANN'},
                {'text': 'Sorry.', 'speaker': 'BOB'},
                {'text': '  ', 'speaker': 'ANN'},
                {'text': 'Unique ending.', 'speaker': 'ANN'}]
        right = [{'text': 'Sorry.', 'speaker': 'BOB'},
                 {'text': 'Unique middle.', 'speaker': 'ANN'},
                 {'text': 'Sorry.', 'speaker': 'ANN'},
                 {'text': '', 'speaker': 'BOB'},
                 {'text': 'Unique ending.', 'speaker': 'BOB'}]
        pairs, coverage = align_arms(left, right)
        self.assertEqual([1, 4], [p[0] for p in pairs])
        self.assertEqual(2 / 5, coverage)
        found = find_disagreements(left, right)
        self.assertEqual([(4, 'Unique ending.')], [(r['index'], r['text']) for r in found])
        shorter = [right[0], right[1], right[4]]
        pairs, coverage = align_arms(left, shorter)
        self.assertEqual([1, 4], [p[0] for p in pairs])
        self.assertEqual(2 / 5, coverage)
        self.assertEqual(2 / 5, align_arms(shorter, left)[1])

    def test_interrupted_comparison_save_preserves_existing_human_artifact(self):
        import json, tempfile, sys
        from pathlib import Path
        from unittest.mock import patch
        import compare_attribution_arms as compare
        import utils
        with tempfile.TemporaryDirectory() as tmp:
            left, right, output = [Path(tmp) / name for name in ('a.json', 'b.json', 'out.json')]
            left.write_text(json.dumps([{'speaker': 'ANN', 'text': 'Hi there.'}]))
            right.write_text(json.dumps([{'speaker': 'BOB', 'text': 'Hi there.'}]))
            original = b'{"checked": "Keep previous scoring"}'
            output.write_bytes(original)
            argv = ['compare_attribution_arms.py', str(left), str(right), '--output', str(output)]
            def interrupted(_value, handle, **_kwargs):
                handle.write('{"partial":')
                raise OSError('disk failure')
            with patch.object(sys, 'argv', argv), patch.object(utils.json, 'dump', side_effect=interrupted):
                with self.assertRaises(OSError):
                    compare.main()
            self.assertEqual(original, output.read_bytes())
            self.assertEqual(['a.json', 'b.json', 'out.json'], sorted(p.name for p in Path(tmp).iterdir()))
            with patch.object(sys, 'argv', argv):
                compare.main()
            result = json.loads(output.read_text())
            self.assertEqual(1, result['aligned'])
            self.assertEqual(1, result['disagreement_count'])
            self.assertEqual('ANN', result['sample'][0]['arm_a'])
            self.assertEqual('BOB', result['sample'][0]['arm_b'])

    def test_cli_negative_sample_size_is_rejected_before_opening_inputs(self):
        import sys, io
        from unittest.mock import patch
        from contextlib import redirect_stderr
        import compare_attribution_arms as compare
        errors = io.StringIO()
        with patch.object(sys, 'argv', ['compare_attribution_arms.py', 'missing-a.json', 'missing-b.json', '--size=-1']), \
             patch('builtins.open', side_effect=AssertionError('invalid size must be rejected before reads')), redirect_stderr(errors):
            with self.assertRaises(SystemExit) as caught:
                compare.main()
        self.assertEqual(2, caught.exception.code)
        self.assertIn('--size must be nonnegative', errors.getvalue())
        for rows in ([], [{'index': 1}]):
            with self.assertRaises(ValueError):
                sample_disagreements(rows, -1)
        self.assertEqual([], sample_disagreements([{'index': 1}], 0))
