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

    def test_shared_mark_disambiguation_is_linear(self):
        # The shared-mark roles are resolved with position-indexed lookups. A
        # linear rescan per quote made a 20k-pair text take seconds; this is a
        # coarse wall-clock guard with a wide margin (measured ~0.05s).
        import time
        source = "\n\n".join("„Speech " + str(i) + " here“ said he." for i in range(20000))
        started = time.monotonic()
        self.assertEqual(20000, len(spoken_spans(source, "paired_quotes")))
        self.assertLess(time.monotonic() - started, 2.0)

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
        # that opens a speech once and closes it paragraphs later is refused,
        # and that speech is then voiced by the narrator. It was kept below the
        # old 2,000-character cap; nothing here claims that was wrong, only
        # that this rule stops doing it.
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

