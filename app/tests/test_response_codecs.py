"""The lines arm must reuse the JSON arm's machinery, not a copy of it.

An A/B is only a measurement if both arms differ in ONE thing. These pin the
places where the line format could silently stop being comparable: a prompt
half-converted, a checkpoint shared between formats, or a codec that quietly
returns nothing instead of failing the attempt.
"""
import types
import unittest

import generate_script
from generate_script import LLMGenParams, get_generation_fingerprint
from response_codecs import (PromptShapeError, build_line_format_prompt,
                             get_codec)

SHIPPED = ("You are a script writer. Output ONLY valid JSON arrays.\n\n"
           "FORMAT:\n[\n  {\"speaker\": \"X\"}\n]\n\n"
           "RULES:\n1. PRESERVE THE AUTHOR'S TEXT.\n")


class LineCodec(unittest.TestCase):
    def test_extract_drops_fences_and_commentary(self):
        codec = get_codec("lines")
        raw = ("Here is the script:\n```\n"
               "NARRATOR|Calm.|The room was cold.\n"
               "ELENA|Firm.|Tell me the truth.\n```")
        entries = codec.parse(codec.extract(raw))
        self.assertEqual([e["speaker"] for e in entries], ["NARRATOR", "ELENA"])
        self.assertEqual(entries[0]["text"], "The room was cold.")

    def test_extract_returns_empty_when_no_records(self):
        """An empty payload must fail the attempt, not yield [] silently."""
        codec = get_codec("lines")
        self.assertEqual(codec.extract("I cannot do that."), "")

    def test_unknown_format_is_rejected(self):
        with self.assertRaises(ValueError):
            get_codec("yaml")


class PromptConversion(unittest.TestCase):
    def test_rules_section_is_byte_identical(self):
        """The rules are the experiment's control; only the format spec moves."""
        converted = build_line_format_prompt(SHIPPED)
        self.assertEqual(converted[converted.find("RULES:"):],
                         SHIPPED[SHIPPED.find("RULES:"):])

    def test_json_instruction_does_not_survive(self):
        self.assertNotIn("valid JSON array", build_line_format_prompt(SHIPPED))

    def test_unrecognised_prompt_raises_rather_than_half_converting(self):
        with self.assertRaises(PromptShapeError):
            build_line_format_prompt("No format section here at all.")
        with self.assertRaises(PromptShapeError):
            build_line_format_prompt("")


class FingerprintSeparatesTheArms(unittest.TestCase):
    def _fp(self, params):
        return get_generation_fingerprint("src", ["c"], "m", "u", params, 3000)

    def test_lines_and_json_do_not_share_a_checkpoint(self):
        a = LLMGenParams(system_prompt="S", user_prompt_template="U")
        b = LLMGenParams(system_prompt="S", user_prompt_template="U",
                         output_format="lines")
        self.assertNotEqual(self._fp(a)["settings_sha256"],
                            self._fp(b)["settings_sha256"])

    def test_existing_json_checkpoints_are_not_invalidated(self):
        """Params from before the field existed must hash as they always did."""
        current = LLMGenParams(system_prompt="S", user_prompt_template="U")
        legacy = types.SimpleNamespace(**{
            f: getattr(current, f) for f in
            ("system_prompt", "user_prompt_template", "max_tokens", "temperature",
             "top_p", "top_k", "min_p", "presence_penalty", "banned_tokens",
             "context_length", "hard_max_tokens")})
        self.assertEqual(self._fp(legacy)["settings_sha256"],
                         self._fp(current)["settings_sha256"])


class _StubClient:
    """Minimal stand-in for the OpenAI client call_llm_for_entries makes."""

    def __init__(self, content):
        message = types.SimpleNamespace(content=content, reasoning_content=None)
        choice = types.SimpleNamespace(message=message, finish_reason="stop")
        response = types.SimpleNamespace(choices=[choice], usage=None)
        create = lambda **kwargs: response
        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=create))


class EndToEndThroughTheRealPipeline(unittest.TestCase):
    def test_line_output_reaches_entries(self):
        source = "The room was cold. Tell me the truth."
        reply = ("NARRATOR|Calm.|The room was cold.\n"
                 "ELENA|Firm.|Tell me the truth.")
        entries = generate_script.call_llm_for_entries(
            _StubClient(reply), "m", "sys", "usr", LLMGenParams(),
            log_name="test_responses.log", label="CHUNK 1/1", max_retries=0,
            codec=get_codec("lines"))
        self.assertEqual([e["speaker"] for e in entries], ["NARRATOR", "ELENA"])
        self.assertEqual(" ".join(e["text"] for e in entries), source)

    def test_json_path_is_unchanged_by_the_refactor(self):
        reply = ('[{"speaker": "NARRATOR", "text": "The room was cold.", '
                 '"instruct": "Calm."}]')
        entries = generate_script.call_llm_for_entries(
            _StubClient(reply), "m", "sys", "usr", LLMGenParams(),
            log_name="test_responses.log", label="CHUNK 1/1", max_retries=0)
        self.assertEqual(entries[0]["speaker"], "NARRATOR")
        self.assertEqual(entries[0]["text"], "The room was cold.")


if __name__ == "__main__":
    unittest.main()
