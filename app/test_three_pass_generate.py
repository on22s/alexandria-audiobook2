import unittest
import three_pass_generate as tp
import json
from types import SimpleNamespace
from generate_script import LLMGenParams


class PassHelperTests(unittest.TestCase):
    def test_batches_split_entries_by_size(self):
        entries = [{"text": str(i)} for i in range(55)]
        batches = list(tp.iter_entry_batches(entries, batch_size=25))
        self.assertEqual([25, 25, 5], [len(b) for b in batches])

    def test_roster_collects_uppercase_non_narrator_speakers(self):
        entries = [{"speaker": "NARRATOR"}, {"speaker": "ELENA"},
                   {"speaker": "MARCUS"}, {"speaker": "ELENA"}, {"speaker": "UNKNOWN"}]
        self.assertEqual(["ELENA", "MARCUS"], tp.build_roster(entries))

    def test_default_instruct_by_type(self):
        self.assertEqual("Neutral, even narration.",
                         tp.default_instruct({"speaker": "NARRATOR", "text": "x"}))
        self.assertEqual("Natural, in-character delivery.",
                         tp.default_instruct({"speaker": "ELENA", "text": "x"}))


def _client_returning(payloads):
    """LM Studio stub: each call returns the next payload as JSON content."""
    responses = iter(payloads)

    def create(**_kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(
            message=SimpleNamespace(content=json.dumps(next(responses))),
            finish_reason="stop")], usage=None)

    return SimpleNamespace(chat=SimpleNamespace(
        completions=SimpleNamespace(create=create)))


class Pass2Tests(unittest.TestCase):
    def _params(self):
        return LLMGenParams(system_prompt="s", user_prompt_template="{roster}{batch}",
                            max_tokens=500, temperature=0.1)

    def test_attributes_a_batch_and_freezes_text(self):
        frozen = [{"type": "NARRATOR", "text": "The room was cold."},
                  {"type": "SPOKEN", "text": "Tell me."}]
        good = [{"speaker": "NARRATOR", "text": "The room was cold."},
                {"speaker": "ELENA", "text": "Tell me."}]
        client = _client_returning([good])
        out = tp.attribute_batch(client, "m", frozen, self._params(), roster=[])
        self.assertEqual(["NARRATOR", "ELENA"], [e["speaker"] for e in out])
        self.assertEqual("Tell me.", out[1]["text"])

    def test_pass2_fail_mode_raises_when_exhausted(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me."}]
        bad = [{"speaker": "NARRATOR", "text": "Tell me."}]
        client = _client_returning([bad, bad, bad, bad])
        with self.assertRaises(tp.PassExhausted):
            tp.attribute_batch(client, "m", frozen, self._params(), roster=[],
                               max_retries=1, on_exhaustion="fail")


class Pass3Tests(unittest.TestCase):
    def _params(self):
        return LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                            max_tokens=500, temperature=0.1)

    def test_adds_instruct_and_freezes(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        good = [{"speaker": "ELENA", "text": "Tell me.", "instruct": "Firm, quiet."}]
        client = _client_returning([good])
        out = tp.instruct_batch(client, "m", prior, self._params())
        self.assertEqual("Firm, quiet.", out[0]["instruct"])

    def test_falls_back_to_default_instruct_on_exhaustion(self):
        prior = [{"speaker": "NARRATOR", "text": "The room was cold."}]
        bad = [{"speaker": "NARRATOR", "text": "The room was cold.", "instruct": ""}]
        client = _client_returning([bad, bad])
        out = tp.instruct_batch(client, "m", prior, self._params(), max_retries=1)
        self.assertEqual("Neutral, even narration.", out[0]["instruct"])
        self.assertEqual("The room was cold.", out[0]["text"])


if __name__ == "__main__":
    unittest.main()
