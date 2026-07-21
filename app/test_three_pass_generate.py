import unittest
import os
import tempfile
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


class EndToEndTests(unittest.TestCase):
    def test_three_passes_assemble_final_entries(self):
        source = "The room was cold. \"Tell me the truth.\""
        seg = [{"type": "NARRATOR", "text": "The room was cold."},
               {"type": "SPOKEN", "text": "Tell me the truth."}]
        named = [{"speaker": "NARRATOR", "text": "The room was cold."},
                 {"speaker": "ELENA", "text": "Tell me the truth."}]
        instructed = [{"speaker": "NARRATOR", "text": "The room was cold.",
                       "instruct": "Cold, still narration."},
                      {"speaker": "ELENA", "text": "Tell me the truth.",
                       "instruct": "Firm, quiet demand."}]
        client = _client_returning([seg, named, instructed])
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        entries = tp.run_three_pass(client, "m", source, params, chunk_size=6000)
        self.assertEqual(2, len(entries))
        self.assertEqual({"speaker", "text", "instruct"}, set(entries[0].keys()))
        self.assertEqual("ELENA", entries[1]["speaker"])
        self.assertEqual("Firm, quiet demand.", entries[1]["instruct"])

    def test_segment_accepts_trigram_only_near_miss_on_exhaustion(self):
        words = [f"word{i}" for i in range(100)]
        source = " ".join(words)
        self.assertEqual([], tp.split_failed_chunk(source))  # unsplittable
        swapped = list(words)
        i = 0
        while i + 1 < len(swapped):
            swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
            i += 25
        near = [{"type": "NARRATOR", "text": " ".join(swapped)}]
        client = _client_returning([near, near, near, near, near, near, near])
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        out = tp.segment_chunk_adaptively(client, "m", source, params)
        self.assertTrue(out)
        self.assertFalse(tp.validate_segment_quality(source, out)["passed"])


class CheckpointTests(unittest.TestCase):
    def _payloads(self):
        seg = [{"type": "NARRATOR", "text": "The room was cold."},
               {"type": "SPOKEN", "text": "Tell me the truth."}]
        named = [{"speaker": "NARRATOR", "text": "The room was cold."},
                 {"speaker": "ELENA", "text": "Tell me the truth."}]
        instructed = [{"speaker": "NARRATOR", "text": "The room was cold.",
                       "instruct": "Cold."},
                      {"speaker": "ELENA", "text": "Tell me the truth.",
                       "instruct": "Firm."}]
        return seg, named, instructed

    def test_completed_stage_is_not_recomputed_on_resume(self):
        source = "The room was cold. \"Tell me the truth.\""
        seg, named, instructed = self._payloads()
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "book.json")
            crashing = _client_returning([seg])  # only pass-1 payload; pass 2 exhausts retries
            with self.assertRaises(tp.PassExhausted):
                tp.run_three_pass(crashing, "m", source, params, chunk_size=6000,
                                  output_path=out)
            cp = tp.three_pass_checkpoint_path(out)
            self.assertTrue(os.path.exists(cp))
            resume_client = _client_returning([named, instructed])
            entries = tp.run_three_pass(resume_client, "m", source, params,
                                        chunk_size=6000, output_path=out)
            self.assertEqual(2, len(entries))
            self.assertEqual("ELENA", entries[1]["speaker"])


if __name__ == "__main__":
    unittest.main()


class FreezeEnforcementTests(unittest.TestCase):
    def test_attribute_reconstructs_text_byte_exact_from_frozen(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me the truth."}]
        # LLM echoes normalized-equal but corrupted text (zero-width + spaces)
        evil = [{"speaker": "ELENA", "text": "Tell me the truth.​  "}]
        p = LLMGenParams(system_prompt="s", user_prompt_template="{roster}{batch}",
                         max_tokens=500, temperature=0.1)
        out = tp.attribute_batch(_client_returning([evil]), "m", frozen, p, roster=[])
        self.assertEqual("Tell me the truth.", out[0]["text"])
        self.assertEqual("ELENA", out[0]["speaker"])

    def test_instruct_reconstructs_speaker_and_text_from_prior(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        # Passes validation (same speaker, normalize-equal text) but text carries
        # an injected zero-width space; reconstruction must restore prior's text.
        echoed = [{"speaker": "ELENA", "text": "Tell me.​", "instruct": "Firm."}]
        p = LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                         max_tokens=500, temperature=0.1)
        out = tp.instruct_batch(_client_returning([echoed]), "m", prior, p)
        self.assertEqual("ELENA", out[0]["speaker"])
        self.assertEqual("Tell me.", out[0]["text"])  # byte-exact, no zero-width
        self.assertEqual("Firm.", out[0]["instruct"])
