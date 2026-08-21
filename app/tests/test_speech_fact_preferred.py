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
from tests.test_support import app_python_files

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


class SpeechFromPunctuationDetector:
    """Finds code that decides speech from quote characters.

    ONE PATTERN IS NOT ENOUGH. The first version of this detector matched only
    `SPEECH if re.search(...)`, which is the shape `weak_supervision` happened
    to use. It would have missed every other site fixed today: a membership
    test, a `startswith`, a coverage threshold, a precompiled pattern. A
    detector narrow enough to miss the bugs it was written for is worse than
    none, because it certifies.
    """

    # THE QUOTE CHARACTER MUST BE THE DATA, NOT THE DELIMITER. In Python
    # source `"` is both, so a naive pattern for `.startswith("` fires on
    # `path.startswith("http")` - it flagged 23 files on its first run, none of
    # them about speech. Only two forms are unambiguous: a curly or corner
    # quote (never a Python delimiter), or a straight quote written inside
    # single quotes, `\'"\'`.
    _Q = '[\u201c\u201d\u300c\u300d\u300e\u300f]'      # curly / corner quotes
    _STRAIGHT = "'\""                                       # a " inside ' '

    PATTERNS = {
        "conditional_expression":
            r'(SPEECH|SPOKEN|True|1\.0) +if +re\.search\(',
        "membership_test":
            r"(" + _STRAIGHT + r"'|" + _Q + r") +in +\w*(text|line|entry|t)\b",
        "startswith":
            r"\.startswith\(\(?(" + _STRAIGHT + r"'|" + _Q + r")",
        "coverage_threshold":
            r'quote_coverage\([^)]*\) *[<>=]',
        "precompiled_quote_regex":
            r'\bQUOTED\.(search|match|findall)\(',
        "counts_quotes":
            r"\.count\((" + _STRAIGHT + r"'|" + _Q + r")\) *[<>=]",
    }

    @classmethod
    def hits(cls, source):
        code = re.sub(r"#[^\n]*|\"\"\".*?\"\"\"", "", source, flags=re.S)
        return {name for name, pat in cls.PATTERNS.items()
                if re.search(pat, code)}


class DetectorSelfTest(unittest.TestCase):
    """The detector must fire on every shape the real bugs used.

    Rule 21: hand-check the instrument on cases whose answer is known,
    including cases it should REJECT, and keep them as tests. Each fixture
    below is the shape of a site actually fixed on 2026-08-20.
    """

    def test_it_catches_the_conditional_expression_shape(self):
        """weak_supervision.lf_quoted"""
        self.assertIn("conditional_expression", SpeechFromPunctuationDetector.hits(
            'def lf(t):\n    return SPEECH if re.search(pat, t) else ABSTAIN'))

    def test_it_catches_a_membership_test(self):
        self.assertIn("membership_test", SpeechFromPunctuationDetector.hits(
            'if \'"\' in text:\n    kind = "SPOKEN"'))

    def test_it_catches_startswith(self):
        self.assertIn("startswith", SpeechFromPunctuationDetector.hits(
            'if text.startswith(\'"\'):\n    spoken = True'))

    def test_it_catches_a_coverage_threshold(self):
        """measure_dialogue_attribution.is_spoken_line"""
        self.assertIn("coverage_threshold", SpeechFromPunctuationDetector.hits(
            'return quote_coverage(text) >= SPOKEN_COVERAGE'))

    def test_it_catches_a_precompiled_pattern(self):
        """measure_dialogue_attribution.measurable"""
        self.assertIn("precompiled_quote_regex", SpeechFromPunctuationDetector.hits(
            'quoted = sum(1 for e in entries if QUOTED.search(e["text"]))'))

    def test_it_ignores_the_same_shape_inside_a_comment(self):
        """Every fixed site now has a comment explaining the old behaviour. A
        detector that read comments would fire on all of them forever."""
        self.assertEqual(set(), SpeechFromPunctuationDetector.hits(
            '# return SPEECH if re.search(r\'["\u201c]\', t) else ABSTAIN\nx = 1'))

    def test_it_ignores_the_same_shape_inside_a_docstring(self):
        self.assertEqual(set(), SpeechFromPunctuationDetector.hits(
            'def f():\n    """return SPEECH if re.search(r\'["]\', t)."""\n    return 1'))

    def test_it_does_not_fire_on_ordinary_string_handling(self):
        """Quote characters appear in normal code constantly. A detector that
        fired on them would be ignored within a day - the first version of this
        one flagged 23 files, none of them about speech."""
        self.assertEqual(set(), SpeechFromPunctuationDetector.hits(
            'name = row.get("speaker")\nif name.startswith("MR"):\n    pass'))

    def test_it_does_not_fire_on_a_path_or_prefix_check(self):
        for line in ('if path.startswith("http"):',
                     'if line.startswith("worktree "):',
                     'if name.startswith("test_"):'):
            with self.subTest(line=line):
                self.assertEqual(set(),
                                 SpeechFromPunctuationDetector.hits(line))


class InventoryTest(unittest.TestCase):
    """The sweep, so a new consumer of speech cannot be added silently.

    Any module deciding speech from quote characters must consult `spoken` or
    appear here with a reason.
    """

    ALLOWED_WITHOUT_THE_FACT = {
        # Deliberately blind: pairs two different segmentations of one book by
        # alphanumerics. Fixing it would break the pairing that makes 5.3's
        # accuracy numbers possible. Pinned in test_script_text_fidelity.py.
        "three_pass_vs_single.py",
        # Diagnose the quote-region defect itself; punctuation IS their subject.
        "quote_aware_chunking.py", "japanese_quote_robustness.py",
        "quote_repair_risk.py", "quote_fallthrough.py",
        # Produces the fact, so it cannot consult it.
        "dialogue_spans.py",
        # Repairs and audits encoding damage; operates on characters by design.
        "repair_source_encoding.py", "source_normalization.py",
        "script_preflight.py", "audit_source_encoding.py",
        "compare_epub_extractors.py", "epub_structural.py",
        # The quote-region gate reads the SOURCE, not model output, and now
        # reports when it declines. Pinned in test_quote_gate_telemetry.py.
        "pass_quality.py",
    }

    def test_every_speech_decider_consults_the_fact_or_is_listed(self):
        offenders = {}
        for path in app_python_files(REPO / "app"):
            if "/tests/" in str(path) or path.name in self.ALLOWED_WITHOUT_THE_FACT:
                continue
            src = path.read_text(encoding="utf-8", errors="replace")
            hits = SpeechFromPunctuationDetector.hits(src)
            if hits and '"spoken"' not in src:
                offenders[path.name] = sorted(hits)
        self.assertEqual({}, offenders,
                         "these decide speech from punctuation without ever "
                         "consulting the recorded fact; fix them or add them "
                         "to ALLOWED_WITHOUT_THE_FACT with a reason")

    def test_the_allow_list_has_no_stale_entries(self):
        """A name left behind after a file is deleted or renamed silently
        widens the exemption for whatever takes that name next."""
        missing = [n for n in self.ALLOWED_WITHOUT_THE_FACT
                   if not list((REPO / "app").rglob(n))]
        self.assertEqual([], missing)


if __name__ == "__main__":
    unittest.main()
