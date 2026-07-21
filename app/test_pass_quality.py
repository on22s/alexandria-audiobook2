import unittest
from pass_quality import validate_segment_quality
from pass_quality import freeze_check, validate_attribution


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


if __name__ == "__main__":
    unittest.main()
