"""Both generation gates must reject unsupported repeated prose."""

import unittest

from chunk_quality import validate_chunk_quality
from pass_quality import validate_segment_quality
from script_preflight import find_adjacent_duplicate_blocks


class SharedDuplicateGateTests(unittest.TestCase):
    def test_zero_one_and_two_source_occurrences_have_matching_verdicts(self):
        filler = " ".join(f"word{index}" for index in range(150)) + "."
        block = ["Purple lantern flickered.", "Silver raven waited."]
        for occurrences in (0, 1, 2):
            source = " ".join([filler] + block * occurrences)
            texts = [filler] + block * 2
            duplicate = find_adjacent_duplicate_blocks(texts, source)
            self.assertEqual(1, len(duplicate))
            self.assertEqual(occurrences, duplicate[0]["details"]["source_occurrences"])
            self.assertEqual("blocking" if occurrences < 2 else "manual_review",
                             duplicate[0]["severity"])
            for gate, label in ((validate_chunk_quality, "speaker"),
                                (validate_segment_quality, "type")):
                with self.subTest(occurrences=occurrences, gate=gate.__name__):
                    entries = [{label: "NARRATOR", "text": text,
                                "instruct": "Read naturally."} for text in texts]
                    report = gate(source, entries)
                    codes = [finding["code"] for finding in report["findings"]]
                    if occurrences < 2:
                        self.assertIn("source_unsupported_duplicate", codes)
                        self.assertFalse(report["passed"])
                    else:
                        self.assertNotIn("source_unsupported_duplicate", codes)
                        self.assertTrue(report["passed"], report["findings"])


if __name__ == "__main__":
    unittest.main()
