"""Soft hyphens (U+00AD) and word joiners (U+2060) inside words (#2316).

A publisher's soft hyphen is an optional line-break point; a word joiner is a
no-break point. Both are invisible and neither is a word boundary, but the pass
gate tokenised them as one, so a model returning the ordinary spelling looked
as if it had dropped words. They also reached the TTS input.

The fix is one rule at the comparison/speech layer: sources, saved books,
checkpoints and takes keep their exact bytes, while word comparison and speech
preparation delete these two characters. Books without them are untouched.
"""
import contextlib
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient

from routers import scripts_library
from silent_word_characters import (SILENT_WORD_CHARACTERS, WORD_BOUNDARIES,
                                    ZERO_WIDTH_SPACE,
                                    remove_silent_word_characters,
                                    map_word_boundaries_to_spaces)
from recall_core import tokens
from chunk_quality import validate_chunk_quality
from pass_quality import (validate_segment_quality, is_attested_name,
                          validate_attribution,
                          get_attribution_tag_evidence, get_attribution_tag_proofs,
                          get_exact_attribution_shift_repair,
                          get_attribution_snapshot_digest)
from literal_fidelity import audit_literal_words, iter_literal_words
from script_preflight import get_normalized_word_tokens, find_adjacent_duplicate_blocks
from dialogue_spans import mark_entries, speaker_labels, uses_speaker_labels
from speech_text import get_speech_normalization, normalize_for_speech
from generate_personas import _resolve_to_canonical, normalize_speaker_name
from pronunciation import (OccurrenceAnchorConflict, apply_pronunciation,
                           get_occurrence_pronunciation_resolution,
                           get_pronunciation_result,
                           get_pronunciation_text_digest,
                           validate_occurrence_pronunciation_rules)
from generate_script import _get_boundary_overlap
from text_diff import get_comparison_words, word_diff
from speaker_identity import (stabilize_speaker_identities,
                              _is_extended_person_name,
                              is_speaker_merge_allowed)
from narrator_prompt import (get_valid_narrator_name, is_narrator_attested,
                             normalize_narrator_name)
from dialogue_continuity import _is_attached_tag
from gold_set_builder import support_for
import candidates
import script_repair
from find_nicknames import _name_tokens, collect_context
from word_timing import get_word_tokens
from speech_verification_compare import get_comparison_tokens
import epub_readaloud
import qwen_align
import three_pass_generate
from tests import test_sound_design_upload_selection as selection_tests
from tests.test_upload_publication_source import epub


SOFT = "\u00ad"
JOINER = "\u2060"


def passage(word_count=181):
    """A synthetic passage of ordinary words (no source text is copied)."""
    return [f"word{number}" for number in range(word_count)]


def soft_hyphenated_passage(word_count=181, positions=(10, 30, 50, 70, 90, 110)):
    words = passage(word_count)
    for position in positions:
        words[position] = ("won" + SOFT + "der" + SOFT + "ful")
    return " ".join(words) + "."


def without_silent_characters(text):
    return text.replace(SOFT, "").replace(JOINER, "")


class SilentWordCharacterRuleTests(unittest.TestCase):
    def test_the_two_characters_are_deleted_not_replaced_by_a_space(self):
        for source, expected in (
                ("won" + SOFT + "der" + SOFT + "ful", "wonderful"),
                ("a" + JOINER + "b", "ab"),
                ("sev" + SOFT + JOINER + SOFT + "eral", "several"),
                ("1" + SOFT + "000", "1000"),
                ("\u6f22" + SOFT + "\u5b57", "\u6f22\u5b57"),
                ("\u201c" + SOFT + "Hi\u201d", "\u201cHi\u201d"),
                ("Son" + SOFT + "ya", "Sonya"),
                ("well" + SOFT + "-being", "well-being"),
                (SOFT + "leading", "leading"),
                ("trailing" + SOFT, "trailing"),
                ("WON" + SOFT + "DERFUL", "WONDERFUL"),
        ):
            with self.subTest(source=repr(source)):
                self.assertEqual(expected, remove_silent_word_characters(source))

    def test_characters_with_meaning_are_not_touched(self):
        for text in ("\U0001f469\u200d\U0001f680",       # emoji ZWJ sequence
                     "\u0645\u06cc\u200c\u0631\u0648\u0645",  # Persian ZWNJ
                     "\u0939\u093f\u0928\u094d\u0926\u0940",  # Devanagari
                     "\u0634\u0644\u0648\u0645\u200f",       # RTL mark
                     "\u0391\u2066\u03b2\u2069",             # bidi isolate
                     "\u2764\ufe0f"):                        # variation selector
            with self.subTest(text=repr(text)):
                self.assertEqual(text, remove_silent_word_characters(text))

    def test_unicode_normalization_forms_are_preserved(self):
        self.assertEqual("e\u0301", remove_silent_word_characters("e" + SOFT + "\u0301"))
        self.assertEqual("\u00e9", remove_silent_word_characters("\u00e9" + JOINER))

    def test_zero_width_space_is_a_boundary_not_a_silent_character(self):
        # U+200B separates words, so deleting it would fuse them (#2316).
        text = "alpha" + ZERO_WIDTH_SPACE + "beta"
        self.assertEqual(text, remove_silent_word_characters(text))
        self.assertEqual("alpha beta", map_word_boundaries_to_spaces(text))
        self.assertIn(ZERO_WIDTH_SPACE, WORD_BOUNDARIES)
        self.assertNotIn(ZERO_WIDTH_SPACE, SILENT_WORD_CHARACTERS)
        # Characters with a lexical or directional meaning stay out of both
        # definitions: ZWNJ/ZWJ, BOM, bidi marks and isolates.
        for char in ("\u200c", "\u200d", "\ufeff", "\u200e", "\u202a", "\u2066"):
            with self.subTest(char=repr(char)):
                self.assertNotIn(char, SILENT_WORD_CHARACTERS)
                self.assertNotIn(char, WORD_BOUNDARIES)

    def test_the_comparison_key_keeps_zwnj_and_deletes_zwj(self):
        # The classification lives here: ZWNJ is lexical (kept inside its word,
        # #2316), ZWJ is a glyph-joining hint (deleted, so the Devanagari
        # conjunct is one word). Both constants name the character the code is
        # talking about.
        from script_preflight import get_comparison_word_view
        self.assertEqual("می\u200cروم".casefold(),
                         get_comparison_word_view("می\u200cروم"))
        self.assertEqual("gohome", get_comparison_word_view("Go\u200dhome"))

    def test_literal_words_keep_a_joiner_inside_the_word(self):
        # literal_fidelity verifies words character for character, so both
        # joiners stay inside the literal token even though the comparison key
        # deletes ZWJ. It keeps its own rule rather than sharing the comparison
        # classification, which would change the word it reports.
        self.assertEqual([("Go\u200dhome", 0, 7)],
                         list(iter_literal_words("Go\u200dhome")))

    def test_a_soft_hyphen_at_a_hard_line_end_leaves_the_line_break(self):
        # A real newline is line structure, not an invisible character. Only the
        # SHY is deleted; the word the line break already split stays split.
        self.assertEqual("won\nderful", remove_silent_word_characters("won" + SOFT + "\nderful"))
        self.assertEqual(["won", "derful"], tokens("won" + SOFT + "\nderful"))


class GateTokenizerTests(unittest.TestCase):
    def test_recall_tokens_join_the_word(self):
        self.assertEqual(["sonya"], tokens("Son" + SOFT + "ya"))
        self.assertEqual(["ab"], tokens("a" + JOINER + "b"))
        self.assertEqual(["wonderful"], tokens("won" + SOFT + "derful"))

    def test_the_fold_happens_before_nfc_so_a_combining_mark_still_composes(self):
        # NFC("e" + SHY + U+0301) leaves the mark loose because SHY sits
        # between; deleting first and normalizing after gives the composed
        # character every comparison expects (#2316).
        written = "e" + SOFT + "\u0301"
        self.assertEqual(["é"], tokens(written))
        self.assertEqual(("é",), get_normalized_word_tokens(written))
        self.assertEqual([("é", 0, 3)], list(iter_literal_words(written)))
        self.assertEqual(["é"], [word for word, _start, _end in
                                 iter_literal_words(written)])
        self.assertTrue(audit_literal_words(
            written, [{"text": "\u00e9"}])["passed"])

    def test_pass1_gate_accepts_the_clean_spelling(self):
        report = validate_segment_quality(
            soft_hyphenated_passage(),
            [{"type": "NARRATOR", "text": without_silent_characters(soft_hyphenated_passage())}])
        self.assertTrue(report["passed"], report["findings"])
        self.assertEqual(1.0, report["metrics"]["ordered_trigram_recall"])

    def test_single_pass_gate_accepts_the_clean_spelling(self):
        report = validate_chunk_quality(
            soft_hyphenated_passage(),
            [{"speaker": "NARRATOR", "text": without_silent_characters(soft_hyphenated_passage()), "instruct": "Read."}])
        self.assertTrue(report["passed"], report["findings"])

    def test_literal_mode_accepts_the_clean_spelling(self):
        source = "Son" + SOFT + "ya read won" + SOFT + "derful words."
        report = audit_literal_words(
            source, [{"text": "Sonya read wonderful words."}])
        self.assertTrue(report["passed"], report)

    def test_literal_word_spans_still_point_into_the_raw_text(self):
        self.assertEqual(
            [("wonderful", 0, 10)],
            list(iter_literal_words("won" + SOFT + "derful")))

    def test_preflight_word_tokens_join_the_word(self):
        self.assertEqual(("sonya", "and", "ab"),
                         get_normalized_word_tokens(
                             "Son" + SOFT + "ya and a" + JOINER + "b"))

    def test_speaker_names_fold_to_the_same_identity(self):
        self.assertEqual("sonya", normalize_speaker_name("Son" + SOFT + "ya"))
        self.assertEqual("Sonya", _resolve_to_canonical("Son" + SOFT + "ya", ["Sonya"]))

    def test_source_spans_are_found_through_the_character(self):
        for source, entry_text in (
                ("He saw Son" + SOFT + "ya go.", "He saw Sonya go."),
                ("He saw Sonya go.", "He saw Son" + SOFT + "ya go.")):
            with self.subTest(source=repr(source), entry=repr(entry_text)):
                marked = mark_entries(
                    [{"type": "NARRATOR", "text": entry_text}], source)
                self.assertEqual([0, len(source)], marked[0].get("source_span"))
                self.assertIs(False, marked[0].get("spoken"))


class SpeechPreparationTests(unittest.TestCase):
    def test_speech_text_never_contains_the_characters(self):
        for source, expected in (
                ("The won" + SOFT + "der" + SOFT + "ful day a" + JOINER + "b.",
                 "The wonderful day ab."),
                ("1" + SOFT + "000.", "1000."),
                ("\u201c" + SOFT + "Hi\u201d", "\u201cHi\u201d."),
        ):
            with self.subTest(source=repr(source)):
                result = normalize_for_speech(source)
                self.assertNotIn(SOFT, result)
                self.assertNotIn(JOINER, result)
                self.assertEqual(expected, result)

    def test_removal_is_recorded(self):
        result = get_speech_normalization(
            "Son" + SOFT + "ya and a" + JOINER + "b.")
        self.assertEqual("Sonya and ab.", result["text"])
        self.assertEqual(
            {"type": "removed_silent_word_characters",
             "symbols": [SOFT, JOINER], "count": 2},
            next(change for change in result["transformations"]
                 if change["type"] == "removed_silent_word_characters"))

    def test_chapter_headings_are_spoken_without_the_characters(self):
        result = normalize_for_speech("Chapter 1: The Won" + SOFT + "der" + SOFT + "ful")
        self.assertEqual("Chapter 1: The Wonderful.", result)
        self.assertNotIn(SOFT, result)

    def test_a_pronunciation_key_clean_matches_text_that_had_the_character(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "pronunciation.json")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump({"names": {"Sonya": "Sone-ya"}}, handle)
            prepared = normalize_for_speech("Son" + SOFT + "ya spoke.")
            spoken, applied = apply_pronunciation(prepared, path)
        self.assertEqual("Sone-ya spoke.", spoken)
        self.assertEqual([{"name": "Sonya", "spoken": "Sone-ya"}], applied)

    def test_text_without_the_characters_is_unchanged(self):
        for text in ("Plain prose.", "She said \u201chello\u201d and it\u2019s fine.",
                     "\u6f22\u5b57\u3067\u3059\u3002"):
            with self.subTest(text=repr(text)):
                result = get_speech_normalization(text)
                self.assertEqual(text, result["text"])
                self.assertFalse(result["changed"])
                self.assertEqual([], result["transformations"])
                self.assertNotIn("removed_silent_word_characters",
                                 [change["type"] for change in result["transformations"]])


class ImportBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_epub_import_keeps_the_source_bytes_and_speech_cleans_them(self):
        text = ("<p>won&shy;der&#173;ful a&NoBreak;b line<wbr>break</p>")
        async with selection_tests.SoundDesignUploadSelectionTests().fixture() as (root, _uploads, client):
            response = await client.post(
                "/api/upload",
                files={"file": ("book.epub", epub(text=text.encode("utf-8")),
                                "application/epub+zip")})
            self.assertEqual(200, response.status_code, response.text)
            selected = root / json.loads((root / "state.json").read_text())["input_file_path"]
            saved = selected.read_text(encoding="utf-8")
            # Import is deliberately unchanged: no hash, offset or checkpoint moves.
            self.assertIn(SOFT, saved)
            self.assertIn(JOINER, saved)
            self.assertNotIn("\u200b", saved)
            self.assertEqual("wonderful ab linebreak.",
                             normalize_for_speech("won" + SOFT + "der" + SOFT + "ful "
                                                  "a" + JOINER + "b linebreak"))

    async def test_txt_import_is_byte_identical(self):
        raw = ("Son" + SOFT + "ya read won" + SOFT + "derful lines.").encode("utf-8")
        async with selection_tests.SoundDesignUploadSelectionTests().fixture() as (root, _uploads, client):
            response = await client.post(
                "/api/upload", files={"file": ("book.txt", raw, "text/plain")})
            self.assertEqual(200, response.status_code, response.text)
            selected = root / json.loads((root / "state.json").read_text())["input_file_path"]
            self.assertEqual(raw, selected.read_bytes())


class SavedBookReloadTests(unittest.TestCase):
    def test_a_saved_book_containing_the_characters_reloads_unchanged(self):
        from tests.test_saved_book_publication import SavedBookPublicationTests
        payload = [{"speaker": "Alice",
                    "text": "Son" + SOFT + "ya read won" + SOFT + "derful lines.",
                    "instruct": "Read."}]
        with SavedBookPublicationTests().fixture() as (root, api), \
                contextlib.ExitStack() as stack:
            for key in ("AUDIOBOOK_PATH", "M4B_PATH", "CHUNKS_PATH"):
                stack.enter_context(patch.object(
                    scripts_library, key,
                    str(root / os.path.basename(getattr(scripts_library, key)))))
            (root / "annotated_script.json").write_text(
                json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            with TestClient(api) as client:
                saved = client.post("/api/scripts/save", json={"name": "book"})
                self.assertEqual(200, saved.status_code, saved.text)
                (root / "annotated_script.json").write_text("[]", encoding="utf-8")
                loaded = client.post("/api/scripts/load", json={"name": "book"})
                self.assertEqual(200, loaded.status_code, loaded.text)
            self.assertEqual(payload, json.loads(
                (root / "annotated_script.json").read_text(encoding="utf-8")))
            self.assertIn(SOFT, (root / "annotated_script.json").read_text(encoding="utf-8"))


class SpeakerIdentityConsumerTests(unittest.TestCase):
    def test_an_attested_name_joins_the_word_in_both_directions(self):
        dirty = ('"Go now," Son' + SOFT + 'ya said. Son' + SOFT + 'ya left. '
                 'Then Son' + SOFT + 'ya sat.')
        clean = without_silent_characters(dirty)
        self.assertTrue(is_attested_name("SONYA", dirty))
        self.assertTrue(is_attested_name(("Son" + SOFT + "ya").upper(), clean))

    def test_speech_tag_evidence_joins_the_word_in_both_directions(self):
        entries = [{"type": "SPOKEN", "text": "Go now,"}]
        dirty = '"Go now," Son' + SOFT + 'ya said. Son' + SOFT + 'ya sat.'
        clean = without_silent_characters(dirty)
        self.assertEqual({0: "SONYA"}, get_attribution_tag_evidence(entries, dirty))
        self.assertEqual({0: "SONYA"}, get_attribution_tag_evidence(entries, clean))
        dirty_entry = [{"type": "SPOKEN", "text": "Go" + JOINER + " now,"}]
        self.assertEqual({0: "SONYA"}, get_attribution_tag_evidence(dirty_entry, clean))

    def test_a_source_carrying_the_character_proves_the_shift(self):
        for source, narration in (
                ('\u201cYes,\u201d said Son' + SOFT + 'ya.', "said Sonya."),
                ('\u201cYes,\u201d said Sonya.', "said Son" + SOFT + "ya.")):
            with self.subTest(source=repr(source), narration=repr(narration)):
                frozen = [{"type": "SPOKEN", "text": "Yes,"},
                          {"type": "NARRATOR", "text": narration}]
                names = ("SONYA",)
                proofs = get_attribution_tag_proofs(frozen, source, names, {})[0]
                evidence = {index: proof["speaker"]
                            for index, proof in proofs.items()}
                snapshot = get_attribution_snapshot_digest([frozen, sorted(names), [], {}])
                result = get_exact_attribution_shift_repair(
                    frozen, [{"n": 0, "speaker": "NARRATOR"},
                             {"n": 1, "speaker": "SONYA"}],
                    source, proofs, evidence, snapshot,
                    established_names=names, speaker_aliases={})
                self.assertEqual(["SONYA", "NARRATOR"],
                                 [entry["speaker"] for entry in result["entries"]])
                self.assertIn("exact_adjacent_attribution_shift",
                              [change["code"] for change in
                               result["accepted_metadata"]["repairs"]])

    def test_a_printed_speaker_label_joins_the_word_in_both_directions(self):
        lines = ["Go now.", "Again.", "Third.", "Fourth.", "Fifth.", "Sixth."]
        labelled = "\n\n".join("Son" + SOFT + "ya \u201c" + line + "\u201d"
                               for line in lines) + "\n"
        clean = without_silent_characters(labelled)
        dirty_name = ("Son" + SOFT + "ya").upper()
        for source, names in ((labelled, ["SONYA"]), (clean, [dirty_name])):
            with self.subTest(source=repr(source), names=names):
                labels = speaker_labels(source, minimum_repeats=3,
                                        speaker_names=names)
                self.assertEqual(["Sonya"] * len(lines),
                                 [name for _pos, name in labels])
                self.assertTrue(uses_speaker_labels(source, speaker_names=names))

    def test_a_dirty_roster_spelling_folds_to_the_clean_one(self):
        entries = [{"speaker": "Son" + SOFT + "ya", "text": "Sonya left."}]
        result = stabilize_speaker_identities(entries, established_speakers=["SONYA"])
        self.assertEqual("SONYA", result["entries"][0]["speaker"])
        self.assertEqual({"type": "speaker_identity", "entry_number": 1,
                          "before": "Son" + SOFT + "ya", "after": "SONYA"},
                         result["changes"][0])

    def test_a_clean_name_is_not_rewritten_to_a_dirty_roster_spelling(self):
        entries = [{"speaker": "SONYA", "text": "Sonya left."}]
        result = stabilize_speaker_identities(
            entries, established_speakers=[("Son" + SOFT + "ya").upper()])
        self.assertEqual("SONYA", result["entries"][0]["speaker"])
        self.assertEqual([], result["changes"])

    def test_a_lone_dirty_name_is_stored_without_the_character(self):
        result = stabilize_speaker_identities(
            [{"speaker": "Son" + SOFT + "ya", "text": "Sonya left."}])
        self.assertEqual("Sonya", result["entries"][0]["speaker"])
        self.assertEqual("Son" + SOFT + "ya", result["changes"][0]["before"])

    def test_extended_person_name_and_group_alias_fold_both_sides(self):
        self.assertTrue(_is_extended_person_name("Son" + SOFT + "ya", "Sonya Smith"))
        self.assertTrue(_is_extended_person_name("Sonya", "Son" + SOFT + "ya Smith"))
        self.assertTrue(is_speaker_merge_allowed(
            "RAM AND RE" + JOINER + "M", "RAM AND REM"))
        self.assertTrue(is_speaker_merge_allowed(
            "RAM AND REM", "RAM AND RE" + JOINER + "M"))

    def test_the_roster_admits_one_canonical_name(self):
        source = "Sonya left. Sonya sat. Sonya read. " * 3
        entries = [{"speaker": "SONYA", "type": "SPOKEN", "text": "Sonya left."},
                   {"speaker": ("Son" + SOFT + "ya").upper(), "type": "SPOKEN",
                    "text": "Sonya sat."}]
        self.assertEqual(["SONYA"], three_pass_generate.build_roster(entries, source))

    def test_a_model_name_resolves_through_the_cast_to_the_same_speaker(self):
        named = three_pass_generate.get_named_from_answer(
            [{"type": "SPOKEN", "text": "Sonya left."}],
            [{"speaker": ("Son" + SOFT + "ya").upper(), "text": "Sonya left."}])
        self.assertEqual("SONYA", named[0]["speaker"])
        cast = three_pass_generate.get_cast_from_data(
            [{"name": "Son" + SOFT + "ya",
              "aliases": ["Son" + SOFT + "ya Bell"]}])
        self.assertEqual(["SONYA"], cast["names"])
        self.assertIn("SONYA BELL", cast["alias_to_name"])
        self.assertEqual("SONYA", cast["alias_to_name"]["SONYA BELL"])
        self.assertEqual("SONYA", three_pass_generate.get_cast_speaker(
            ("Son" + JOINER + "ya").upper(), cast))
        self.assertEqual("SONYA", three_pass_generate.get_cast_speaker(
            ("Son" + SOFT + "ya Bell").upper(), cast))

    def test_a_cast_name_that_is_only_invisible_characters_is_refused(self):
        with self.assertRaises(ValueError):
            three_pass_generate.get_cast_from_data(
                [{"name": "  " + SOFT + JOINER, "aliases": []}])

    def test_a_cast_lookup_without_a_hit_keeps_the_callers_spelling(self):
        # Only the lookup key is canonicalised (#2316): returning the upper
        # folded label here renamed "Narrator" to "NARRATOR" in books without
        # the characters and stored "SONYA" where the model wrote "Sonya".
        cast = three_pass_generate.get_cast_from_data(
            [{"name": "Sonya", "aliases": ["Miss" + SOFT + " Bell"]}])
        for speaker, expected in (
                ("Sonya", "Sonya"),
                ("Narrator", "Narrator"),
                ("So" + JOINER + "nya", "Sonya"),
                (("Son" + SOFT + "ya").lower(), "sonya"),
                ("Miss" + SOFT + " Bell", "SONYA"),
                ("m" + JOINER + "iss bell", "SONYA")):
            with self.subTest(speaker=repr(speaker)):
                self.assertEqual(expected,
                                 three_pass_generate.get_cast_speaker(speaker, cast))
        self.assertEqual("Sonya", three_pass_generate.get_cast_speaker(
            "Son" + SOFT + "ya", {"alias_to_name": {}}))

    def test_an_invisible_only_speaker_cannot_be_stored(self):
        # The gate, the cast fold and the stored label are one chain: the fold
        # alone would store "" (#2316), so the gate must refuse the name.
        source = "Someone read the letter. Nobody named here."
        frozen = [{"type": "SPOKEN", "text": "Someone read the letter."}]
        for name in (SOFT, JOINER, SOFT + JOINER):
            with self.subTest(name=repr(name)):
                self.assertFalse(is_attested_name(name, source))
        # An actually empty name keeps its old no-op meaning.
        self.assertTrue(is_attested_name("", source))
        report = validate_attribution(
            frozen, [{"n": 0, "speaker": SOFT}], source)
        self.assertFalse(report["passed"])
        self.assertEqual(["speaker_not_in_source"],
                         [finding["code"] for finding in report["findings"]])
        self.assertEqual("", three_pass_generate.get_cast_speaker(SOFT, {}))

    def test_the_narrator_canonicaliser_folds_into_one_roster_name(self):
        dirty = "Son" + SOFT + "ya"
        self.assertEqual("SONYA", normalize_narrator_name(dirty))
        self.assertEqual("SONYA", get_valid_narrator_name(" " + dirty + " "))
        self.assertIsNone(get_valid_narrator_name(" " + SOFT + " "))
        source = "Sonya left. Sonya sat. Sonya read. " * 3
        roster = three_pass_generate.build_roster(
            [{"speaker": dirty.upper(), "type": "SPOKEN", "text": "Sonya left."}],
            source)
        self.assertEqual(["SONYA"], roster)
        # get_attribution_roster inserts the narrator only if it is missing, so
        # the folded canonicaliser keeps it at one row.
        self.assertIn(normalize_narrator_name(dirty), roster)


class SourceComparisonConsumerTests(unittest.TestCase):
    def test_nickname_words_join_the_word(self):
        self.assertEqual(["sonya"], _name_tokens("Son" + SOFT + "ya"))
        speakers, _samples, cooccur = collect_context([
            {"speaker": "SONYA", "text": "Son" + SOFT + "ya met Rem."},
            {"speaker": "REM", "text": "Son" + SOFT + "ya met Rem."}])
        self.assertEqual(["REM", "SONYA"], speakers)
        self.assertEqual(["Son" + SOFT + "ya met Rem."], cooccur)

    def test_narrator_attestation_joins_the_word_in_both_directions(self):
        source = "SONYA " * 4
        self.assertTrue(is_narrator_attested("Sonya", ("Son" + SOFT + "ya ") * 4))
        self.assertTrue(is_narrator_attested("Son" + SOFT + "ya", source))

    def test_boundary_overlap_joins_the_word_in_both_directions(self):
        right = [{"text": "Sonya read books today"}]
        for left in ([{"text": "we saw Sonya read books"}],
                     [{"text": "we saw Son" + SOFT + "ya read books"}]):
            with self.subTest(left=left):
                self.assertEqual(["sonya", "read", "books"],
                                 _get_boundary_overlap(left, right))
        dirty_right = [{"text": "Son" + JOINER + "ya read books today"}]
        self.assertEqual(["sonya", "read", "books"],
                         _get_boundary_overlap(
                             [{"text": "we saw Sonya read books"}], dirty_right))

    def test_a_duplicate_block_is_found_when_one_copy_has_the_character(self):
        unit = ["alpha bravo charlie delta", "echo foxtrot golf hotel",
                "india juliet kilo lima", "mike november oscar papa",
                "quebec romeo sierra tango"]
        dirty = [line.replace("charlie", "char" + SOFT + "lie") for line in unit]
        source = " ".join(unit) + " narration " + " ".join(dirty)
        findings = find_adjacent_duplicate_blocks(unit + dirty, source)
        self.assertEqual([[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]],
                         [finding["entry_numbers"] for finding in findings])

    def test_text_diff_words_join_the_word_through_an_en_dash(self):
        self.assertEqual(["ab", "c"], get_comparison_words("a" + SOFT + "\u2013b c"))
        self.assertEqual(["ab", "c"], get_comparison_words("a\u2013b c"))
        totals = word_diff("They met Son" + SOFT + "ya at the hall.",
                           [{"text": "They met Sonya at the hall."}])["totals"]
        self.assertEqual(0, totals["deleted"] + totals["inserted"] + totals["replaced"])

    def test_pronunciation_matches_raw_chunk_text_from_the_editor_preview(self):
        spoken, applied = get_pronunciation_result(
            "Son" + SOFT + "ya sat", {"Sonya": "Sohn-yah"})
        self.assertEqual("Sohn-yah sat", spoken)
        self.assertEqual([{"name": "Sonya", "spoken": "Sohn-yah"}], applied)

    def test_word_timing_joins_the_word_and_keeps_raw_offsets(self):
        text = "Son" + SOFT + "ya paid five fifty."
        self.assertEqual(
            [("sonya", 0, 6), ("paid", 7, 11), ("five", 12, 16), ("fifty", 17, 22)],
            get_word_tokens(text))
        self.assertEqual("Son" + SOFT + "ya", text[0:6])
        self.assertEqual(["sonya", "paid", "five", "fifty"],
                         get_comparison_tokens("Son" + SOFT + "ya paid five fifty."))
        self.assertEqual(get_comparison_tokens("Sonya paid five fifty."),
                         get_comparison_tokens("Son" + SOFT + "ya paid five fifty."))

    def test_epub_and_script_words_match_when_only_one_side_has_the_character(self):
        cleaned_script = epub_readaloud.get_normalized_words(
            normalize_for_speech("Son" + SOFT + "ya paid five fifty."))
        self.assertEqual(["sonya", "paid", "five", "fifty"],
                         epub_readaloud.get_spoken_words(
                             "Son" + SOFT + "ya paid five fifty."))
        self.assertEqual(epub_readaloud.get_spoken_words("Sonya paid five fifty."),
                         cleaned_script)

    def test_aligner_stream_and_token_key_fold_the_character(self):
        self.assertEqual(
            qwen_align.get_normalized_stream("Sonya paid five fifty.")[0],
            qwen_align.get_normalized_stream("Son" + SOFT + "ya paid five fifty.")[0])
        self.assertEqual(qwen_align.get_token_key("Sonya"),
                         qwen_align.get_token_key("Son" + SOFT + "ya"))

    def test_an_attached_dialogue_tag_folds_the_printed_name(self):
        # dialogue_continuity compares the printed narration against a roster
        # label that is already clean (#2316).
        self.assertTrue(_is_attached_tag("Sonya said, quietly.", "SONYA"))
        self.assertTrue(_is_attached_tag("Son" + SOFT + "ya said, quietly.",
                                         "SONYA"))
        self.assertTrue(_is_attached_tag("Sonya said, quietly.",
                                         "Son" + JOINER + "ya"))
        self.assertFalse(_is_attached_tag("Son" + SOFT + "ya said, quietly.",
                                          "BRENDA"))

    def test_the_aligner_unit_count_does_not_split_the_word(self):
        # count_units plans a window from spoken words; an invisible character
        # is not spoken and must not add a unit (#2316).
        self.assertEqual(2, qwen_align.count_units("won" + SOFT + "derful day"))
        self.assertEqual(qwen_align.count_units("wonderful day"),
                         qwen_align.count_units(
                             "won" + SOFT + "der" + SOFT + "ful day"))

    def test_a_gold_answer_folds_against_the_passage_and_the_book(self):
        row = {"passage_before": "Then", "line": "Son" + SOFT + "ya spoke.",
               "passage_after": "and left."}
        self.assertEqual("window", support_for("Sonya", row))
        self.assertEqual("window", support_for("Son" + SOFT + "ya", row))
        self.assertEqual("book", support_for("Brenda", row, "Brenda waved."))
        self.assertEqual("book", support_for("Bren" + JOINER + "da", row,
                                             "Brenda waved."))
        self.assertEqual("not_a_name", support_for("NARR" + SOFT + "ATOR", row))

    def test_a_candidate_name_folds_the_text_and_the_roster(self):
        self.assertEqual(["SONYA"],
                         candidates._names_in("Son" + SOFT + "ya spoke.", ["SONYA"]))
        self.assertEqual(["SONYA"],
                         candidates._names_in(
                             "Sonya spoke.", [("Son" + SOFT + "ya").upper()]))
        segmented = [{"type": "NARRATOR",
                      "text": "Son" + SOFT + "ya said, quietly."},
                     {"type": "SPOKEN", "text": "Hi."},
                     {"type": "NARRATOR", "text": "Silence."}]
        self.assertEqual(["SONYA"],
                         candidates.tag_candidates(segmented, 1, ["SONYA"]))

    def test_a_cyrillic_source_word_split_by_the_character_stays_faithful(self):
        # script_repair compares entry words against _normalize(source_text),
        # which now folds (#2316); a faithful mixed-script word must not be
        # reported as unsupported corruption.
        source = "He said \u0441" + SOFT + "\u043e. Then he left."
        result = script_repair.build_deterministic_repair(
            [{"type": "NARRATOR", "text": "He said \u0441\u043e."}], source)
        self.assertEqual([], result["changes"])
        self.assertEqual([], result["unresolved"])

    def test_a_printed_label_may_start_with_an_invisible_character(self):
        lines = ["One.", "Two.", "Three.", "Four.", "Five."]
        labelled = "\n\n".join(SOFT + "Son" + SOFT + "ya \u201c" + line + "\u201d"
                               for line in lines) + "\n"
        labels = speaker_labels(labelled, minimum_repeats=3,
                                speaker_names=["SONYA"])
        self.assertEqual(["Sonya"] * len(lines), [name for _pos, name in labels])

    def test_an_approved_occurrence_rule_goes_stale_loudly(self):
        # Preparation now deletes the characters, so a rule approved against the
        # older prepared string is reported stale (never re-anchored by search):
        # the render keeps the ordinary pronunciation and records the decision.
        original = "Son" + SOFT + "ya"
        prepared_before = original + " spoke here."
        prepared_now = "Sonya spoke here."
        text = "Sonya spoke here."
        rule = {"id": "rule-1", "state": "approved",
                "coordinate_space": "pre_lexicon_v1",
                "offset_unit": "unicode_codepoint", "uid": "chunk-1",
                "text_sha256": get_pronunciation_text_digest(text),
                "prepared_sha256": get_pronunciation_text_digest(prepared_before),
                "start": 0, "end": len(original), "original": original,
                "replacement": "Sone-ya", "phonetic_hint": None,
                "fallback": None, "restrictions": {}}
        with self.assertRaises(OccurrenceAnchorConflict) as caught:
            validate_occurrence_pronunciation_rules(
                [rule], "chunk-1", text, prepared_now)
        self.assertIn("stale_preparation", str(caught.exception))
        result = get_occurrence_pronunciation_resolution(
            prepared_now, {}, rules=[rule], uid="chunk-1",
            text_digest=get_pronunciation_text_digest(text))
        self.assertEqual([{"id": "rule-1", "applied": False,
                           "reason": "stale_preparation"}], result["decisions"])
        self.assertEqual(prepared_now, result["text"])


class BooksWithoutTheCharactersTests(unittest.TestCase):
    """Every changed function keeps its pre-fix result on text without them."""

    def test_no_changed_function_moves_for_a_book_without_the_characters(self):
        source = '"Sonya left," Sonya said. Sonya sat. Sonya read.'
        entries = [{"type": "SPOKEN", "text": "Sonya left,"}]
        self.assertTrue(is_attested_name("SONYA", source))
        self.assertFalse(is_attested_name("BRENDA", source))
        self.assertEqual({0: "SONYA"}, get_attribution_tag_evidence(entries, source))
        self.assertEqual("SONYA", get_attribution_tag_proofs(
            entries, source, ["SONYA"])[0][0]["speaker"])
        self.assertEqual(["sonya", "smith"], _name_tokens("Sonya Smith"))
        self.assertEqual(["REM", "SONYA"], collect_context([
            {"speaker": "SONYA", "text": "Sonya met Rem."},
            {"speaker": "REM", "text": "Sonya met Rem."}])[0])
        self.assertTrue(is_narrator_attested("Sonya", "Sonya " * 4))
        self.assertEqual(["sonya", "read", "books"], _get_boundary_overlap(
            [{"text": "we saw Sonya read books"}], [{"text": "Sonya read books today"}]))
        self.assertEqual(["ab", "c"], get_comparison_words("a\u2013b c"))
        self.assertEqual(0, word_diff("Sonya sat.", [{"text": "Sonya sat."}])
                         ["totals"]["replaced"])
        unit = ["alpha bravo charlie delta", "echo foxtrot golf hotel",
                "india juliet kilo lima", "mike november oscar papa",
                "quebec romeo sierra tango"]
        self.assertEqual([[1, 2, 3, 4, 5, 6, 7, 8, 9, 10]],
                         [finding["entry_numbers"] for finding in
                          find_adjacent_duplicate_blocks(
                              unit + unit, " ".join(unit) + " narration " +
                              " ".join(unit))])
        self.assertEqual("Sonya", stabilize_speaker_identities(
            [{"speaker": "Sonya", "text": "Sonya left."}])["entries"][0]["speaker"])
        self.assertTrue(_is_extended_person_name("Sonya", "Sonya Smith"))
        self.assertTrue(is_speaker_merge_allowed("RAM AND REM", "RAM AND REM"))
        self.assertEqual(("Sohn-yah sat", [{"name": "Sonya", "spoken": "Sohn-yah"}]),
                         get_pronunciation_result("Sonya sat", {"Sonya": "Sohn-yah"}))
        self.assertEqual([("sonya", 0, 5), ("paid", 6, 10)],
                         get_word_tokens("Sonya paid."))
        self.assertEqual(["sonya", "paid"], get_comparison_tokens("Sonya paid."))
        self.assertEqual(["sonya", "paid"],
                         epub_readaloud.get_spoken_words("Sonya paid."))
        self.assertEqual("sonyapaid",
                         qwen_align.get_normalized_stream("Sonya paid")[0])
        self.assertEqual(["SONYA"], three_pass_generate.build_roster(
            [{"speaker": "SONYA", "type": "SPOKEN", "text": "Sonya left."}],
            "Sonya left. Sonya sat. Sonya read."))
        self.assertEqual(["Sonya"] * 3, [name for _pos, name in speaker_labels(
            "Sonya \u201cOne.\u201d\n\nSonya \u201cTwo.\u201d\n\nSonya \u201cThree.\u201d\n",
            minimum_repeats=3, speaker_names=["SONYA"])])
        self.assertEqual("Sonya", three_pass_generate.get_named_from_answer(
            [{"type": "SPOKEN", "text": "Sonya left."}],
            [{"speaker": "SONYA", "text": "Sonya left."}])[0]["speaker"].title())
        # Repair 2 additions: the spelling-preserving cast fold, the narrator
        # canonicaliser, the fold-before-NFC tokenizers, the dialogue tag, the
        # aligner unit count and the three gold/candidate comparisons.
        self.assertEqual("Sonya", three_pass_generate.get_cast_speaker(
            "Sonya", {"alias_to_name": {}}))
        self.assertEqual("Narrator", three_pass_generate.get_cast_speaker(
            "Narrator", None))
        self.assertEqual("SONYA", normalize_narrator_name("Sonya"))
        self.assertEqual("SONYA", get_valid_narrator_name("  Sonya "))
        self.assertEqual(["sonya"], tokens("Sonya"))
        # script_preflight now NFC-normalizes, so its identity case is text
        # that is already NFC (every fixture string is); decomposed text is
        # covered by the combining-mark test above.
        self.assertEqual(("sonya", "and", "rem"),
                         get_normalized_word_tokens("Sonya and Rem"))
        self.assertEqual([("é", 0, 1)], list(iter_literal_words("\u00e9")))
        self.assertTrue(_is_attached_tag("Sonya said, quietly.", "SONYA"))
        self.assertEqual(2, qwen_align.count_units("wonderful day"))
        self.assertEqual("window", support_for(
            "Sonya", {"passage_before": "Then", "line": "Sonya spoke.",
                      "passage_after": "and left."}))
        self.assertEqual(["SONYA"], candidates._names_in("Sonya spoke.", ["SONYA"]))
        self.assertEqual(["SONYA"], candidates.tag_candidates(
            [{"type": "NARRATOR", "text": "Sonya said, quietly."},
             {"type": "SPOKEN", "text": "Hi."},
             {"type": "NARRATOR", "text": "Silence."}], 1, ["SONYA"]))
        self.assertEqual([], script_repair.build_deterministic_repair(
            [{"type": "NARRATOR", "text": "He said so."}],
            "He said so. Then he left.")["unresolved"])


if __name__ == "__main__":
    unittest.main()
