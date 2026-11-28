"""Regression tests for #2397: mark_entries must treat text=None as empty."""

import unittest
from unittest.mock import patch

import dialogue_spans
from dialogue_spans import apply_dialogue_map, mark_entries


class MarkEntriesNoneTextTest(unittest.TestCase):
    SOURCE = (
        '“None of us moved.”\n\n'
        '“Line 1.”\n\n'
        '“Line 2.”\n\n'
        '“Line 3.”\n\n'
        '“Line 4.”\n\n'
        '“Line 5.”'
    )

    def test_issue_repro_leaves_none_text_unlocated(self):
        marked = mark_entries(
            [{"speaker": None, "text": None}],
            self.SOURCE,
            "paired_quotes",
        )
        self.assertEqual(marked, [{"speaker": None, "text": None}])

        # With corroborated printed speaker labels, no source_speaker is stamped either.
        labeled_source = (
            'Alice “None of us moved.”\n'
            'Alice “Line 1.”\n'
            'Alice “Line 2.”\n'
            'Alice “Line 3.”\n'
            'Alice “Line 4.”\n'
        )
        labeled = mark_entries(
            [{"speaker": "ALICE", "text": None}],
            labeled_source,
            "paired_quotes",
            speaker_names=("Alice", "ALICE"),
        )
        self.assertEqual(labeled, [{"speaker": "ALICE", "text": None}])

    def test_none_text_matches_missing_key_and_empty_string(self):
        source = (
            'Alice “None of us moved.”\n'
            'Alice “Line 1.”\n'
            'Alice “Line 2.”\n'
            'Alice “Line 3.”\n'
            'Alice “Line 4.”\n'
            'Alice “Line 5.”\n'
        )
        variants = (
            [{"speaker": "NARRATOR", "text": None}, {"speaker": "ALICE", "text": "Line 1."}],
            [{"speaker": "NARRATOR"}, {"speaker": "ALICE", "text": "Line 1."}],
            [{"speaker": "NARRATOR", "text": ""}, {"speaker": "ALICE", "text": "Line 1."}],
        )
        results = [
            mark_entries(entries, source, "paired_quotes", speaker_names=["Alice"])
            for entries in variants
        ]
        # The non-text fields of entry 0 and the entire entry 1 must be identical.
        first_without_text = [
            {key: value for key, value in result[0].items() if key != "text"}
            for result in results
        ]
        self.assertEqual(first_without_text[0], first_without_text[1])
        self.assertEqual(first_without_text[0], first_without_text[2])
        self.assertEqual(first_without_text[0], {"speaker": "NARRATOR"})
        self.assertEqual(results[0][1], results[1][1])
        self.assertEqual(results[0][1], results[2][1])

    def test_none_text_between_real_entries_does_not_advance_cursor_or_shift_spans(self):
        source = (
            '“First.”\n\n'
            '“Repeat.”\n\n'
            '“None of us moved.”\n\n'
            '“Repeat.”\n\n'
            '“Line 4.”\n\n'
            '“Line 5.”'
        )
        with_none = mark_entries(
            [
                {"speaker": "A", "text": "First."},
                {"speaker": None, "text": None},
                {"speaker": "B", "text": "Repeat."},
            ],
            source,
            "paired_quotes",
        )
        without_none = mark_entries(
            [
                {"speaker": "A", "text": "First."},
                {"speaker": "B", "text": "Repeat."},
            ],
            source,
            "paired_quotes",
        )
        self.assertEqual(with_none[0], without_none[0])
        self.assertEqual(with_none[1], {"speaker": None, "text": None})
        self.assertEqual(with_none[2], without_none[1])
        first_repeat_start = source.index("Repeat.")
        self.assertEqual(
            with_none[2]["source_span"],
            [first_repeat_start, first_repeat_start + len("Repeat.")],
        )

    def test_literal_none_string_still_locates(self):
        marked = mark_entries(
            [{"speaker": "A", "text": "None"}],
            self.SOURCE,
            "paired_quotes",
        )
        self.assertEqual(
            marked,
            [{"speaker": "A", "text": "None", "spoken": True, "source_span": [1, 5]}],
        )

    def test_fallback_match_positions_does_not_index_none_text(self):
        # This asserts on the needles passed to the fallback matcher because
        # that is the only available discriminator: reverting the fallback-site
        # _entry_needle call changed no output for any input the gate tried, so
        # an output-only assertion could not catch it.
        captured_needles = []
        original = dialogue_spans.get_source_match_positions

        def capture(source, needles):
            materialized = list(needles)
            captured_needles.append(materialized)
            return original(source, materialized)

        with patch("dialogue_spans.get_source_match_positions", side_effect=capture):
            marked = mark_entries(
                [
                    {"speaker": "A", "text": "Line 2."},
                    {"speaker": "B", "text": "Line 1."},
                    {"speaker": None, "text": None},
                ],
                self.SOURCE,
                "paired_quotes",
            )
        self.assertEqual(len(captured_needles), 1)
        self.assertNotIn("None", captured_needles[0])
        self.assertEqual(marked[2], {"speaker": None, "text": None})
        self.assertIn("source_span", marked[0])
        self.assertIn("source_span", marked[1])

    def test_fallback_match_positions_still_locates_literal_none_string(self):
        captured_needles = []
        original = dialogue_spans.get_source_match_positions

        def capture(source, needles):
            materialized = list(needles)
            captured_needles.append(materialized)
            return original(source, materialized)

        with patch("dialogue_spans.get_source_match_positions", side_effect=capture):
            marked = mark_entries(
                [
                    {"speaker": "A", "text": "Line 2."},
                    {"speaker": "B", "text": "Line 1."},
                    {"speaker": "C", "text": "None"},
                ],
                self.SOURCE,
                "paired_quotes",
            )
        self.assertEqual(len(captured_needles), 1)
        self.assertIn("None", captured_needles[0])
        self.assertEqual(
            marked[2],
            {"speaker": "C", "text": "None", "spoken": True, "source_span": [1, 5]},
        )

    def test_apply_dialogue_map_does_not_locate_none_text_as_narration(self):
        source = (
            'None of them spoke.\n\n'
            '“Line 1.”\n\n'
            '“Line 2.”\n\n'
            '“Line 3.”\n\n'
            '“Line 4.”\n\n'
            '“Line 5.”'
        )
        result = apply_dialogue_map(
            [
                {"speaker": "ALICE", "text": None},
                {"speaker": "ALICE", "text": "Line 1."},
            ],
            source,
        )
        self.assertEqual(result["convention"], "paired_quotes")
        self.assertEqual(result["located"], 1)
        self.assertEqual(result["spoken"], 1)
        self.assertEqual(result["narration_changes"], [])
        self.assertEqual(result["entries"][0], {"speaker": "ALICE", "text": None})


if __name__ == "__main__":
    unittest.main()
