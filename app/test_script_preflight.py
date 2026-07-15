import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from routers import scripts_library
from script_preflight import audit_script
from script_repair import build_deterministic_repair


def _entry(text, speaker="Narrator", instruct="Read naturally"):
    return {"text": text, "speaker": speaker, "instruct": instruct}


class ScriptPreflightTests(unittest.TestCase):
    def test_deterministic_repair_is_source_backed_and_does_not_mutate_input(self):
        entries = [_entry("Please take саге of it."), _entry("First repeated line."),
                   _entry("Second repeated line."), _entry("First repeated line."),
                   _entry("Second repeated line.")]
        original = json.loads(json.dumps(entries))
        source = "Please take саге of it. First repeated line. Second repeated line."

        repair = build_deterministic_repair(entries, source)

        self.assertEqual(original, entries)
        self.assertEqual("Please take care of it.", repair["entries"][0]["text"])
        self.assertEqual(3, len(repair["entries"]))
        self.assertEqual([], repair["unresolved"])

    def test_deterministic_repair_refuses_unproven_unicode_and_source_duplicates(self):
        entries = [_entry("Unknown жук token."), _entry("Repeated long first line."),
                   _entry("Repeated long second line."), _entry("Repeated long first line."),
                   _entry("Repeated long second line.")]
        source = "Repeated long first line. Repeated long second line. " * 2

        repair = build_deterministic_repair(entries, source)

        self.assertEqual(entries, repair["entries"])
        self.assertEqual(2, len(repair["unresolved"]))

    def test_empty_entry_becomes_explicit_pause_on_previous_spoken_entry(self):
        entries = [_entry("Spoken line."), _entry("", instruct="A beat of silence."),
                   _entry("Following line.")]

        repair = build_deterministic_repair(entries, "Spoken line. Following line.")

        self.assertEqual(2, len(repair["entries"]))
        self.assertEqual(1000, repair["entries"][0]["pause_after"])
        self.assertEqual("empty_entry_to_pause", repair["changes"][0]["type"])
        self.assertNotIn("pause_after", entries[0])

    def test_empty_entry_repair_refuses_to_overwrite_existing_pause(self):
        previous = _entry("Spoken line.")
        previous["pause_after"] = 750
        entries = [previous, _entry("")]

        repair = build_deterministic_repair(entries, "Spoken line.")

        self.assertEqual(entries, repair["entries"])
        self.assertEqual("previous_pause_already_set", repair["unresolved"][0]["reason"])

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

    def test_apply_requires_current_preview_and_creates_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            scripts = Path(tmp, "scripts")
            uploads = Path(tmp, "uploads")
            scripts.mkdir()
            uploads.mkdir()
            script_path = scripts / "book.json"
            script_path.write_text(json.dumps([_entry("Take саге.")]), encoding="utf-8")
            (uploads / "book.txt").write_text("Take саге.", encoding="utf-8")
            request = scripts_library.ScriptRepairRequest(source_filename="book.txt")

            with patch.object(scripts_library, "SCRIPTS_DIR", str(scripts)), \
                 patch.object(scripts_library, "UPLOADS_DIR", str(uploads)):
                preview = asyncio.run(scripts_library.preview_deterministic_repair("book", request))
                stale = scripts_library.ScriptRepairRequest(
                    source_filename="book.txt", expected_sha256="0" * 64)
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(scripts_library.apply_deterministic_repair("book", stale))
                apply_request = scripts_library.ScriptRepairRequest(
                    source_filename="book.txt", expected_sha256=preview["sha256"])
                result = asyncio.run(scripts_library.apply_deterministic_repair("book", apply_request))

            self.assertEqual(409, raised.exception.status_code)
            self.assertEqual("repaired", result["status"])
            self.assertEqual("Take care.", json.loads(script_path.read_text())[0]["text"])
            self.assertTrue((scripts / result["backup"]).exists())


if __name__ == "__main__":
    unittest.main()
