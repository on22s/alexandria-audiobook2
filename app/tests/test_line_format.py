"""The compact codec must not lose or invent an entry.

generate_script's gate compares the concatenated entry text against the source
chunk, so a codec that silently drops a line would show up as a recall failure
blamed on the model. Each test below pins one way that could happen quietly.
"""
import json
import unittest

import line_format
from chunk_quality import validate_chunk_quality

ENTRIES = [
    {"speaker": "NARRATOR",
     "text": "The room had gone cold. Elena stood by the window.",
     "instruct": "Quiet, tense narration."},
    {"speaker": "ELENA", "text": "Tell me the truth.",
     "instruct": "Firm quiet authority, low and controlled."},
    {"speaker": "MARCUS", "text": "There is nothing to tell.",
     "instruct": "Defensive evasion, flat and guarded."},
]
SOURCE = " ".join(e["text"] for e in ENTRIES)


class LineFormatRoundTrip(unittest.TestCase):
    def test_round_trip_is_exact(self):
        self.assertEqual(
            line_format.parse_entries(line_format.format_entries(ENTRIES)), ENTRIES)

    def test_delimiter_inside_text_survives(self):
        """text is last and split uses maxsplit=2, so a '|' is just prose."""
        entries = [{"speaker": "NARRATOR", "text": "a | b | c", "instruct": "Flat."}]
        self.assertEqual(
            line_format.parse_entries(line_format.format_entries(entries)), entries)

    def test_newline_inside_text_survives(self):
        entries = [{"speaker": "NARRATOR", "text": "one\ntwo", "instruct": "Flat."}]
        round_tripped = line_format.parse_entries(line_format.format_entries(entries))
        self.assertEqual(round_tripped, entries)
        self.assertEqual(len(line_format.format_entries(entries).splitlines()), 1)

    def test_carriage_returns_and_literal_escapes_round_trip_through_both_parsers(self):
        from response_codecs import get_codec
        for text in ("one\rtwo", "one\r\ntwo", r"literal\r and \n and \backslash"):
            with self.subTest(text=text):
                entries = [{"speaker": "NARRATOR", "text": text, "instruct": "Flat."}]
                encoded = line_format.format_entries(entries)
                self.assertEqual(1, len(encoded.splitlines()))
                self.assertEqual(entries, line_format.parse_entries(encoded))
                self.assertEqual((entries, []), line_format.salvage_entries(encoded))
                self.assertEqual(entries, get_codec("lines").parse(encoded))
                self.assertEqual(text, entries[0]["text"])

    def test_backslash_is_not_eaten(self):
        entries = [{"speaker": "NARRATOR", "text": r"a\nb", "instruct": "Flat."}]
        self.assertEqual(
            line_format.parse_entries(line_format.format_entries(entries)), entries)


class LineFormatFailsLoud(unittest.TestCase):
    def test_delimiters_in_metadata_are_rejected_before_serialization(self):
        for field in ('speaker', 'instruct'):
            with self.subTest(field=field):
                entries = [{"speaker": "NARRATOR", "instruct": "Calm.", "text": "a | b"}]
                entries[0][field] = "first|second"
                with self.assertRaisesRegex(line_format.LineFormatError, field):
                    line_format.format_entries(entries)
                self.assertEqual("first|second", entries[0][field])

    def test_missing_field_raises(self):
        with self.assertRaises(line_format.LineFormatError):
            line_format.parse_entries("NARRATOR|only two fields")

    def test_empty_speaker_raises(self):
        with self.assertRaises(line_format.LineFormatError):
            line_format.parse_entries("|Flat.|Some text.")

    def test_salvage_reports_what_it_skipped(self):
        text = "\n".join(["NARRATOR|Flat.|Good line.",
                          "BROKEN LINE WITH NO DELIMITER",
                          "ELENA|Firm.|Another good line."])
        entries, skipped = line_format.salvage_entries(text)
        self.assertEqual(len(entries), 2)
        self.assertEqual(len(skipped), 1)
        self.assertIn("BROKEN", skipped[0]["line"])

    def test_salvage_does_not_silently_shorten(self):
        """A dropped line must be visible, never just a shorter list."""
        entries, skipped = line_format.salvage_entries("NARRATOR|only two fields")
        self.assertEqual(entries, [])
        self.assertTrue(skipped)


class WhitespaceNormalisation(unittest.TestCase):
    """Found in real data: 6 of 3,813 entries carry a trailing space in
    `instruct`. The codec normalises the labels and must NEVER touch `text`,
    which is the field the quality gate compares against the source."""

    def test_text_is_byte_exact_including_edge_whitespace(self):
        entries = [{"speaker": "NARRATOR", "text": "  padded text  ",
                    "instruct": "Flat."}]
        restored = line_format.parse_entries(line_format.format_entries(entries))
        self.assertEqual(restored[0]["text"], "  padded text  ")

    def test_label_whitespace_is_normalised(self):
        entries = [{"speaker": " NARRATOR ", "text": "Some text.",
                    "instruct": "Flat. "}]
        restored = line_format.parse_entries(line_format.format_entries(entries))
        self.assertEqual(restored[0]["speaker"], "NARRATOR")
        self.assertEqual(restored[0]["instruct"], "Flat.")


class GateIsUnaffected(unittest.TestCase):
    def test_quality_metrics_identical_via_either_codec(self):
        """Same entries, two encodings: the gate must not be able to tell."""
        from_json = json.loads(json.dumps(ENTRIES))
        from_lines = line_format.parse_entries(line_format.format_entries(ENTRIES))
        self.assertEqual(validate_chunk_quality(SOURCE, from_json),
                         validate_chunk_quality(SOURCE, from_lines))


if __name__ == "__main__":
    unittest.main()
