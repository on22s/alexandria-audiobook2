"""Issue #611: a paragraph longer than the chunk size was split at sentence
ends, and a sentence end inside a quotation put half the quotation in the
next chunk. Cuts now fall only at newlines or sentence ends outside a spoken
span; where no such cut exists the old space split applies and the record
says so."""
import unittest

from generate_script import split_into_chunk_records, _safe_cut_points

LINES = [
    'However, Ian would become furious and exclaim, “My dream is not to be a scholar! My dream is to become a magic knight!”',
    'After making such an unrealistic claim, he would end up getting countless laughs from the other students in response.',
    '“Grade E? E, huh? How dare such an inferior being stand in the same line as one as superior as myself?! It’s funny—no, it’s utterly hilarious!”',
    'As expected, Tristan Humphrey, the vain blond aristocrat, made a gruesome mockery of my terrible score.',
    'The lines he said were funny, sure, but the one saying them was undoubtedly stupid.',
    'Bastard.',
    '“Excuse me!”',
    'At that time, the second seat, Kaya Astrea, suddenly raised her hand and shouted.',
    'What is it this time? I don’t think I’ve ever seen an event like this happening in the game.',
    '“Have there ever been any cases where an error occurs in the process of evaluating the amount of mana? For example, a case where the grade is measured incorrectly…”',
    '“What?”',
    'Both Professor Fernando and the students responded to her question as if they couldn’t understand.',
]


def balanced(text):
    return text.count("“") == text.count("”")


class QuoteSafeCuts(unittest.TestCase):
    def test_a_one_paragraph_per_line_book_is_cut_between_lines_never_inside_a_quotation(self):
        text = "\n".join(LINES)                      # no blank line anywhere: one "paragraph"
        records = split_into_chunk_records(text, 500)
        self.assertGreater(len(records), 1)
        for r in records:
            self.assertTrue(balanced(r["text"]), r["text"][:80])
            self.assertLessEqual(len(r["text"]), 500)
            self.assertNotIn("cut_inside_quote", r)
        self.assertEqual(" ".join(text.split()), " ".join(" ".join(r["text"] for r in records).split()))

    def test_a_sentence_end_inside_a_quotation_is_not_a_cut_point(self):
        piece = 'He said, “Stop! Now.” Then he left. “Fine.”'
        cuts = _safe_cut_points(piece)
        self.assertNotIn(piece.index("! ") + 1, cuts)      # inside the first quotation
        self.assertIn(piece.index("” Then") + 1, cuts)     # right after it closes
        self.assertIn(piece.index(". “Fine") + 1, cuts)

    def test_a_paragraph_with_no_newline_still_cuts_at_sentence_ends(self):
        text = " ".join(["The road went on and on toward the hills."] * 40)
        records = split_into_chunk_records(text, 300)
        self.assertTrue(all(len(r["text"]) <= 300 for r in records))
        self.assertTrue(all(r["text"].endswith(".") for r in records))
        self.assertTrue(all("cut_inside_quote" not in r for r in records))

    def test_a_quotation_longer_than_the_chunk_falls_back_and_is_flagged(self):
        text = "“" + " ".join(["word"] * 300) + "”"      # ~1,500 chars, one quotation
        records = split_into_chunk_records(text, 400)
        self.assertGreater(len(records), 1)
        self.assertTrue(any(r.get("cut_inside_quote") for r in records))
        self.assertTrue(all(len(r["text"]) <= 400 for r in records))

    def test_a_long_weak_dialogue_line_is_not_cut_inside_when_the_book_says_dash(self):
        """#2447 gate 3 blocker 1: a `--` book's long line is a quote to protect.

        The whole-book question (`detect_convention` + the weak evidence) is
        asked once and passed down. Without it (`weak_dash` False) the
        sentence ends inside the line become cut points and the record is
        split between them; with it the line is one spoken span and the
        fallback space split marks the record `cut_inside_quote`.
        """
        intro = "\n\n".join([
            "-- Where are you going now?", "-- Come back to me.",
            "-- Why did you leave?", "-- I am sorry.",
            "-- Come back to me."])
        long_line = "-- " + " ".join(
            ["The road went on toward the hills."] * 120)
        records = split_into_chunk_records(intro + "\n\n" + long_line + "\n",
                                           3000)
        self.assertTrue(any(r.get("cut_inside_quote") for r in records),
                        [r["text"][:40] for r in records])

    def test_a_long_weak_narration_line_in_an_em_dash_book_still_cuts(self):
        """#2447 gate 3 blocker 1: an em-dash book's `--` line is narration.

        The em dashes make the book `dash_lines`, but the book does not run
        `--` dialogue, so the long `--`-opened narration line is not a spoken
        span and its sentence ends stay safe cut points. Certifying it (a
        `weak_dash` forced True) would swallow those cuts and flag the record.
        """
        em = "\n\n".join("—¿Vienes mañana? —preguntó Juan." for _ in range(6))
        long_line = "-- " + " ".join(
            ["The road went on toward the hills."] * 120)
        records = split_into_chunk_records(em + "\n\n" + long_line + "\n", 3000)
        self.assertTrue(records)
        self.assertFalse(any(r.get("cut_inside_quote") for r in records),
                         [r["text"][:40] for r in records])

    def test_continuation_metadata_still_links_the_pieces(self):
        records = split_into_chunk_records("\n".join(LINES), 500)
        self.assertFalse(records[0]["continues_paragraph_from_previous"])
        self.assertTrue(records[0]["continues_paragraph_to_next"])
        self.assertTrue(records[-1]["continues_paragraph_from_previous"])
        self.assertFalse(records[-1]["continues_paragraph_to_next"])


if __name__ == "__main__":
    unittest.main()
