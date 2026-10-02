import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import numpy as np
import soundfile as sf
import mutagen.wave
from tests import test_preparer_run_state as support

p = support.preparer


class PreparerCompletedResumeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        old = Path.cwd()
        os.chdir(self.temp.name)
        self.addCleanup(os.chdir, old)
        self.work = Path('dataset_temp')
        self.work.mkdir()
        self.samples = np.sin(np.arange(57600) * .05).astype('float32') * .1
        sf.write(self.work / 'sample_0000.wav', self.samples[:28800], 24000)
        self.entry = {'audio_filepath': 'sample_0000.wav', 'text': 'Done.',
                      'duration': 1.2, 'start': 0, 'end': 1.2,
                      'speaker': 'ONLY', 'speaker_labels': ['ONLY'], 'book_title': 'Saved Title'}
        (self.work / 'metadata.jsonl').write_text(json.dumps(self.entry) + '\n')
        self.words = [{'word': 'Done.', 'start': 0, 'end': 1.2,
                       'speaker': 'ONLY', 'confidence': 1}]

    def annotate(self, words, **kwargs):
        with patch.object(p, 'ensure_run_manifest'), patch.object(p, 'log_gpu_stats'), patch.object(p, 'clear_vram') as cleanup, patch.object(p, '_calculate_chunk_snr', return_value=80):
            rows = p.annotate_chunks(words, 'unused.gguf', 1, self.samples,
                resume=True, min_chunk_duration=1, run_identity={'test': 'resume'}, **kwargs)
        cleanup.assert_called_once()
        return rows

    def test_completed_checkpoint_skips_model_but_tags_and_exports_native_audio(self):
        original_checkpoint = (self.work / 'metadata.jsonl').read_bytes()
        sf.write(self.work / 'sample_0001.wav', self.samples[:28800], 24000)
        with patch.object(p, '_load_llm', side_effect=AssertionError('unused model must not load')) as model:
            rows = self.annotate(self.words)
        model.assert_not_called()
        self.assertEqual(['Done.'], [row['text'] for row in rows])
        self.assertNotIn('wav_path', rows[0])
        self.assertNotIn('book_title', rows[0])
        self.assertFalse((self.work / 'sample_0001.wav').exists())
        self.assertEqual(original_checkpoint, (self.work / 'metadata.jsonl').read_bytes())
        self.assertEqual(['Saved Title'], mutagen.wave.WAVE(self.work / 'sample_0000.wav').tags['TALB'].text)
        values, rate = sf.read(self.work / 'sample_0000.wav')
        self.assertEqual(24000, rate)
        self.assertAlmostEqual(1.2, len(values) / rate)
        p._create_zip_dataset(rows, 'completed.zip')
        self.assertTrue(Path('completed.zip').is_file())

    def test_blank_incomplete_and_pre_resume_words_do_not_load_model(self):
        words = [*self.words, {'word': ' ', 'start': 2, 'end': 3}, {'word': 'missing time'}]
        with patch.object(p, '_load_llm') as model:
            self.assertEqual(1, len(self.annotate(words)))
        model.assert_not_called()

    def test_partial_resume_keeps_first_remaining_word_and_loads_once(self):
        llm = SimpleNamespace(create_chat_completion=lambda **request: {
            'choices': [{'message': {'content': 'Remaining.'}}]})
        words = [*self.words, {'word': 'Remaining.', 'start': 1.2, 'end': 2.4,
                              'speaker': 'ONLY', 'confidence': 1}]
        with patch.object(p, '_load_llm', return_value=llm) as model:
            rows = self.annotate(words)
        model.assert_called_once_with('unused.gguf')
        self.assertEqual(['Done.', 'Remaining.'], [row['text'] for row in rows])
        self.assertEqual(['sample_0000.wav', 'sample_0001.wav'], [row['audio_filepath'] for row in rows])
        saved = [json.loads(line) for line in (self.work / 'metadata.jsonl').read_text().splitlines()]
        self.assertEqual(['Done.', 'Remaining.'], [row['text'] for row in saved])

    def test_invalid_completed_checkpoint_still_fails_before_loading_model(self):
        (self.work / 'sample_0000.wav').unlink()
        with patch.object(p, '_load_llm') as model:
            with self.assertRaises(p.RunStateError):
                self.annotate(self.words)
        model.assert_not_called()

    def test_partial_resume_preserves_primary_to_fallback_model_loading(self):
        Path('fallback.gguf').write_bytes(b'fixture model identity; not loaded')
        llm = SimpleNamespace(create_chat_completion=lambda **request: {
            'choices': [{'message': {'content': 'Remaining.'}}]})
        words = [*self.words, {'word': 'Remaining.', 'start': 1.2, 'end': 2.4,
                              'speaker': 'ONLY', 'confidence': 1}]
        with patch.object(p, '_load_llm', side_effect=[RuntimeError('primary unavailable'), llm]) as model:
            rows = self.annotate(words, fallback_model_path='fallback.gguf')
        self.assertEqual(['unused.gguf', 'fallback.gguf'], [call.args[0] for call in model.call_args_list])
        self.assertEqual(['Done.', 'Remaining.'], [row['text'] for row in rows])
