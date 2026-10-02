import json
import os
from pathlib import Path
import re
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
import soundfile as sf
from tests import test_preparer_run_state as support

p = support.preparer


class PreparerBatchEtaTests(unittest.TestCase):
    def run_chunks(self, count):
        labels = 'Quartz Velvet Banana Ocean Dragon Puzzle Citrus Falcon Meadow Anchor Copper Willow Beacon Jungle Ripple Sphinx Tomato Zebra Glacier Ivory Lotus Maples Nebula Whisky'.split()
        words = [{'word': labels[i] + '.', 'start': i * 1.2, 'end': (i + 1) * 1.2,
                  'confidence': 1, 'speaker': 'ONLY'} for i in range(count)]
        samples = np.sin(np.arange(count * 28800) * .05).astype('float32') * .1
        now = [0.0]
        def snr(audio):
            now[0] += 2.0
            return 80
        def complete(**request):
            prompt = request['messages'][1]['content']
            return {'choices': [{'message': {'content': json.dumps(re.findall(r'Annotate this segment:\n([^\n]+)', prompt))}}]}
        old = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with patch.object(p, 'ensure_run_manifest'), patch.object(p, '_load_llm', return_value=SimpleNamespace(create_chat_completion=complete)), patch.object(p, 'log_gpu_stats') as gpu, patch.object(p, 'clear_vram'), patch.object(p, '_calculate_chunk_snr', side_effect=snr), patch.object(p.time, 'monotonic', side_effect=lambda: now[0]), self.assertLogs('alexandria', 'DEBUG') as logs:
                    rows = p.annotate_chunks(words, 'fixture.gguf', 1, samples,
                        batch_size=3, min_chunk_duration=1, run_identity={'test': 'eta'})
                saved = [json.loads(line) for line in Path('dataset_temp/metadata.jsonl').read_text().splitlines()]
                self.assertEqual([word['word'] for word in words], [row['text'] for row in rows])
                for row in saved:
                    values, rate = sf.read(Path('dataset_temp', row['audio_filepath']))
                    self.assertEqual(24000, rate)
                    self.assertAlmostEqual(1.2, len(values) / rate)
                    row.pop('wav_path', None)
                return [line for line in logs.output if '↳ Progress:' in line], saved, gpu.call_args_list
            finally:
                os.chdir(old)

    def test_completed_boundary_and_average_cover_entire_batch(self):
        lines, _, gpu = self.run_chunks(12)
        self.assertEqual(1, len(lines))
        self.assertIn('Progress: 12 chunks', lines[0])
        self.assertIn('Avg: 2.0s/chunk', lines[0])
        self.assertTrue(any('annotation segment 12 ' in call.args[0] for call in gpu))

    def test_crossed_boundaries_and_exhausted_heuristic_do_not_claim_completion(self):
        lines, _, _ = self.run_chunks(24)
        self.assertEqual(2, len(lines))
        for line, completed in zip(lines, (12, 21)):
            self.assertIn(f'Progress: {completed} chunks', line)
            self.assertIn('Avg: 2.0s/chunk', line)
            self.assertIn('ETA: unknown', line)
