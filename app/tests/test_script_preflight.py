import asyncio
import json
from collections import Counter
from pathlib import Path
import random
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from alexandria_alignment import is_same_number, is_spelled_number
from chunk_quality import validate_chunk_quality
import generate_script
from routers import scripts_library
from script_preflight import (audit_script, audit_unicode_text,
                              find_contained_source_duplicates,
                              get_boundary_insensitive_counter,
                              _build_number_spelling_digit_index,
                              _number_spelling_digit_lines)
from script_repair import build_deterministic_repair
from speaker_repair import apply_speaker_selections, build_speaker_review
from content_repair import apply_content_selections, build_content_review


def _entry(text, speaker="Narrator", instruct="Read naturally"):
    return {"text": text, "speaker": speaker, "instruct": instruct}


class ScriptPreflightTests(unittest.TestCase):
    def test_non_string_entry_fields_are_blocking(self):
        report = audit_script([{"text": 123, "speaker": ["A"], "instruct": {}}])
        types = [f for f in report["findings"] if f["code"] == "invalid_field_type"]
        self.assertEqual({"text", "speaker", "instruct"},
                         {f["details"]["field"] for f in types})

    def test_content_review_is_selective_and_normalization_is_lossless(self):
        entries = [_entry("Copyright © Publisher", instruct="  Calm,   clear. "),
                   _entry("Chapter One", instruct="Dramatic and nuanced.")]
        review = build_content_review(entries)
        self.assertEqual([1], [item["entry_number"] for item in review["front_matter"]])
        self.assertEqual("Calm, clear.", review["direction_normalizations"][0]["suggested"])
        self.assertEqual(1, len(review["direction_normalizations"]))

    def test_content_apply_requires_expected_values_and_does_not_mutate(self):
        entries = [_entry("Copyright"), _entry("Story", instruct=" Old   direction ")]
        repair = apply_content_selections(entries,
            [{"entry_number": 1, "expected_text": "Copyright"}],
            [{"entry_number": 2, "expected_instruct": " Old   direction ",
              "new_instruct": "Old direction"}])
        self.assertEqual("Copyright", entries[0]["text"])
        self.assertEqual(["Story"], [entry["text"] for entry in repair["entries"]])
        self.assertEqual(" Old   direction ", entries[1]["instruct"])
        self.assertEqual("Old direction", repair["entries"][0]["instruct"])
        with self.assertRaises(ValueError):
            apply_content_selections(entries,
                [{"entry_number": 1, "expected_text": "Changed"}], [])
    def test_speaker_review_only_suggests_strong_third_person_case(self):
        entries = [_entry("Subaru looked toward the door.", "SUBARU"),
                   _entry("She said she would return.", "SUBARU")]
        candidates = build_speaker_review(entries)
        self.assertEqual("NARRATOR", candidates[0]["suggested_speaker"])
        self.assertIsNone(candidates[1]["suggested_speaker"])
        self.assertEqual(2, candidates[0]["next"]["entry_number"])

    def test_speaker_apply_requires_expected_value_and_does_not_mutate(self):
        entries = [_entry("Subaru looked around.", "SUBARU")]
        repaired = apply_speaker_selections(entries, [{"entry_number": 1,
            "expected_speaker": "SUBARU", "new_speaker": "NARRATOR"}])
        self.assertEqual("SUBARU", entries[0]["speaker"])
        self.assertEqual("NARRATOR", repaired["entries"][0]["speaker"])
        with self.assertRaises(ValueError):
            apply_speaker_selections(entries, [{"entry_number": 1,
                "expected_speaker": "EMILIA", "new_speaker": "NARRATOR"}])
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

    def test_deterministic_repair_keeps_cyrillic_word_present_in_source(self):
        entries = [_entry("Он рос быстро.")]
        repair = build_deterministic_repair(entries, "Он рос быстро.")
        self.assertEqual(entries, repair["entries"])
        self.assertEqual([], repair["changes"])
        self.assertEqual([], repair["unresolved"])

    def test_deterministic_repair_refuses_unproven_unicode_and_keeps_source_duplicates(self):
        """Nothing is deleted here, and only the unicode token blocks.

        The source repeats this block, so emitting it twice is faithful
        transcription rather than a defect. It used to be reported as
        `unresolved`, which rejects the chunk: grimgar03 opens with its title
        eight times and could not be generated at all, failing chunk 1 of 49
        after 17 attempts in which the model was correct every time.

        The conservatism that matters is unchanged - the entries are still
        returned untouched, because deleting text the source supports would
        corrupt what the model was asked to reproduce. Only the BLOCKING
        changed, from rejecting the chunk to recording the decision.
        """
        entries = [_entry("Unknown жук token."), _entry("Repeated long first line."),
                   _entry("Repeated long second line."), _entry("Repeated long first line."),
                   _entry("Repeated long second line.")]
        source = "Repeated long first line. Repeated long second line. " * 2

        repair = build_deterministic_repair(entries, source)

        self.assertEqual(entries, repair["entries"])
        self.assertEqual(1, len(repair["unresolved"]))
        self.assertEqual("unsupported_cyrillic_character",
                         repair["unresolved"][0]["reason"])
        self.assertIn("adjacent_duplicate_block_kept",
                      [c.get("type") for c in repair["notes"]])

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

    def test_empty_pause_is_attached_to_a_surviving_duplicate_block_row(self):
        block = [_entry("The first sufficiently long line."),
                 _entry("The second sufficiently long line.")]
        entries = block + [dict(entry) for entry in block] + [_entry("")]
        original = json.loads(json.dumps(entries))
        repair = build_deterministic_repair(entries, " ".join(e["text"] for e in block))
        self.assertEqual(2, len(repair["entries"]))
        self.assertEqual(1000, repair["entries"][1].get("pause_after"))
        self.assertEqual([], repair["unresolved"])
        pause = next(c for c in repair["changes"] if c["type"] == "empty_entry_to_pause")
        self.assertEqual(2, pause["pause_after_entry_number"])
        self.assertEqual(original, entries)

    def test_explicit_empty_pause_preserves_authored_duration_including_zero(self):
        for duration in (0, 500, 1500):
            with self.subTest(duration=duration):
                entries = [_entry("Spoken line."), {**_entry(""), "pause_after": duration},
                           _entry("Following line.")]
                original = json.loads(json.dumps(entries))
                repair = build_deterministic_repair(entries, "Spoken line. Following line.")
                self.assertEqual(2, len(repair["entries"]))
                self.assertEqual(duration, repair["entries"][0]["pause_after"])
                self.assertEqual(duration, repair["changes"][0]["pause_ms"])
                self.assertEqual([], repair["unresolved"])
                self.assertEqual(original, entries)

    def test_untransferable_empty_metadata_and_invalid_pause_are_not_discarded(self):
        for extra in ({"chapter": "Part Two"}, {"pause_after": -1},
                      {"pause_after": "500"}, {"pause_after": True}):
            with self.subTest(extra=extra):
                entries = [_entry("Spoken line."), {**_entry(""), **extra}]
                repair = build_deterministic_repair(entries, "Spoken line.")
                self.assertEqual(entries, repair["entries"])
                self.assertEqual([], repair["changes"])
                self.assertEqual(1, len(repair["unresolved"]))

    def test_saved_repair_preserves_pause_after_duplicate_removal_and_keeps_backup(self):
        block = [_entry("The first sufficiently long line."),
                 _entry("The second sufficiently long line.")]
        entries = block + [dict(entry) for entry in block] + [
            {**_entry(""), "pause_after": 500}, _entry("Following spoken line.")]
        with tempfile.TemporaryDirectory() as folder:
            scripts = Path(folder, "scripts"); scripts.mkdir()
            uploads = Path(folder, "uploads"); uploads.mkdir()
            script_path = scripts / "book.json"
            original = json.dumps(entries).encode()
            script_path.write_bytes(original)
            (uploads / "book.txt").write_text(
                " ".join(e["text"] for e in block) + " Following spoken line.")
            with patch.object(scripts_library, "SCRIPTS_DIR", str(scripts)), \
                 patch.object(scripts_library, "UPLOADS_DIR", str(uploads)):
                preview = asyncio.run(scripts_library.preview_deterministic_repair(
                    "book", scripts_library.ScriptRepairRequest(source_filename="book.txt")))
                result = asyncio.run(scripts_library.apply_deterministic_repair(
                    "book", scripts_library.ScriptRepairRequest(
                        source_filename="book.txt", expected_sha256=preview["sha256"])))
            saved = json.loads(script_path.read_bytes())
            self.assertEqual(3, len(saved))
            self.assertEqual(500, saved[1].get("pause_after"))
            self.assertEqual(original, (scripts / result["backup"]).read_bytes())
            from pydub import AudioSegment
            from tts import compute_timeline
            segment = AudioSegment(data=b"\x01\x10" * 2400, sample_width=2,
                                   frame_rate=24000, channels=1)
            timeline = compute_timeline([(entry, segment) for entry in saved],
                                        pause_ms=777, same_speaker_pause_ms=111)
            gap = AudioSegment.silent(duration=500).set_frame_rate(24000)
            self.assertAlmostEqual(gap.duration_seconds * 1000,
                timeline[2][2] - (timeline[1][2] + timeline[1][1].duration_seconds * 1000))

    def test_saved_repair_refuses_untransferable_empty_metadata_without_rewriting(self):
        entries = [_entry("Spoken line."), {**_entry(""), "chapter": "Part Two"}]
        with tempfile.TemporaryDirectory() as folder:
            scripts = Path(folder, "scripts"); scripts.mkdir()
            uploads = Path(folder, "uploads"); uploads.mkdir()
            path = scripts / "book.json"
            original = json.dumps(entries).encode()
            path.write_bytes(original)
            (uploads / "book.txt").write_text("Spoken line.")
            with patch.object(scripts_library, "SCRIPTS_DIR", str(scripts)), \
                 patch.object(scripts_library, "UPLOADS_DIR", str(uploads)):
                preview = asyncio.run(scripts_library.preview_deterministic_repair(
                    "book", scripts_library.ScriptRepairRequest(source_filename="book.txt")))
                self.assertTrue(preview["unresolved"])
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(scripts_library.apply_deterministic_repair(
                        "book", scripts_library.ScriptRepairRequest(
                            source_filename="book.txt", expected_sha256=preview["sha256"])))
            self.assertEqual(409, raised.exception.status_code)
            self.assertEqual(original, path.read_bytes())

    def test_saved_repair_cannot_use_embedded_source_tokens_to_authorize_deletion(self):
        block = [_entry("foo bar baz qux quux"),
                 _entry("another fabricated line fits five words")]
        entries = block + [dict(entry) for entry in block]
        with tempfile.TemporaryDirectory() as folder:
            scripts = Path(folder, "scripts"); scripts.mkdir()
            uploads = Path(folder, "uploads"); uploads.mkdir()
            path = scripts / "book.json"
            original = json.dumps(entries).encode()
            path.write_bytes(original)
            (uploads / "book.txt").write_text("pre" + " ".join(e["text"] for e in block))
            with patch.object(scripts_library, "SCRIPTS_DIR", str(scripts)), \
                 patch.object(scripts_library, "UPLOADS_DIR", str(uploads)):
                preview = asyncio.run(scripts_library.preview_deterministic_repair(
                    "book", scripts_library.ScriptRepairRequest(source_filename="book.txt")))
                with self.assertRaises(HTTPException) as raised:
                    asyncio.run(scripts_library.apply_deterministic_repair(
                        "book", scripts_library.ScriptRepairRequest(
                            source_filename="book.txt", expected_sha256=preview["sha256"])))
            self.assertEqual(409, raised.exception.status_code)
            self.assertTrue(preview["unresolved"])
            self.assertEqual(original, path.read_bytes())

    def test_reports_empty_text_and_cyrillic_homoglyphs_as_blocking(self):
        entries = [_entry(""), _entry("The саге was quiet.")]

        report = audit_script(entries)

        self.assertEqual(2, report["counts"]["blocking"])
        self.assertFalse(report["can_apply_repairs"])
        self.assertEqual(
            {"empty_text", "introduced_unicode_script"},
            {finding["code"] for finding in report["findings"]},
        )

    def test_nonprose_risk_is_manual_and_validation_state_is_explicit(self):
        report = audit_script([
            _entry("Reference AB-1234-Z"),
            _entry("Visit https://example.com/help"),
            _entry("One • Two • Three"),
            _entry("She arrived on 12 August 2026."),
        ])
        findings = [item for item in report["findings"]
                    if item["code"] == "nonprose_speech_risk"]
        self.assertEqual([1, 2, 3], [item["entry_numbers"][0] for item in findings])
        self.assertEqual(0, report["counts"]["blocking"])
        for finding in findings:
            validation = finding["details"]["validation"]
            self.assertTrue(validation["recommended"])
            self.assertEqual("not_run", validation["status"])
        identifier = findings[0]["details"]
        self.assertEqual("Reference AB-1234-Z.", identifier["normalized_preview"])
        self.assertEqual([{
            "type": "normalized_sentence_boundary", "trimmed_characters": 0,
            "appended_period": True}], identifier["transformations"])

    def test_legitimate_japanese_is_allowed_when_source_backed(self):
        report = audit_script([_entry("彼はありがとうと言った。")], "彼はありがとうと言った。")
        self.assertEqual(0, report["counts"]["blocking"])

    def test_introduced_hiragana_and_mixed_word_are_reported(self):
        report = audit_script([_entry("Subあru saw a cat.")], "Subaru saw a cat.")
        codes = {finding["code"] for finding in report["findings"]}
        self.assertIn("introduced_unicode_script", codes)
        self.assertIn("mixed_script_word", codes)

    def test_unicode_audit_reports_nfc_and_unsafe_characters(self):
        report = audit_unicode_text("Cafe\u0301\ufffd\x00")
        self.assertFalse(report["is_nfc"])
        self.assertEqual(1, report["replacement_character_count"])
        self.assertEqual(["U+0000"], report["unsafe_controls"])

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

    def test_flags_paraphrased_adjacent_pair_as_non_blocking_manual_review(self):
        first = "The old sailor walked slowly down the narrow creaking wooden pier before dawn."
        second = "The old sailor walked slowly down the narrow creaking wooden dock before dawn."

        report = audit_script([_entry(first), _entry(second)], source_text=first)

        near_dup = next(f for f in report["findings"] if f["code"] == "adjacent_near_duplicate")
        self.assertEqual([1, 2], near_dup["entry_numbers"])
        self.assertEqual("manual_review", near_dup["severity"])
        self.assertGreaterEqual(near_dup["details"]["similarity"], 0.90)
        self.assertTrue(report["can_apply_repairs"])

    def test_flags_identical_adjacent_pair_too_short_for_block_detector(self):
        line = "A repeated sentence of decent length here."

        report = audit_script([_entry(line), _entry(line)], source_text=line)

        near_dup = next(f for f in report["findings"] if f["code"] == "adjacent_near_duplicate")
        self.assertEqual([1, 2], near_dup["entry_numbers"])
        self.assertEqual(1.0, near_dup["details"]["similarity"])

    def test_does_not_flag_near_duplicate_when_both_sides_are_source_backed(self):
        line = "The captain repeated the same warning twice."
        source = f"{line} {line}"

        report = audit_script([_entry(line), _entry(line)], source_text=source)

        self.assertNotIn("adjacent_near_duplicate", {f["code"] for f in report["findings"]})

    def test_does_not_flag_short_echo_as_near_duplicate(self):
        report = audit_script([_entry("No."), _entry("No.")])

        self.assertNotIn("adjacent_near_duplicate", {f["code"] for f in report["findings"]})

    def test_near_duplicate_skips_entries_already_covered_by_exact_block(self):
        block = [_entry("A sufficiently long first line."), _entry("A sufficiently long second line.")]
        source = "A sufficiently long first line. A sufficiently long second line."

        report = audit_script(block + block, source)

        codes = {f["code"] for f in report["findings"]}
        self.assertIn("adjacent_duplicate_block", codes)
        self.assertNotIn("adjacent_near_duplicate", codes)

    def test_does_not_flag_distinct_adjacent_entries_as_near_duplicate(self):
        entries = [_entry("The weather in the mountains was cold and clear this morning."),
                   _entry("My favorite recipe requires flour sugar eggs and melted butter today.")]

        report = audit_script(entries)

        self.assertNotIn("adjacent_near_duplicate", {f["code"] for f in report["findings"]})

    def test_audit_script_surfaces_near_duplicate_alongside_other_manual_review_findings(self):
        first = "The old sailor walked slowly down the narrow creaking wooden pier before dawn."
        second = "The old sailor walked slowly down the narrow creaking wooden dock before dawn."
        entries = [_entry(first), _entry(second), _entry("Some other line entirely.", instruct="")]

        report = audit_script(entries, source_text=first)

        codes = {f["code"] for f in report["findings"]}
        self.assertIn("adjacent_near_duplicate", codes)
        self.assertIn("missing_instruction", codes)
        near_dup = next(f for f in report["findings"] if f["code"] == "adjacent_near_duplicate")
        self.assertEqual([1, 2], near_dup["entry_numbers"])
        self.assertGreaterEqual(report["counts"]["manual_review"], 2)

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




class NearDuplicateNormalizationTests(unittest.TestCase):
    def test_case_and_punctuation_variants_need_two_source_occurrences(self):
        from script_preflight import find_adjacent_near_duplicate_entries
        first = 'The captain repeated the same warning twice.'
        variants = (first.upper(), 'The captain, repeated the same warning twice!',
                    'The captain repeated the same warning twice…')
        for second in variants:
            with self.subTest(second=second):
                direct = find_adjacent_near_duplicate_entries([first, second], first)
                self.assertEqual(1, len(direct))
                self.assertEqual([1, 2], direct[0]['entry_numbers'])
                self.assertEqual(1.0, direct[0]['details']['similarity'])
                self.assertEqual([False, False], direct[0]['details']['source_supported'])
                entries = [_entry(first), _entry(second)]
                before = json.loads(json.dumps(entries))
                report = audit_script(entries, first)
                findings = [f for f in report['findings'] if f['code'] == 'adjacent_near_duplicate']
                self.assertEqual(1, len(findings))
                self.assertEqual('manual_review', findings[0]['severity'])
                self.assertEqual(0, report['counts']['blocking'])
                self.assertEqual(before, entries)
                repeated_source = first + ' ' + first
                self.assertEqual([], find_adjacent_near_duplicate_entries([first, second], repeated_source))
                self.assertNotIn('adjacent_near_duplicate', {f['code'] for f in audit_script(entries, repeated_source)['findings']})

    def test_supported_distinct_near_lines_and_short_echoes_remain_unflagged(self):
        from script_preflight import find_adjacent_near_duplicate_entries
        first = 'The captain repeated the same warning twice before dawn today.'
        second = 'The captain repeated the same warning twice before sunset today.'
        self.assertEqual([], find_adjacent_near_duplicate_entries([first, second], first + ' ' + second))
        self.assertEqual([], find_adjacent_near_duplicate_entries(['No.', 'NO!'], 'No.'))
        self.assertEqual([], find_adjacent_near_duplicate_entries([
            first, 'Her favorite recipe calls for flour sugar and eggs.'], first))


class MixedSourceCyrillicRepairTests(unittest.TestCase):
    def test_source_backed_cyrillic_survives_mixed_prose_but_ocr_and_introductions_stay_guarded(self):
        import copy
        source = 'He said привет and left.'
        rows = [_entry(source), _entry('')]
        before = copy.deepcopy(rows)
        repair = build_deterministic_repair(rows, source)
        self.assertEqual([], repair['unresolved'])
        self.assertEqual(source, repair['entries'][0]['text'])
        self.assertTrue(any(row['type'] == 'empty_entry_to_pause' for row in repair['changes']))
        self.assertEqual(before, rows)
        introduced = build_deterministic_repair([_entry(source)], 'He said hello and left.')
        self.assertTrue(any(row['reason'] == 'unsupported_cyrillic_character' for row in introduced['unresolved']))
        corruption = build_deterministic_repair([_entry('Please take саге of it.')], 'Please take саге of it.')
        self.assertEqual('Please take care of it.', corruption['entries'][0]['text'])
        self.assertEqual([], corruption['unresolved'])


class MixedSourceRepairRouteTests(unittest.TestCase):
    def test_native_preview_apply_preserves_mixed_source_word_and_refuses_introduced_word(self):
        for source_backed in (True, False):
            with self.subTest(source_backed=source_backed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp); scripts = root / 'scripts'; scripts.mkdir(); uploads = root / 'uploads'; uploads.mkdir()
                rows = [_entry('He said привет and left.'), _entry(''), _entry('Following synthetic line.')]
                book = scripts / 'book.json'; book.write_text(json.dumps(rows)); before = book.read_bytes()
                source = uploads / 'source.txt'; source.write_text(('He said привет and left.' if source_backed else 'He said hello and left.') + ' Following synthetic line.', encoding='utf-8'); source_before = source.read_bytes()
                with patch.object(scripts_library, 'SCRIPTS_DIR', str(scripts)), patch.object(scripts_library, 'UPLOADS_DIR', str(uploads)):
                    preview = scripts_library._preview_deterministic_repair_sync('book', scripts_library.ScriptRepairRequest(source_filename='source.txt'))
                    request = scripts_library.ScriptRepairRequest(source_filename='source.txt', expected_sha256=preview['sha256'])
                    if source_backed:
                        result = scripts_library._apply_deterministic_repair_sync('book', request)
                        self.assertEqual('repaired', result['status'])
                        saved = json.loads(book.read_text())
                        self.assertEqual(rows[0]['text'], saved[0]['text'])
                        self.assertGreater(saved[0]['pause_after'], 0)
                        self.assertEqual(before, (scripts / result['backup']).read_bytes())
                    else:
                        with self.assertRaises(HTTPException) as refused:
                            scripts_library._apply_deterministic_repair_sync('book', request)
                        self.assertEqual(409, refused.exception.status_code)
                        self.assertEqual(before, book.read_bytes())
                self.assertEqual(source_before, source.read_bytes())


_SEPARATE_DUPLICATE_FILLER = (
    "amber birch cedar dune ember fjord glacier harbour island juniper kettle lantern "
    "meadow nectar orchard pebble quartz river saddle timber umber valley willow xenon "
    "yellow zephyr").split() * 5

# (as written with a typography variant, the same words plainly)
_TYPOGRAPHY_VARIANTS = (
    ("Wel\u00adcome", "Welcome"),   # soft hyphen
    ("Can\u0060t", "Can't"),        # grave accent
    ("Can\u00b4t", "Can't"),        # acute accent
    ("Can\u2018t", "Can't"),        # left single quotation mark
    ("Can\u02bct", "Can't"),        # modifier letter apostrophe
    ("\uff2f\uff2b", "OK"),         # full-width Latin letters
)


class ContainedSeparateDuplicateTests(unittest.TestCase):
    """A line repeated as separate character entries is still source-counted."""

    LINE = "Where is the train?"

    def _entries(self, line=None):
        before = [_entry(word) for word in _SEPARATE_DUPLICATE_FILLER[:60]]
        after = [_entry(word) for word in _SEPARATE_DUPLICATE_FILLER[60:]]
        return (before + [_entry(line or self.LINE, "MARTA")] + after
                + [_entry(line or self.LINE, "PORTER")])

    def _adjacent_entries(self, line, copies=2):
        """A faithful split of one fused source token: the copies are adjacent."""
        return ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER]
                + [_entry(line, "C%d" % k) for k in range(copies)])

    def _source(self, occurrences=1, extra=""):
        quotes = " ".join('"%s" she asked.' % self.LINE for _ in range(occurrences))
        return " ".join(_SEPARATE_DUPLICATE_FILLER) + extra + " " + quotes

    def _duplicate_findings(self, entries, source):
        return [finding for finding in find_contained_source_duplicates(entries, source)
                if finding["code"] == "contained_source_duplicate"]

    def test_separate_character_duplicate_is_reported(self):
        entries = self._entries()
        findings = self._duplicate_findings(entries, self._source())

        self.assertEqual(1, len(findings))
        duplicate = findings[0]
        self.assertEqual("blocking", duplicate["severity"])
        self.assertEqual([61, 132], duplicate["entry_numbers"])
        self.assertEqual(1, duplicate["details"]["source_occurrences"])
        self.assertEqual(2, duplicate["details"]["all_text_occurrences"])
        self.assertEqual(2, duplicate["details"]["output_quotation_occurrences"])

    def test_two_source_occurrences_are_a_legitimate_repeat(self):
        self.assertEqual([], self._duplicate_findings(self._entries(), self._source(2)))

    def test_short_line_repeated_as_often_as_the_source_is_not_reported(self):
        source = " ".join(_SEPARATE_DUPLICATE_FILLER) + ' "Yes." "Yes." "Yes."'
        entries = ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER]
                   + [_entry("Yes.", "MARTA"), _entry("Yes.", "PORTER"),
                      _entry("Yes.", "ANNA")])

        self.assertEqual([], self._duplicate_findings(entries, source))

    def test_case_punctuation_and_quote_style_follow_the_source_count(self):
        entries = ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER]
                   + [_entry("yes.", "MARTA"), _entry("Yes", "PORTER")])
        repeated = " ".join(_SEPARATE_DUPLICATE_FILLER) + " \u201cYES!\u201d he said. \u00abyes\u00bb she said."
        single = " ".join(_SEPARATE_DUPLICATE_FILLER) + " \u201cYES!\u201d he said."

        self.assertEqual([], self._duplicate_findings(entries, repeated))
        self.assertTrue(self._duplicate_findings(entries, single))

    def test_japanese_quoted_line_follows_the_source_count(self):
        entries = ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER]
                   + [_entry("\u306f\u3044", "MARTA"), _entry("\u306f\u3044", "PORTER")])
        once = " ".join(_SEPARATE_DUPLICATE_FILLER) + " \u300c\u306f\u3044\u300d\u3068\u5f7c\u306f\u8a00\u3063\u305f\u3002"
        twice = once + " \u300c\u306f\u3044\u300d\u3068\u5f7c\u5973\u306f\u7b54\u3048\u305f\u3002"

        finding = self._duplicate_findings(entries, once)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])
        self.assertEqual([], self._duplicate_findings(entries, twice))

    def test_line_split_across_two_entries_is_not_reported(self):
        source = (" ".join(_SEPARATE_DUPLICATE_FILLER)
                  + ' "Do not wait for me" she said.')
        entries = ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER]
                   + [_entry("Do not wait", "MARTA"), _entry("for me", "MARTA")])

        self.assertEqual([], self._duplicate_findings(entries, source))

    def test_unrelated_line_is_not_downgraded_by_a_number_elsewhere(self):
        # "Where is the train?" cannot be the spelled form of "2", so a
        # chapter number rendered as words elsewhere must not excuse its repeat.
        findings = self._duplicate_findings(self._entries(),
                                            self._source(extra=" Chapter 2."))

        self.assertEqual(1, len(findings))
        self.assertEqual("blocking", findings[0]["severity"])
        self.assertFalse(findings[0]["details"]["rendering_capacity_unknown"])
        self.assertEqual(0, findings[0]["details"]["unemitted_source_split_support"])

    def test_missing_source_abstains(self):
        entries = self._entries()

        self.assertEqual([], find_contained_source_duplicates(entries, None))
        self.assertNotIn("contained_source_duplicate",
                         {finding["code"] for finding in audit_script(entries)["findings"]})

    def test_mixed_unicode_normalisation_of_one_word_is_not_a_duplicate(self):
        composed, decomposed = "caf\u00e9", "cafe\u0301"
        source = (" ".join(_SEPARATE_DUPLICATE_FILLER)
                  + ' "%s" she said. "%s" he said.' % (composed, decomposed))

        self.assertEqual([], self._duplicate_findings(self._entries(composed), source))
        self.assertEqual([], self._duplicate_findings(self._entries(decomposed), source))

    def test_one_source_word_still_counts_an_extra_copy_in_either_spelling(self):
        composed, decomposed = "caf\u00e9", "cafe\u0301"
        for source_word, emitted in ((decomposed, composed), (composed, decomposed)):
            with self.subTest(source_word=source_word):
                source = (" ".join(_SEPARATE_DUPLICATE_FILLER)
                          + ' "%s" she said.' % source_word)
                findings = self._duplicate_findings(self._entries(emitted), source)

                self.assertEqual(1, len(findings))
                self.assertEqual("blocking", findings[0]["severity"])
                self.assertEqual(1, findings[0]["details"]["source_occurrences"])

    def test_audit_script_does_not_block_a_mixed_normalisation_copy(self):
        source = (" ".join(_SEPARATE_DUPLICATE_FILLER)
                  + ' "caf\u00e9" she said. "cafe\u0301" he said.')
        report = audit_script(self._entries("caf\u00e9"), source)

        self.assertEqual(0, report["counts"]["blocking"])

    def test_combining_marks_keep_a_faithful_fused_thai_split_unblocked(self):
        # _WORD_RE cannot match a combining mark, so "\u0e41\u0e25\u0e49\u0e27"
        # tokenized as "\u0e41\u0e25" + "\u0e27"; the fused source
        # "\u0e41\u0e25\u0e49\u0e27\u0e40\u0e0a\u0e49\u0e32" then looked
        # like one extra copy of a phrase the source carried once.
        source = "\u0e41\u0e25\u0e49\u0e27 \u0e41\u0e25\u0e49\u0e27\u0e40\u0e0a\u0e49\u0e32"
        entries = [_entry("\u0e41\u0e25\u0e49\u0e27", "MARTA"),
                   _entry("\u0e41\u0e25\u0e49\u0e27", "PORTER"),
                   _entry("\u0e40\u0e0a\u0e49\u0e32", "ANNA")]

        blocking = [finding for finding in self._duplicate_findings(entries, source)
                    if finding["severity"] == "blocking"]
        self.assertEqual([], blocking)
        self.assertEqual(0, audit_script(entries, source)["counts"]["blocking"])

    def test_marked_thai_words_with_different_tones_stay_distinct(self):
        # \u0e01\u0e48\u0e32 and \u0e01\u0e49\u0e32 differ only by a mark.
        # Dropping marks would merge them, so a source carrying both would look
        # like it carried two copies of whichever one the output repeated.
        source = "\u0e01\u0e48\u0e32 \u0e01\u0e49\u0e32"
        both_once = [_entry("\u0e01\u0e48\u0e32", "MARTA"),
                     _entry("\u0e01\u0e49\u0e32", "PORTER")]
        one_twice = [_entry("\u0e01\u0e48\u0e32", "MARTA"),
                     _entry("\u0e01\u0e48\u0e32", "PORTER")]

        self.assertEqual([], self._duplicate_findings(both_once, source))
        findings = self._duplicate_findings(one_twice, source)
        self.assertEqual(1, len(findings))
        self.assertEqual("blocking", findings[0]["severity"])
        self.assertEqual(1, findings[0]["details"]["source_occurrences"])
        self.assertEqual(1, audit_script(one_twice, source)["counts"]["blocking"])

    def test_casefold_produced_mark_agrees_with_the_decomposed_spelling(self):
        # "\u0130".casefold() is "i\u0307". A source "\u0130stanbul" and an
        # emitted "i\u0307stanbul" must be one word, so case folding happens
        # before any mark handling.
        entries = [_entry("i\u0307stanbul", "MARTA"),
                   _entry("i\u0307stanbul", "PORTER")]

        findings = self._duplicate_findings(entries, "\u0130stanbul")
        self.assertEqual(1, len(findings))
        self.assertEqual("blocking", findings[0]["severity"])
        self.assertEqual(1, findings[0]["details"]["source_occurrences"])

    def test_source_typography_variants_are_not_an_extra_copy(self):
        # The source carries the line twice, once in a typography variant. The
        # model wrote both copies plainly, which is the same line twice.
        for variant, plain in _TYPOGRAPHY_VARIANTS:
            with self.subTest(variant=variant):
                source = f'"{variant}." He left. "{plain}," she said.'
                entries = [_entry(f"{plain}.", "MARTA"), _entry("He left."),
                           _entry(f"{plain}.", "PORTER"), _entry("she said.")]

                self.assertEqual([], self._duplicate_findings(entries, source))

    def test_output_typography_variants_are_not_an_extra_copy(self):
        # The reverse: plain in the source twice, one emitted copy in a variant.
        for variant, plain in _TYPOGRAPHY_VARIANTS:
            with self.subTest(variant=variant):
                source = f'"{plain}." He left. "{plain}," she said.'
                entries = [_entry(f"{plain}.", "MARTA"), _entry("He left."),
                           _entry(f"{variant}.", "PORTER"), _entry("she said.")]

                self.assertEqual([], self._duplicate_findings(entries, source))

    def test_a_real_duplicate_differing_only_in_typography_still_blocks(self):
        # The source carries the line once; the two emitted copies differ only
        # by typography, which is not two different lines.
        for variant, plain in _TYPOGRAPHY_VARIANTS:
            with self.subTest(variant=variant):
                source = f'"{plain}," she said.'
                entries = [_entry(f"{variant}.", "MARTA"), _entry("she said."),
                           _entry(f"{plain}.", "PORTER")]

                finding = self._duplicate_findings(entries, source)[0]
                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(1, finding["details"]["source_occurrences"])
                self.assertEqual(2, finding["details"]["all_text_occurrences"])

    def test_a_source_word_boundary_accepts_the_spaced_and_fused_spelling(self):
        # U+200B is a word boundary (#2316): "Go\u200bhome" in the source is one
        # copy of the two words "Go home". Writing them twice is a duplicate
        # whether the model keeps the boundary, writes a space, or drops it; a
        # kept boundary in the output must compare equal to both source keys.
        source = '"Go\u200bhome," she said.'
        for line in ("Go home", "Gohome", "Go\u200bhome"):
            with self.subTest(line=line):
                entries = [_entry(line + ".", "MARTA"), _entry("she said."),
                           _entry(line + ".", "PORTER")]

                finding = self._duplicate_findings(entries, source)[0]
                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(1, finding["details"]["source_occurrences"])
                self.assertEqual(2, finding["details"]["all_text_occurrences"])

    def test_a_faithful_single_copy_is_clean_under_either_boundary_spelling(self):
        # The same one source copy, emitted once, is not a finding in any
        # spelling: spaced or kept must not be read as a repeat, and fused must
        # not lose the source's word coverage.
        source = '"Go\u200bhome," she said.'
        for line in ("Go home", "Gohome", "Go\u200bhome"):
            with self.subTest(line=line):
                entries = [_entry(line + ".", "MARTA"), _entry("she said.")]

                self.assertEqual([], self._duplicate_findings(entries, source))
                report = audit_script(entries, source)
                self.assertEqual(0, report["counts"]["blocking"])
                self.assertNotIn("low_source_word_coverage",
                                 {finding["code"] for finding in report["findings"]})

    def test_a_thai_boundary_character_accepts_the_spaced_spelling(self):
        # The reported failure: the source carries "สวัสดี\u200bครับ" once, the
        # model writes the two words with a space three times, and the fused
        # source key alone matched nothing.
        source = '"สวัสดี\u200bครับ" เธอพูด'
        spaced = "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35 \u0e04\u0e23\u0e31\u0e1a"
        fused = "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35\u0e04\u0e23\u0e31\u0e1a"

        finding = self._duplicate_findings(
            [_entry(spaced, "C%d" % index) for index in range(3)], source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])
        self.assertEqual(3, finding["details"]["all_text_occurrences"])
        self.assertEqual("blocking",
                         self._duplicate_findings(
                             [_entry(fused, "C%d" % index) for index in range(3)],
                             source)[0]["severity"])

    def test_a_source_with_two_boundary_copies_carries_both_into_capacity(self):
        # Two source copies of the same boundary-bearing line stay two copies
        # after the boundary is resolved, so a third written copy still blocks.
        source = "Go\u200bhome. Then the rain came. Go\u200bhome."
        faithful = [_entry("Go home.", "MARTA"), _entry("Then the rain came."),
                    _entry("Go home.", "PORTER")]

        self.assertEqual([], self._duplicate_findings(faithful, source))
        finding = self._duplicate_findings(faithful + [_entry("Go home.", "ANNA")],
                                           source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(2, finding["details"]["source_occurrences"])
        self.assertEqual(3, finding["details"]["all_text_occurrences"])

    def test_a_word_joiner_is_not_a_word_boundary(self):
        # U+2060 is the opposite of U+200B (#2316): it joins what it sits
        # between and is deleted, never turned into a space, so the fused
        # spelling is the source's one word. Both spellings share that single
        # copy of capacity (and that fused token must not be credited a second
        # time as unemitted split support), so two copies in either spelling
        # block.
        source = "Go\u2060home"
        fused = [_entry("Gohome", "MARTA"), _entry("Gohome", "PORTER")]
        spaced = [_entry("Go home", "MARTA"), _entry("Go home", "PORTER")]

        finding = self._duplicate_findings(fused, source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])
        self.assertEqual([], self._duplicate_findings(spaced[:1], source))
        finding = self._duplicate_findings(spaced, source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])
        self.assertEqual(0, finding["details"]["unemitted_source_split_support"])

    def test_an_output_boundary_does_not_change_a_source_without_one(self):
        # A source with no boundary character has one spelling, so an output
        # that keeps a ZWSP keeps the previous treatment (deleted): the line is
        # the source's one word and two copies still block.
        source = '"Gohome," she said.'
        kept = "Go\u200bhome"
        duplicate = [_entry(kept + ".", "MARTA"), _entry("she said."),
                     _entry(kept + ".", "PORTER")]

        finding = self._duplicate_findings(duplicate, source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])
        self.assertEqual(2, finding["details"]["all_text_occurrences"])
        self.assertEqual([], self._duplicate_findings(
            [_entry(kept + ".", "MARTA"), _entry("she said.")], source))

    def test_mixed_boundary_source_keeps_two_copies_for_every_output_spelling(self):
        # The source carries the line twice, once spaced and once with U+200B.
        # Capacity is per line, not per spelling, so either written spelling is
        # clean twice and blocks on a third copy. This is also the over-grant
        # example: two source copies must stay two, never three or four.
        source = '"Go home," she said. "Go\u200bhome," he said.'
        for line in ("Go home", "Gohome", "Go\u200bhome"):
            with self.subTest(line=line):
                faithful = [_entry(line + ".", "MARTA"), _entry("she said."),
                            _entry(line + ".", "PORTER"), _entry("he said.")]
                self.assertEqual([], self._duplicate_findings(faithful, source))
                finding = self._duplicate_findings(
                    faithful + [_entry(line + ".", "ANNA")], source)[0]
                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(2, finding["details"]["source_occurrences"])
                self.assertEqual(3, finding["details"]["all_text_occurrences"])

    def test_a_kept_boundary_matches_a_spaced_source_without_one(self):
        # The source has no U+200B; a ZWSP the model kept is still a word break,
        # so it is one copy of the spaced line and a second copy blocks.
        cases = (
            ('"Go home," she said.', "Go\u200bhome", "."),
            ('"สวัสดี ครับ" เธอพูด', "สวัสดี\u200bครับ", ""),
        )
        for source, line, suffix in cases:
            with self.subTest(line=line):
                self.assertEqual([], self._duplicate_findings(
                    [_entry(line + suffix, "MARTA")], source))
                finding = self._duplicate_findings(
                    [_entry(line + suffix, "MARTA"),
                     _entry(line + suffix, "PORTER")], source)[0]
                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(1, finding["details"]["source_occurrences"])
                self.assertEqual(2, finding["details"]["all_text_occurrences"])

    def test_zwsp_positions_do_not_change_capacity(self):
        # Start, end, repeated and next-to-punctuation positions are all the
        # same word break, so the line still has one source copy.
        cases = (
            ('"\u200bGo home," she said.', "Go home"),
            ('"Go home\u200b," she said.', "Go home"),
            ('"Go\u200b\u200bhome," she said.', "Go home"),
            ('"Go home.\u200b" she said.', "Go home"),
        )
        for source, line in cases:
            with self.subTest(source=source):
                self.assertEqual([], self._duplicate_findings(
                    [_entry(line + ".", "MARTA"), _entry("she said.")], source))
                finding = self._duplicate_findings(
                    [_entry(line + ".", "MARTA"), _entry("she said."),
                     _entry(line + ".", "PORTER")], source)[0]
                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(1, finding["details"]["source_occurrences"])

    def test_a_cjk_word_with_an_internal_zwsp_is_one_word(self):
        # A line-break hint inside a CJK word must not make the word two.
        source = '"東\u200b京," she said.'
        self.assertEqual([], self._duplicate_findings(
            [_entry("東京", "MARTA"), _entry("she said.")], source))
        finding = self._duplicate_findings(
            [_entry("東京", "MARTA"), _entry("she said."),
             _entry("東京", "PORTER")], source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])

        # One spaced copy and one intra-word-ZWSP copy are two source copies
        # of the same fused line, not one.
        mixed = "東 京. Then the rain came. 東\u200b京."
        faithful = [_entry("東京", "MARTA"), _entry("Then the rain came."),
                    _entry("東京", "PORTER")]
        self.assertEqual([], self._duplicate_findings(faithful, mixed))

    def test_punctuation_still_separates_two_boundary_lines(self):
        # The spelling-independent count fuses tokens across whitespace, never
        # across punctuation: "Go. Home" is not one copy of "Gohome".
        source = "Go. Home\u200b."
        entries = [_entry("Gohome.", "MARTA"), _entry("Gohome.", "PORTER")]

        self.assertEqual([], self._duplicate_findings(entries, source))

    def test_a_boundary_line_is_not_counted_inside_a_longer_word(self):
        # A copy must start and end on token boundaries, so "Gohome" is not a
        # source copy of "Gohomework".
        source = "Gohomework\u200b."
        entries = [_entry("Gohome.", "MARTA"), _entry("Gohome.", "PORTER")]

        self.assertEqual([], self._duplicate_findings(entries, source))

    def test_boundary_capacity_counts_non_overlapping_copies_only(self):
        # "gogo" appears once (two "go" tokens) followed by another "go". The
        # overlapping second start must not turn one source copy into two and
        # let a genuine duplicate through.
        source = "go\u200bgo\u200bgo"
        finding = self._duplicate_findings(
            [_entry("Gogo", "MARTA"), _entry("Gogo", "PORTER")], source)[0]

        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])

    def test_capacity_is_per_line_not_per_spelling(self):
        # A plain source with no U+200B still spells one line two ways: "the
        # cat" and "thecat" are two copies. The spelling-independent count
        # gives each spelling capacity two without letting unemitted split
        # support credit the same fused token a second time, so a third copy in
        # either spelling blocks.
        source = '"the cat" sat. Then thecat left.'
        for line in ("the cat.", "thecat."):
            with self.subTest(line=line):
                faithful = [_entry(line, "MARTA"), _entry("sat."),
                            _entry(line, "PORTER"), _entry("Then left.")]
                self.assertEqual([], self._duplicate_findings(faithful, source))
                extra = faithful + [_entry(line, "ANNA")]
                finding = self._duplicate_findings(extra, source)[0]
                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(2, finding["details"]["source_occurrences"])
                self.assertEqual(3, finding["details"]["all_text_occurrences"])
                self.assertEqual(0, finding["details"]["unemitted_source_split_support"])
                self.assertFalse(generate_script.passes_final_generation_gate(
                    validate_chunk_quality(source, extra),
                    audit_script(extra, source)))

    def test_fused_source_copies_are_not_counted_twice_in_support(self):
        # N fused whole-token copies counted into capacity by the
        # spelling-independent counter must never be credited again by
        # unspent_token_support (which otherwise allowed 2N spaced emissions).
        source = "The thecat sat. Then thecat ran."
        entries = [_entry("the cat", "C%d" % index) for index in range(4)]
        self.assertEqual([], self._duplicate_findings(entries[:2], source))
        for count in (3, 4):
            with self.subTest(count=count):
                finding = self._duplicate_findings(entries[:count], source)[0]
                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(2, finding["details"]["source_occurrences"])
                self.assertEqual(count, finding["details"]["all_text_occurrences"])
                self.assertEqual(0, finding["details"]["unemitted_source_split_support"])

        # Four single-fused-copy shapes (1 source copy, 1 spaced emission clean,
        # 2 spaced emissions must block).
        single_cases = (
            ("word_joiner", "Go\u2060home", "Go home"),
            ("persian_plain", "میروم", "می روم"),
            ("persian_zwnj", "می\u200cروم", "می روم"),
            ("japanese", "はいそうです", "はい そうです"),
            ("thai", "สวัสดีครับ", "สวัสดี ครับ"),
        )
        for label, src, spaced in single_cases:
            with self.subTest(case=label):
                self.assertEqual([], self._duplicate_findings(
                    [_entry(spaced, "MARTA")], src))
                finding = self._duplicate_findings(
                    [_entry(spaced, "MARTA"), _entry(spaced, "PORTER")], src)[0]
                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(1, finding["details"]["source_occurrences"])
                self.assertEqual(2, finding["details"]["all_text_occurrences"])
                self.assertEqual(0, finding["details"]["unemitted_source_split_support"])

        # A multi-token span already counted by the boundary counter must not
        # be credited a second time through a straddle window either.
        straddle_finding = self._duplicate_findings(
            [_entry("a b c", "MARTA"), _entry("a b c", "PORTER")], "a bc")[0]
        self.assertEqual("blocking", straddle_finding["severity"])
        self.assertEqual(1, straddle_finding["details"]["source_occurrences"])
        self.assertEqual(0, straddle_finding["details"]["unemitted_source_split_support"])

    def test_a_soft_wrapped_copy_shares_capacity_with_a_fused_copy(self):
        # The project's paragraph definition decides fusion (#2293): a single
        # line ending is a soft wrap, so a wrapped source copy is the same line
        # as a fused one and the two copies share capacity two. A third emitted
        # copy blocks. The old r14 rule refused the wrap and reported the
        # faithful two-copy book.
        ja, ja_wrapped = "\u306f\u3044\u305d\u3046\u3067\u3059", "\u306f\u3044\u305d\u3046\n\u3067\u3059"
        th, th_wrapped = ("\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35\u0e04\u0e23\u0e31\u0e1a",
                          "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35\n\u0e04\u0e23\u0e31\u0e1a")
        for label, line, wrapped in (("ja", ja, ja_wrapped), ("th", th, th_wrapped)):
            with self.subTest(case=label):
                source = ("「%s」と彼は言った。「%s」と彼女は言った。" % (line, wrapped)
                          if label == "ja" else
                          "%s เธอพูด %s เธอพูด" % (line, wrapped))
                faithful = [_entry(line, "C%d" % index) for index in range(2)]
                self.assertEqual([], self._duplicate_findings(faithful, source))
                finding = self._duplicate_findings(
                    faithful + [_entry(line, "C2")], source)[0]
                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(2, finding["details"]["source_occurrences"])
                self.assertEqual(3, finding["details"]["all_text_occurrences"])

    def test_a_soft_wrap_gain_beside_a_heading_is_a_known_limit(self):
        # Known limit (#2293): a soft wrap makes the words on either side
        # candidate one fused word, so the real fused copy "Gohome" and the
        # heading-to-sentence "Go" + "Home" share capacity two. Pinned: two
        # emitted copies stay clean, a third blocks. The alternative - refusing
        # wrap fusion - false-blocks every faithfully wrapped book.
        source = '"Gohome," she said.\nChapter Two: Go\nHome was far away.'
        twice = [_entry("Gohome.", "C0"), _entry("Gohome.", "C1")]
        self.assertEqual([], self._duplicate_findings(twice, source))
        finding = self._duplicate_findings(
            twice + [_entry("Gohome.", "C2")], source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(2, finding["details"]["source_occurrences"])
        self.assertEqual(3, finding["details"]["all_text_occurrences"])

    def test_a_wrapped_copy_between_ordinary_characters_is_never_silently_dropped(self):
        # The capacity check skips a needle with zero capacity, but a wrapped
        # source copy between ordinary characters is counted by the
        # spelling-independent counter, so that skip is unreachable for it: one
        # wrapped copy blocks a second emission instead of being dropped. A wrap
        # immediately before a combining mark is the known limit pinned below.
        source = "\u300c\u306f\u3044\u305d\u3046\n\u3067\u3059\u300d\u3068\u5f7c\u306f\u8a00\u3063\u305f\u3002"
        needle = "\u306f\u3044\u305d\u3046\u3067\u3059"
        self.assertEqual(1, get_boundary_insensitive_counter(source)(needle))

        finding = self._duplicate_findings(
            [_entry(needle, "C0"), _entry(needle, "C1")], source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])
        self.assertEqual(2, finding["details"]["all_text_occurrences"])

        # The same one wrapped copy spelled as two separate words.
        spaced = "\u306f\u3044\u305d\u3046 \u3067\u3059"
        finding = self._duplicate_findings(
            [_entry(spaced, "C0"), _entry(spaced, "C1")], source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])

    def test_a_soft_wrap_before_a_combining_mark_is_a_known_limit(self):
        # Known limit (#2293): when a soft wrap falls immediately before a
        # combining mark, the mark can no longer attach to the word it follows,
        # so neither source spelling fuses and the wrapped copy is not counted.
        # The same source without the wrap blocks the second emission, which
        # pins that this is the wrap shape and not the line. Kept for a
        # follow-up: the fix has to reattach a mark across a line ending, and
        # the six-language sweep is clean without it.
        cases = (
            ('"สวัสดีครับ" เขาพูด', '"สวัสด\nีครับ" เขาพูด', 'สวัสดีครับ'),
            ('"नमस्ते" वे बोले', '"नमस\n्ते" वे बोले', 'नमस्ते'),
        )
        for unwrapped, wrapped, line in cases:
            with self.subTest(line=line):
                self.assertEqual(
                    1, get_boundary_insensitive_counter(unwrapped)(line))
                self.assertEqual(
                    0, get_boundary_insensitive_counter(wrapped)(line))
                finding = self._duplicate_findings(
                    [_entry(line, "C0"), _entry(line, "C1")], unwrapped)[0]
                self.assertEqual("blocking", finding["severity"])
                # The known miss: the wrapped copy is invisible, so the second
                # emission is clean.
                self.assertEqual([], self._duplicate_findings(
                    [_entry(line, "C0"), _entry(line, "C1")], wrapped))

    def test_a_hyphen_at_a_line_break_is_a_known_limit(self):
        # Known limit (#2293): the model removed a hyphen the source kept at a
        # hard wrap. "go-" and "home" stay two tokens, so the fused spelling is
        # not a source copy; the spaced spelling is one source line and two of
        # it block. Kept because fusing "go-" + "home" would also fuse every
        # hyphenated compound at a wrap, which is not a duplicate question.
        source = '"Let\'s go-\nhome," she said.'
        fused = [_entry("Let's gohome.", "C0"), _entry("Let's gohome.", "C1")]
        self.assertEqual([], self._duplicate_findings(fused, source))
        spaced = [_entry("Let's go home.", "C0"), _entry("Let's go home.", "C1")]
        finding = self._duplicate_findings(spaced, source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])
        self.assertEqual(2, finding["details"]["all_text_occurrences"])

    def test_a_blank_line_holding_only_an_invisible_character_is_a_break(self):
        # The paragraph decision used to run on the comparison key: a ZWNJ-only
        # blank line stayed a soft wrap (ZWNJ is kept lexical) while an LRM-only
        # one became a paragraph break (LRM is deleted). Both lines are blank,
        # so both are breaks. Neither shape may block a faithful single copy,
        # and two fused emissions are no copy of any source line.
        for character, name in (("\u200c", "ZWNJ"), ("\u200e", "LRM")):
            with self.subTest(character=name):
                source = "Z z go\n" + character + "\nhome z z rest"
                self.assertEqual([], self._duplicate_findings(
                    [_entry("Gohome.", "C0"), _entry("Gohome.", "C1")], source))
                self.assertEqual([], self._duplicate_findings(
                    [_entry("Gohome.", "C0")], source))
                # The primary key still sees one spaced line, so two spaced
                # emissions block.
                finding = self._duplicate_findings(
                    [_entry("Go home.", "C0"), _entry("Go home.", "C1")],
                    source)[0]
                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(1, finding["details"]["source_occurrences"])

    def test_a_source_with_a_wrapped_copy_and_a_spaced_emission_still_blocks(self):
        # One wrapped source copy, two fused emissions and one spaced emission:
        # all three are the same line, and only one is in the book.
        source = "\u300c\u306f\u3044\u305d\u3046\n\u3067\u3059\u300d\u3068\u5f7c\u306f\u8a00\u3063\u305f\u3002"
        fused = "\u306f\u3044\u305d\u3046\u3067\u3059"
        spaced = "\u306f\u3044\u305d\u3046 \u3067\u3059"
        entries = [_entry(fused, "C0"), _entry(fused, "C1"), _entry(spaced, "C2")]

        findings = self._duplicate_findings(entries, source)
        self.assertTrue(any(finding["severity"] == "blocking" for finding in findings))

    def test_every_single_line_ending_is_a_soft_wrap(self):
        # \n, \r\n, \r and a lone U+2028 are one line ending to the project's
        # paragraph definition: the wrapped words may fuse, so the fused needle
        # sees the copy.
        for separator in ("\n", "\r\n", "\r", "\u2028"):
            with self.subTest(separator=repr(separator)):
                source = "Z z go" + separator + "home z z rest"
                finding = self._duplicate_findings(
                    [_entry("Gohome.", "C0"), _entry("Gohome.", "C1")], source)[0]
                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(1, finding["details"]["source_occurrences"])
                self.assertEqual(2, finding["details"]["all_text_occurrences"])

    def test_a_paragraph_break_never_fuses(self):
        # A blank line and U+2029 end a paragraph: the words on either side are
        # separate lines, so no fused copy exists and the fused needle is not
        # counted. This is the r13 behaviour the decision keeps.
        for separator in ("\n\n", "\r\r", "\r\n\r\n", "\n \n", "\u2029"):
            with self.subTest(separator=repr(separator)):
                source = "Z z go" + separator + "home z z rest"
                entries = [_entry("Gohome.", "C0"), _entry("Gohome.", "C1")]
                self.assertEqual([], self._duplicate_findings(entries, source))

                # The spaced spelling is still the source's one line to the
                # primary key, so a second spaced copy blocks.
                spaced = [_entry("Go home.", "C0"), _entry("Go home.", "C1")]
                finding = self._duplicate_findings(spaced, source)[0]
                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(1, finding["details"]["source_occurrences"])

        # The B1 Japanese wrapped shape with a break instead of the soft wrap.
        line = "\u306f\u3044\u305d\u3046\u3067\u3059"
        spaced_line = "\u306f\u3044\u305d\u3046 \u3067\u3059"
        for separator in ("\n\n", "\u2029"):
            with self.subTest(language="ja", separator=repr(separator)):
                source = ("\u300c\u306f\u3044\u305d\u3046%s\u3067\u3059\u300d"
                          "\u3068\u5f7c\u306f\u8a00\u3063\u305f\u3002" % separator)
                self.assertEqual([], self._duplicate_findings(
                    [_entry(line, "C0"), _entry(line, "C1")], source))
                finding = self._duplicate_findings(
                    [_entry(spaced_line, "C0"), _entry(spaced_line, "C1")], source)[0]
                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(1, finding["details"]["source_occurrences"])

    def test_other_unicode_whitespace_follows_the_paragraph_definition(self):
        # L2: \x0b, \x0c, \x1c-\x1e and \x85 are whitespace but not a line
        # ending to iter_source_paragraph_ranges. Alone they behave like a lone
        # \n (the words fuse); inside a blank-line run they behave like the
        # blank line (the words do not fuse). A form feed is not special.
        for character in ("\x0b", "\x0c", "\x1c", "\x1d", "\x1e", "\x85"):
            with self.subTest(character=repr(character)):
                soft = "Z z go" + character + "home z z rest"
                finding = self._duplicate_findings(
                    [_entry("Gohome.", "C0"), _entry("Gohome.", "C1")], soft)[0]
                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(1, finding["details"]["source_occurrences"])

                blank = "Z z go\n" + character + "\nhome z z rest"
                self.assertEqual([], self._duplicate_findings(
                    [_entry("Gohome.", "C0"), _entry("Gohome.", "C1")], blank))

    def test_soft_wrapped_source_sweep_stays_faithful_and_catches_injection(self):
        # 72 deterministic wrapped-source cases (ja/zh/th; wrap inside a word
        # and at its word gap; every single line ending; one, two and three
        # source copies): every faithful script is clean and every injected
        # extra copy blocks.
        families = (
            ("ja", "\u6771\u4eac\u30bf\u30ef\u30fc",
             "\u6771\u4eac\u30bf\n\u30ef\u30fc", "\u6771\u4eac\n\u30bf\u30ef\u30fc",
             "\u591c\u306e\u9593\u306b\u96e8\u304c\u964d\u308a\u7d9a\u304d\u9053\u306f\u9759\u304b\u3060\u3063\u305f\u3002"),
            ("zh", "\u5317\u4eac\u996d\u5e97",
             "\u5317\u4eac\u996d\n\u5e97", "\u5317\u4eac\n\u996d\u5e97",
             "\u96e8\u4e0b\u4e86\u4e00\u6574\u591c\uff0c\u8def\u4e0a\u5f88\u5b89\u9759\u3002"),
            ("th", "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35\u0e04\u0e23\u0e31\u0e1a",
             "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35\u0e04\n\u0e23\u0e31\u0e1a",
             "\u0e2a\u0e27\u0e31\u0e2a\u0e14\u0e35\n\u0e04\u0e23\u0e31\u0e1a",
             "\u0e1d\u0e19\u0e15\u0e01\u0e17\u0e31\u0e49\u0e07\u0e04\u0e37\u0e19\u0e16\u0e19\u0e19\u0e40\u0e07\u0e35\u0e22\u0e1a\u0e07\u0e31\u0e1a"),
        )
        for language, line, mid_word, at_gap, filler in families:
            for wrap, wrapped in (("mid_word", mid_word), ("at_gap", at_gap)):
                for separator in ("\n", "\r\n", "\r", "\u2028"):
                    shape = wrapped.replace("\n", separator)
                    copy = filler + "\u300c" + shape + "\u300d\u3068\u5f7c\u306f\u8a00\u3063\u305f\u3002"
                    for copies in (1, 2, 3):
                        with self.subTest(language=language, wrap=wrap,
                                          separator=repr(separator), copies=copies):
                            source = copy * copies + filler
                            faithful = [_entry(line, "C%d" % index)
                                        for index in range(copies)]
                            self.assertEqual([], self._duplicate_findings(faithful, source))
                            finding = self._duplicate_findings(
                                faithful + [_entry(line, "CX")], source)[0]
                            self.assertEqual("blocking", finding["severity"])
                            self.assertEqual(copies, finding["details"]["source_occurrences"])
                            self.assertEqual(copies + 1,
                                             finding["details"]["all_text_occurrences"])

    def test_hard_wrapped_source_stays_clean_once_and_blocks_at_two(self):
        # The primary comparison key still collapses a single newline to a word
        # separator for spaced output, so a hard-wrapped line stays faithful at
        # one copy and blocks at two.
        source = '"Go\nhome," she said.'
        once = [_entry("Go home.", "MARTA"), _entry("she said.")]
        self.assertEqual([], self._duplicate_findings(once, source))
        finding = self._duplicate_findings(
            once + [_entry("Go home.", "PORTER")], source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])
        self.assertEqual(2, finding["details"]["all_text_occurrences"])

    def test_an_unrelated_zwsp_does_not_change_a_fused_capacity(self):
        # With the spelling-independent count built on demand for every source,
        # a U+200B in an unrelated entry no longer flips a fused duplicate from
        # missed to blocking: the spaced source is one copy of "Gohome" either
        # way.
        source = '"Go home," she said.'
        plain = [_entry("Gohome.", "MARTA"), _entry("Gohome.", "PORTER")]
        unrelated = plain + [_entry("z\u200bz", "Narrator")]

        first = self._duplicate_findings(plain, source)[0]
        second = self._duplicate_findings(unrelated, source)[0]
        self.assertEqual("blocking", first["severity"])
        self.assertEqual(first["severity"], second["severity"])
        self.assertEqual(first["details"]["source_occurrences"],
                         second["details"]["source_occurrences"])
        self.assertEqual(first["details"]["all_text_occurrences"],
                         second["details"]["all_text_occurrences"])

    def test_audit_coverage_uses_the_comparison_key_without_a_zwsp(self):
        # A ZWJ source with no U+200B: the comparison key deletes ZWJ, so the
        # faithful fused entry covers it. The old view split "Go\\u200dhome"
        # into two words and reported low coverage for one faithful copy.
        source = '"Go\u200dhome," she said.'
        entries = [_entry("Gohome.", "MARTA"), _entry("she said.")]

        report = audit_script(entries, source)
        self.assertNotIn("low_source_word_coverage",
                         {finding["code"] for finding in report["findings"]})

    def test_audit_coverage_tokenizes_both_sides_the_same_way(self):
        # Coverage's fallback comparison view must tokenize both source and
        # script with the same mark-aware, ZWNJ-insensitive comparison key so
        # Thai combining marks, Persian ZWNJ toggles, and ASCII apostrophe
        # folds never trip low_source_word_coverage on a faithful script.
        faithful_cases = (
            ("th_zwsp", '"คน\u200bดี" เธอพูด',
             [_entry("คนดี", "MARTA"), _entry("เธอพูด")]),
            ("th_zwj", '"คน\u200dดี" เธอพูด',
             [_entry("คนดี", "MARTA"), _entry("เธอพูด")]),
            ("fa_zwnj_to_plain", "می\u200cروم", [_entry("میروم", "MARTA")]),
            ("fa_plain_to_zwnj", "میروم", [_entry("می\u200cروم", "MARTA")]),
            ("ascii_apostrophe_dropped", "Don't stop.",
             [_entry("Dont stop.", "MARTA")]),
            ("ascii_apostrophe_added", "Dont stop.",
             [_entry("Don't stop.", "MARTA")]),
        )
        for label, source, entries in faithful_cases:
            with self.subTest(case=label):
                report = audit_script(entries, source)
                self.assertNotIn("low_source_word_coverage",
                                 {finding["code"] for finding in report["findings"]})

        # Genuinely missing content still reports low_source_word_coverage.
        missing = audit_script([_entry("คนดี", "MARTA")],
                               "คน\u200bดี สวัสดีครับ วันนี้ฝนตกทั้งคืน")
        self.assertIn("low_source_word_coverage",
                      {finding["code"] for finding in missing["findings"]})

    def test_one_source_copy_in_two_spellings_is_a_known_miss(self):
        """Known miss (#2353): spellings are counted separately.

        The source carries "Go home" once. The model writes "Gohome" once and
        "Go home" once; neither needle's own count exceeds the shared capacity,
        although together they are two emissions of one line. Kept for this PR
        and pinned here so the follow-up (sum emissions across spelling
        variants against the shared capacity) has a regression test.
        """
        source = '"Go home," she said.'
        entries = [_entry("Gohome.", "MARTA"), _entry("Go home.", "PORTER"),
                   _entry("she said.")]

        self.assertEqual([], self._duplicate_findings(entries, source))

    def test_a_persian_line_in_three_spellings_is_a_known_miss_2353(self):
        """Known miss (#2353): capacity is shared, emissions are per spelling.

        The source carries two copies (ZWNJ and plain). The model emits one
        copy in each of the three spellings; each needle is emitted once, so no
        needle's own count exceeds the shared capacity although the line was
        emitted three times for two copies. Pinned for the follow-up (sum
        emissions across spelling variants against the shared capacity).
        """
        source = "می\u200cروم و میروم"
        entries = [_entry("می\u200cروم", "C0"), _entry("میروم", "C1"),
                   _entry("می روم", "C2")]

        self.assertEqual([], self._duplicate_findings(entries, source))

    def test_an_okay_rewrite_beside_ok_still_blocks(self):
        # NIT kept (already under Limits): "OK" and "Okay" are different words,
        # not typography. The source carries each once; two OK entries exceed
        # the one OK copy and block, because the model may not rewrite the
        # Okay copy (same class as cannot/can't and Saint/St.).
        source = '"OK," she said. "Okay," he said.'
        entries = [_entry("OK", "C0"), _entry("OK", "C1")]

        finding = self._duplicate_findings(entries, source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])
        self.assertEqual(2, finding["details"]["all_text_occurrences"])

    def test_the_zwsp_sweep_stays_clean_and_catches_each_injection(self):
        # Deterministic form of the ZWSP sweep: every language, both source
        # forms (boundary-bearing and spaced), and every model spelling are
        # checked without random sampling. Faithful once is clean; injected
        # twice in any spelling blocks.
        families = (
            ("en", "Go\u200bhome", "The rain fell all night and the road was quiet.",
             ("Go home", "Gohome", "Go\u200bhome")),
            ("th", "สวัสดี\u200bครับ", "ฝนตกทั้งคืนถนนเงียบงับ",
             ("สวัสดี ครับ", "สวัสดีครับ", "สวัสดี\u200bครับ")),
            ("ja", "東京\u200bタワー", "夜の間に雨が降り続き道は静かだった。",
             ("東京 タワー", "東京タワー", "東京\u200bタワー")),
            ("zh", "北\u200b京", "雨下了一整夜，路上很安静。",
             ("北 京", "北京", "北\u200b京")),
        )
        for language, line, filler, spellings in families:
            for source_line in (line, line.replace("\u200b", " ")):
                for spelling in spellings:
                    source = filler + ' "' + source_line + '," she said. ' + filler
                    with self.subTest(language=language, source=source_line,
                                      spelling=spelling):
                        self.assertEqual([], self._duplicate_findings(
                            [_entry(spelling + ".", "MARTA")], source))
                        finding = self._duplicate_findings(
                            [_entry(spelling + ".", "MARTA"),
                             _entry(spelling + ".", "PORTER")], source)[0]
                        self.assertEqual("blocking", finding["severity"])
                        self.assertEqual(1, finding["details"]["source_occurrences"])

    def test_a_zwnj_source_and_a_plain_output_share_one_capacity(self):
        # #2316 keeps ZWNJ lexical in the primary key: the ZWNJ spelling is a
        # word of its own. For capacity it is an optional separator, so the
        # plain spelling is the same line and a second copy blocks (#2293).
        plain, with_zwnj = "میروم", "می\u200cروم"
        source = "The rain fell. " + with_zwnj + " and the road was quiet."

        self.assertEqual([], self._duplicate_findings([_entry(plain, "MARTA")], source))
        finding = self._duplicate_findings(
            [_entry(plain, "MARTA"), _entry(plain, "PORTER")], source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])
        self.assertEqual(2, finding["details"]["all_text_occurrences"])

    def test_a_plain_source_and_a_zwnj_output_share_one_capacity(self):
        # The reverse spelling: the source is plain and the model writes the
        # ZWNJ form. One source copy, so one emitted copy is clean and two block.
        plain, with_zwnj = "میروم", "می\u200cروم"
        source = "The rain fell. " + plain + " and the road was quiet."

        self.assertEqual([], self._duplicate_findings([_entry(with_zwnj, "MARTA")], source))
        finding = self._duplicate_findings(
            [_entry(with_zwnj, "MARTA"), _entry(with_zwnj, "PORTER")], source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])
        self.assertEqual(2, finding["details"]["all_text_occurrences"])

    def test_each_zwnj_spelling_in_the_source_is_one_copy_of_the_same_line(self):
        # A source carrying both spellings holds two copies of one line. Plain,
        # ZWNJ, and spaced forms all get capacity two (without double-counting
        # the fused tokens in split support), so two emitted copies are
        # faithful and a third copy blocks in every spelling.
        plain, with_zwnj = "میروم", "می\u200cروم"
        source = plain + " " + with_zwnj
        for line in (plain, with_zwnj, "می روم"):
            with self.subTest(line=line):
                faithful = [_entry(line, "MARTA"), _entry(line, "PORTER")]
                self.assertEqual([], self._duplicate_findings(faithful, source))
                finding = self._duplicate_findings(
                    faithful + [_entry(line, "ANNA")], source)[0]
                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(2, finding["details"]["source_occurrences"])
                self.assertEqual(0, finding["details"]["unemitted_source_split_support"])

    def test_zwnj_after_space_or_zwsp_is_fusable(self):
        # A leading ZWNJ after a space or U+200B sits in the gap between the
        # two tokens rather than trailing the first token; it is still an
        # optional separator and must fuse. A line ending is a soft wrap too
        # (#2293), so it fuses; punctuation and a paragraph break still
        # separate lines.
        for label, source in (
                ("space_then_zwnj", "Hello. می \u200cروم world."),
                ("zwsp_then_zwnj", "Hello. می\u200b\u200cروم world."),
                ("newline_then_zwnj", "Hello. می\n\u200cروم world.")):
            with self.subTest(case=label):
                self.assertEqual([], self._duplicate_findings(
                    [_entry("میروم", "MARTA")], source))
                finding = self._duplicate_findings(
                    [_entry("میروم", "MARTA"), _entry("میروم", "PORTER")], source)[0]
                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(1, finding["details"]["source_occurrences"])
                self.assertEqual(2, finding["details"]["all_text_occurrences"])

        for label, source in (
                ("punctuation_then_zwnj", "Hello. می.\u200cروم world."),
                ("blank_line_then_zwnj", "Hello. می\n\n\u200cروم world."),
                ("paragraph_separator_then_zwnj", "Hello. می\u2029\u200cروم world.")):
            with self.subTest(negative=label):
                self.assertEqual([], self._duplicate_findings(
                    [_entry("میروم", "MARTA"), _entry("میروم", "PORTER")], source))

    def test_a_zero_width_joiner_is_deleted_for_devanagari_conjuncts(self):
        # ZWJ is a glyph-joining hint: "क्\u200dष" and "क्ष" are the same
        # conjunct, so a source carrying both spellings holds two copies.
        plain, with_zwj = "क्ष", "क्\u200dष"
        source = plain + " " + with_zwj
        faithful = [_entry(plain, "MARTA"), _entry(plain, "PORTER")]

        self.assertEqual([], self._duplicate_findings(faithful, source))
        finding = self._duplicate_findings(faithful + [_entry(plain, "ANNA")], source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(2, finding["details"]["source_occurrences"])

    def test_audit_coverage_uses_the_duplicate_key_for_a_joined_word(self):
        # A ZWJ is deleted by the comparison key, so "Go\u200dhome" is one word
        # for capacity. Coverage must use the same key: the old inline fused
        # view stripped only U+200B, split the ZWJ into a second token, and
        # reported low coverage for a faithful single copy.
        source = '"Go\u200dhome\u200b," she said.'
        entries = [_entry("Gohome.", "MARTA"), _entry("she said.")]

        report = audit_script(entries, source)
        self.assertNotIn("low_source_word_coverage",
                         {finding["code"] for finding in report["findings"]})

    def test_source_spans_stay_indexed_to_the_primary_key(self):
        # The fused source key wins capacity, but its offsets index a different
        # string. Reported spans must stay indexed to the primary key, so a
        # needle absent from the primary key has none.
        source = '"Go\u200bhome," she said.'
        entries = [_entry("Gohome.", "MARTA"), _entry("she said."),
                   _entry("Gohome.", "PORTER")]
        finding = self._duplicate_findings(entries, source)[0]

        self.assertEqual(1, finding["details"]["source_occurrences"])
        self.assertEqual([], finding["details"]["source_word_normalized_spans"])
        self.assertTrue(finding["details"]["source_spans_truncated"])

    def test_a_superscript_numeral_behaves_like_a_literal_one(self):
        # NFKC folds "x\u00b2" to "x2", so a superscript is a source numeral
        # exactly as a literal "x2" is. The digit rule therefore downgrades a
        # spelled-number repeat to manual_review instead of proving the extra
        # "Two" unrelated to it. Kept deliberately rather than narrowing the
        # rule to guess a numeral's written form: a literal "x2" already
        # behaves this way, and the two must not disagree. Emitting the numeral
        # removes the doubt and the repeat blocks.
        entries = [_entry("Two.", "MARTA"), _entry("x squared"),
                   _entry("Two.", "PORTER")]
        for numeral in ("x\u00b2", "x2"):
            with self.subTest(numeral=numeral):
                finding = self._duplicate_findings(
                    entries, '"Two." he said. "%s"' % numeral)[0]
                self.assertEqual("manual_review", finding["severity"])
                self.assertTrue(finding["details"]["rendering_capacity_unknown"])

        emitted = [_entry("Two.", "MARTA"), _entry("x\u00b2"),
                   _entry("Two.", "PORTER")]
        self.assertEqual("blocking", self._duplicate_findings(
            emitted, '"Two." he said. "x\u00b2"')[0]["severity"])

    def test_unsegmented_reduplication_downgrades_to_manual_review(self):
        # The fused source token is split into adjacent entries, which is what
        # the run rule credits; the old fixture was non-adjacent because the
        # whole-count rule ignored adjacency.
        source = (" ".join(_SEPARATE_DUPLICATE_FILLER)
                  + " \u300c\u306f\u3044\u300d\u3068\u5f7c\u306f\u8a00\u3063\u305f\u3002"
                  + " \u300c\u306f\u3044\u306f\u3044\u300d\u3068\u5f7c\u5973\u306f\u7b54\u3048\u305f\u3002")
        findings = self._duplicate_findings(self._adjacent_entries("\u306f\u3044"), source)

        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])
        self.assertFalse(findings[0]["details"]["rendering_capacity_unknown"])
        self.assertEqual(2, findings[0]["details"]["unemitted_source_split_support"])

    def test_unsegmented_phrase_inside_a_larger_entry_still_blocks(self):
        source = (" ".join(_SEPARATE_DUPLICATE_FILLER)
                  + " \u300c\u306f\u3044\u300d\u3068\u5f7c\u306f\u8a00\u3063\u305f\u3002"
                  + " \u306f\u3044\u306f\u3044")
        entries = ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER[:60]]
                   + [_entry("\u306f\u3044\u3001\u305d\u3046\u3067\u3059", "MARTA")]
                   + [_entry(word) for word in _SEPARATE_DUPLICATE_FILLER[60:]]
                   + [_entry("\u306f\u3044", "PORTER"), _entry("\u306f\u3044", "ANNA")])
        findings = self._duplicate_findings(entries, source)

        self.assertEqual(1, len(findings))
        self.assertEqual("blocking", findings[0]["severity"])

    def test_latin_phrase_is_not_counted_inside_longer_words(self):
        source = " ".join(_SEPARATE_DUPLICATE_FILLER) + ' "no" she said. I know.'
        findings = self._duplicate_findings(self._entries("no"), source)

        self.assertEqual(1, len(findings))
        self.assertEqual("blocking", findings[0]["severity"])

    def test_a_longer_word_that_is_not_an_exact_repetition_still_blocks(self):
        cases = (
            ("\u306f\u3044", "\u306f\u3044\u3051\u3044"),
            ("\u306f\u3044", "\u306f\u3044\u306f\u3044\u3051\u3044"),
            ("\u30cf\u30a4", "\u30cf\u30a4\u30ad\u30f3\u30b0"),
            ("\u6771\u4eac", "\u6771\u4eac\u90fd"),
            ("\u3042\u308a\u304c\u3068\u3046", "\u3042\u308a\u304c\u3068\u3046\u3054\u3056\u3044\u307e\u3059"),
            ("\ub124", "\ub124\uac00"),
        )
        for needle, word in cases:
            with self.subTest(needle=needle, word=word):
                source = (" ".join(_SEPARATE_DUPLICATE_FILLER)
                          + ' "%s" she asked. %s' % (needle, word))
                findings = self._duplicate_findings(self._entries(needle), source)

                self.assertEqual(1, len(findings))
                self.assertEqual("blocking", findings[0]["severity"])

    def test_halfwidth_glued_repetition_downgrades_to_manual_review(self):
        source = (" ".join(_SEPARATE_DUPLICATE_FILLER)
                  + " \u300c\uff8a\uff72\u300d\u3068\u8a00\u3063\u305f\u3002 \uff8a\uff72\uff8a\uff72")
        findings = self._duplicate_findings(self._adjacent_entries("\uff8a\uff72"), source)

        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])

    def test_latin_glued_repetition_downgrades_to_manual_review(self):
        source = " ".join(_SEPARATE_DUPLICATE_FILLER) + ' "ha" she said. haha'
        findings = self._duplicate_findings(self._adjacent_entries("ha"), source)

        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])

    def test_thai_joined_repetition_does_not_block_a_faithful_split(self):
        source = (" ".join(_SEPARATE_DUPLICATE_FILLER)
                  + " \u0e40\u0e02\u0e32\u0e1e\u0e39\u0e14\u0e43\u0e0a\u0e48"
                  + " \u0e43\u0e0a\u0e48\u0e43\u0e0a\u0e48")
        findings = self._duplicate_findings(self._entries("\u0e43\u0e0a\u0e48"), source)

        self.assertFalse([finding for finding in findings
                          if finding["severity"] == "blocking"])

    def test_thai_mark_shifted_run_alone_abstains(self):
        source = " ".join(_SEPARATE_DUPLICATE_FILLER) + " \u0e2e\u0e48\u0e32\u0e2e\u0e48\u0e32"
        findings = self._duplicate_findings(self._entries("\u0e2e\u0e48\u0e32"), source)

        self.assertEqual([], findings)

    def test_a_glued_repetition_covers_exactly_its_own_entries(self):
        source = (" ".join(_SEPARATE_DUPLICATE_FILLER)
                  + " \u300c\u306f\u3044\u300d\u3068\u8a00\u3063\u305f\u3002 \u306f\u3044\u306f\u3044")
        entries = ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER]
                   + [_entry("\u306f\u3044", speaker)
                      for speaker in ("MARTA", "PORTER", "ANNA")])
        findings = self._duplicate_findings(entries, source)

        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])

    def test_each_glued_repetition_covers_its_own_entries(self):
        source = (" ".join(_SEPARATE_DUPLICATE_FILLER)
                  + " \u300c\u306f\u3044\u300d\u3068\u8a00\u3063\u305f\u3002"
                  + " \u306f\u3044\u306f\u3044 \u306f\u3044\u306f\u3044")
        entries = ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER]
                   + [_entry("\u306f\u3044", "C%d" % k) for k in range(5)])
        findings = self._duplicate_findings(entries, source)

        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])

    def test_a_three_copy_run_covers_the_extra_entries(self):
        source = (" ".join(_SEPARATE_DUPLICATE_FILLER)
                  + " \u300c\u306f\u3044\u300d\u3068\u8a00\u3063\u305f\u3002"
                  + " \u306f\u3044\u306f\u3044\u306f\u3044")
        entries = ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER]
                   + [_entry("\u306f\u3044", "C%d" % k) for k in range(4)])
        findings = self._duplicate_findings(entries, source)

        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])

    def test_a_glued_repetition_and_an_extra_copy_still_block(self):
        source = (" ".join(_SEPARATE_DUPLICATE_FILLER)
                  + " \u300c\u306f\u3044\u300d\u3068\u8a00\u3063\u305f\u3002 \u306f\u3044\u306f\u3044")
        entries = ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER]
                   + [_entry("\u306f\u3044", "C%d" % k) for k in range(4)])
        findings = self._duplicate_findings(entries, source)

        self.assertEqual(1, len(findings))
        self.assertEqual("blocking", findings[0]["severity"])

    def test_a_decomposed_repetition_token_explains_the_split(self):
        source = (" ".join(_SEPARATE_DUPLICATE_FILLER)
                  + ' "\u304c" she asked. ' + "\u304b\u3099" * 2)
        findings = self._duplicate_findings(self._adjacent_entries("\u304c"), source)

        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])

    def test_a_known_expansion_and_a_glued_repetition_both_cover_entries(self):
        source = (" ".join(_SEPARATE_DUPLICATE_FILLER)
                  + ' "and" she asked. & andand')
        entries = ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER]
                   + [_entry("and", "C%d" % k) for k in range(4)])
        findings = self._duplicate_findings(entries, source)

        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])

    def test_a_fused_source_word_split_faithfully_is_manual_review(self):
        source = "\u5f7c\u306f\u306f\u3044\u3001\u306f\u3044\u3002"
        entries = [_entry("\u5f7c\u306f"), _entry("\u306f\u3044", "MARTA"),
                   _entry("\u306f\u3044", "PORTER"), _entry("\u3002")]
        findings = self._duplicate_findings(entries, source)

        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])
        self.assertFalse(findings[0]["details"]["rendering_capacity_unknown"])
        self.assertEqual(1, findings[0]["details"]["unemitted_source_split_support"])

    def test_a_fused_thai_source_word_split_faithfully_is_manual_review(self):
        source = "\u0e40\u0e02\u0e32\u0e1e\u0e39\u0e14\u0e27\u0e48\u0e32\u0e43\u0e0a\u0e48 \u0e43\u0e0a\u0e48"
        entries = [_entry("\u0e40\u0e02\u0e32\u0e1e\u0e39\u0e14\u0e27\u0e48\u0e32"),
                   _entry("\u0e43\u0e0a\u0e48", "MARTA"),
                   _entry("\u0e43\u0e0a\u0e48", "PORTER")]
        findings = self._duplicate_findings(entries, source)

        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])

    def test_a_fused_thai_word_with_a_dropped_remainder_still_blocks(self):
        source = "\u0e01\u0e32\u0e02\u0e32\u0e27 \u0e01\u0e32"
        entries = [_entry("\u0e01\u0e32", "MARTA"),
                   _entry("\u0e01\u0e32", "PORTER")]
        findings = self._duplicate_findings(entries, source)

        self.assertEqual(1, len(findings))
        self.assertEqual("blocking", findings[0]["severity"])

    def test_a_run_emitted_whole_does_not_excuse_an_extra_copy(self):
        source = "\u306f\u3044 \u306f\u3044\u306f\u3044"
        entries = [_entry("\u306f\u3044", "MARTA"), _entry("\u306f\u3044", "PORTER"),
                   _entry("\u306f\u3044\u306f\u3044", "ANNA")]
        findings = self._duplicate_findings(entries, source)

        self.assertEqual(1, len(findings))
        self.assertEqual("blocking", findings[0]["severity"])

    def test_a_latin_run_emitted_whole_does_not_excuse_an_extra_copy(self):
        source = "ha haha"
        entries = [_entry("ha", "MARTA"), _entry("ha", "PORTER"),
                   _entry("haha", "ANNA")]
        findings = self._duplicate_findings(entries, source)

        self.assertEqual(1, len(findings))
        self.assertEqual("blocking", findings[0]["severity"])

    def test_a_dropped_word_explains_a_split_only_when_its_remainder_was_emitted(self):
        source = "\u6771\u4eac \u6771\u4eac\u90fd"
        split = [_entry("\u6771\u4eac", "MARTA"), _entry("\u6771\u4eac", "PORTER"),
                 _entry("\u90fd", "ANNA")]
        dropped = [_entry("\u6771\u4eac", "MARTA"), _entry("\u6771\u4eac", "PORTER")]

        findings = self._duplicate_findings(split, source)
        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])
        self.assertEqual("blocking", self._duplicate_findings(dropped, source)[0]["severity"])

    def test_a_dropped_word_elsewhere_does_not_excuse_a_real_duplicate(self):
        source = (" ".join(_SEPARATE_DUPLICATE_FILLER)
                  + " he found nothing there and the thing was gone . No .")
        entries = ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER]
                   + [_entry("he found there and the thing was gone"),
                      _entry("No.", "MARTA"), _entry("No.", "PORTER")])
        finding = self._duplicate_findings(entries, source)[0]

        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(0, finding["details"]["unemitted_source_split_support"])

    def test_a_remainder_emitted_elsewhere_does_not_excuse_a_real_duplicate(self):
        source = (" ".join(_SEPARATE_DUPLICATE_FILLER)
                  + " he went somit home with mit the dog . So .")
        entries = ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER]
                   + [_entry("he went home with mit the dog"),
                      _entry("So.", "MARTA"), _entry("So.", "PORTER")])
        finding = self._duplicate_findings(entries, source)[0]

        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(0, finding["details"]["unemitted_source_split_support"])

    def test_a_remainder_inside_a_longer_output_word_does_not_count(self):
        source = " ".join(_SEPARATE_DUPLICATE_FILLER) + " nothing . No ."
        entries = ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER]
                   + [_entry("something"), _entry("No.", "MARTA"),
                      _entry("No.", "PORTER")])
        finding = self._duplicate_findings(entries, source)[0]

        self.assertEqual("blocking", finding["severity"])

    def test_a_fused_needle_run_still_completes_a_remainder_split(self):
        # The unspent "somit" is split as consecutive "so", "mit"; "mitmit" is
        # emitted whole. The old fixture placed the split beside the fused run,
        # which only the key*k expansion could credit.
        source = "mit somit mitmit"
        entries = [_entry("mit", "MARTA"), _entry("so"), _entry("mit", "PORTER"),
                   _entry("mitmit")]
        finding = self._duplicate_findings(entries, source)[0]

        self.assertEqual("manual_review", finding["severity"])
        self.assertEqual(1, finding["details"]["unemitted_source_split_support"])

    def test_a_fused_word_split_at_a_different_offset_is_manual_review(self):
        source = ('Am Bahnhof wartete Marta . "ja ja" sagte er . Achjajaso .')
        entries = [_entry("Am Bahnhof wartete Marta ."), _entry("ja ja", "MARTA"),
                   _entry("Ach"), _entry("ja ja", "PORTER"), _entry("so")]
        finding = self._duplicate_findings(entries, source)[0]

        self.assertEqual("manual_review", finding["severity"])
        self.assertEqual(1, finding["details"]["unemitted_source_split_support"])

        extra = entries + [_entry("ja ja", "ANNA")]
        self.assertEqual("blocking", self._duplicate_findings(extra, source)[0]["severity"])

    def test_a_run_may_cross_an_entry_boundary(self):
        source = "a ab"
        entries = [_entry("a", "MARTA"), _entry("a", "PORTER"), _entry("b", "ANNA")]
        finding = self._duplicate_findings(entries, source)[0]

        self.assertEqual("manual_review", finding["severity"])
        self.assertEqual(1, finding["details"]["unemitted_source_split_support"])

    def test_a_scattered_remainder_does_not_excuse_a_duplicate(self):
        source = ('Am Bahnhof wartete Marta . "ja ja" sagte er . Achjajaso .')
        entries = [_entry("Ach"), _entry("Am Bahnhof wartete Marta ."),
                   _entry("ja ja", "MARTA"), _entry("so"), _entry("ja ja", "PORTER")]
        finding = self._duplicate_findings(entries, source)[0]

        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(0, finding["details"]["unemitted_source_split_support"])

    def test_two_fused_words_split_in_either_source_order_are_both_credited(self):
        for source in ("damit somit mit", "mit somit damit"):
            with self.subTest(source=source):
                entries = [_entry("da"), _entry("mit", "MARTA"),
                           _entry("so"), _entry("mit", "PORTER")]
                finding = self._duplicate_findings(entries, source)[0]

                self.assertEqual("manual_review", finding["severity"])
                self.assertEqual(2, finding["details"]["unemitted_source_split_support"])

    def test_a_fused_only_source_with_no_whole_copy_reports_nothing(self):
        # capacity is 0, and reporting the excess would need every sub-word
        # needle to be scored, which adds review noise on faithful scripts.
        source = "jaja"
        entries = [_entry("ja", "C%d" % k) for k in range(5)]

        self.assertEqual([], self._duplicate_findings(entries, source))

    def test_a_run_must_end_on_a_word_boundary(self):
        # "ab" is unspent, but the second "a" is followed by "bcd", so the run
        # would stop inside that word; it must not be credited.
        source = "a ab"
        entries = [_entry("a", "MARTA"), _entry("a", "PORTER"), _entry("bcd", "ANNA")]
        finding = self._duplicate_findings(entries, source)[0]

        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(0, finding["details"]["unemitted_source_split_support"])

    def test_competing_runs_are_chosen_to_avoid_a_false_block(self):
        # "zq" covers the first "q" only, while "qq" can cover a later pair.
        # Taking "qq" first exhausts it and leaves the last copy unexplained;
        # preferring the run that starts earlier finds all three.
        source = "q zq qq"
        entries = [_entry("z"), _entry("q", "C1"), _entry("q", "C2"),
                   _entry("q", "C3"), _entry("q", "C4")]
        finding = self._duplicate_findings(entries, source)[0]

        self.assertEqual("manual_review", finding["severity"])
        self.assertEqual(3, finding["details"]["unemitted_source_split_support"])

    def test_two_unspent_splits_do_not_credit_the_same_output_words(self):
        source = " ".join(_SEPARATE_DUPLICATE_FILLER) + " so sox xso"
        entries = ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER]
                   + [_entry("so", "MARTA"), _entry("x"), _entry("so", "PORTER"),
                      _entry("so", "ANNA")])
        finding = self._duplicate_findings(entries, source)[0]

        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["unemitted_source_split_support"])

    def test_a_long_unspent_token_does_not_repeat_work_per_candidate_copy(self):
        source = "a " + "b" * 300000
        entries = [_entry("a", "MARTA"), _entry("a", "PORTER")]
        lines = []

        def trace(frame, event, arg):
            if event == "line" and frame.f_code.co_filename.endswith("script_preflight.py"):
                lines.append(frame.f_lineno)
            return trace

        previous = sys.gettrace()
        sys.settrace(trace)
        try:
            findings = self._duplicate_findings(entries, source)
        finally:
            sys.settrace(previous)

        self.assertEqual(1, len(findings))
        self.assertEqual("blocking", findings[0]["severity"])
        self.assertLess(len(lines), 50000)

    def test_a_multi_word_needle_fused_in_the_source_is_not_blocked(self):
        ja_fill = ("\u671d\u306f\u5bd2\u304f\u3066\u30db\u30fc\u30e0\u306f\u7a7a\u3063\u307d\u3060\u3063\u305f\u3002"
                   "\u30de\u30eb\u30bf\u306f\u53e4\u3044\u6642\u8a08\u306e\u305d\u3070\u3067\u5f85\u3063\u3066\u3044\u305f\u3002"
                   "\u8a70\u3081\u6240\u306f\u30e9\u30f3\u30d7\u3092\u6301\u3063\u3066\u8fd1\u3065\u3044\u3066\u304d\u305f\u3002")
        cases = (
            (ja_fill + "\u300c\u306f\u3044\u3001\u305d\u3046\u3067\u3059\u300d\u3068\u5f7c\u306f\u8a00\u3063\u305f\u3002"
             "\u300c\u306f\u3044\u305d\u3046\u3067\u3059\u300d\u3068\u5f7c\u5973\u306f\u8a00\u3063\u305f\u3002",
             [_entry(ja_fill), _entry("\u306f\u3044\u3001\u305d\u3046\u3067\u3059", "MARTA"),
              _entry("\u3068\u5f7c\u306f\u8a00\u3063\u305f\u3002"),
              _entry("\u306f\u3044\u3001\u305d\u3046\u3067\u3059", "PORTER"),
              _entry("\u3068\u5f7c\u5973\u306f\u8a00\u3063\u305f\u3002")],
             "\u306f\u3044\u3001\u305d\u3046\u3067\u3059"),
        )
        for source, entries, line in cases:
            with self.subTest(line=line):
                report = audit_script(entries, source)
                duplicate = next(finding for finding in report["findings"]
                                 if finding["code"] == "contained_source_duplicate")
                self.assertEqual("manual_review", duplicate["severity"])
                self.assertEqual(1, duplicate["details"]["unemitted_source_split_support"])
                self.assertEqual(0, report["counts"]["blocking"])
                self.assertTrue(generate_script.passes_final_generation_gate(
                    validate_chunk_quality(source, entries), report))

                extra = entries + [_entry(line, "ANNA")]
                blocked = audit_script(extra, source)
                self.assertEqual(1, blocked["counts"]["blocking"])
                self.assertFalse(generate_script.passes_final_generation_gate(
                    validate_chunk_quality(source, extra), blocked))

    def test_a_line_spelled_fused_and_spaced_in_the_source_carries_two_copies(self):
        # "ja ja" in a quotation and "jaja" as one word are two source copies of
        # one line, so two emitted copies are faithful and pass the final gate.
        # A third copy exceeds capacity and must block without crediting "jaja"
        # a second time in unspent split support.
        de_fill = ("Der Morgen war kalt und der Bahnsteig leer . Marta wartete neben der "
                   "alten Uhr und beobachtete die Gleise . Der Traeger ging langsam und "
                   "hielt eine Lampe . ")
        source = de_fill + '\u201eja ja\u201c sagte er . Und dann sagte sie nur: jaja .'
        faithful = [_entry(de_fill), _entry("ja ja", "MARTA"),
                    _entry("sagte er . Und dann sagte sie nur:"), _entry("ja ja", "PORTER")]

        self.assertEqual([], self._duplicate_findings(faithful, source))
        self.assertTrue(generate_script.passes_final_generation_gate(
            validate_chunk_quality(source, faithful),
            audit_script(faithful, source)))
        extra = faithful + [_entry("ja ja", "ANNA")]
        finding = self._duplicate_findings(extra, source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(2, finding["details"]["source_occurrences"])
        self.assertEqual(3, finding["details"]["all_text_occurrences"])
        self.assertEqual(0, finding["details"]["unemitted_source_split_support"])
        self.assertFalse(generate_script.passes_final_generation_gate(
            validate_chunk_quality(source, extra),
            audit_script(extra, source)))

    def test_a_multi_word_line_straddling_two_fused_tokens_is_not_blocked(self):
        # The second "はい はい" has one word at the tail of そうはい and one at
        # the head of はいですと答えた. Neither fused token contains the whole
        # line, but the consecutive source tokens do.
        source = ("\u5f7c\u306f\u300c\u306f\u3044\u300d\u300c\u306f\u3044\u300d\u3068\u8a00\u3063\u305f\u3002"
                  "\u5f7c\u5973\u306f\u305d\u3046\u306f\u3044\u3001\u306f\u3044\u3067\u3059\u3068\u7b54\u3048\u305f\u3002")
        entries = ([_entry("\u5f7c\u306f"), _entry("\u306f\u3044 \u306f\u3044", "C1"),
                    _entry("\u3068\u8a00\u3063\u305f"), _entry("\u5f7c\u5973\u306f"),
                    _entry("\u305d\u3046"), _entry("\u306f\u3044 \u306f\u3044", "C2"),
                    _entry("\u3067\u3059\u3068\u7b54\u3048\u305f")])

        finding = self._duplicate_findings(entries, source)[0]

        self.assertEqual("manual_review", finding["severity"])
        self.assertEqual(1, finding["details"]["unemitted_source_split_support"])
        self.assertEqual(0, audit_script(entries, source)["counts"]["blocking"])
        # A third copy has no unemitted token left to explain it.
        extra = entries + [_entry("\u306f\u3044 \u306f\u3044", "C3")]
        self.assertEqual("blocking",
                         self._duplicate_findings(extra, source)[0]["severity"])

    def test_a_straddle_may_cover_a_whole_word_and_a_fused_token(self):
        # "a b" occurs once as whole tokens; "bc" and "abc" each render as
        # adjacent words, so the other two copies straddle a word boundary.
        source = "a b x a bc y abc z"
        entries = ([_entry("a b", "C1"), _entry("x"), _entry("a b", "C2"),
                    _entry("c"), _entry("y"), _entry("a b", "C3"),
                    _entry("c"), _entry("z")])

        finding = self._duplicate_findings(entries, source)[0]

        self.assertEqual("manual_review", finding["severity"])
        self.assertEqual(2, finding["details"]["unemitted_source_split_support"])
        extra = entries + [_entry("a b", "C4")]
        self.assertEqual("blocking", self._duplicate_findings(extra, source)[0]["severity"])

    def test_one_fused_token_can_explain_a_copy_on_each_side(self):
        # それいいえ splits to それ いいえ; the line "いいえ それ" straddles it
        # on the left (one word before) and on the right (one word after).
        source = "\u3044\u3044\u3048 \u305d\u308c \u306f\u3044 \u3044\u3044\u3048 \u305d\u308c\u3044\u3044\u3048 \u305d\u308c"
        entries = ([_entry("\u3044\u3044\u3048 \u305d\u308c", "C0"), _entry("\u306f\u3044"),
                    _entry("\u3044\u3044\u3048 \u305d\u308c", "C3"),
                    _entry("\u3044\u3044\u3048 \u305d\u308c", "C5")])

        finding = self._duplicate_findings(entries, source)[0]

        self.assertEqual("manual_review", finding["severity"])
        self.assertEqual(2, finding["details"]["unemitted_source_split_support"])

    def test_a_fused_token_can_explain_several_copies_beyond_the_first(self):
        # The source carries four copies of "정말 아니요": one spaced, one exact
        # fused ("정말아니요", counted into capacity = 2), one prefixed fused
        # ("오늘정말아니요") and one straddled ("정말" + "아니요오늘"). Four
        # emissions stay unblocked (capacity 2 + support 2), and a fifth blocks
        # because the exact fused copy is not credited twice.
        source = ("\ub0b4\uc77c \ub0b4\uc77c \uc815\ub9d0 \uc544\ub2c8\uc694 "
                  "\uc815\ub9d0\uc544\ub2c8\uc694 \uc624\ub298\uc815\ub9d0\uc544\ub2c8\uc694 "
                  "\uc815\ub9d0 \uc544\ub2c8\uc694\uc624\ub298 \uc624\ub298")
        entries = ([_entry("\ub0b4\uc77c"), _entry("\ub0b4\uc77c"),
                    _entry("\uc815\ub9d0 \uc544\ub2c8\uc694", "C2"),
                    _entry("\uc815\ub9d0 \uc544\ub2c8\uc694", "C4"),
                    _entry("\uc624\ub298"),
                    _entry("\uc815\ub9d0 \uc544\ub2c8\uc694", "C7"),
                    _entry("\uc815\ub9d0 \uc544\ub2c8\uc694", "C9"),
                    _entry("\uc624\ub298"), _entry("\uc624\ub298")])

        finding = self._duplicate_findings(entries, source)[0]

        self.assertEqual("manual_review", finding["severity"])
        self.assertEqual(2, finding["details"]["source_occurrences"])
        self.assertEqual(2, finding["details"]["unemitted_source_split_support"])
        self.assertEqual(0, audit_script(entries, source)["counts"]["blocking"])
        extra = entries + [_entry("\uc815\ub9d0 \uc544\ub2c8\uc694", "C10")]
        self.assertEqual("blocking",
                         self._duplicate_findings(extra, source)[0]["severity"])

    def test_the_above_threshold_fallback_keeps_a_faithful_split_unblocked(self):
        # Both fixtures yield more window/run candidates (23 and 47) than the
        # exhaustive threshold, so the DP plus its greedy and retry fallbacks
        # decide. Their exact optima are 4 and 8. Returning the unconstrained
        # DP answer over-credits the first (8, hiding the duplicate); dropping
        # the coverage-descending greedy undershoots (3, blocking a faithful
        # split); dropping the DP retry undershoots the second (7).
        cases = (
            ("babaab b a bab aba",
             "a b a b a ba ab ab ab a b a b a b a b a b a b b a b", "b a", 4),
            ("aa aa aa aaaa aaaa a aaa a aaa aaaa",
             "a a a a y a a a a a a a a a a a a", "a a", 8),
        )
        for source, output, phrase, support in cases:
            with self.subTest(phrase=phrase):
                words = output.split()
                entries = [_entry(" ".join(words[index:index + 2]), "C%d" % index)
                           for index in range(0, len(words), 2)]
                findings = [finding for finding in self._duplicate_findings(entries, source)
                            if finding["details"]["phrase"] == phrase]

                self.assertEqual(1, len(findings))
                self.assertEqual("manual_review", findings[0]["severity"])
                self.assertEqual(support,
                                 findings[0]["details"]["unemitted_source_split_support"])
                self.assertEqual(0, audit_script(entries, source)["counts"]["blocking"])

    def test_a_left_expansion_must_match_its_source_token(self):
        # The word before the fused run has the source token's length but not
        # its characters, so it cannot complete a faithful window.
        source = "\u6211\u7684\u5417 \u662f\u5417\u662f \u5417 \u662f \u4e0d"
        entries = ([_entry("\u6211"), _entry("\u7684"),
                    _entry("\u5417 \u662f", "C2"), _entry("\u5417 \u662f", "C4"),
                    _entry("\u5417 \u662f", "C6"), _entry("\u4e0d")])

        finding = self._duplicate_findings(entries, source)[0]

        self.assertEqual("manual_review", finding["severity"])
        self.assertEqual(2, finding["details"]["unemitted_source_split_support"])

    def test_a_long_unspaced_token_is_not_walked_per_output_word(self):
        # Every "a" word can begin the run, so this shape used to cost one
        # prefix walk per output word. Quadrupling the token must not cost
        # anything like the square of that.
        def trace_lines(size):
            source = "b " + "a" * size
            entries = [_entry("b", "C1"), _entry("b", "C2"),
                       _entry(" ".join(["a"] * size))]
            seen = []

            def trace(frame, event, arg):
                if event == "line" and frame.f_code.co_filename.endswith("script_preflight.py"):
                    seen.append(frame.f_lineno)
                return trace

            previous = sys.gettrace()
            sys.settrace(trace)
            try:
                self._duplicate_findings(entries, source)
            finally:
                sys.settrace(previous)
            return len(seen)

        small, large = trace_lines(2000), trace_lines(8000)
        self.assertLess(large, small * 6)

    def test_an_unadjacent_split_is_not_credited_in_either_source_order(self):
        for source in ("somit damit mit", "damit somit mit"):
            with self.subTest(source=source):
                entries = [_entry("da so"), _entry("mit", "MARTA"),
                           _entry("mit", "PORTER"), _entry("mit", "ANNA")]
                finding = self._duplicate_findings(entries, source)[0]

                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(1, finding["details"]["unemitted_source_split_support"])

    def test_adjacent_repeated_splits_are_both_credited(self):
        source = "somit somit mit"
        entries = [_entry("so"), _entry("mit", "MARTA"),
                   _entry("so"), _entry("mit", "PORTER")]
        finding = self._duplicate_findings(entries, source)[0]

        self.assertEqual("manual_review", finding["severity"])
        self.assertEqual(2, finding["details"]["unemitted_source_split_support"])

    def test_a_fused_pair_split_around_a_standalone_copy_is_not_blocked(self):
        # Source: one standalone needle, a fused "needle+word" and a fused
        # "needle needle". A faithful script emits the standalone copy and
        # splits both fused tokens, so four needle rows are all explained.
        # A greedy choice that spends the "needle needle" token on the
        # standalone copy credits only two and blocks this faithful script.
        source = " ".join(_SEPARATE_DUPLICATE_FILLER) + " \u306f\u3044 \u306f\u3044\u305d\u3046 \u306f\u3044\u306f\u3044"
        entries = ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER]
                   + [_entry("\u306f\u3044", "C1"), _entry("\u306f\u3044", "C2"),
                      _entry("\u305d\u3046"),
                      _entry("\u306f\u3044", "C3"), _entry("\u306f\u3044", "C4")])

        report = audit_script(entries, source)

        self.assertEqual(0, report["counts"]["blocking"])
        finding = self._duplicate_findings(entries, source)[0]
        self.assertEqual("manual_review", finding["severity"])
        self.assertEqual(3, finding["details"]["unemitted_source_split_support"])

    def test_audit_script_reports_a_casefold_mark_repeat(self):
        # "İ".casefold() is "i\u0307". The audit passes a word view tokenized
        # with the mark-dropping _WORD_RE, which would leave the source with
        # zero capacity while the direct check blocks. Both paths must agree.
        entries = ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER[:60]]
                   + [_entry("\u0130stanbul", "MARTA")]
                   + [_entry(word) for word in _SEPARATE_DUPLICATE_FILLER[60:]]
                   + [_entry("\u0130stanbul", "PORTER")])
        source = " ".join(_SEPARATE_DUPLICATE_FILLER) + ' "\u0130stanbul" dedi.'

        report = audit_script(entries, source)

        self.assertEqual(1, report["counts"]["blocking"])
        self.assertEqual(1, self._duplicate_findings(entries, source)[0]
                         ["details"]["source_occurrences"])

    def test_audit_script_keeps_an_ascii_apostrophe_word_count(self):
        # The audit word view splits "can't" into "can" and "t"; canonical
        # keeps one token by dropping the apostrophe. Trusting the split view
        # for an ASCII source would give the source zero capacity and abstain.
        entries = ([_entry(word) for word in _SEPARATE_DUPLICATE_FILLER[:60]]
                   + [_entry("can't", "MARTA")]
                   + [_entry(word) for word in _SEPARATE_DUPLICATE_FILLER[60:]]
                   + [_entry("can't", "PORTER")])
        source = " ".join(_SEPARATE_DUPLICATE_FILLER) + ' "can\'t" she said.'

        report = audit_script(entries, source)

        self.assertEqual(1, report["counts"]["blocking"])

    def test_audit_script_folds_an_apostrophe_variant_word_count(self):
        # The source carries two copies of a word spelled with a grave accent,
        # an apostrophe variant and no ASCII "'; canonical folds it, while the
        # audit word view splits it and gives the source zero capacity. A guard
        # that only covers ASCII " would abstain and miss the third copy.
        source = '"Can`t." He left. "Can`t," she said.'
        entries = [_entry("Can't.", "MARTA"), _entry("He left."),
                   _entry("Can't.", "PORTER"), _entry("she said."),
                   _entry("Can't.", "ANNA")]

        report = audit_script(entries, source)

        self.assertEqual(1, report["counts"]["blocking"])
        self.assertEqual(2, self._duplicate_findings(entries, source)[0]
                         ["details"]["source_occurrences"])

    def test_support_counts_only_non_overlapping_needle_occurrences(self):
        # Words "a", "a a", "a a" make overlapping "a a" matches at 0 and 1.
        # Only the non-overlapping matches (0 and 2) may be credited.
        source = '"a a" aaa'
        entries = [_entry("a", "MARTA"), _entry("a a", "PORTER"),
                   _entry("a a", "ANNA")]
        finding = next(finding for finding in self._duplicate_findings(entries, source)
                       if finding["details"]["phrase"] == "a a")

        self.assertEqual("manual_review", finding["severity"])
        self.assertEqual(1, finding["details"]["unemitted_source_split_support"])

    def test_an_emitted_digit_does_not_downgrade_a_separate_duplicate(self):
        # The digit word reaches the output as written, so it cannot have been
        # spelled out as the extra words of this repeat.
        findings = self._duplicate_findings(
            self._entries() + [_entry("3\u4eba")], self._source(extra=" 3\u4eba"))

        self.assertEqual(1, len(findings))
        self.assertEqual("blocking", findings[0]["severity"])
        self.assertFalse(findings[0]["details"]["rendering_capacity_unknown"])

    def test_a_number_rendered_as_words_downgrades_that_number(self):
        # The source carries "twelve" once and "12" once; emitting "twelve"
        # twice is the two spellings of one number, not a repeat.
        source = ' "twelve," she said. "12," he said.'
        entries = [_entry("twelve", "C1"), _entry("twelve", "C2")]

        findings = self._duplicate_findings(entries, source)

        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])
        self.assertTrue(findings[0]["details"]["rendering_capacity_unknown"])

    def test_a_digit_spelled_source_line_is_capacity_for_its_word_spelling(self):
        # "Twenty minutes" and "20 minutes" are one line written two ways: one
        # source copy of each makes two spelled-out emissions faithful (the
        # second renders the digit line). A third copy exceeds the book and
        # still blocks, so the number spelling is capacity, not an excuse.
        source = ('"Twenty minutes!" she cried. He waited. '
                  '"20 minutes," he repeated.')
        entries = [_entry("Twenty minutes", "C%d" % index) for index in range(2)]

        self.assertEqual([], self._duplicate_findings(entries, source))
        self.assertEqual(0, audit_script(entries, source)["counts"]["blocking"])

        finding = self._duplicate_findings(
            entries + [_entry("Twenty minutes", "C2")], source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(2, finding["details"]["source_occurrences"])
        self.assertEqual(3, finding["details"]["all_text_occurrences"])

    def test_a_digit_copy_after_punctuation_is_not_capacity(self):
        # The digit and the rest of the line are separated by a full stop, so
        # "20. Minutes" is two lines, not a spelling of "Twenty minutes". The
        # source holds the line once; the script's two copies are an excess.
        source = '"Twenty minutes!" she cried. It was 20. Minutes passed.'

        finding = self._duplicate_findings(
            [_entry("Twenty minutes", "C0"), _entry("Twenty minutes", "C1")],
            source)[0]

        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])
        self.assertEqual(2, finding["details"]["all_text_occurrences"])

    def test_a_digit_copy_across_a_paragraph_break_is_not_capacity(self):
        # A blank line ends the paragraph, so "20" and "minutes" on either side
        # of it are not the line "Twenty minutes" and cannot carry a copy.
        source = ('"Twenty minutes!" she cried.\n\nHe said 20\n\n'
                  "minutes passed.")

        finding = self._duplicate_findings(
            [_entry("Twenty minutes", "C0"), _entry("Twenty minutes", "C1")],
            source)[0]

        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])
        self.assertEqual(2, finding["details"]["all_text_occurrences"])

    def test_a_digit_copy_the_script_also_emits_is_not_counted_twice(self):
        # Source: one "Twenty minutes" and two "20 minutes" copies (three
        # lines). The script emits the digit line once and the spelled line
        # three times: four emissions for three copies. The digit line's own
        # emission consumes one digit copy, so only the second digit copy is
        # capacity for the spelled line and the third spelled copy blocks.
        source = ('"Twenty minutes!" she cried. "20 minutes," he said. '
                  '"20 minutes," she said.')
        entries = ([_entry("Twenty minutes", "C%d" % index) for index in range(3)]
                   + [_entry("20 minutes", "C3")])

        findings = self._duplicate_findings(entries, source)

        self.assertEqual(1, len(findings))
        self.assertEqual("blocking", findings[0]["severity"])
        self.assertEqual("twenty minutes", findings[0]["details"]["phrase"])
        self.assertEqual(2, findings[0]["details"]["source_occurrences"])
        self.assertEqual(3, findings[0]["details"]["all_text_occurrences"])

        # One spelled copy plus one digit copy emitted as digits is faithful.
        self.assertEqual([], self._duplicate_findings(
            [_entry("Twenty minutes", "C0"), _entry("20 minutes", "C1")],
            source))

    def test_a_number_spelling_only_excuses_the_same_number_and_line(self):
        # A digit elsewhere cannot excuse a line whose number does not match:
        # "99 minutes" is not a spelling of "Twenty minutes".
        source = ('"Twenty minutes!" she cried. He waited. '
                  '"99 minutes," he repeated.')

        finding = self._duplicate_findings(
            [_entry("Twenty minutes", "C0"), _entry("Twenty minutes", "C1")],
            source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])

        # A different line in the source cannot be the digit spelling either.
        source = '"Twenty minutes!" she cried. He waited. "20 seconds," he said.'
        finding = self._duplicate_findings(
            [_entry("Twenty minutes", "C0"), _entry("Twenty minutes", "C1")],
            source)[0]
        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])

    def test_a_number_does_not_downgrade_an_unrelated_line(self):
        findings = self._duplicate_findings(
            self._entries() + [_entry("12"), _entry("twelve")],
            self._source(extra=" 12 12"))

        self.assertEqual(1, len(findings))
        self.assertEqual("blocking", findings[0]["severity"])

    def test_a_chapter_numeral_downgrades_only_a_spelled_number(self):
        related = self._duplicate_findings(
            [_entry("four", "C1"), _entry("four", "C2")],
            ' "four," she said. "Chapter IV," he said.')
        unrelated = self._duplicate_findings(
            self._entries() + [_entry("Chapter"), _entry("Four")],
            self._source(extra=" Chapter IV"))

        self.assertEqual("manual_review", related[0]["severity"])
        self.assertEqual(1, len(unrelated))
        self.assertEqual("blocking", unrelated[0]["severity"])

    def test_a_digit_bearing_needle_keeps_the_whole_source_rule(self):
        # The source carries two spellings of one line, "three" and "3".
        # Emitting "3." twice is those two copies, not a repeat, so a digit in
        # the needle must keep the conservative rendering rule.
        source = ' "three," she said. "3," he said.'
        entries = [_entry("3.", "C1"), _entry("3.", "C2")]

        findings = self._duplicate_findings(entries, source)

        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])

    def test_a_bracket_keeps_the_whole_source_rule(self):
        findings = self._duplicate_findings(self._entries(), self._source(extra=" [note]"))

        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])
        self.assertTrue(findings[0]["details"]["rendering_capacity_unknown"])

    def test_an_over_emitted_digit_line_cannot_erase_the_spelled_finding(self):
        # An over-emitted digit line must not reduce the spelled line's
        # capacity below zero. Before the clamp, capacity landed on exactly 0
        # and `if not capacity: continue` dropped the finding entirely, even
        # though four emissions stand against two source copies.
        source = '"Twenty minutes!" she cried. "20 minutes," he said. Chapter 2.'
        entries = [_entry("Twenty minutes", "C0"), _entry("Twenty minutes", "C1"),
                   _entry("20 minutes", "C2"), _entry("20 minutes", "C3")]

        findings = self._duplicate_findings(entries, source)

        spelled = next(finding for finding in findings
                       if finding["details"]["phrase"] == "twenty minutes")
        self.assertEqual("blocking", spelled["severity"])
        self.assertEqual(1, spelled["details"]["source_occurrences"])
        self.assertEqual(2, spelled["details"]["all_text_occurrences"])
        self.assertTrue(all(finding["details"]["source_occurrences"] >= 0
                            for finding in findings))

    def test_a_fused_digit_spelling_cannot_erase_the_spelled_finding(self):
        # The source carries three copies (spelled, spaced digit, fused digit)
        # and the script emits two of each. The spaced digit line is emitted
        # beyond its one copy, which used to zero the spelled capacity and
        # silently accept the duplicate.
        source = ('"Twenty minutes!" she cried. "20 minutes," he said. '
                  '"20minutes," she said. Chapter 2.')
        entries = [_entry("Twenty minutes", "C0"), _entry("Twenty minutes", "C1"),
                   _entry("20 minutes", "C2"), _entry("20 minutes", "C3")]

        findings = self._duplicate_findings(entries, source)

        spelled = next(finding for finding in findings
                       if finding["details"]["phrase"] == "twenty minutes")
        self.assertEqual("blocking", spelled["severity"])
        self.assertEqual(1, spelled["details"]["source_occurrences"])
        self.assertEqual(2, spelled["details"]["all_text_occurrences"])
        self.assertTrue(all(finding["details"]["source_occurrences"] >= 0
                            for finding in findings))

    def test_an_over_emitted_digit_line_never_reports_a_negative_count(self):
        # Two spelled emissions and three digit emissions against one copy of
        # each. The unclamped subtraction reported source_occurrences=-1.
        source = '"Twenty minutes," she said. "20 minutes," he said. Chapter 2.'
        entries = ([_entry("Twenty minutes", "C0"), _entry("Twenty minutes", "C1")]
                   + [_entry("20 minutes", "C%d" % index) for index in range(2, 5)])

        findings = self._duplicate_findings(entries, source)
        spelled = next(finding for finding in findings
                       if finding["details"]["phrase"] == "twenty minutes")

        self.assertEqual("blocking", spelled["severity"])
        self.assertEqual(1, spelled["details"]["source_occurrences"])
        self.assertTrue(all(finding["details"]["source_occurrences"] >= 0
                            for finding in findings))

        # Three spelled copies plus one digit copy: the true source count is
        # three, so the clamp also stops the reported count sinking below it
        # when the digit line is over-emitted.
        source = ('"Twenty minutes!" a. "Twenty minutes!" b. '
                  '"Twenty minutes!" c. "20 minutes," d. Chapter 2.')
        entries = ([_entry("Twenty minutes", "C%d" % index) for index in range(4)]
                   + [_entry("20 minutes", "C%d" % index) for index in range(4, 7)])

        findings = self._duplicate_findings(entries, source)
        spelled = next(finding for finding in findings
                       if finding["details"]["phrase"] == "twenty minutes")

        self.assertEqual(3, spelled["details"]["source_occurrences"])
        self.assertEqual(4, spelled["details"]["all_text_occurrences"])
        self.assertTrue(all(finding["details"]["source_occurrences"] >= 0
                            for finding in findings))

    def test_a_decimal_fragment_is_not_a_whole_source_number(self):
        # "3.5" tokenizes as "3", "5". The "5" is part of one numeral, not a
        # whole source number, so it must not spell "Five minutes" and accept
        # a real duplicate of the line that the source carries once.
        source = '"Five minutes!" she cried. He waited. "3.5 minutes," he said.'
        finding = self._duplicate_findings(
            [_entry("Five minutes", "C0"), _entry("Five minutes", "C1")],
            source)[0]

        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])
        self.assertEqual(2, finding["details"]["all_text_occurrences"])

    # (spelled line, digit spelling, credited, severity with 3 word emissions)
    _WHOLE_NUMBER_SHAPES = (
        ("Five minutes", "5 minutes", True, "blocking"),
        ("Zero minutes", "1,000 minutes", False, "blocking"),
        ("Zero minutes", "1 000 minutes", False, "blocking"),
        ("Zero minutes", "1\u00a0000 minutes", False, "blocking"),
        ("Zero minutes", "1\u2009000 minutes", False, "blocking"),
        ("Zero minutes", "1\u2007000 minutes", False, "blocking"),
        ("Zero minutes", "1 000 000 minutes", False, "blocking"),
        ("Twenty five", "twenty 5.", True, "manual_review"),
        ("Twenty five", "twenty 5, and more", True, "manual_review"),
        ("Five minutes", "5th minutes", False, "blocking"),
        ("Five percent of it", "5% of it", False, "blocking"),
        ("Five dollars", "$5 dollars", True, "blocking"),
        ("Five dollars each", "$5 each", False, "blocking"),
        ("Six minutes", "5-6 minutes", False, "blocking"),
        ("Five minutes", "5-6 minutes", False, "blocking"),
        ("Six minutes", "5\u20136 minutes", False, "blocking"),
        ("Six minutes", "5\u20146 minutes", False, "blocking"),
        ("Six minutes", "5\u20126 minutes", False, "blocking"),
        ("Six minutes", "5\u22126 minutes", False, "blocking"),
        ("Six minutes", "5/6 minutes", False, "blocking"),
        ("Thirty exactly", "5:30 exactly", False, "blocking"),
        ("Five minutes", "\u0665 minutes", True, "blocking"),
        ("Five minutes", "\uff15 minutes", True, "blocking"),
    )

    def test_a_whole_source_number_is_one_undivided_numeral(self):
        # One table for every whole-number shape the rule defines. Credited
        # means the source digit copy is capacity for the word spelling: two
        # word emissions are clean and the third blocks (blocking, or
        # manual_review when the digit spelling was not emitted and the needle
        # is a number word). Not credited means the digit spelling is not
        # capacity for the line (a fragment of a longer numeral, or a shape the
        # rule does not cover): the spelled word emissions block at src=1/out=2,
        # and a third emission also blocks.
        for spelled, digits, credited, severity in self._WHOLE_NUMBER_SHAPES:
            with self.subTest(shape=spelled, digits=digits):
                source = '"%s," she said. "%s," he said.' % (spelled, digits)
                entries = [_entry(spelled, "C0"), _entry(spelled, "C1")]

                findings = self._duplicate_findings(entries, source)
                if credited:
                    self.assertEqual([], findings)
                else:
                    self.assertEqual(1, len(findings))
                    self.assertEqual("blocking", findings[0]["severity"])
                    self.assertEqual(1, findings[0]["details"]["source_occurrences"])
                    self.assertEqual(2, findings[0]["details"]["all_text_occurrences"])

                third = self._duplicate_findings(
                    entries + [_entry(spelled, "C2")], source)
                self.assertEqual(1, len(third))
                self.assertEqual(severity, third[0]["severity"])
                self.assertEqual(2 if credited else 1,
                                 third[0]["details"]["source_occurrences"])
                self.assertEqual(3, third[0]["details"]["all_text_occurrences"])

    def test_a_line_break_is_never_a_numeral_separator(self):
        # Gate 3 B1: a digit that opens a line after a line ending in a number
        # opens a NEW line, not the far half of one numeral. Every line ending
        # and every vertical whitespace that readers treat as a break keeps
        # the digit whole, so a faithful book with a chapter number or a year
        # above "5 minutes later" stays clean.
        entries = [_entry("Five minutes later", "C0"),
                   _entry("Five minutes later", "C1")]
        for line_break in ("\n", "\r", "\r\n", "\u2028", "\u2029",
                           "\x0b", "\x0c", "\x85"):
            with self.subTest(line_break=repr(line_break)):
                source = ('"Five minutes later," she said.'
                          '%s12%s5 minutes later, he left.'
                          % (line_break, line_break))

                self.assertEqual([], self._duplicate_findings(entries, source))

                third = self._duplicate_findings(
                    entries + [_entry("Five minutes later", "C2")], source)
                self.assertEqual(1, len(third))
                self.assertEqual("blocking", third[0]["severity"])
                self.assertEqual(2, third[0]["details"]["source_occurrences"])
                self.assertEqual(3, third[0]["details"]["all_text_occurrences"])

    def test_the_reviewed_line_break_sources_stay_clean(self):
        # The gate 3 B1 repro and the shapes the review named beside it: a
        # chapter heading, a year line, a numbered line, a "- 12 -" page
        # number and a hard-wrapped Gutenberg line. The source is faithful, so
        # two word emissions are clean and only a third blocks.
        entries = [_entry("Five minutes later", "C0"),
                   _entry("Five minutes later", "C1")]
        sources = (
            '"Five minutes later," she said.\n\nChapter 12\n\n'
            '5 minutes later, he left.',                      # the B1 repro
            '"Five minutes later," she said.\n\n1945\n\n'
            '5 minutes later, he left.',                      # year line
            '"Five minutes later," she said.\n\n3\n\n'
            '5 minutes later, he left.',                      # numbered line
            '"Five minutes later," she said.\n\n- 12 -\n\n'
            '5 minutes later, he left.',                      # page number
            '"Five minutes later," she said. It was room 12\n'
            '5 minutes later, he left.',                      # soft wrap
        )
        for source in sources:
            with self.subTest(source=source):
                self.assertEqual([], self._duplicate_findings(entries, source))

                third = self._duplicate_findings(
                    entries + [_entry("Five minutes later", "C2")], source)
                self.assertEqual(1, len(third))
                self.assertEqual("blocking", third[0]["severity"])
                self.assertEqual(2, third[0]["details"]["source_occurrences"])
                self.assertEqual(3, third[0]["details"]["all_text_occurrences"])

    def test_a_thousands_space_joins_only_a_three_digit_group(self):
        # Gate 3 L1: a space groups thousands only when the group after it is
        # exactly three digits. "1 000" is one numeral, so neither the "1" nor
        # the "000" can spell a word number ("1 000 000" joins all three
        # groups), while "In 1999 5 minutes later" is a year and a separate
        # number: the "5" is whole and carries the digit copy.
        for digits in ("1 000 minutes", "1\u00a0000 minutes",
                       "1\u2009000 minutes", "1 000 000 minutes"):
            with self.subTest(digits=digits):
                source = '"Zero minutes," she said. "%s," he said.' % digits
                self.assertEqual(1, len(self._duplicate_findings(
                    [_entry("Zero minutes", "C0"),
                     _entry("Zero minutes", "C1")], source)))

        source = ('"Five minutes later," she said. In 1999 5 minutes later, '
                  'he left.')
        entries = [_entry("Five minutes later", "C0"),
                   _entry("Five minutes later", "C1")]
        self.assertEqual([], self._duplicate_findings(entries, source))
        third = self._duplicate_findings(
            entries + [_entry("Five minutes later", "C2")], source)
        self.assertEqual(1, len(third))
        self.assertEqual(2, third[0]["details"]["source_occurrences"])
        self.assertEqual(3, third[0]["details"]["all_text_occurrences"])

    def test_a_digit_used_as_an_identifier_is_a_known_limit(self):
        # The second F2 repro, kept and documented. "Route 1 more often" holds
        # the whole number "1" beside "more", one token different from "One
        # more", so the source is credited with two spellings and two word
        # emissions stay clean. Nothing in the text says whether "1" is a count
        # or a route number; separating them needs provenance, not a token rule.
        source = '"One more!" she cried. He waited. "Route 1 more often," he said.'

        self.assertEqual([], self._duplicate_findings(
            [_entry("One more", "C0"), _entry("One more", "C1")], source))

    _DOCUMENTED_BLOCK_SHAPES = (
        ("Twenty-one minutes", "21 minutes"),
        ("One hundred men", "100 men"),
        ("Second floor", "2nd floor"),
        ("Nineteen ninety, it was", "1990, it was"),
        ("Chapter four begins", "Chapter IV begins"),
        ("Three point five miles", "3.5 miles"),
        ("Zwanzig Minuten", "20 Minuten"),
        ("The nineties were loud", "The 1990s were loud"),
        ("Five percent of it", "5% of it"),
        ("Five dollars each", "$5 each"),
        ("One half of it", "0.5 of it"),
        ("Half past three exactly", "3:30 exactly"),
    )

    def test_documented_number_shapes_outside_the_table_still_block(self):
        # These shapes need variable-length number phrases, ordinals, decimals,
        # a fused digit spelling or another language's tables. Each is a
        # documented false block: a faithful 1 spelled + 1 digit source with the
        # word form emitted twice blocks at src=1/out=2, and a real third copy
        # still blocks. Pinned so a future widening cannot silently accept them.
        for spelled, digits in self._DOCUMENTED_BLOCK_SHAPES:
            with self.subTest(shape=spelled):
                source = '"%s," she said. "%s," he said.' % (spelled, digits)
                entries = [_entry(spelled, "C0"), _entry(spelled, "C1")]

                finding = self._duplicate_findings(entries, source)[0]
                self.assertEqual("blocking", finding["severity"])
                self.assertEqual(1, finding["details"]["source_occurrences"])
                self.assertEqual(2, finding["details"]["all_text_occurrences"])

                duplicate = self._duplicate_findings(
                    entries + [_entry(spelled, "C2")], source)[0]
                self.assertEqual("blocking", duplicate["severity"])
                self.assertEqual(3, duplicate["details"]["all_text_occurrences"])

    def test_a_fused_digit_spelling_still_blocks(self):
        # "20minutes" is one token, not a digit line, so it cannot be capacity
        # for "Twenty minutes"; the duplicate still blocks.
        source = '"Twenty minutes!" she cried. "20minutes," he said.'
        finding = self._duplicate_findings(
            [_entry("Twenty minutes", "C0"), _entry("Twenty minutes", "C1")],
            source)[0]

        self.assertEqual("blocking", finding["severity"])
        self.assertEqual(1, finding["details"]["source_occurrences"])

    def test_audit_script_surfaces_the_separate_duplicate_as_blocking(self):
        report = audit_script(self._entries(), self._source())

        duplicate = next(finding for finding in report["findings"]
                         if finding["code"] == "contained_source_duplicate")
        self.assertEqual("blocking", duplicate["severity"])
        self.assertFalse(report["can_apply_repairs"])
        self.assertEqual(1, report["counts"]["blocking"])


class NumberSpellingDigitIndexTests(unittest.TestCase):
    """The digit-context index must select exactly the full scan's windows."""

    @staticmethod
    def _brute_force(tokens, fusable, words, whole_number):
        accepted = Counter()
        next_free = 0
        for start in range(len(tokens) - len(words) + 1):
            if start < next_free:
                continue
            window = tokens[start:start + len(words)]
            differences = [index for index, (left, right)
                           in enumerate(zip(words, window)) if left != right]
            if len(differences) != 1:
                continue
            if not all(fusable[position]
                       for position in range(start + 1, start + len(words))):
                continue
            position = differences[0]
            digits = window[position]
            if not (digits.isdigit() and whole_number[start + position]
                    and is_spelled_number([words[position]])
                    and is_same_number([words[position]], [digits])):
                continue
            next_free = start + len(words)
            accepted[" ".join(window)] += 1
        return accepted

    def test_the_digit_context_index_selects_the_full_scan_windows(self):
        # P1: candidates come from digits beside the needle's own words instead
        # of every position of its first word. Each case plants a digit window
        # at the first, an interior or the last needle position, then pads it
        # with random words. Two decoy windows of the same needle follow it:
        # one behind a digit that is not a whole number (excluded from the
        # index) and one behind a non-fusable gap (rejected by the caller).
        # Both are otherwise acceptable, so the accepted set equals the full
        # scan's only if both filters survive, with the same non-overlap order.
        rng = random.Random(2293)
        spellings = {"1": "one", "2": "two", "5": "five",
                     "20": "twenty", "90": "ninety"}
        words_vocabulary = ["the", "she", "said", "minutes", "one",
                            "two", "five", "twenty", "ninety"]
        for case in range(300):
            mode = case % 3
            digit = rng.choice(sorted(spellings))
            spelled = spellings[digit]
            before = rng.choice(words_vocabulary)
            after = rng.choice(words_vocabulary)
            lead = [rng.choice(words_vocabulary) for _ in range(rng.randint(0, 3))]
            tail = [rng.choice(words_vocabulary) for _ in range(rng.randint(0, 3))]
            if mode == 0:
                tokens = lead + [before, digit, after] + tail
                words = [before, spelled, after]
            elif mode == 1:
                tokens = lead + [digit, after] + tail
                words = [spelled, after]
            else:
                tokens = lead + [before, digit] + tail
                words = [before, spelled]
            # Decoy windows: same shape as the needle, so only the whole-number
            # flag and the gap rule can stop the full scan accepting them.
            fragment_start = len(tokens)
            tokens += [before, digit, after]
            gap_start = len(tokens)
            tokens += [before, digit, after]
            fusable = [True] * len(tokens)
            whole_number = [True] * len(tokens)
            whole_number[fragment_start + 1] = False
            fusable[gap_start + 1] = False
            fusable[gap_start + 2] = False
            index = _build_number_spelling_digit_index(tokens, whole_number)
            expected = self._brute_force(tokens, fusable, words, whole_number)
            actual = _number_spelling_digit_lines(tokens, fusable, words, index)
            self.assertEqual(
                expected, actual,
                "tokens=%r words=%r fusable=%r whole=%r"
                % (tokens, words, fusable, whole_number))
            # The lead and tail hold no digit, and both decoys are filtered, so
            # the planted window is the only one a full scan can accept; a
            # candidate set that misses a first/interior/last digit, or an
            # index that credits a fragment or ignores a gap, differs here.
            self.assertEqual(1, sum(expected.values()),
                             "planted window was not accepted: %r %r"
                             % (tokens, words))


if __name__ == "__main__":
    unittest.main()
