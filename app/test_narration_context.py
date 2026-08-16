"""Pass 2 must see the narration, because that is where the dialogue tags are.

Narration needs no LLM to resolve, so it used to be filtered out of the
attribution batch entirely and the model was handed a wall of bare quotes.
Measured against 147 hand-judged lines of mushoku16, 22% of the pipeline's
wrong answers had the true speaker's name in a narration line adjacent to the
one it was judging - "... Nina said it with a small voice." was answered
UNKNOWN. Keeping narration in the batch moved that gold set from 29.9% to
37.0%. It is sent for company, not for an answer.
"""
import json
import unittest
from types import SimpleNamespace

import three_pass_generate as tp
from generate_script import LLMGenParams


def _client(responses):
    queue = list(responses)
    seen = []
    def create(**kwargs):
        seen.append(kwargs["messages"][-1]["content"])
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(queue.pop(0))),
            finish_reason="stop")], usage=None)
    client = SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))
    return client, seen


SOURCE = 'Nina said it with a small voice. "Hey, Eris."'
SEGMENTED = [{"type": "NARRATOR", "text": "Nina said it with a small voice."},
             {"type": "SPOKEN", "text": "Hey, Eris."}]


class NarrationIsSentAsContextTest(unittest.TestCase):
    def _run(self, attribution):
        client, seen = _client([SEGMENTED, attribution,
                                [{"n": 0, "instruct": "Soft."},
                                 {"n": 1, "instruct": "Warm."}]])
        entries = tp.run_three_pass(
            client, "m", SOURCE, LLMGenParams(max_tokens=500, temperature=0.1),
            chunk_size=6000)
        return entries, seen

    def test_the_dialogue_tag_reaches_the_attribution_prompt(self):
        entries, seen = self._run([{"n": 0, "speaker": "NARRATOR"},
                                   {"n": 1, "speaker": "NINA"}])
        self.assertIn("Nina said it with a small voice.", seen[1])
        self.assertEqual(["NARRATOR", "NINA"], [e["speaker"] for e in entries])

    def test_a_mislabelled_narration_line_costs_a_retry_and_still_ends_NARRATOR(self):
        # Sending narration for an answer means the model can get it wrong, and
        # validate_attribution rejects the whole batch when it does. This is the
        # price of the context: one retry, then the known speaker stands.
        client, seen = _client([SEGMENTED,
                                [{"n": 0, "speaker": "ERIS"},
                                 {"n": 1, "speaker": "NINA"}],
                                [{"n": 0, "speaker": "NARRATOR"},
                                 {"n": 1, "speaker": "NINA"}],
                                [{"n": 0, "instruct": "Soft."},
                                 {"n": 1, "instruct": "Warm."}]])
        entries = tp.run_three_pass(
            client, "m", SOURCE, LLMGenParams(max_tokens=500, temperature=0.1),
            chunk_size=6000)
        self.assertEqual(["NARRATOR", "NINA"], [e["speaker"] for e in entries])
        self.assertEqual(4, len(seen))

    def test_narration_never_reaches_the_roster(self):
        entries, _ = self._run([{"n": 0, "speaker": "NARRATOR"},
                                {"n": 1, "speaker": "NINA"}])
        self.assertEqual(["NINA"], tp.build_roster(entries))


class NarrationOnlyBatchTest(unittest.TestCase):
    def test_a_batch_with_nothing_to_attribute_makes_no_llm_call(self):
        # Context with no line to give context to is pure cost.
        source = "The room was cold. The fire had gone out."
        seg = [{"type": "NARRATOR", "text": "The room was cold."},
               {"type": "NARRATOR", "text": "The fire had gone out."}]
        client, seen = _client([seg, [{"n": 0, "instruct": "Flat."},
                                      {"n": 1, "instruct": "Flat."}]])
        entries = tp.run_three_pass(
            client, "m", source, LLMGenParams(max_tokens=500, temperature=0.1),
            chunk_size=6000)
        self.assertEqual(["NARRATOR", "NARRATOR"], [e["speaker"] for e in entries])
        # segment + instruct only: no attribution call was made.
        self.assertEqual(2, len(seen))


if __name__ == "__main__":
    unittest.main()
