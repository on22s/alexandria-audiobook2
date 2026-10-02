import hashlib
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


class PreparerBatchFlushTests(unittest.TestCase):
    def run_batches(self, invalid_batches=False, failed_chunks=(), save_failure=None):
        texts = ['One.', 'Two.', 'Three.', 'Four.', 'Five.']
        words = [{'word': text, 'start': i * 1.2, 'end': (i + 1) * 1.2,
                  'confidence': 1, 'speaker': 'ONLY'} for i, text in enumerate(texts)]
        calls = []
        def complete(**request):
            prompt = request['messages'][1]['content']
            is_batch = request['messages'][0]['content'] == p.TTS_ANNOTATION_BATCH_SYSTEM_PROMPT
            calls.append({'batch': is_batch, 'prompt': prompt})
            if is_batch:
                content = 'malformed batch' if invalid_batches else json.dumps(re.findall(r'Annotate this segment:\n([^\n]+)', prompt))
            else:
                content = prompt.split('Annotate this segment:\n', 1)[1]
                if content in failed_chunks:
                    raise RuntimeError('fixture failure')
            return {'choices': [{'message': {'content': content}}]}
        real_save = p._save_chunk_metadata
        def save(*args):
            if args[-2] == save_failure:
                raise OSError('fixture checkpoint failure')
            return real_save(*args)
        samples = np.sin(np.arange(144000) * .05).astype('float32') * .1
        old = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with patch.object(p, 'ensure_run_manifest'), patch.object(p, '_load_llm', return_value=SimpleNamespace(create_chat_completion=complete)), patch.object(p, 'log_gpu_stats'), patch.object(p, '_calculate_chunk_snr', return_value=80), patch.object(p, '_save_chunk_metadata', side_effect=save), self.assertLogs('alexandria', 'DEBUG') as logs:
                    if save_failure is not None:
                        with self.assertRaisesRegex(OSError, 'fixture checkpoint failure'):
                            p.annotate_chunks(words, 'fixture.gguf', 1, samples, min_chunk_duration=1, batch_size=3, run_identity={'test': 'flush'})
                    else:
                        returned = p.annotate_chunks(words, 'fixture.gguf', 1, samples, min_chunk_duration=1, batch_size=3, run_identity={'test': 'flush'})
                        self.assertEqual(texts, [row['text'] for row in returned])
                saved = [json.loads(line) for line in Path('dataset_temp/metadata.jsonl').read_text().splitlines()]
                wanted = 5 if save_failure is None else save_failure
                self.assertEqual(texts[:wanted], [row['text'] for row in saved])
                hashes = []
                for i, row in enumerate(saved):
                    self.assertEqual(f'sample_{i:04d}.wav', row['audio_filepath'])
                    self.assertAlmostEqual(i * 1.2, row['start'])
                    self.assertAlmostEqual((i + 1) * 1.2, row['end'])
                    values, rate = sf.read(Path('dataset_temp', row['audio_filepath']))
                    self.assertEqual(24000, rate)
                    self.assertAlmostEqual(1.2, len(values) / rate)
                    hashes.append(hashlib.sha256(values.tobytes()).hexdigest())
                    row.pop('wav_path', None)
                self.assertEqual(wanted, len(list(Path('dataset_temp').glob('sample_*.wav'))))
                return {'rows': saved, 'pcm_hashes': hashes, 'calls': calls, 'logs': logs.output}
            finally:
                os.chdir(old)

    def test_valid_full_and_multi_item_tail_keep_indices_audio_and_checkpoint(self):
        result = self.run_batches()
        self.assertEqual([True, True], [call['batch'] for call in result['calls']])
        self.assertIn('LLM annotations: 5 ok, 0 failed', '\n'.join(result['logs']))

    def test_invalid_full_and_tail_use_live_preceding_context_and_raw_failure(self):
        result = self.run_batches(invalid_batches=True, failed_chunks=('Two.', 'Four.'))
        prompts = [call['prompt'] for call in result['calls'] if not call['batch']]
        self.assertEqual([
            'Annotate this segment:\nOne.',
            'Previous context: One.\n\nAnnotate this segment:\nTwo.',
            'Previous context: One. Two.\n\nAnnotate this segment:\nThree.',
            'Previous context: Two. Three.\n\nAnnotate this segment:\nFour.',
            'Previous context: Three. Four.\n\nAnnotate this segment:\nFive.',
        ], prompts)
        self.assertIn('LLM annotations: 3 ok, 2 failed', '\n'.join(result['logs']))
        self.assertIn('LLM batch attempts: 2 failed', '\n'.join(result['logs']))

    def test_failed_save_stops_full_and_tail_at_last_durable_chunk(self):
        for index in (1, 4):
            with self.subTest(index=index):
                self.run_batches(invalid_batches=True, save_failure=index)
