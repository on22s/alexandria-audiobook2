"""A server that cuts the prompt must be reported, never silently accepted.

Synthetic text only: the production prompt is never sent anywhere here. The
stub client returns valid attribution for a batch whose text is long enough
that a 100-token report cannot account for it.
"""
import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from types import SimpleNamespace as NS
from unittest.mock import patch

import cast_list
import three_pass_generate as tp
from generate_script import LLMGenParams
from llm_request_budget import get_suspect_prompt_tokens

LATIN = "The quick brown fox jumps over the lazy dog. " * 1000
CHINESE = ("这是服务器应当如实报告的提示词文本。" * 3000)[:45000]
JAPANESE = ("これはサーバーが正直に報告すべきプロンプトです。" * 2000)[:45000]

# Faithful token counts for prompts tokenizers pack many-to-one, from the
# review's Qwen3 measurements: dot leaders 6.08-6.61 chars/token, "#" rules
# 10.95, trailing spaces 6.43, blank lines 7.47, space runs 7.59, a 100-space
# indent 16.9. Each fixture reports fewer tokens than the measured rate allows,
# so the count is honest-or-harsher and the prompt still must not warn.
TOC = "".join(f"Chapter {index}: A title long enough to wrap {'.' * 80} {index}\n"
              for index in range(300))
RULES = ("#" * 100 + "\n") * 60
PADDED = ("A line of ordinary prose about the harbour and the tide." + " " * 40 + "\n") * 600
BLANK_LINES = "\n" * 5000
SPACE_RUNS = (" " * 80 + "\n") * 600
INDENTED = (" " * 100 + "word\n") * 100


class SuspectPromptTokensTest(unittest.TestCase):
    messages = [{"role": "user", "content": LATIN}]

    def test_issue_report_is_suspect(self):
        for prompt in (LATIN, LATIN[:40000]):
            with self.subTest(characters=len(prompt)):
                finding = get_suspect_prompt_tokens(
                    [{"role": "user", "content": prompt}],
                    NS(prompt_tokens=100, completion_tokens=9))
                self.assertIsNotNone(finding)
                self.assertEqual(100, finding["prompt_tokens_reported"])
                self.assertGreater(finding["prompt_tokens_estimate"], 100)

    def test_integral_float_report_is_evidence(self):
        self.assertIsNotNone(get_suspect_prompt_tokens(
            self.messages, NS(prompt_tokens=100.0, completion_tokens=9)))

    def test_missing_or_malformed_usage_is_no_evidence(self):
        for usage in (None, NS(), NS(prompt_tokens=None), NS(prompt_tokens=0),
                      NS(prompt_tokens=True), NS(prompt_tokens=-100),
                      NS(prompt_tokens=100.5), NS(prompt_tokens="100"),
                      {"prompt_tokens": 0}, {}):
            with self.subTest(usage=usage):
                self.assertIsNone(get_suspect_prompt_tokens(self.messages, usage))

    def test_dense_scripts_with_honest_counts_are_not_suspect(self):
        # Qwen3 measures Chinese at ~1.66 chars/token and Japanese at ~0.95
        # (review of this PR); the estimate assumes 3, so both stay under it.
        # The bound to state in the PR: detection needs reported < chars/6, so
        # a server keeping more than ~28% of a Chinese prompt or ~16% of a
        # Japanese one is not caught.
        for prompt, chars_per_token in ((CHINESE, 1.7), (JAPANESE, 1.0)):
            with self.subTest(characters=len(prompt)):
                self.assertEqual(45000, len(prompt))
                self.assertIsNone(get_suspect_prompt_tokens(
                    [{"role": "user", "content": prompt}],
                    NS(prompt_tokens=int(len(prompt) / chars_per_token))))

    def test_honest_latin_count_is_not_suspect(self):
        self.assertIsNone(get_suspect_prompt_tokens(
            self.messages, NS(prompt_tokens=len(LATIN) // 4)))

    def test_reports_between_half_and_a_sixth_of_the_estimate_warn(self):
        """Pins both sides: warn below estimate/2, not at or above it.

        With 45,000 characters the estimate is 15,000, so a 6,000 report must
        warn (a *3 factor would miss it) and a 7,500 report must not (a *1
        factor would flag it).
        """
        self.assertIsNotNone(get_suspect_prompt_tokens(
            self.messages, NS(prompt_tokens=6000)))
        self.assertIsNone(get_suspect_prompt_tokens(
            self.messages, NS(prompt_tokens=7500)))

    def test_compressible_prompts_with_faithful_counts_are_not_suspect(self):
        for label, prompt, chars_per_token in (
                ("dot leaders", TOC, 7), ("rules", RULES, 12),
                ("trailing spaces", PADDED, 7), ("blank lines", BLANK_LINES, 8),
                ("space runs", SPACE_RUNS, 8), ("indent", INDENTED, 17)):
            with self.subTest(prompt=label, characters=len(prompt)):
                reported = len(prompt) // chars_per_token
                self.assertIsNone(get_suspect_prompt_tokens(
                    [{"role": "user", "content": prompt}],
                    NS(prompt_tokens=reported)))

    def test_subset_cache_tokens_do_not_suppress_a_true_warning(self):
        # 45,000 chars, estimate 15,000: a 5,000 report is about half the
        # prompt, so it must warn. Cached tokens are a subset of prompt_tokens
        # in every OpenAI-compatible shape; adding them would hide the cut.
        for usage in (NS(prompt_tokens=5000, prompt_tokens_details=NS(cached_tokens=4500)),
                      {"prompt_tokens": 5000, "prompt_cache_hit_tokens": 4500},
                      {"prompt_tokens": 5000,
                       "input_tokens_details": {"cached_tokens": 4500}}):
            with self.subTest(usage=usage):
                finding = get_suspect_prompt_tokens(self.messages, usage)
                self.assertIsNotNone(finding)
                self.assertEqual(5000, finding["prompt_tokens_reported"])

    def test_separate_anthropic_cache_is_added_back(self):
        # Anthropic reports input_tokens without cache_read_input_tokens, so the
        # two together (9,500 of an estimated 15,000) are a plausible report.
        self.assertIsNone(get_suspect_prompt_tokens(
            self.messages, NS(prompt_tokens=5000, cache_read_input_tokens=4500)))

    def test_zero_separate_cache_does_not_hide_a_low_report(self):
        self.assertIsNotNone(get_suspect_prompt_tokens(
            self.messages, NS(prompt_tokens=100, cache_read_input_tokens=0)))


class _CastClient:
    def __init__(self, content, usage):
        self.content, self.usage = content, usage
        self.requests = []
        self.chat = NS(completions=NS(create=self.create))

    def create(self, **kwargs):
        self.requests.append(kwargs)
        return NS(choices=[NS(finish_reason="stop", message=NS(content=self.content))],
                  usage=self.usage)


class CastListTruncationTest(unittest.TestCase):
    reply = json.dumps([{"name": "ALICE", "aliases": []}])

    def test_incremental_window_warns_and_records_the_fact(self):
        window = ("Alice walked to the harbour and waited for the ferry. " * 600)[:30000]
        client = _CastClient(self.reply, NS(prompt_tokens=100, completion_tokens=9))
        reports = []
        cast, windows, dropped = cast_list.request_incremental_cast(
            client, "m", window, 30000, 8000, report=reports.append)
        self.assertEqual(1, len(client.requests))
        self.assertEqual([], dropped)
        self.assertEqual([{"name": "ALICE", "aliases": []}], cast)
        self.assertTrue(windows[0]["prompt_tokens_suspect"])
        self.assertGreater(windows[0]["prompt_tokens_estimate"], 100)
        self.assertEqual(100, windows[0]["usage"]["prompt_tokens"])
        self.assertIn("WARNING: server reported prompt_tokens=100 for a request estimated at",
                      "\n".join(reports))

    def test_whole_book_cast_records_the_fact(self):
        with tempfile.TemporaryDirectory() as directory:
            source = os.path.join(directory, "book.txt")
            out = os.path.join(directory, "cast.json")
            with open(source, "w", encoding="utf-8") as handle:
                handle.write("Alice walked to the harbour and waited. " * 1000)
            client = _CastClient(self.reply, {"prompt_tokens": 100, "completion_tokens": 9})
            output = io.StringIO()
            with patch("config_settings.load_app_config", return_value={}), \
                    patch("lmstudio_settings.get_active_llm_config",
                          return_value={"model_name": "m"}), \
                    patch("llm_provider.make_run_client", return_value=client), \
                    redirect_stdout(output):
                code = cast_list.main([source, "--out", out])
            self.assertEqual(0, code, output.getvalue())
            with open(out, encoding="utf-8") as handle:
                provenance = json.load(handle)["provenance"]
            self.assertTrue(provenance["prompt_tokens_suspect"])
            self.assertGreater(provenance["prompt_tokens_estimate"], 100)
            self.assertEqual(100, provenance["usage"]["prompt_tokens"])
            self.assertIn("WARNING: server reported prompt_tokens=100", output.getvalue())


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

    def test_dict_usage_is_recorded_like_an_object(self):
        result, calls, attempts, output = self.run_batch(
            {"prompt_tokens": 100, "completion_tokens": 9})
        self.assertEqual(["ALICE"], [entry["speaker"] for entry in result])
        self.assertIn("WARNING: server reported prompt_tokens=100", output)
        self.assertIn("tokens: prompt=100 completion=9", output)
        self.assertEqual(100, attempts[0]["prompt_tokens"])
        self.assertEqual(9, attempts[0]["completion_tokens"])

    def test_missing_usage_warns_nothing(self):
        result, calls, attempts, output = self.run_batch(None)
        self.assertEqual(["ALICE"], [entry["speaker"] for entry in result])
        self.assertNotIn("WARNING: server reported prompt_tokens", output)
        self.assertNotIn("prompt_tokens_suspect", attempts[0])

    def test_warn_only_across_usage_shapes(self):
        """The reviewer's stub diff: usage shape changes the warning and only that.

        Every shipped usage shape, valid or malformed, must leave acceptance,
        call counts and attempt records identical; the warning appears exactly
        when the decision function says suspect.
        """
        good = json.dumps([{"n": 0, "speaker": "ALICE"}])
        usages = {
            "missing": None,
            "honest": NS(prompt_tokens=len(LATIN) // 4, completion_tokens=9),
            "suspect": NS(prompt_tokens=100, completion_tokens=9),
            "integral float": NS(prompt_tokens=100.0, completion_tokens=9),
            "zero": NS(prompt_tokens=0, completion_tokens=9),
            "negative": NS(prompt_tokens=-1, completion_tokens=9),
            "bool": NS(prompt_tokens=True, completion_tokens=9),
            "deepseek subset": {"prompt_tokens": 5000, "prompt_cache_hit_tokens": 4500},
            "anthropic separate": NS(prompt_tokens=5000, cache_read_input_tokens=4500),
            "dict suspect": {"prompt_tokens": 100, "completion_tokens": 9},
        }
        shaping = lambda record: {key: value for key, value in record.items()
                                  if key not in ("request_id", "elapsed_seconds",
                                                 "prompt_tokens", "completion_tokens",
                                                 "prompt_tokens_suspect", "prompt_tokens_estimate")}
        results = {name: self.run_batch(usage, ["not JSON", good])
                   for name, usage in usages.items()}
        control = results["missing"]
        self.assertEqual(["ALICE"], [entry["speaker"] for entry in control[0]])
        self.assertEqual(2, len(control[1]))
        for name, (result, calls, attempts, output) in results.items():
            with self.subTest(usage=name):
                self.assertEqual([entry["speaker"] for entry in control[0]],
                                 [entry["speaker"] for entry in result])
                self.assertEqual(len(control[1]), len(calls))
                self.assertEqual([shaping(record) for record in control[2]],
                                 [shaping(record) for record in attempts])
                expected = get_suspect_prompt_tokens(calls[0]["messages"], usages[name])
                self.assertEqual(bool(expected),
                                 "WARNING: server reported prompt_tokens" in output)

    def test_exhausted_retries_are_unchanged(self):
        counts = []
        for usage in (None, NS(prompt_tokens=100, completion_tokens=9),
                      {"prompt_tokens": 100, "completion_tokens": 9}):
            calls = []

            def create(**kwargs):
                calls.append(kwargs)
                return NS(choices=[NS(message=NS(content="not JSON"), finish_reason="stop")],
                          usage=usage)
            client = NS(chat=NS(completions=NS(create=create)))
            with redirect_stdout(io.StringIO()), self.assertRaises(tp.PassExhausted):
                tp.attribute_batch(client, "m", [{"type": "SPOKEN", "text": LATIN}],
                                   LLMGenParams(max_tokens=500), roster=["ALICE"],
                                   max_retries=1)
            counts.append(len(calls))
        self.assertEqual([2, 2, 2], counts)

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
        self.assertNotIn("WARNING: server reported prompt_tokens", honest_output)
        self.assertIn("WARNING: server reported prompt_tokens=100", suspect_output)


if __name__ == "__main__":
    unittest.main()
