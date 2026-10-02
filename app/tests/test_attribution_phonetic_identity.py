import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import attribution_accuracy as accuracy
from experiments.scoring import same_speaker


class PhoneticIdentityTests(unittest.TestCase):
    def test_key_collisions_are_not_identity_evidence(self):
        for expected, actual in (('MIKA', 'MIKO'), ('AKIRA', 'AKARA'), ('ALICE', 'ALICA')):
            with self.subTest(pair=(expected, actual)):
                self.assertEqual(accuracy.romaji_key(expected), accuracy.romaji_key(actual))
                self.assertFalse(accuracy.same_person_phonetic(expected, actual, []))
                self.assertFalse(same_speaker(expected, actual, phonetic=True))
        self.assertFalse(accuracy.same_person_phonetic('田中A', '山田A', []))
        self.assertFalse(accuracy.same_person_phonetic('MIKA', '', []))

    def test_declared_variants_preserve_separate_exact_and_phonetic_metrics(self):
        groups = accuracy.get_phonetic_alias_groups(accuracy.load_gold())
        gold = {'entries': [{'id': 'f', 'entry_index': 0, 'line': 'A line.',
                             'expected_speaker': 'RUDEUS'}], 'phonetic_aliases': [list(g) for g in groups]}
        for variant in ('RUDIUS', 'RUDIEUS', 'RUDUEUS'):
            with self.subTest(variant=variant):
                rows = accuracy.score_run([{'speaker': variant, 'text': 'A line.'}], gold)
                stats = accuracy.summarize(rows)
                self.assertEqual(0, stats['correct'])
                self.assertEqual(1, stats['correct_phonetic'])
                self.assertTrue(same_speaker('RUDEUS', variant, phonetic=True, phonetic_groups=groups))
                self.assertFalse(same_speaker('RUDEUS', variant, phonetic=False, phonetic_groups=groups))
        self.assertTrue(accuracy.same_person_phonetic('ALMANFI', 'ARUMANFI', [], groups))
        self.assertFalse(accuracy.same_person_phonetic('RUDEUS', 'RUDIUS', []))
        self.assertTrue(accuracy.same_person_phonetic('RUDEUS', 'RUDI', [{'RUDEUS', 'RUDI'}]))

    def test_gold_fixtures_keep_answers_and_aliases_with_consistent_variant_declarations(self):
        root = Path(accuracy.__file__).parent / 'fixtures'
        for name in ('attribution_gold.json', 'attribution_gold_random.json',
                     'attribution_gold_mushoku16.json', 'attribution_gold_mushoku16_provisional.json'):
            with self.subTest(fixture=name):
                def unique_keys(pairs):
                    result = {}
                    for key, value in pairs:
                        self.assertNotIn(key, result, f'duplicate fixture key: {key}')
                        result[key] = value
                    return result
                gold = json.loads((root / name).read_text(), object_pairs_hook=unique_keys)
                self.assertEqual('mushoku16', gold['book'])
                self.assertTrue(accuracy.same_person_phonetic('RUDEUS', 'RUDIUS', [],
                                                            accuracy.get_phonetic_alias_groups(gold)))

    def test_real_stored_prediction_uses_documented_variant_without_changing_primary_score(self):
        root = Path(accuracy.__file__).parent.parent
        artifact = root / 'ab_test_runtime/experiments/closed_set__mushoku16__mistralai__magistral-small__local-llamacpp.json'
        row = next(r for r in json.loads(artifact.read_text())['rows']
                   if r['id'] == 'mushoku16-01051' and r['arm'] == 'closed-6')
        gold = accuracy.load_gold(str(root / 'app/fixtures/attribution_gold_random.json'))
        item = next(r for r in gold['entries'] if r['id'] == row['id'])
        self.assertEqual('RUDEUS', item['expected_speaker'])
        self.assertEqual(item['line'], row['line'])
        self.assertEqual('RUDIUS', row['predicted'])
        results = accuracy.score_run([{'text': row['line'], 'speaker': row['predicted']}],
                                     dict(gold, entries=[item]))
        self.assertEqual((False, True), (results[0]['correct'], results[0]['correct_phonetic']))

    def test_native_cli_does_not_report_collision_as_correct_phonetic_attribution(self):
        worker = Path(accuracy.__file__).resolve()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); gold = root / 'gold.json'; run = root / 'run.json'
            gold.write_text(json.dumps({'entries': [{'id': 'f', 'entry_index': 0,
                'line': 'A line.', 'expected_speaker': 'MIKA'}]}))
            run.write_text(json.dumps({'named': [{'speaker': 'MIKO', 'text': 'A line.'}]}))
            before = (gold.read_bytes(), run.read_bytes())
            result = subprocess.run([sys.executable, str(worker), str(run), '--gold', str(gold)],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertIn('correct : 0/1 (0.0%)', result.stdout)
            self.assertNotIn('phonetic:', result.stdout)
            self.assertEqual(before, (gold.read_bytes(), run.read_bytes()))
