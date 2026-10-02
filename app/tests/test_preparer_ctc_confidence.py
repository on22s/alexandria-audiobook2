import unittest
from unittest.mock import patch
import torch
from tests import test_preparer_run_state as support

p = support.preparer


class PreparerCtcConfidenceTests(unittest.TestCase):
    def test_peak_probabilities_equal_full_softmax_across_blocks_and_dtypes(self):
        generator = torch.Generator(device='cpu').manual_seed(67)
        for frames in (1, 255, 256, 257, 1500):
            for dtype in (torch.float16, torch.float32, torch.float64):
                with self.subTest(frames=frames, dtype=dtype):
                    logits = torch.randn((1, frames, 32), generator=generator, dtype=torch.float64).to(dtype)
                    logits[:, 0, :] = 10000
                    if frames > 1:
                        logits[:, 1, :] = -10000
                        logits[:, 1, 3] = 10000
                    original = logits.clone()
                    expected = torch.max(torch.nn.functional.softmax(logits, dim=-1), dim=-1).values
                    current = p.get_ctc_frame_confidence(logits, torch)
                    self.assertTrue(torch.equal(expected, current))
                    self.assertTrue(torch.equal(original, logits))
                    self.assertEqual((1, frames), tuple(current.shape))

    def test_softmax_temporary_frames_are_bounded_and_confidence_is_not_approximated(self):
        logits = torch.zeros((1, 1500, 64), dtype=torch.float32)
        shapes = []
        softmax = torch.nn.functional.softmax
        def observe(block, **kwargs):
            shapes.append(tuple(block.shape))
            return softmax(block, **kwargs)
        with patch.object(torch.nn.functional, 'softmax', side_effect=observe):
            confidence = p.get_ctc_frame_confidence(logits, torch)
        self.assertEqual(6, len(shapes))
        self.assertEqual(1500, sum(shape[1] for shape in shapes))
        self.assertLessEqual(max(shape[1] for shape in shapes), 256)
        self.assertTrue(torch.equal(confidence, torch.full((1, 1500), 1 / 64)))

    def run_native_ctc(self):
        import sys
        from types import SimpleNamespace
        import numpy as np
        logits = torch.zeros((1, 600, 32), device='cpu')
        logits[:, 260:270, 3] = 8
        offsets = [{'word': 'low', 'start_offset': 0, 'end_offset': 10},
                   {'word': 'high', 'start_offset': 260, 'end_offset': 270},
                   {'word': 'tail', 'start_offset': 510, 'end_offset': 520}]
        class Processor:
            def __call__(self, audio, **kwargs):
                return {'input_values': torch.from_numpy(audio).unsqueeze(0)}
            def batch_decode(self, ids, **kwargs):
                self_ids = torch.argmax(logits, dim=-1)
                if not torch.equal(ids, self_ids):
                    raise AssertionError('CTC IDs changed')
                return SimpleNamespace(word_offsets=[offsets])
        class Model:
            config = SimpleNamespace(inputs_to_logits_ratio=320)
            def to(self, device):
                if device != 'cpu':
                    raise AssertionError('CPU fixture requested another device')
                return self
            def eval(self):
                pass
            def __call__(self, **kwargs):
                return SimpleNamespace(logits=logits)
        processors = SimpleNamespace(from_pretrained=lambda *args, **kwargs: Processor())
        models = SimpleNamespace(from_pretrained=lambda *args, **kwargs: Model())
        shapes = []
        softmax = torch.nn.functional.softmax
        def observe(block, **kwargs):
            value = softmax(block, **kwargs)
            shapes.append({'frames': block.shape[1], 'probability_bytes': value.numel() * value.element_size()})
            return value
        with patch.dict(sys.modules, {'transformers': SimpleNamespace(Wav2Vec2Processor=processors, Wav2Vec2ForCTC=models)}), patch.object(p, 'TRANSFORMERS_WHISPER_AVAILABLE', True), patch.object(p, 'resolve_cuda_device', return_value='cpu'), patch.object(p, 'log_gpu_stats'), patch.object(p, 'clear_vram'), patch.object(torch.nn.functional, 'softmax', side_effect=observe):
            rows, language = p.transcribe_with_wav2vec2(np.zeros(30 * 16000, dtype='float32'))
        expected = torch.max(softmax(logits, dim=-1), dim=-1).values[0].numpy()
        self.assertEqual('en', language)
        self.assertEqual(['low', 'high', 'tail'], [row['word'] for row in rows])
        for row, offset in zip(rows, offsets):
            self.assertEqual(offset['start_offset'] * .02, row['start'])
            self.assertEqual(offset['end_offset'] * .02, row['end'])
            self.assertEqual(float(np.mean(expected[offset['start_offset']:offset['end_offset']])), row['confidence'])
        self.assertLess(rows[0]['confidence'], .85)
        self.assertGreater(rows[1]['confidence'], .85)
        return rows, shapes

    def test_actual_ctc_path_preserves_ids_offsets_and_filter_confidences(self):
        _, shapes = self.run_native_ctc()
        self.assertEqual([256, 256, 88], [item['frames'] for item in shapes])
