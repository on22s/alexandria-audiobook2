from pass_quality import MIN_ORDERED_TRIGRAM_RECALL
import unittest
from pass_quality import validate_segment_quality
from pass_quality import freeze_check, validate_attribution
from pass_quality import validate_instruct
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
        named = [{"speaker": "NARRATOR", "text": "The room was cold."},
                 {"speaker": "ELENA", "text": "Tell me."}]
        report = validate_attribution(frozen, named)
        self.assertTrue(report["passed"], report["findings"])

    def test_attribution_fails_when_spoken_left_as_narrator(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me."}]
        named = [{"speaker": "NARRATOR", "text": "Tell me."}]
        report = validate_attribution(frozen, named)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("spoken_not_named", codes)

    def test_attribution_fails_on_text_drift(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me."}]
        named = [{"speaker": "ELENA", "text": "Tell me now."}]
        report = validate_attribution(frozen, named)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("text_freeze_violated", codes)


class InstructValidatorTests(unittest.TestCase):
    def test_passes_when_all_have_instruct(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        annotated = [{"speaker": "ELENA", "text": "Tell me.", "instruct": "Firm, quiet."}]
        report = validate_instruct(prior, annotated)
        self.assertTrue(report["passed"], report["findings"])

    def test_fails_when_instruct_missing_or_empty(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        annotated = [{"speaker": "ELENA", "text": "Tell me.", "instruct": "  "}]
        report = validate_instruct(prior, annotated)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("missing_instruct", codes)

    def test_fails_when_speaker_changed(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        annotated = [{"speaker": "MARCUS", "text": "Tell me.", "instruct": "Firm."}]
        report = validate_instruct(prior, annotated)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("speaker_changed", codes)

    def test_fails_on_text_drift(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        annotated = [{"speaker": "ELENA", "text": "Tell me now.", "instruct": "Firm."}]
        report = validate_instruct(prior, annotated)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("text_freeze_violated", codes)


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
                                   [{"speaker": "NARRATOR", "text": "Hi."}])
        ins = validate_instruct([{"speaker": "X", "text": "Hi."}],
                                [{"speaker": "X", "text": "Hi.", "instruct": ""}])
        for f in att["findings"] + ins["findings"]:
            self.assertIn("message", f)

    def test_segment_thresholds_are_shared_with_chunk_quality(self):
        import chunk_quality
        self.assertIs(MIN_ORDERED_TRIGRAM_RECALL, chunk_quality.MIN_ORDERED_TRIGRAM_RECALL)
