import unittest

from experiments.lora_serving_eval import get_eval_arms


class LoraServingEvalModeTests(unittest.TestCase):
    def test_normal_mode_toggles_both_adapter_scales(self):
        self.assertEqual((("base", 0.0), ("lora", 1.0)), get_eval_arms())

    def test_base_only_mode_never_requests_adapter_state(self):
        self.assertEqual((("base", None),), get_eval_arms(base_only=True))


if __name__ == "__main__":
    unittest.main()
