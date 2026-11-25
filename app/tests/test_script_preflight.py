import asyncio
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi import HTTPException

from chunk_quality import validate_chunk_quality
import generate_script
from routers import scripts_library
from script_preflight import (audit_script, audit_unicode_text,
                              find_contained_source_duplicates)
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

    def test_unknown_number_rendering_downgrades_to_manual_review(self):
        findings = self._duplicate_findings(self._entries(),
                                            self._source(extra=" Chapter 2."))

        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])
        self.assertTrue(findings[0]["details"]["rendering_capacity_unknown"])
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
        de_fill = ("Der Morgen war kalt und der Bahnsteig leer . Marta wartete neben der "
                   "alten Uhr und beobachtete die Gleise . Der Traeger ging langsam und "
                   "hielt eine Lampe . ")
        ja_fill = ("\u671d\u306f\u5bd2\u304f\u3066\u30db\u30fc\u30e0\u306f\u7a7a\u3063\u307d\u3060\u3063\u305f\u3002"
                   "\u30de\u30eb\u30bf\u306f\u53e4\u3044\u6642\u8a08\u306e\u305d\u3070\u3067\u5f85\u3063\u3066\u3044\u305f\u3002"
                   "\u8a70\u3081\u6240\u306f\u30e9\u30f3\u30d7\u3092\u6301\u3063\u3066\u8fd1\u3065\u3044\u3066\u304d\u305f\u3002")
        cases = (
            (de_fill + '\u201eja ja\u201c sagte er . Und dann sagte sie nur: jaja .',
             [_entry(de_fill), _entry("ja ja", "MARTA"),
              _entry("sagte er . Und dann sagte sie nur:"), _entry("ja ja", "PORTER")],
             "ja ja"),
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

    def test_a_number_rendered_as_words_downgrades_a_separate_duplicate(self):
        findings = self._duplicate_findings(
            self._entries() + [_entry("twelve")], self._source(extra=" 12"))

        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])
        self.assertTrue(findings[0]["details"]["rendering_capacity_unknown"])

    def test_a_digit_emitted_once_of_twice_downgrades_a_separate_duplicate(self):
        findings = self._duplicate_findings(
            self._entries() + [_entry("12"), _entry("twelve")],
            self._source(extra=" 12 12"))

        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])

    def test_a_chapter_numeral_rendered_as_words_downgrades_a_duplicate(self):
        findings = self._duplicate_findings(
            self._entries() + [_entry("Chapter"), _entry("Four")],
            self._source(extra=" Chapter IV"))

        self.assertEqual(1, len(findings))
        self.assertEqual("manual_review", findings[0]["severity"])

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

    def test_audit_script_surfaces_the_separate_duplicate_as_blocking(self):
        report = audit_script(self._entries(), self._source())

        duplicate = next(finding for finding in report["findings"]
                         if finding["code"] == "contained_source_duplicate")
        self.assertEqual("blocking", duplicate["severity"])
        self.assertFalse(report["can_apply_repairs"])
        self.assertEqual(1, report["counts"]["blocking"])


if __name__ == "__main__":
    unittest.main()
