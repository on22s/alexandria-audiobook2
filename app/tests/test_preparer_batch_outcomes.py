import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
import soundfile as sf
from tests import test_preparer_run_state as support

preparer = support.preparer


class PreparerBatchOutcomeTests(unittest.TestCase):
    def run_annotation(self, failed_chunk=None, valid_batch=False, rewritten_chunk=None):
        words = [{'word': text, 'start': i * 1.2, 'end': (i + 1) * 1.2,
                  'confidence': 1, 'speaker': 'ONLY'}
                 for i, text in enumerate(('First.', 'Second.', 'Third.'))]
        calls = []
        def complete(**request):
            prompt = request['messages'][1]['content']
            calls.append(prompt)
            if '2. Annotate this segment:' in prompt:
                content = '["First.", "Second."]' if valid_batch else 'invalid batch JSON'
            else:
                content = prompt.split('Annotate this segment:\n', 1)[1]
                if content == failed_chunk:
                    raise RuntimeError('fixture chunk failure')
                if content == rewritten_chunk:
                    content = 'Invented words.'
            return {'choices': [{'message': {'content': content}}]}
        llm = SimpleNamespace(create_chat_completion=complete)
        samples = np.sin(np.arange(86400) * .05).astype('float32') * .1
        old = Path.cwd()
        with tempfile.TemporaryDirectory() as tmp:
            os.chdir(tmp)
            try:
                with patch.object(preparer, 'ensure_run_manifest'), patch.object(preparer, '_load_llm', return_value=llm), patch.object(preparer, 'log_gpu_stats'), patch.object(preparer, '_calculate_chunk_snr', return_value=80), self.assertLogs('alexandria', 'DEBUG') as logs:
                    rows = preparer.annotate_chunks(words, 'mock.gguf', 1, samples,
                        min_chunk_duration=1, batch_size=2, run_identity={'test': 'fixture'})
                persisted = [json.loads(line) for line in Path('dataset_temp/metadata.jsonl').read_text().splitlines()]
                self.assertEqual(['First.', 'Second.', 'Third.'], [row['text'] for row in persisted])
                self.assertEqual(3, len(rows))
                for row in persisted:
                    audio, rate = sf.read(Path('dataset_temp', row['audio_filepath']))
                    self.assertEqual(24000, rate)
                    self.assertAlmostEqual(1.2, len(audio) / rate)
                return '\n'.join(logs.output), calls
            finally:
                os.chdir(old)

    def test_recovered_batch_has_zero_failed_chunks(self):
        text, calls = self.run_annotation()
        self.assertEqual(4, len(calls))
        self.assertIn('LLM annotations: 3 ok, 0 failed', text)
        self.assertIn('LLM batch attempts: 1 failed; per-chunk fallback used', text)

    def test_unrecovered_chunk_is_still_counted_and_keeps_raw_text(self):
        text, calls = self.run_annotation(failed_chunk='Second.')
        self.assertEqual(4, len(calls))
        self.assertIn('LLM annotations: 2 ok, 1 failed', text)
        self.assertIn('Batch fallback LLM failed for chunk 1', text)
        self.assertIn('LLM batch attempts: 1 failed', text)

    def test_successful_batch_has_only_chunk_outcomes(self):
        text, calls = self.run_annotation(valid_batch=True)
        self.assertEqual(2, len(calls))
        self.assertIn('LLM annotations: 3 ok, 0 failed', text)
        self.assertNotIn('LLM batch attempts:', text)

    def test_discarded_partial_batch_does_not_count_success_before_fallback(self):
        from unittest.mock import Mock
        llm = SimpleNamespace(create_chat_completion=Mock(return_value={
            'choices': [{'message': {'content': '["First.", "Second."]'}}]}))
        alignment = SimpleNamespace(merge_annotations_with_source=Mock(
            side_effect=['First changed.', RuntimeError('merge failed')]))
        stats = {'llm_success': 0, 'llm_fail': 0, 'sanitize_changed': 0}
        batch = [{'segment_idx': i, 'text': text, 'source_words_for_merge': [text]}
                 for i, text in enumerate(('First.', 'Second.'))]
        self.assertIsNone(preparer._annotate_batch(llm, batch, alignment, 2,
                         {'llm_infer': 0, 'sanitize': 0}, stats))
        self.assertEqual({'llm_success': 0, 'llm_fail': 0,
                         'sanitize_changed': 0, 'llm_batch_fail': 1}, stats)
