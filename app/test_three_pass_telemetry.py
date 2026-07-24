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


class RunManifestTest(unittest.TestCase):

    def test_manifest_summarizes_a_run(self):
        from three_pass_generate import build_run_manifest
        manifest = build_run_manifest(
            model_name="qwen3.5-9b", thinking_mode="none",
            elapsed_s={"segment": 95.0, "attribute": 1059.0},
            counters={"truncations": 44, "subdivisions": 3,
                      "near_misses": 1, "context_rescues": 2},
            unicode_report={"repaired": 6036, "residual": 626},
            failures=[{"reason": "reasoning_overflow"},
                      {"reason": "missing_json_array"},
                      {"reason": "reasoning_overflow"}])
        self.assertEqual(manifest["model_name"], "qwen3.5-9b")
        self.assertEqual(manifest["thinking_mode"], "none")
        self.assertEqual(manifest["elapsed_s"]["attribute"], 1059.0)
        self.assertEqual(manifest["counters"]["truncations"], 44)
        self.assertEqual(manifest["failure_reasons"]["reasoning_overflow"], 2)
        self.assertEqual(manifest["failure_reasons"]["missing_json_array"], 1)
        self.assertEqual(manifest["unicode"]["residual"], 626)
        self.assertEqual(manifest["failure_count"], 3)


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
