"""The dialogue map is built from the source, not from what the model returned.

Generation is told to drop the outermost dialogue quotes, and follows that
unevenly - 22%, 16% and 1% retention across three books - so the fact that a
line was speech is destroyed rather than recorded. Rebuilding it afterwards
from the model's own prose is what made today's attribution scorer wrong three
times. The source still has the marks: arc4_volume10wn carries 6,925 of them
while its generated entries kept 16.
"""
import unittest
from unittest.mock import patch

import dialogue_spans
from dialogue_spans import (apply_dialogue_map, apply_source_narration, apply_source_speakers,
                            detect_convention, get_other_speech_spans, mark_entries,
                            speaker_labels, spoken_spans, uses_speaker_labels,
                            validate_spoken_spans)


class ConventionTest(unittest.TestCase):
    def test_one_character_utterances_are_counted_and_located_for_each_quote_style(self):
        for opener, closer in [('"', '"'), ('“', '”'), ('「', '」'), ('『', '』'),
                               ('„', '“'), ('＂', '＂')]:
            with self.subTest(opener=opener):
                words = list('AIえあ!?')
                text = '\n'.join(opener + word + closer for word in words)
                self.assertEqual('paired_quotes', detect_convention(text))
                self.assertEqual(words, [text[a:b] for a, b in spoken_spans(text)])
                self.assertEqual([], spoken_spans(opener + closer, 'paired_quotes'))

        # Low-high single marks are secondary speech, like «»: recognised so
        # the line is not called narration, without being the convention that
        # carries the book's dialogue. That is the same line this module draws
        # for every single mark, whose closer is also an apostrophe.
        source = "Er zögerte. ‚Komm her!‘ sagte er."
        self.assertEqual([], [source[a:b] for a, b in spoken_spans(source, "paired_quotes")])
        self.assertEqual(["Komm her!"], [source[a:b] for a, b in
                                         get_other_speech_spans(source, "paired_quotes")])

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

    def test_inches_before_dialogue_do_not_shift_speech_into_narration(self):
        for measurement in ('5"', '12.5"'):
            with self.subTest(measurement=measurement):
                source = f'A board measured {measurement} tall. She said "Hello." Then he answered "Bye."'
                self.assertEqual(['Hello.', 'Bye.'], [source[a:b] for a, b in spoken_spans(source, 'paired_quotes')])
                mapped = mark_entries([{'text': text} for text in ('tall. She said', 'Hello.', 'Then he answered', 'Bye.')], source, 'paired_quotes')
                self.assertEqual([False, True, False, True], [row['spoken'] for row in mapped])
        source = 'She said "I am 5" and he said "I am 6".'
        self.assertEqual(['I am 5', 'I am 6'], [source[a:b] for a, b in spoken_spans(source, 'paired_quotes')])

    def test_an_inch_mark_inside_speech_does_not_split_the_span(self):
        cases = [
            ('"The board is 6" wide," he said. "Good," she said.',
             ['The board is 6" wide,', 'Good,']),
            ('"It is 6" by 4" wide," he said.',
             ['It is 6" by 4" wide,']),
            ('"I counted 5" he said. "Next."',
             ['I counted 5', 'Next.']),
            ('"I am 5\'10"," he said.', ['I am 5\'10",']),
            ('He said "it was 6" and left 5" of rope. "Ok."',
             ['it was 6', 'Ok.']),
            ('"Only 5"', ['Only 5']),
            ('＂The board is 6＂ wide,＂ he said. ＂Good,＂ she said.',
             ['The board is 6＂ wide,', 'Good,']),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, [source[a:b] for a, b in
                                            spoken_spans(source, "paired_quotes")])

    def test_a_straight_inch_mark_inside_another_marks_speech_stays_spoken(self):
        cases = [
            ('“The board is 6" wide,” he said. “Good,” she said.',
             ['The board is 6" wide,', 'Good,']),
            ('„Es ist 6" breit“, sagte er. „Gut.“', ['Es ist 6" breit', 'Gut.']),
            ('«C\'est 6" de large», dit-il.', ["C'est 6\" de large"]),
            ('「6"の板」と言った。', ['6"の板']),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, [source[a:b] for a, b in
                                            spoken_spans(source, "paired_quotes")])

    def test_a_curly_inch_mark_is_not_a_closer(self):
        cases = [
            ('“The board is 6” wide,” he said. “Good,” she said.',
             ['The board is 6” wide,', 'Good,']),
            ('The board was 6” wide. “Good,” she said.', ['Good,']),
            ('“He is 5’10” tall,” she said.', ['He is 5’10” tall,']),
            ('“I am 12” he said. “Good.”', ['I am 12', 'Good.']),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, [source[a:b] for a, b in
                                            spoken_spans(source, "paired_quotes")])

    def test_a_real_closer_after_a_digit_beats_a_later_inch_mark(self):
        # A later inch mark of the same character, with no opener between,
        # must not turn the real closer after a digit into one (#2314 review).
        pairs = [('"', '"'), ("“", "”"), ("„", "“"), ("„", "”"),
                 ("「", "」"), ("『", "』"), ("＂", "＂"), ("«", "»"), ("»", "«")]
        for opener, closer in pairs:
            with self.subTest(opener=opener, closer=closer):
                source = (f'{opener}I am 12{closer} she said. '
                          f'The board was 6{closer} wide.')
                self.assertEqual(
                    ['I am 12'],
                    [source[a:b] for a, b in spoken_spans(source, "paired_quotes")])

    def test_a_later_speech_after_a_digit_closer_is_still_speech(self):
        cases = [
            ('"I am 12" she said. "The board was 6" wide."',
             ['I am 12', 'The board was 6" wide.']),
            ('“I am 12” she said. “The board was 6” wide.”',
             ['I am 12', 'The board was 6” wide.']),
            ('「I am 12」 she said. 「The board was 6」 wide.」',
             ['I am 12', 'The board was 6」 wide.']),
            ('«I am 12» she said. «The board was 6» wide.»',
             ['I am 12', 'The board was 6» wide.']),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, [source[a:b] for a, b in
                                            spoken_spans(source, "paired_quotes")])

    def test_when_every_later_closer_is_digit_preceded_the_continuation_decides(self):
        # No real closer is left, so a bare digit end must close
        # (`12" she said`) unless the text after it is still a measurement
        # (`6" by 8"`, `6" wide and 8"`).
        cases = [
            ('"I am 12" she said. The board was 6" wide.', ['I am 12']),
            ('"It is 6" by 8", he said.', ['It is 6" by 8']),
            ('"It is 6" wide and 8"', ['It is 6" wide and 8']),
            ('“I am 12” she said. The board was 6” wide.', ['I am 12']),
            ('“It is 6” by 8”, he said.', ['It is 6” by 8']),
            ('“It is 6” wide and 8”', ['It is 6” wide and 8']),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, [source[a:b] for a, b in
                                            spoken_spans(source, "paired_quotes")])

    def test_a_digit_final_speech_closes_when_no_later_mark_can_close_it(self):
        # A mark with no later closer in the paragraph is the only thing that
        # can close this speech, so the word after it cannot make it an inch
        # mark. Main already answered this way; the inch fix must not undo it.
        cases = [
            ('“It was 1945” long ago.', ['It was 1945']),
            ('"It was 1945" long ago.', ['It was 1945']),
            ('“We counted 20” high up on the wall.', ['We counted 20']),
            ('"We counted 20" high up on the wall.', ['We counted 20']),
            ('“Chapter 7” to 9 were missing.', ['Chapter 7']),
            ('“He is 12” and 13 is old.', ['He is 12']),
            ('“Agent 47” across the hall waved.', ['Agent 47']),
            ('He wrote “1984” long before.', ['1984']),
            ('He wrote "1984" long before.', ['1984']),
            ('“Room 12” deep in the cellar was cold.', ['Room 12']),
            ('She said “10” and 12 people laughed.', ['10']),
            ('She said "10" and 12 people laughed.', ['10']),
            ('“It was 1945” long ago, she wrote.', ['It was 1945']),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, [source[a:b] for a, b in
                                            spoken_spans(source, "paired_quotes")])

    def test_a_later_number_mark_behind_a_new_opener_cannot_close(self):
        # Only the second speech's closer is number-preceded, and it belongs
        # to that speech, so the first speech still closes at its own mark.
        for source, expected in [
                ('“Catch 22” high and low, she searched. “Area 51” too.',
                 ['Catch 22', 'Area 51']),
                ('"Catch 22" high and low, she searched. "Area 51" too.',
                 ['Catch 22', 'Area 51']),
                ('“We counted 20” high up on the wall. “Good,” she said.',
                 ['We counted 20', 'Good,'])]:
            with self.subTest(source=source):
                self.assertEqual(expected, [source[a:b] for a, b in
                                            spoken_spans(source, "paired_quotes")])
        # A number mark before the new opener can still close the speech, so
        # the measurement before it stays in the speech.
        source = '“It is 6” wide and 8” He said so. “Fine.”'
        self.assertEqual(['It is 6” wide and 8', 'Fine.'],
                         [source[a:b] for a, b in
                          spoken_spans(source, "paired_quotes")])

    def test_a_digit_final_speech_before_a_new_speaker_keeps_its_close(self):
        # The narration after the digit and the next speaker's speech both sit
        # in the same convention, so the only question is whether this mark
        # closes. It does: the next paragraph re-opening the quote cannot
        # prove the speech continued, and main closes here (#2318).
        cases = [
            ('“Give me 5” high above his head, Tom raised a palm.\n\n'
             '“Ha!” said Ann.', ['Give me 5', 'Ha!']),
            ('“Table for 2” and 3 more chairs arrived.\n\n'
             '“Thanks,” she said.', ['Table for 2', 'Thanks,']),
            ('"Give me 5" high above his head, Tom raised a palm.\n\n'
             '"Ha!" said Ann.', ['Give me 5', 'Ha!']),
            ('"Table for 2" and 3 more chairs arrived.\n\n'
             '"Thanks," she said.', ['Table for 2', 'Thanks,']),
            ('„Give me 5“ high above his head, Tom raised a palm.\n\n'
             '„Ha!“ said Ann.', ['Give me 5', 'Ha!']),
            ('«Give me 5» high above his head, Tom raised a palm.\n\n'
             '«Ha!» said Ann.', ['Give me 5', 'Ha!']),
            ('「Give me 5」 high above his head, Tom raised a palm.\n\n'
             '「Ha!」 said Ann.', ['Give me 5', 'Ha!']),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, [source[a:b] for a, b in
                                            spoken_spans(source, "paired_quotes")])

    def test_a_mark_after_a_dash_colon_or_bracket_opens_the_next_speech(self):
        # A mark followed by `…`, `.` or `'` looked closer-shaped, so the next
        # speech merged into the first. A mark right after `—`, `:` or `(`
        # is opener-shaped whatever follows it, so the digit mark closes
        # (#2318).
        prefixes = [' he said—', ' she said:', ' (he said—',
                    ' he said, then:', ' (she said:', ' he said (a pause —',
                    ' per the note:']
        leaders = ['…', '.', "'"]
        for opener, closer in (('"', '"'), ('＂', '＂')):
            for prefix in prefixes:
                for leader in leaders:
                    source = (f'{opener}Room 5{closer}{prefix}'
                              f'{opener}{leader}no, wait.{closer}')
                    with self.subTest(source=source):
                        self.assertEqual(
                            ['Room 5', f'{leader}no, wait.'],
                            [source[a:b] for a, b in
                             spoken_spans(source, "paired_quotes")])

    def test_link_and_number_in_narration_do_not_keep_the_close(self):
        # `and 3` / `by 8` after the digit read like `6" by 8"`, so the
        # paragraph's later real inch mark kept the closer and swallowed the
        # narration. A link counts only when the second number is itself
        # followed by the mark (#2318).
        pairs = [('"', '"'), ('“', '”'), ('„', '“'), ('„', '”'), ('「', '」'),
                 ('『', '』'), ('＂', '＂'), ('«', '»'), ('»', '«')]
        for opener, closer in pairs:
            for link in ('and', 'by', 'to', 'x'):
                source = (f'{opener}We leave at 5{closer} {link} 3 of them '
                          f'nodded. The shelf was 12{closer} deep.')
                with self.subTest(source=source):
                    self.assertEqual(
                        ['We leave at 5'],
                        [source[a:b] for a, b in
                         spoken_spans(source, "paired_quotes")])
        # The `,` and `×` separators are links too, so the second number must
        # also carry the mark to be a measurement, as `6", 8"` does.
        for source, expected in [
                ('“It is 6”, 8 people came. The shelf was 12” deep.',
                 ['It is 6']),
                ('“It is 6” × 8 people came. The shelf was 12” deep.',
                 ['It is 6']),
                ('“It is 6”, 8” wide” she said.', ['It is 6”, 8” wide'])]:
            with self.subTest(source=source):
                self.assertEqual(expected, [source[a:b] for a, b in
                                            spoken_spans(source, "paired_quotes")])

    def test_a_reply_mark_after_any_narration_punctuation_opens_a_speech(self):
        # Round 6 recognised only a mark right after `—`, `:` or `(`. The
        # shape is the same after every dash, bracket, other punctuation or
        # symbol, spaced or glued, and after a full stop with no space: the
        # mark is glued to the reply and no letter or digit precedes it, so it
        # opens a speech and cannot close this one (#2318).
        separators = [
            # Pd, Ps, Po, Sm: the four categories the reply mark follows.
            '–', '—', '‒', '―', '-',
            '[', '(', '{', '［', '（',
            '/', ';', '…', '*', '.', '!', '?', '&', ':', '，', '。', '：',
            '+', '=', '±', '×', '÷', '<', '>', '|', '~',
        ]
        leaders = ['...', '…', '.', "'", 'N']
        for opener, closer in (('"', '"'), ('＂', '＂')):
            for separator in separators:
                forms = [f'he said{separator}', f'he said {separator} ']
                for narration in forms:
                    for leader in leaders:
                        source = (f'{opener}Room 5{closer}{narration}'
                                  f'{opener}{leader}no, wait.{closer}')
                        with self.subTest(source=source):
                            self.assertEqual(
                                ['Room 5', f'{leader}no, wait.'],
                                [source[a:b] for a, b in
                                 spoken_spans(source, "paired_quotes")])
        # No separator at all: the narration runs into the reply's mark.
        for leader in leaders:
            source = f'"Room 5"he said"{leader}no, wait."'
            with self.subTest(source=source):
                self.assertEqual(['Room 5', f'{leader}no, wait.'],
                                 [source[a:b] for a, b in
                                  spoken_spans(source, "paired_quotes")])

    def test_a_unit_word_does_not_keep_the_close_when_narration_follows(self):
        # A unit word after the digit kept the mark as an inch mark even when
        # the next word began narration, so a later real inch mark in the
        # paragraph became this speech's closer and swallowed everything
        # between. The unit branch needs the same confirmation the link branch
        # has: a mark, or a linked number a mark follows, inside the clause
        # (#2318).
        pairs = [('"', '"'), ('“', '”'), ('„', '“'), ('„', '”'), ('「', '」'),
                 ('『', '』'), ('＂', '＂'), ('«', '»'), ('»', '«')]
        continuations = ['in the cellar', 'after midnight', 'open',
                         'above the door', 'across the yard']
        for opener, closer in pairs:
            for unit in sorted(dialogue_spans._MEASURE_UNITS):
                for continuation in continuations:
                    source = (f'{opener}Room 12{closer} {unit} '
                              f'{continuation}, the shelf was cold. '
                              f'The board was 8{closer} deep.')
                    with self.subTest(source=source):
                        self.assertEqual(
                            ['Room 12'],
                            [source[a:b] for a, b in
                             spoken_spans(source, "paired_quotes")])
        # What follows the unit word may still confirm the measurement: the
        # speech's closer, or a linked number the closer follows.
        for source, expected in [
                ('"It is 6" wide and 8"', 'It is 6" wide and 8'),
                ('“It is 6” wide and 8”', 'It is 6” wide and 8'),
                ('"Cut it 3½" wide and 12" long," he said.',
                 'Cut it 3½" wide and 12" long,'),
                ('"Make it 12" long and 4" deep," he said.',
                 'Make it 12" long and 4" deep,'),
                ('"Room 9" by 2"', 'Room 9" by 2')]:
            with self.subTest(source=source):
                self.assertEqual([expected], [source[a:b] for a, b in
                                              spoken_spans(source, "paired_quotes")])

    def test_an_english_reply_inside_brackets_does_not_hold_a_german_speech_open(self):
        # `(“nein”)` inside a German low-high paragraph opens an English
        # speech, so its mark cannot close the German one: `„Zimmer 5“` is
        # spoken on its own (#2318).
        german = ('„Hallo.“ sagte er.\n\n„Guten Tag.“ sagte sie.\n\n'
                  '„Wie geht es?“ fragte er.\n\n„Gut.“ sagte sie.\n\n')
        source = german + '„Zimmer 5“ sagte er. (“nein”).'
        self.assertEqual(['Hallo.', 'Guten Tag.', 'Wie geht es?', 'Gut.',
                          'Zimmer 5', 'nein'],
                         [source[a:b] for a, b in
                          spoken_spans(source, "paired_quotes")])

    def test_a_continued_speech_is_the_only_reason_to_keep_the_mark(self):
        # The name records the round that added the paragraph-reopen
        # exception; #2318 removed it. A next paragraph that re-opens the same
        # mark reads identically whether the speech continues or a new speaker
        # answers, so it is no evidence, and the digit-preceded mark closes as
        # main has always read it.
        for opener, closer in (('“', '”'), ('"', '"')):
            source = (opener + 'The board is 6' + closer + ' wide and\n\n'
                      + opener + 'it is heavy,' + closer + ' he said.')
            with self.subTest(source=source):
                start = source.index('it is heavy,')
                self.assertEqual(
                    [(1, 1 + len('The board is 6')),
                     (start, start + len('it is heavy,'))],
                    spoken_spans(source, "paired_quotes"))
        # The same break with no measurement word after the digit closes.
        for opener, closer in (('“', '”'), ('"', '"')):
            source = (opener + 'I am 12' + closer + ' she said\n\n'
                      + opener + 'Good,' + closer + ' he said.')
            with self.subTest(source=source):
                start = source.index('Good,')
                self.assertEqual(
                    [(1, 1 + len('I am 12')), (start, start + len('Good,'))],
                    spoken_spans(source, "paired_quotes"))
        # A paragraph beginning with something else is not a continuation,
        # even when the word after the digit is a unit.
        for source in ('“It was 1945” long ago.\n\nHe left.',
                       '"It was 1945" long ago.\n\nHe left.'):
            with self.subTest(source=source):
                self.assertEqual(['It was 1945'],
                                 [source[a:b] for a, b in
                                  spoken_spans(source, "paired_quotes")])
        source = '“It was 1945” long ago.\n\n"Good," he said.'
        self.assertEqual(['It was 1945', 'Good,'],
                         [source[a:b] for a, b in
                          spoken_spans(source, "paired_quotes")])

    def test_measurement_continuation_forms_keep_the_mark(self):
        cases = [
            ('“It is 6” by 8”', 'It is 6” by 8'),
            ('“It is 6”, 8”', 'It is 6”, 8'),
            ('“It is 6” × 8”', 'It is 6” × 8'),
            ('“It is 6” x 8”', 'It is 6” x 8'),
            ('“It is 6” to 8”', 'It is 6” to 8'),
            ('“It is 6” and 8”', 'It is 6” and 8'),
            ('“It is 6” wide and 8”', 'It is 6” wide and 8'),
            # The link's second number may end with a different mark than the
            # straight inch mark that opened the measurement.
            ('“It is 6" and 8”', 'It is 6" and 8'),
            ('«It is 6" by 8»', 'It is 6" by 8'),
            ('»It is 6" x 8«', 'It is 6" x 8'),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual([expected], [source[a:b] for a, b in
                                              spoken_spans(source, "paired_quotes")])
        # A different closer right after the inch mark is the real closer, not
        # a duplicate of it, so the inch mark stays and the speech ends.
        source = "«C'est 6\"», dit-il."
        self.assertEqual(["C'est 6\""],
                         [source[a:b] for a, b in spoken_spans(source, "paired_quotes")])

    def test_a_duplicate_curly_closer_keeps_the_first_closing(self):
        cases = [
            ('“I am 12”” she said.', ['I am 12']),
            ('“I am 12””', ['I am 12']),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, [source[a:b] for a, b in
                                            spoken_spans(source, "paired_quotes")])

    def test_a_real_closer_after_a_unicode_number_is_not_an_inch_mark(self):
        self.assertTrue(dialogue_spans.is_inch_mark('It is 3½” long', 8, '”'))
        self.assertTrue(dialogue_spans.is_inch_mark('It is 8²" long', 8, '"'))
        self.assertFalse(dialogue_spans.is_inch_mark('「三」', 2, '」'))

    def test_an_inch_mark_at_a_line_end_outside_speech_starts_no_span(self):
        cases = [
            ('The board was 6"', []),
            ('He wrote 5"\nShe read it. "Go," she said.', ['Go,']),
            ('A 12"', []),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, [source[a:b] for a, b in
                                            spoken_spans(source, "paired_quotes")])

    def test_adjacent_straight_marks_after_a_close_keep_todays_spans(self):
        source = '"I counted 5""Next," she said.'
        self.assertEqual(['I counted 5', 'Next,'], [source[a:b] for a, b in
                                                    spoken_spans(source, "paired_quotes")])

    def test_an_unmatched_straight_quote_does_not_swallow_the_book(self):
        text = '"Only one mark here, and then a great deal of narration. ' + "x " * 500
        self.assertEqual([], spoken_spans(text, "paired_quotes"))

    def test_a_long_one_paragraph_speech_is_spoken(self):
        # #1533: a single speech past 2,000 characters matched no span, so
        # every entry inside it was recorded as narration. Dorian Gray has nine.
        for opener, closer in (("“", "”"), ('"', '"'), ("「", "」")):
            for size in (1900, 2000, 2001, 3300):
                with self.subTest(mark=opener, size=size):
                    speech = ("word " * size)[:size]
                    text = f"He said, {opener}{speech}{closer} and left."
                    self.assertEqual([speech], [text[a:b] for a, b in spoken_spans(text, "paired_quotes")])

    def test_a_pair_across_narration_is_refused_whatever_its_length(self):
        # A break the next paragraph does not re-open is narration, so a pair
        # reaching across it is refused and the real pair after it is found.
        # A chain that re-opens the mark at every paragraph is a speech, not a
        # swallow - and costs one indexed pass, not a walk per opener.
        narration = "The narrator went on at great length. " * 60
        text = f"“Never closed.\n\n{narration}\n\n“Real line.” she said."
        self.assertEqual(["Real line."], [text[a:b] for a, b in spoken_spans(text, "paired_quotes")])

        class MeasuredSource(str):
            def __new__(cls, value):
                result = super().__new__(cls, value)
                result.search_extent = result.copied_characters = 0
                return result

            def find(self, needle, start=0, end=None):
                stop = len(self) if end is None else end
                result = super().find(needle, start, stop)
                # Logical prefix searched, not wall-clock time or instructions.
                self.search_extent += max(0, (result + len(needle) if result != -1 else stop) - start)
                return result

            def __getitem__(self, index):
                if isinstance(index, slice):
                    self.copied_characters += len(range(*index.indices(len(self))))
                return super().__getitem__(index)

        for opener, closer in (('“', '”'), ('「', '」'), ('『', '』')):
            for shape, value in [('many_breaks', (opener + '\n\n') * 4096 + 'x' + closer),
                                 ('distant_break', opener * 4096 + 'x' * 4000 + '\n\n' + closer)]:
                with self.subTest(mark=opener, shape=shape):
                    measured = MeasuredSource(value)
                    spans = spoken_spans(measured, 'paired_quotes')
                    # The trailing 'x' paragraph is narration, so every
                    # candidate crosses it - including the short pair the old
                    # 2,000-character exemption used to let through. Nothing
                    # here is spoken.
                    expected = []
                    self.assertEqual(expected, spans)
                    self.assertEqual(0, measured.copied_characters)
                    self.assertLessEqual(measured.search_extent, 8 * len(value))

    def test_an_orphan_closing_mark_does_not_swallow_narration(self):
        # A stray `”` after a German closer must not make that closer look like
        # an English opener — otherwise the German pair runs on over narration.
        source = "„German.“ narration ”orphan closer. “Real.”"
        self.assertEqual(["German.", "Real."],
            [source[a:b] for a, b in spoken_spans(source, "paired_quotes")])

    def test_a_straight_quote_inside_english_does_not_close_german(self):
        # Straight marks pair among themselves; they must not decide what the
        # next mark after a `“` is, or the utterance fragments at the `"` in
        # `5" wide`.
        source = "„He said “it's \"5\" wide” to me.“ end"
        self.assertEqual(["He said “it's \"5\" wide” to me."],
            [source[a:b] for a, b in spoken_spans(source, "paired_quotes")])

    def test_a_reprise_closer_after_a_digit_is_not_an_inch_mark(self):
        # The inch-mark rule only applies to a mark opening an inline expression
        # (5" wide). At the end of a paragraph it still closes the speech.
        source = '"Part one.\n\n"Part two ends 5"'
        self.assertEqual(['Part one.\n\n"Part two ends 5'],
            [source[a:b] for a, b in spoken_spans(source, "paired_quotes")])

    def test_the_inch_mark_predicate_matches_the_pairing_rule(self):
        # One shared predicate: _paired_spans and pass_quality both ask this
        # exact question (#2314).
        cases = [
            ('A 6" wide board', 3, False, True),
            ('A 12" wide board', 4, False, True),
            ('A 12""Next', 4, False, True),
            ('A 12" "Next', 4, False, True),
            ('A 12"\n"Next', 4, False, True),
            # Outside speech a line-ending mark is a measurement too; only the
            # parity-only caller can read a paragraph-final mark as a reprise
            # closer, and it does so only with an unmatched earlier mark.
            ('A 12"\nnext', 4, False, True),
            ('A 12"', 4, False, True),
            ('A six" wide', 5, False, False),
            ('"6 of us"', 0, False, False),
            # Inside open speech: the next non-digit-preceded mark decides.
            ('"A 6" wide," he said.', 4, True, True),
            ('"I counted 5" he said. "Next."', 12, True, False),
            ('"I counted 5" he said.', 12, True, False),
            ('"It is 6" by 4" wide," he said.', 8, True, True),
        ]
        for text, position, inside, expected in cases:
            with self.subTest(text=text, inside=inside):
                self.assertEqual(expected, dialogue_spans.is_inch_mark(
                    text, position, '"', inside))

    def test_a_paragraph_final_mark_is_a_reprise_closer_only_when_unbalanced(self):
        # `_paired_spans` has parity, not the analyzer's real depth. A
        # paragraph that re-opened the quote has no closer but its last mark,
        # so that mark still closes; with balanced earlier marks it cannot.
        for gap in ("\n\n", "\r\n\r\n"):
            reprise = f'"Part one.{gap}"Part two ends 5"'
            self.assertFalse(dialogue_spans.is_inch_mark(
                reprise, len(reprise) - 1, '"', False))
        self.assertTrue(dialogue_spans.is_inch_mark('A 12"', 4, '"', False))
        # A mark ending an ordinary line, not a paragraph, is a measurement.
        self.assertTrue(dialogue_spans.is_inch_mark('A 12"\nnext', 4, '"', False))
        # A paragraph-final measurement followed by a new quote stays a
        # measurement even when the paragraph holds an unmatched earlier mark;
        # main read it that way. Without this the odd-count rule would close.
        self.assertTrue(dialogue_spans.is_inch_mark(
            '"Part one. A 12"\n\n"Next"', 15, '"', False))

    def test_the_inch_mark_predicate_handles_a_different_opener(self):
        # Inside speech opened by a mark other than the one before the digit,
        # the opener's own closer decides whether this mark can close (#2314).
        cases = [
            ('“The board is 6" wide,” he said.', 15, '"', '“', True),
            ('„Es ist 5" breit“, sagte er.', 9, '"', '„', True),
            ('„Es ist 5" sagte er. „Gut."', 9, '"', '„', False),
            ('«C\'est 6" de large», dit-il.', 8, '"', '«', True),
            ('「6"の板」と言った。', 2, '"', '「', True),
            ('“I am 12" he said. “Good.”', 8, '"', '“', False),
        ]
        for text, position, mark, opener, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(expected, dialogue_spans.is_inch_mark(
                    text, position, mark, True, opener=opener))

    def test_the_inch_mark_predicate_handles_a_curly_closer(self):
        # Direction is known for `”`: another `”` later in the paragraph makes
        # this one a measurement; a `“` makes it the closer.
        cases = [
            ('“The board is 6” wide,” he said.', 15, True, '“', True),
            ('“I am 12” he said. “Good.”', 8, True, '“', False),
            ('“He said ‘it is 6” wide’ to me,” she said.', 17, True, '“', True),
            ('The board was 6” wide.', 15, False, None, True),
            ('The board was 6”', 15, False, None, True),
        ]
        for text, position, inside, opener, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(expected, dialogue_spans.is_inch_mark(
                    text, position, '”', inside, opener=opener))

    def test_the_number_test_covers_unicode_numbers_but_not_cjk_numerals(self):
        for char in ("7", "½", "²", "①"):
            with self.subTest(char=char):
                self.assertTrue(dialogue_spans.is_number_char(char))
        # A CJK numeral is a letter, so a quote after it is a delimiter.
        self.assertFalse(dialogue_spans.is_number_char("三"))

    def test_a_stray_closer_after_german_speech_keeps_narration_out(self):
        # A `”` after narration, with no later `“`, must not flip the real
        # German closer into an English opener — otherwise the speech is lost
        # and the narration is marked spoken and voiced by a character.
        source = "„Ich bin da.“Er ging nach Hause”und schwieg."
        self.assertEqual(["Ich bin da."],
            [source[a:b] for a, b in spoken_spans(source, "paired_quotes")])
        marked = mark_entries([
            {"speaker": "OTHER", "text": "Ich bin da."},
            {"speaker": "HER", "text": "Er ging nach Hause"},
        ], source, "paired_quotes")
        self.assertTrue(marked[0]["spoken"])
        self.assertNotEqual(True, marked[1].get("spoken"))

    def test_a_later_german_pair_does_not_merge_an_earlier_speech(self):
        # The close-capable `“` of a SECOND German pair must not make the first
        # pair's closer look like an English opener. Otherwise the two speeches
        # merge and the narration between them is labelled as spoken.
        clean = "\n\n".join("„Clean speech " + str(i) + " here“ said he."
                            for i in range(6))
        source = clean + "\n\n„Red“ she muttered ”stray „Blue.“ he replied."
        rendered = [source[a:b] for a, b in spoken_spans(source)]
        self.assertIn("Red", rendered)
        self.assertIn("Blue.", rendered)
        self.assertFalse(any("she muttered" in span for span in rendered))

    def test_straight_quote_reprise_chain_is_linear(self):
        # A mark that opens a paragraph re-opens the quote, so the closer search
        # skips it. Restarting that scan at every opener was quadratic: 8k
        # marks took ~3.3s. A forward index keeps it linear (~0.02s).
        import time
        for mark in ('"', '＂'):
            with self.subTest(mark=mark):
                source = "\n\n".join(mark + "line" for _ in range(8000))
                started = time.monotonic()
                # Every mark opens a paragraph, so none can close a pair.
                self.assertEqual([], spoken_spans(source, "paired_quotes"))
                self.assertLess(time.monotonic() - started, 1.0)

    def test_a_foreign_quote_density_stays_secondary_in_an_english_book(self):
        # Even at the detection threshold, low-high pairs fewer than the book's
        # English pairs stay other speech rather than getting character voices.
        english = "\n\n".join("“English " + str(i) + ".” said he." for i in range(6))
        german = "\n\n".join("„Wort" + str(i) + "“" for i in range(5))
        source = english + "\n\n" + german
        rendered = [source[a:b] for a, b in spoken_spans(source, "paired_quotes")]
        self.assertEqual(6, len(rendered))
        self.assertFalse(any("Wort" in span for span in rendered))

    def test_a_stray_german_opener_before_one_english_quote(self):
        # With a single English quote there is no later “ to disambiguate, so
        # the space before it identifies the stray `„`. Base gets this right.
        for source, expected in [
            ("„narrative words here “Real English.” end", "Real English."),
            ("He paused. „what now. “Actual speech here.” she said.",
             "Actual speech here."),
        ]:
            with self.subTest(source=source):
                self.assertEqual([expected],
                    [source[a:b] for a, b in spoken_spans(source, "paired_quotes")])

    def test_a_stray_german_opener_does_not_swallow_english_speech(self):
        # `„` is a stray mark here; both following “English” pairs are real
        # speech. Treating the first `“` as the German closer drops it and
        # marks the narration spoken — base gets both right.
        source = "„orphan stray “English one” and “English two” end"
        self.assertEqual(["English one", "English two"],
            [source[a:b] for a, b in spoken_spans(source, "paired_quotes")])
        marked = mark_entries([
            {"speaker": "A", "text": "English one"},
            {"speaker": "B", "text": "English two"},
            {"speaker": "N", "text": "orphan stray"},
        ], source, "paired_quotes")
        self.assertTrue(marked[0].get("spoken"))
        self.assertTrue(marked[1].get("spoken"))
        self.assertNotEqual(True, marked[2].get("spoken"))

    def test_padded_english_speech_nested_in_german_is_not_an_orphan(self):
        # Edge whitespace (including a newline) is not evidence of an orphan
        # `”` — a real English quote inside a German pair keeps the German
        # closer at the later `“`, so the whole utterance stays spoken.
        for source, expected in [
            ("„He said “ Hi ” to me.“ she said.", "He said “ Hi ” to me."),
            ("„He said “Hi ” to me.“ she said.", "He said “Hi ” to me."),
            ("„He said “\nHi” to me.“ she said.", "He said “\nHi” to me."),
        ]:
            with self.subTest(source=source):
                self.assertEqual([expected],
                    [source[a:b] for a, b in spoken_spans(source, "paired_quotes")])

    def test_an_orphan_closer_after_nesting_keeps_both_speeches(self):
        # An orphan `”` has empty or whitespace-padded content — it is not a
        # real English closer, so it must not turn the preceding German closer
        # into an English opener (which would drop nested speech or swallow a
        # later German speech into the narration).
        for source, expected in [
            ("„He said “Hi” to me.“ ”orphan", ["He said “Hi” to me."]),
            ("„A “B” C“ ”orphan „D.“", ["A “B” C", "D."]),
        ]:
            with self.subTest(source=source):
                self.assertEqual(expected,
                    [source[a:b] for a, b in spoken_spans(source, "paired_quotes")])

    def test_shared_mark_disambiguation_is_linear(self):
        # The shared-mark roles are resolved with position-indexed lookups. A
        # linear rescan per quote made a 20k-pair text take seconds; this is a
        # coarse wall-clock guard with a wide margin (measured ~0.05s).
        import time
        source = "\n\n".join("„Speech " + str(i) + " here“ said he." for i in range(20000))
        started = time.monotonic()
        self.assertEqual(20000, len(spoken_spans(source, "paired_quotes")))
        self.assertLess(time.monotonic() - started, 2.0)

    def test_guillemet_closers_do_not_pair_as_openers(self):
        # Both «…» and »…« are in PAIRED, but a text uses one direction. With
        # single newlines the narration between speeches must stay narration.
        for opener, closer in [('«', '»'), ('»', '«')]:
            with self.subTest(opener=opener):
                source = opener + "Bonjour" + closer + " then he left."
                self.assertEqual(["Bonjour"],
                    [source[a:b] for a, b in spoken_spans(source, "paired_quotes")])

    def test_a_stray_guillemet_before_the_first_speech_does_not_flip_direction(self):
        # A breadcrumb `»` ahead of the first `«` once chose `»…«` for the
        # whole text: every speech became narration and every gap between two
        # speeches became spoken. The marks' shape decides, not their order.
        speeches = ["Bonjour", "Ça va?", "Oui.", "Non.", "Peut-être."]
        for stray, opener, closer in [("Menu » Chapitre. ", "«", "»"),
                                      ("Menu « Kapitel. ", "»", "«")]:
            with self.subTest(opener=opener):
                source = stray + " ".join(opener + s + closer + ", dit-il."
                                          for s in speeches)
                self.assertEqual("paired_quotes", detect_convention(source))
                self.assertEqual(
                    speeches,
                    [source[a:b] for a, b in spoken_spans(source, "paired_quotes")])

    def test_guillemet_direction_falls_back_to_first_mark_on_a_tie(self):
        # Padded `« x »` mid-line casts no shape vote either way; the old
        # first-mark rule still decides it, so such books behave as before.
        self.assertEqual(("«", "»"), dialogue_spans.get_guillemet_direction("a « b » c"))
        self.assertEqual(("»", "«"), dialogue_spans.get_guillemet_direction("a » b « c"))
        self.assertEqual((None, None), dialogue_spans.get_guillemet_direction("no marks"))

    def test_a_stray_straight_mark_does_not_carry_a_pair_across_a_paragraph(self):
        # A reprise break (next paragraph opens with `"`) looks identical
        # after a stray mark in narration. Only an opener-shaped mark may
        # carry a pair across it; a padded stray must not swallow narration.
        source = 'He held a " thing up.\n\n"Go home," she said. "Now," she said.'
        self.assertEqual(
            ["Go home,", "Now,"],
            [source[a:b] for a, b in dialogue_spans._paired_spans(source)])
        # The real reprise is still one continued speech.
        reprise = '"First para of speech.\n\n"Second para," he said.'
        self.assertEqual(
            ['First para of speech.\n\n"Second para,'],
            [reprise[a:b] for a, b in dialogue_spans._paired_spans(reprise)])

    def test_guillemets_polish_and_danish_are_first_class_conventions(self):
        # French/Russian guillemets, Polish low-high and Danish reversed marks
        # must be detected and give every speech a spoken fact, not a narrator.
        for opener, closer in [('«', '»'), ('»', '«'), ('„', '”')]:
            with self.subTest(opener=opener):
                source = "\n\n".join(opener + "Speech " + str(i) + "." + closer
                                     for i in range(6))
                self.assertEqual("paired_quotes", detect_convention(source))
                self.assertEqual(6, len(spoken_spans(source, "paired_quotes")))

    def test_a_book_that_only_reopens_nowhere_is_deliberately_refused(self):
        # Documented trade-off: typography that opens a speech once and closes
        # it paragraphs later, without re-opening the mark, cannot be told from
        # a mis-pair swallowing narration. The strict rule refuses it; a bounded
        # fallback was tried and reinstated exactly the swallow this rule bans,
        # so the loss is accepted rather than repaired silently. The text is
        # still surfaced by get_other_speech_spans for review.
        source = "\n\n".join(
            "“" + ("word " * 120) + "\n\n" + ("word " * 120) + ".” she said."
            for _ in range(6))
        self.assertEqual([], spoken_spans(source, "paired_quotes"))
        self.assertIsNone(detect_convention(source))
        self.assertTrue(get_other_speech_spans(source, "paired_quotes"))

    def test_a_german_quote_in_an_english_book_is_not_given_a_voice(self):
        # A sign or foreign term is secondary speech, not dialogue. German only
        # carries the convention when it is dense enough to be the book's mark.
        english = "\n\n".join("“Line " + str(i) + ", ” he said." for i in range(6))
        source = english + "\n\nThe sign read „Ausgang“ and he left."
        rendered = [source[a:b] for a, b in spoken_spans(source, "paired_quotes")]
        self.assertEqual(6, len(rendered))
        self.assertFalse(any("Ausgang" in span for span in rendered))
        self.assertTrue(any("Ausgang" in source[a:b] for a, b in
                            get_other_speech_spans(source, "paired_quotes")))
        german = "\n\n".join("Er sagte „Zeile " + str(i) + " hier!“ und ging."
                             for i in range(6))
        self.assertEqual(6, len(spoken_spans(german, "paired_quotes")))
        self.assertEqual("paired_quotes", detect_convention(german))

    def test_an_inch_mark_before_a_dialogue_paragraph_does_not_start_the_span(self):
        source = 'The blade was 12"\n\n"Run now, he said."'
        self.assertEqual(["Run now, he said."],
            [source[a:b] for a, b in spoken_spans(source, "paired_quotes")])

    def test_a_blank_line_before_the_closing_mark_is_a_refused_candidate(self):
        # Deliberate scope of the rule: a blank line inside a pair must be
        # followed by the SAME mark. Here it is followed by the closer, so the
        # candidate is refused rather than treated as a continued speech — the
        # same refusal that bounds a chain of unclosed openers. The text is not
        # silently lost in every convention: other-speech detection may still
        # surface it for review, depending on the mark.
        for opener, closer in [('“', '”'), ('"', '"')]:
            with self.subTest(opener=opener):
                source = opener + "Hello\n\n" + closer + " she said."
                self.assertEqual([], [source[a:b] for a, b in spoken_spans(
                    source, "paired_quotes")])

    def test_a_short_multi_paragraph_speech_is_kept_by_the_reprise_rule(self):
        # Nothing to do with length: a speech whose paragraphs re-open the same
        # mark is one span, and was before the rule applied to every pair.
        text = "“First paragraph of the speech.\n\n“Second paragraph.” he said."
        self.assertEqual(["First paragraph of the speech.\n\n“Second paragraph."],
                         [text[a:b] for a, b in spoken_spans(text, "paired_quotes")])

    def test_a_long_speech_whose_paragraphs_reopen_the_quote_is_one_span(self):
        # The former length cap dropped the first paragraph of a long speech
        # and kept only the last; the reprise rule keeps the whole utterance.
        first, second = ("filler " * 200)[:1400], ("filler " * 200)[:1200]
        text = f"“{first}\n\n“{second}” he said."
        self.assertGreater(len(first) + len(second), 2000)
        self.assertEqual([text[1:text.index("”")]],
                         [text[a:b] for a, b in spoken_spans(text, "paired_quotes")])

    def test_german_and_full_width_marks_are_paired_and_stripped_for_matching(self):
        # German/Polish/Czech „…“ prints outside the marks this module paired
        # before, so the line was neither located nor stripped - it read as
        # narration with no error. Both halves are asserted here because
        # pairing without stripping still leaves every entry unlocated.
        source = "Er zögerte. „Komm sofort her!“ sagte er."
        (start, end), = spoken_spans(source, "paired_quotes")
        self.assertEqual("Komm sofort her!", source[start:end])
        marked = mark_entries([{"speaker": "A", "text": "Komm sofort her!"}],
                              source, "paired_quotes")
        self.assertTrue(marked[0]["spoken"])
        self.assertEqual("Komm sofort her!", source[slice(*marked[0]["source_span"])])

    def test_a_german_closer_is_not_mistaken_for_an_english_opener(self):
        # In a book mixing the two, `“` closes „…“ but also opens “…”. Treating
        # the German closer as an English opener paired it with the next English
        # closer and produced a phantom span over the narration between them.
        source = ("Er sagte „Komm her!“ und dann “Never mind.”\n\n"
                  "Er sagte „Geh jetzt!“ und dann “Too late.”")
        self.assertEqual(
            ["Komm her!", "Never mind.", "Geh jetzt!", "Too late."],
            [source[a:b] for a, b in spoken_spans(source, "paired_quotes")])

    def test_an_english_opener_is_not_taken_for_a_german_closer(self):
        # The reverse mistake: a `“` followed by `”` opens English speech even
        # while a German pair is pending. A greedy closer scan either drops it
        # or cuts the German utterance short; both lose real speech.
        for source, expected in [
            ("A stray „ mark here. “Real speech here.” she said.",
             ["Real speech here."]),
            ("„He said “Hi” to me.“ she said.",
             ["He said “Hi” to me."]),
        ]:
            with self.subTest(source=source):
                self.assertEqual(expected,
                    [source[a:b] for a, b in spoken_spans(source, "paired_quotes")])

    def test_a_detected_span_must_be_a_real_range_in_the_source(self):
        # The guard runs on raw detection, before the sort and merge, because
        # after the merge nothing can be wrong and a check there never fires.
        for spans in ([(4, 2)], [(2, 2)], [(-1, 2)], [(0, 11)], [(1.0, 2)],
                      [(1, True)]):
            with self.subTest(spans=spans):
                with self.assertRaises(ValueError):
                    validate_spoken_spans(spans, "0123456789")
        self.assertTrue(validate_spoken_spans([], "0123456789"))
        # Overlap and order are the merge's business: two conventions may mark
        # the same line, and only the merged map has to be ordered.
        self.assertTrue(validate_spoken_spans([(2, 5), (4, 6)], "0123456789"))
        self.assertTrue(validate_spoken_spans([(0, 5), (5, 10)], "0123456789"))

    def test_a_broken_detection_raises_before_the_merge_can_hide_it(self):
        # Not just the function: the guard sits on the path `spoken_spans`
        # takes, so a detection returning garbage raises instead of being
        # tidied into a plausible map.
        text = 'She waited. “Get down!” he shouted.'
        for broken in ([(5, 0)], [(-1, 3)], [(0, 500)], [(1.5, 3)]):
            with self.subTest(broken=broken):
                with patch("dialogue_spans._paired_spans", return_value=broken):
                    with self.assertRaises(ValueError):
                        spoken_spans(text, "paired_quotes")

    def test_a_short_pair_across_narration_is_refused_too(self):
        # The length cap exempted this: a short pair could swallow a narration
        # paragraph sitting between two speeches. Length is not evidence.
        text = '“Hello.\n\nShe left without answering.\n\n“Goodbye.”'
        self.assertEqual(["Goodbye."],
                         [text[a:b] for a, b in spoken_spans(text, "paired_quotes")])

    def test_a_blank_line_is_a_break_however_the_source_spells_it(self):
        # `"\n\n"` alone was blind to a blank line carrying a space or tab,
        # and to CRLF, so on those sources the refusal below was silently
        # optional while every fixture here wrote "\n\n".
        for gap in ("\n\n", "\n \n", "\n\t\n", "\r\n\r\n", "\r\r"):
            with self.subTest(gap=gap):
                text = f"“One.{gap}Narration here.{gap}“Two.”"
                self.assertEqual(["Two."],
                                 [text[a:b] for a, b in spoken_spans(text, "paired_quotes")])
            with self.subTest(reopened=gap):
                text = f"“One.{gap}“Two.” he said."
                self.assertEqual([f"One.{gap}“Two."],
                                 [text[a:b] for a, b in spoken_spans(text, "paired_quotes")])

    def test_a_straight_or_full_width_speech_that_reopens_its_mark_is_one_span(self):
        # Straight quotes can only be paired by position, and the old rule
        # banned every blank line in a straight pair instead of asking who
        # re-opened it - so the first paragraph of every continued speech in
        # such a book was read as narration. A distinct-mark pair answers the
        # same question, and the full-width pair added here inherits it.
        for mark in ('"', "＂"):
            with self.subTest(mark=mark):
                text = f"{mark}One.\n\n{mark}Two.{mark} he said."
                self.assertEqual([f"One.\n\n{mark}Two."],
                                 [text[a:b] for a, b in spoken_spans(text, "paired_quotes")])

    def test_a_straight_pair_across_narration_is_still_refused(self):
        # The mushoku18 failure the old blanket rule existed for: a shifted
        # pair must not swallow narration. The break it crosses is not
        # re-opened, so the check that keeps a continued speech refuses it, and
        # the real pair after it survives.
        text = '"Never closed.\n\nNarration here.\n\n"Real line." she said.'
        self.assertEqual(["Real line."],
                         [text[a:b] for a, b in spoken_spans(text, "paired_quotes")])

    def test_narration_after_a_german_speech_is_narration_not_other_speech(self):
        # German puts the closing mark where English puts the opening one.
        # Reading every “ as an unclosed opener marked the rest of its
        # paragraph as speech-by-another-convention: neither spoken nor
        # narration, so apply_source_narration had nothing to correct and every
        # speech logged a phantom review.
        source = "Er zögerte. „Komm sofort her!“ sagte er leise.\n\nDann ging er weg.\n"
        self.assertEqual(["Komm sofort her!"],
                         [source[a:b] for a, b in spoken_spans(source, "paired_quotes")])
        self.assertEqual([], [source[a:b] for a, b in
                              get_other_speech_spans(source, "paired_quotes")])
        marked = mark_entries([{"speaker": "ALICE", "text": "sagte er leise."}],
                              source, "paired_quotes")
        self.assertIs(False, marked[0]["spoken"])
        self.assertNotIn("dialogue_ambiguity", marked[0])

    def test_a_printed_name_before_every_opening_mark_is_found(self):
        # The label lookahead listed only the marks paired when it was written,
        # so a German transcript's own `Name „line“` got no label at all -
        # the one exact attribution signal those files print.
        for mark in ("“", "„", "＂", "「"):
            with self.subTest(mark=mark):
                source = "\n".join(f"Alice {mark}Line {i}.{mark}" for i in range(6))
                self.assertEqual(6, len(speaker_labels(source, speaker_names=["ALICE"])))

    def test_a_multi_paragraph_speech_that_does_not_reopen_the_quote_is_refused(self):
        # The cost of the rule, stated rather than discovered later: typography
        # that opens a speech once and closes it paragraphs later is refused.
        # End to end the first paragraph is flagged `other_convention` with no
        # spoken verdict (it keeps its character voice, pending review), while
        # later paragraphs go to NARRATOR; a book whose refusals drop it below
        # the detection threshold gets no speech map at all. Kept below the old
        # 2,000-character cap; nothing here claims that was wrong.
        text = '“This speech runs on.\n\nAnd on, in a second paragraph,\n\nand closes here.” he said.'
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

    def test_source_span_maps_collapsed_whitespace_back_to_raw_text(self):
        source = 'He said, “Hello\t  wide\nworld.” Then left.'
        marked = mark_entries(
            [{"speaker": "A", "text": "Hello wide world."}], source,
            "paired_quotes")
        start, end = marked[0]["source_span"]
        self.assertEqual("Hello\t  wide\nworld.", source[start:end])
        self.assertTrue(marked[0]["spoken"])

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

    def test_roster_confirmed_unicode_labels_keep_inventory_and_repeat_guards(self):
        from dialogue_spans import apply_dialogue_map
        for name in ('ELODIE', 'ÉLODIE', 'АННА', 'ÉLODIE АННА'):
            with self.subTest(name=name):
                lines = [f'Synthetic line {i}.' for i in range(6)]
                source = '\n'.join(f'{name} “{line}”' for line in lines)
                entries = [{'text': line, 'speaker': 'NARRATOR'} for line in lines]
                mapped = apply_dialogue_map(entries, source, speaker_names=[name])
                self.assertEqual([name] * 6, [row['speaker'] for row in mapped['entries']])
                self.assertEqual(6, len(speaker_labels(source, speaker_names=[name])))
                self.assertEqual([], speaker_labels(source, speaker_names=['OTHER']))
                self.assertEqual([], speaker_labels('\n'.join(source.splitlines()[:2]), speaker_names=[name]))
                self.assertEqual(['NARRATOR'] * 6, [row['speaker'] for row in entries])


    def test_repeated_adverbs_cannot_create_authoritative_speakers(self):
        import copy
        from dialogue_spans import apply_dialogue_map
        for word in ('Perhaps','Meanwhile','Nevertheless','Apparently','Possibly'):
            with self.subTest(word=word):
                lines=[f'Actual spoken line {i}.' for i in range(6)]
                source='\n\n'.join(f'{word} “{line}”' for line in lines)
                entries=[{'speaker':'ALICE','text':line} for line in lines];before=copy.deepcopy(entries)
                result=apply_dialogue_map(entries,source)
                self.assertEqual(['ALICE']*6,[row['speaker'] for row in result['entries']])
                self.assertEqual([],result['speaker_changes']);self.assertEqual(6,result['spoken'])
                self.assertTrue(all('source_speaker' not in row for row in result['entries']))
                self.assertEqual([],speaker_labels(source,speaker_names=['ALICE']))
                self.assertEqual(before,entries)

    def test_corroborated_labels_still_correct_other_entries_and_ignore_unknown_adverbs(self):
        from dialogue_spans import apply_dialogue_map
        lines=[(prefix,f'{prefix} line {i}.') for i in range(4)
               for prefix in ('Alice','Perhaps','Meanwhile')]
        source='\n\n'.join(f'{prefix} “{line}”' for prefix,line in lines)
        entries=[{'speaker':'ALICE' if i==0 else 'NARRATOR','text':line}
                 for i,(_prefix,line) in enumerate(lines)]
        result=apply_dialogue_map(entries,source)
        self.assertEqual(3,len(result['speaker_changes']))
        for (prefix,line),row in zip(lines,result['entries']):
            self.assertEqual('ALICE' if prefix=='Alice' else 'NARRATOR',row['speaker'])
            self.assertEqual(line,source[slice(*row['source_span'])]);self.assertTrue(row['spoken'])
            if prefix!='Alice':self.assertNotIn('source_speaker',row)

    def test_explicit_inventory_can_establish_real_name_that_is_also_a_sentence_word(self):
        source='\n\n'.join(f'The “A line {i}.”' for i in range(6))
        entries=[{'speaker':'NARRATOR','text':'A line 0.'}]
        marked=mark_entries(iter(entries),source,'paired_quotes',speaker_names=iter(['THE']))
        fixed,changes=apply_source_speakers(marked)
        self.assertEqual('THE',fixed[0]['speaker']);self.assertEqual(1,len(changes))

    def test_generator_entries_use_same_inventory_without_consuming_the_rows(self):
        entries=({'speaker':'ALICE','text':f'Line {i}.'} for i in range(6))
        source='\n'.join(f'Alice “Line {i}.”' for i in range(6))
        marked=mark_entries(entries,source,'paired_quotes')
        self.assertEqual(6,len(marked));self.assertTrue(all(row.get('source_speaker')=='Alice' for row in marked))

    def test_remapping_drops_old_uncorroborated_source_speaker_without_mutating_input(self):
        import copy
        source='\n'.join(f'Perhaps “Line {i}.”' for i in range(6))
        entries=[{'speaker':'ALICE','text':'Line 0.','source_speaker':'Perhaps'}];before=copy.deepcopy(entries)
        mapped=mark_entries(entries,source,'paired_quotes')
        fixed,changes=apply_source_speakers(mapped)
        self.assertNotIn('source_speaker',mapped[0]);self.assertEqual('ALICE',fixed[0]['speaker'])
        self.assertEqual([],changes);self.assertEqual(before,entries)
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
        names = {name for _, name in speaker_labels(self.TRANSCRIPT,speaker_names=['SUBARU','PETRA'])}
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
        marked = mark_entries(entries, self.TRANSCRIPT, "paired_quotes",speaker_names=['SUBARU','PETRA'])
        fixed, changes = apply_source_speakers(marked)
        self.assertEqual("SUBARU", fixed[0]["speaker"])
        self.assertEqual("printed_speaker_label", changes[0]["type"])

    def test_entries_without_a_label_are_untouched(self):
        entries = [{"speaker": "NARRATOR", "text": "Nothing here matches."}]
        fixed, changes = apply_source_speakers(
            mark_entries(entries, self.TRANSCRIPT, "paired_quotes"))
        self.assertEqual("NARRATOR", fixed[0]["speaker"])
        self.assertEqual([], changes)


class FullEntrySourceMappingTests(unittest.TestCase):
    def test_long_shared_prefix_uses_full_text_for_speaker_and_raw_span(self):
        import copy
        prefix = "This is the same long opening of a different passage. " * 4
        narration = prefix + "narration ending."
        spoken = [prefix + f"spoken ending {number}." for number in range(6)]
        source = narration + "\n" + "\n".join('Alice “' + line + '”' for line in spoken)
        self.assertTrue(uses_speaker_labels(source,speaker_names=['ALICE']))
        entries = [{"speaker": "WRONG", "text": line} for line in spoken]
        original = copy.deepcopy(entries)
        marked = mark_entries(entries, source, "paired_quotes",speaker_names=['ALICE'])
        for line, row in zip(spoken, marked):
            self.assertTrue(row["spoken"])
            self.assertEqual("Alice", row["source_speaker"])
            self.assertEqual(line, source[slice(*row["source_span"])])
        self.assertEqual(original, entries)
        applied, changes = apply_source_speakers(marked)
        self.assertEqual(["ALICE"] * 6, [row["speaker"] for row in applied])
        self.assertEqual(6, len(changes))

    def test_shared_prefix_cannot_establish_an_unwritten_suffix(self):
        prefix = "A repeating introductory phrase with no unique identity. " * 4
        source = '“' + prefix + 'authored ending.”'
        entries = [{"text": prefix + "invented ending.", "speaker": "UNKNOWN"}]
        marked = mark_entries(entries, source, "paired_quotes")
        self.assertNotIn("spoken", marked[0])
        self.assertNotIn("source_span", marked[0])
        self.assertNotIn("source_speaker", marked[0])


class ConventionValidationTests(unittest.TestCase):
    def test_unknown_explicit_convention_is_rejected(self):
        text = 'ANNA: Hello there.\n— Dash speech.\n“Quoted speech.”'
        for convention in ('dash', 'quote', '', 'LABEL_LINES', 7, []):
            with self.subTest(convention=convention):
                with self.assertRaises(ValueError):
                    spoken_spans(text, convention)
                with self.assertRaises(ValueError):
                    mark_entries([{'text': 'Hello there.'}], text, convention)

    def test_each_supported_convention_preserves_offsets(self):
        text = 'ANNA: Hello there.\n— Dash speech.\n“Quoted speech.”'
        for convention, expected in (('label_lines', 'Hello there.'),
                                     ('dash_lines', 'Dash speech.'),
                                     ('paired_quotes', 'Quoted speech.')):
            with self.subTest(convention=convention):
                self.assertEqual([expected], [text[a:b] for a,b in spoken_spans(text, convention)])
        self.assertEqual([], spoken_spans('Plain narration.', None))


class SourceNarrationTest(unittest.TestCase):
    """#1534: narration the model typed as speech must not keep a character voice."""

    def test_narration_given_a_character_is_returned_to_the_narrator(self):
        source = ("He wondered how charming the boy had been at dinner. "
                  "“You are late,” said Basil. “Sit down.”") + " " + "“Line.” " * 5
        entries = [{"speaker": "LORD HENRY", "text": "He wondered how charming the boy had been at dinner."},
                   {"speaker": "BASIL", "text": "You are late,"},
                   {"speaker": "NARRATOR", "text": "said Basil."},
                   {"speaker": "BASIL", "text": "Sit down."}]
        result = apply_dialogue_map(entries, source)
        self.assertEqual(["NARRATOR", "BASIL", "NARRATOR", "BASIL"],
                         [row["speaker"] for row in result["entries"]])
        self.assertEqual([{"type": "source_narration", "before": "LORD HENRY", "after": "NARRATOR"}],
                         result["narration_changes"])
        self.assertEqual("LORD HENRY", entries[0]["speaker"])
        # Speech marked another way lies outside the main quotes without being
        # narration: it is unestablished, so neither this reset nor the review
        # guard may give it to the narrator.
        from review_script import get_review_source_narration
        pad = " " + "“Line.” " * 5
        for source, rows in (
                ("“Come here,” said Tom. ‘No, I don’t want to,’ Anna said." + pad,
                 [("TOM", "Come here,"), ("NARRATOR", "said Tom."), ("ANNA", "No, I don’t want to,"),
                  ("NARRATOR", "Anna said.")]),
                ("Tom turned.\n—Where are you going? he asked." + pad,
                 [("NARRATOR", "Tom turned."), ("TOM", "Where are you going?")]),
                (pad + "\n\n“Wait for me, Tom said\n\nThe road went on.",
                 [("TOM", "Wait for me, Tom said"), ("NARRATOR", "The road went on.")])):
            with self.subTest(source=source[:30]):
                result = apply_dialogue_map([{"speaker": s, "text": t} for s, t in rows], source)
                self.assertEqual([s for s, _t in rows], [row["speaker"] for row in result["entries"]])
                self.assertEqual([], result["narration_changes"])
                character = next(row for row in result["entries"] if row["speaker"] not in ("NARRATOR",))
                if character["text"] != "Come here,":
                    self.assertEqual("other_convention", character["dialogue_ambiguity"])
                    self.assertIsNone(get_review_source_narration(character))
        # A closed empty pair is not an unclosed opener: the narration after it
        # is still narration.
        result = apply_dialogue_map([{"speaker": "ANNA", "text": "The road was dark."}],
                                    "“Line.”\n\n" * 5 + "An empty pair “” stood on the sign. The road was dark.")
        self.assertEqual(("NARRATOR", False), (result["entries"][0]["speaker"], result["entries"][0]["spoken"]))
        # Each paired span is visited once, not once per opener: well-formed
        # quotes must not make the unclosed check quadratic.
        import dialogue_spans
        visits = []

        class Counted(list):
            def __iter__(self):
                for item in list.__iter__(self):
                    visits.append(item)
                    yield item

        original = dialogue_spans._paired_spans
        with patch.object(dialogue_spans, "_paired_spans", lambda text: Counted(original(text))):
            for repeats in (500, 2000):
                visits.clear()
                dialogue_spans.get_other_speech_spans("“A.”\n\n" * repeats, "paired_quotes")
                self.assertLessEqual(len(visits), repeats, repeats)
        # Unclosed marks in one paragraph also share one forward search. The
        # search is the module's single blank-line lookup, so it is counted
        # there rather than at one string method: the property is one search
        # per paragraph, not one particular `find` call.
        paragraph_searches = []
        original_blank_line = dialogue_spans._next_blank_line

        def counted_blank_line(search_text, start):
            paragraph_searches.append(start)
            return original_blank_line(search_text, start)

        for opener in ("“", "「", "『"):
            for repeats in (500, 2000):
                paragraph_searches.clear()
                text = (opener + "Wait ") * repeats
                with patch.object(dialogue_spans, "_next_blank_line", counted_blank_line):
                    spans = dialogue_spans.get_other_speech_spans(text, "paired_quotes")
                self.assertEqual(repeats, len(spans))
                self.assertTrue(all(stop == len(text) for _start, stop in spans))
                self.assertEqual(1, len(paragraph_searches))
            paragraph_searches.clear()
            text = f"{opener}Wait {opener}stay\n\n{opener}Go {opener}now"
            with patch.object(dialogue_spans, "_next_blank_line", counted_blank_line):
                spans = dialogue_spans.get_other_speech_spans(text, "paired_quotes")
            boundary = text.index("\n\n")
            self.assertEqual([boundary, boundary, len(text), len(text)], [end for _start, end in spans])
            self.assertEqual(2, len(paragraph_searches))

    def test_unestablished_and_printed_lines_are_left_alone(self):
        # No `spoken` key means the source did not answer; a printed label is
        # the source's own answer. Neither is overridden.
        rows = [{"speaker": "ALICE", "text": "unlocated"},
                {"speaker": "ALICE", "text": "x", "dialogue_ambiguity": "interior_dash_turn"},
                {"speaker": "ALICE", "text": "y", "spoken": False, "source_speaker": "Alice"},
                {"speaker": "ALICE", "text": "z", "spoken": True}]
        updated, changes = apply_source_narration(rows)
        self.assertEqual([], changes)
        self.assertEqual(rows, updated)

    def test_review_guard_reads_the_same_fact(self):
        from review_script import get_review_source_narration
        for spoken, expected in ((False, True), (True, False), (None, None), ("yes", None)):
            row = {} if spoken is None else {"spoken": spoken}
            self.assertIs(expected, get_review_source_narration(row))

