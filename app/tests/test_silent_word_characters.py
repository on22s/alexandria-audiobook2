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
from silent_word_characters import remove_silent_word_characters
from recall_core import tokens
from chunk_quality import validate_chunk_quality
from pass_quality import validate_segment_quality
from literal_fidelity import audit_literal_words, iter_literal_words
from script_preflight import get_normalized_word_tokens
from dialogue_spans import mark_entries
from speech_text import get_speech_normalization, normalize_for_speech
from generate_personas import _resolve_to_canonical, normalize_speaker_name
from pronunciation import apply_pronunciation
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


if __name__ == "__main__":
    unittest.main()
