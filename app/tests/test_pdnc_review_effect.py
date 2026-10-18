"""Known answers and refusals for source-bound, aggregate-only review scoring."""
import contextlib
import csv
import hashlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from experiments import pdnc_review_effect as scorer


def gold(source, spans, speaker='Alice', quote_id='q1'):
    byte_spans = [[len(source[:start].encode('utf-8')), len(source[:end].encode('utf-8'))]
                  for start, end in spans]
    return {'quoteID': quote_id, 'quoteByteSpans': repr(byte_spans),
            'subQuotationList': repr([source[start:end] for start, end in spans]),
            'speaker': speaker}


def row(source, start=0, end=None, speaker='Alice', text=None):
    end = len(source) if end is None else end
    return {'source_span': [start, end], 'text': source[start:end] if text is None else text,
            'speaker': speaker}


class PositionScoringTests(unittest.TestCase):
    def score(self, source, records, rows, groups=()):
        return scorer.score_rows(rows, scorer.get_gold_quotes(records, source), source, groups)

    def test_substring_in_the_cannot_match_he_at_its_real_position(self):
        source = 'The. He.'
        summary, predictions = self.score(source, [gold(source, [(5, 7)])],
                                           [row(source, 0, 3, text='He')])
        self.assertEqual(0, summary['matched'])
        self.assertFalse(predictions[0]['correct'])
        self.assertEqual({'text_mismatch': 1}, summary['excluded_rows'])

    def test_repeated_quotes_use_their_own_positions(self):
        source = 'Yes. Yes.'
        records = [gold(source, [(0, 4)]), gold(source, [(5, 9)], 'Bob', 'q2')]
        summary, predictions = self.score(source, records,
                                           [row(source, 0, 4), row(source, 5, 9, 'Bob')])
        self.assertEqual(2, summary['correct'])
        self.assertEqual(['Alice', 'Bob'], [p['speaker'] for p in predictions])

    def test_missing_multipart_piece_never_scores_as_a_match(self):
        source = 'One. Then two.'
        summary, _ = self.score(source, [gold(source, [(0, 4), (10, 14)])],
                                [row(source, 0, 4)])
        self.assertEqual((0, 1, 1, 2), (summary['matched'], summary['partial'],
                         summary['covered_pieces'], summary['total_pieces']))

    def test_punctuation_only_piece_is_explicitly_unscorable(self):
        source = 'Word!'
        summary, _ = self.score(source, [gold(source, [(0, 4), (4, 5)])], [row(source)])
        self.assertEqual((0, 1, 0), (summary['matched'], summary['unscorable'], summary['correct']))

    def test_adjacent_rows_cover_piece_and_majority_counts_alphanumeric_only(self):
        source = 'A...... BBB'
        summary, predictions = self.score(source, [gold(source, [(0, len(source))], 'Bob')],
                                           [row(source, 0, 8), row(source, 8, 11, 'Bob')])
        self.assertEqual((1, 1, 0), (summary['correct'], summary['multirow'], summary['ties']))
        self.assertEqual('Bob', predictions[0]['speaker'])

    def test_missing_interior_character_makes_quote_partial(self):
        source = 'ABC'
        summary, _ = self.score(source, [gold(source, [(0, 3)])],
                                [row(source, 0, 1), row(source, 2, 3)])
        self.assertEqual((0, 2, 3), (summary['matched'], summary['covered_positions'],
                                   summary['required_positions']))

    def test_overlapping_distinct_validated_rows_refuse(self):
        with self.assertRaisesRegex(scorer.ScoringError, 'overlapping_validated_rows'):
            scorer.get_validated_rows([row('ABCDE', 0, 4), row('ABCDE', 2, 5)], 'ABCDE')

    def test_parent_span_excludes_whole_group_even_if_one_text_matches(self):
        source = 'ABCD'
        rows = [row(source), row(source, text='AB'), row(source, text='CD')]
        summary, _ = self.score(source, [gold(source, [(0, 4)])], rows)
        self.assertEqual(0, summary['matched'])
        self.assertEqual({'coarse_or_repeated_span': 3}, summary['excluded_rows'])

    def test_malformed_parent_sibling_still_excludes_the_whole_span_group(self):
        for malformed in (None, 5):
            with self.subTest(malformed=malformed):
                source = 'Yes'
                rows = [row(source), {**row(source), 'text': malformed}]
                summary, _ = self.score(source, [gold(source, [(0, 3)])], rows)
                self.assertEqual((0, 0), (summary['matched'], summary['correct']))
                self.assertEqual({'coarse_or_repeated_span': 2}, summary['excluded_rows'])

    def test_tie_uses_earlier_script_row_across_multiple_pieces(self):
        source = 'AA x BB'
        records = [gold(source, [(0, 2), (5, 7)], 'Bob')]
        summary, predictions = self.score(source, records,
                                           [row(source, 5, 7, 'Bob'), row(source, 0, 2)])
        self.assertEqual((1, 1, 1), (summary['correct'], summary['multirow'], summary['ties']))
        self.assertEqual('Bob', predictions[0]['speaker'])

    def test_native_quotes_and_whitespace_pass_but_lexical_changes_do_not(self):
        source = '“Hello,”   Alice!'
        for text, valid in [('Hello, Alice!', True), ('hello, Alice!', False),
                            ('Hello Alice!', False), ('Hello, Alice?', False),
                            ("Hello, Alice’s!", False)]:
            with self.subTest(text=text):
                summary, _ = self.score(source, [gold(source, [(0, len(source))])],
                                        [row(source, text=text)])
                self.assertEqual(int(valid), summary['matched'])

    def test_invalid_or_missing_rows_are_counted_without_guessed_positions(self):
        source = 'Yes'
        rows = [{'text': source}, row(source, text='No'), row(source), 5,
                {'text': source, 'source_span': [True, 3]}]
        # The exact row and mismatch share a parent span, so both are excluded.
        summary, _ = self.score(source, [gold(source, [(0, 3)])], rows)
        self.assertEqual(0, summary['validated_rows'])
        self.assertEqual({'coarse_or_repeated_span': 2, 'invalid_span': 1,
                          'malformed_row': 1, 'missing_span': 1}, summary['excluded_rows'])

    def test_missing_unknown_narrator_alias_and_pseudo_labels(self):
        groups = [{'ALICE', 'DR ALICE'}]
        for expected, actual, correct in [('Alice', '', 0), ('Alice', 'UNKNOWN', 0),
                                          ('Narrator', 'NARRATOR', 1),
                                          ('Alice', 'Dr. Alice', 1), ('_group', '_group', 1),
                                          ('', 'Alice', 0)]:
            with self.subTest(expected=expected, actual=actual):
                summary, _ = self.score('Yes', [gold('Yes', [(0, 3)], expected)],
                                        [row('Yes', speaker=actual)], groups)
                self.assertEqual(correct, summary['correct'])
                self.assertEqual(int(expected.startswith('_')), summary['pseudo_gold'])
                self.assertEqual(int(not expected), summary['empty_gold'])


class InputValidationTests(unittest.TestCase):
    def test_utf8_offsets_map_characters_and_refuse_mid_codepoint(self):
        source = 'é猫 says yes'
        quote = gold(source, [(1, 2)])
        parsed = scorer.get_gold_quotes([quote], source)
        self.assertEqual([(1,)], parsed[0]['pieces'])
        quote['quoteByteSpans'] = '[[1, 5]]'
        with self.assertRaises(scorer.ScoringError):
            scorer.get_gold_quotes([quote], source)

    def test_gold_ids_pieces_spans_and_text_have_strict_rejections(self):
        base = gold('ABCDE', [(0, 2)])
        cases = [{'quoteID': ''}, {'quoteByteSpans': '[[True, 2]]'},
                 {'quoteByteSpans': '[[-1, 2]]'}, {'quoteByteSpans': '[[0, 6]]'},
                 {'quoteByteSpans': '[[0, 0]]'}, {'quoteByteSpans': '[[0, 2], [0, 2]]',
                                               'subQuotationList': "['AB', 'AB']"},
                 {'quoteByteSpans': '[[0, 3], [2, 4]]', 'subQuotationList': "['ABC', 'CD']"},
                 {'subQuotationList': '[]'}, {'subQuotationList': "['ab']"},
                 {'subQuotationList': "['AB!']"}, {'quoteByteSpans': 'not a literal'}]
        for replacement in cases:
            with self.subTest(replacement=replacement), self.assertRaises(scorer.ScoringError):
                scorer.get_gold_quotes([{**base, **replacement}], 'ABCDE')
        for records in [[], [base, base]]:
            with self.assertRaises(scorer.ScoringError):
                scorer.get_gold_quotes(records, 'ABCDE')

    def test_different_gold_ids_may_overlap(self):
        source = 'ABCDE'
        self.assertEqual(2, len(scorer.get_gold_quotes(
            [gold(source, [(0, 3)]), gold(source, [(1, 4)], quote_id='q2')], source)))

    def test_source_hash_and_preparation_are_explicit_and_position_preserving(self):
        raw = b'I don t know. I don t know. I don t know.'
        prepared, _ = scorer.restore_stripped_apostrophes(raw.decode())
        self.assertNotEqual(raw.decode(), prepared)
        manifest = {'fingerprint': {'source_sha256': scorer.get_sha256(prepared.encode())}}
        self.assertEqual(prepared, scorer.get_prepared_source(raw, manifest, 'apostrophe-repair')[1])
        with self.assertRaises(scorer.ScoringError):
            scorer.get_prepared_source(raw, manifest, 'raw')
        for transformed in ['shorter', raw.decode().replace('I', 'X'), raw.decode() + '!']:
            with patch.object(scorer, 'restore_stripped_apostrophes', return_value=(transformed, [])):
                with self.assertRaises(scorer.ScoringError):
                    scorer.get_prepared_source(raw, manifest, 'apostrophe-repair')
        with self.assertRaises(UnicodeDecodeError):
            scorer.get_prepared_source(b'\xff', manifest, 'raw')

    def test_arm_names_are_nonempty_unique_and_cannot_replace_baseline(self):
        for values in [[], ['=x'], ['full='], ['full=x', 'full=y'], ['baseline=x'],
                       ['/private/path=x'], ['no_separator']]:
            with self.subTest(values=values), self.assertRaises(scorer.ScoringError):
                scorer.get_arm_inputs(values)


class PairedScoringTests(unittest.TestCase):
    def pred(self, correct=False, matched=True, speaker='Alice'):
        return {'correct': correct, 'matched': matched, 'speaker': speaker}

    def test_repairs_damages_wrong_change_and_coverage_are_separate(self):
        baseline = [self.pred(), self.pred(True), self.pred(speaker='Bob'),
                    self.pred(matched=False), self.pred(True)]
        arm = [self.pred(True), self.pred(), self.pred(speaker='Carol'),
               self.pred(True), self.pred(matched=False)]
        all_gold = scorer.get_paired_comparison(baseline, arm)
        self.assertEqual((2, 2, 1), (all_gold['repairs'], all_gold['damages'],
                                    all_gold['wrong_to_other_wrong']))
        self.assertEqual((1, 1, 1, 1), tuple(all_gold[k] for k in
                         ['coverage_gains', 'coverage_losses', 'repairs_with_new_coverage',
                          'damages_with_lost_coverage']))
        common = scorer.get_paired_comparison(baseline, arm, common_coverage=True)
        self.assertEqual((3, 1, 1), (common['quotes'], common['repairs'], common['damages']))

    def test_nine_discordances_still_flat_and_ten_can_help_or_hurt(self):
        for count, reverse, decision in [(0, False, 'flat'), (9, False, 'flat'),
                                         (10, False, 'helps'), (10, True, 'hurts')]:
            a, b = [self.pred(False)] * count, [self.pred(True)] * count
            if reverse:
                a, b = b, a
            result = scorer.get_paired_comparison(a, b)
            self.assertEqual(decision, result['decision'])
            self.assertAlmostEqual(1 if count == 0 else 2 ** (1 - count),
                                   result['exact_mcnemar_p'])
        self.assertEqual('flat', scorer.get_paired_comparison(
            [self.pred(True)] * 5 + [self.pred()] * 5,
            [self.pred()] * 5 + [self.pred(True)] * 5)['decision'])

    def test_alias_equivalent_wrong_names_do_not_become_other_wrong(self):
        result = scorer.get_paired_comparison([self.pred(speaker='Dr. Bob')],
            [self.pred(speaker='Bob')], [{'DR BOB', 'BOB'}])
        self.assertEqual(0, result['wrong_to_other_wrong'])


class AggregateCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = 'SYNTHETIC_SECRET_TEXT.'
        (self.root / 'novel_text.txt').write_bytes(self.source.encode())
        records = {'quotation_info.csv': [gold(self.source, [(0, len(self.source))],
                                               'SYNTHETIC_SECRET_NAME')],
                   'character_info.csv': [{'Main Name': 'SYNTHETIC_SECRET_NAME', 'Aliases': '[]'}]}
        for name, rows in records.items():
            with (self.root / name).open('w', encoding='utf-8', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
                writer.writeheader()
                writer.writerows(rows)
        self.manifest = self.root / 'manifest.json'
        self.manifest.write_text(json.dumps({'fingerprint': {
            'source_sha256': hashlib.sha256(self.source.encode()).hexdigest()}}))
        self.baseline = self.root / 'baseline.json'
        self.baseline.write_text(json.dumps([row(self.source, speaker='SYNTHETIC_SECRET_NAME')]))
        self.output = self.root / 'result.json'
        self.args = ['--novel', str(self.root), '--manifest', str(self.manifest),
                     '--baseline', str(self.baseline), '--arm', 'full=' + str(self.baseline),
                     '--source-profile', 'raw', '--output', str(self.output)]

    def run_cli(self, args=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = scorer.main(self.args if args is None else args)
        return result, stdout.getvalue(), stderr.getvalue()

    def test_success_is_aggregate_only_and_hash_bound_without_model_or_network(self):
        with patch('subprocess.Popen', side_effect=AssertionError('unexpected worker')):
            with patch('socket.create_connection', side_effect=AssertionError('unexpected network')):
                status, stdout, stderr = self.run_cli()
        self.assertEqual(0, status)
        self.assertEqual('', stderr)
        result = json.loads(self.output.read_text())
        self.assertEqual((1, 1), (result['baseline']['correct'], result['arms']['full']['correct']))
        self.assertEqual(hashlib.sha256(self.source.encode()).hexdigest(),
                         result['input_sha256']['raw_source'])
        self.assertEqual('offline_source_position', result['provenance']['mode'])
        self.assertEqual(result['code_sha256']['scorer'], result['provenance']['scorer_sha256'])
        self.assertEqual(hashlib.sha256(json.dumps(result['input_sha256'], sort_keys=True,
            separators=(',', ':')).encode()).hexdigest(), result['provenance']['input_fingerprint_sha256'])
        published = self.output.read_text() + stdout + stderr
        for value in [self.source, 'SYNTHETIC_SECRET_NAME', str(self.root)]:
            self.assertNotIn(value, published)

    def test_input_failure_preserves_previous_result_without_private_error_text(self):
        self.output.write_bytes(b'prior complete result')
        self.manifest.write_text('{}')
        status, stdout, stderr = self.run_cli()
        self.assertEqual((2, ''), (status, stdout))
        self.assertEqual(b'prior complete result', self.output.read_bytes())
        self.assertNotIn(str(self.root), stderr)

    def test_output_cannot_replace_any_input_or_its_symlink_or_hardlink(self):
        inputs = [self.baseline, self.manifest, *[self.root / name for name in
                  ('novel_text.txt', 'quotation_info.csv', 'character_info.csv')]]
        link = self.root / 'input-link'
        link.symlink_to(self.baseline)
        hardlink = self.root / 'input-hardlink'
        hardlink.hardlink_to(self.baseline)
        before = {p: p.read_bytes() for p in inputs}
        for output in [*inputs, link, hardlink]:
            with self.subTest(output=output.name):
                status, _, _ = self.run_cli(self.args[:-1] + [str(output)])
                self.assertEqual(2, status)
                self.assertEqual(before, {p: p.read_bytes() for p in inputs})

    def test_argument_and_io_errors_are_sanitized_and_publication_is_atomic(self):
        self.output.write_bytes(b'prior result')
        for args in [self.args + ['--unknown=SYNTHETIC_SECRET_NAME'],
                     [*self.args[:1], str(self.root / 'SYNTHETIC_SECRET_NAME'), *self.args[2:]]]:
            status, _, stderr = self.run_cli(args)
            self.assertEqual(2, status)
            self.assertNotIn('SYNTHETIC_SECRET_NAME', stderr)
            self.assertNotIn(str(self.root), stderr)
        with patch('utils.os.replace', side_effect=OSError('private error data')):
            status, _, stderr = self.run_cli()
        self.assertEqual(2, status)
        self.assertNotIn('private error data', stderr)
        self.assertEqual(b'prior result', self.output.read_bytes())
        self.assertEqual([], list(self.root.glob('.tmp_*')))


if __name__ == '__main__':
    unittest.main()
