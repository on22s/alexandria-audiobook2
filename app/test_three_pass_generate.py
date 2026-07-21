import re
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

    def test_next_attribute_batch_isolates_duplicate_text(self):
        seg = [{"type": "SPOKEN", "text": "Yes."},
               {"type": "SPOKEN", "text": "Yes."},
               {"type": "NARRATOR", "text": "He left the room."}]
        b0 = tp.next_attribute_batch(seg, 0)
        self.assertEqual(["Yes."], [e["text"] for e in b0])  # 2nd "Yes." excluded
        b1 = tp.next_attribute_batch(seg, 1)
        self.assertEqual(["Yes.", "He left the room."], [e["text"] for e in b1])

    def test_next_attribute_batch_caps_at_batch_size(self):
        seg = [{"type": "NARRATOR", "text": f"line {i}"} for i in range(60)]
        self.assertEqual(tp.BATCH_SIZE, len(tp.next_attribute_batch(seg, 0)))

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

    def test_attribute_preserves_pause_after_and_drops_type(self):
        frozen = [{"type": "NARRATOR", "text": "The room was cold.", "pause_after": 1000}]
        named = [{"speaker": "NARRATOR", "text": "The room was cold."}]
        p = LLMGenParams(system_prompt="s", user_prompt_template="{roster}{batch}",
                         max_tokens=500, temperature=0.1)
        out = tp.attribute_batch(_client_returning([named]), "m", frozen, p, roster=[])
        self.assertEqual(1000, out[0]["pause_after"])
        self.assertNotIn("type", out[0])
        self.assertEqual("NARRATOR", out[0]["speaker"])

    def test_attribute_fallback_preserves_pause_after(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me.", "pause_after": 500}]
        bad = [{"speaker": "NARRATOR", "text": "Tell me."}]  # never names the spoken line
        p = LLMGenParams(system_prompt="s", user_prompt_template="{roster}{batch}",
                         max_tokens=500, temperature=0.1)
        out = tp.attribute_batch(_client_returning([bad]), "m", frozen, p, roster=[],
                                 max_retries=1, on_exhaustion="fallback")
        self.assertEqual(500, out[0]["pause_after"])
        self.assertNotIn("type", out[0])

    def test_instruct_preserves_pause_after(self):
        prior = [{"speaker": "NARRATOR", "text": "The room was cold.", "pause_after": 1000}]
        good = [{"speaker": "NARRATOR", "text": "The room was cold.", "instruct": "Cold."}]
        p = LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                         max_tokens=500, temperature=0.1)
        out = tp.instruct_batch(_client_returning([good]), "m", prior, p)
        self.assertEqual(1000, out[0]["pause_after"])
        self.assertEqual("Cold.", out[0]["instruct"])

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


class ContextBleedTests(unittest.TestCase):
    def test_bleed_helper_flags_context_only_entry(self):
        chunk = " ".join(f"c{i}" for i in range(200))
        ctx = "the quiet harbor lay still under the morning fog and gulls"
        entries = [{"type": "NARRATOR", "text": chunk},
                   {"type": "SPOKEN", "text": ctx}]
        self.assertTrue(tp._output_has_context_bleed(entries, chunk, ctx, ""))

    def test_bleed_helper_ignores_short_generic_line(self):
        chunk = " ".join(f"c{i}" for i in range(200))
        entries = [{"type": "NARRATOR", "text": chunk}, {"type": "SPOKEN", "text": "Yes."}]
        self.assertFalse(tp._output_has_context_bleed(entries, chunk, "Yes.", ""))

    def test_context_rescue_rejects_bleeding_output(self):
        chunk = " ".join(f"c{i}" for i in range(200))
        ctx = "the quiet harbor lay still under the morning fog and gulls"
        # Target-correct segmentation PLUS a leaked context sentence: passes
        # recall/trigram/ratio but must be rejected as context bleed -> [].
        payload = [{"type": "NARRATOR", "text": chunk},
                   {"type": "SPOKEN", "text": ctx}]
        client = _client_returning([payload, payload, payload])
        params = LLMGenParams(system_prompt="s", max_tokens=800, temperature=0.1)
        out = tp.segment_chunk_with_context(client, "m", chunk, ctx, "", params,
                                            max_retries=1)
        self.assertEqual([], out)


class RescueBudgetTests(unittest.TestCase):
    def test_window_fits_returns_true_when_context_unknown(self):
        p = LLMGenParams(max_tokens=500, temperature=0.1)  # context_length None
        self.assertTrue(tp._rescue_prompt_fits("x" * 100000, "y" * 6000, "", 500, p))

    def test_oversized_window_is_skipped_and_no_call_made(self):
        big = "c " * 15000  # ~30k chars -> prompt+output tokens >> 8192
        calls = {"n": 0}

        def create(**_kwargs):
            calls["n"] += 1
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="[]"), finish_reason="stop")], usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        p = LLMGenParams(max_tokens=4000, temperature=0.1, context_length=8192)
        out = tp.rescue_chunk_with_context(client, "m", [big], 0, p)
        self.assertEqual([], out)
        self.assertEqual(0, calls["n"], "no LLM call should be made for over-budget windows")


class ManifestTests(unittest.TestCase):
    def test_manifest_records_clean_resolution_counts_and_timing(self):
        source = "The room was cold. \"Tell me the truth.\""
        seg = [{"type": "NARRATOR", "text": "The room was cold."},
               {"type": "SPOKEN", "text": "Tell me the truth."}]
        named = [{"speaker": "NARRATOR", "text": "The room was cold."},
                 {"speaker": "ELENA", "text": "Tell me the truth."}]
        instructed = [{"speaker": "NARRATOR", "text": "The room was cold.",
                       "instruct": "Cold."},
                      {"speaker": "ELENA", "text": "Tell me the truth.",
                       "instruct": "Firm."}]
        client = _client_returning([seg, named, instructed])
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "book.json")
            tp.run_three_pass(client, "m", source, params, chunk_size=6000, output_path=out)
            man = json.load(open(tp.three_pass_manifest_path(out)))
        self.assertEqual("complete", man["status"])
        self.assertEqual("clean", man["chunks"][0]["resolution"])
        self.assertEqual(0, man["counts"]["context_rescued"])
        self.assertEqual(0, man["counts"]["near_miss_accepted"])
        for pass_name in ("segment", "attribute", "instruct"):
            self.assertIn("elapsed_s", man["passes"][pass_name])
            self.assertEqual("complete", man["passes"][pass_name]["status"])

    def test_manifest_written_on_failure_with_failing_chunk(self):
        source = "The room was cold. \"Tell me the truth.\""
        seg = [{"type": "NARRATOR", "text": "The room was cold."},
               {"type": "SPOKEN", "text": "Tell me the truth."}]
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "book.json")
            crashing = _client_returning([seg])  # pass 2 exhausts -> failure
            with self.assertRaises(tp.PassExhausted):
                tp.run_three_pass(crashing, "m", source, params=LLMGenParams(
                    max_tokens=500, temperature=0.1), chunk_size=6000, output_path=out)
            man = json.load(open(tp.three_pass_manifest_path(out)))
        self.assertEqual("failed", man["status"])
        self.assertEqual("attribute", man["failed_pass"])

    def test_resolution_sink_records_near_miss(self):
        words = [f"word{i}" for i in range(100)]
        source = " ".join(words)
        self.assertEqual([], tp.split_failed_chunk(source))
        swapped = list(words)
        i = 0
        while i + 1 < len(swapped):
            swapped[i], swapped[i + 1] = swapped[i + 1], swapped[i]
            i += 25
        near = [{"type": "NARRATOR", "text": " ".join(swapped)}]
        client = _client_returning([near] * 7)
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        sink = []
        out = tp.segment_chunk_adaptively(client, "m", source, params, resolution_sink=sink)
        self.assertTrue(out)
        self.assertEqual(["near_miss"], sink)


class RecombinationAcceptanceTests(unittest.TestCase):
    def _splittable_source(self, n=400):
        words = [f"w{i}" for i in range(n)]
        return words, ". ".join(" ".join(words[i:i+8]) for i in range(0, n, 8)) + "."

    def test_accepts_recombination_when_trigram_only_defect_at_seam(self):
        # Full chunk fails hard (no near-miss) -> splits. Each half echoes all its
        # words but lightly reordered -> each half is recall-1.0/trigram-reduced
        # (passes its own near-miss gate), and the recombined whole is
        # recall-1.0 with trigram below the gate: a trigram-only seam defect the
        # fix must accept instead of discarding both good halves.
        words, source = self._splittable_source(400)
        self.assertTrue(tp.split_failed_chunk(source))  # confirm it splits

        def create(**kwargs):
            cw = re.findall(r"w\d+", kwargs["messages"][-1]["content"])
            if len(cw) > 300:                      # full chunk -> truncate hard
                payload = [{"type": "NARRATOR", "text": " ".join(cw[:5])}]
            else:                                   # split half -> all words, reordered
                sw = list(cw); i = 0
                while i + 1 < len(sw):
                    sw[i], sw[i+1] = sw[i+1], sw[i]; i += 25
                payload = [{"type": "NARRATOR", "text": " ".join(sw)}]
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(payload)),
                finish_reason="stop")], usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        params = LLMGenParams(max_tokens=800, temperature=0.1)
        out = tp.segment_chunk_adaptively(client, "m", source, params)
        self.assertTrue(out, "recombination with trigram-only seam defect must be accepted")
        q = tp.validate_segment_quality(source, out)
        self.assertGreaterEqual(q["metrics"]["source_token_recall"], 0.9)  # content all there

    def test_recombination_floor_predicate_rejects_below_floor(self):
        # The recombination branch now gates on is_trigram_only_near_miss, which
        # enforces the 0.82 floor - a trigram-only defect BELOW the floor (heavy
        # reorder / real loss) must NOT be waived, only one within [0.82, 0.90).
        below = {"passed": False,
                 "findings": [{"code": "low_ordered_trigram_recall"}],
                 "metrics": {"ordered_trigram_recall": 0.60}}
        within = {"passed": False,
                  "findings": [{"code": "low_ordered_trigram_recall"}],
                  "metrics": {"ordered_trigram_recall": 0.85}}
        self.assertFalse(tp.is_trigram_only_near_miss(below))
        self.assertTrue(tp.is_trigram_only_near_miss(within))

    def test_rejects_recombination_on_real_recall_loss(self):
        # Each half drops ~60% of its words -> combined recall low = real content
        # loss (not a seam artifact) -> must NOT be accepted.
        words, source = self._splittable_source(400)

        def create(**kwargs):
            cw = re.findall(r"w\d+", kwargs["messages"][-1]["content"])
            if len(cw) > 300:
                payload = [{"type": "NARRATOR", "text": " ".join(cw[:5])}]
            else:
                payload = [{"type": "NARRATOR", "text": " ".join(cw[:max(3, len(cw)*4//10)])}]
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content=json.dumps(payload)),
                finish_reason="stop")], usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        params = LLMGenParams(max_tokens=800, temperature=0.1)
        out = tp.segment_chunk_adaptively(client, "m", source, params)
        self.assertEqual([], out)  # real recall loss is not waived by the fix
