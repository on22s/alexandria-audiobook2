import json
from unittest.mock import patch
import unittest
from types import SimpleNamespace

import three_pass_generate as tp
import attribution_prompt_variants as apv
from attribution_prompt_variants import (VARIANTS, make_provider,
                                                     passage_text, roster_line)
from generate_script import LLMGenParams

FROZEN = [{"type": "NARRATOR", "text": "Ranta slurped his soup."},
          {"type": "SPOKEN", "text": "Tell us already."},
          {"type": "SPOKEN", "text": "Don't underestimate me!"}]
ROSTER = ["HARUHIRO", "RANTA"]
# the window as the harness sees it whole: FROZEN's entries in order with
# their frozen index, plus an unsent narration entry, and text either side
SURROUND = {"entries": [{"type": "NARRATOR", "text": "Ranta slurped his soup.", "n": 0},
                        {"type": "SPOKEN", "text": "Tell us already.", "n": 1},
                        {"type": "NARRATOR", "text": "Nobody answered him.", "n": None},
                        {"type": "SPOKEN", "text": "Don't underestimate me!", "n": 2}],
            "before": "Earlier that day the party had argued.",
            "after": "The fire burned low."}


class _Client:
    def __init__(self):
        self.prompts = []
        self.systems = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kw):
        self.prompts.append(kw["messages"][-1]["content"])
        self.systems.append(kw["messages"][0]["content"])
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
                                     entries_provider=make_provider(variant, [["HARUHIRO", "HARU"]]),
                                     surround=SURROUND)
            self.assertEqual(["NARRATOR", "HARUHIRO", "RANTA"], [e["speaker"] for e in out], variant)
            self.assertEqual("Tell us already.", out[1]["text"])   # text freeze intact
            # continuity makes one extra request after the window: the summary
            # rewrite. The attribution prompt is the one before it.
            prompt = client.prompts[-2] if variant == "continuity" else client.prompts[-1]
            if variant == "continuity":
                self.assertIn("SUMMARY SO FAR", client.prompts[-1])
            if variant in ("aliases", "michel", "michel2"):
                self.assertIn("HARUHIRO (also: HARU)", prompt)
            if variant in ("passage", "michel"):
                self.assertIn('|1|"Tell us already."|1|', prompt)
                self.assertIn("Step 3", prompt)
            elif variant.startswith("michel2"):
                self.assertIn('|1|"Tell us already."|1|', prompt)
                self.assertNotIn("Step 3", prompt)
            else:
                self.assertIn('"n": 1', prompt)

    def test_michel2_full_shows_the_whole_window_and_its_surroundings(self):
        client = _Client()
        tp.attribute_batch(client, "m", FROZEN, _params(), ROSTER,
                           entries_provider=make_provider("michel2_full"), surround=SURROUND)
        prompt = client.prompts[-1]
        self.assertIn("BEFORE THE PASSAGE", prompt)
        self.assertIn("Earlier that day the party had argued.", prompt)
        self.assertIn("Nobody answered him.", prompt)          # unsent narration, unmarked
        self.assertNotIn("[1] Nobody", prompt)
        self.assertIn('|1|"Tell us already."|1|', prompt)
        self.assertIn("AFTER THE PASSAGE", prompt)
        with self.assertRaises(ValueError):
            tp.attribute_batch(_Client(), "m", FROZEN, _params(), ROSTER,
                               entries_provider=make_provider("michel2_full"))

    def test_michel2_shot_puts_the_worked_example_first(self):
        client = _Client()
        tp.attribute_batch(client, "m", FROZEN, _params(), ROSTER,
                           entries_provider=make_provider("michel2_shot"))
        self.assertTrue(client.prompts[-1].startswith("EXAMPLE (a different book)"))
        self.assertIn("ANSWER: [", client.prompts[-1])

    def test_michel2_swaps_the_system_prompt_and_carries_the_tail(self):
        client = _Client()
        provider = make_provider("michel2", [["HARUHIRO", "HARU"]])
        tp.attribute_batch(client, "m", FROZEN, _params(), ROSTER, entries_provider=provider)
        self.assertNotIn("PREVIOUS PASSAGE ENDED", client.prompts[0])
        tp.attribute_batch(client, "m", FROZEN, _params(), ROSTER, entries_provider=provider)
        self.assertIn('HARUHIRO: "Tell us already."', client.prompts[1])
        # the system prompt describes the passage format and keeps the
        # minor-speaker rule the shipped prompt measured +6.5/+9.6 with
        self.assertIn("|n|", client.systems[-1])
        self.assertIn("main characters", client.systems[-1])

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



class PresetTextsTests(unittest.TestCase):
    """A preset carries the variant's texts; None sends exactly the builtin."""

    def test_builtin_texts_are_what_every_variant_sends(self):
        params = _params()
        for variant in apv.USER_VARIANTS:
            surround = {"before": "B", "after": "A"} if variant == "michel2_full" else None
            client = _Client()
            tp.attribute_batch(client, "m", FROZEN, params, ROSTER,
                               entries_provider=make_provider(variant, [["HARUHIRO", "HARU"]]),
                               surround=surround)
            system, body = apv.build_variant_request(
                variant, FROZEN, params, ROSTER, [["HARUHIRO", "HARU"]],
                [{}] * len(FROZEN), surround, None, apv.builtin_texts(variant))
            # continuity makes one extra request after the window (the summary)
            at = -2 if variant == "continuity" else -1
            self.assertEqual(client.systems[at], system, variant)
            self.assertEqual(client.prompts[at], body, variant)

    def test_custom_texts_replace_the_builtin_pieces(self):
        params = _params()
        texts = {"system": "MY SYSTEM", "user": "MY INSTRUCTION", "example": "MY EXAMPLE\n"}
        system, body = apv.build_variant_request("michel2_shot", FROZEN, params, ROSTER,
                                                 texts=texts)
        self.assertEqual("MY SYSTEM", system)
        self.assertTrue(body.startswith("MY EXAMPLE\n"))
        self.assertTrue(body.endswith("MY INSTRUCTION"))
        self.assertIn('|1|"Tell us already."|1|', body)          # the pipeline's part stays
        system, body = apv.build_variant_request(
            "default", FROZEN, params, ROSTER,
            texts={"system": "S", "user": "R={roster} B={batch}", "example": ""})
        self.assertEqual("S", system)
        self.assertTrue(body.startswith("R=HARUHIRO, RANTA B=["))

    def test_default_template_must_keep_its_placeholders(self):
        self.assertIsNone(apv.validate_preset_texts("default", None))
        self.assertIsNone(apv.validate_preset_texts("michel2", {"user": "anything"}))
        self.assertIsNotNone(apv.validate_preset_texts("default", {"user": "no placeholders"}))
        self.assertIsNotNone(apv.validate_preset_texts("continuity", {"user": "{roster} only"}))

    def test_active_preset_resolution(self):
        cfg = {"prompts": {"attribution_preset": "mine"},
               "prompt_presets": [{"name": "mine", "variant": "michel2", "system_prompt": "S",
                                   "user_prompt": "U", "example": ""}]}
        self.assertEqual(("michel2", {"system": "S", "user": "U", "example": ""}, "mine"),
                         apv.resolve_attribution_preset(cfg))
        self.assertEqual(("michel", None, "michel"),
                         apv.resolve_attribution_preset({"prompts": {"attribution_preset": "michel"}}))
        self.assertEqual(("passage", None, "passage"), apv.resolve_attribution_preset(
            {"generation": {"three_pass_attribute_prompt_variant": "passage"}}))
        builtins = apv.builtin_presets()
        self.assertEqual(list(apv.USER_VARIANTS), [b["name"] for b in builtins])
        self.assertTrue(all(b["system_prompt"] and b["user_prompt"] for b in builtins))
        self.assertTrue(next(b for b in builtins if b["name"] == "michel2_shot")["example"])
