"""The parser that reads a loader's report, checked on real report text.

Rule 21: this is the instrument, so it is hand-checked on cases whose answer is
already known - including the ones it must REJECT. A finding-detector that
cannot come back empty would report a problem on every clean load, and a
probe that always agrees with its hypothesis measures nothing.
"""

import unittest

from experiments.fp8_scale_load_probe import scale_findings


# Verbatim from qwen38-multientry-thinking-low-8k-n10-h100-20260904.log, the
# 12.5-hour run this probe exists to explain. Kept exactly as printed, ANSI
# codes stripped, so the fixture cannot quietly stop resembling the real thing.
REAL_FP8_REPORT = """[transformers] Qwen3_5ForConditionalGeneration LOAD REPORT from: /home/ubuntu/models/Qwen3.8-27B-FP8
Key                                                                 | Status     |  |
--------------------------------------------------------------------+------------+--+-
model.language_model.layers.{0...63}.mlp.gate_proj.weight_scale_inv | UNEXPECTED |  |

Notes:
- UNEXPECTED:\tcan be ignored when loading from different task/architecture; not ok if you expect identical arch.
"""

CLEAN_REPORT = """[transformers] Qwen3ForCausalLM LOAD REPORT from: /home/ubuntu/models/Qwen3-14B
All keys matched successfully.
"""


class ScaleFindingsTest(unittest.TestCase):

    def test_it_finds_the_unplaced_scale_in_the_real_report(self):
        findings = scale_findings(REAL_FP8_REPORT)
        self.assertEqual(1, len(findings))
        self.assertEqual("UNEXPECTED", findings[0]["status"])
        self.assertIn("weight_scale_inv", findings[0]["key"])
        # The layer range matters: this is one line standing for all 64 MLP
        # blocks, not a single stray tensor, and a reader of the artifact must
        # be able to see that without going back to the log.
        self.assertIn("{0...63}", findings[0]["key"])

    def test_it_marks_a_scale_tensor_as_a_scale(self):
        # The count this drives is what the artifact reports beside
        # stopped_on_its_own, so mislabelling it would mis-state the finding.
        self.assertTrue(scale_findings(REAL_FP8_REPORT)[0]["is_scale"])

    def test_a_clean_load_reports_nothing(self):
        # THE CASE IT MUST REJECT. A detector that cannot return empty would
        # find fault with the BF16 control too, and the comparison - which is
        # the entire probe - would be meaningless.
        self.assertEqual([], scale_findings(CLEAN_REPORT))

    def test_no_report_is_not_a_clean_report_by_accident(self):
        # Empty for empty input, so "loaded clean" and "never captured a
        # report" are told apart by the artifact's own arm fields rather than
        # collapsing into the same [].
        self.assertEqual([], scale_findings(""))
        self.assertEqual([], scale_findings(None))

    def test_a_non_scale_unplaced_tensor_is_recorded_but_not_counted(self):
        report = ("Key | Status |  |\n"
                  "model.layers.0.self_attn.q_proj.weight | MISSING |  |\n")
        findings = scale_findings(report)
        self.assertEqual(1, len(findings))
        self.assertEqual("MISSING", findings[0]["status"])
        self.assertFalse(findings[0]["is_scale"])


if __name__ == "__main__":
    unittest.main()
