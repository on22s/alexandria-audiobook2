"""Classification stays deterministic while instruction generation can sample."""
import unittest
from unittest.mock import patch

import three_pass_generate as tp


class ClassificationTemperatureTest(unittest.TestCase):
    def test_defaults_and_global_profile_precedence(self):
        # Exercise the native policy builder, not the spelling of its source.
        keys = ("segment_temperature", "attribute_temperature", "instruct_temperature")
        global_values = dict(zip(keys, (0.2, 0.3, 0.4)))
        profile_values = dict(zip(keys, (0.5, 0.6, 0.7)))
        for generation, profile, expected in (
                ({}, {}, (0.0, 0.0, 0.1)),
                ({f"three_pass_{k}": v for k, v in global_values.items()},
                 {}, tuple(global_values.values())),
                ({f"three_pass_{k}": v for k, v in global_values.items()},
                 profile_values, tuple(profile_values.values()))):
            with self.subTest(generation=generation, profile=profile):
                config = {"llm": {"model_name": "unit-temperature-profile"},
                          "generation": {**generation, "three_pass_model_profiles": {
                              "unit-temperature-profile": profile}}}
                with patch.object(tp, "load_default_model_profiles", return_value={}):
                    params = tp.get_three_pass_run_params(config, {})
                self.assertEqual(expected, tuple(getattr(params, k) for k in keys))


if __name__ == "__main__":
    unittest.main()
