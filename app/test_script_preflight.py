import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from routers import scripts_library
from script_preflight import audit_script


def _entry(text, speaker="Narrator", instruct="Read naturally"):
    return {"text": text, "speaker": speaker, "instruct": instruct}


class ScriptPreflightTests(unittest.TestCase):
    def test_reports_empty_text_and_cyrillic_homoglyphs_as_blocking(self):
        entries = [_entry(""), _entry("The саге was quiet.")]

        report = audit_script(entries)

        self.assertEqual(2, report["counts"]["blocking"])
        self.assertFalse(report["can_apply_repairs"])
        self.assertEqual(
            {"empty_text", "cyrillic_in_text"},
            {finding["code"] for finding in report["findings"]},
        )

    def test_reports_adjacent_multi_entry_duplicate_with_source_evidence(self):
        block = [_entry("A sufficiently long first line."), _entry("A sufficiently long second line.")]
        source = "A sufficiently long first line. A sufficiently long second line."

        report = audit_script(block + block, source)

        duplicate = next(f for f in report["findings"] if f["code"] == "adjacent_duplicate_block")
        self.assertEqual([1, 2, 3, 4], duplicate["entry_numbers"])
        self.assertEqual(1, duplicate["details"]["source_occurrences"])

    def test_does_not_flag_non_adjacent_or_short_repetition_as_duplicate_block(self):
        entries = [_entry("Yes."), _entry("Yes."), _entry("Long repeated phrase here."),
                   _entry("Intervening phrase here."), _entry("Long repeated phrase here.")]

        report = audit_script(entries)

        self.assertNotIn("adjacent_duplicate_block", {f["code"] for f in report["findings"]})

    def test_reuses_generic_speaker_policy_and_only_reports_narration_for_character(self):
        entries = [_entry("Subaru looked toward the door.", "Guard 2"),
                   _entry("Subaru looked toward the door.", "Narrator")]

        report = audit_script(entries, is_generic_speaker_fn=lambda name: name == "Guard 2")

        generic = [f for f in report["findings"] if f["code"] == "generic_speaker"]
        narration = [f for f in report["findings"] if f["code"] == "possible_misattributed_narration"]
        self.assertEqual([1], generic[0]["entry_numbers"])
        self.assertEqual([1], narration[0]["entry_numbers"])

    def test_endpoint_reads_saved_script_and_source_without_writing(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp, "scripts")
            uploads = Path(tmp, "uploads")
            scripts.mkdir()
            uploads.mkdir()
            script_path = scripts / "book.json"
            source_path = uploads / "book.txt"
            original = json.dumps([_entry("The саге was quiet.")])
            script_path.write_text(original, encoding="utf-8")
            source_path.write_text("The sage was quiet.", encoding="utf-8")

            with patch.object(scripts_library, "SCRIPTS_DIR", str(scripts)), \
                 patch.object(scripts_library, "UPLOADS_DIR", str(uploads)):
                report = asyncio.run(scripts_library.preflight_saved_script(
                    "book", scripts_library.ScriptPreflightRequest(source_filename="book.txt")))

            self.assertEqual(1, report["counts"]["blocking"])
            self.assertEqual(original, script_path.read_text(encoding="utf-8"))

    def test_endpoint_rejects_non_text_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp, "scripts")
            scripts.mkdir()
            (scripts / "book.json").write_text("[]", encoding="utf-8")
            with patch.object(scripts_library, "SCRIPTS_DIR", str(scripts)):
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(scripts_library.preflight_saved_script(
                        "book", scripts_library.ScriptPreflightRequest(source_filename="book.epub")))
            self.assertEqual(400, raised.exception.status_code)


if __name__ == "__main__":
    unittest.main()
