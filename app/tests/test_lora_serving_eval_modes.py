import unittest

from experiments.lora_serving_eval import (get_book_paths, get_eval_arms,
                                           get_eval_metadata)
from experiments.distill_eval import (get_book_paths as get_distill_book_paths,
                                      get_model_loader_name,
                                      get_model_load_kwargs,
                                      load_peft_adapter_or_raise)


class LoraServingEvalModeTests(unittest.TestCase):
    def test_normal_mode_toggles_both_adapter_scales(self):
        self.assertEqual((("base", 0.0), ("lora", 1.0)), get_eval_arms())

    def test_base_only_mode_never_requests_adapter_state(self):
        self.assertEqual((("base", None),), get_eval_arms(base_only=True))

    def test_base_only_metadata_claims_only_the_arm_actually_run(self):
        decoding, notes = get_eval_metadata(base_only=True)
        self.assertEqual(["base"], decoding["arms"])
        self.assertNotIn("base_quant", decoding)
        self.assertNotIn("lora", decoding)
        self.assertIn("no adapter was loaded", notes)
        self.assertIn("does not observe base quantisation", notes)

    def test_metadata_records_nondefault_request_controls(self):
        decoding, _ = get_eval_metadata(
            base_only=True, batch=5, reasoning_effort="low")
        self.assertEqual(5, decoding["batch"])
        self.assertEqual("low", decoding["reasoning_effort"])

    def test_paired_metadata_matches_the_two_executed_arms(self):
        decoding, notes = get_eval_metadata()
        self.assertEqual(["base", "lora"], decoding["arms"])
        self.assertNotIn("base_quant", decoding)
        self.assertNotIn("lora", decoding)
        self.assertIn("differ only by adapter scale", notes)
        self.assertIn("does not observe base quantisation", notes)

    def test_corrected_data_directories_override_legacy_layout(self):
        self.assertEqual(
            ("/inputs/index18.txt",
             "/checkpoints/index18__three_pass.json.threepass_checkpoint.json"),
            get_book_paths("index18", "/inputs", "/checkpoints"),
        )

    def test_distill_eval_uses_the_same_corrected_layout(self):
        self.assertEqual(
            ("/inputs/index18.txt",
             "/checkpoints/index18__three_pass.json.threepass_checkpoint.json"),
            get_distill_book_paths("index18", "/inputs", "/checkpoints"),
        )

    def test_nf4_loader_policy_preserves_bf16_compute(self):
        class Torch:
            bfloat16 = "bf16"

        marker = object()
        self.assertEqual(
            {"torch_dtype": "bf16", "device_map": "auto",
             "trust_remote_code": True, "quantization_config": marker},
            get_model_load_kwargs(Torch, marker),
        )

    def test_qwen35_moe_uses_training_compatible_wrapper(self):
        self.assertEqual(
            "AutoModelForImageTextToText",
            get_model_loader_name(["Qwen3_5MoeForConditionalGeneration"]),
        )
        self.assertEqual(
            "AutoModelForCausalLM",
            get_model_loader_name(["Qwen3ForCausalLM"]),
        )

    def test_missing_adapter_keys_fail_instead_of_running_inert(self):
        import warnings

        class Peft:
            @staticmethod
            def from_pretrained(base, adapter):
                warnings.warn("Found missing adapter keys while loading")
                return "plausible but inert"

        with self.assertRaisesRegex(RuntimeError, "did not activate"):
            load_peft_adapter_or_raise(Peft, object(), "/adapter")

    def test_clean_adapter_load_is_returned(self):
        class Peft:
            @staticmethod
            def from_pretrained(base, adapter):
                return (base, adapter)

        base = object()
        self.assertEqual(
            (base, "/adapter"),
            load_peft_adapter_or_raise(Peft, base, "/adapter"),
        )


if __name__ == "__main__":
    unittest.main()
