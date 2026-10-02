"""Lost letters must remain visible alongside safe, independently known repairs."""
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

import repair_source_encoding as repair


class AmbiguousSourceRepairTests(unittest.TestCase):
    def test_pure_lost_runs_do_not_establish_repetition(self):
        for source in ("Ng���n", "Đ���ng", "���", "a����b", "Ng���?" + "�?" * 3):
            with self.subTest(source=source):
                output, counts = repair.collapse_repetitions(source)
                self.assertEqual(source, output)
                self.assertEqual({}, counts)

    def test_damaged_pairs_cannot_consume_the_edge_of_an_unknown_run(self):
        source = "Ng���?" + "�?" * 3
        output, counts, _ = repair.repair(source)
        self.assertTrue(output.startswith("Ng���?"), output)
        self.assertEqual(3, output.count(repair.FFFD))
        self.assertNotIn("collapsed_repeated_pair", counts)

    def test_later_rules_and_last_resort_cannot_erase_ambiguous_runs(self):
        # Terminal punctuation and unmatched quotes used to permit an edge
        # glyph to be consumed, allowing the rest to fall through as dashes.
        sources = ("Ng���n", "Đ���ng", "���", "a����b",
                   "���Name.”", "“Name���", "“Name���!”",
                   "  ���Name.”  ", "“Name���  ", "\n���Name\n")
        for source in sources:
            for last_resort in (False, True):
                with self.subTest(source=source, last_resort=last_resort):
                    output, counts, _ = repair.repair(source, last_resort)
                    self.assertEqual(source, output)
                    self.assertEqual({}, counts)
                    result = repair.preflight_source(source)
                    self.assertEqual(source, result["text"])
                    self.assertFalse(result["healthy"])
                    finding = next(row for row in result["findings"]
                                   if row["issue"] == "replacement_characters")
                    self.assertEqual(source.count(repair.FFFD), finding["count"])

    def test_known_repairs_and_verified_repeated_pairs_are_retained(self):
        source = "don�t; coup d��tat; " + "�?" * 25
        output, counts, _ = repair.repair(source)
        self.assertEqual("don’t; coup d’état; …?", output)
        self.assertEqual(1, counts["apostrophe_in_word"])
        self.assertEqual(1, counts["named_phrase:d’état"])
        self.assertEqual(1, counts["collapsed_repeated_pair"])
        self.assertTrue(repair.preflight_source(source)["healthy"])

    def test_partial_repair_survives_unchanged_number_of_warning_categories(self):
        source = "Ng���n said don�t."
        expected = "Ng���n said don’t."
        before = repair.check_source_health(source)
        result = repair.preflight_source(source)
        self.assertEqual(expected, result["text"])
        self.assertFalse(result["healthy"])
        self.assertEqual(len(before["findings"]), len(result["findings"]))
        self.assertEqual(4, before["findings"][0]["count"])
        self.assertEqual(3, result["findings"][0]["count"])
        self.assertEqual({"apostrophe_in_word": 1}, result["applied"])
        self.assertTrue(any("remain" in message for message in result["messages"]))
        self.assertFalse(any("changed nothing" in message
                             for message in result["messages"]))

    def test_native_cli_artifact_preserves_damage_and_reports_partial_repairs(self):
        for source, expected, repaired_count in (
                ("Ng���n", "Ng���n", 0),
                ("Ng���n said don�t.", "Ng���n said don’t.", 1)):
            with self.subTest(source=source), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "source.txt"
                path.write_text(source, encoding="utf-8")
                original = path.read_bytes()
                process = subprocess.run(
                    [sys.executable, str(Path(repair.__file__)), str(path), "--apply"],
                    capture_output=True, text=True, timeout=20)
                self.assertEqual(0, process.returncode, process.stderr)
                output = (Path(tmp) / "source.repaired.txt").read_text(encoding="utf-8")
                self.assertEqual(expected, output)
                self.assertEqual(original, path.read_bytes())
                report = json.loads((Path(tmp) / "source.repair_report.json").read_text())
                self.assertEqual(3, report["replacement_chars_after"])
                self.assertEqual(repaired_count, report["repaired"])
                self.assertNotIn("collapsed_long_run", report["by_rule"])
                self.assertTrue(report["remaining_samples"])
                self.assertEqual(output, repair.preflight_source(source)["text"])


if __name__ == "__main__":
    unittest.main()
