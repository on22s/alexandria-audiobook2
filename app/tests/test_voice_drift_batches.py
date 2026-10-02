"""Bounded real PCM staging and numeric result ownership, without ECAPA."""
import copy
import json
import os
from pathlib import Path
import struct
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
import wave

import voice_drift

if os.environ.get('VOICE_DRIFT_BATCH_BASELINE'):
    baseline = Path(os.environ['VOICE_DRIFT_BATCH_BASELINE'])
    exec(compile(baseline.read_text(), str(baseline), 'exec'), voice_drift.__dict__)


class VoiceDriftBatchTests(unittest.TestCase):
    def prepare(self, root, count=65):
        chunks = []
        for index in range(count):
            path = root / f'clip{index}.wav'
            with wave.open(str(path), 'wb') as handle:
                handle.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
                handle.writeframes(struct.pack('<h', index + 1) * 400)
            chunks.append({'uid': f'u{index}', 'speaker': 'A', 'status': 'done', 'audio_path': path.name})
        return chunks, {'A': {'type': 'clone', 'ref_audio': 'clip0.wav'}}

    def test_batches_bound_pcm_files_and_keep_owner_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunks, config = self.prepare(root)
            original = copy.deepcopy((chunks, config))
            decoded_paths = []
            batches = []
            real_decode = voice_drift._decode_to_wav
            def decode(src, folder, stem):
                path = real_decode(src, folder, stem)
                decoded_paths.append(Path(path))
                return path
            previous = []
            def score(pairs, python):
                self.assertTrue(all(not path.exists() for path in previous))
                directory = Path(pairs[0][0]).parent
                files = list(directory.glob('*.wav'))
                self.assertLessEqual(len(files), 64)  # two unique files per target at most
                self.assertLessEqual(len(pairs), 32)
                batches.append(len(pairs))
                scores = []
                for target, reference in pairs:
                    with wave.open(target, 'rb') as handle:
                        value = struct.unpack('<h', handle.readframes(1))[0]
                    self.assertTrue(Path(reference).exists())
                    scores.append(value / 100)
                previous[:] = files
                return scores, None
            with patch.object(voice_drift, '_decode_to_wav', side_effect=decode):
                report = voice_drift.check_voice_drift(chunks, config, str(root), sys.executable, 0.45,
                    resolve_asset_path=lambda name: str(root / name), score_pairs=score)
            self.assertEqual([32, 32, 1], batches)
            self.assertIsNone(report['error'])
            self.assertEqual([f'u{i}' for i in range(65)], [row['uid'] for row in report['results']])
            self.assertEqual([round((i + 1) / 100, 4) for i in range(65)], [row['score'] for row in report['results']])
            self.assertEqual(44, sum(row['flagged'] for row in report['results']))
            self.assertTrue(all(not path.exists() for path in decoded_paths))
            self.assertEqual(original, (chunks, config))

    def test_later_batch_error_returns_no_partial_measurement_and_cleans_files(self):
        for failure in ('error', 'short'):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                chunks, config = self.prepare(root)
                calls = []
                paths = []
                def score(pairs, python):
                    calls.append(len(pairs))
                    paths.extend(Path(path) for pair in pairs for path in pair)
                    if len(calls) == 2:
                        return ([], 'worker failed') if failure == 'error' else ([0.8], None)
                    return [0.8] * len(pairs), None
                report = voice_drift.check_voice_drift(chunks, config, str(root), sys.executable, 0.45,
                    resolve_asset_path=lambda name: str(root / name), score_pairs=score)
                self.assertEqual([32, 32], calls)
                self.assertEqual([], report['results'])
                self.assertIn('not measured', report['error'])
                self.assertTrue(all(not path.exists() for path in paths))

    def test_unselected_first_chunk_stays_fallback_reference_across_batches(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            chunks, _ = self.prepare(root, 66)
            scores = []
            def score(pairs, python):
                for target, reference in pairs:
                    with wave.open(reference, 'rb') as handle:
                        self.assertEqual(1, struct.unpack('<h', handle.readframes(1))[0])
                scores.append(len(pairs))
                return [0.8] * len(pairs), None
            report = voice_drift.check_voice_drift(chunks, {'A': {'type': 'custom'}}, str(root), sys.executable, 0.45,
                indices=list(range(1, 66)), score_pairs=score)
            self.assertEqual([32, 32, 1], scores)
            self.assertEqual(65, len(report['results']))
            self.assertTrue(all(row['reference'] == 'chunk:u0' for row in report['results']))

    def test_default_scorer_preserves_one_total_deadline_across_subprocesses(self):
        from experiments import library_voice_fidelity
        for expired in (False, True):
            with self.subTest(expired=expired), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                chunks, config = self.prepare(root)
                caps = []
                def worker(argv, **options):
                    caps.append(options['timeout'])
                    pairs = json.loads(options['input'])
                    return SimpleNamespace(returncode=0, stderr='', stdout=json.dumps([0.8] * len(pairs)))
                ticks = [10, 10, 20, 3611 if expired else 21]
                with patch.object(library_voice_fidelity.subprocess, 'run', side_effect=worker), \
                     patch.object(voice_drift, 'time', SimpleNamespace(monotonic=unittest.mock.Mock(side_effect=ticks))):
                    report = voice_drift.check_voice_drift(chunks, config, str(root), sys.executable, 0.45,
                        resolve_asset_path=lambda name: str(root / name))
                self.assertEqual([3600, 3590] if expired else [3600, 3590, 3589], caps)
                if expired:
                    self.assertEqual([], report['results'])
                    self.assertIn('deadline exceeded', report['error'])
                else:
                    self.assertEqual(65, len(report['results']))
