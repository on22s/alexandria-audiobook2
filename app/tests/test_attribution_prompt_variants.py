import json
from unittest.mock import patch
import unittest
from types import SimpleNamespace

import three_pass_generate as tp
import experiments.attribution_prompt_variants as apv
from experiments.attribution_prompt_variants import (VARIANTS, make_provider,
                                                     passage_text, roster_line)
from generate_script import LLMGenParams

FROZEN = [{"type": "NARRATOR", "text": "Ranta slurped his soup."},
          {"type": "SPOKEN", "text": "Tell us already."},
          {"type": "SPOKEN", "text": "Don't underestimate me!"}]
ROSTER = ["HARUHIRO", "RANTA"]


class _Client:
    def __init__(self):
        self.prompts = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.prompts.append(kw["messages"][-1]["content"])
        good = [{"n": 0, "speaker": "NARRATOR"}, {"n": 1, "speaker": "HARUHIRO"}, {"n": 2, "speaker": "RANTA"}]
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(good)), finish_reason="stop")], usage=None)


def _params():
    # the product's own templates, so the roster line the variants rewrite is present
    return LLMGenParams(structured_output="off")


class PromptVariants(unittest.TestCase):
    def test_roster_line_and_passage_rendering(self):
        self.assertEqual("HARUHIRO (also: HARU), RANTA", roster_line(ROSTER, [["HARUHIRO", "HARU"]]))
        text = passage_text(FROZEN)
        self.assertIn('|1|"Tell us already."|1|', text)
        self.assertIn("[0] Ranta slurped his soup.", text)

    def test_every_variant_keeps_the_output_contract(self):
        for variant in VARIANTS[1:]:
            client = _Client()
            out = tp.attribute_batch(client, "m", FROZEN, _params(), ROSTER,
                                     entries_provider=make_provider(variant, [["HARUHIRO", "HARU"]]))
            self.assertEqual(["NARRATOR", "HARUHIRO", "RANTA"], [e["speaker"] for e in out], variant)
            self.assertEqual("Tell us already.", out[1]["text"])   # text freeze intact
            # continuity makes one extra request after the window: the summary
            # rewrite. The attribution prompt is the one before it.
            prompt = client.prompts[-2] if variant == "continuity" else client.prompts[-1]
            if variant == "continuity":
                self.assertIn("SUMMARY SO FAR", client.prompts[-1])
            if variant in ("aliases", "michel"):
                self.assertIn("HARUHIRO (also: HARU)", prompt)
            if variant in ("passage", "michel"):
                self.assertIn('|1|"Tell us already."|1|', prompt)
                self.assertIn("Step 3", prompt)
            else:
                self.assertIn('"n": 1', prompt)

    def test_incremental_carries_the_previous_window(self):
        client = _Client()
        provider = make_provider("incremental")
        tp.attribute_batch(client, "m", FROZEN, _params(), ROSTER, entries_provider=provider)
        self.assertNotIn("already decided", client.prompts[0])
        tp.attribute_batch(client, "m", FROZEN, _params(), ROSTER, entries_provider=provider)
        self.assertIn("already decided", client.prompts[1])
        self.assertIn("1: HARUHIRO; 2: RANTA", client.prompts[1])

    def test_unknown_variant_is_refused(self):
        with self.assertRaises(ValueError):
            make_provider("shouty")


if __name__ == "__main__":
    unittest.main()


class VariantProviderKeepsNarrationTests(unittest.TestCase):
    """The harness sends SPOKEN lines only and carries the narration in each
    entry's previous/next context. A provider that rebuilds the request
    without them shows the model dialogue with no narration - the 2026-09-14
    run scored 25% on four arms against 63% for the canonical prompt."""

    def test_aliases_request_is_the_canonical_request_with_contexts(self):
        from three_pass_generate import build_attribute_request
        frozen = [{"type": "SPOKEN", "text": "Tell us already."},
                  {"type": "SPOKEN", "text": "Fine."}]
        ctx = [{"previous_context": {"type": "NARRATOR", "text": "Haruhiro sighed."},
                "next_context": {"type": "NARRATOR", "text": "Ranta grinned."}},
               {"previous_context": {"type": "NARRATOR", "text": "Ranta grinned."},
                "next_context": None}]
        params = LLMGenParams(max_tokens=64, context_length=4096)
        _, canonical = build_attribute_request(frozen, params, ["HARUHIRO", "RANTA"], ctx)
        bodies = []
        with patch.object(apv, "call_llm_for_entries",
                          lambda c, m, s, body, p, **kw: bodies.append(body)):
            apv.make_provider("aliases")(None, "m", "sys", "user", params, "l", "A", 1, None, None,
                                          frozen, roster=["HARUHIRO", "RANTA"], neighbor_contexts=ctx)
            apv.make_provider("passage")(None, "m", "sys", "user", params, "l", "A", 1, None, None,
                                          frozen, roster=["HARUHIRO", "RANTA"], neighbor_contexts=ctx)
        self.assertEqual(canonical, bodies[0])
        self.assertIn("Haruhiro sighed.", bodies[1])
        self.assertIn("Ranta grinned.", bodies[1])
        self.assertEqual(1, bodies[1].count("Ranta grinned."), "shared context is interleaved once")


class MentionedRosterTests(unittest.TestCase):
    """--roster-mode mentioned: only names attested in the window (by name or
    alias) or carried from the previous window's speakers are shown."""

    def test_attested_by_alias_or_carried_only_in_roster_order(self):
        from experiments.lora_serving_eval import mentioned_roster
        roster = ["ABAEL", "HARUHIRO", "RANTA", "SHIHORU"]
        groups = [{"haruhiro", "haru"}]
        shown = mentioned_roster(roster, groups, ["\"Haru!\" Ranta shouted."], carried=["SHIHORU", "NOBODY"])
        self.assertEqual(["HARUHIRO", "RANTA", "SHIHORU"], shown)
        self.assertEqual([], mentioned_roster(roster, groups, ["nothing here"]))
        self.assertEqual(["ABAEL"], mentioned_roster(roster, groups, ["abael spoke"] , cap=1))


class ContinuityVariantTests(unittest.TestCase):
    """continuity: window 1 gets no preamble; window 2 gets the summary the
    model wrote after window 1 and window 1's last lines with their speakers,
    and the summary call is one extra request per window."""

    def test_second_window_carries_summary_and_tail(self):
        from types import SimpleNamespace
        frozen = [{"type": "SPOKEN", "text": "Tell us already."},
                  {"type": "SPOKEN", "text": "Fine."}]
        ctx = [{"previous_context": {"type": "NARRATOR", "text": "Haruhiro sighed."}, "next_context": None},
               {"previous_context": None, "next_context": None}]
        params = LLMGenParams(max_tokens=64, context_length=4096)
        summary_calls = []

        class _Completions:
            def create(self, **kw):
                summary_calls.append(kw)
                return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
                    content="Haruhiro and Ranta argue in camp."))])
        client = SimpleNamespace(chat=SimpleNamespace(completions=_Completions()))
        bodies = []

        def fake_call(c, m, sysp, body, p, **kw):
            bodies.append(body)
            return [{"n": 0, "speaker": "RANTA"}, {"n": 1, "speaker": "HARUHIRO"}]
        provider = apv.make_provider("continuity")
        with patch.object(apv, "call_llm_for_entries", fake_call):
            provider(client, "m", "sys", "user", params, "l", "A", 1, None, None, frozen,
                     roster=["HARUHIRO", "RANTA"], neighbor_contexts=ctx)
            provider(client, "m", "sys", "user", params, "l", "A", 1, None, None, frozen,
                     roster=["HARUHIRO", "RANTA"], neighbor_contexts=ctx)
        self.assertNotIn("STORY SO FAR", bodies[0])
        self.assertIn("STORY SO FAR", bodies[1])
        self.assertIn("Haruhiro and Ranta argue in camp.", bodies[1])
        self.assertIn('RANTA: "Tell us already."', bodies[1])
        self.assertEqual(2, len(summary_calls), "one summary call per window")
        self.assertIn("Haruhiro sighed.", summary_calls[0]["messages"][0]["content"],
                      "the summary sees the narration, not only the spoken lines")
        self.assertEqual("none", summary_calls[0]["extra_body"]["reasoning_effort"])

