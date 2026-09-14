import json
import unittest
from types import SimpleNamespace

import three_pass_generate as tp
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
            prompt = client.prompts[-1]
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
