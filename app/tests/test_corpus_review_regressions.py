"""CPU provider fixtures and native volume exports reproduce the reviewed failures."""
import contextlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as NS
import unittest
from unittest.mock import patch

import numpy as np
import torch
from tests.test_preparer_run_state import preparer

ROOT = Path(__file__).resolve().parents[2]
spec = importlib.util.spec_from_file_location('corpus_review_report', ROOT / 'corpus_run_report.py')
report = importlib.util.module_from_spec(spec)
spec.loader.exec_module(report)


class CorpusReviewRegressions(unittest.TestCase):
    def test_actual_ctc_tail_owns_each_overlap_word_once(self):
        for seconds in (30, 40, 59, 100):
            with self.subTest(seconds=seconds):
                positions = list(range(2, seconds - 1, 2))
                state = {}
                class Processor:
                    def __call__(self, audio, **kwargs):
                        state['start'] = float(audio[0])
                        state['end'] = state['start'] + len(audio) / 16000
                        return {'input_values': torch.from_numpy(audio).unsqueeze(0)}
                    def batch_decode(self, ids, **kwargs):
                        offsets = [{'word': str(position),
                                    'start_offset': round((position - state['start']) * 50),
                                    'end_offset': round((position + .2 - state['start']) * 50)}
                                   for position in positions
                                   if state['start'] <= position and position + .2 < state['end']]
                        return NS(word_offsets=[offsets])
                class Model:
                    config = NS(inputs_to_logits_ratio=320)
                    def to(self, device):
                        self.assert_cpu = device == 'cpu'
                        if not self.assert_cpu:
                            raise AssertionError('fixture must stay on CPU')
                        return self
                    def eval(self):
                        pass
                    def __call__(self, **kwargs):
                        return NS(logits=torch.zeros((1, 1500, 2)))
                providers = NS(Wav2Vec2Processor=NS(from_pretrained=lambda *a, **k: Processor()),
                               Wav2Vec2ForCTC=NS(from_pretrained=lambda *a, **k: Model()))
                with patch.dict(sys.modules, {'transformers': providers}), \
                     patch.object(preparer, 'TRANSFORMERS_WHISPER_AVAILABLE', True), \
                     patch.object(preparer, 'resolve_cuda_device', return_value='cpu'), \
                     patch.object(preparer, 'clear_vram'), patch.object(preparer, 'log_gpu_stats'):
                    rows, language = preparer.transcribe_with_wav2vec2(
                        np.arange(seconds * 16000, dtype='float32') / 16000)
                self.assertEqual('en', language)
                self.assertEqual([str(position) for position in positions], [row['word'] for row in rows])

    def test_actual_corpus_dispatch_completes_and_rechecks_every_volume(self):
        from tests.test_corpus_dispatch_identity import CorpusDispatchIdentityTests
        from tests.corpus_fixture_support import WORKER
        with tempfile.TemporaryDirectory() as folder:
            base = Path(folder)
            fixture = CorpusDispatchIdentityTests()
            repo = fixture.create_repo(base)
            worker = WORKER.replace("output.write_bytes(b'dataset from '+audio.name.encode())", """
names=[output.stem+'_vol01.zip',output.stem+'_vol02.zip']
for name in names:(output.parent/name).write_bytes(b'volume '+name.encode())
write_json_atomic({'volumes':names},Path(str(output)+'.volumes.json'))
""")
            (repo / 'run_with_restart.sh').write_text('#!' + sys.executable + '\n' + worker)
            output = repo / 'output'
            result = fixture.invoke(repo, output, '--run', base)
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            index = json.loads((output / 'corpus_attempts.json').read_text())
            self.assertTrue(all(row['status'] == 'completed' for row in index['rows']))
            row = index['rows'][0]
            self.assertEqual(2, len(row['dataset_identity']['volumes']))
            name = next(iter(row['dataset_identity']['volumes']))
            (output / name).unlink()
            result = fixture.invoke(repo, output, '--aggregate', base)
            self.assertEqual(1, result.returncode)
            self.assertIn('1 pair(s) failed', (output / 'aggregated_report.md').read_text())

    def test_native_multivolume_exports_are_owned_and_all_revalidated(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            output = root / 'book.zip'
            work = root / 'dataset_temp'
            work.mkdir()
            (work / 'sample_0000.wav').write_bytes(b'fixture audio')
            entries = [{'audio_filepath': 'sample_0000.wav', 'speaker': 'UNKNOWN',
                        'speaker_labels': ['UNKNOWN'], 'duration': 10, 'text': str(i)}
                       for i in range(201)]
            previous = os.getcwd()
            try:
                os.chdir(root)
                preparer._create_zip_dataset(entries, str(output))
            finally:
                os.chdir(previous)
            self.assertFalse(output.exists())
            identity = report.get_dataset_identity(output)
            self.assertEqual({'book_vol01.zip', 'book_vol02.zip'}, set(identity['volumes']))
            self.assertEqual(identity, report.get_dataset_identity(output))
            for name in identity['volumes']:
                volume = root / name
                original = volume.read_bytes()
                volume.write_bytes(original + b'changed')
                self.assertNotEqual(identity, report.get_dataset_identity(output))
                volume.unlink()
                with self.assertRaises(OSError):
                    report.get_dataset_identity(output)
                volume.write_bytes(original)
            manifest = Path(str(output) + '.volumes.json')
            for names in ([], ['../private.zip'], ['book_vol01.zip'] * 2, ['unrelated.zip']):
                manifest.write_text(json.dumps({'volumes': names}))
                with self.assertRaises(ValueError):
                    report.get_dataset_identity(output)
