"""Every site that decides "is this speech" must prefer the recorded fact.

`dialogue_spans` maps speech from the SOURCE before any model runs, so `spoken`
survives whatever an arm did to the punctuation. Generation removes the
outermost quotes and how completely depends on the arm - three-pass strips
every one, single-pass keeps 37-61% - so any code that asks the punctuation
gets an answer that varies with the arm rather than with the line.

This module is the inventory. One test per site, so that adding a new consumer
of speech means adding a case here rather than discovering the omission from a
result that looks plausible.
"""
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "app"))
sys.path.insert(0, str(REPO / "app" / "experiments"))


class MeasurabilityGateTest(unittest.TestCase):
    def test_mapped_book_without_quotes_is_measurable(self):
        from experiments import measure_dialogue_attribution as m
        entries = [{"text": f"line {i}", "spoken": True} for i in range(200)]
        ok, _ = m.measurable(entries)
        self.assertTrue(ok)


class WeakSupervisionTest(unittest.TestCase):
    """lf_quoted went SILENT on a stripped script - ABSTAIN on every line.

    An abstaining labelling function costs coverage without ever looking wrong,
    which is why nothing reported it.
    """

    def setUp(self):
        from experiments import weak_supervision as m
        self.m = m

    def test_a_mapped_spoken_line_labels_speech_without_quotes(self):
        seg = [{"text": "Hello there", "spoken": True}]
        self.assertEqual(self.m.SPEECH, self.m.lf_quoted(seg, 0, seg[0]["text"]))

    def test_a_mapped_narration_line_labels_narration(self):
        """Previously ABSTAIN - the map turns a missing signal into a real one."""
        seg = [{"text": "He waited by the door.", "spoken": False}]
        self.assertEqual(self.m.NARR, self.m.lf_quoted(seg, 0, seg[0]["text"]))

    def test_an_unmapped_line_still_uses_the_punctuation(self):
        seg = [{"text": '"Hello there," she said.'}]
        self.assertEqual(self.m.SPEECH, self.m.lf_quoted(seg, 0, seg[0]["text"]))
        seg = [{"text": "He waited."}]
        self.assertEqual(self.m.ABSTAIN, self.m.lf_quoted(seg, 0, seg[0]["text"]))


class GoldSetSamplingTest(unittest.TestCase):
    """Sampling gold from the model's own `type` is a selection effect.

    A line the model misfiled as NARRATOR could never enter the gold set, so
    accuracy measured against that gold was blind to exactly the failures the
    model makes.
    """

    def setUp(self):
        import gold_set_builder as m
        self.m = m

    def test_a_source_confirmed_line_is_eligible_though_the_model_said_narrator(self):
        segmented = [{"text": "Hello there friend", "type": "NARRATOR",
                      "spoken": True}]
        self.assertEqual([0], self.m.eligible_indexes(segmented))

    def test_a_source_confirmed_narration_line_is_not_eligible(self):
        segmented = [{"text": "He waited by the door", "type": "SPOKEN",
                      "spoken": False}]
        self.assertEqual([], self.m.eligible_indexes(segmented))

    def test_an_unmapped_entry_still_uses_the_model_type(self):
        segmented = [{"text": "Hello there friend", "type": "SPOKEN"}]
        self.assertEqual([0], self.m.eligible_indexes(segmented))

    def test_a_repeated_line_is_still_excluded(self):
        """The protection this must not weaken: a line appearing twice cannot
        be aligned to one position, mapped or not."""
        segmented = [{"text": "Yes indeed", "spoken": True},
                     {"text": "Yes indeed", "spoken": True}]
        self.assertEqual([], self.m.eligible_indexes(segmented))


class ClassifierFeatureTest(unittest.TestCase):
    def test_has_quote_prefers_the_record(self):
        from experiments import segmentation_classifier as m
        seg = [{"text": "Hello there", "spoken": True}]
        self.assertEqual(1.0, m.features(seg, 0, seg[0]["text"])["has_quote"])
        seg = [{"text": "He waited.", "spoken": False}]
        self.assertEqual(0.0, m.features(seg, 0, seg[0]["text"])["has_quote"])


class InventoryTest(unittest.TestCase):
    """The sweep itself, so a new consumer cannot be added silently.

    Any module that decides speech from quote characters must either consult
    `spoken` or be listed here with a reason.
    """

    ALLOWED_WITHOUT_THE_FACT = {
        # Deliberately blind: pairs two different segmentations of one book by
        # alphanumerics. Fixing it would break the pairing that makes 5.3's
        # accuracy numbers possible. Its blindness is pinned in
        # test_script_text_fidelity.py.
        "three_pass_vs_single.py",
        # Diagnoses the quote-region defect itself; punctuation IS its subject.
        "quote_aware_chunking.py", "japanese_quote_robustness.py",
        "quote_repair_risk.py", "quote_fallthrough.py",
        # Produces the fact, so it cannot consult it.
        "dialogue_spans.py",
    }

    def test_every_speech_decider_consults_the_fact_or_is_listed(self):
        offenders = []
        for path in sorted((REPO / "app").rglob("*.py")):
            if "/tests/" in str(path) or path.name in self.ALLOWED_WITHOUT_THE_FACT:
                continue
            src = path.read_text(encoding="utf-8", errors="replace")
            code = re.sub(r"#[^\n]*|\"\"\".*?\"\"\"", "", src, flags=re.S)
            decides = re.search(r'(SPEECH|SPOKEN|True) if re\.search\(r?.[\[]?[“"”]', code)
            if decides and '"spoken"' not in code:
                offenders.append(path.name)
        self.assertEqual([], offenders,
                         "these decide speech from punctuation without ever "
                         "consulting the recorded fact")


if __name__ == "__main__":
    unittest.main()
