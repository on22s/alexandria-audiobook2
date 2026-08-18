"""The dialogue map is built from the source, not from what the model returned.

Generation is told to drop the outermost dialogue quotes, and follows that
unevenly - 22%, 16% and 1% retention across three books - so the fact that a
line was speech is destroyed rather than recorded. Rebuilding it afterwards
from the model's own prose is what made today's attribution scorer wrong three
times. The source still has the marks: arc4_volume10wn carries 6,925 of them
while its generated entries kept 16.
"""
import unittest

from dialogue_spans import (apply_source_speakers, detect_convention, mark_entries,
                            speaker_labels, spoken_spans, uses_speaker_labels)


class ConventionTest(unittest.TestCase):
    def test_curly_quotes_are_recognised(self):
        text = "\n".join(f"He paused. “Line number {i}, spoken aloud.”" for i in range(6))
        self.assertEqual("paired_quotes", detect_convention(text))

    def test_corner_brackets_are_recognised(self):
        text = "\n".join(f"「セリフ{i}です」と言った。" for i in range(6))
        self.assertEqual("paired_quotes", detect_convention(text))

    def test_an_em_dash_convention_is_recognised(self):
        """Quote marks are one tool. Continental typography opens a line with
        a dash and no quotes at all."""
        text = "\n".join(f"— Line number {i}, spoken aloud." for i in range(6))
        self.assertEqual("dash_lines", detect_convention(text))

    def test_a_script_style_label_is_recognised(self):
        text = "\n".join(f"ANNA: line number {i} spoken aloud." for i in range(6))
        self.assertEqual("label_lines", detect_convention(text))

    def test_a_text_with_no_convention_says_so(self):
        # "None" is a third answer: an empty map would read as "no dialogue",
        # which is the mistake that recorded a 6,925-quote book as unmarked.
        self.assertIsNone(detect_convention("Plain narration with no speech at all. " * 20))


class SpanTest(unittest.TestCase):
    # The convention is passed explicitly in these: detection needs five
    # markers before it will name one, deliberately, so a three-line fixture
    # has no convention to find. The unit under test here is the span maths.
    def test_spans_cover_the_words_and_not_the_marks(self):
        text = 'She waited. “Get down!” he shouted.'
        (start, end), = spoken_spans(text, "paired_quotes")
        self.assertEqual("Get down!", text[start:end])

    def test_narration_between_two_quotes_is_not_included(self):
        text = '“First line.” Marcus pulled on his coat. “Second line.”'
        spans = spoken_spans(text, "paired_quotes")
        self.assertEqual(["First line.", "Second line."],
                         [text[a:b] for a, b in spans])

    def test_an_unmatched_straight_quote_does_not_swallow_the_book(self):
        text = '"Only one mark here, and then a great deal of narration. ' + "x " * 500
        self.assertEqual([], spoken_spans(text, "paired_quotes"))


class MarkEntriesTest(unittest.TestCase):
    SOURCE = ('Petra looked over. “Is there some problem, Subaru?”\n\n'
              'He said nothing for a moment, thinking it over carefully.\n\n'
              '“Feel like a dad,” he admitted at last.\n')

    def test_a_stripped_line_is_still_located_and_marked_spoken(self):
        """The entry text has lost its quotes - that is the whole problem."""
        entries = [{"speaker": "PETRA", "text": "Is there some problem, Subaru?"},
                   {"speaker": "NARRATOR",
                    "text": "He said nothing for a moment, thinking it over carefully."}]
        marked = mark_entries(entries, self.SOURCE, "paired_quotes")
        self.assertTrue(marked[0]["spoken"])
        self.assertFalse(marked[1]["spoken"])

    def test_the_source_span_points_at_the_real_words(self):
        entries = [{"speaker": "PETRA", "text": "Is there some problem, Subaru?"}]
        start, end = mark_entries(entries, self.SOURCE, "paired_quotes")[0]["source_span"]
        self.assertIn("Is there some problem", self.SOURCE[start:end])

    def test_an_entry_that_cannot_be_located_is_left_unmarked(self):
        # Absent `spoken` means "not established", which is a different claim
        # from `spoken: false` and must not be collapsed into it.
        marked = mark_entries([{"speaker": "X", "text": "Nothing like this is in the source."}],
                              self.SOURCE, "paired_quotes")
        self.assertNotIn("spoken", marked[0])

    def test_the_input_entries_are_not_mutated(self):
        entries = [{"speaker": "PETRA", "text": "Is there some problem, Subaru?"}]
        mark_entries(entries, self.SOURCE, "paired_quotes")
        self.assertEqual({"speaker", "text"}, set(entries[0]))


class MixedQuoteStyleTest(unittest.TestCase):
    """Straight quotes can only be paired by position, so they need a guard.

    mushoku18 mixes “ ” with " ". One unmatched straight mark shifts every
    pair after it, and the resulting "span" covered the narration between two
    unrelated quotes - marking `I was taken aback.` as spoken and inflating the
    book's misattribution rate.
    """

    SOURCE = ('...pleased to make your acquaintance.”\n\n'
              'I was taken aback.\n\n'
              'Wait a minute, who is that?\n\n'
              '"Armored Dragon King Perugius," he said.\n')

    def test_narration_between_two_quotes_is_not_marked_spoken(self):
        spans = spoken_spans(self.SOURCE, "paired_quotes")
        covered = " ".join(self.SOURCE[a:b] for a, b in spans)
        self.assertNotIn("I was taken aback", covered)
        self.assertNotIn("Wait a minute", covered)

    def test_a_real_quoted_line_on_one_paragraph_still_counts(self):
        text = 'He turned. "Armored Dragon King Perugius." Silence followed.'
        spans = spoken_spans(text, "paired_quotes")
        self.assertEqual(["Armored Dragon King Perugius."],
                         [text[a:b] for a, b in spans])


class PrintedSpeakerLabelTest(unittest.TestCase):
    """When the book prints the speaker, copying it beats inferring it.

    arc4_volume10wn is a web-novel transcript: `Subaru “line”`, the name
    immediately before every quote. 88.9% of its quotes carry one, and against
    the model's own answers the printed label agrees on 2,909 of 2,967 lines -
    with every disagreement being the model MISSPELLING the printed name
    ("LONG HAILED GIRL" for "Long Haired Girl"). mushoku18 prints none, and
    gets none.
    """

    TRANSCRIPT = ("\n\n".join(
        [f"Subaru “Line {i} from Subaru.”\n\nPetra “Line {i} from Petra.”"
         for i in range(4)]))

    def test_printed_names_are_extracted(self):
        names = {name for _, name in speaker_labels(self.TRANSCRIPT)}
        self.assertEqual({"Subaru", "Petra"}, names)

    def test_a_name_must_recur_before_it_is_believed(self):
        """One capitalised word before a quote is a coincidence, not a cast."""
        text = 'Suddenly “Get down!”\n\nNobody moved at all.\n'
        self.assertEqual([], speaker_labels(text))

    def test_sentence_openers_are_not_mistaken_for_names(self):
        # "The" cleared the three-repeat bar in mushoku23 and would have
        # invented a character called The.
        text = "\n\n".join(['The “first thing” was ready.'] * 4)
        self.assertEqual([], speaker_labels(text))

    def test_a_book_without_the_convention_is_left_alone(self):
        text = "\n\n".join(['She waited. “Line {}.”'.format(i) for i in range(6)])
        self.assertFalse(uses_speaker_labels(text))

    def test_the_label_becomes_the_speaker(self):
        entries = [{"speaker": "NARRATOR", "text": "Line 0 from Subaru."}]
        marked = mark_entries(entries, self.TRANSCRIPT, "paired_quotes")
        fixed, changes = apply_source_speakers(marked)
        self.assertEqual("SUBARU", fixed[0]["speaker"])
        self.assertEqual("printed_speaker_label", changes[0]["type"])

    def test_entries_without_a_label_are_untouched(self):
        entries = [{"speaker": "NARRATOR", "text": "Nothing here matches."}]
        fixed, changes = apply_source_speakers(
            mark_entries(entries, self.TRANSCRIPT, "paired_quotes"))
        self.assertEqual("NARRATOR", fixed[0]["speaker"])
        self.assertEqual([], changes)
