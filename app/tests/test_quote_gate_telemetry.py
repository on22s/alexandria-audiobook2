"""A gate that declines to run must say so, and must not thereby fail a chunk.

`_quote_region_findings` checks that SPOKEN text came from inside quotes in the
source and NARRATOR text from outside. It only understands paired quotes, and
it used to return `[]` when the source had none - indistinguishable, to every
caller, from "checked and found nothing wrong".

That is the shape this project keeps paying for: a guard that disables itself
without saying so. Three separate measurements were wrong today for exactly
that reason. Measured over all 85 sources on this machine the risk is latent -
every one uses paired quotes - which is why the fix must not cost anything on
the books that do.

So the skip is TELEMETRY, not a finding: a narration-only chunk has no quotes,
legitimately passes, and the report now records whether the gate ran.
"""
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).parent.parent.parent
sys.path.insert(0, str(REPO / "app"))


class QuoteGateTelemetryTest(unittest.TestCase):
    def setUp(self):
        import pass_quality
        self.q = pass_quality

    def _report(self, source, entries):
        return self.q.validate_segment_quality(source, entries)

    def test_a_quoted_source_reports_the_gate_ran(self):
        source = 'He waited. "Hello there," she said. He nodded.'
        entries = [{"type": "NARRATOR", "text": "He waited."},
                   {"type": "SPOKEN", "text": "Hello there,"},
                   {"type": "NARRATOR", "text": "she said. He nodded."}]
        report = self._report(source, entries)
        self.assertEqual("ran", report["quote_gate"])

    def test_a_source_with_no_quotes_reports_the_gate_skipped(self):
        """The case that used to be silent."""
        source = "He waited. He nodded. The road went on for a long while."
        entries = [{"type": "NARRATOR", "text": source}]
        report = self._report(source, entries)
        self.assertTrue(report["quote_gate"].startswith("skipped:"),
                        report["quote_gate"])

    def test_skipping_does_not_fail_the_chunk(self):
        """A narration-only chunk is legitimate and must still pass, or the
        fix would break every book by protecting against a latent risk."""
        source = "He waited. He nodded. The road went on for a long while."
        entries = [{"type": "NARRATOR", "text": source}]
        report = self._report(source, entries)
        self.assertTrue(report["passed"], report["findings"])
        self.assertEqual([], [f for f in report["findings"]
                              if "quote_gate" in f.get("code", "")])

    def test_the_report_always_carries_the_field(self):
        """A reader comparing two clean reports must always be able to tell
        which of them actually had its dialogue checked."""
        for source in ('She said "yes" firmly.', "Nothing quoted here at all."):
            with self.subTest(source=source[:24]):
                report = self._report(source, [{"type": "NARRATOR",
                                                "text": source}])
                self.assertIn("quote_gate", report)

    def test_the_gate_still_catches_a_real_misclassification(self):
        """The protection this fix must not weaken: SPOKEN text that did not
        come from inside quotes."""
        source = 'He waited outside. "Hello there," she said.'
        entries = [{"type": "SPOKEN", "text": "He waited outside."}]
        report = self._report(source, entries)
        codes = {f.get("code") for f in report["findings"]}
        self.assertTrue(codes & {"quote_region_misclassified",
                                 "crosses_quote_boundary"}, report["findings"])


if __name__ == "__main__":
    unittest.main()
