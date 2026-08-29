"""Architecture and adapter-target guards for Qwen3.5 distillation."""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from experiments.distill_train import (
    get_lora_target_modules,
    get_model_loader_name,
    truncate_for_supervision,
)


class _FakeModel:
    def named_modules(self):
        return iter([
            ("model.visual.blocks.0.attn.q_proj", object()),
            ("model.language_model.layers.0.self_attn.q_proj", object()),
            ("model.language_model.layers.0.mlp.down_proj", object()),
            ("model.language_model.norm", object()),
        ])


class Qwen35TrainingTest(unittest.TestCase):
    def test_conditional_architectures_use_image_text_loader(self):
        for architecture in (
                "Qwen3_5ForConditionalGeneration",
                "Qwen3_5MoeForConditionalGeneration"):
            self.assertEqual(
                get_model_loader_name([architecture]),
                "AutoModelForImageTextToText")

    def test_existing_causal_model_keeps_causal_loader(self):
        self.assertEqual(
            get_model_loader_name(["Qwen3ForCausalLM"]),
            "AutoModelForCausalLM")

    def test_qwen35_targets_language_model_but_not_vision(self):
        targets = get_lora_target_modules(
            _FakeModel(), ["Qwen3_5ForConditionalGeneration"])
        self.assertEqual(targets, [
            "model.language_model.layers.0.mlp.down_proj",
            "model.language_model.layers.0.self_attn.q_proj",
        ])

    def test_qwen35_refuses_when_language_targets_are_absent(self):
        class VisionOnly:
            def named_modules(self):
                return iter([("model.visual.blocks.0.attn.q_proj", object())])
        with self.assertRaisesRegex(RuntimeError, "were not found"):
            get_lora_target_modules(
                VisionOnly(), ["Qwen3_5ForConditionalGeneration"])

    def test_long_prompt_is_truncated_without_losing_answer_labels(self):
        ids, labels = truncate_for_supervision(
            list(range(20)), [91, 92, 93], max_len=8)
        self.assertEqual(ids, [15, 16, 17, 18, 19, 91, 92, 93])
        self.assertEqual(labels, [-100] * 5 + [91, 92, 93])

    def test_answer_longer_than_limit_is_still_supervised(self):
        ids, labels = truncate_for_supervision(
            [1, 2], [91, 92, 93, 94], max_len=3)
        self.assertEqual(ids, [91, 92, 93])
        self.assertEqual(labels, [91, 92, 93])


if __name__ == "__main__":
    unittest.main()
