import copy
import hashlib
import io
import json
from pathlib import Path
import sys
import tempfile
from contextlib import redirect_stdout
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

import numpy as np
import soundfile as sf
from tests.test_voice_dataset_quality import quality
from tests.test_lora_evaluation import evaluation


class FiniteAudioMetricsTests(unittest.TestCase):
    def wav(self, samples):
        stream = io.BytesIO()
        sf.write(stream, samples, 16000, format='WAV', subtype='FLOAT')
        return stream.getvalue()

    def test_both_scorers_reject_real_nonfinite_float_wav_before_mono_average(self):
        for bad in (float('nan'), float('inf'), -float('inf')):
            for stereo in (False, True):
                with self.subTest(bad=bad, stereo=stereo), tempfile.TemporaryDirectory() as tmp:
                    samples = np.full((16000, 2) if stereo else 16000, .1, dtype=np.float32)
                    samples[17] = [bad, -bad] if stereo else bad
                    payload = self.wav(samples)
                    path = Path(tmp, 'probe.wav')
                    path.write_bytes(payload)
                    with self.assertRaisesRegex(ValueError, 'non-finite'):
                        quality.get_clip_metrics(payload)
                    with self.assertRaisesRegex(ValueError, 'non-finite'):
                        evaluation.get_audio_metrics(str(path))
        payload = self.wav(np.full(16000, .1, dtype=np.float32))
        metrics, _ = quality.get_clip_metrics(payload)
        self.assertAlmostEqual(.1, metrics['rms'], places=6)
        self.assertEqual(0., metrics['silence_ratio'])

    def test_dataset_cli_rebuilds_invalid_cache_then_reuses_unreadable_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            narrator = root / 'reader'
            narrator.mkdir()
            archive = narrator / 'dataset.zip'
            samples = np.full(16000, .1, dtype=np.float32)
            samples[7] = np.nan
            with zipfile.ZipFile(archive, 'w') as handle:
                handle.writestr('train/clip.wav', self.wav(samples))
            output = root / '_quality'
            output.mkdir()
            fingerprint = quality.get_file_fingerprint(archive)
            old = quality.audit_zip(archive, fingerprint)
            old['clips'] = [{'path': 'train/clip.wav', 'metrics': {'rms': float('nan')},
                             'warnings': [], 'pcm_sha256': 'old'}]
            report = output / (hashlib.sha256(b'reader/dataset.zip').hexdigest()[:16] + '.json')
            report.write_text(json.dumps(old))
            argv = ['audit_voice_datasets.py', '--zips2', str(root)]
            with patch.object(sys, 'argv', argv), redirect_stdout(io.StringIO()), \
                 patch.object(quality, 'audit_zip', wraps=quality.audit_zip) as audit:
                self.assertEqual(0, quality.main())
                audit.assert_called_once()
                saved = json.loads(report.read_text())
                self.assertEqual(['unreadable'], saved['clips'][0]['warnings'])
                self.assertIn('non-finite', saved['clips'][0]['error'])
                self.assertNotIn('metrics', saved['clips'][0])
                summary = json.loads((output / 'summary.json').read_text())
                self.assertEqual(1, summary['warning_clip_count'])
                audit.reset_mock()
                self.assertEqual(0, quality.main())
                audit.assert_not_called()
                self.assertEqual(saved, json.loads(report.read_text()))

    def test_lora_cli_fails_invalid_probe_or_similarity_preserving_candidates_and_rebuilds_cache(self):
        for bad_probe, similarity in ((True, .9), (False, float('nan')),
                                      (False, float('inf')), (False, -float('inf'))):
            with self.subTest(bad_probe=bad_probe, similarity=similarity), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                adapter = root / 'voice'
                adapter.mkdir()
                (adapter / 'adapter_model.safetensors').write_bytes(b'checkpoint')
                (adapter / 'ref_sample.wav').write_bytes(self.wav(np.full(16000, .1)))
                candidate = adapter / 'candidates' / 'epoch_001'
                candidate.mkdir(parents=True)
                sentinel = candidate / 'adapter_model.safetensors'
                sentinel.write_bytes(b'keep candidate')
                manifest, config = root / 'manifest.json', root / 'config.json'
                manifest.write_text('[{"id":"voice"}]')
                config.write_text('{}')
                samples = np.full(16000, .1, dtype=np.float32)
                if bad_probe:
                    samples[7] = np.nan
                payload = self.wav(samples)
                engine = Mock()
                def generate(output_path, **kwargs):
                    Path(output_path).write_bytes(payload)
                    return True
                engine.generate_voice.side_effect = generate
                package = ModuleType('speechbrain'); package.__path__ = []
                inference = ModuleType('speechbrain.inference'); inference.__path__ = []
                speaker = ModuleType('speechbrain.inference.speaker')
                speaker.EncoderClassifier = SimpleNamespace(from_hparams=Mock(return_value=SimpleNamespace(eval=Mock())))
                modules = {'torch': ModuleType('torch'), 'speechbrain': package,
                           'speechbrain.inference': inference, 'speechbrain.inference.speaker': speaker}
                argv = ['evaluate_lora.py', '--manifest', str(manifest), '--models-dir', str(root),
                        '--config', str(config), '--device', 'cpu']
                with patch.dict(sys.modules, modules), patch.object(sys, 'argv', argv), \
                     patch.object(evaluation, 'TTSEngine', return_value=engine), \
                     patch.object(evaluation, 'get_speaker_similarity', return_value=similarity), \
                     patch.object(evaluation, 'apply_evaluation_seed'), \
                     patch.object(evaluation, 'cleanup_candidates') as cleanup, redirect_stdout(io.StringIO()):
                    self.assertEqual(1, evaluation.main())
                    cleanup.assert_not_called()
                status = json.loads(manifest.read_text())[0]['evaluation']
                self.assertEqual('failed', status['status'])
                self.assertIn('non-finite', status['warnings'][0])
                self.assertEqual(b'keep candidate', sentinel.read_bytes())
                self.assertFalse((adapter / 'evaluation.json').exists())
                # A real finite replacement provides trustworthy evidence; NaN cache must not reuse it.
                payload = self.wav(np.full(16000, .1))
                with patch.object(evaluation, 'get_speaker_similarity', return_value=.9), \
                     patch.object(evaluation, 'apply_evaluation_seed'):
                    valid = evaluation.evaluate_adapter({'id': 'voice'}, tmp, engine, object(), 'cpu')
                self.assertTrue(evaluation.is_complete_evaluation(valid, str(adapter)))
                invalid = copy.deepcopy(valid)
                invalid['probes'][0]['metrics']['rms'] = float('nan')
                self.assertFalse(evaluation.is_complete_evaluation(invalid, str(adapter)))
                self.assertTrue(evaluation.is_complete_evaluation(valid, str(adapter)))


    def test_lora_main_rebuilds_nan_cache_and_skips_finite_replacement(self):
        from tests.test_lora_evaluation import EvaluationCacheShapeTests
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            adapter, valid, engine_type = EvaluationCacheShapeTests().make_valid_evidence(root)
            invalid = copy.deepcopy(valid)
            invalid['probes'][0]['metrics']['speaker_similarity'] = float('nan')
            cache = adapter / 'evaluation.json'
            cache.write_text(json.dumps(invalid))
            before = cache.read_bytes()
            manifest, config = root / 'manifest.json', root / 'config.json'
            manifest.write_text('[{"id":"voice"}]')
            config.write_text('{}')
            package = ModuleType('speechbrain'); package.__path__ = []
            inference = ModuleType('speechbrain.inference'); inference.__path__ = []
            speaker = ModuleType('speechbrain.inference.speaker')
            speaker.EncoderClassifier = SimpleNamespace(from_hparams=Mock(return_value=SimpleNamespace(eval=Mock())))
            modules = {'torch': ModuleType('torch'), 'speechbrain': package,
                       'speechbrain.inference': inference, 'speechbrain.inference.speaker': speaker}
            argv = ['evaluate_lora.py', '--manifest', str(manifest), '--models-dir', str(root),
                    '--config', str(config), '--device', 'cpu']
            with patch.dict(sys.modules, modules), patch.object(sys, 'argv', argv), \
                 patch.object(evaluation, 'TTSEngine', engine_type), \
                 patch.object(evaluation, 'get_speaker_similarity', return_value=.9), \
                 patch.object(evaluation, 'apply_evaluation_seed'), \
                 patch.object(evaluation, 'evaluate_adapter', wraps=evaluation.evaluate_adapter) as evaluate, \
                 redirect_stdout(io.StringIO()):
                self.assertEqual(0, evaluation.main())
                evaluate.assert_called_once()
                self.assertNotEqual(before, cache.read_bytes())
                replacement = json.loads(cache.read_text())
                self.assertTrue(evaluation.is_complete_evaluation(replacement, str(adapter)))
                self.assertEqual('pass', json.loads(manifest.read_text())[0]['evaluation']['status'])
                evaluate.reset_mock()
                self.assertEqual(0, evaluation.main())
                evaluate.assert_not_called()
                self.assertEqual(replacement, json.loads(cache.read_text()))

    def test_finite_pcm_overflow_is_rejected_in_computed_metrics(self):
        samples = np.full(16000, np.finfo(np.float32).max / 2, dtype=np.float32)
        payload = self.wav(samples)
        with tempfile.TemporaryDirectory() as tmp, np.errstate(over='ignore', invalid='ignore'):
            path = Path(tmp, 'probe.wav')
            path.write_bytes(payload)
            with self.assertRaisesRegex(ValueError, 'non-finite'):
                quality.get_clip_metrics(payload)
            with self.assertRaisesRegex(ValueError, 'non-finite'):
                evaluation.get_audio_metrics(str(path))
