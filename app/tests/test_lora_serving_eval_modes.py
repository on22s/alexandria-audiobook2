import unittest

from experiments.lora_serving_eval import get_book_paths, get_eval_arms


class LoraServingEvalModeTests(unittest.TestCase):
    def test_normal_mode_toggles_both_adapter_scales(self):
        self.assertEqual((("base", 0.0), ("lora", 1.0)), get_eval_arms())

    def test_base_only_mode_never_requests_adapter_state(self):
        self.assertEqual((("base", None),), get_eval_arms(base_only=True))

    def test_corrected_data_directories_override_legacy_layout(self):
        self.assertEqual(
            ("/inputs/index18.txt",
             "/checkpoints/index18__three_pass.json.threepass_checkpoint.json"),
            get_book_paths("index18", "/inputs", "/checkpoints"),
        )


if __name__ == "__main__":
    unittest.main()
