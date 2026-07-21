from pass_quality import MIN_ORDERED_TRIGRAM_RECALL
import unittest
from pass_quality import validate_segment_quality
from pass_quality import freeze_check, validate_attribution
from pass_quality import validate_instruct, index_head_check
import default_prompts


def _seg(text, type_="NARRATOR"):
    return {"type": type_, "text": text}


class SegmentQualityTests(unittest.TestCase):
    def test_complete_segment_passes(self):
        source = " ".join(f"word{i}" for i in range(50))
        report = validate_segment_quality(source, [_seg(source)])
        self.assertTrue(report["passed"], report["findings"])

    def test_missing_type_field_fails(self):
        report = validate_segment_quality("A line.", [{"text": "A line."}])
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("missing_fields", codes)

    def test_invalid_type_value_fails(self):
        report = validate_segment_quality("A line.", [{"type": "MARCUS", "text": "A line."}])
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("invalid_type", codes)

    def test_severe_truncation_fails_recall(self):
        source = " ".join(f"word{i}" for i in range(100))
        report = validate_segment_quality(source, [_seg("word0 word1")])
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("low_source_token_recall", codes)


class FreezeAndAttributionTests(unittest.TestCase):
    def test_freeze_passes_when_text_identical(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me the truth."}]
        new = [{"speaker": "ELENA", "text": "Tell me the truth."}]
        ok, reason = freeze_check(frozen, new)
        self.assertTrue(ok, reason)

    def test_freeze_fails_when_text_altered(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me the truth."}]
        new = [{"speaker": "ELENA", "text": "Tell me the whole truth."}]
        ok, reason = freeze_check(frozen, new)
        self.assertFalse(ok)

    def test_freeze_fails_on_count_mismatch(self):
        frozen = [{"type": "NARRATOR", "text": "A."}, {"type": "SPOKEN", "text": "B."}]
        new = [{"speaker": "NARRATOR", "text": "A."}]
        ok, reason = freeze_check(frozen, new)
        self.assertFalse(ok)

    def test_freeze_ignores_punctuation_and_case(self):
        frozen = [{"type": "SPOKEN", "text": "We should leave."}]
        new = [{"speaker": "MARCUS", "text": "we should leave"}]
        ok, reason = freeze_check(frozen, new)
        self.assertTrue(ok, reason)

    def test_attribution_passes_when_all_spoken_named(self):
        frozen = [{"type": "NARRATOR", "text": "The room was cold."},
                  {"type": "SPOKEN", "text": "Tell me."}]
        resp = [{"n": 0, "head": "The room was", "speaker": "NARRATOR"},
                {"n": 1, "head": "Tell me.", "speaker": "ELENA"}]
        report = validate_attribution(frozen, resp)
        self.assertTrue(report["passed"], report["findings"])

    def test_attribution_passes_when_response_reordered_by_index(self):
        # Binding is by index, so a reordered response still validates + binds.
        frozen = [{"type": "NARRATOR", "text": "The room was cold."},
                  {"type": "SPOKEN", "text": "Tell me."}]
        resp = [{"n": 1, "head": "Tell me.", "speaker": "ELENA"},
                {"n": 0, "head": "The room was", "speaker": "NARRATOR"}]
        report = validate_attribution(frozen, resp)
        self.assertTrue(report["passed"], report["findings"])

    def test_attribution_fails_when_spoken_left_as_narrator(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me."}]
        resp = [{"n": 0, "head": "Tell me.", "speaker": "NARRATOR"}]
        report = validate_attribution(frozen, resp)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("spoken_not_named", codes)

    def test_attribution_fails_on_head_anchor_mismatch(self):
        # Model answered about the wrong line -> head doesn't match the frozen start.
        frozen = [{"type": "SPOKEN", "text": "Tell me."}]
        resp = [{"n": 0, "head": "Something else entirely", "speaker": "ELENA"}]
        report = validate_attribution(frozen, resp)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("alignment_violated", codes)

    def test_attribution_fails_on_duplicate_or_missing_index(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me."},
                  {"type": "SPOKEN", "text": "Now go."}]
        resp = [{"n": 0, "head": "Tell me.", "speaker": "ELENA"},
                {"n": 0, "head": "Tell me.", "speaker": "ELENA"}]  # dup 0, index 1 missing
        report = validate_attribution(frozen, resp)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("alignment_violated", codes)


class IndexHeadCheckTests(unittest.TestCase):
    def test_reproduces_gemma_drift_survives(self):
        # The exact class of failure that crashed the run: the model would drift
        # on a long line's full text. Under the new contract it only echoes the
        # head + speaker, so a correct head/speaker validates regardless of what
        # it would have done to the body.
        frozen = [{"type": "NARRATOR",
                   "text": "The strength in those clinging fingers was weak, and not "
                           "even Beatrice knew what she was trying to do with this touch."}]
        resp = [{"n": 0, "head": "The strength in those", "speaker": "NARRATOR"}]
        ok, reason, ordered = index_head_check(frozen, resp)
        self.assertTrue(ok, reason)
        self.assertEqual(0, ordered[0]["n"])

    def test_punctuation_only_line_needs_no_head(self):
        frozen = [{"type": "SPOKEN", "text": "――――."}]
        ok, _, _ = index_head_check(frozen, [{"n": 0, "head": "", "speaker": "RYUZU"}])
        self.assertTrue(ok)


class InstructValidatorTests(unittest.TestCase):
    def test_passes_when_all_have_instruct(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        resp = [{"n": 0, "head": "Tell me.", "instruct": "Firm, quiet."}]
        report = validate_instruct(prior, resp)
        self.assertTrue(report["passed"], report["findings"])

    def test_fails_when_instruct_missing_or_empty(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        resp = [{"n": 0, "head": "Tell me.", "instruct": "  "}]
        report = validate_instruct(prior, resp)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("missing_instruct", codes)

    def test_fails_on_head_anchor_mismatch(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        resp = [{"n": 0, "head": "Completely different words", "instruct": "Firm."}]
        report = validate_instruct(prior, resp)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("alignment_violated", codes)


class PromptLoaderTests(unittest.TestCase):
    def test_three_pass_prompts_load_and_have_placeholders(self):
        seg_sys, seg_usr = default_prompts.load_segment_prompts()
        self.assertIn("{chunk}", seg_usr)
        self.assertTrue(seg_sys.strip())
        att_sys, att_usr = default_prompts.load_attribute_prompts()
        self.assertIn("{batch}", att_usr)
        self.assertIn("{roster}", att_usr)
        ins_sys, ins_usr = default_prompts.load_instruct_prompts()
        self.assertIn("{batch}", ins_usr)


if __name__ == "__main__":
    unittest.main()


class GateParityAndMessagesTests(unittest.TestCase):
    def test_segment_gate_rejects_introduced_cyrillic(self):
        src = "the quick brown fox jumps over the lazy dog today"
        rep = validate_segment_quality(src, [{"type": "NARRATOR", "text": src.replace("fox", "fох")}])
        self.assertIn("unsupported_cyrillic", {f["code"] for f in rep["findings"]})

    def test_segment_findings_carry_message(self):
        rep = validate_segment_quality("a b c", [{"type": "MARCUS", "text": "a b c"}])
        for f in rep["findings"]:
            self.assertIn("message", f, f"finding {f['code']} lacks a message")

    def test_attribution_and_instruct_findings_carry_message(self):
        att = validate_attribution([{"type": "SPOKEN", "text": "Hi."}],
                                   [{"n": 0, "head": "Hi.", "speaker": "NARRATOR"}])
        ins = validate_instruct([{"speaker": "X", "text": "Hi."}],
                                [{"n": 0, "head": "Hi.", "instruct": ""}])
        for f in att["findings"] + ins["findings"]:
            self.assertIn("message", f)

    def test_segment_thresholds_are_shared_with_chunk_quality(self):
        import chunk_quality
        self.assertIs(MIN_ORDERED_TRIGRAM_RECALL, chunk_quality.MIN_ORDERED_TRIGRAM_RECALL)

    def test_introduced_unicode_finding_uses_dict_shape(self):
        # Must match chunk_quality's shape: characters are dicts with
        # character/codepoint/name, not bare "U+XXXX" strings, so consumers
        # that read finding["characters"][0]["character"] don't KeyError.
        src = "the quick brown fox jumps over the lazy dog today"
        rep = validate_segment_quality(src, [{"type": "NARRATOR", "text": src + " café"}])
        uni = [f for f in rep["findings"] if f["code"] == "unsupported_unicode_character"]
        self.assertTrue(uni, "expected an unsupported_unicode_character finding")
        char = uni[0]["characters"][0]
        self.assertEqual({"character", "codepoint", "name"}, set(char.keys()))
        self.assertEqual("é", char["character"])
