"""Verify language conditioning in real CPU teacher-forcing input tensors."""
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import patch

import torch

from train_lora import build_teacher_forcing_input, parse_args


class TrackedEmbedding(torch.nn.Embedding):
    def __init__(self):
        super().__init__(40, 4)
        self.seen = []
        with torch.no_grad():
            self.weight.copy_(torch.arange(40).unsqueeze(1).expand(40, 4))

    def forward(self, ids):
        self.seen.append(ids.detach().clone())
        return super().forward(ids)


def make_fixture():
    codec = TrackedEmbedding()
    text = TrackedEmbedding()
    with torch.no_grad():
        text.weight.zero_()
    tc = SimpleNamespace(num_code_groups=2, codec_language_id={"english": 7, "japanese": 8},
                         codec_think_id=10, codec_think_bos_id=11, codec_think_eos_id=12,
                         codec_nothink_id=9, codec_pad_id=13, codec_bos_id=14)
    talker = SimpleNamespace(text_projection=torch.nn.Identity(),
        get_text_embeddings=lambda: text, get_input_embeddings=lambda: codec,
        code_predictor=SimpleNamespace(get_input_embeddings=lambda: [torch.nn.Embedding(40, 4)]))
    model = SimpleNamespace(talker=talker, config=SimpleNamespace(talker_config=tc,
                            tts_bos_token_id=15, tts_eos_token_id=16, tts_pad_token_id=17))
    sample = {"codec_ids": torch.tensor([[1, 2], [3, 4], [5, 6]]),
              "spk_embedding": torch.zeros(1, 4), "text_ids": torch.arange(1, 11).unsqueeze(0)}
    return sample, model, codec, text


class TeacherForcingLanguageTests(unittest.TestCase):
    def test_typo_or_missing_language_map_fails_before_embedding_work(self):
        for mapping in ({"english": 7}, {}, None):
            with self.subTest(mapping=mapping):
                sample, model, codec, text = make_fixture()
                model.config.talker_config.codec_language_id = mapping
                with self.assertRaisesRegex(ValueError, "Unsupported.*language"):
                    build_teacher_forcing_input(sample, model, "cpu", torch.float32, "englsh")
                self.assertEqual([], codec.seen)
                self.assertEqual([], text.seen)

    def test_supported_languages_condition_the_actual_input_tensor(self):
        for language, ident in (("english", 7), ("japanese", 8), (" English ", 7)):
            with self.subTest(language=language):
                sample, model, codec, _ = make_fixture()
                inputs, labels, codes, prefill = build_teacher_forcing_input(
                    sample, model, "cpu", torch.float32, language)
                self.assertEqual([[10, 11, ident, 12]], codec.seen[0].tolist())
                self.assertEqual([float(ident)] * 4, inputs[0, 5].tolist())
                self.assertEqual([1, 3, 5], labels[0, prefill:].tolist())
                self.assertTrue(torch.equal(codes, sample["codec_ids"]))
                self.assertTrue(torch.isfinite(inputs).all())

    def test_explicit_auto_retains_the_automatic_prefix(self):
        sample, model, codec, _ = make_fixture()
        _inputs, labels, _codes, prefill = build_teacher_forcing_input(
            sample, model, "cpu", torch.float32, "auto")
        self.assertEqual([[9, 11, 12]], codec.seen[0].tolist())
        self.assertEqual([1, 3, 5], labels[0, prefill:].tolist())

    def test_cli_typo_is_refused_by_the_training_input_builder(self):
        with patch.object(sys, "argv", ["train_lora.py", "--data_dir", "data",
                         "--output_dir", "output", "--language", "englsh"]):
            args = parse_args()
        sample, model, codec, text = make_fixture()
        with self.assertRaisesRegex(ValueError, "Unsupported.*language"):
            build_teacher_forcing_input(sample, model, "cpu", torch.float32, args.language)
        self.assertEqual([], codec.seen)
        self.assertEqual([], text.seen)

    def test_zero_language_token_is_valid_conditioning(self):
        sample, model, codec, _ = make_fixture()
        model.config.talker_config.codec_language_id = {"english": 0}
        inputs, _labels, _codes, _prefill = build_teacher_forcing_input(
            sample, model, "cpu", torch.float32)
        self.assertEqual([[10, 11, 0, 12]], codec.seen[0].tolist())
        self.assertEqual([0.0] * 4, inputs[0, 5].tolist())
