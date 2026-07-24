import unittest

from three_pass_generate import build_failure_record


class BuildFailureRecordTest(unittest.TestCase):

    def test_record_carries_causal_fields(self):
        record = build_failure_record(
            pass_name="attribute", index=7, text="He said hello.",
            last_attempt={"finish_reason": "length", "prompt_tokens": 2328,
                          "completion_tokens": 10000, "reasoning_tokens": 9987,
                          "effective_max_tokens": 10000, "attempt": 3,
                          "failure_codes": ["missing_json_array"]})
        self.assertEqual(record["pass"], "attribute")
        self.assertEqual(record["entry"], 7)
        self.assertEqual(record["finish_reason"], "length")
        self.assertEqual(record["reasoning_tokens"], 9987)
        self.assertEqual(record["effective_max_tokens"], 10000)
        self.assertEqual(record["attempt"], 3)
        self.assertEqual(record["reason"], "missing_json_array")
        self.assertEqual(record["text_preview"], "He said hello.")
        self.assertEqual(len(record["text_sha256"]), 64)

    def test_record_tolerates_missing_attempt_data(self):
        record = build_failure_record(
            pass_name="instruct", index=0, text="x", last_attempt=None)
        self.assertEqual(record["pass"], "instruct")
        self.assertIsNone(record["finish_reason"])
        self.assertIsNone(record["reasoning_tokens"])
        self.assertEqual(record["reason"], "unknown")


class ModelProfileMappingTest(unittest.TestCase):
    """load_app_config validates profiles into ThreePassModelProfile objects,
    but this module reads them with .get(). A configured profile previously
    crashed the run with AttributeError."""

    def test_pydantic_profile_is_readable(self):
        from config_settings import ThreePassModelProfile
        from three_pass_generate import as_profile_mapping
        profile = as_profile_mapping(
            ThreePassModelProfile(segment_output_ratio=5.0))
        self.assertEqual(profile.get("segment_output_ratio"), 5.0)

    def test_unset_fields_fall_back_to_the_default(self):
        from config_settings import ThreePassModelProfile
        from three_pass_generate import as_profile_mapping
        profile = as_profile_mapping(
            ThreePassModelProfile(segment_output_ratio=5.0))
        # model_dump() emits None for unset fields; those must not shadow the
        # caller's default or chunk_size would become None.
        self.assertEqual(profile.get("chunk_size", 3000), 3000)

    def test_plain_dict_still_works(self):
        from three_pass_generate import as_profile_mapping
        self.assertEqual(
            as_profile_mapping({"segment_output_ratio": 4.0}),
            {"segment_output_ratio": 4.0})

    def test_missing_profile_is_empty(self):
        from three_pass_generate import as_profile_mapping
        self.assertEqual(as_profile_mapping(None), {})


class ObserverPlumbingTest(unittest.TestCase):

    def test_attribute_batch_accepts_an_observer(self):
        import inspect

        from three_pass_generate import attribute_batch, instruct_batch
        self.assertIn("attempt_observer",
                      inspect.signature(attribute_batch).parameters)
        self.assertIn("attempt_observer",
                      inspect.signature(instruct_batch).parameters)


if __name__ == "__main__":
    unittest.main()
