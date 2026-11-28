"""The dialogue map is built from the source, not from what the model returned.

Generation is told to drop the outermost dialogue quotes, and follows that
unevenly - 22%, 16% and 1% retention across three books - so the fact that a
line was speech is destroyed rather than recorded. Rebuilding it afterwards
from the model's own prose is what made today's attribution scorer wrong three
times. The source still has the marks: arc4_volume10wn carries 6,925 of them
while its generated entries kept 16.
"""
import json
import random
import re
import unittest
from pathlib import Path
from unittest.mock import patch

import dialogue_spans
from dialogue_spans import (apply_dialogue_map, apply_source_narration, apply_source_speakers,
                            detect_convention, get_other_speech_spans, mark_entries,
                            speaker_labels, spoken_spans, uses_speaker_labels,
                            validate_spoken_spans)


def _main_get_other_speech_spans(text, convention):
    """Frozen origin/main (c0085045) original, used only as a differential.

    #2360 is the only known difference: this version appends an unclosed
    opener even when its paragraph body is empty or whitespace-only, which
    returns the zero-length (start, start) range validate_spoken_spans refuses.
    """
    if convention is None:
        return []
    pairs = sorted(dialogue_spans._paired_spans(text))
    closed = {stop for _begin, stop in pairs}
    merged = []
    for begin, stop in pairs:
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
        else:
            merged.append((begin, stop))
    starts = [begin for begin, _stop in merged]

    def inside_primary(start, end):
        index = dialogue_spans.bisect_right(starts, start) - 1
        return index >= 0 and merged[index][1] >= end

    spans = [(match.start(1), match.end(1))
             for pattern in dialogue_spans.OTHER_PAIRED
             for match in pattern.finditer(text)
             if dialogue_spans.BLANK_LINE.search(text, match.start(1), match.end(1)) is None
             and not inside_primary(match.start(1), match.end(1))]
    if convention != "dash_lines":
        spans += [(match.start(1), match.end(1))
                  for match in dialogue_spans.DASH_LINE.finditer(text)]
    if convention != "paired_quotes":
        spans += pairs
    else:
        for opener, closer in dialogue_spans.UNCLOSED_OPENERS.items():
            paragraph_break = None
            for match in re.finditer(re.escape(opener), text):
                start = match.start() + 1
                if text.startswith(closer, start):
                    continue
                if match.start() in closed:
                    continue
                index = dialogue_spans.bisect_right(starts, start) - 1
                if index >= 0 and merged[index][1] >= start:
                    continue
                if paragraph_break is None or 0 <= paragraph_break < start:
                    paragraph_break = dialogue_spans._next_blank_line(text, start)
                spans.append((start, len(text) if paragraph_break == -1 else paragraph_break))
    return spans


def _has_paragraph_body(text, start, end):
    return _NON_WHITESPACE.search(text, start, end) is not None


_NON_WHITESPACE = re.compile(r"\S")


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

    def test_a_german_speech_holding_an_inch_mark_is_not_dropped(self):
        # German low-high (`„…“`) shares `“` with the English opener `“…”` and
        # is scoped out of inch-mark skipping so `main`'s behaviour is preserved
        # (#2357): each speech closes at the digit mark instead of being dropped
        # when `_ambiguous_mark_roles` classifies the real closer as an opener.
        prefix = '„Hallo.“ sagte er.\n\n„Guten Tag.“ sagte sie.\n\n'
        for body, spoken in [
                ('„It is 6“ wide“ she said.', 'It is 6'),
                ('„It is 6“ wide“, sagte er.', 'It is 6'),
                ('„It is 6“ wide.“ Er ging.', 'It is 6'),
                ('„It is 6“ wide“... Er ging.', 'It is 6'),
                ('„It is 6“ by 8“ board“ he said.', 'It is 6'),
                ('„We leave at 6“.” he said', 'We leave at 6')]:
            with self.subTest(body=body):
                source = prefix + body
                self.assertEqual(['Hallo.', 'Guten Tag.', spoken],
                                 [source[a:b] for a, b in
                                  spoken_spans(source, "paired_quotes")])
        # The gate's five-pair German prefix crosses MIN_CONVENTION_PAIRS, so
        # the low-high density filter is not what kept the speech.
        many = ''.join(f'„Wort{i} hier.“ sagte er.\n\n' for i in range(5))
        source = many + '„It is 6“ wide“ she said.'
        self.assertIn('It is 6', [source[a:b] for a, b in
                                  spoken_spans(source, "paired_quotes")])

    def test_german_low_high_inch_mark_does_not_merge_or_drop_later_speech(self):
        # Gate-8 merge cases: with any later German speech, skipping `6“` while
        # `_ambiguous_mark_roles` rejects the real closer merged narration and
        # the next speech into one span (or dropped the first speech across a
        # narration paragraph break). Scoping out German low-high restores
        # `main`'s exact spans (#2357).
        prefix = '„A“, sagte er. „B“, sagte sie. „C.“ „D.“ „E.“ '
        expected_prefix = ['A', 'B', 'C.', 'D.', 'E.']
        cases = [
            ('„Es ist 6“ breit“, sagte er. Dann „Komm“, sagte sie.',
             ['Es ist 6', 'Komm']),
            ('„Es ist 6“ breit“, sagte er.\n\n„Komm“, sagte sie.',
             ['Es ist 6', 'Komm']),
            ('„Es ist 6“ breit“, sagte er.\n\nEr ging. „Komm“, sagte sie.',
             ['Es ist 6', 'Komm']),
            ('„Ein 6“ Brett.“ Dann „Komm“, sagte sie.',
             ['Ein 6', 'Komm']),
            ('„Er hat 6“ gesagt“, meinte sie. Dann „Komm“, sagte sie.',
             ['Er hat 6', 'Komm']),
            ('„It is 6“ wide“ she said. „Fine.“',
             ['It is 6', 'Fine.']),
        ]
        for body, expected_tail in cases:
            with self.subTest(body=body):
                source = prefix + body
                self.assertEqual(expected_prefix + expected_tail,
                                 [source[a:b] for a, b in
                                  spoken_spans(source, "paired_quotes")])
        self.assertFalse(dialogue_spans.is_inch_mark(
            '„Es ist 6“ breit“, sagte er.', 9, '“', True, opener='„'))
        self.assertFalse(dialogue_spans.is_inch_mark(
            'Es ist 6“ breit', 8, '“', False))

    def test_a_later_german_pair_does_not_reopen_the_reply_mark(self):
        # The bracket reply `(“nein”)` must not hold the German speech open
        # even when a later German pair follows (#2318 review, #2357).
        prefix = '„Hallo.“ sagte er.\n\n'
        source = prefix + '„Zimmer 5“ sagte er. (“nein”). „Gut.“ sagte sie.'
        self.assertEqual(['Hallo.', 'Zimmer 5', 'nein', 'Gut.'],
                         [source[a:b] for a, b in
                          spoken_spans(source, "paired_quotes")])
        # In curly high (`“…”`), an opener-shaped closer (`”no”`) later in the
        # paragraph must likewise not hold `“Room 5”` open (#2318).
        curly = '“Hello.” he said.\n\n“Room 5” he said. Later, ”no” came back.'
        self.assertEqual(['Hello.', 'Room 5'],
                         [curly[a:b] for a, b in
                          spoken_spans(curly, "paired_quotes")])

    def test_full_width_inch_mark_keeps_measurement_and_does_not_invert(self):
        # Full-width quotes (`＂…＂`) keep an interior inch mark as text both on
        # its own and when another full-width speech follows (#2318).
        for source, expected in [
                ('＂It is 6＂ wide＂, she said.', ['It is 6＂ wide']),
                ('＂It is 6＂ wide＂, she said. ＂Fine.＂',
                 ['It is 6＂ wide', 'Fine.'])]:
            with self.subTest(source=source):
                self.assertEqual(expected,
                                 [source[a:b] for a, b in
                                  spoken_spans(source, "paired_quotes")])

    def test_a_real_closer_before_a_sentence_end_stays_a_known_limit(self):
        # Known limit, identical on main and head: the shape rule reads a real
        # closer followed directly by `.`/`…`/`'` as an opener, so only the
        # text up to the inch mark is spoken. Fixing it would unpick the rule
        # that lets a reply follow a no-space stop (`he said."...No."`), and
        # the fallback for the German drop does not reach it (#2318 review).
        prefix = '"Hello." he said.\n\n'
        for body, expected in [
                ('"It is 6" wide". She left.', 'It is 6'),
                ('"It is 6" wide"... She left.', 'It is 6'),
                ('"It is 6" wide"\'s fine.', 'It is 6')]:
            with self.subTest(body=body):
                source = prefix + body
                self.assertEqual(['Hello.', expected],
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
        # A CJK numeral is a letter (Lo), a Roman numeral is a letter number
        # (Nl), and a prime mark is punctuation (Po), so a quote after any of
        # them is treated as a delimiter rather than an inch mark.
        for char in ("三", "Ⅻ", "′"):
            with self.subTest(char=char):
                self.assertFalse(dialogue_spans.is_number_char(char))

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


class UnclosedOpenerTest(unittest.TestCase):
    """#2360: an unclosed opener with no paragraph body is not speech."""

    OPENERS = ("“", "「", "『", "„")

    def _no_body_texts(self):
        for opener in self.OPENERS:
            yield "alone", opener
            yield "end of text", "text " + opener
            yield "before a blank line", opener + "\n\nmore"
            yield "before a whitespace-only paragraph", opener + "   \n\nmore"
            yield "before trailing whitespace", opener + " \t "

    def test_every_unclosed_opener_with_no_body_yields_no_span(self):
        # The issue's three shapes, for each of the four openers the module
        # treats as unclosed: alone, at the end of the text, and immediately
        # before a blank line.
        for opener in self.OPENERS:
            for shape, text in (("alone", opener),
                                ("end of text", "text " + opener),
                                ("before a blank line", opener + "\n\nmore")):
                with self.subTest(opener=opener, shape=shape):
                    spans = get_other_speech_spans(text, "paired_quotes")
                    self.assertEqual([], spans)
                    self.assertIs(True, validate_spoken_spans(spans, text))

    def test_an_opener_before_a_whitespace_only_paragraph_yields_no_span(self):
        # mark_entries normalizes and strips whitespace before matching, so a
        # whitespace-only body has no words to speak and is treated as empty.
        for opener in self.OPENERS:
            for body in (" ", "   ", "\t", " \t "):
                for text in (f"{opener}{body}\n\nmore", f"{opener}{body}"):
                    with self.subTest(opener=opener, body=body, text=text):
                        self.assertEqual(
                            [], get_other_speech_spans(text, "paired_quotes"))

    def test_an_unclosed_opener_with_a_body_still_yields_its_span(self):
        # Unchanged behaviour: the words after the mark are still another
        # convention's speech, up to the paragraph end.
        for text, expected in (("“Wait for me", "Wait for me"),
                               ("“Wait for me\n\nNext paragraph.", "Wait for me"),
                               ("「待って", "待って"),
                               ("『まって", "まって"),
                               ("„Warte", "Warte")):
            with self.subTest(text=text):
                spans = get_other_speech_spans(text, "paired_quotes")
                self.assertEqual([expected], [text[start:end] for start, end in spans])
                self.assertIs(True, validate_spoken_spans(spans, text))

    def test_a_line_straddling_a_no_body_opener_keeps_mains_verdict(self):
        # Gate 1 (#2380 repair): mark_entries' overlap test is keyed on the
        # ENTRY's end, so main marked a line straddling a zero-length span
        # "other_convention", not narration. Returning no span must not change
        # what reaches the audio, so the line is still ambiguous. The last two
        # cases show the boundary: a line that stops at the mark is narration,
        # and a line inside a real pair stays spoken.
        for text, texts, verdicts in (
                ('He said “\n\nHello there.',
                 ["He said Hello there.", "said Hello"],
                 ["other_convention", "other_convention"]),
                ('He said “   \n\nHello there.', ["said Hello"],
                 ["other_convention"]),
                ('“A” he said “\n\n“B” she said.',
                 ["A", "he said B", "she said."],
                 ["spoken", "spoken", "narration"]),
                ('He said “', ["He said"], ["narration"])):
            with self.subTest(text=text):
                self.assertEqual([], get_other_speech_spans(text, "paired_quotes"))
                marked = mark_entries([{"text": entry_text} for entry_text in texts],
                                      text, "paired_quotes")
                actual = ["other_convention" if row.get("dialogue_ambiguity")
                          else "spoken" if row.get("spoken") else "narration"
                          for row in marked]
                self.assertEqual(verdicts, actual)

    _STRADDLE_PREFIXES = ("He said ", "She said ", "Tom muttered ", "Er sagte ",
                          "“A” then ", "“A” he said ")
    _STRADDLE_SUFFIXES = ("Hello there.", "Hello", "there.", "Wait for me",
                          "Zimmer fünf", "A")

    def _straddling_cases(self, count):
        """-> (text, entries) pairs whose entries cross an empty unclosed opener."""
        rng = random.Random(2380)
        cases = [('He said “\n\nHello there.', [{"text": "He said Hello there."}]),
                 ('He said “\n\nHello there.', [{"text": "said Hello"}]),
                 ('He said “   \n\nHello there.', [{"text": "said Hello"}])]
        for _ in range(count):
            opener = rng.choice(self.OPENERS)
            body = rng.choice(("", " ", "\t", "   ", " \t ", "　", "\u00a0"))
            prefix = rng.choice(self._STRADDLE_PREFIXES)
            suffix = rng.choice(self._STRADDLE_SUFFIXES)
            text = (prefix + opener + body
                    + rng.choice(("\n\n", "\n \n", ""))
                    + suffix
                    + rng.choice(("", "\n\n“B” she said.", "\n\nmore words.")))
            cases.append((text, [
                {"speaker": "A", "text": prefix.strip() + " " + suffix},
                {"speaker": "A", "text": prefix.strip()},
                {"speaker": "B", "text": suffix},
            ]))
        return cases

    def test_mark_entries_matches_origin_main_on_straddling_lines(self):
        # Gate 1 differential (#2380 repair): entries built deliberately to
        # straddle an unclosed opener with no body. main marked these lines
        # "other_convention"; the contract fix must leave every entry's output
        # byte-identical, including source_span and speaker fields.
        for index, (text, entries) in enumerate(self._straddling_cases(600)):
            with self.subTest(index=index, text=text):
                used = []

                def frozen_main(value, convention, _used=used):
                    _used.append(value)
                    return _main_get_other_speech_spans(value, convention), []

                with patch.object(dialogue_spans, "_other_speech_spans", frozen_main):
                    expected = mark_entries([dict(entry) for entry in entries],
                                            text, "paired_quotes")
                actual = mark_entries([dict(entry) for entry in entries], text,
                                      "paired_quotes")
                self.assertTrue(used, "mark_entries did not read the span source")
                self.assertEqual(expected, actual)

    @staticmethod
    def _fixture_text(path):
        raw = Path(path).read_bytes()
        for encoding in ("utf-8", "cp1252"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")

    def _repo_fixture_texts(self):
        # Synthetic fixtures already in the repo: the text-import fuzz seeds,
        # and the synthetic dialogue-continuity passage joined into one source.
        root = Path(__file__).resolve().parents[2]
        seeds = sorted((root / "tools" / "fuzz" / "corpus" / "txt_import").glob("*.txt"))
        self.assertGreaterEqual(len(seeds), 4)
        texts = [self._fixture_text(path) for path in seeds]
        passage = root / "app" / "experiments" / "fixtures" / "dialogue_continuity_passage.json"
        entries = json.loads(passage.read_text(encoding="utf-8"))["entries"]
        texts.append("\n\n".join(entry["text"] for entry in entries))
        return texts

    _GENERATED_ALPHABET = ("abcXYZ.,;:!?…—–-'\"0123456789\t\n\r"
                           + "".join(dict.fromkeys(
                               mark for pair in dialogue_spans.PAIRED for mark in pair)))

    def _generated_texts(self, count):
        rng = random.Random(2360)
        alphabet = self._GENERATED_ALPHABET
        return ["".join(rng.choice(alphabet) for _ in range(rng.randint(0, 90)))
                for _ in range(count)]

    def test_non_empty_spans_are_byte_identical_to_origin_main(self):
        # Differential against a frozen origin/main original. Only spans whose
        # paragraph body is empty or whitespace-only may disappear; every other
        # span must have the same offsets, in the same order.
        texts = (self._repo_fixture_texts()
                 + self._generated_texts(600)
                 + [text for _shape, text in self._no_body_texts()])
        self.assertGreaterEqual(len(texts), 500)
        for index, text in enumerate(texts):
            expected = [(start, end)
                        for start, end in _main_get_other_speech_spans(text, "paired_quotes")
                        if _has_paragraph_body(text, start, end)]
            actual = get_other_speech_spans(text, "paired_quotes")
            self.assertEqual(expected, actual, f"text #{index}: {text!r}")


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


class DashDialogueTagTests(unittest.TestCase):
    """#2329: split narrator tags out of dash dialogue only on tag evidence."""

    def test_issue_rows_split_narrator_tags_out_of_spoken_spans(self):
        cases = [
            ("—Hola —dijo Ana—. Vamos.", ["Hola", "Vamos."], "dijo Ana"),
            ("— Oui, répondit-il.", ["Oui,"], "répondit-il."),
            ("— Yes, he replied.", ["Yes,"], "he replied."),
        ]
        for line, expected_spoken, tag in cases:
            with self.subTest(line=line):
                spans = spoken_spans(line, "dash_lines")
                self.assertEqual(expected_spoken, [line[a:b] for a, b in spans])
                entries = [{"speaker": "ANA", "text": expected_spoken[0]},
                           {"speaker": "ANA", "text": tag}]
                if len(expected_spoken) > 1:
                    entries.append({"speaker": "ANA", "text": expected_spoken[1]})
                source = line + "\n" + "\n".join(f"— Turn {i}." for i in range(5))
                mapped = apply_dialogue_map(entries, source)
                self.assertEqual("dash_lines", mapped["convention"])
                self.assertTrue(mapped["entries"][0]["spoken"])
                self.assertFalse(mapped["entries"][1]["spoken"])
                self.assertEqual("NARRATOR", mapped["entries"][1]["speaker"])
                if len(expected_spoken) > 1:
                    self.assertTrue(mapped["entries"][2]["spoken"])

    def test_reporting_verbs_cover_english_spanish_french_past_and_present(self):
        cases = [
            ("— Yes, he said.", ["Yes,"], ["he said."]),
            ("— Yes, she says.", ["Yes,"], ["she says."]),
            ("— Wait, Ana replies.", ["Wait,"], ["Ana replies."]),
            ("— Go, whispered Tom.", ["Go,"], ["whispered Tom."]),
            ("— Stop, he asks.", ["Stop,"], ["he asks."]),
            ("—Hola —dijo Ana.", ["Hola"], ["dijo Ana."]),
            ("—Hola —dice Ana—. Vamos.", ["Hola", "Vamos."], ["dice Ana"]),
            ("—Sí —respondió Luis.", ["Sí"], ["respondió Luis."]),
            ("—Sí —responde el guía.", ["Sí"], ["responde el guía."]),
            ("—¿Quién? —preguntó Ana.", ["¿Quién?"], ["preguntó Ana."]),
            ("—¿Quién? —pregunta Ana.", ["¿Quién?"], ["pregunta Ana."]),
            ("— Oui, répondit-il.", ["Oui,"], ["répondit-il."]),
            ("— Oui, répond-il.", ["Oui,"], ["répond-il."]),
            ("— Non, dit-elle.", ["Non,"], ["dit-elle."]),
            ("— Venez, s'écria-t-il.", ["Venez,"], ["s'écria-t-il."]),
            ("— Venez, s’écrie-t-elle.", ["Venez,"], ["s’écrie-t-elle."]),
            ("— Chut, murmura-t-elle.", ["Chut,"], ["murmura-t-elle."]),
            ("— Bon, fit-il.", ["Bon,"], ["fit-il."]),
            ("— Bon, fit-elle.", ["Bon,"], ["fit-elle."]),
            ("— Bon, fit-on.", ["Bon,"], ["fit-on."]),
            ("–Hola –dijo Ana–. Vamos.", ["Hola", "Vamos."], ["dijo Ana"]),
            ("— Sí, —dijo Ana—. Vamos.", ["Sí,", "Vamos."], ["dijo Ana"]),
            ("— Sí, señor —dijo Ana—. Vamos.", ["Sí, señor", "Vamos."], ["dijo Ana"]),
            ("—Hola —dijo Ana—. Es tarde —añadió Juan.",
             ["Hola", "Es tarde"], ["dijo Ana", "añadió Juan."]),
        ]
        for line, expected_spoken, expected_narrator in cases:
            with self.subTest(line=line):
                parts, ambiguous = dialogue_spans.split_dash_line(line, 1, len(line))
                self.assertFalse(ambiguous)
                self.assertEqual(
                    expected_spoken,
                    [line[a:b] for kind, a, b in parts if kind == "SPOKEN"],
                )
                self.assertEqual(
                    expected_narrator,
                    [line[a:b] for kind, a, b in parts if kind == "NARRATOR"],
                )
                self.assertEqual(
                    expected_spoken,
                    [line[a:b] for a, b in spoken_spans(line, "dash_lines")],
                )

    @staticmethod
    def _parts(line):
        parts, ambiguous = dialogue_spans.split_dash_line(line, 1, len(line))
        return ambiguous, [(kind, line[a:b].strip()) for kind, a, b in parts]

    def test_word_limit_and_boundary_guards_before_reporting_verb(self):
        for line, expected_tag in (
            ("— Yes, said John.", "said John."),
            ("— Yes, he replied.", "he replied."),
            ("— Yes, old John replied.", "old John replied."),
        ):
            with self.subTest(accepted=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual(
                    [("NARRATOR", expected_tag)],
                    [part for part in parts if part[0] == "NARRATOR"],
                )
        for line in (
            "— Yes, the old man replied.",
            "—Hola —el viejo hombre dijo—. Vamos.",
            "— Hola, el muy tonto dijo que no.",
            "— Wait, I said nothing.",
            "— Wait, it won't fit.",
        ):
            with self.subTest(rejected=line):
                ambiguous, parts = self._parts(line)
                self.assertTrue(ambiguous)
                self.assertEqual(["SPOKEN"], [kind for kind, _text in parts])
        for line in (
            # The reporting verb is outside the 0-3 subject window, or is not a
            # reporting form: the line is plain speech, not an unverified tag.
            "— Yes, the tired old man replied.",
            "— Wait, no. He said nothing.",
            "— Please, don't ask.",
            "— Please, continue.",
        ):
            with self.subTest(no_candidate=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual(["SPOKEN"], [kind for kind, _text in parts])

    def test_comma_introduced_tag_resumes_speech_and_period_tag_stays_narrator(self):
        cases = (
            ("— Oui, répondit-il, je viens demain.",
             [("SPOKEN", "Oui,"), ("NARRATOR", "répondit-il,"),
              ("SPOKEN", "je viens demain.")]),
            ("— Non, dit-elle, jamais.",
             [("SPOKEN", "Non,"), ("NARRATOR", "dit-elle,"),
              ("SPOKEN", "jamais.")]),
            ("— Non, dit-elle en souriant.",
             [("SPOKEN", "Non,"), ("NARRATOR", "dit-elle en souriant.")]),
            ("— Que vous seriez bon, monsieur, dit la dame, "
             "de vouloir bien ramasser mon éventail.",
             [("SPOKEN", "Que vous seriez bon, monsieur,"),
              ("NARRATOR", "dit la dame,"),
              ("SPOKEN", "de vouloir bien ramasser mon éventail.")]),
            ("— Non, dit-elle. — Jamais.",
             [("SPOKEN", "Non,"), ("NARRATOR", "dit-elle."),
              ("SPOKEN", "Jamais.")]),
        )
        for line, expected in cases:
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual(expected, parts)

    def test_period_tag_with_continuation_is_not_vouched_for(self):
        line = "— Partez! dit Emma. C'est se moquer, je vous chasse."
        ambiguous, parts = self._parts(line)
        self.assertTrue(ambiguous)
        self.assertEqual(
            [("SPOKEN", "Partez!"),
             ("NARRATOR", "dit Emma. C'est se moquer, je vous chasse.")],
            parts,
        )

    def test_question_exclamation_and_ellipsis_introduced_tags(self):
        cases = (
            ("— Des moyens? répliqua M. Homais.",
             [("SPOKEN", "Des moyens?"), ("NARRATOR", "répliqua M. Homais.")]),
            ("— Artémise! criait la maîtresse d’auberge, de la voix.",
             [("SPOKEN", "Artémise!"),
              ("NARRATOR", "criait la maîtresse d’auberge,"),
              ("SPOKEN", "de la voix.")]),
            ("— Quel pauvre homme! quel pauvre homme! disait-elle tout bas, "
             "en se mordant les lèvres.",
             [("SPOKEN", "Quel pauvre homme! quel pauvre homme!"),
              ("NARRATOR", "disait-elle tout bas,"),
              ("SPOKEN", "en se mordant les lèvres.")]),
            ("— Ma cas... fit timidement le nouveau, promenant autour de lui.",
             [("SPOKEN", "Ma cas..."),
              ("NARRATOR", "fit timidement le nouveau,"),
              ("SPOKEN", "promenant autour de lui.")]),
            ("— Je ne sais pas… dit-il.",
             [("SPOKEN", "Je ne sais pas…"), ("NARRATOR", "dit-il.")]),
            ("— Vraiment! s'écria-t-il.",
             [("SPOKEN", "Vraiment!"), ("NARRATOR", "s'écria-t-il.")]),
        )
        for line, expected in cases:
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual(expected, parts)

    def test_false_tag_readings_inside_speech_stay_whole(self):
        for line in (
            "— Dije que no, dijo que sí",
            "— Wait, I said nothing.",
            "— Fine, that said nothing.",
            "— Rien, autrement dit.",
            "— Señor, dice Pepe que sea yo franco con usted.",
            "— Bueno, como dice el refrán, no hay mal que dure.",
            "— En efecto, le vi, y me dijeron su nombre.",
            "— Hola, el muy tonto dijo que no.",
            "— Hola, el viejo hombre dijo—. Vamos.",
            "— Oui, comme on dit, c'est vrai.",
        ):
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertTrue(ambiguous)
                self.assertEqual(["SPOKEN"], [kind for kind, _text in parts])
                self.assertEqual([line[2:].strip()], [text for _kind, text in parts])

    def test_first_person_reporting_forms_are_speech_not_tags(self):
        for line in (
            "— Pues allí tienes, digo, loca no, cuerda.",
            "— Dije que no.",
        ):
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual([("SPOKEN", line[2:].strip())], parts)

    def test_spanish_enclitic_tags(self):
        cases = (
            ("— Vete, preguntóle.", "preguntóle."),
            ("— Lárgate, díjole con rabia.", "díjole con rabia."),
            ("— Lárgate, contestóles.", "contestóles."),
            ("— Lárgate, preguntóle Ana.", "preguntóle Ana."),
        )
        for line, expected_tag in cases:
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual(
                    [("NARRATOR", expected_tag)],
                    [part for part in parts if part[0] == "NARRATOR"],
                )

    def test_a_later_unverified_break_keeps_the_line_ambiguous(self):
        for line in (
            "—Hola —dijo Ana— y el tren —tarde otra vez— llegó.",
            "—Hola —dijo Ana—. El tren —tarde otra vez— llegó.",
        ):
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertTrue(ambiguous)
                self.assertIn(("NARRATOR", "dijo Ana"), parts)
        ambiguous, parts = self._parts("—Hola —dijo Ana—. Vamos.")
        self.assertFalse(ambiguous)
        self.assertEqual(
            [("SPOKEN", "Hola"), ("NARRATOR", "dijo Ana"), ("SPOKEN", "Vamos.")],
            parts,
        )

    def test_negative_controls_and_degenerate_dash_lines(self):
        for line, expected_ambiguous in (
            ("— The train — late again — finally came.", True),
            ("— Wait — I forgot the keys.", True),
            ("— Wait, she turned away.", False),
            ("— I was going to—", False),
            ("— Primera línea.", False),
        ):
            with self.subTest(line=line):
                parts, ambiguous = dialogue_spans.split_dash_line(line, 1, len(line))
                self.assertEqual(expected_ambiguous, ambiguous)
                self.assertEqual([line[2:]], [line[a:b] for a, b in spoken_spans(line, "dash_lines")])
                self.assertEqual(["SPOKEN"], [kind for kind, _a, _b in parts])
        self.assertEqual([], spoken_spans("The train — late again — finally came.", "dash_lines"))
        paired = 'She sighed. "The train — late again — finally came." "Yes, he replied."'
        self.assertEqual(
            ["The train — late again — finally came.", "Yes, he replied."],
            [paired[a:b] for a, b in spoken_spans(paired, "paired_quotes")],
        )
        mixed = "“Line.”\n" * 5 + "— Oui, répondit-il."
        other = [mixed[a:b] for a, b in get_other_speech_spans(mixed, "paired_quotes")]
        self.assertEqual(["Oui,"], other)
        for edge in ("—   ", "— —dijo Ana.", "—Hola —dijo Ana—."):
            with self.subTest(edge=edge):
                spans = spoken_spans(edge, "dash_lines")
                self.assertIs(True, validate_spoken_spans(spans, edge))

    def test_period_tag_continuation_is_ambiguous_in_the_source_map(self):
        """The unverified tail must not be voiced as NARRATOR by the map."""
        line = "— Partez! dit Emma. C'est se moquer, je vous chasse."
        source = "\n".join([line] + [f"— Filler {i} dit-il." for i in range(5)])
        entries = [
            {"speaker": "EMMA", "text": "Partez!"},
            {"speaker": "NARRATOR", "text": "dit Emma."},
            {"speaker": "EMMA", "text": "C'est se moquer, je vous chasse."},
        ]
        mapped = apply_dialogue_map(entries, source)["entries"]
        self.assertTrue(mapped[0]["spoken"])
        self.assertFalse(mapped[1]["spoken"])
        self.assertNotIn("spoken", mapped[2])
        self.assertEqual("unverified_dash_tail", mapped[2]["dialogue_ambiguity"])
        self.assertEqual("EMMA", mapped[2]["speaker"])
        self.assertEqual([], apply_dialogue_map(entries, source)["narration_changes"])
        # The same line through both production callers leaves the speaker.
        self.assertEqual("EMMA", apply_source_narration(mapped)[0][2]["speaker"])

    def test_confident_comma_tag_keeps_the_source_map(self):
        line = "— Oui, répondit-il, je viens demain."
        source = "\n".join([line] + [f"— Filler {i} dit-il." for i in range(5)])
        entries = [
            {"speaker": "CHARLES", "text": "Oui,"},
            {"speaker": "NARRATOR", "text": "répondit-il,"},
            {"speaker": "CHARLES", "text": "je viens demain."},
        ]
        mapped = apply_dialogue_map(entries, source)
        self.assertEqual([True, False, True], [row["spoken"] for row in mapped["entries"]])
        self.assertEqual([], mapped["narration_changes"])

    def test_relative_clause_after_the_subject_does_not_reject_the_tag(self):
        cases = (
            ("—Olor no, sino peste de ellos —replicó un viejo ladino que montaba "
             "el burro delantero—.",
             "replicó un viejo ladino que montaba el burro delantero"),
            ("—Es de mi madre —dijo Calpena mirando el papel que la condesa de "
             "Luchana puso ante sus ojos.",
             "dijo Calpena mirando el papel que la condesa de Luchana puso ante sus ojos."),
            ("—Los que hemos visto la subida de Banderas —indicó don Fernando— "
             "estamos curados de asombro.",
             "indicó don Fernando"),
        )
        for line, expected_tag in cases:
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertIn(("NARRATOR", expected_tag), parts)

    def test_penser_songer_objecter_and_pronominal_clitic_tags(self):
        cases = (
            ("— Pauvre garçon! pensait-elle.",
             [("SPOKEN", "Pauvre garçon!"), ("NARRATOR", "pensait-elle.")]),
            ("— Quelle folie! se disait-il, comme les autres.",
             [("SPOKEN", "Quelle folie!"), ("NARRATOR", "se disait-il,"),
              ("SPOKEN", "comme les autres.")]),
            ("— Comment vais-je signer, maintenant? se dit-il.",
             [("SPOKEN", "Comment vais-je signer, maintenant?"),
              ("NARRATOR", "se dit-il.")]),
            ("— Cependant…, objectait madame Bovary.",
             [("SPOKEN", "Cependant…,"), ("NARRATOR", "objectait madame Bovary.")]),
            ("— On dit pourtant qu'il a des moyens, objecta l'hôtesse.",
             [("SPOKEN", "On dit pourtant qu'il a des moyens,"),
              ("NARRATOR", "objecta l'hôtesse.")]),
            ("— Ah! non, ici, pensa-t-elle, je serai bien.",
             [("SPOKEN", "Ah! non, ici,"), ("NARRATOR", "pensa-t-elle,"),
              ("SPOKEN", "je serai bien.")]),
        )
        for line, expected in cases:
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual(expected, parts)

    def test_dit_on_and_dizque_are_idioms_not_tags(self):
        for line in ("— Rien, dit-on.", "— Nada, dizque.",
                     "— Ça lui a passé, dit-on."):
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual([("SPOKEN", line[2:].strip())], parts)

    def test_dit_on_exclusion_is_shared_with_the_evidence_scan(self):
        """One predicate decides "tabled tag token" for both scans.

        `dit-on` is never the split point, even when a real tag shares the
        clause; the line is then routed whole instead of splitting at the
        idiom (#2390 review 3 finding 6).
        """
        ambiguous, parts = self._parts("— Rien, dit-on et dit-elle.")
        self.assertTrue(ambiguous)
        self.assertEqual([("SPOKEN", "Rien, dit-on et dit-elle.")], parts)
        self.assertNotIn("NARRATOR", [kind for kind, _text in parts])
        # A comma after the idiom still lets the real tag be found.
        ambiguous, parts = self._parts("— Rien, dit-on, dit-elle.")
        self.assertFalse(ambiguous)
        self.assertEqual(
            [("SPOKEN", "Rien, dit-on,"), ("NARRATOR", "dit-elle.")], parts)

    def test_spanish_pense_participle_is_speech_not_a_tag(self):
        """The tabled Spanish 1sg `pensé` must not become a confident tag.

        Bare French participles (`pensé`, `songé`) are not tags either
        (#2390 review 3 finding 1).
        """
        for line in (
            "— No, pensé, es tarde.",
            "— Buena persona, pensé, y me fui.",
            "— Buena persona, pensé para mí.",
        ):
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual([("SPOKEN", line[2:].strip())], parts)
        # The French past simple is still a tag, including the inverted form.
        ambiguous, parts = self._parts("— Oui, pensa-t-elle.")
        self.assertFalse(ambiguous)
        self.assertEqual(
            [("SPOKEN", "Oui,"), ("NARRATOR", "pensa-t-elle.")], parts)

    def test_whole_line_reporting_verbs_from_the_measured_books(self):
        """`faisait`, `fit`, `concéda`, `repartit` are tags (#2390 finding 2)."""
        cases = (
            ("— Oh! c'est vrai! faisait l'autre en caressant sa mâchoire, "
             "avec un air de dédain mêlé de satisfaction.",
             "faisait l'autre en caressant sa mâchoire,"),
            ("— C'est vrai! c'est vrai! concéda le bonhomme en se rasseyant "
             "sur sa chaise.",
             "concéda le bonhomme en se rasseyant sur sa chaise."),
            ("— Non, repartit-il.", "repartit-il."),
            ("— Bon, fit-elle.", "fit-elle."),
            ("— Non, concédait-il.", "concédait-il."),
        )
        for line, expected_tag in cases:
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertIn(("NARRATOR", expected_tag), parts)

    def test_spanish_compound_tags_need_a_subject_shape(self):
        """`ha dicho Ana` is a tag; `ha dicho:` inside speech is not.

        (#2390 review 3 finding 5.)
        """
        for line, expected_tag in (
            ("— Sí, ha dicho Ana.", "ha dicho Ana."),
            ("— Sí, había dicho Ana.", "había dicho Ana."),
            ("— Sí, habían contestado ellas.", "habían contestado ellas."),
        ):
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual(
                    [("SPOKEN", line[2:].split(",")[0] + ","),
                     ("NARRATOR", expected_tag)], parts)
        for line in (
            "— Me ha dicho que vengas.",
            "— Sí, ha dicho que viene.",
            "— Sí, lo ha dicho.",
        ):
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertEqual([("SPOKEN", line[2:].strip())], parts)
        # The measured Vergara shape: a colon announces reported speech.
        ambiguous, parts = self._parts(
            "— El señor Arias, ha dicho: «Señor, los brutos llevarán a "
            "Vuestra Majestad a Madrid».")
        self.assertNotIn("NARRATOR", [kind for kind, _text in parts])

    def test_object_complements_are_speech_not_subjects(self):
        """A tag names a subject; `ha dicho la verdad` names an object.

        The compound shape has to pass the same subject gate as a simple verb
        (#2390 review 4 finding 2). A determiner + object noun (`la verdad`,
        `una mentira`), a bare object (`eso`, `esto`, `cosas`) and the French
        equivalents (`cela`, `ça`, `ceci`) are speech, so the line is routed
        whole instead of confidently splitting at a non-tag. The real tag
        `ha dicho Ana` still splits.
        """
        for line in (
            "— Sí, ha dicho la verdad.",
            "— Sí, ha dicho eso.",
            "— Sí, ha dicho esto.",
            "— Sí, ha dicho cosas.",
            "— Sí, ha dicho una mentira.",
            "— Sí, dijo la verdad.",
            "— Sí, había contestado la verdad.",
            "— Sí, han respondido eso.",
        ):
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertTrue(ambiguous)
                self.assertEqual([("SPOKEN", line[2:].strip())], parts)
        ambiguous, parts = self._parts("— Sí, ha dicho Ana.")
        self.assertFalse(ambiguous)
        self.assertEqual(
            [("SPOKEN", "Sí,"), ("NARRATOR", "ha dicho Ana.")], parts)

    def test_plain_verb_clauses_with_an_object_stay_speech(self):
        """`faisait`/`firent` reading a plain clause as a tag is refused.

        The widened table must not table ordinary speech: a third-person
        pronoun + `faisait`/`firent`/`fit` + `cela` is a clause, not a tag
        (#2390 review 4 finding 3). The two measured Flaubert lines still
        split, because their subject is a name.
        """
        for line in (
            "— Oui, il faisait cela pour moi.",
            "— Oui, elle faisait cela souvent.",
            "— Oui, ils firent cela.",
            "— Oui, il fit cela pour moi.",
        ):
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertTrue(ambiguous)
                self.assertEqual([("SPOKEN", line[2:].strip())], parts)
        ambiguous, parts = self._parts(
            "— Oh! c'est vrai! faisait l'autre en caressant sa mâchoire, "
            "avec un air de dédain mêlé de satisfaction.")
        self.assertFalse(ambiguous)
        self.assertIn(("NARRATOR", "faisait l'autre en caressant sa mâchoire,"),
                      parts)
        # Inverted, so G2 cannot help: the object list is what refuses `cela`.
        ambiguous, parts = self._parts("— Oui, fit cela.")
        self.assertTrue(ambiguous)
        self.assertEqual([("SPOKEN", "Oui, fit cela.")], parts)

    def test_segun_and_a_relative_clause_are_not_subjects(self):
        """`según me dijo` is speech inside the line, not a narrator tag.

        The measured Galdós line kept `según me dijo` in `NARRATOR`, because
        `según` was missing from the pre-verb stopwords and `me` is a clitic
        (#2390 review 4 finding 4).
        """
        ambiguous, parts = self._parts("— Era, según me dijo, amigo.")
        self.assertTrue(ambiguous)
        self.assertEqual([("SPOKEN", "Era, según me dijo, amigo.")], parts)

    def test_subjectless_present_imperatives_are_routed(self):
        """A tabled imperative is not a narrator tag after a comma.

        The Spanish `tú` imperative and the French `tu` imperative of an -er
        verb share the 3sg present spelling, so `— Vamos, continúa.` must be
        routed rather than split at `continúa` (#2390 gate 1 G1). A real
        subjectless past-tense tag is still a tag.
        """
        for line in (
            "— Vamos, continúa.",
            "— Anda, contesta la carta.",
            "— Allons, avoue!",
            "— Eh bien, demande!",
            "— Allons, demande-lui pardon.",
        ):
            with self.subTest(routed=line):
                ambiguous, parts = self._parts(line)
                self.assertTrue(ambiguous)
                self.assertEqual([("SPOKEN", line[2:].strip())], parts)
        ambiguous, parts = self._parts("— Vamos, dijo.")
        self.assertFalse(ambiguous)
        self.assertEqual([("SPOKEN", "Vamos,"), ("NARRATOR", "dijo.")], parts)
        # A present -re/-ir form is not an imperative, so a real inverted tag
        # with a determiner subject still splits.
        ambiguous, parts = self._parts("— Oui, dit le pharmacien.")
        self.assertFalse(ambiguous)
        self.assertEqual(
            [("SPOKEN", "Oui,"), ("NARRATOR", "dit le pharmacien.")], parts)
        # A name after the verb is a subject, so the imperative reading does
        # not apply (#2390 gate 2 T-1).
        for line, expected in (
            ("— Bien, continúa Ana.", [("SPOKEN", "Bien,"),
                                       ("NARRATOR", "continúa Ana.")]),
            ("— Oui, ajoute Emma.", [("SPOKEN", "Oui,"),
                                     ("NARRATOR", "ajoute Emma.")]),
        ):
            with self.subTest(split=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual(expected, parts)

    def test_every_imperative_present_form_is_routed_subjectless(self):
        """The set itself is pinned, not just three sample forms.

        A table-driven line for every member kills the mutants that drop a
        single entry (`explique`, `pregunta`) from the set
        (#2390 gate 2 T-1).
        """
        for form in sorted(dialogue_spans._DASH_PRESENT_IMPERATIVE):
            line = f"— Allons, {form}."
            with self.subTest(form=form):
                ambiguous, parts = self._parts(line)
                self.assertTrue(ambiguous)
                self.assertEqual([("SPOKEN", line[2:].strip())], parts)

    def test_the_imperative_present_set_covers_every_french_er_form(self):
        """A French -er 3sg present is its own `tu` imperative.

        Every such form in the one table belongs in the set, so leaving one
        out is a slip rather than a trade; `demande` was the one omission
        (#2390 gate 2 G1-a).
        """
        forms = dialogue_spans._DASH_REPORTING_VERBS.split("|")
        french = forms[forms.index("dit"):]
        expected = {
            form for form in french
            if form.endswith("e") and not form.endswith(("ait", "aient"))
        }
        self.assertLessEqual(expected, dialogue_spans._DASH_PRESENT_IMPERATIVE)

    def test_a_subjectless_french_tag_at_the_end_is_speech(self):
        """A French incise always carries an inverted subject.

        `— …, disait:` has no subject after the verb, so it is the character
        speaking, not a tag (#2390 gate 2 B1). `disait-il`, `dit Emma` and
        `s'écria-t-elle` still carry their subjects and still split.
        """
        for line in (
            "— Pourtant l'autre, celui d'hier, disait:",
            "— Mais celui qui est venu, disait:",
            "— Et l'autre, ce matin, répondait.",
        ):
            with self.subTest(routed=line):
                ambiguous, parts = self._parts(line)
                self.assertTrue(ambiguous)
                self.assertEqual([("SPOKEN", line[2:].strip())], parts)
        for line, expected in (
            ("— Oui, dit-il.", [("SPOKEN", "Oui,"),
                                ("NARRATOR", "dit-il.")]),
            ("— Allons, disait-elle.", [("SPOKEN", "Allons,"),
                                        ("NARRATOR", "disait-elle.")]),
            ("— Ah! s'écria-t-elle.", [("SPOKEN", "Ah!"),
                                       ("NARRATOR", "s'écria-t-elle.")]),
        ):
            with self.subTest(split=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual(expected, parts)

    def test_imperatives_after_speech_punctuation_are_still_imperatives(self):
        """`!`/`?`/`…` introduce the same subjectless imperative as a comma.

        G1 is not comma-only: 19th-century French writes a lower-case verb
        after `!` routinely, and that verb is still the character's command
        (#2390 gate 2 G1-c).
        """
        for line in (
            "— Allons! avoue.",
            "— Voyons! explique-toi.",
            "— Ah! répète-le.",
            "— ¡Vamos! contesta.",
            "— Eh bien… explique.",
        ):
            with self.subTest(routed=line):
                ambiguous, parts = self._parts(line)
                self.assertTrue(ambiguous)
                self.assertEqual([("SPOKEN", line[2:].strip())], parts)

    def test_a_preposition_after_an_imperative_opens_an_object(self):
        """`pregunta a Ana` names the person addressed, not a tag subject.

        A window that starts with a preposition has no subject, so the
        imperative reading applies (#2390 gate 2 G1-b). A name directly
        after the verb is still a subject and still splits.
        """
        for line in (
            "— Anda, pregunta a Ana.",
            "— Allons, explique à Emma.",
            "— Allons, avoue à Charles!",
            "— Venga, pregunta por Juan.",
            "— Anda, explica a Pedro lo que pasó.",
            "— Anda, pregunta por él.",
        ):
            with self.subTest(routed=line):
                ambiguous, parts = self._parts(line)
                self.assertTrue(ambiguous)
                self.assertEqual([("SPOKEN", line[2:].strip())], parts)
        for line, expected in (
            ("— Bien, continúa Ana.", [("SPOKEN", "Bien,"),
                                       ("NARRATOR", "continúa Ana.")]),
            ("— Oui, répète-t-il.", [("SPOKEN", "Oui,"),
                                     ("NARRATOR", "répète-t-il.")]),
        ):
            with self.subTest(split=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual(expected, parts)

    def test_a_clitic_before_the_imperative_does_not_hide_it(self):
        """A pro-drop imperative keeps its reading behind an object clitic.

        G1 requires an empty pre-verb window; a clitic alone is still no
        subject (#2390 gate 2 G1-d). A clitic before an inverted tag still
        splits.
        """
        for line in (
            "— Sí, le pregunta la hora.",
            "— Sí, les explica la lección.",
            "— Sí, le repite la pregunta.",
            "— Oui, lui explique la leçon.",
        ):
            with self.subTest(routed=line):
                ambiguous, parts = self._parts(line)
                self.assertTrue(ambiguous)
                self.assertEqual([("SPOKEN", line[2:].strip())], parts)
        for line in ("— Oui, lui dit-il.", "— Sí, me dijo Ana."):
            with self.subTest(split=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertTrue(any(kind == "NARRATOR" for kind, _ in parts))

    def test_past_tense_collisions_route_only_when_nothing_follows(self):
        """The five French -er forms that are also passés simples.

        With nothing after the verb they are the character's command
        (`— Venga, observa.`); a word after them keeps the real tag
        (`observa le clerc`) (#2390 gate 2 G1-a).
        """
        for line in (
            "— Venga, observa.",
            "— Anda, protesta.",
            "— Vamos, declara.",
            "— Vamos, exclama.",
        ):
            with self.subTest(routed=line):
                ambiguous, parts = self._parts(line)
                self.assertTrue(ambiguous)
                self.assertEqual([("SPOKEN", line[2:].strip())], parts)
        for line, expected in (
            ("— En effet, observa le clerc.",
             [("SPOKEN", "En effet,"), ("NARRATOR", "observa le clerc.")]),
            ("— Sí, observa Ana.",
             [("SPOKEN", "Sí,"), ("NARRATOR", "observa Ana.")]),
            ("— Venga, observa el cielo.",
             [("SPOKEN", "Venga,"), ("NARRATOR", "observa el cielo.")]),
        ):
            with self.subTest(split=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual(expected, parts)

    def test_a_subject_and_object_pronoun_before_the_verb_is_a_clause(self):
        """The object is before the verb, where G2's tail test cannot see it.

        `— Non, il le dit.` is speech, not a tag (#2390 gate 2 G2-a). A
        subject with nothing after it (`— Yes, he said.`) and a clitic before
        an inverted tag (`— Oui, lui dit-il.`) still split.
        """
        for line in (
            "— Non, il le dit.",
            "— Oui, elle le demande.",
            "— Oui, Dieu le dit.",
        ):
            with self.subTest(routed=line):
                ambiguous, parts = self._parts(line)
                self.assertTrue(ambiguous)
                self.assertEqual([("SPOKEN", line[2:].strip())], parts)
        for line, expected in (
            ("— Yes, he said.", [("SPOKEN", "Yes,"),
                                 ("NARRATOR", "he said.")]),
            ("— Oui, lui dit-il.", [("SPOKEN", "Oui,"),
                                    ("NARRATOR", "lui dit-il.")]),
        ):
            with self.subTest(split=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual(expected, parts)

    def test_the_imperative_present_set_is_a_subset_of_the_tag_table(self):
        """The G1 classification must not drift from the one verb table.

        `_DASH_PRESENT_IMPERATIVE` is a property of a subset of the tabled
        forms, not a second verb list; a form removed from the table must not
        linger here.
        """
        table = set(dialogue_spans._DASH_REPORTING_VERBS.split("|"))
        self.assertLessEqual(dialogue_spans._DASH_PRESENT_IMPERATIVE, table)

    def test_a_real_subject_with_words_after_the_verb_is_routed(self):
        """Subject + tabled verb + object is a clause, not a tag.

        A tag with a real subject before the verb has nothing after it but
        adverbs; any word here is an object, so the line is routed
        (#2390 gate 1 G2). The inverted halves still split.
        """
        for line in (
            "— Oui, il disait des bêtises.",
            "— Sí, Ana contestó la carta.",
            "— Yes, he called the police.",
        ):
            with self.subTest(routed=line):
                ambiguous, parts = self._parts(line)
                self.assertTrue(ambiguous)
                self.assertEqual([("SPOKEN", line[2:].strip())], parts)
        for line, expected in (
            ("— Oui, dit-il.", [("SPOKEN", "Oui,"),
                                ("NARRATOR", "dit-il.")]),
            ("— Sí —dijo Ana—.", [("SPOKEN", "Sí"),
                                  ("NARRATOR", "dijo Ana")]),
        ):
            with self.subTest(split=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual(expected, parts)

    def test_a_bare_infinitive_is_not_a_reporting_verb(self):
        """`seguir` was a table slip: `seguir adelante` is a subject.

        (#2390 gate 1 G3.)
        """
        for line in (
            "— Bueno, seguir adelante es lo mejor.",
            "— Ahora, seguir es inútil.",
        ):
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual([("SPOKEN", line[2:].strip())], parts)

    def test_speech_punctuation_introducer_requires_a_lower_case_verb(self):
        """`!`/`?`/`…` before an upper-case word starts a new sentence.

        Without the lower-case rule these confident splits appear
        (#2390 gate 1 N-a).
        """
        for line in (
            "— Arrête! Demande-lui pardon.",
            "— Ah! Répète-le.",
            "— Assez! Explique-toi.",
        ):
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual([("SPOKEN", line[2:].strip())], parts)

    def test_a_later_evidenced_tag_is_not_swallowed_by_an_unverified_range(self):
        """The unverified range stops at the next evidenced split.

        (#2390 review 3 finding 4.)
        """
        line = (
            "— Donc, reprit celle-ci faisant une révérence, si ce n'était pas "
            "trop vous demander..., — elle salua encore une fois, — quand "
            "vous voudrez, — et son regard suppliait, — un cruchon "
            "d'eau-de-vie, dit-elle enfin, et j'en frotterai les pieds de "
            "votre petite, qui les a tendres comme la langue.")
        ambiguous, parts = self._parts(line)
        self.assertTrue(ambiguous)
        self.assertIn(("NARRATOR", "dit-elle enfin,"), parts)
        tails = [line[a:b].strip() for a, b in
                 dialogue_spans.dash_line_unverified_tails(line)]
        self.assertTrue(tails)
        self.assertFalse(any("dit-elle" in tail for tail in tails))
        source = line + "\n" + "\n".join(f"— Turn {i}." for i in range(5))
        entries = []
        speaker = 0
        for kind, a, b in dialogue_spans.split_dash_line(line, 1, len(line))[0]:
            text = line[a:b].strip()
            if not text:
                continue
            if kind == "SPOKEN":
                speaker += 1
                entries.append({"speaker": f"CHAR{speaker}", "text": text})
            else:
                entries.append({"speaker": "NARRATOR", "text": text})
        mapped = apply_dialogue_map(entries, source)["entries"]
        tag = next(row for row in mapped if row["text"].startswith("dit-elle"))
        self.assertEqual("NARRATOR", tag["speaker"])
        self.assertIs(False, tag["spoken"])
        self.assertNotIn("dialogue_ambiguity", tag)


    def test_first_person_reporting_forms_stay_speech_in_the_comma_path(self):
        # `dije` is absent from the table; `decíamos` is tabled and is still
        # refused by the first-person guard, so the rule has one test that
        # actually reaches the guard (#2390 review 6).
        for line in ("— No, dije, es tarde.", "— Pues, decíamos, no es tarde."):
            with self.subTest(line=line):
                _ambiguous, parts = self._parts(line)
                self.assertEqual(["SPOKEN"], [kind for kind, _text in parts])

    def test_introducer_tags_need_a_lower_case_verb_behind_at_most_one_clitic(self):
        self.assertEqual(
            [("SPOKEN", "Vraiment! Marie s'écria.")],
            self._parts("— Vraiment! Marie s'écria.")[1],
        )
        # A capitalised name after speech punctuation is a new sentence, not a
        # tag: without the introducer gate `Anna demanda` would split away.
        ambiguous, parts = self._parts("— Vraiment! Anna demanda une explication.")
        self.assertFalse(ambiguous)
        self.assertEqual([("SPOKEN", "Vraiment! Anna demanda une explication.")], parts)
        for line, expected in (
            ("— Vraiment! s'écria-t-il.",
             [("SPOKEN", "Vraiment!"), ("NARRATOR", "s'écria-t-il.")]),
            ("— Quelle folie! se disait-il.",
             [("SPOKEN", "Quelle folie!"), ("NARRATOR", "se disait-il.")]),
        ):
            with self.subTest(line=line):
                ambiguous, parts = self._parts(line)
                self.assertFalse(ambiguous)
                self.assertEqual(expected, parts)

    def test_leading_indented_dash_line_is_dialogue(self):
        line = "  —Hola —dijo Ana—. Vamos."
        self.assertEqual(
            ["Hola", "Vamos."],
            [line[a:b] for a, b in spoken_spans(line, "dash_lines")],
        )

