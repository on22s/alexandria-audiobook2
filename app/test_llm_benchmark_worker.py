import unittest
from unittest.mock import patch

import benchmark_runner
import llm_benchmark_worker


class LlmBenchmarkWorkerTests(unittest.TestCase):
    def test_script_generation_stage_calls_process_chunk_per_repetition(self):
        payload = {
            "base_url": "http://localhost:1234/v1", "model_name": "m",
            "max_retries": 0, "params": {"context_length": 8192},
            "fixtures": [{"id": "f1", "text": "Hello.", "chunk_number": 1,
                         "total_chunks": 1, "repetition_numbers": [1, 2]}],
        }
        with patch.object(benchmark_runner, "process_chunk", return_value=["entry"]), \
             patch.object(benchmark_runner, "validate_chunk_quality",
                          return_value={"passed": True}) as validate:
            cases = llm_benchmark_worker.execute_payload("script_generation", payload)
        self.assertEqual(2, len(cases))
        self.assertEqual([1, 2], [case["repetition"] for case in cases])
        self.assertTrue(all(case["status"] == "passed" for case in cases))
        self.assertEqual(2, validate.call_count)

    def test_script_review_stage_calls_review_batch_per_repetition(self):
        payload = {
            "base_url": "http://localhost:1234/v1", "model_name": "m",
            "max_retries": 0, "params": {"context_length": 8192},
            "word_ratio_min": 0.95, "word_ratio_max": 1.05,
            "fixtures": [{"id": "f1", "original": [{"speaker": "N", "text": "hi"}],
                         "repetition_numbers": [1]}],
        }
        with patch.object(benchmark_runner, "review_batch",
                          return_value=[{"speaker": "N", "text": "hi"}]), \
             patch.object(benchmark_runner, "check_text_loss",
                          return_value=(True, None, None, 1.0)):
            cases = llm_benchmark_worker.execute_payload("script_review", payload)
        self.assertEqual(1, len(cases))
        self.assertEqual("passed", cases[0]["status"])

    def test_persona_generation_stage_reuses_run_persona_case(self):
        payload = {"base_url": "http://localhost:1234/v1", "model_name": "m",
                  "context_length": 8192,
                  "fixtures": [{"id": "f1", "repetition_numbers": [1]}]}
        fake_result = {"status": "passed", "elapsed_seconds": 1.0}
        with patch.object(benchmark_runner, "_run_persona_case",
                          return_value=fake_result) as run_case:
            cases = llm_benchmark_worker.execute_payload("persona_generation", payload)
        self.assertEqual(1, len(cases))
        self.assertEqual("f1", cases[0]["fixture_id"])
        self.assertEqual(1, cases[0]["repetition"])
        run_case.assert_called_once()

    def test_nickname_detection_stage_reuses_run_nickname_case(self):
        payload = {"base_url": "http://localhost:1234/v1", "model_name": "m",
                  "context_length": 4096, "concurrency": 1,
                  "fixtures": [{"id": "f1", "repetition_numbers": [1]}]}
        fake_case = {"fixture_id": "f1", "repetition": 1, "status": "passed"}
        with patch.object(benchmark_runner, "_run_nickname_case",
                          return_value=fake_case) as run_case:
            cases = llm_benchmark_worker.execute_payload("nickname_detection", payload)
        self.assertEqual([fake_case], cases)
        run_case.assert_called_once()

    def test_unsupported_stage_raises(self):
        with self.assertRaisesRegex(ValueError, "unsupported LLM benchmark stage"):
            llm_benchmark_worker.execute_payload("bogus_stage", {
                "base_url": "http://localhost:1234/v1", "model_name": "m", "fixtures": []})

    def test_uses_repetitions_default_when_fixture_omits_numbers(self):
        self.assertEqual([1, 2, 3], list(llm_benchmark_worker._repetitions_for(
            {"id": "f1"}, range(1, 4))))
        self.assertEqual([5], llm_benchmark_worker._repetitions_for(
            {"id": "f1", "repetition_numbers": [5]}, range(1, 4)))


if __name__ == "__main__":
    unittest.main()
