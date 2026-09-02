"""The two-step path must not be able to lose content quietly.

Splitting annotation from serialisation adds a second place text can go
missing. The gate scores the FINAL entries against the ORIGINAL chunk, so a
stage-2 drop has to fail exactly like a stage-1 truncation. Each test pins one
way that could stop being true.
"""
import types
import unittest

import generate_script
from generate_script import LLMGenParams, get_generation_fingerprint
from response_codecs import FREEFORM_KEY, get_codec
from two_step import (CONVERSION_SYSTEM, PromptShapeError,
                      build_conversion_prompt, build_freeform_prompt)

SHIPPED = ("You are a script writer. Output ONLY valid JSON arrays.\n\n"
           "FORMAT:\n[\n  {\"speaker\": \"X\"}\n]\n\n"
           "RULES:\n1. PRESERVE THE AUTHOR'S TEXT.\n")
CHUNK = "The room had gone cold. Tell me the truth. There is nothing to tell."
FREEFORM_REPLY = (
    "SPEAKER: NARRATOR\nDIRECTION: Quiet.\nTEXT: The room had gone cold.\n\n"
    "SPEAKER: ELENA\nDIRECTION: Firm.\nTEXT: Tell me the truth.\n\n"
    "SPEAKER: MARCUS\nDIRECTION: Flat.\nTEXT: There is nothing to tell.")
FULL_JSON = ('[{"speaker":"NARRATOR","text":"The room had gone cold.","instruct":"Quiet."},'
             '{"speaker":"ELENA","text":"Tell me the truth.","instruct":"Firm."},'
             '{"speaker":"MARCUS","text":"There is nothing to tell.","instruct":"Flat."}]')
SHORT_JSON = ('[{"speaker":"NARRATOR","text":"The room had gone cold.","instruct":"Quiet."}]')


class _ScriptedClient:
    """Returns queued replies in order, so stage 1 and stage 2 can differ."""

    def __init__(self, *replies):
        self.replies = list(replies)
        self.seen = []

        def create(**kwargs):
            self.seen.append(kwargs)
            content = self.replies.pop(0) if self.replies else ""
            message = types.SimpleNamespace(content=content, reasoning_content=None)
            return types.SimpleNamespace(
                choices=[types.SimpleNamespace(message=message, finish_reason="stop")],
                usage=None)

        self.chat = types.SimpleNamespace(
            completions=types.SimpleNamespace(create=create))


def run_two_step(client):
    params = LLMGenParams(system_prompt=SHIPPED, user_prompt_template="{context}{chunk}",
                          output_format="two_step")
    return generate_script.process_chunk(client, "m", CHUNK, 1, 1, params,
                                         max_retries=0)


class FreeformCodec(unittest.TestCase):
    def test_wraps_the_whole_reply(self):
        codec = get_codec("freeform")
        self.assertEqual(codec.parse(codec.extract("anything at all")),
                         [{FREEFORM_KEY: "anything at all"}])

    def test_empty_reply_is_a_failed_attempt_not_an_empty_success(self):
        codec = get_codec("freeform")
        self.assertEqual(codec.parse(codec.extract("   \n ")), [])


class Prompts(unittest.TestCase):
    def test_stage1_keeps_rules_and_drops_the_json_demand(self):
        p = build_freeform_prompt(SHIPPED)
        self.assertEqual(p[p.find("RULES:"):], SHIPPED[SHIPPED.find("RULES:"):])
        self.assertNotIn("valid JSON array", p)

    def test_stage1_refuses_an_unrecognised_prompt(self):
        with self.assertRaises(PromptShapeError):
            build_freeform_prompt("no format section")

    def test_stage2_refuses_empty_input(self):
        with self.assertRaises(PromptShapeError):
            build_conversion_prompt("   ")

    def test_stage2_is_never_shown_the_source_chunk(self):
        prompt = build_conversion_prompt(FREEFORM_REPLY)
        self.assertIn("SPEAKER: NARRATOR", prompt)
        self.assertNotIn("RULES:", prompt)


class EndToEnd(unittest.TestCase):
    def test_both_stages_run_and_produce_entries(self):
        client = _ScriptedClient(FREEFORM_REPLY, FULL_JSON)
        entries = run_two_step(client)
        self.assertEqual([e["speaker"] for e in entries],
                         ["NARRATOR", "ELENA", "MARCUS"])
        self.assertEqual(len(client.seen), 2, "expected exactly two LLM calls")

    def test_stage2_gets_stage1_output_not_the_chunk(self):
        client = _ScriptedClient(FREEFORM_REPLY, FULL_JSON)
        run_two_step(client)
        stage2_user = client.seen[1]["messages"][-1]["content"]
        self.assertIn("SPEAKER: NARRATOR", stage2_user)
        self.assertEqual(client.seen[1]["messages"][0]["content"], CONVERSION_SYSTEM)

    def test_a_stage2_drop_is_caught_by_the_gate(self):
        """The whole safety property: serialising away two thirds must fail."""
        client = _ScriptedClient(FREEFORM_REPLY, SHORT_JSON)
        self.assertEqual(run_two_step(client), [])

    def test_empty_stage1_fails_without_calling_stage2(self):
        client = _ScriptedClient("", "")
        self.assertEqual(run_two_step(client), [])
        self.assertEqual(len(client.seen), 1, "stage 2 must not run on empty stage 1")


class Fingerprint(unittest.TestCase):
    def test_two_step_does_not_share_a_checkpoint_with_json(self):
        a = LLMGenParams(system_prompt="S", user_prompt_template="U")
        b = LLMGenParams(system_prompt="S", user_prompt_template="U",
                         output_format="two_step")
        fp = lambda p: get_generation_fingerprint("src", ["c"], "m", "u", p, 3000)
        self.assertNotEqual(fp(a)["settings_sha256"], fp(b)["settings_sha256"])


if __name__ == "__main__":
    unittest.main()
