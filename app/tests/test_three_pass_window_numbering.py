""""window x of n" counts only model requests (#1925). Synthetic text only.

What this verifies, through a real run_three_pass with a stub model that
answers whatever window it is sent:
- pass 2 numbers only windows that hold a line to attribute: a
  narration-only window is neither numbered nor totalled, so the numbers
  announced while waiting run 1..n with n equal to the requests actually made;
- every "asking the model" line is followed by its "done" line with the same
  numbers, and n matches the pass total the ETA line reports;
- on main before #1925 the same run announced "window 1 of 3" for pass 2,
  which makes two requests (checked by running this test against it).
"""
import contextlib
import io
import json
import re
from types import SimpleNamespace
import unittest

import three_pass_generate as tp
from generate_script import LLMGenParams
from tests.test_three_pass_generate import _load_orchestration_fixture_cast

ASK_RE = re.compile(r"^Step (\d) \(\w+\): (?:window|chunk) (\d+) of (\d+) - asking the model$")
DONE_RE = re.compile(r"^Step (\d) \(\w+\): (?:window|chunk) (\d+)/(\d+) done - ")


def answering_client():
    """Reply to each request from the entries it carries."""
    calls = []

    def create(**kwargs):
        prompt = kwargs["messages"][-1]["content"]
        entries = json.loads(prompt[prompt.rindex("[{"):prompt.rindex("}]") + 2])
        attribution = "Assign a speaker" in prompt
        calls.append("attribute" if attribution else "instruct")
        reply = [{"n": entry["n"], "head": " ".join(entry["text"].split()[:3]),
                  **({"speaker": "NARRATOR" if entry.get("type") == "NARRATOR" else "ELENA"}
                     if attribution else {"instruct": "Calm, even delivery."})}
                 for entry in entries]
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(reply)), finish_reason="stop")], usage=None)

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


SOURCE = " ".join([
    "The room was cold.", "The fire had gone out.",          # narration only
    "\"Tell me the truth.\"", "She waited by the door.",      # a spoken line
    "The wind pressed at the glass.", "Nobody moved at all.",  # narration only
    "\"I already did.\"", "He did not look up.",               # a spoken line
])


class WindowNumberingTests(unittest.TestCase):
    def run_pipeline(self):
        client, calls = answering_client()
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            tp.run_three_pass(client, "m", SOURCE,
                              LLMGenParams(max_tokens=800, temperature=0.1, segmentation="auto"),
                              chunk_size=6000, cast=_load_orchestration_fixture_cast(),
                              attribute_batch_size=2)
        return out.getvalue().splitlines(), calls

    def test_pass_two_numbers_only_windows_that_ask_the_model(self):
        lines, calls = self.run_pipeline()
        asked = [tuple(map(int, m.groups())) for m in map(ASK_RE.match, lines) if m]
        done = [tuple(map(int, m.groups())) for m in map(DONE_RE.match, lines) if m]
        for step, kind in ((2, "attribute"), (3, "instruct")):
            with self.subTest(step=step):
                step_asked = [(x, n) for s, x, n in asked if s == step]
                requests = calls.count(kind)
                self.assertGreater(requests, 0)
                self.assertEqual(list(range(1, requests + 1)), [x for x, _ in step_asked])
                self.assertEqual({requests}, {n for _, n in step_asked})
                self.assertEqual(step_asked, [(x, n) for s, x, n in done if s == step])
        # Pass 2 has three windows of two lines; the middle one is narration
        # only. Before #1925 this run announced "window 1 of 3" for a pass that
        # makes two requests.
        self.assertEqual([(2, 1, 2), (2, 2, 2), (3, 1, 1)], asked)


if __name__ == "__main__":
    unittest.main()
