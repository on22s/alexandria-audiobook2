"""Detector faults are observable while conservative segment behavior stays intact."""
import unittest
from unittest.mock import patch

from pass_quality import validate_segment_quality


class ConventionErrorTelemetryTests(unittest.TestCase):
    def test_detector_error_is_distinct_from_normal_no_convention(self):
        source = "The room was quiet and the door remained closed."
        entries = [{"type": "NARRATOR", "text": source}]
        normal = validate_segment_quality(source, entries)
        self.assertEqual("skipped:none_detected", normal["quote_gate"])
        for error in (ValueError("private detail"), RuntimeError("private detail")):
            with self.subTest(error=type(error).__name__), \
                 patch("dialogue_spans.detect_convention", side_effect=error):
                failed = validate_segment_quality(source, entries)
            self.assertEqual("skipped:error:" + type(error).__name__, failed["quote_gate"])
            self.assertEqual(normal["findings"], failed["findings"])
            self.assertTrue(failed["passed"], failed)
            self.assertNotIn("private detail", str(failed))


if __name__ == "__main__":
    unittest.main()
