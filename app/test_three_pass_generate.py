import re
import unittest
import os
import tempfile
import three_pass_generate as tp
import json
from types import SimpleNamespace
from generate_script import LLMGenParams
from script_repair import build_deterministic_repair


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

    def test_resolve_chunk_size_cli_overrides_config(self):
        self.assertEqual(3000, tp.resolve_chunk_size(3000, 6000))
        self.assertEqual(6000, tp.resolve_chunk_size(None, 6000))

    def test_resolve_chunk_size_rejects_bad_config_value(self):
        with self.assertRaises(ValueError):
            tp.resolve_chunk_size(None, 0)      # bad config value now caught
        with self.assertRaises(ValueError):
            tp.resolve_chunk_size(-5, 6000)     # bad CLI value still caught
        with self.assertRaises(ValueError):
            tp.resolve_chunk_size(None, "big")  # non-int config

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
        good = [{"n": 0, "head": "The room was", "speaker": "NARRATOR"},
                {"n": 1, "head": "Tell me.", "speaker": "ELENA"}]
        client = _client_returning([good])
        out = tp.attribute_batch(client, "m", frozen, self._params(), roster=[])
        self.assertEqual(["NARRATOR", "ELENA"], [e["speaker"] for e in out])
        self.assertEqual("Tell me.", out[1]["text"])

    def test_pass2_fail_mode_raises_when_exhausted(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me."}]
        bad = [{"n": 0, "head": "Tell me.", "speaker": "NARRATOR"}]
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
        good = [{"n": 0, "head": "Tell me.", "instruct": "Firm, quiet."}]
        client = _client_returning([good])
        out = tp.instruct_batch(client, "m", prior, self._params())
        self.assertEqual("Firm, quiet.", out[0]["instruct"])

    def test_falls_back_to_default_instruct_on_exhaustion(self):
        prior = [{"speaker": "NARRATOR", "text": "The room was cold."}]
        bad = [{"n": 0, "head": "The room was", "instruct": ""}]
        client = _client_returning([bad, bad])
        out = tp.instruct_batch(client, "m", prior, self._params(), max_retries=1)
        self.assertEqual("Neutral, even narration.", out[0]["instruct"])
        self.assertEqual("The room was cold.", out[0]["text"])


class EndToEndTests(unittest.TestCase):
    def test_three_passes_assemble_final_entries(self):
        source = "The room was cold. \"Tell me the truth.\""
        seg = [{"type": "NARRATOR", "text": "The room was cold."},
               {"type": "SPOKEN", "text": "Tell me the truth."}]
        named = [{"n": 0, "head": "The room was", "speaker": "NARRATOR"},
                 {"n": 1, "head": "Tell me the", "speaker": "ELENA"}]
        instructed = [{"n": 0, "head": "The room was", "instruct": "Cold, still narration."},
                      {"n": 1, "head": "Tell me the", "instruct": "Firm, quiet demand."}]
        client = _client_returning([seg, named, instructed])
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        entries = tp.run_three_pass(client, "m", source, params, chunk_size=6000)
        self.assertEqual(2, len(entries))
        self.assertEqual({"speaker", "text", "instruct"}, set(entries[0].keys()))
        self.assertEqual("ELENA", entries[1]["speaker"])
        self.assertEqual("Firm, quiet demand.", entries[1]["instruct"])

    def test_fallback_mode_completes_and_rebuilds_roster(self):
        # Exercises on_exhaustion="fallback": an unnameable SPOKEN line degrades
        # to UNKNOWN (no PassExhausted), and the pass-2 loop takes the
        # roster-rebuild branch (finding #15) without crashing.
        source = "Hi there friend."
        seg = [{"type": "SPOKEN", "text": "Hi there friend."}]
        bad = [{"n": 0, "head": "Hi there friend", "speaker": "NARRATOR"}]  # never names it
        instr = [{"n": 0, "head": "Hi there friend", "instruct": "z"}]
        client = _client_returning([seg, bad, bad, bad, bad, instr])
        params = LLMGenParams(max_tokens=500, temperature=0.1)
        entries = tp.run_three_pass(client, "m", source, params, chunk_size=6000,
                                    on_exhaustion="fallback")
        self.assertEqual(1, len(entries))
        self.assertEqual("UNKNOWN", entries[0]["speaker"])

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
        named = [{"n": 0, "head": "The room was", "speaker": "NARRATOR"},
                 {"n": 1, "head": "Tell me the", "speaker": "ELENA"}]
        instructed = [{"n": 0, "head": "The room was", "instruct": "Cold."},
                      {"n": 1, "head": "Tell me the", "instruct": "Firm."}]
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
    def test_attribute_text_comes_byte_exact_from_frozen(self):
        # The model no longer returns text at all - only {n, head, speaker} - so
        # the output text is always the frozen text verbatim, and nothing the
        # model does to a body can corrupt it.
        frozen = [{"type": "SPOKEN", "text": "Tell me the truth."}]
        resp = [{"n": 0, "head": "Tell me the", "speaker": "ELENA"}]
        p = LLMGenParams(system_prompt="s", user_prompt_template="{roster}{batch}",
                         max_tokens=500, temperature=0.1)
        out = tp.attribute_batch(_client_returning([resp]), "m", frozen, p, roster=[])
        self.assertEqual("Tell me the truth.", out[0]["text"])
        self.assertEqual("ELENA", out[0]["speaker"])

    def test_attribute_survives_gemma_style_body_drift(self):
        # Reproduction of the failure that crashed the real run: a weak model that
        # would have mangled a long line's body. Under the index+head contract it
        # only echoes the head + speaker, so the batch validates and the frozen
        # text is preserved byte-exact - no more "entry N text changed" abort.
        frozen = [{"type": "NARRATOR",
                   "text": "The strength in those clinging fingers was weak, and not "
                           "even Beatrice knew what she was trying to do."},
                  {"type": "SPOKEN", "text": "Thank you―― Goodbye, Betty."}]
        resp = [{"n": 0, "head": "The strength in those", "speaker": "NARRATOR"},
                {"n": 1, "head": "Thank you―― Goodbye,", "speaker": "RYUZU"}]
        p = LLMGenParams(system_prompt="s", user_prompt_template="{roster}{batch}",
                         max_tokens=800, temperature=0.1)
        out = tp.attribute_batch(_client_returning([resp]), "m", frozen, p, roster=[])
        self.assertEqual(frozen[0]["text"], out[0]["text"])
        self.assertEqual("RYUZU", out[1]["speaker"])

    def test_attribute_preserves_pause_after_and_drops_type(self):
        frozen = [{"type": "NARRATOR", "text": "The room was cold.", "pause_after": 1000}]
        named = [{"n": 0, "head": "The room was", "speaker": "NARRATOR"}]
        p = LLMGenParams(system_prompt="s", user_prompt_template="{roster}{batch}",
                         max_tokens=500, temperature=0.1)
        out = tp.attribute_batch(_client_returning([named]), "m", frozen, p, roster=[])
        self.assertEqual(1000, out[0]["pause_after"])
        self.assertNotIn("type", out[0])
        self.assertEqual("NARRATOR", out[0]["speaker"])

    def test_attribute_fallback_preserves_pause_after(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me.", "pause_after": 500}]
        bad = [{"n": 0, "head": "Tell me.", "speaker": "NARRATOR"}]  # never names the spoken line
        p = LLMGenParams(system_prompt="s", user_prompt_template="{roster}{batch}",
                         max_tokens=500, temperature=0.1)
        out = tp.attribute_batch(_client_returning([bad]), "m", frozen, p, roster=[],
                                 max_retries=1, on_exhaustion="fallback")
        self.assertEqual(500, out[0]["pause_after"])
        self.assertNotIn("type", out[0])

    def test_instruct_preserves_pause_after(self):
        prior = [{"speaker": "NARRATOR", "text": "The room was cold.", "pause_after": 1000}]
        good = [{"n": 0, "head": "The room was", "instruct": "Cold."}]
        p = LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                         max_tokens=500, temperature=0.1)
        out = tp.instruct_batch(_client_returning([good]), "m", prior, p)
        self.assertEqual(1000, out[0]["pause_after"])
        self.assertEqual("Cold.", out[0]["instruct"])

    def test_instruct_keeps_speaker_and_text_from_prior(self):
        # Pass 3 returns only {n, head, instruct}; speaker and text come byte-exact
        # from the prior entry, so neither can change.
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        resp = [{"n": 0, "head": "Tell me.", "instruct": "Firm."}]
        p = LLMGenParams(system_prompt="s", user_prompt_template="{batch}",
                         max_tokens=500, temperature=0.1)
        out = tp.instruct_batch(_client_returning([resp]), "m", prior, p)
        self.assertEqual("ELENA", out[0]["speaker"])
        self.assertEqual("Tell me.", out[0]["text"])
        self.assertEqual("Firm.", out[0]["instruct"])
        self.assertEqual("Firm.", out[0]["instruct"])


class SegmentRepairTypeSafetyTests(unittest.TestCase):
    def test_segment_repair_does_not_merge_empty_across_type(self):
        # An empty SPOKEN unit between a NARRATOR and a SPOKEN entry must NOT be
        # converted into a pause on the NARRATOR + dropped before the gate; it
        # should stay so validate_segment_quality's empty_text finding sees it.
        entries = [{"type": "NARRATOR", "text": "He spoke softly."},
                   {"type": "SPOKEN", "text": ""},
                   {"type": "SPOKEN", "text": "Hello."}]
        res = build_deterministic_repair(entries, "He spoke softly. Hello.",
                                         merge_empty_into_pause=False)
        self.assertEqual(3, len(res["entries"]))
        self.assertNotIn("pause_after", res["entries"][0])

    def test_single_pass_default_still_merges_empty_into_pause(self):
        entries = [{"speaker": "NARRATOR", "text": "He spoke softly."},
                   {"speaker": "ELENA", "text": ""}]
        res = build_deterministic_repair(entries, "He spoke softly.")
        self.assertEqual(1, len(res["entries"]))  # empty dropped
        self.assertEqual(1000, res["entries"][0]["pause_after"])


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


class BoundedContextJoinTests(unittest.TestCase):
    def test_tail_and_head_join_match_naive_slice(self):
        chunks = [f"chunk{i}_" * 50 for i in range(20)]  # ~300 chars each
        index = 12
        for window in tp._CONTEXT_RESCUE_WINDOWS:
            naive_before = "".join(chunks[:index])[-window:]
            naive_after = "".join(chunks[index + 1:])[:window]
            bounded_before = tp._tail_join(chunks[:index], max(tp._CONTEXT_RESCUE_WINDOWS))[-window:]
            bounded_after = tp._head_join(chunks[index + 1:], max(tp._CONTEXT_RESCUE_WINDOWS))[:window]
            self.assertEqual(naive_before, bounded_before)
            self.assertEqual(naive_after, bounded_after)

    def test_join_bounded_does_not_materialize_whole_book(self):
        chunks = ["x" * 1000 for _ in range(100)]  # 100k-char "book"
        joined = tp._tail_join(chunks[:50], max(tp._CONTEXT_RESCUE_WINDOWS))
        # Only enough trailing chunks to cover the max window, not all 50.
        self.assertLess(len(joined), 50 * 1000)
        self.assertGreaterEqual(len(joined), max(tp._CONTEXT_RESCUE_WINDOWS))


class RescueBudgetTests(unittest.TestCase):
    def test_window_fits_returns_true_when_context_unknown(self):
        p = LLMGenParams(max_tokens=500, temperature=0.1)  # context_length None
        self.assertTrue(tp._rescue_prompt_fits("x" * 100000, "y" * 6000, "", 500, p))

    def test_custom_windows_control_rescue_attempts(self):
        # finding #12: windows are configurable. A single small window means at
        # most one segmentation attempt (here it fails -> one call, then []).
        attempts = {"n": 0}

        def create(**_kwargs):
            attempts["n"] += 1
            return SimpleNamespace(choices=[SimpleNamespace(
                message=SimpleNamespace(content="[]"), finish_reason="stop")], usage=None)

        client = SimpleNamespace(chat=SimpleNamespace(
            completions=SimpleNamespace(create=create)))
        p = LLMGenParams(max_tokens=500, temperature=0.1)  # no context_length cap
        out = tp.rescue_chunk_with_context(client, "m", ["small chunk"], 0, p,
                                           windows=(100,), max_retries=0)
        self.assertEqual([], out)
        self.assertEqual(1, attempts["n"], "one window x (max_retries=0 -> 1 attempt)")

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
        named = [{"n": 0, "head": "The room was", "speaker": "NARRATOR"},
                 {"n": 1, "head": "Tell me the", "speaker": "ELENA"}]
        instructed = [{"n": 0, "head": "The room was", "instruct": "Cold."},
                      {"n": 1, "head": "Tell me the", "instruct": "Firm."}]
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
