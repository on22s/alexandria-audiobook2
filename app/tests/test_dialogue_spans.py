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
    def test_one_character_utterances_are_counted_and_located_for_each_quote_style(self):
        for opener, closer in [('"', '"'), ('“', '”'), ('「', '」'), ('『', '』')]:
            with self.subTest(opener=opener):
                words = list('AIえあ!?')
                text = '\n'.join(opener + word + closer for word in words)
                self.assertEqual('paired_quotes', detect_convention(text))
                self.assertEqual(words, [text[a:b] for a, b in spoken_spans(text)])
                self.assertEqual([], spoken_spans(opener + closer, 'paired_quotes'))

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
