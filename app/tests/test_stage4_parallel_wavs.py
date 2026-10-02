"""Native WAV decoding, with barriers verifying bounded overlap and stable errors."""
import copy
import hashlib
import importlib.util
import os
from pathlib import Path
import threading
import time
import unittest
from unittest.mock import patch
import wave

from tests.test_stage4_checkpoint_runner import Stage4CheckpointRunnerTest as Fixture
from tests.test_stage4_checkpoint_runner import runner, summarize

if os.environ.get('STAGE4_VALIDATOR_SOURCE'):
    original = runner
    spec = importlib.util.spec_from_file_location('saved_stage4', os.environ['STAGE4_VALIDATOR_SOURCE'])
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    runner.REPO, runner.APP = original.REPO, original.APP


class Stage4ParallelWavTests(unittest.TestCase):
    setUp = Fixture.setUp
    tearDown = Fixture.tearDown
    _wav = Fixture._wav
    _write = Fixture._write

    def artifact(self):
        doc = Fixture._artifact(self)
        template = copy.deepcopy(doc['rows'])
        doc['provenance']['args']['adapters'] = ['adapter-' + str(i) for i in range(4)]
        doc['rows'] = []
        for adapter in doc['provenance']['args']['adapters']:
            for row in template:
                next_row = dict(row, adapter=adapter, wav=self._wav(adapter + row['class']))
                doc['rows'].append(next_row)
        # Each file extends beyond the decoder's 65,536-frame read boundary.
        for row in doc['rows']:
            with wave.open(str(Path(runner.REPO) / row['wav']), 'wb') as handle:
                handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(16000)
                handle.writeframes(b'\x01\x00' * 131100)
        doc['summary'] = summarize(doc['rows'])
        return doc

    def test_four_decoders_overlap_without_changing_audio_or_artifact(self):
        doc = self.artifact(); path = self._write(doc)
        files = [Path(runner.REPO) / row['wav'] for row in doc['rows']]
        before = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files + [Path(path)]}
        actual = runner._read_wav_fully
        lock, barrier = threading.Lock(), threading.Barrier(4)
        active = peak = 0
        calls = []
        def decode(wav):
            nonlocal active, peak
            with lock:
                active += 1; peak = max(peak, active); calls.append(wav)
            try:
                barrier.wait(timeout=1)
                actual(wav)
            finally:
                with lock:
                    active -= 1
        with patch.object(runner, '_provenance_harness_matches', return_value=True), \
             patch.object(runner, '_read_wav_fully', side_effect=decode):
            result = runner.validate_stage4_artifact(path, len(doc['rows']))
        self.assertEqual(doc, result)
        self.assertEqual(4, peak)
        self.assertEqual(0, active)
        self.assertCountEqual([str(p) for p in files], calls)
        self.assertEqual(before, {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in files + [Path(path)]})

    def test_last_block_truncation_rejected_with_original_row_number(self):
        doc = self.artifact()
        # Header promises 131100 frames, truncation occurs after a full block.
        bad = Path(runner.REPO) / doc['rows'][6]['wav']
        with bad.open('rb+') as handle:
            handle.truncate(44 + 65536 * 2 + 10)
        before = bad.read_bytes()
        with patch.object(runner, '_provenance_harness_matches', return_value=True):
            with self.assertRaisesRegex(runner.ArtifactValidationError, 'row 6 WAV is not fully decodable'):
                runner.validate_stage4_artifact(self._write(doc), len(doc['rows']))
        self.assertEqual(before, bad.read_bytes())

    def test_failure_selection_is_by_row_order_and_all_readers_are_joined(self):
        doc = self.artifact(); actual = runner._read_wav_fully
        files = [str(Path(runner.REPO) / row['wav']) for row in doc['rows']]
        completed = []
        def decode(wav):
            try:
                if wav == files[1]:
                    time.sleep(.04)
                    raise RuntimeError('earlier-row failure')
                if wav == files[6]:
                    raise RuntimeError('later-row failure')
                actual(wav)
            finally:
                completed.append(wav)
        with patch.object(runner, '_provenance_harness_matches', return_value=True), \
             patch.object(runner, '_read_wav_fully', side_effect=decode):
            with self.assertRaisesRegex(runner.ArtifactValidationError,
                    'row 1 WAV is not fully decodable: earlier-row failure'):
                runner.validate_stage4_artifact(self._write(doc), len(doc['rows']))
        self.assertCountEqual(files, completed)
