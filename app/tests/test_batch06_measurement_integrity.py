"""Real input/output regressions for the audit's measurement-integrity batch."""
import contextlib
import csv
from decimal import Decimal, localcontext
import io
import json
import math
from pathlib import Path
import random
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import wave
import zipfile

import numpy as np
from experiments import build_tight_dataset, chinese_attribution, pdnc_balanced_trainset, pdnc_trainset
from experiments import roster_order_bias, shipped_book_lexicon_coverage


def pcm(value):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as audio:
        audio.setparams((1, 2, 24000, 0, 'NONE', 'not compressed'))
        audio.writeframes(value.to_bytes(2, 'little') * 240)
    return buffer.getvalue()


def write_csv(path, fields, rows):
    with path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader(); writer.writerows(rows)


class Batch06MeasurementIntegrityTests(unittest.TestCase):
    def test_reference_audio_and_text_follow_each_selected_arm_centroid(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); source = root / 'source.zip'
            with zipfile.ZipFile(source, 'w') as archive:
                for index in range(4): archive.writestr(f'audio/{index}.wav', pcm(index + 1))
            vectors = [np.array([1., 0.]), np.array([0., 1.]), np.array([0., 1.]), np.array([-1., 0.])]
            pool = [('volume', str(source), f'{i}.wav', vector) for i, vector in enumerate(vectors)]
            texts = {('volume', f'{i}.wav'): f'Clip {i}' for i in range(4)}
            for arm in ('control', 'tight'):
                build_tight_dataset.write_arm(str(root / arm), pool[:3], pool[3:], texts)
                self.assertEqual(pcm(2), (root / arm / 'ref.wav').read_bytes())
                self.assertEqual('Clip 1', (root / arm / 'ref_text.txt').read_text())
                train = [json.loads(row) for row in (root / arm / 'train/metadata.jsonl').read_text().splitlines()]
                self.assertEqual(['Clip 0', 'Clip 1', 'Clip 2'], [row['text'] for row in train])
                self.assertEqual(pcm(4), (root / arm / 'val/val_0000.wav').read_bytes())

    def test_large_exact_sign_test_matches_integer_probability_and_small_controls(self):
        for first, second in ((1100, 0), (600, 500), (0, 0), (4, 0), (3, 2), (5000, 5000)):
            with self.subTest(first=first, second=second), localcontext() as context:
                context.prec = 80
                total = first + second
                term = tail = 1
                for k in range(1, min(first, second) + 1):
                    term = term * (total - k + 1) // k
                    tail += term
                expected = min(Decimal(1), Decimal(2) * Decimal(tail) / Decimal(2) ** total)
                actual = roster_order_bias.sign_test(first, second)
                self.assertTrue(math.isfinite(actual))
                self.assertAlmostEqual(float(expected), actual, delta=1e-9)
                self.assertEqual(actual, roster_order_bias.sign_test(second, first))

    def test_both_pdnc_builders_exclude_pseudo_targets_and_rosters(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            names = ['Alice', '_group', '_unknowable', '_narr', '_future_marker']
            characters = [{'Main Name': name, 'Aliases': '[]', 'Category': 'major'} for name in names]
            quotes = [{'quoteText': 'Hello', 'speaker': name, 'quoteByteSpans': '[(0, 5)]'} for name in names]
            fields = ['Main Name', 'Aliases', 'Category']; quote_fields = ['quoteText', 'speaker', 'quoteByteSpans']
            write_csv(root / 'character_info.csv', fields, characters)
            write_csv(root / 'Novel_chars.csv', fields, characters)
            write_csv(root / 'quotation_info.csv', quote_fields, quotes)
            write_csv(root / 'Novel_quotes.csv', quote_fields, quotes)
            (root / 'novel_text.txt').write_text('Hello world'); (root / 'Novel.txt').write_text('Hello world')
            for rows in (pdnc_balanced_trainset.read_rows(str(root), 20),
                         pdnc_trainset.convert(str(root), 'Novel', 50, 20, random.Random(1))):
                self.assertEqual(['ALICE'], [row['teacher'] for row in rows])
                self.assertEqual(['ALICE'], rows[0]['roster'])
                self.assertEqual('Hello', rows[0]['line'])

    def run_chinese(self, root, *options):
        source = root / 'source.json'; output = root / 'result.json'; calls = []; scales = []
        source.write_text(json.dumps([{'text': ['甲说话。'], 'target_idx': 0,
                                      'roleid2idx': {'2': '[C0]'}, 'speaker_ids': ['2']}]))
        def complete(**kwargs):
            calls.append(kwargs)
            return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content='[C0]'))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=complete)))
        printed = io.StringIO()
        with patch.object(sys, 'argv', ['chinese', '--dataset', str(source), '--out', str(output), *options]), \
                patch.object(chinese_attribution, 'OpenAI', return_value=client), \
                patch.object(chinese_attribution, 'set_scale', side_effect=lambda url, value: scales.append(value)), \
                contextlib.redirect_stdout(printed):
            chinese_attribution.main()
        return json.loads(output.read_text()), calls, scales, printed.getvalue()

    def test_no_scale_measures_one_as_served_arm_without_adapter_effect_claims(self):
        with tempfile.TemporaryDirectory() as temporary:
            for options in (('--no-scale',), ('--no-scale', '--scales', '.25', '.75')):
                document, calls, scales, printed = self.run_chinese(Path(temporary), *options)
                self.assertEqual(1, len(calls)); self.assertEqual([], scales)
                self.assertEqual({'as_served'}, set(document['results']))
                self.assertEqual(1, document['results']['as_served']['correct'])
                self.assertNotIn('adapter effect', printed); self.assertNotIn('adapter off', printed)
                self.assertIn('as served', printed)

    def test_scaled_chinese_comparison_keeps_real_interventions_and_result_keys(self):
        with tempfile.TemporaryDirectory() as temporary:
            document, calls, scales, printed = self.run_chinese(Path(temporary))
            self.assertEqual([0., 1.], scales); self.assertEqual(2, len(calls))
            self.assertEqual({'0.0', '1.0'}, set(document['results']))
            self.assertIn('adapter effect', printed)

    def test_current_empty_third_state_is_available_and_legacy_absence_is_distinct(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); scripts = root / 'scripts'; scripts.mkdir()
            (scripts / 'Book.json').write_text('[{"text":"samurai"}]')
            candidates = root / 'candidates.json'; output = root / 'coverage.json'
            for state in ({'plain_already_works': []}, {}, {'plain_already_works': ['samurai']}):
                candidates.write_text(json.dumps(state)); printed = io.StringIO()
                with patch.object(sys, 'argv', ['coverage', '--scripts', str(scripts), '--candidates', str(candidates),
                                              '--out', str(output), '--triage', str(root / 'missing')]), \
                        patch.object(shipped_book_lexicon_coverage, 'discover', return_value={'samurai': {'Book'}}), \
                        contextlib.redirect_stdout(printed):
                    shipped_book_lexicon_coverage.main()
                document = json.loads(output.read_text())
                self.assertEqual('plain_already_works' in state, document['third_state_available'])
                self.assertEqual('plain_already_works' not in state, 'Regenerate it' in printed.getvalue())
                self.assertEqual(int(bool(state.get('plain_already_works'))), document['present_plain_already_works'])
                self.assertEqual(0 if state.get('plain_already_works') else 1, document['present_in_neither_state'])

    def test_luar_cli_uses_fixture_aliases_for_both_measured_arms(self):
        import torch
        from experiments import luar_character_selector
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); inputs = root / 'inputs.json'; baseline = root / 'baseline.json'; output = root / 'luar.json'
            inputs.write_text(json.dumps({'aliases': [['MRS. MARCH', 'MOTHER']], 'entries': [
                {'id': '1', 'quote_type': 'Explicit', 'line': 'A profile quote.', 'expected_speaker': 'MRS. MARCH'},
                {'id': '2', 'quote_type': 'Implicit', 'line': 'An implicit quote.', 'expected_speaker': 'MRS. MARCH'}]}))
            baseline.write_text(json.dumps({'rows': [
                {'arm': 'baseline', 'id': 'book:1', 'predicted': 'MOTHER', 'correct': True},
                {'arm': 'baseline', 'id': 'book:2', 'predicted': 'UNKNOWN', 'correct': False}]}))
            class Tokenizer:
                @staticmethod
                def from_pretrained(*args, **kwargs): return Tokenizer()
                def __call__(self, quotes, **kwargs):
                    return SimpleNamespace(input_ids=torch.ones((len(quotes), 2), dtype=torch.long),
                                           attention_mask=torch.ones((len(quotes), 2), dtype=torch.long))
            class Model:
                @staticmethod
                def from_pretrained(*args, **kwargs): return Model()
                def to(self, device):
                    self.assert_cpu = device == 'cpu'; return self
                def eval(self): return self
                def __call__(self, inputs, mask):
                    if not self.assert_cpu: raise AssertionError('Fixture must stay on CPU')
                    return torch.tensor([[1., 0.]])
            for candidates, gained in ((['MRS. MARCH'], 1), (['BOB'], 0)):
                source = json.loads(baseline.read_text())
                source['rows'][1]['candidates'] = candidates
                baseline.write_text(json.dumps(source))
                with patch.dict(sys.modules, {'transformers': SimpleNamespace(AutoModel=Model, AutoTokenizer=Tokenizer)}), \
                        patch.object(sys, 'argv', ['luar', '--baseline', str(baseline), '--inputs', str(inputs),
                                                  '--out', str(output), '--device', 'cpu']), \
                        patch.object(luar_character_selector, 'provenance', return_value={'fixture': True}), \
                        contextlib.redirect_stdout(io.StringIO()):
                    luar_character_selector.main()
                document = json.loads(output.read_text())
                self.assertEqual(gained, document['summary']['gained'])
                self.assertEqual((1, 1 + gained), (document['summary']['baseline_correct'], document['summary']['luar_correct']))
                self.assertEqual('MOTHER' if gained else 'UNKNOWN', document['rows'][1]['predicted'])
                self.assertEqual('luar_profile_override' if gained else 'baseline_default', document['rows'][1]['reason'])
                self.assertEqual('baseline_default', document['rows'][0]['reason'])
                self.assertTrue(document['rows'][0]['source_baseline_correct'])

    def test_stop_cli_records_all_requested_failures_and_cannot_pass_a_survivor_subset(self):
        from experiments import generation, verify_adapter_stops
        import tts
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary); build = root / 'build.json'; config = root / 'config.json'; output = root / 'gate.json'
            rows = [{'id': str(i), 'text': f'Line {i}', 'seconds': .01} for i in range(5)]
            build.write_text(json.dumps({'test': rows})); config.write_text('{}')
            for failures in (set(range(4)), set(range(5)), set()):
                attempted = []
                def render(engine, text, instruct, speaker, voice_config, entry, wav):
                    number = int(text.split()[-1]); attempted.append(number)
                    if number in failures: raise generation.GenerationFailed('fixture generation unavailable')
                    Path(wav).write_bytes(pcm(3))
                with patch.object(sys, 'argv', ['stop', '--build', str(build), '--adapter', str(root / 'adapter'),
                                              '--config', str(config), '--out', str(output), '--out-dir', str(root / 'audio')]), \
                        patch.object(tts, 'TTSEngine', return_value=object()), \
                        patch.object(generation, 'render', side_effect=render), \
                        contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as stopped:
                        verify_adapter_stops.main()
                self.assertEqual(3 if failures else 0, stopped.exception.code)
                self.assertEqual(list(range(5)), attempted)
                document = json.loads(output.read_text())
                self.assertIs(not failures, document['passed'])
                if failures: self.assertNotIn('PASS', document['verdict'])
                self.assertEqual((5, 5 - len(failures), len(failures)),
                                 (document['requested_count'], document['generated_count'], document['failed_count']))
                self.assertEqual({str(i) for i in failures}, {row['id'] for row in document['failed_lines']})
                self.assertEqual(5 - len(failures), len(document['lines']))
                if not failures: self.assertAlmostEqual(1., document['worst_ratio'])
