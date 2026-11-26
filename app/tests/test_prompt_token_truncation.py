"""A server that cuts the prompt must be reported, never silently accepted.

Synthetic text only: the production prompt is never sent anywhere here. The
stub client returns valid attribution for a batch whose text is long enough
that a 100-token report cannot account for it.
"""
import io
import json
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace as NS

import three_pass_generate as tp
from generate_script import LLMGenParams
from llm_request_budget import get_suspect_prompt_tokens

LATIN = "The quick brown fox jumps over the lazy dog. " * 1000
CHINESE = ("这是服务器应当如实报告的提示词文本。" * 3000)[:45000]
JAPANESE = ("これはサーバーが正直に報告すべきプロンプトです。" * 2000)[:45000]


class SuspectPromptTokensTest(unittest.TestCase):
    messages = [{"role": "user", "content": LATIN}]

    def test_issue_report_is_suspect(self):
        finding = get_suspect_prompt_tokens(
            self.messages, NS(prompt_tokens=100, completion_tokens=9))
        self.assertIsNotNone(finding)
        self.assertEqual(100, finding["prompt_tokens_reported"])
        self.assertGreater(finding["prompt_tokens_estimate"], 100)

    def test_dense_scripts_with_honest_counts_are_not_suspect(self):
        for prompt in (CHINESE, JAPANESE):
            with self.subTest(characters=len(prompt)):
                self.assertEqual(45000, len(prompt))
                # One token per character is what a faithful server reports
                # for CJK; the project's estimate (3 chars/token) is lower.
                self.assertIsNone(get_suspect_prompt_tokens(
                    [{"role": "user", "content": prompt}], NS(prompt_tokens=len(prompt))))

    def test_honest_latin_count_is_not_suspect(self):
        self.assertIsNone(get_suspect_prompt_tokens(
            self.messages, NS(prompt_tokens=len(LATIN) // 4)))

    def test_cached_prompt_tokens_are_not_truncation(self):
        self.assertIsNone(get_suspect_prompt_tokens(
            self.messages, NS(prompt_tokens=100,
                              prompt_tokens_details=NS(cached_tokens=40000))))
        self.assertIsNone(get_suspect_prompt_tokens(
            self.messages, {"prompt_tokens": 100, "prompt_cache_hit_tokens": 40000}))

    def test_no_cache_does_not_hide_a_low_report(self):
        self.assertIsNotNone(get_suspect_prompt_tokens(
            self.messages, NS(prompt_tokens=100,
                              prompt_tokens_details=NS(cached_tokens=0))))

    def test_missing_or_zero_usage_is_no_evidence(self):
        for usage in (None, NS(), NS(prompt_tokens=None), NS(prompt_tokens=0),
                      NS(prompt_tokens=True), {"prompt_tokens": 0}, {}):
            with self.subTest(usage=usage):
                self.assertIsNone(get_suspect_prompt_tokens(self.messages, usage))


class AttributeBatchTruncationWarningTest(unittest.TestCase):

    def run_batch(self, usage, contents=None):
        calls = []
        contents = contents or [json.dumps([{"n": 0, "speaker": "ALICE"}])]

        def create(**kwargs):
            calls.append(kwargs)
            content = contents[min(len(calls) - 1, len(contents) - 1)]
            return NS(choices=[NS(message=NS(content=content), finish_reason="stop")],
                      usage=usage)
        client = NS(chat=NS(completions=NS(create=create)))
        attempts = []
        output = io.StringIO()
        with redirect_stdout(output):
            result = tp.attribute_batch(
                client, "m", [{"type": "SPOKEN", "text": LATIN}], LLMGenParams(max_tokens=500),
                roster=["ALICE"], max_retries=1, attempt_observer=attempts.append)
        return result, calls, attempts, output.getvalue()

    def test_repro_warns_and_records_the_fact(self):
        result, calls, attempts, output = self.run_batch(
            NS(prompt_tokens=100, completion_tokens=9))
        self.assertEqual(1, len(calls))
        self.assertEqual(["ALICE"], [entry["speaker"] for entry in result])
        self.assertIn("WARNING: server reported prompt_tokens=100", output)
        self.assertTrue(attempts[0]["prompt_tokens_suspect"])
        self.assertGreater(attempts[0]["prompt_tokens_estimate"], 100)

    def test_missing_usage_warns_nothing(self):
        result, calls, attempts, output = self.run_batch(None)
        self.assertEqual(["ALICE"], [entry["speaker"] for entry in result])
        self.assertNotIn("WARNING", output)
        self.assertNotIn("prompt_tokens_suspect", attempts[0])

    def test_retry_and_acceptance_are_unchanged(self):
        good = json.dumps([{"n": 0, "speaker": "ALICE"}])
        honest, honest_calls, honest_attempts, honest_output = self.run_batch(
            NS(prompt_tokens=len(LATIN) // 4, completion_tokens=9), ["not JSON", good])
        suspect, suspect_calls, suspect_attempts, suspect_output = self.run_batch(
            NS(prompt_tokens=100, completion_tokens=9), ["not JSON", good])
        self.assertEqual([entry["speaker"] for entry in honest], ["ALICE"])
        self.assertEqual([entry["speaker"] for entry in suspect], ["ALICE"])
        self.assertEqual(2, len(suspect_calls))
        self.assertEqual(len(honest_calls), len(suspect_calls))
        self.assertEqual(len(honest_attempts), len(suspect_attempts))
        self.assertNotIn("WARNING", honest_output)
        self.assertIn("WARNING: server reported prompt_tokens=100", suspect_output)


if __name__ == "__main__":
    unittest.main()
