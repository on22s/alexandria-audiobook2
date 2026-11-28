from pass_quality import MIN_ORDERED_TRIGRAM_RECALL
import unittest
from chunk_quality import validate_chunk_quality
from pass_quality import (classify_lexical_quote_regions, analyze_outer_quote_regions,
                          split_outer_quote_regions, validate_attribution,
                          validate_segment_quality)
from pass_quality import validate_instruct, index_head_check
from validation_reporting import format_validation_findings
from dialogue_spans import spoken_spans
import default_prompts


def _seg(text, type_="NARRATOR"):
    return {"type": type_, "text": text}


class SegmentQualityTests(unittest.TestCase):

    def test_cyrillic_case_only_changes_use_the_same_gate_as_single_pass(self):
        for source, output in (('михаил', 'Михаил'), ('ЁЛКА', 'ёлка')):
            with self.subTest(source=source):
                report = validate_segment_quality(source, [{'type': 'NARRATOR', 'text': output}])
                self.assertTrue(report['passed'], report['findings'])
        report = validate_segment_quality('михаил', [{'type': 'NARRATOR', 'text': 'Михаъил'}])
        finding = next(row for row in report['findings'] if row['code'] == 'unsupported_cyrillic')
        self.assertEqual(['ъ'], finding['characters'])

    def test_source_credit_quotes_stay_narration_for_supported_styles(self):
        for opener, closer in [('"', '"'), ('“', '”'), ('「', '」')]:
            with self.subTest(opener=opener):
                source = f'Light Novel Adaptation found in {opener}Chapter One{closer}\n\nShe said {opener}Hello.{closer}'
                analysis = classify_lexical_quote_regions(source, analyze_outer_quote_regions(source))
                title = [row for row in analysis['regions'] if 'Chapter One' in row['text']]
                speech = [row for row in analysis['regions'] if row['text'] == 'Hello.']
                self.assertEqual(['NARRATOR'], [row['type'] for row in title])
                self.assertEqual(['SPOKEN'], [row['type'] for row in speech])

    def test_german_low_high_quotes_label_speech_and_narration_in_order(self):
        # `“` closes „…“ but opens “…”. The segmenter must not read a German
        # speech backwards (narration as SPOKEN, speech as NARRATOR).
        source = "Er sagte „Komm her!“ und ging."
        regions = analyze_outer_quote_regions(source)["regions"]
        self.assertEqual(
            [("NARRATOR", "Er sagte"), ("SPOKEN", "Komm her!"), ("NARRATOR", "und ging.")],
            [(row["type"], row["text"]) for row in regions])
        # English behaviour is unchanged by the shared-mark support.
        english = analyze_outer_quote_regions("He said “Go.” then left.")["regions"]
        self.assertEqual(
            [("NARRATOR", "He said"), ("SPOKEN", "Go."), ("NARRATOR", "then left.")],
            [(row["type"], row["text"]) for row in english])

    def test_generation_segments_every_supported_convention_as_speech(self):
        # The map and the pass that creates entries must agree on which marks
        # carry dialogue; the German inversion was the urgent case.
        for source, expected in [
            ('He said "Hello." then left.', 'Hello.'),
            ('Er sagte „Hallo!“ und ging.', 'Hallo!'),
            ('Il a dit « Bonjour » puis parti.', 'Bonjour'),
            ('Han sagde »Hej« og gik.', 'Hej'),
        ]:
            with self.subTest(source=source):
                regions = analyze_outer_quote_regions(source)["regions"]
                self.assertEqual(
                    [("NARRATOR", source.split(expected)[0].strip().strip('"“”„«» ').strip()),
                     ("SPOKEN", expected),
                     ("NARRATOR", source.split(expected)[1].strip().strip('"“”„«» ').strip())],
                    [(row["type"], row["text"]) for row in regions])

    def test_low_high_reporting_word_is_not_a_speaker_label(self):
        # A single capitalised word before „ is a reporting verb (Polish
        # "Powiedział", German "Nein"), not a printed speaker name.
        for source in ['Powiedział „Cześć!“ i wyszedł.',
                       'Nein „Hallo!“ sagte er.']:
            with self.subTest(source=source):
                regions = analyze_outer_quote_regions(source)["regions"]
                self.assertFalse(any("source_label" in row for row in regions))

    def test_polish_close_leaves_no_stale_opener_for_a_later_english_quote(self):
        # `”` closing `„…”` once left `„` on the open stack, so the next `“`
        # was read as English nested in German: its speech went to NARRATOR,
        # or (unclosed) the narration before it went to SPOKEN.
        cases = [
            ('„Dzień dobry” – powiedział.\n\nPotem: “Hello there,” said Bob.',
             "Hello there,", "– powiedział."),
            ('„Tak” – rzekł. On czytał “Hamlet. Koniec.',
             "Hamlet. Koniec.", "– rzekł. On czytał"),
        ]
        for source, spoken, narration in cases:
            with self.subTest(source=source):
                regions = analyze_outer_quote_regions(source)["regions"]
                kinds = {row["text"]: row["type"] for row in regions}
                self.assertEqual("SPOKEN", kinds.get(spoken), regions)
                self.assertEqual("NARRATOR", kinds.get(narration), regions)

    def test_stray_guillemet_before_first_speech_keeps_speech_spoken(self):
        source = "Menu » Chapitre. «Bonjour», dit-il. Il partit."
        regions = analyze_outer_quote_regions(source)["regions"]
        self.assertEqual(
            [("NARRATOR", "Menu » Chapitre."), ("SPOKEN", "Bonjour"),
             ("NARRATOR", ", dit-il. Il partit.")],
            [(row["type"], row["text"]) for row in regions])

    def test_nested_english_inside_german_keeps_german_as_speech(self):
        source = "Er sagte „He said “Hi” to me.“ und ging."
        regions = analyze_outer_quote_regions(source)["regions"]
        self.assertEqual(
            [("NARRATOR", "Er sagte"),
             ("SPOKEN", "He said Hi to me."),
             ("NARRATOR", "und ging.")],
            [(row["type"], row["text"]) for row in regions])

    def test_line_dash_dialogue_is_segmented_as_speech(self):
        source = "— Primera línea.\n\n— Segunda línea."
        regions = analyze_outer_quote_regions(source)["regions"]
        self.assertEqual(2, len(regions))
        self.assertEqual(["SPOKEN", "SPOKEN"], [row["type"] for row in regions])

    def test_one_word_dialogue_split_passes_but_wrong_region_and_crossing_fail(self):
        source = '“No. Not at all.”'
        report = validate_segment_quality(source, [{'type': 'SPOKEN', 'text': text} for text in ('No.', 'Not at all.')])
        self.assertTrue(report['passed'], report['findings'])
        wrong = validate_segment_quality(source, [{'type': 'NARRATOR', 'text': 'No.'}, {'type': 'SPOKEN', 'text': 'Not at all.'}])
        self.assertFalse(wrong['passed'])
        self.assertIn('quote_region_misclassified', {row['code'] for row in wrong['findings']})
        crossing = validate_segment_quality('Outside. “Inside.”', [{'type': 'SPOKEN', 'text': 'Outside. Inside.'}])
        self.assertIn('crosses_quote_boundary', {row['code'] for row in crossing['findings']})
        absent = validate_segment_quality('“Nobody.”', [{'type': 'SPOKEN', 'text': 'No.'}])
        self.assertFalse(absent['passed'])

    def test_source_label_cannot_hide_missing_spoken_words(self):
        source = 'Alice “one two three four five six seven eight nine ten.”'
        entries = [{"type": "SPOKEN", "text": "one two three four five",
                    "source_label": "Alice six seven eight nine ten"}]
        report = validate_segment_quality(source, entries)
        self.assertIn("low_source_token_recall",
                      {finding["code"] for finding in report["findings"]})

    def test_lexical_quote_classifier_relabels_reference_term_only(self):
        source = 'He was known as "the Fox". Then she said, "Run!"'
        analysis = analyze_outer_quote_regions(source)
        classified = classify_lexical_quote_regions(source, analysis)
        self.assertEqual(
            ["NARRATOR", "NARRATOR", "NARRATOR", "SPOKEN"],
            [region["type"] for region in classified["regions"]])
        self.assertEqual("LEXICAL_QUOTE", classified["regions"][1]["quote_role"])
        self.assertEqual("SPOKEN", analysis["regions"][1]["type"])

    def test_lexical_quote_classifier_keeps_repeated_dialogue_term_spoken(self):
        source = 'He was known as "the Fox". "The Fox" shouted.'
        classified = classify_lexical_quote_regions(
            source, analyze_outer_quote_regions(source))
        self.assertEqual(
            ["NARRATOR", "NARRATOR", "NARRATOR", "SPOKEN", "NARRATOR"],
            [region["type"] for region in classified["regions"]])

    def test_lexical_quote_classifier_handles_exact_issue_644_grammar(self):
        source = (
            'After being used, that aura would end up leaving something known as a '
            '"Mana Trail". It was evidence that proved whether or not the magic of a '
            'specific element had been used. In addition to elemental types and mana '
            'capacity, this mana evaluation also contained an additional hidden '
            'evaluation factor that tested students for their ability in something '
            'known as "Mana Control". Professor Fernando began to read each grade '
            'aloud. "…Provisional 3rd Class. 1st, Grade C-."')
        classified = classify_lexical_quote_regions(
            source, analyze_outer_quote_regions(source))

        self.assertEqual(
            ["NARRATOR", "NARRATOR", "NARRATOR", "NARRATOR", "NARRATOR", "SPOKEN"],
            [region["type"] for region in classified["regions"]])
        entries = [
            _seg('After being used, that aura would end up leaving something known as a'),
            _seg('Mana Trail'),
            _seg('. It was evidence that proved whether or not the magic of a specific '
                 'element had been used. In addition to elemental types and mana '
                 'capacity, this mana evaluation also contained an additional hidden '
                 'evaluation factor that tested students for their ability in something '
                 'known as'),
            _seg('Mana Control'),
            _seg('. Professor Fernando began to read each grade aloud.'),
            _seg('…Provisional 3rd Class. 1st, Grade C-.', "SPOKEN"),
        ]
        report = validate_segment_quality(source, entries, quote_analysis=classified)
        self.assertTrue(report["passed"], report["findings"])

    def test_lexical_quote_classifier_preserves_other_issue_644_terms(self):
        source = ('Ian was able to enroll thanks to his "rarity". It was a result of '
                  'Ian’s unique ability to use "light" magic.')
        classified = classify_lexical_quote_regions(
            source, analyze_outer_quote_regions(source))
        self.assertEqual(
            ["NARRATOR"] * 5,
            [region["type"] for region in classified["regions"]])

    def test_lexical_quote_classifier_keeps_reporting_verb_dialogue_spoken(self):
        sources = (
            'He called "Run!"',
            'He called "John"',
            'The man called "Run!"',
            'He called out, "Run!"',
            'Professor Fernando said, "Mana Trail."',
            'She said the word "No."',
            'She whispered the term "retreat".',
            '"Mana Trail," he said.',
            '"Run!" shouted Professor Fernando.',
        )
        for source in sources:
            with self.subTest(source=source):
                classified = classify_lexical_quote_regions(
                    source, analyze_outer_quote_regions(source))
                quoted = [region for region in classified["regions"]
                          if any(term in region["text"] for term in
                                 ("Mana Trail", "Run!", "John", "No.", "retreat"))]
                self.assertEqual(["SPOKEN"], [region["type"] for region in quoted])

    def test_lexical_quote_classifier_handles_articles_and_quote_styles(self):
        for opener, closer in (("\"", "\""), ("“", "”"), ("「", "」"), ("『", "』")):
            for article, term in (("", "Mana Trail"), ("a ", "Mana Trail"),
                                  ("an ", "Arcane Trace"), ("the ", "Mana Trail")):
                source = f'The effect was known as {article}{opener}{term}{closer}.'
                with self.subTest(source=source):
                    classified = classify_lexical_quote_regions(
                        source, analyze_outer_quote_regions(source))
                    quoted = next(region for region in classified["regions"]
                                  if region["text"] == term)
                    self.assertEqual("NARRATOR", quoted["type"])
                    self.assertEqual("LEXICAL_QUOTE", quoted["quote_role"])

    def test_lexical_quote_classifier_handles_strong_grammar_contexts(self):
        sources = (
            'The effect was referred to as "Mana Trail".',
            'The effect was classified as "rare".',
            'The result was rated "E".',
            'The result was ranked as "S".',
            'The phenomenon was called "Mana Trail".',
            'The sword was named "Dawn".',
            'A phenomenon called "Mana Trail" appeared.',
            'There was a man called "John".',
            'There was a force called "aether".',
            'The term "Mana Trail" was precise.',
            'The concept of "rarity" mattered.',
            'The sign means "stop".',
            'She could use "light" magic.',
            'The mage wielded "light" magic.',
        )
        for source in sources:
            with self.subTest(source=source):
                classified = classify_lexical_quote_regions(
                    source, analyze_outer_quote_regions(source))
                quoted = classified["regions"][1]
                self.assertEqual("NARRATOR", quoted["type"])
                self.assertEqual("LEXICAL_QUOTE", quoted["quote_role"])

    def test_lexical_suffix_does_not_cross_a_sentence_or_paragraph(self):
        sources = (
            'He shouted, "Run!" Magic filled the room.',
            'He said, "No."\n\nSkill was useless here.',
        )
        for source in sources:
            with self.subTest(source=source):
                classified = classify_lexical_quote_regions(
                    source, analyze_outer_quote_regions(source))
                quoted = classified["regions"][1]
                self.assertEqual("SPOKEN", quoted["type"])
                self.assertNotIn("quote_role", quoted)

    def test_reporting_suffix_does_not_override_a_lexical_subject(self):
        lexical = (
            'The word "magic" said nothing about its origin.',
            'The title "King" said little about his character.',
            'The term "magic," as the old historian said, was imprecise.',
        )
        for source in lexical:
            with self.subTest(source=source):
                classified = classify_lexical_quote_regions(
                    source, analyze_outer_quote_regions(source))
                quoted = classified["regions"][1]
                self.assertEqual("NARRATOR", quoted["type"])
                self.assertEqual("LEXICAL_QUOTE", quoted["quote_role"])

    def test_chapter_title_in_source_credit_is_not_dialogue(self):
        source = ('Light Novel Adaptation found in Volume 9, Interlude '
                  '“To Each Their Vows”.')
        self.assertEqual(
            [{"type": "NARRATOR",
              "text": "Light Novel Adaptation found in Volume 9, Interlude "
                      "To Each Their Vows."}],
            split_outer_quote_regions(source))

    def test_quote_free_analysis_does_not_create_nonexistent_boundaries(self):
        source = "The room was quiet and the door remained closed."
        analysis = analyze_outer_quote_regions(source)
        self.assertEqual([], analysis["regions"])
        report = validate_segment_quality(source, [_seg(source)], quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])
        self.assertEqual("skipped:none_detected", report["quote_gate"])

    def test_empty_analysis_still_checks_actual_quotes_and_text_fidelity(self):
        source = '"Come here." She stopped.'
        entries = [_seg("Come here.", "SPOKEN"), _seg("She stopped.")]
        empty = {"regions": []}
        report = validate_segment_quality(source, entries, quote_analysis=empty)
        self.assertTrue(report["passed"], report["findings"])
        self.assertEqual("ran", report["quote_gate"])
        misclassified = [_seg("Come here."), _seg("She stopped.")]
        bad = validate_segment_quality(source, misclassified, quote_analysis=empty)
        self.assertIn("quote_region_misclassified",
                      {finding["code"] for finding in bad["findings"]})
        narration = " ".join(f"word{i}" for i in range(40))
        truncated = validate_segment_quality(
            narration, [_seg("word0 word1")],
            quote_analysis=analyze_outer_quote_regions(narration))
        self.assertIn("low_source_token_recall",
                      {finding["code"] for finding in truncated["findings"]})
        self.assertEqual({"regions": []}, empty)

    def test_quote_free_continuation_keeps_supplied_spoken_regions(self):
        source = "The speech continues across this chunk."
        analysis = analyze_outer_quote_regions(source, initial_depth=1, allow_open_end=True)
        self.assertEqual(["SPOKEN"], [region["type"] for region in analysis["regions"]])
        report = validate_segment_quality(source, [_seg(source, "SPOKEN")],
                                          quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])
        self.assertEqual("ran", report["quote_gate"])
        misclassified = validate_segment_quality(source, [_seg(source)], quote_analysis=analysis)
        self.assertIn("quote_region_misclassified",
                      {finding["code"] for finding in misclassified["findings"]})

    def test_quote_continuation_checks_the_first_paragraph_character(self):
        cases = [('A“B” tail “C” end”', [{"type": "SPOKEN", "text": "AB tail C end"}]),
                 ('A「B」 tail”', [{"type": "SPOKEN", "text": "AB tail"}]),
                 ('A" Tail.', [{"type": "SPOKEN", "text": "A"},
                              {"type": "NARRATOR", "text": "Tail."}])]
        for source, expected in cases:
            with self.subTest(source=source):
                analysis = analyze_outer_quote_regions(source, initial_depth=1)
                self.assertEqual(expected, analysis["regions"])
                self.assertEqual(0, analysis["final_depth"])
                self.assertEqual([], analysis["repairs"])

    def test_repeated_opening_at_an_actual_paragraph_start_stays_decorative(self):
        for source in ('“More words.”', '\n\n“More words.”', '"More words."'):
            with self.subTest(source=source):
                analysis = analyze_outer_quote_regions(source, initial_depth=1)
                self.assertEqual([{"type": "SPOKEN", "text": "More words."}],
                                 analysis["regions"])
                self.assertEqual(0, analysis["final_depth"])
                self.assertEqual([], analysis["repairs"])

    def test_complete_segment_passes(self):
        source = " ".join(f"word{i}" for i in range(50))
        report = validate_segment_quality(source, [_seg(source)])
        self.assertTrue(report["passed"], report["findings"])

    def test_missing_type_field_fails(self):
        report = validate_segment_quality("A line.", [{"text": "A line."}])
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("missing_fields", codes)

    def test_invalid_type_value_fails(self):
        report = validate_segment_quality("A line.", [{"type": "MARCUS", "text": "A line."}])
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("invalid_type", codes)

    def test_severe_truncation_fails_recall(self):
        source = " ".join(f"word{i}" for i in range(100))
        report = validate_segment_quality(source, [_seg("word0 word1")])
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("low_source_token_recall", codes)

    def test_rejects_narration_combined_with_quoted_dialogue(self):
        source = '"Yes," Ilya said. "Stay behind me."'
        entries = [_seg("Yes,", "SPOKEN"),
                   _seg('Ilya said. "Stay behind me."', "NARRATOR")]
        codes = {f["code"] for f in validate_segment_quality(source, entries)["findings"]}
        self.assertIn("mixed_quote_region", codes)

    def test_rejects_attribution_tag_marked_spoken(self):
        source = '"No," Mara repeated, more firmly.'
        entries = [_seg("No,", "SPOKEN"),
                   _seg("Mara repeated, more firmly.", "SPOKEN")]
        codes = {f["code"] for f in validate_segment_quality(source, entries)["findings"]}
        self.assertIn("quote_region_misclassified", codes)

    def test_accepts_quote_aware_dialogue_and_attribution(self):
        source = '"Yes," Ilya said. "Stay behind me."'
        entries = [_seg("Yes,", "SPOKEN"), _seg("Ilya said.", "NARRATOR"),
                   _seg("Stay behind me.", "SPOKEN")]
        report = validate_segment_quality(source, entries)
        self.assertTrue(report["passed"], report["findings"])


class InchMarkTests(unittest.TestCase):
    # #2314: a straight mark after a number is an inch mark, not a delimiter,
    # so it must not invert every later quote. Inside open speech a mark after
    # a digit still closes, which is the case that cannot be told apart.

    def _regions(self, text, **kwargs):
        return [(row["type"], row["text"])
                for row in analyze_outer_quote_regions(text, **kwargs)["regions"]]

    def _types(self, text, **kwargs):
        return [row["type"] for row in analyze_outer_quote_regions(text, **kwargs)["regions"]]

    def test_inch_mark_does_not_invert_later_quotes(self):
        source = ('The carpenter measured a board that was 6" wide. "Hand me the saw," '
                  'Anna said. Ben passed it over. "Here you go," he replied.')
        analysis = analyze_outer_quote_regions(source)
        self.assertEqual(
            [("NARRATOR", 'The carpenter measured a board that was 6" wide.'),
             ("SPOKEN", "Hand me the saw,"),
             ("NARRATOR", "Anna said. Ben passed it over."),
             ("SPOKEN", "Here you go,"),
             ("NARRATOR", "he replied.")],
            [(row["type"], row["text"]) for row in analysis["regions"]])
        self.assertEqual([], analysis["repairs"])
        self.assertEqual(0, analysis["final_depth"])
        correct = analysis["regions"]
        inverted = [dict(row, type="SPOKEN" if row["type"] == "NARRATOR" else "NARRATOR")
                    for row in correct]
        good = validate_segment_quality(source, correct, quote_analysis=analysis)
        self.assertTrue(good["passed"], good["findings"])
        self.assertEqual("ran", good["quote_gate"])
        bad = validate_segment_quality(source, inverted, quote_analysis=analysis)
        self.assertFalse(bad["passed"])
        self.assertIn("quote_region_misclassified",
                      {finding["code"] for finding in bad["findings"]})
        self.assertNotIn("mixed_quote_region",
                         {finding["code"] for finding in bad["findings"]})

    def test_a_row_keeping_the_inch_mark_is_not_mixed_quote_region(self):
        source = ('The carpenter measured a board that was 6" wide. "Hand me the saw," '
                  'Anna said.')
        entries = [_seg('The carpenter measured a board that was 6" wide.'),
                   _seg('Hand me the saw,', 'SPOKEN'),
                   _seg('Anna said.')]
        report = validate_segment_quality(source, entries)
        self.assertTrue(report["passed"], report["findings"])
        self.assertNotIn("mixed_quote_region",
                         {finding["code"] for finding in report["findings"]})
        # The analyzer's row text and a row that omits the mark are both
        # located, because the shared comparison normalizes punctuation.
        without_mark = [dict(entry) for entry in entries]
        without_mark[0]["text"] = "The carpenter measured a board that was 6 wide."
        self.assertTrue(validate_segment_quality(source, without_mark)["passed"])

    def test_a_real_delimiter_still_makes_an_inch_mark_row_mixed(self):
        source = 'He said "the board was 6" wide."'
        codes = {finding["code"] for finding in validate_segment_quality(
            source, [_seg('He said "the board was 6" wide."')])["findings"]}
        self.assertIn("mixed_quote_region", codes)

    def test_a_real_closer_after_a_digit_does_not_swallow_the_narration(self):
        # Review blocking case: a later inch mark of the same character made
        # the real closer after a digit read as one, merging the narration into
        # the spoken region. German `„…“` is absent: its narration inch mark is
        # a second opener, which main also reads that way.
        cases = [
            ('"I am 12" she said. The board was 6" wide.',
             [("SPOKEN", "I am 12"), ("NARRATOR", 'she said. The board was 6" wide.')]),
            ('“I am 12” she said. The board was 6” wide.',
             [("SPOKEN", "I am 12"), ("NARRATOR", 'she said. The board was 6” wide.')]),
            ('「I am 12」 she said. The board was 6」 wide.',
             [("SPOKEN", "I am 12"), ("NARRATOR", 'she said. The board was 6」 wide.')]),
            ('『I am 12』 she said. The board was 6』 wide.',
             [("SPOKEN", "I am 12"), ("NARRATOR", 'she said. The board was 6』 wide.')]),
            ('«I am 12» she said. The board was 6» wide.',
             [("SPOKEN", "I am 12"), ("NARRATOR", 'she said. The board was 6» wide.')]),
            ('»I am 12« she said. The board was 6« wide.',
             [("SPOKEN", "I am 12"), ("NARRATOR", 'she said. The board was 6« wide.')]),
            ('„I am 12” she said. The board was 6” wide.',
             [("SPOKEN", "I am 12"), ("NARRATOR", 'she said. The board was 6” wide.')]),
            ('＂I am 12＂ she said. The board was 6＂ wide.',
             [("SPOKEN", "I am 12"), ("NARRATOR", 'she said. The board was 6＂ wide.')]),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, self._regions(source))
                self.assertEqual(0, analyze_outer_quote_regions(source)["final_depth"])

    def test_when_every_later_closer_is_after_a_digit_the_text_decides(self):
        # Nothing later can close the speech, so the mark closes at the bare
        # digit unless what follows is still a measurement.
        self.assertEqual(
            [("SPOKEN", "I am 12"), ("NARRATOR", 'she said. The board was 6” wide.')],
            self._regions('“I am 12” she said. The board was 6” wide.'))
        self.assertEqual(
            [("SPOKEN", 'It is 6” by 8'), ("NARRATOR", ", he said.")],
            self._regions('“It is 6” by 8”, he said.'))
        self.assertEqual(
            [("SPOKEN", 'It is 6” wide and 8')],
            self._regions('“It is 6” wide and 8”'))

    def test_a_digit_final_speech_closes_unless_something_can_still_close_it(self):
        # A closer-shaped mark after a bare digit is an inch mark only while a
        # later mark can still close the same speech. With none (`1945” long`)
        # or only the second speech's own number mark (`“Area 51”`), it closes.
        cases = [
            ('“It was 1945” long ago.',
             [("SPOKEN", "It was 1945"), ("NARRATOR", "long ago.")]),
            ('"We counted 20" high up on the wall.',
             [("SPOKEN", "We counted 20"), ("NARRATOR", "high up on the wall.")]),
            ('“Chapter 7” to 9 were missing.',
             [("SPOKEN", "Chapter 7"), ("NARRATOR", "to 9 were missing.")]),
            ('“He is 12” and 13 is old.',
             [("SPOKEN", "He is 12"), ("NARRATOR", "and 13 is old.")]),
            ('“Catch 22” high and low, she searched. “Area 51” too.',
             [("SPOKEN", "Catch 22"), ("NARRATOR", "high and low, she searched."),
              ("SPOKEN", "Area 51"), ("NARRATOR", "too.")]),
            ('"Catch 22" high and low, she searched. "Area 51" too.',
             [("SPOKEN", "Catch 22"), ("NARRATOR", "high and low, she searched."),
              ("SPOKEN", "Area 51"), ("NARRATOR", "too.")]),
            ('“It was 1945” long ago, she wrote.',
             [("SPOKEN", "It was 1945"), ("NARRATOR", "long ago, she wrote.")]),
            # A number mark before the new opener can still close the speech.
            ('“It is 6” wide and 8” He said so. “Fine.”',
             [("SPOKEN", 'It is 6” wide and 8'), ("NARRATOR", "He said so."),
              ("SPOKEN", "Fine.")]),
            # A self-paired opener (＂…＂) has no separate opener mark to bound
            # the search, so its later number mark is still a closer.
            ('＂It is 6" wide and 8＂', [("SPOKEN", 'It is 6" wide and 8')]),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, self._regions(source))
                self.assertEqual(0, analyze_outer_quote_regions(source)["final_depth"])

    def test_a_digit_final_speech_before_a_new_speaker_keeps_narration_out(self):
        # The narration after the digit and the next speaker's speech share the
        # same convention, so nothing in the marks says the speech continued.
        # Main closes at the digit; marking the narration SPOKEN was the #2318
        # regression.
        cases = [
            ('“Give me 5” high above his head, Tom raised a palm.\n\n'
             '“Ha!” said Ann.',
             [("SPOKEN", "Give me 5"),
              ("NARRATOR", "high above his head, Tom raised a palm."),
              ("SPOKEN", "Ha!"), ("NARRATOR", "said Ann.")]),
            ('"Table for 2" and 3 more chairs arrived.\n\n'
             '"Thanks," she said.',
             [("SPOKEN", "Table for 2"),
              ("NARRATOR", "and 3 more chairs arrived."),
              ("SPOKEN", "Thanks,"), ("NARRATOR", "she said.")]),
            ('„Give me 5“ high above his head, Tom raised a palm.\n\n'
             '„Ha!“ said Ann.',
             [("SPOKEN", "Give me 5"),
              ("NARRATOR", "high above his head, Tom raised a palm."),
              ("SPOKEN", "Ha!"), ("NARRATOR", "said Ann.")]),
            ('«Give me 5» high above his head, Tom raised a palm.\n\n'
             '«Ha!» said Ann.',
             [("SPOKEN", "Give me 5"),
              ("NARRATOR", "high above his head, Tom raised a palm."),
              ("SPOKEN", "Ha!"), ("NARRATOR", "said Ann.")]),
            ('「Give me 5」 high above his head, Tom raised a palm.\n\n'
             '「Ha!」 said Ann.',
             [("SPOKEN", "Give me 5"),
              ("NARRATOR", "high above his head, Tom raised a palm."),
              ("SPOKEN", "Ha!"), ("NARRATOR", "said Ann.")]),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, self._regions(source))
                self.assertEqual(0, analyze_outer_quote_regions(source)["final_depth"])

    def test_a_continued_speech_is_the_only_reason_to_keep_the_mark(self):
        # The name records the round that added the paragraph-reopen
        # exception; #2318 removed it. A paragraph that re-opens the same mark
        # cannot tell a continued speech from a new speaker, so main closes at
        # the digit, and this now matches main.
        for opener, closer in (('“', '”'), ('"', '"')):
            source = (opener + 'The board is 6' + closer + ' wide and\n\n'
                      + opener + 'it is heavy,' + closer + ' he said.')
            with self.subTest(source=source):
                self.assertEqual(
                    [("SPOKEN", 'The board is 6'), ("NARRATOR", "wide and"),
                     ("SPOKEN", "it is heavy,"), ("NARRATOR", "he said.")],
                    self._regions(source))
        # Without the measurement word the same break is two speeches.
        for opener, closer in (('“', '”'), ('"', '"')):
            source = (opener + 'I am 12' + closer + ' she said\n\n'
                      + opener + 'Good,' + closer + ' he said.')
            with self.subTest(source=source):
                self.assertEqual(
                    [("SPOKEN", "I am 12"), ("NARRATOR", "she said"),
                     ("SPOKEN", "Good,"), ("NARRATOR", "he said.")],
                    self._regions(source))
        # A paragraph that begins with something else never continues it.
        for source in ('“It was 1945” long ago.\n\nHe left.',
                       '"It was 1945" long ago.\n\nHe left.'):
            with self.subTest(source=source):
                self.assertEqual(
                    [("SPOKEN", "It was 1945"),
                     ("NARRATOR", "long ago.\n\nHe left.")],
                    self._regions(source))

    def test_a_mark_after_a_dash_colon_or_bracket_is_an_opener(self):
        # The next speech's opening mark sat behind `—`, `:` or `(`, so it
        # read as closer-shaped and this speech kept its digit mark as an inch
        # mark, merging the narration and the reply (#2318).
        cases = [
            ('"Room 5" he said—"...no, wait."',
             [("SPOKEN", "Room 5"), ("NARRATOR", "he said—"),
              ("SPOKEN", "...no, wait.")]),
            ('"Room 5" he said:"…no, wait."',
             [("SPOKEN", "Room 5"), ("NARRATOR", "he said:"),
              ("SPOKEN", "…no, wait.")]),
            ("\"Room 5\" (he said—\"'...no, wait.\")",
             [("SPOKEN", "Room 5"), ("NARRATOR", "(he said—"),
              ("SPOKEN", "'...no, wait."), ("NARRATOR", ")")]),
            ('＂Room 5＂ he said—＂…no, wait.＂',
             [("SPOKEN", "Room 5"), ("NARRATOR", "he said—"),
              ("SPOKEN", "…no, wait.")]),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, self._regions(source))
                self.assertEqual(0, analyze_outer_quote_regions(source)["final_depth"])

    def test_a_link_and_number_in_narration_do_not_keep_the_close(self):
        # `and 3` / `by 8` after the digit looked like `6" by 8"`, so the
        # paragraph's later inch mark kept the closer and the narration stayed
        # SPOKEN. The link counts only when the second number is itself
        # followed by the mark (#2318).
        cases = [
            ('“We leave at 5” and 3 of them nodded. The shelf was 12” deep.',
             [("SPOKEN", "We leave at 5"),
              ("NARRATOR", "and 3 of them nodded. The shelf was 12” deep.")]),
            ('"Meet me at 5" by 8 o\'clock she was gone. The shelf was 12" deep.',
             [("SPOKEN", "Meet me at 5"),
              ("NARRATOR", 'by 8 o\'clock she was gone. The shelf was 12" deep.')]),
            ('「We leave at 5」 to 9 they counted. The beam was 4」 thick.',
             [("SPOKEN", "We leave at 5"),
              ("NARRATOR", "to 9 they counted. The beam was 4」 thick.")]),
            ('«We leave at 5» x 2 they multiplied. The shelf was 12» deep.',
             [("SPOKEN", "We leave at 5"),
              ("NARRATOR", "x 2 they multiplied. The shelf was 12» deep.")]),
            ('“It is 6”, 8 people came. The shelf was 12” deep.',
             [("SPOKEN", "It is 6"),
              ("NARRATOR", ", 8 people came. The shelf was 12” deep.")]),
            ('“It is 6” × 8 people came. The shelf was 12” deep.',
             [("SPOKEN", "It is 6"),
              ("NARRATOR", "× 8 people came. The shelf was 12” deep.")]),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, self._regions(source))
                self.assertEqual(0, analyze_outer_quote_regions(source)["final_depth"])
        # A mark after the second number still makes it a measurement.
        self.assertEqual([("SPOKEN", 'It is 6" by 8" wide')],
                         self._regions('"It is 6" by 8" wide"'))
        self.assertEqual([("SPOKEN", 'It is 6” by 8” wide')],
                         self._regions('“It is 6” by 8” wide”'))
        self.assertEqual([("SPOKEN", 'It is 6”, 8” wide')],
                         self._regions('“It is 6”, 8” wide”'))
        # The second number may end with a different mark than the straight
        # inch mark that opened the measurement.
        self.assertEqual([("SPOKEN", 'It is 6" and 8')],
                         self._regions('“It is 6" and 8”'))
        self.assertEqual([("SPOKEN", 'It is 6" by 8')],
                         self._regions('«It is 6" by 8»'))

    def test_any_narration_punctuation_before_a_reply_mark_opens_a_speech(self):
        # The opener test knew `—`, `:` and `(` only. Every dash, bracket,
        # other punctuation or symbol, spaced or glued, and a full stop with
        # no space, leaves the reply's mark opener-shaped, so the digit mark
        # closes and the reply is its own region (#2318).
        cases = [
            ('"Room 5" he said–"...no, wait."',
             [("SPOKEN", "Room 5"), ("NARRATOR", "he said–"),
              ("SPOKEN", "...no, wait.")]),
            ('"Room 5" he said;"…no, wait."',
             [("SPOKEN", "Room 5"), ("NARRATOR", "he said;"),
              ("SPOKEN", "…no, wait.")]),
            ('"Room 5" he said/"...no, wait."',
             [("SPOKEN", "Room 5"), ("NARRATOR", "he said/"),
              ("SPOKEN", "...no, wait.")]),
            ('"Room 5" he said…"…no, wait."',
             [("SPOKEN", "Room 5"), ("NARRATOR", "he said…"),
              ("SPOKEN", "…no, wait.")]),
            ('"Room 5" he said*"...no, wait."',
             [("SPOKEN", "Room 5"), ("NARRATOR", "he said*"),
              ("SPOKEN", "...no, wait.")]),
            ('"Room 5" he said."...No."',
             [("SPOKEN", "Room 5"), ("NARRATOR", "he said."),
              ("SPOKEN", "...No.")]),
            ('"Room 5"he said"…no, wait."',
             [("SPOKEN", "Room 5"), ("NARRATOR", "he said"),
              ("SPOKEN", "…no, wait.")]),
            ('＂Room 5＂ he said：＂…no, wait.＂',
             [("SPOKEN", "Room 5"), ("NARRATOR", "he said："),
              ("SPOKEN", "…no, wait.")]),
            ('＂Room 5＂ he said（＂…no＂）.',
             [("SPOKEN", "Room 5"), ("NARRATOR", "he said（"),
              ("SPOKEN", "…no"), ("NARRATOR", "）.")]),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, self._regions(source))
                self.assertEqual(0, analyze_outer_quote_regions(source)["final_depth"])

    def test_a_unit_word_does_not_keep_the_close_when_narration_follows(self):
        # A unit word kept the digit mark as an inch mark even when the next
        # word began narration, so a later real inch mark in the paragraph
        # became the closer and the narration stayed SPOKEN. The unit branch
        # needs the confirmation the link branch has (#2318).
        cases = [
            ('"Room 12" deep in the cellar, it was cold. The shelf was 8" deep.',
             [("SPOKEN", "Room 12"),
              ("NARRATOR", 'deep in the cellar, it was cold. The shelf was 8" deep.')]),
            ('“Platform 9” long after midnight, the train left. The gap was 3” wide.',
             [("SPOKEN", "Platform 9"),
              ("NARRATOR", "long after midnight, the train left. The gap was 3” wide.")]),
            ('"Room 12" wide open and the shelf was 8" deep.',
             [("SPOKEN", "Room 12"),
              ("NARRATOR", 'wide open and the shelf was 8" deep.')]),
            ('"Room 12" deep. The shelf was 8" deep.',
             [("SPOKEN", "Room 12"),
              ("NARRATOR", 'deep. The shelf was 8" deep.')]),
            ('「Room 12」 across the yard, a dog barked. The board was 3」 deep.',
             [("SPOKEN", "Room 12"),
              ("NARRATOR", "across the yard, a dog barked. The board was 3」 deep.")]),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, self._regions(source))
                self.assertEqual(0, analyze_outer_quote_regions(source)["final_depth"])
        # True measurements are still one spoken region.
        self.assertEqual([("SPOKEN", 'It is 6" wide and 8')],
                         self._regions('"It is 6" wide and 8"'))
        self.assertEqual([("SPOKEN", 'It is 6” by 8')],
                         self._regions('“It is 6” by 8”'))
        # The English reply inside brackets does not hold the German speech
        # open, so the source map and the analyzer agree on it.
        german = ('„Hallo.“ sagte er.\n\n„Guten Tag.“ sagte sie.\n\n'
                  '„Wie geht es?“ fragte er.\n\n„Gut.“ sagte sie.\n\n')
        self.assertEqual([("SPOKEN", "Zimmer 5"), ("NARRATOR", "sagte er. ("),
                          ("SPOKEN", "nein"), ("NARRATOR", ").")],
                         self._regions(german + '„Zimmer 5“ sagte er. (“nein”).')[-4:])
        self.assertEqual([("SPOKEN", "Hallo."), ("NARRATOR", "sagte er."),
                          ("SPOKEN", "Guten Tag."), ("NARRATOR", "sagte sie."),
                          ("SPOKEN", "Wie geht es?"), ("NARRATOR", "fragte er."),
                          ("SPOKEN", "Gut."), ("NARRATOR", "sagte sie."),
                          ("SPOKEN", "Zimmer 5"), ("NARRATOR", "sagte er. ("),
                          ("SPOKEN", "nein"), ("NARRATOR", ").")],
                         self._regions(german + '„Zimmer 5“ sagte er. (“nein”).'))

    def test_german_inch_mark_speech_agrees_with_the_source_map(self):
        # German low-high (`„…“`) is scoped out to match `main` (#2357): both
        # the analyzer and `spoken_spans` end the speech at `6“` without
        # dropping it or merging into a later speech.
        prefix = '„Hallo.“ sagte er.\n\n„Guten Tag.“ sagte sie.\n\n'
        for body in ('„It is 6“ wide“ she said.',
                     '„It is 6“ wide“, sagte er.',
                     '„It is 6“ wide.“ Er ging.'):
            with self.subTest(body=body):
                source = prefix + body
                self.assertIn(("SPOKEN", "It is 6"), self._regions(source))
                self.assertEqual(['Hallo.', 'Guten Tag.', 'It is 6'],
                                 [source[a:b] for a, b in
                                  spoken_spans(source, "paired_quotes")])

    def test_full_width_inch_mark_keeps_measurement_without_inverting(self):
        # Full-width quotes (`＂…＂`) keep the inch mark as text and do not
        # invert the reporting clause or a later speech (#2318).
        self.assertEqual(
            [("SPOKEN", 'It is 6＂ wide'), ("NARRATOR", ', she said.')],
            self._regions('＂It is 6＂ wide＂, she said.'))
        self.assertEqual(
            [("SPOKEN", 'It is 6＂ wide'), ("NARRATOR", ', she said.'),
             ("SPOKEN", 'Fine.')],
            self._regions('＂It is 6＂ wide＂, she said. ＂Fine.＂'))

    def test_a_stray_closer_after_a_digit_keeps_narration_out(self):
        # Ambiguous input: `by 8”` could open the next speech. Closing at
        # `ten”` instead of main's swallow of `by 8` is deliberate, and the
        # unmatched closer is surfaced as a repair rather than hidden (#2318).
        source = '“It is ten” by 8” board” he said.'
        analysis = analyze_outer_quote_regions(source)
        self.assertEqual([("SPOKEN", "It is ten"),
                          ("NARRATOR", "by 8” board he said.")],
                         [(row["type"], row["text"])
                          for row in analysis["regions"]])
        self.assertEqual(["ignored_unmatched_close_quote"],
                         [row["code"] for row in analysis["repairs"]])
        # The same rule keeps an inch mark in the narration out of the speech.
        self.assertEqual([("SPOKEN", "Wait,"),
                          ("NARRATOR", "he said, holding the 6” board."),
                          ("SPOKEN", "Go.")],
                         self._regions(
                             '“Wait,” he said, holding the 6” board. “Go.”'))

    def test_roman_numeral_and_prime_before_closer_stay_known_limits(self):
        # Known limit pinned on head: `Ⅻ` (Unicode Nl, not Nd/No) and `′`
        # (Unicode Po) are not number characters in `is_number_char`, so the
        # following `”` counts as a real closer while `8”` stays a narration
        # inch mark and the final `”` surfaces as `ignored_unmatched_close_quote`
        # (main swallowed `by 8 board` into the spoken region).
        cases = [
            ('“It is Ⅻ” by 8” board” he said.',
             [("SPOKEN", "It is Ⅻ"),
              ("NARRATOR", "by 8” board he said.")]),
            ('“It is 6′” by 8” board” he said.',
             [("SPOKEN", "It is 6′"),
              ("NARRATOR", "by 8” board he said.")]),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                analysis = analyze_outer_quote_regions(source)
                self.assertEqual(expected,
                                 [(row["type"], row["text"])
                                  for row in analysis["regions"]])
                self.assertEqual(["ignored_unmatched_close_quote"],
                                 [row["code"] for row in analysis["repairs"]])
                self.assertEqual([text for kind, text in expected
                                  if kind == "SPOKEN"],
                                 [source[a:b] for a, b in
                                  spoken_spans(source, "paired_quotes")])

    def test_an_inch_mark_token_must_match_whole_in_another_region(self):
        # A row kept `5"`; the analyzed narration holds `15"`. A substring test
        # found `5"` inside `15"`, called the mark kept, and reported only
        # crosses_quote_boundary. A whole number-and-mark token must not match
        # inside a longer number (#2318 review).
        source = ('"Hello." he said.\n\n"We leave at 5" she said. '
                  'The shelf was 15" deep.')
        entries = [_seg("Hello.", "SPOKEN"), _seg("he said."),
                   _seg('We leave at 5" she said.'),
                   _seg('The shelf was 15" deep.')]
        codes = {finding["code"] for finding in
                 validate_segment_quality(source, entries)["findings"]}
        self.assertIn("mixed_quote_region", codes)
        self.assertNotIn("crosses_quote_boundary", codes)

    def test_a_unit_word_before_a_sentence_end_closes_in_curly_high(self):
        # The unit branch must not read a full stop after the unit as the
        # confirmation a quote mark gives. Curly high, not the straight case
        # already pinned: `wide.` with a later `8” deep` would otherwise keep
        # the digit mark and swallow the narration (#2318 review).
        self.assertEqual([("SPOKEN", "Room 12"),
                          ("NARRATOR", "wide. The board was 8” deep.")],
                         self._regions(
                             '“Room 12” wide. The board was 8” deep.'))

    def test_initial_depth_keeps_an_inch_mark_when_a_later_closer_follows(self):
        # A chunk continuation has no opener to consult, but a later real
        # closer in the paragraph still proves this mark is a measurement.
        for source, spoken in [
                ('The board is 6" wide," he said.', 'The board is 6" wide,'),
                ('The board is 6” wide,” he said.', 'The board is 6” wide,'),
                ('The board is 6＂ wide,＂ he said.', 'The board is 6＂ wide,')]:
            with self.subTest(source=source):
                self.assertEqual([("SPOKEN", spoken), ("NARRATOR", "he said.")],
                                 self._regions(source, initial_depth=1))
        # With no later closer there is nothing to prove it, so it closes.
        self.assertEqual([("SPOKEN", "I counted 5"), ("NARRATOR", "and left.")],
                         self._regions('I counted 5" and left.', initial_depth=1))

    def test_a_unicode_number_before_a_quote_is_a_measurement(self):
        for source in ('It is 3½” long.', 'It is 8½" long.', 'It is 8²" long.'):
            with self.subTest(source=source):
                self.assertEqual([("NARRATOR", source)], self._regions(source))
        # A CJK numeral is a letter, so this is a real pair.
        self.assertEqual([("SPOKEN", "三"), ("NARRATOR", "と言った。")],
                         self._regions('「三」と言った。'))

    def test_a_different_closer_right_after_the_inch_mark_is_the_real_one(self):
        # `«6"»`: the `"` is a measurement even though a closer follows
        # immediately, because it is a different mark, not a duplicate.
        self.assertEqual([("SPOKEN", 'C\'est 6"'), ("NARRATOR", ", dit-il.")],
                         self._regions("«C'est 6\"», dit-il."))

    def test_the_closer_search_stops_at_the_paragraph_end(self):
        # A stray closer in the next paragraph must not turn this paragraph's
        # real closer after a digit into an inch mark and merge the narration.
        analysis = analyze_outer_quote_regions('“I am 12” she said.\n\nGood.”')
        self.assertEqual([("SPOKEN", "I am 12"),
                          ("NARRATOR", "she said.\n\nGood.")],
                         [(row["type"], row["text"]) for row in analysis["regions"]])
        self.assertEqual(["ignored_unmatched_close_quote"],
                         [repair["code"] for repair in analysis["repairs"]])

    def test_a_duplicate_curly_closer_reads_as_main(self):
        self.assertEqual([("SPOKEN", "I am 12")], self._regions('“I am 12””'))
        self.assertEqual([("SPOKEN", "I am 12"), ("NARRATOR", "she said.")],
                         self._regions('“I am 12”” she said.'))

    def test_a_crlf_paragraph_reprise_does_not_keep_the_delimiter(self):
        # The analyzer reaches this final digit mark outside speech because
        # its paragraph-start test only knows LF. The paragraph's odd earlier
        # mark makes the mark the reprise closer, as main read it, instead of
        # leaving a delimiter in the narration.
        analysis = analyze_outer_quote_regions('"Part one.\r\n\r\n"Part two ends 5"')
        self.assertEqual([("SPOKEN", "Part one."), ("NARRATOR", "Part two ends 5")],
                         [(row["type"], row["text"]) for row in analysis["regions"]])
        self.assertEqual(["inferred_missing_close_quote"],
                         [repair["code"] for repair in analysis["repairs"]])

    def test_a_kept_real_closer_after_a_digit_is_still_mixed(self):
        # The gate may only accept a number+mark that the region also kept.
        cases = [
            ('"I am 12" he said.', 'I am 12"', "he said."),
            ('“I am 12” she said.', 'I am 12”', "she said."),
            ('「私は12」と彼女は言った。', '私は12」', "と彼女は言った。"),
            ('„Ich bin 12“ sagte er.', 'Ich bin 12“', "sagte er."),
        ]
        for source, kept, narration in cases:
            with self.subTest(source=source):
                codes = {finding["code"] for finding in validate_segment_quality(
                    source, [_seg(kept, "SPOKEN"), _seg(narration)])["findings"]}
                self.assertIn("mixed_quote_region", codes)

    def test_an_inch_mark_inside_open_speech_is_kept_and_does_not_invert(self):
        source = '"The board is 6" wide," he said. "Good," she said.'
        self.assertEqual(
            [("SPOKEN", 'The board is 6" wide,'),
             ("NARRATOR", "he said."),
             ("SPOKEN", "Good,"),
             ("NARRATOR", "she said.")],
            self._regions(source))
        analysis = analyze_outer_quote_regions(source)
        good = validate_segment_quality(source, analysis["regions"], quote_analysis=analysis)
        self.assertTrue(good["passed"], good["findings"])
        inverted = [dict(row, type="SPOKEN" if row["type"] == "NARRATOR" else "NARRATOR")
                    for row in analysis["regions"]]
        bad = validate_segment_quality(source, inverted, quote_analysis=analysis)
        self.assertFalse(bad["passed"])

    def test_two_inch_marks_and_feet_and_inches_inside_speech(self):
        self.assertEqual(
            [("SPOKEN", 'It is 6" by 4" wide,'), ("NARRATOR", "he said.")],
            self._regions('"It is 6" by 4" wide," he said.'))
        self.assertEqual(
            [("SPOKEN", 'I am 5\'10",'), ("NARRATOR", "he said.")],
            self._regions('"I am 5\'10"," he said.'))

    def test_an_opener_shaped_or_absent_next_mark_still_closes(self):
        self.assertEqual(["SPOKEN", "NARRATOR", "SPOKEN"],
                         self._types('"I counted 5" he said. "Next."'))
        self.assertEqual([("SPOKEN", "Only 5")], self._regions('"Only 5"'))
        self.assertEqual([("SPOKEN", "I counted 5"), ("NARRATOR", "and left.")],
                         self._regions('I counted 5" and left.', initial_depth=1))

    def test_a_digit_mark_outside_speech_after_a_close_stays_an_inch_mark(self):
        self.assertEqual(
            [("NARRATOR", "He said"),
             ("SPOKEN", "it was 6"),
             ("NARRATOR", 'and left 5" of rope.'),
             ("SPOKEN", "Ok.")],
            self._regions('He said "it was 6" and left 5" of rope. "Ok."'))

    def test_fullwidth_inch_mark_inside_speech(self):
        source = '＂The board is 6＂ wide,＂ he said. ＂Good,＂ she said.'
        self.assertEqual(
            [("SPOKEN", 'The board is 6＂ wide,'),
             ("NARRATOR", "he said."),
             ("SPOKEN", "Good,"),
             ("NARRATOR", "she said.")],
            self._regions(source))

    def test_different_inch_mark_inside_self_paired_speech_stays_spoken(self):
        """#2398: straight/curly inch mark inside a different self-paired opener stays SPOKEN."""
        for source, expected in (
            ('＂The board is 6" wide,＂ he said.',
             [("SPOKEN", 'The board is 6" wide,'), ("NARRATOR", "he said.")]),
            ('"The board is 6” wide," he said.',
             [("SPOKEN", 'The board is 6” wide,'), ("NARRATOR", "he said.")]),
            # Negative controls: opener-shaped later `"` starts a new speech, so `1945”` closes.
            ('"It was 1945” he said. "Next speech."',
             [("SPOKEN", "It was 1945"),
              ("NARRATOR", "he said."),
              ("SPOKEN", "Next speech.")]),
            ('"It was 1945” he said. "5 people came."',
             [("SPOKEN", "It was 1945"),
              ("NARRATOR", "he said."),
              ("SPOKEN", "5 people came.")]),
        ):
            with self.subTest(source=source):
                self.assertEqual(expected, self._regions(source))

    def test_compound_and_two_measurements_stay_narration(self):
        cases = [
            ('The board was 6" wide and the beam was 12" long. "Measure twice," she said.',
             [("NARRATOR", 'The board was 6" wide and the beam was 12" long.'),
              ("SPOKEN", "Measure twice,"),
              ("NARRATOR", "she said.")]),
            ('He stood 5\'10" tall. "Not bad," she said.',
             [("NARRATOR", 'He stood 5\'10" tall.'),
              ("SPOKEN", "Not bad,"),
              ("NARRATOR", "she said.")]),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, self._regions(source))

    def test_inch_mark_before_curly_speech_leaves_the_measurement_narration(self):
        source = 'The board was 6" long and he said “fine”. “Good,” she answered.'
        regions = self._regions(source)
        self.assertEqual(["fine", "Good,"],
                         [text for kind, text in regions if kind == "SPOKEN"])
        self.assertTrue(any(kind == "NARRATOR"
                            and text.startswith('The board was 6" long and he said')
                            for kind, text in regions), regions)

    def test_inch_mark_directly_before_a_new_straight_quote(self):
        for source in ('The gap was 12""Move," he said. "Now."',
                       'The gap was 12" "Move," he said. "Now."'):
            with self.subTest(source=source):
                self.assertEqual(
                    [("NARRATOR", 'The gap was 12"'),
                     ("SPOKEN", "Move,"),
                     ("NARRATOR", "he said."),
                     ("SPOKEN", "Now.")],
                    self._regions(source))

    def test_a_real_closer_after_a_digit_inside_speech_still_closes(self):
        self.assertEqual(["SPOKEN", "NARRATOR", "SPOKEN"],
                         self._types('"I counted 5," he said. "Only 5"'))
        reprise = analyze_outer_quote_regions('"Part one.\n\n"Part two ends 5"')
        self.assertEqual(["SPOKEN", "SPOKEN"],
                         [row["type"] for row in reprise["regions"]])
        self.assertEqual(0, reprise["final_depth"])
        self.assertEqual(["SPOKEN", "NARRATOR"],
                         self._types('"6 of us," he said. The elevator closed.'))

    def test_an_inch_mark_inside_an_open_quote_keeps_todays_closing_behaviour(self):
        # Ambiguous: at depth one this mark could be a closer after a digit or
        # an inch mark. Today it closes, and that is what stays true.
        self.assertEqual([("SPOKEN", "I counted 5"), ("NARRATOR", "and left.")],
                         self._regions('I counted 5" and left.', initial_depth=1))

    def test_inch_mark_after_a_continued_speech_closes_at_zero_depth(self):
        analysis = analyze_outer_quote_regions(
            'done." The board was 6" wide. "Next," she said.', initial_depth=1)
        self.assertEqual(
            [("SPOKEN", "done."),
             ("NARRATOR", 'The board was 6" wide.'),
             ("SPOKEN", "Next,"),
             ("NARRATOR", "she said.")],
            [(row["type"], row["text"]) for row in analysis["regions"]])
        self.assertEqual(0, analysis["final_depth"])

    def test_inch_mark_in_narration_after_reopened_speech(self):
        source = ('"First paragraph.\n\n"Second paragraph ends here." '
                  'Then the board, 6" wide, fell.')
        self.assertEqual(
            [("SPOKEN", "First paragraph."),
             ("SPOKEN", "Second paragraph ends here."),
             ("NARRATOR", 'Then the board, 6" wide, fell.')],
            self._regions(source))

    def test_the_real_inch_sign_is_not_a_delimiter(self):
        self.assertEqual(
            [("NARRATOR", "The board was 6\u2033 wide."),
             ("SPOKEN", "Hand me the saw,"),
             ("NARRATOR", "Anna said.")],
            self._regions('The board was 6\u2033 wide. "Hand me the saw," Anna said.'))

    def test_a_single_quote_book_is_unchanged(self):
        source = "He said 'hello.' She left. It was 6\" wide."
        self.assertEqual([("NARRATOR", 'He said \'hello.\' She left. It was 6" wide.')],
                         self._regions(source))

    def test_narration_with_only_an_inch_mark_is_one_narrator_region(self):
        source = 'The board was 6" wide and nothing else happened.'
        analysis = analyze_outer_quote_regions(source)
        self.assertEqual([("NARRATOR", 'The board was 6" wide and nothing else happened.')],
                         [(row["type"], row["text"]) for row in analysis["regions"]])
        self.assertEqual([], analysis["repairs"])
        self.assertTrue(validate_segment_quality(
            source, analysis["regions"], quote_analysis=analysis)["passed"])

    def test_speech_regions_match_the_source_map_with_inch_marks(self):
        from dialogue_spans import spoken_spans
        for source in ('"The board is 6" wide," he said. "Good," she said.',
                       '"It is 6" by 4" wide," he said.',
                       '"I counted 5" he said. "Next."',
                       'He said "it was 6" and left 5" of rope. "Ok."'):
            with self.subTest(source=source):
                regions = analyze_outer_quote_regions(source)["regions"]
                self.assertEqual(
                    [row["text"] for row in regions if row["type"] == "SPOKEN"],
                    [source[start:end] for start, end in
                     spoken_spans(source, "paired_quotes")])

    def test_an_opener_glued_after_a_dash_keeps_todays_close(self):
        self.assertEqual(
            [("SPOKEN", "I counted 5"),
             ("NARRATOR", "\u2014"),
             ("SPOKEN", "Next,"),
             ("NARRATOR", "she said.")],
            self._regions('"I counted 5"\u2014"Next," she said.'))

    def test_the_analyzer_and_the_source_map_share_one_inch_mark_rule(self):
        import dialogue_spans
        import pass_quality
        self.assertIs(pass_quality._is_inch_mark, dialogue_spans.is_inch_mark)

    # --- Item A: a straight inch mark inside speech opened by another mark ---

    def test_a_straight_inch_mark_inside_a_curly_speech_stays_spoken(self):
        source = '\u201cThe board is 6" wide,\u201d he said. \u201cGood,\u201d she said.'
        expected = [("SPOKEN", 'The board is 6" wide,'),
                    ("NARRATOR", "he said."),
                    ("SPOKEN", "Good,"),
                    ("NARRATOR", "she said.")]
        analysis = analyze_outer_quote_regions(source)
        self.assertEqual([], analysis["repairs"])
        self.assertEqual(expected, [(row["type"], row["text"])
                                    for row in analysis["regions"]])
        self.assertEqual([text for kind, text in expected if kind == "SPOKEN"],
                         [source[a:b] for a, b in
                          spoken_spans(source, "paired_quotes")])
        good = validate_segment_quality(source, analysis["regions"],
                                        quote_analysis=analysis)
        self.assertTrue(good["passed"], good["findings"])
        inverted = [dict(row, type="SPOKEN" if row["type"] == "NARRATOR"
                         else "NARRATOR") for row in analysis["regions"]]
        self.assertFalse(validate_segment_quality(
            source, inverted, quote_analysis=analysis)["passed"])

    def test_a_straight_inch_mark_inside_german_speech_stays_spoken(self):
        source = '\u201eEs ist 6" breit\u201c, sagte er. \u201eGut.\u201c'
        expected = [("SPOKEN", 'Es ist 6" breit'),
                    ("NARRATOR", ", sagte er."),
                    ("SPOKEN", "Gut.")]
        analysis = analyze_outer_quote_regions(source)
        self.assertEqual([], analysis["repairs"])
        self.assertEqual(expected, [(row["type"], row["text"])
                                    for row in analysis["regions"]])
        self.assertEqual([text for kind, text in expected if kind == "SPOKEN"],
                         [source[a:b] for a, b in
                          spoken_spans(source, "paired_quotes")])
        self.assertTrue(validate_segment_quality(
            source, analysis["regions"], quote_analysis=analysis)["passed"])

    def test_a_curly_inch_mark_inside_german_speech_stays_spoken(self):
        # German low-high (`„…“`) closes with `“`, so an interior curly `”`
        # after a digit is an inch mark rather than a closer. Unlike `“`
        # (#2357), curly `”` inside German is fixed in the analyzer and agrees
        # with `spoken_spans` instead of inverting narration and later speech.
        prefix = '„A“ „B“ „C“ „D“ „E“ '
        source = (prefix + '„Es ist 6” breit“, sagte er. '
                  'Dann „Komm“, sagte sie.')
        expected = [
            ("SPOKEN", "A"), ("SPOKEN", "B"), ("SPOKEN", "C"),
            ("SPOKEN", "D"), ("SPOKEN", "E"),
            ("SPOKEN", "Es ist 6” breit"),
            ("NARRATOR", ", sagte er. Dann"),
            ("SPOKEN", "Komm"),
            ("NARRATOR", ", sagte sie."),
        ]
        analysis = analyze_outer_quote_regions(source)
        self.assertEqual([], analysis["repairs"])
        self.assertEqual(expected, [(row["type"], row["text"])
                                    for row in analysis["regions"]])
        self.assertEqual([text for kind, text in expected if kind == "SPOKEN"],
                         [source[a:b] for a, b in
                          spoken_spans(source, "paired_quotes")])
        self.assertTrue(validate_segment_quality(
            source, analysis["regions"], quote_analysis=analysis)["passed"])

    def test_a_straight_inch_mark_inside_guillemet_and_japanese_speech(self):
        cases = [
            ('\u00abC\'est 6" de large\u00bb, dit-il.',
             [("SPOKEN", 'C\'est 6" de large'), ("NARRATOR", ", dit-il.")]),
            ('\u300c6"\u306e\u677f\u300d\u3068\u8a00\u3063\u305f\u3002',
             [("SPOKEN", '6"\u306e\u677f'), ("NARRATOR", "\u3068\u8a00\u3063\u305f\u3002")]),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, self._regions(source))
                self.assertEqual([text for kind, text in expected if kind == "SPOKEN"],
                                 [source[a:b] for a, b in
                                  spoken_spans(source, "paired_quotes")])

    def test_a_german_speech_closed_with_a_straight_mark_still_closes(self):
        # Pinned: `„...“` opened German, closed with a straight mark, and a new
        # `„` before the proper closer means the straight mark is the closer.
        source = '\u201eEs ist 5" sagte er. \u201eGut."'
        self.assertEqual(
            [("SPOKEN", "Es ist 5"), ("NARRATOR", "sagte er."),
             ("SPOKEN", "Gut.")],
            self._regions(source))

    # --- Item B: a curly inch mark ---

    def test_a_curly_inch_mark_inside_speech_is_kept(self):
        source = '\u201cThe board is 6\u201d wide,\u201d he said. \u201cGood,\u201d she said.'
        expected = [("SPOKEN", 'The board is 6\u201d wide,'),
                    ("NARRATOR", "he said."),
                    ("SPOKEN", "Good,"),
                    ("NARRATOR", "she said.")]
        analysis = analyze_outer_quote_regions(source)
        self.assertEqual([], analysis["repairs"])
        self.assertEqual(expected, [(row["type"], row["text"])
                                    for row in analysis["regions"]])
        self.assertEqual([text for kind, text in expected if kind == "SPOKEN"],
                         [source[a:b] for a, b in
                          spoken_spans(source, "paired_quotes")])
        good = validate_segment_quality(source, analysis["regions"],
                                        quote_analysis=analysis)
        self.assertTrue(good["passed"], good["findings"])
        # A row that keeps the mark is not refused as mixed; the merged row is.
        self.assertNotIn("mixed_quote_region",
                         {f["code"] for f in validate_segment_quality(
                             "The board was 6\u201d wide.",
                             [_seg("The board was 6\u201d wide.")])["findings"]})
        self.assertIn("mixed_quote_region",
                      {f["code"] for f in validate_segment_quality(
                          source, [{"type": "SPOKEN",
                                    "text": 'The board is 6\u201d wide,\u201d he said.'}])["findings"]})

    def test_a_curly_inch_mark_outside_speech_is_text(self):
        cases = [
            ('The board was 6\u201d wide. \u201cGood,\u201d she said.',
             [("NARRATOR", "The board was 6\u201d wide."),
              ("SPOKEN", "Good,"), ("NARRATOR", "she said.")]),
            ('He was 12\u201d. \u201cGood,\u201d she said.',
             [("NARRATOR", "He was 12\u201d."),
              ("SPOKEN", "Good,"), ("NARRATOR", "she said.")]),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                analysis = analyze_outer_quote_regions(source)
                self.assertEqual([], analysis["repairs"])
                self.assertEqual(expected, [(row["type"], row["text"])
                                            for row in analysis["regions"]])
                self.assertTrue(validate_segment_quality(
                    source, analysis["regions"], quote_analysis=analysis)["passed"])

    def test_a_real_curly_closer_after_a_digit_still_closes(self):
        self.assertEqual(
            [("SPOKEN", "I am 12"), ("NARRATOR", "he said."),
             ("SPOKEN", "Good.")],
            self._regions('\u201cI am 12\u201d he said. \u201cGood.\u201d'))
        # One-row merge of speech and narration stays refused in every form.
        for source in ('"I am 12" he said.',
                       '\u201cI am 12\u201d he said.',
                       '\u201eIch bin 12" sagte er.'):
            with self.subTest(source=source):
                codes = {f["code"] for f in validate_segment_quality(
                    source, [_seg(source)])["findings"]}
                self.assertIn("mixed_quote_region", codes)

    def test_curly_feet_and_inches_are_text(self):
        cases = [
            ('\u201cHe is 5\u201910\u201d tall,\u201d she said.',
             [("SPOKEN", "He is 5\u201910\u201d tall,"), ("NARRATOR", "she said.")]),
            ('He is 5\u201910\u201d tall. \u201cGo,\u201d he said.',
             [("NARRATOR", "He is 5\u201910\u201d tall."),
              ("SPOKEN", "Go,"), ("NARRATOR", "he said.")]),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, self._regions(source))

    def test_a_curly_or_straight_inch_mark_at_a_line_end_inside_speech(self):
        cases = [
            ('\u201cThe board is 6\u201d\nwide,\u201d he said.',
             [("SPOKEN", "The board is 6\u201d\nwide,"), ("NARRATOR", "he said.")]),
            ('\u201cThe board is 6"\nwide,\u201d he said.',
             [("SPOKEN", 'The board is 6"\nwide,'), ("NARRATOR", "he said.")]),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                self.assertEqual(expected, self._regions(source))

    def test_a_multi_paragraph_speech_without_a_closer_keeps_todays_answer(self):
        # Deliberate limit: the `”` has no later `”` in its paragraph, so it
        # closes (8c2e355). Extending it to the re-opened next paragraph would
        # read the reporting clause of `“I am 12” she said.` as speech, which
        # is the worse error. Pinned with a test rather than guessed.
        source = '\u201cI have 5\u201d of rope.\n\n\u201cAnd more.\u201d'
        expected = [("SPOKEN", "I have 5"), ("NARRATOR", "of rope."),
                    ("SPOKEN", "And more.")]
        self.assertEqual(expected, self._regions(source))
        self.assertEqual([text for kind, text in expected if kind == "SPOKEN"],
                         [source[a:b] for a, b in
                          spoken_spans(source, "paired_quotes")])
        # The counterexample: `she said.` must stay narration.
        self.assertEqual(
            [("SPOKEN", "I am 12"), ("NARRATOR", "she said."),
             ("SPOKEN", "Good.")],
            self._regions('\u201cI am 12\u201d she said.\n\n\u201cGood.\u201d'))

    def test_a_curly_inch_mark_in_nested_speech(self):
        source = '\u201cHe said \u2018it is 6\u201d wide\u2019 to me,\u201d she said.'
        expected = [("SPOKEN", "He said \u2018it is 6\u201d wide\u2019 to me,"),
                    ("NARRATOR", "she said.")]
        self.assertEqual(expected, self._regions(source))
        self.assertEqual([text for kind, text in expected if kind == "SPOKEN"],
                         [source[a:b] for a, b in
                          spoken_spans(source, "paired_quotes")])

    def test_a_decorative_closer_does_not_swallow_an_inch_mark_in_narration(self):
        # `“Ha!”Haha!”Hahaha!”` keeps its decorative closers, but a `”` after a
        # number in the following narration is an inch mark, not a reason to
        # defer the real closer.
        self.assertEqual(
            [("SPOKEN", "Ha!Haha!Hahaha!"), ("NARRATOR", "Then she stopped.")],
            self._regions('\u201cHa!\u201dHaha!\u201dHahaha!\u201d Then she stopped.'))
        self.assertEqual(
            [("SPOKEN", "Good,"),
             ("NARRATOR", "she said. The board was 6\u201d wide.")],
            self._regions('\u201cGood,\u201d she said. The board was 6\u201d wide.'))

    # --- Item C: outside speech at a line/paragraph end ---

    def test_a_straight_or_curly_inch_mark_at_a_paragraph_end_is_text(self):
        for source in ('The board was 6"', 'The board was 6".',
                       'The board was 6\u201d', 'The board was 6\u201d.'):
            with self.subTest(source=source):
                analysis = analyze_outer_quote_regions(source)
                self.assertEqual([("NARRATOR", source)],
                                 [(row["type"], row["text"])
                                  for row in analysis["regions"]])
                self.assertEqual([], analysis["repairs"])
                self.assertTrue(validate_segment_quality(
                    source, analysis["regions"], quote_analysis=analysis)["passed"])


class AttributionTests(unittest.TestCase):
    def test_attribution_passes_when_all_spoken_named(self):
        frozen = [{"type": "NARRATOR", "text": "The room was cold."},
                  {"type": "SPOKEN", "text": "Tell me."}]
        resp = [{"n": 0, "head": "The room was", "speaker": "NARRATOR"},
                {"n": 1, "head": "Tell me.", "speaker": "ELENA"}]
        report = validate_attribution(frozen, resp)
        self.assertTrue(report["passed"], report["findings"])

    def test_attribution_passes_when_response_reordered_by_index(self):
        # Binding is by index, so a reordered response still validates + binds.
        frozen = [{"type": "NARRATOR", "text": "The room was cold."},
                  {"type": "SPOKEN", "text": "Tell me."}]
        resp = [{"n": 1, "head": "Tell me.", "speaker": "ELENA"},
                {"n": 0, "head": "The room was", "speaker": "NARRATOR"}]
        report = validate_attribution(frozen, resp)
        self.assertTrue(report["passed"], report["findings"])

    def test_attribution_fails_when_spoken_left_as_narrator(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me."}]
        resp = [{"n": 0, "head": "Tell me.", "speaker": "NARRATOR"}]
        report = validate_attribution(frozen, resp)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("spoken_not_named", codes)

    def test_attribution_finding_carries_source_and_expected_context(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me exactly."}]
        report = validate_attribution(
            frozen, [{"n": 0, "head": "Tell me exactly.", "speaker": "NARRATOR"}])
        finding = report["findings"][0]
        self.assertEqual("Tell me exactly.", finding["source_line"])
        self.assertEqual("a character name, not NARRATOR or an empty value",
                         finding["expected"])
        rendered = format_validation_findings(report["findings"])
        self.assertIn('entry 1', rendered)
        self.assertIn('source line="Tell me exactly."', rendered)
        self.assertIn("expected=a character name", rendered)

        renamed = validate_attribution(
            [{"type": "NARRATOR", "text": "The door opened."}],
            [{"n": 0, "head": "The door opened.", "speaker": "ERIS"}])
        self.assertIn('rejected="ERIS"',
                      format_validation_findings(renamed["findings"]))

    def test_attribution_ignores_legacy_head_when_index_is_valid(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me."}]
        resp = [{"n": 0, "head": "Something else entirely", "speaker": "ELENA"}]
        report = validate_attribution(frozen, resp)
        self.assertTrue(report["passed"])

    def test_attribution_fails_on_duplicate_or_missing_index(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me."},
                  {"type": "SPOKEN", "text": "Now go."}]
        resp = [{"n": 0, "head": "Tell me.", "speaker": "ELENA"},
                {"n": 0, "head": "Tell me.", "speaker": "ELENA"}]  # dup 0, index 1 missing
        report = validate_attribution(frozen, resp)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("alignment_violated", codes)


class IndexHeadCheckTests(unittest.TestCase):
    def test_reproduces_gemma_drift_survives(self):
        # The exact class of failure that crashed the run: the model would drift
        # on a long line's full text. Under the new contract it only echoes the
        # head + speaker, so a correct head/speaker validates regardless of what
        # it would have done to the body.
        frozen = [{"type": "NARRATOR",
                   "text": "The strength in those clinging fingers was weak, and not "
                           "even Beatrice knew what she was trying to do with this touch."}]
        resp = [{"n": 0, "head": "The strength in those", "speaker": "NARRATOR"}]
        ok, reason, ordered = index_head_check(frozen, resp)
        self.assertTrue(ok, reason)
        self.assertEqual(0, ordered[0]["n"])

    def test_punctuation_only_line_needs_no_head(self):
        frozen = [{"type": "SPOKEN", "text": "――――."}]
        ok, _, _ = index_head_check(frozen, [{"n": 0, "head": "", "speaker": "RYUZU"}])
        self.assertTrue(ok)

    def test_head_is_ignored_when_immutable_indexes_are_complete(self):
        frozen = [{"type": "SPOKEN", "text": "The old door opened."},
                  {"type": "SPOKEN", "text": "The old guard waited."}]
        short = [{"n": 0, "head": "The", "speaker": "A"},
                 {"n": 1, "head": "The old guard", "speaker": "B"}]
        ambiguous = [{"n": 0, "head": "The old", "speaker": "A"},
                     {"n": 1, "head": "The old guard", "speaker": "B"}]
        self.assertTrue(index_head_check(frozen, short)[0])
        self.assertTrue(index_head_check(frozen, ambiguous)[0])

    def test_rejects_duplicate_or_missing_immutable_indexes(self):
        frozen = [{"type": "SPOKEN", "text": "A"},
                  {"type": "SPOKEN", "text": "B"}]
        response = [{"n": 0, "speaker": "A"}, {"n": 0, "speaker": "B"}]
        self.assertFalse(index_head_check(frozen, response)[0])

    def test_accepts_integral_float_index(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me now."}]
        ok, reason, _ = index_head_check(
            frozen, [{"n": 0.0, "head": "Tell me now", "speaker": "ELENA"}])
        self.assertTrue(ok, reason)

    def test_non_string_model_fields_fail_validation_without_crashing(self):
        frozen = [{"type": "SPOKEN", "text": "Tell me now."}]
        attribution = validate_attribution(
            frozen, [{"n": 0, "head": "Tell me now", "speaker": {"name": "ELENA"}}])
        instruct = validate_instruct(
            [{"speaker": "ELENA", "text": "Tell me now."}],
            [{"n": 0, "head": "Tell me now", "instruct": ["firm"]}])
        self.assertFalse(attribution["passed"])
        self.assertFalse(instruct["passed"])

    def test_instruct_finding_uses_frozen_source_line(self):
        report = validate_instruct(
            [{"speaker": "ELENA", "text": "Stay here."}],
            [{"n": 0, "head": "Stay here.", "instruct": ""}])
        self.assertEqual("Stay here.", report["findings"][0]["source_line"])


class InstructValidatorTests(unittest.TestCase):
    def test_passes_when_all_have_instruct(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        resp = [{"n": 0, "head": "Tell me.", "instruct": "Firm, quiet."}]
        report = validate_instruct(prior, resp)
        self.assertTrue(report["passed"], report["findings"])

    def test_fails_when_instruct_missing_or_empty(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        resp = [{"n": 0, "head": "Tell me.", "instruct": "  "}]
        report = validate_instruct(prior, resp)
        codes = {f["code"] for f in report["findings"]}
        self.assertIn("missing_instruct", codes)

    def test_ignores_legacy_head_when_index_is_valid(self):
        prior = [{"speaker": "ELENA", "text": "Tell me."}]
        resp = [{"n": 0, "head": "Completely different words", "instruct": "Firm."}]
        report = validate_instruct(prior, resp)
        self.assertTrue(report["passed"])


class PromptLoaderTests(unittest.TestCase):
    def test_three_pass_prompts_load_and_have_placeholders(self):
        seg_sys, seg_usr = default_prompts.load_segment_prompts()
        self.assertIn("{chunk}", seg_usr)
        self.assertTrue(seg_sys.strip())
        att_sys, att_usr = default_prompts.load_attribute_prompts()
        self.assertIn("{batch}", att_usr)
        self.assertIn("{roster}", att_usr)
        ins_sys, ins_usr = default_prompts.load_instruct_prompts()
        self.assertIn("{batch}", ins_usr)


class AttributionRejectsTypeAsSpeakerTest(unittest.TestCase):
    """The attribution prompt shows each entry as {"n", "type", "text"}, and the
    model sometimes echoes the type back as the speaker. The gate required a
    SPOKEN span's speaker to be non-empty and not NARRATOR, which "SPOKEN"
    satisfies - so it passed. Measured live: 326 entries in one book, and the
    leak appeared in 8 of 9 books overnight."""

    def test_narrative_metadata_is_rejected_even_without_source_or_with_a_roster(self):
        frozen = [{"type": "SPOKEN", "text": "Oh-ho."}]
        for speaker in ("NARRATIVE", "narrative"):
            for kwargs in ({}, {"source_text": "Narrative appeared. Narrative returned.",
                                "known_names": {"NARRATIVE"}}):
                with self.subTest(speaker=speaker, kwargs=kwargs):
                    report = validate_attribution(frozen, [{"n": 0, "head": "Oh-ho.",
                                                          "speaker": speaker}], **kwargs)
                    self.assertFalse(report["passed"])
                    self.assertEqual("speaker_is_entry_type", report["findings"][0]["code"])

    def test_literal_type_name_is_rejected(self):
        frozen = [{"type": "SPOKEN", "text": "Oh-ho."}]
        report = validate_attribution(frozen, [{"n": 0, "head": "Oh-ho.",
                                                "speaker": "SPOKEN"}])
        self.assertFalse(report["passed"])
        self.assertTrue(any("type" in f.get("code", "")
                            or "type" in f.get("message", "").lower()
                            for f in report["findings"]))

    def test_rejection_is_case_insensitive(self):
        frozen = [{"type": "SPOKEN", "text": "Oh-ho."}]
        report = validate_attribution(frozen, [{"n": 0, "head": "Oh-ho.",
                                                "speaker": "spoken"}])
        self.assertFalse(report["passed"])

    def test_a_real_name_still_passes(self):
        frozen = [{"type": "SPOKEN", "text": "Oh-ho."}]
        report = validate_attribution(frozen, [{"n": 0, "head": "Oh-ho.",
                                                "speaker": "SHIRON"}])
        self.assertTrue(report["passed"], report["findings"])

    def test_unknown_placeholder_is_still_allowed(self):
        # UNKNOWN is the legitimate placeholder stabilize_speaker_identities
        # assigns; it must not be swept up with the type-name rejection.
        frozen = [{"type": "SPOKEN", "text": "Oh-ho."}]
        report = validate_attribution(frozen, [{"n": 0, "head": "Oh-ho.",
                                                "speaker": "UNKNOWN"}])
        self.assertTrue(report["passed"], report["findings"])




class GateParityAndMessagesTests(unittest.TestCase):
    def test_segment_gate_rejects_introduced_cyrillic(self):
        src = "the quick brown fox jumps over the lazy dog today"
        rep = validate_segment_quality(src, [{"type": "NARRATOR", "text": src.replace("fox", "fох")}])
        self.assertIn("unsupported_cyrillic", {f["code"] for f in rep["findings"]})

    def test_segment_findings_carry_message(self):
        rep = validate_segment_quality("a b c", [{"type": "MARCUS", "text": "a b c"}])
        for f in rep["findings"]:
            self.assertIn("message", f, f"finding {f['code']} lacks a message")

    def test_attribution_and_instruct_findings_carry_message(self):
        att = validate_attribution([{"type": "SPOKEN", "text": "Hi."}],
                                   [{"n": 0, "head": "Hi.", "speaker": "NARRATOR"}])
        ins = validate_instruct([{"speaker": "X", "text": "Hi."}],
                                [{"n": 0, "head": "Hi.", "instruct": ""}])
        for f in att["findings"] + ins["findings"]:
            self.assertIn("message", f)

    def test_segment_thresholds_are_shared_with_chunk_quality(self):
        import chunk_quality
        self.assertIs(MIN_ORDERED_TRIGRAM_RECALL, chunk_quality.MIN_ORDERED_TRIGRAM_RECALL)

    def test_introduced_unicode_finding_uses_dict_shape(self):
        # Must match chunk_quality's shape: characters are dicts with
        # character/codepoint/name, not bare "U+XXXX" strings, so consumers
        # that read finding["characters"][0]["character"] don't KeyError.
        src = "the quick brown fox jumps over the lazy dog today"
        rep = validate_segment_quality(src, [{"type": "NARRATOR", "text": src + " café"}])
        uni = [f for f in rep["findings"] if f["code"] == "unsupported_unicode_character"]
        self.assertTrue(uni, "expected an unsupported_unicode_character finding")
        char = uni[0]["characters"][0]
        self.assertEqual({"character", "codepoint", "name"}, set(char.keys()))
        self.assertEqual("é", char["character"])


class PassOneSeamDuplicateTests(unittest.TestCase):
    """A short lexical quote duplicated at a split seam (issue #2143).

    Deliberately use realistic chunk size: on a tiny chunk the aggregate
    recall/ratio metrics happen to catch the extra copy, which hides the real
    defect. At book-chunk scale only a provenance-aware duplicate check can
    see it. Synthetic text only.
    """

    FILLER = ("The old stone gate groaned softly as the morning rain fell "
              "across the quiet yard. " * 120)
    GATE = {"quoted_must_be_spoken": False, "unquoted_must_be_narrator": False}

    def _report(self, source, entries):
        return validate_segment_quality(source, entries, **self.GATE)

    def test_duplicated_lexical_quote_is_rejected_with_clean_aggregate_metrics(self):
        source = (self.FILLER
                  + "At the gate stood the [Silver Knight]. He waited there quietly.")
        faithful = [
            _seg(self.FILLER.strip()),
            _seg("At the gate stood the [Silver Knight]. He waited there quietly."),
        ]
        duplicated = [
            _seg(self.FILLER.strip()),
            _seg("At the gate stood the [Silver Knight]."),
            _seg("Silver Knight"),
            _seg(". He waited there quietly."),
        ]
        clean = self._report(source, faithful)
        self.assertTrue(clean["passed"], clean["findings"])

        report = self._report(source, duplicated)
        metrics = report["metrics"]
        self.assertGreaterEqual(metrics["source_token_recall"], 0.99)
        self.assertGreaterEqual(metrics["ordered_trigram_recall"], 0.99)
        self.assertLessEqual(metrics["output_source_ratio"], 1.01)
        self.assertFalse(report["passed"])
        finding = next(f for f in report["findings"]
                       if f["code"] == "source_unsupported_duplicate")
        self.assertIn(3, finding["entry_numbers"])
        self.assertEqual("silver knight", finding["details"]["phrase"])

    def test_embedded_copy_at_next_entry_head_is_rejected(self):
        source = (self.FILLER
                  + "At the gate stood the [Silver Knight]. He waited there quietly.")
        entries = [
            _seg(self.FILLER.strip()),
            _seg("At the gate stood the [Silver Knight]."),
            _seg("Silver Knight. He waited there quietly."),
        ]
        report = self._report(source, entries)
        self.assertGreaterEqual(report["metrics"]["ordered_trigram_recall"], 0.99)
        self.assertLessEqual(report["metrics"]["output_source_ratio"], 1.01)
        finding = next(f for f in report["findings"]
                       if f["code"] == "source_unsupported_duplicate")
        self.assertEqual("silver knight", finding["details"]["phrase"])
        self.assertEqual({2, 3}, set(finding["entry_numbers"]))

    def test_repeated_quote_inside_one_entry_is_rejected(self):
        source = (self.FILLER
                  + "At the gate stood the [Silver Knight]. He waited there quietly.")
        entries = [
            _seg(self.FILLER.strip()),
            _seg("At the gate stood the [Silver Knight] [Silver Knight]. "
                 "He waited there quietly."),
        ]
        report = self._report(source, entries)
        self.assertGreaterEqual(report["metrics"]["ordered_trigram_recall"], 0.99)
        self.assertLessEqual(report["metrics"]["output_source_ratio"], 1.01)
        finding = next(f for f in report["findings"]
                       if f["code"] == "source_unsupported_duplicate")
        self.assertEqual("silver knight", finding["details"]["phrase"])
        self.assertEqual(1, finding["details"]["source_occurrences"])
        self.assertEqual(2, finding["details"]["output_occurrences"])
        self.assertEqual([2], finding["entry_numbers"])

    def test_faithful_source_repeat_with_same_seam_shape_is_accepted(self):
        # The source itself repeats the term at the seam; the output carries
        # exactly that many copies, so the triggering shape is not a defect.
        source = (self.FILLER
                  + "At the gate stood the [Silver Knight]. "
                    "[Silver Knight] waited there quietly.")
        entries = [
            _seg(self.FILLER.strip()),
            _seg("At the gate stood the [Silver Knight]."),
            _seg("Silver Knight waited there quietly."),
        ]
        report = self._report(source, entries)
        self.assertTrue(report["passed"], report["findings"])

    def test_faithful_source_repeat_inside_one_entry_is_accepted(self):
        source = (self.FILLER
                  + "At the gate stood the [Silver Knight] [Silver Knight]. "
                    "He waited there quietly.")
        entries = [
            _seg(self.FILLER.strip()),
            _seg("At the gate stood the [Silver Knight] [Silver Knight]. "
                 "He waited there quietly."),
        ]
        report = self._report(source, entries)
        self.assertTrue(report["passed"], report["findings"])

    def test_ordinary_dialogue_is_not_treated_as_a_lexical_copy(self):
        source = 'He said "Go now." Then he said "Come back." She left.'
        entries = [
            _seg("He said", "NARRATOR"),
            _seg("Go now.", "SPOKEN"),
            _seg("Then he said", "NARRATOR"),
            _seg("Come back.", "SPOKEN"),
            _seg("She left.", "NARRATOR"),
        ]
        report = self._report(source, entries)
        self.assertTrue(report["passed"], report["findings"])

    def test_lexical_quote_region_from_analysis_is_a_candidate(self):
        source = (self.FILLER
                  + "They called him the \u201cSilver Knight\u201d. He waited there quietly.")
        analysis = {"regions": [
            {"type": "NARRATOR", "text": "the \u201cSilver Knight\u201d",
             "quote_role": "LEXICAL_QUOTE"},
        ]}
        entries = [
            _seg(self.FILLER.strip()),
            _seg("They called him the \u201cSilver Knight\u201d."),
            _seg("Silver Knight. He waited there quietly."),
        ]
        report = validate_segment_quality(source, entries, quote_analysis=analysis,
                                          **self.GATE)
        self.assertFalse(report["passed"])
        finding = next(f for f in report["findings"]
                       if f["code"] == "source_unsupported_duplicate")
        self.assertEqual("silver knight", finding["details"]["phrase"])

    def _mixed_source(self):
        return (self.FILLER + "At the gate stood the [Silver Knight]. "
                "He waited there quietly. A child whispered, \"Silver Knight.\"")

    def _mixed_analysis(self, source):
        return classify_lexical_quote_regions(source, analyze_outer_quote_regions(source))

    def _validate_with_real_analysis(self, source, entries, analysis, restrictions=True):
        from generate_script import LLMGenParams
        from three_pass_generate import validate_segment_for_params
        params = LLMGenParams(segmentation="lexical", max_tokens=500,
                              quoted_must_be_spoken=restrictions,
                              unquoted_must_be_narrator=restrictions)
        return validate_segment_for_params(source, entries, params, analysis)

    def test_mixed_source_extra_narrator_seam_copy_is_rejected(self):
        source = self._mixed_source()
        entries = [
            _seg(self.FILLER.strip()),
            _seg("At the gate stood the [Silver Knight]."),
            _seg("Silver Knight. He waited there quietly. A child whispered,"),
            _seg("Silver Knight.", "SPOKEN"),
        ]
        report = self._validate_with_real_analysis(source, entries, self._mixed_analysis(source))
        finding = next(f for f in report["findings"]
                       if f["code"] == "source_unsupported_duplicate")
        # The spoken source occurrence must not supply a second narrator
        # allowance: one narration copy in source, two emitted.
        self.assertEqual(1, finding["details"]["source_occurrences"])
        self.assertEqual(2, finding["details"]["output_occurrences"])
        self.assertEqual({2, 3}, set(finding["entry_numbers"]))

    def test_mixed_source_faithful_narration_and_dialogue_is_accepted(self):
        source = self._mixed_source()
        entries = [
            _seg(self.FILLER.strip()),
            _seg("At the gate stood the [Silver Knight]. He waited there quietly. "
                 "A child whispered,"),
            _seg("Silver Knight.", "SPOKEN"),
        ]
        report = self._validate_with_real_analysis(source, entries, self._mixed_analysis(source))
        self.assertTrue(report["passed"], report["findings"])

    def test_mixed_source_within_entry_extra_copy_is_rejected(self):
        source = self._mixed_source()
        entries = [
            _seg(self.FILLER.strip()),
            _seg("At the gate stood the [Silver Knight] [Silver Knight]. "
                 "He waited there quietly. A child whispered,"),
            _seg("Silver Knight.", "SPOKEN"),
        ]
        report = self._validate_with_real_analysis(source, entries, self._mixed_analysis(source),
                                                   restrictions=False)
        finding = next(f for f in report["findings"]
                       if f["code"] == "source_unsupported_duplicate")
        # Permissive policy: compatible whole-source / whole-output counts.
        self.assertEqual(2, finding["details"]["source_occurrences"])
        self.assertEqual(3, finding["details"]["output_occurrences"])
        self.assertIn(2, finding["entry_numbers"])

    def test_repeated_spoken_line_cannot_create_a_narration_duplicate(self):
        source = (self.FILLER + "At the gate stood the [Silver Knight]. "
                  "A child whispered, \"Silver Knight.\" Another whispered, \"Silver Knight.\"")
        entries = [
            _seg(self.FILLER.strip()),
            _seg("At the gate stood the [Silver Knight]."),
            _seg("Silver Knight.", "SPOKEN"),
            _seg("Silver Knight.", "SPOKEN"),
        ]
        report = self._validate_with_real_analysis(source, entries, self._mixed_analysis(source))
        self.assertTrue(report["passed"], report["findings"])
        self.assertFalse([f for f in report["findings"]
                          if f["code"] == "source_unsupported_duplicate"])

    def _permissive_source_and_suffix(self):
        suffix = ('At the gate stood the [Silver Knight]. A child whispered, '
                  '"Silver Knight." Then she left.')
        return self.FILLER + suffix, suffix

    def test_permissive_policy_faithful_embedded_dialogue_is_accepted(self):
        source, suffix = self._permissive_source_and_suffix()
        entries = [_seg(self.FILLER.strip()), _seg(suffix)]
        report = self._validate_with_real_analysis(source, entries,
                                                    self._mixed_analysis(source),
                                                    restrictions=False)
        self.assertTrue(report["passed"], report["findings"])

    def test_permissive_policy_extra_copy_is_rejected_with_whole_counts(self):
        source, _suffix = self._permissive_source_and_suffix()
        entries = [
            _seg(self.FILLER.strip()),
            _seg('At the gate stood the [Silver Knight] [Silver Knight]. '
                 'A child whispered, "Silver Knight." Then she left.'),
        ]
        report = self._validate_with_real_analysis(source, entries,
                                                    self._mixed_analysis(source),
                                                    restrictions=False)
        finding = next(f for f in report["findings"]
                       if f["code"] == "source_unsupported_duplicate")
        self.assertEqual(2, finding["details"]["source_occurrences"])
        self.assertEqual(3, finding["details"]["output_occurrences"])

    def test_quoted_must_be_spoken_false_alone_accepts_faithful_dialogue(self):
        source, suffix = self._permissive_source_and_suffix()
        from generate_script import LLMGenParams
        from three_pass_generate import validate_segment_for_params
        params = LLMGenParams(segmentation="lexical", max_tokens=500,
                              quoted_must_be_spoken=False,
                              unquoted_must_be_narrator=True)
        report = validate_segment_for_params(
            source, [_seg(self.FILLER.strip()), _seg(suffix)], params,
            self._mixed_analysis(source))
        self.assertTrue(report["passed"], report["findings"])

    def test_legitimately_repeated_source_quote_is_accepted(self):
        source = " ".join(["The [Silver Knight] waited."] * 4)
        entries = [_seg("The [Silver Knight] waited. " * 4)]
        report = self._report(source, entries)
        self.assertTrue(report["passed"], report["findings"])

    def test_short_narration_repeated_in_source_is_not_flagged(self):
        # The same short phrase on both sides of a seam is faithful when the
        # source carries it that many times: no excess copy to reject.
        source = "The bell rang. The bell rang again."
        entries = [_seg("The bell rang."), _seg("The bell rang again.")]
        report = self._report(source, entries)
        self.assertTrue(report["passed"], report["findings"])


class AccentedCaseParityTests(unittest.TestCase):
    """Both gates must answer "which non-ASCII letters did this introduce?"
    from one shared rule; #2291 was pass 1 rejecting a case-only change on an
    accented Latin letter that the single-pass gate has allowed since #938."""

    # (source, output, introduced letters both gates must report)
    CASES = (
        ("We met at the café near the river. émile waved.",
         "We met at the CAFÉ near the river. Émile waved.", []),
        ("Café is open.", "CAFÉ IS OPEN.", []),
        ("CAFÉ IS OPEN.", "Café is open.", []),
        ("Cafe\u0301 is open.", "CAFÉ IS OPEN.", []),
        ("Café is open.", "CAFE\u0301 IS OPEN.", []),
        ("café", "cafe\u0300", ["è"]),
        ("café", "cafè", ["è"]),
        ("Café is open.", "CAFË IS OPEN.", ["Ë"]),
        ("cafe", "café", ["é"]),
        ("This is my resume.", "This is my résumé.", ["é"]),
        ("café", "CAFÊ", ["Ê"]),
        ("The straße", "The STRAẞE", []),
        ("The STRAẞE", "The straße", []),
        ("Die Straße ist lang.", "DIE STRASSE IST LANG.", []),
        ("ﬁne", "ﬁne", []),
        ("fine", "ﬁne", ["ﬁ"]),
        ("Džungla", "DŽUNGLA", []),
        ("DŽUNGLA", "Džungla", []),
        ("İstanbul", "istanbul", []),
        ("istanbul", "İstanbul", ["İ"]),
        ("ı", "I", []),
        ("I", "ı", ["ı"]),
        ("a", "ᴀ", ["ᴀ"]),
        ("a", "Ａ", ["Ａ"]),
        ("ａ", "Ａ", []),
        ("ΣΟΦΟΣ", "σοφος", ["ο", "ς", "σ", "φ"]),
        ("ΑΒΓ", "αβγ", ["α", "β", "γ"]),
        ("σοφος", "ΣΟΦΟΣ", ["Ο", "Σ", "Φ"]),
        ("日本", "日本語", ["語"]),
        ("михаил", "Михаил", []),
        ("михаил", "Михаъил", []),
        ("s", "ſ", ["ſ"]),
        ("ſ", "ſ", []),
        ("n", "ŉ", ["ŉ"]),
        ("ↄ", "Ↄ", ["Ↄ"]),
        ("Ↄ", "ↄ", ["ↄ"]),
        ("abc def", "abc deg", []),
        ("a b", "a — b", []),
    )

    def _introduced(self, report):
        return [char["character"] for finding in report["findings"]
                if finding["code"] == "unsupported_unicode_character"
                for char in finding["characters"]]

    def _reports(self, source, output):
        chunk = validate_chunk_quality(
            source, [{"speaker": "NARRATOR", "text": output, "instruct": "Read."}])
        segment = validate_segment_quality(
            source, [{"type": "NARRATOR", "text": output}])
        return chunk, segment

    def test_accented_case_change_passes_the_segment_gate(self):
        source = "We met at the café near the river. émile waved."
        entries = [{"type": "NARRATOR", "text": "We met at the CAFÉ near the river. Émile waved."}]
        report = validate_segment_quality(source, entries)
        self.assertTrue(report["passed"], report["findings"])
        self.assertEqual(report["metrics"]["source_token_recall"], 1.0)

    def test_introduced_accented_letter_still_fails(self):
        source = ("A plain Latin source sentence with quite a few ordinary "
                  "words in it.")
        entries = [{"type": "NARRATOR", "text": source + " É"}]
        report = validate_segment_quality(source, entries)
        self.assertFalse(report["passed"])
        introduced = [char["character"] for finding in report["findings"]
                      if finding["code"] == "unsupported_unicode_character"
                      for char in finding["characters"]]
        self.assertEqual(["É"], introduced)

    def test_both_gates_report_the_same_introduced_letters(self):
        for source, output, expected in self.CASES:
            with self.subTest(source=source, output=output):
                chunk, segment = self._reports(source, output)
                self.assertEqual(expected, self._introduced(chunk))
                self.assertEqual(expected, self._introduced(segment))


class ValidationExpectedValueTests(unittest.TestCase):
    def test_falsey_expected_values_remain_in_diagnostic_context(self):
        import copy
        findings = [{"message": "Invalid value.", "entry_number": 1,
                     "source_line": "Original line.", "value": 2, "expected": value}
                    for value in (0, False, "", [], {})]
        original = copy.deepcopy(findings)
        for finding in findings:
            with self.subTest(expected=finding["expected"]):
                text = format_validation_findings([finding])
                self.assertIn("expected=" + str(finding["expected"]), text)
                self.assertIn("entry 1", text)
                self.assertIn('source line="Original line."', text)
                self.assertIn("rejected=2", text)
        self.assertEqual(original, findings)
        self.assertNotIn("expected=", format_validation_findings([
            {"message": "No constraint."}, {"message": "No constraint.", "expected": None}]))


class ConcurrentQuoteTelemetryTests(unittest.TestCase):
    def test_overlapping_validations_publish_their_own_quote_gate_status(self):
        import json, tempfile, threading
        from concurrent.futures import ThreadPoolExecutor
        from pathlib import Path
        from unittest.mock import patch
        import pass_quality
        plain = 'The room was quiet and the door remained closed.'
        quoted = '“Come here.” She stopped.'
        dashed = '\n'.join('— ' + text for text in ('Come here.', 'Wait there.', 'Stay calm.', 'Look outside.', 'Go ahead.'))
        for other_source, other_entries, expected in (
                (quoted, [_seg('Come here.', 'SPOKEN'), _seg('She stopped.')], 'ran'),
                (dashed, [_seg(dashed)], 'skipped:dash_lines')):
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as tmp:
                paused, release = threading.Event(), threading.Event()
                original = pass_quality._introduced_character_findings
                def hold_first(source, *args, **kwargs):
                    if source == plain:
                        paused.set()
                        if not release.wait(5):
                            raise RuntimeError('first validation was not released')
                    return original(source, *args, **kwargs)
                with patch.object(pass_quality, '_introduced_character_findings', side_effect=hold_first), ThreadPoolExecutor(max_workers=2) as pool:
                    first = pool.submit(validate_segment_quality, plain, [_seg(plain)])
                    try:
                        self.assertTrue(paused.wait(5))
                        second = pool.submit(validate_segment_quality, other_source, other_entries)
                        other = second.result(timeout=5)
                    finally:
                        release.set()
                    initial = first.result(timeout=5)
                for name, report in (('first', initial), ('second', other)):
                    Path(tmp, name + '.json').write_text(json.dumps(report))
                saved_first = json.loads(Path(tmp, 'first.json').read_text())
                saved_second = json.loads(Path(tmp, 'second.json').read_text())
                self.assertEqual('skipped:none_detected', saved_first['quote_gate'])
                self.assertEqual(expected, saved_second['quote_gate'])
                self.assertTrue(saved_first['passed'], saved_first['findings'])
                self.assertTrue(saved_second['passed'], saved_second['findings'])


class DashDialogueTagSegmenterTests(unittest.TestCase):
    """#2329: pass_quality and quote_regions_decision share dialogue_spans' rule."""

    def test_issue_rows_segment_speech_and_tag_and_agree_with_dialogue_spans(self):
        from dialogue_spans import spoken_spans
        from three_pass_generate import quote_regions_decision

        cases = [
            ("—Hola —dijo Ana—. Vamos.",
             [("SPOKEN", "Hola"), ("NARRATOR", "dijo Ana"), ("SPOKEN", "Vamos.")]),
            ("— Oui, répondit-il.",
             [("SPOKEN", "Oui,"), ("NARRATOR", "répondit-il.")]),
            ("— Yes, he replied.",
             [("SPOKEN", "Yes,"), ("NARRATOR", "he replied.")]),
        ]
        for source, expected in cases:
            with self.subTest(source=source):
                analysis = analyze_outer_quote_regions(source)
                self.assertNotIn("unverified_dash", analysis)
                self.assertEqual(
                    expected,
                    [(row["type"], row["text"]) for row in analysis["regions"]],
                )
                self.assertEqual(
                    [text for kind, text in expected if kind == "SPOKEN"],
                    [source[a:b] for a, b in spoken_spans(source, "dash_lines")],
                )
                regions, resolution = quote_regions_decision("auto", source, analysis)
                self.assertEqual("quote_presegmented", resolution)
                self.assertEqual(
                    expected,
                    [(row["type"], row["text"]) for row in regions],
                )

    def test_no_evidence_interior_break_routes_to_model_and_allows_model_split(self):
        from three_pass_generate import quote_regions_decision

        cases = [
            ("She sighed.\n— The train — late again — finally came.",
             "The train — late again — finally came."),
            ("He paused.\n— Wait — I forgot the keys.",
             "Wait — I forgot the keys."),
            ("She smiled.\n— Wait, she said nothing.",
             "Wait, she said nothing."),
            ("He paused.\n— Wait, it won't fit.",
             "Wait, it won't fit."),
        ]
        for chunk, whole_line in cases:
            with self.subTest(chunk=chunk):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertTrue(analysis.get("unverified_dash"))
                self.assertEqual(
                    whole_line,
                    [row["text"] for row in analysis["regions"] if row["type"] == "SPOKEN"][0],
                )
                self.assertEqual((None, None), quote_regions_decision("auto", chunk, analysis))
                self.assertEqual((None, None), quote_regions_decision("lexical", chunk, analysis))
                forced_regions, forced_res = quote_regions_decision("quotes", chunk, analysis)
                self.assertEqual("quote_forced", forced_res)
                self.assertEqual(analysis["regions"], forced_regions)

        # When routed to the model, validate_segment_quality must not reject a
        # valid model split using the unverified whole-line dash region.
        chunk = "She smiled.\n— The train — late again — finally came."
        analysis = analyze_outer_quote_regions(chunk)
        model_entries = [
            {"type": "NARRATOR", "text": "She smiled."},
            {"type": "SPOKEN", "text": "The train"},
            {"type": "NARRATOR", "text": "late again finally came."},
        ]
        report = validate_segment_quality(chunk, model_entries, quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])

    def test_unverified_dash_gate_checks_the_opening_speech(self):
        chunk = "She smiled.\n— Wait — I forgot the keys."
        analysis = analyze_outer_quote_regions(chunk)
        self.assertTrue(analysis.get("unverified_dash"))
        wrong = [{"type": "NARRATOR", "text": "She smiled. Wait I forgot the keys."}]
        report = validate_segment_quality(chunk, wrong, quote_analysis=analysis)
        self.assertFalse(report["passed"])
        self.assertEqual("ran", report["quote_gate"])
        correct = [
            {"type": "NARRATOR", "text": "She smiled."},
            {"type": "SPOKEN", "text": "Wait"},
            {"type": "NARRATOR", "text": "I forgot the keys."},
        ]
        report = validate_segment_quality(chunk, correct, quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])

    def test_clean_dash_interruption_narration_and_paired_quotes_keep_existing_behaviour(self):
        from three_pass_generate import quote_regions_decision

        cases = [
            ("— Primera línea.\n\n— Segunda línea.",
             [("SPOKEN", "Primera línea."), ("SPOKEN", "Segunda línea.")]),
            ("He paused.\n— I was going to—",
             [("NARRATOR", "He paused."), ("SPOKEN", "I was going to—")]),
            ("The train — late again — finally came.\n— All aboard!",
             [("NARRATOR", "The train — late again — finally came."), ("SPOKEN", "All aboard!")]),
            ('She sighed. "The train — late again — finally came." "Yes, he replied."',
             [("NARRATOR", "She sighed."),
              ("SPOKEN", "The train — late again — finally came."),
              ("SPOKEN", "Yes, he replied.")]),
        ]
        for chunk, expected in cases:
            with self.subTest(chunk=chunk):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertNotIn("unverified_dash", analysis)
                regions, resolution = quote_regions_decision("auto", chunk, analysis)
                self.assertEqual("quote_presegmented", resolution)
                self.assertEqual(
                    expected,
                    [(row["type"], row["text"]) for row in regions],
                )

    def test_opening_speech_may_span_consecutive_spoken_entries(self):
        """The review's finding 2: a model may split the prefix itself."""
        chunk = "She smiled.\n— It is late. The road is long — I forgot the keys."
        analysis = analyze_outer_quote_regions(chunk)
        self.assertTrue(analysis.get("unverified_dash"))
        self.assertIn("It is late. The road is long",
                      analysis.get("unverified_dash_prefixes"))
        entries = [
            {"type": "NARRATOR", "text": "She smiled."},
            {"type": "SPOKEN", "text": "It is late."},
            {"type": "SPOKEN", "text": "The road is long"},
            {"type": "NARRATOR", "text": "I forgot the keys."},
        ]
        report = validate_segment_quality(chunk, entries, quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])
        self.assertEqual("ran", report["quote_gate"])

    def test_one_ambiguous_line_does_not_disable_validation_of_confident_lines(self):
        """The downgrade is per line: a confident line keeps its gate.

        (#2390 review 3 finding 3.)
        """
        confident = "— Oui, répondit-il."
        ambiguous = "— Wait — I forgot the keys."
        chunk = confident + "\n\n" + ambiguous
        analysis = analyze_outer_quote_regions(chunk)
        self.assertTrue(analysis.get("unverified_dash"))
        merged = [
            {"type": "SPOKEN", "text": "Oui, répondit-il."},
            {"type": "SPOKEN", "text": "Wait"},
            {"type": "NARRATOR", "text": "I forgot the keys."},
        ]
        report = validate_segment_quality(chunk, merged, quote_analysis=analysis)
        self.assertFalse(report["passed"])
        self.assertIn("crosses_quote_boundary",
                      [finding["code"] for finding in report["findings"]])
        alone = validate_segment_quality(
            confident, merged[:1],
            quote_analysis=analyze_outer_quote_regions(confident))
        self.assertFalse(alone["passed"])
        # The ambiguous line's unverified tail, and only that tail, is left to
        # the model; the opening speech is still checked.
        correct = [
            {"type": "SPOKEN", "text": "Oui,"},
            {"type": "NARRATOR", "text": "répondit-il."},
            {"type": "SPOKEN", "text": "Wait I forgot the keys."},
        ]
        report = validate_segment_quality(chunk, correct, quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])
        wrong_prefix = [
            {"type": "SPOKEN", "text": "Oui,"},
            {"type": "NARRATOR", "text": "répondit-il. Wait"},
            {"type": "NARRATOR", "text": "I forgot the keys."},
        ]
        report = validate_segment_quality(
            chunk, wrong_prefix, quote_analysis=analysis)
        self.assertFalse(report["passed"])
        self.assertIn("quote_region_misclassified",
                      [finding["code"] for finding in report["findings"]])

    def test_a_merged_confident_and_ambiguous_entry_is_still_validated(self):
        """Only an entry INSIDE an unverified tail is left to the model.

        (#2390 review 4 finding 1.) A model that merges a confident line and
        the ambiguous line into one SPOKEN entry contains vouched text, so the
        per-line skip must not apply and the confident tag is caught.
        """
        confident = "— Oui, répondit-il."
        ambiguous = "— Wait — I forgot the keys."
        chunk = confident + "\n\n" + ambiguous
        analysis = analyze_outer_quote_regions(chunk)
        self.assertTrue(analysis.get("unverified_dash"))
        merged = [{"type": "SPOKEN",
                   "text": "Oui, répondit-il. Wait — I forgot the keys."}]
        report = validate_segment_quality(chunk, merged, quote_analysis=analysis)
        self.assertFalse(report["passed"])
        self.assertIn("crosses_quote_boundary",
                      [finding["code"] for finding in report["findings"]])
        # An entry that lies inside the unverified tail (a model sentence
        # split of the ambiguous line) is still left to the model.
        inside = [
            {"type": "SPOKEN", "text": "Oui,"},
            {"type": "NARRATOR", "text": "répondit-il."},
            {"type": "SPOKEN", "text": "Wait"},
            {"type": "NARRATOR", "text": "I forgot the keys."},
        ]
        report = validate_segment_quality(chunk, inside, quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])
        # The ambiguous line kept whole as one entry is still accepted: it is
        # the model's own line, which the segmenter routes rather than labels.
        whole = [
            {"type": "SPOKEN", "text": "Oui,"},
            {"type": "NARRATOR", "text": "répondit-il."},
            {"type": "SPOKEN", "text": "Wait — I forgot the keys."},
        ]
        report = validate_segment_quality(chunk, whole, quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])

    def test_opening_speech_across_a_narrator_entry_still_rejects(self):
        chunk = "She smiled.\n— It is late. The road is long — I forgot the keys."
        analysis = analyze_outer_quote_regions(chunk)
        entries = [
            {"type": "NARRATOR", "text": "She smiled."},
            {"type": "SPOKEN", "text": "It is late."},
            {"type": "NARRATOR", "text": "The road"},
            {"type": "SPOKEN", "text": "is long"},
            {"type": "NARRATOR", "text": "I forgot the keys."},
        ]
        report = validate_segment_quality(chunk, entries, quote_analysis=analysis)
        self.assertFalse(report["passed"])
        self.assertEqual("quote_region_misclassified",
                         report["findings"][0]["code"])

    def test_leading_indented_dash_line_is_dialogue(self):
        analysis = analyze_outer_quote_regions("He paused.\n  —Hola —dijo Ana—. Vamos.")
        self.assertEqual(
            [("NARRATOR", "He paused."), ("SPOKEN", "Hola"),
             ("NARRATOR", "dijo Ana"), ("SPOKEN", "Vamos.")],
            [(row["type"], row["text"]) for row in analysis["regions"]],
        )

    def test_line_dash_inside_an_unclosed_guillemet_is_not_split(self):
        analysis = analyze_outer_quote_regions("«Et il dit:\n— Oui, répondit-il.")
        self.assertEqual(
            [("SPOKEN", "Et il dit:\n— Oui, répondit-il.")],
            [(row["type"], row["text"]) for row in analysis["regions"]],
        )

    def test_line_dash_inside_an_open_quote_chunk_is_not_split(self):
        """`not depth` keeps a chunk that starts inside a quote conservative."""
        analysis = analyze_outer_quote_regions(
            "— Oui, répondit-il.", initial_depth=1, allow_open_end=True)
        self.assertEqual(
            [("SPOKEN", "— Oui, répondit-il.")],
            [(row["type"], row["text"]) for row in analysis["regions"]],
        )

    def test_single_ascii_dash_line_in_quote_free_chunk_abstains_like_main(self):
        """#2395 review 1 blocker 2: one `--` line is weaker evidence than `—`."""
        chunk = ("He walked into the room and looked around.\n"
                 "-- The door was already open.\n"
                 "He sat down and waited for someone to come.")
        analysis = analyze_outer_quote_regions(chunk)
        self.assertEqual([], analysis["regions"])
        self.assertNotIn("unverified_dash", analysis)
        from three_pass_generate import quote_regions_decision
        self.assertEqual((None, None),
                         quote_regions_decision("auto", chunk, analysis))
        # The same chunk with two `--` lines establishes the convention and
        # certifies, exactly as `—` does.
        established = ("He walked into the room and looked around.\n"
                       "-- The door was already open.\n"
                       "-- Nobody was inside.\n"
                       "He sat down and waited for someone to come.")
        analysis = analyze_outer_quote_regions(established)
        self.assertEqual(
            [("NARRATOR", "He walked into the room and looked around."),
             ("SPOKEN", "The door was already open."),
             ("SPOKEN", "Nobody was inside."),
             ("NARRATOR", "He sat down and waited for someone to come.")],
            [(row["type"], row["text"]) for row in analysis["regions"]],
        )
        self.assertEqual(
            "quote_presegmented",
            quote_regions_decision("auto", established, analysis)[1],
        )
        # A single em dash still certifies: `—` is strong evidence on its own.
        em_dash = ("He walked into the room and looked around.\n"
                   "— The door was already open.\n"
                   "He sat down and waited for someone to come.")
        self.assertEqual(
            "quote_presegmented",
            quote_regions_decision("auto", em_dash,
                                   analyze_outer_quote_regions(em_dash))[1],
        )

    def test_name_only_line_in_established_dash_chunk_is_spoken(self):
        """#2395 review 2 low 1: the attribution exclusion needs an
        UNESTABLISHED dash convention, so a name-only vocative in a dash chunk
        keeps main's SPOKEN label.
        """
        from three_pass_generate import quote_regions_decision

        for vocative in ("—Mamá", "— Señor García", "—Maman", "—Мама",
                         "—Ana", "—YES", "—Yes, Sir", "—_Yes_"):
            with self.subTest(vocative=vocative):
                chunk = f"— Buenos días, señor.\n{vocative}\n— Buenos días.\n"
                analysis = analyze_outer_quote_regions(chunk)
                self.assertNotIn("unverified_dash", analysis)
                self.assertEqual(
                    [("SPOKEN", "Buenos días, señor."),
                     ("SPOKEN", vocative.lstrip("— ")),
                     ("SPOKEN", "Buenos días.")],
                    [(row["type"], row["text"]) for row in analysis["regions"]],
                )
                self.assertEqual(
                    "quote_presegmented",
                    quote_regions_decision("auto", chunk, analysis)[1],
                )

    def test_ascii_attribution_lines_do_not_certify_their_own_chunk(self):
        """#2447 gate 1 blocker 2: `--` signatures/ToC/epigraph attributions and
        hard-wrapped lower-case continuations cannot certify a convention.

        Main abstains on every chunk below (and `—` is unchanged, main already
        certifying signature pairs); before the fix two `--` attribution lines
        established the convention and were certified SPOKEN.
        """
        from three_pass_generate import quote_regions_decision

        for chunk in (
            ("Vanity of vanities, all is vanity.\n--Ecclesiastes\n"
             "The paths of glory lead but to the grave.\n--Thomas Gray\n"
             "The story begins.\n"),
            "Yours very truly,\n--John Smith\n--Mary Smith\n",
            "CONTENTS\n--Chapter One\n--Chapter Two\n--Chapter Three\n",
            ("--Vanity of vanities, saith the Preacher.\n--_Ecclesiastes_\n"
             "The story begins.\n"),
            ("and I thought of her\n--and wept.\nThe rain fell\n"
             "--the road was gone.\n"),
        ):
            with self.subTest(chunk=chunk):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertEqual([], analysis["regions"])
                self.assertEqual((None, None),
                                 quote_regions_decision("auto", chunk, analysis))
        # A real two-line `--` dialogue chunk still certifies.
        real = "He walked in.\n-- Hello there.\n-- Hi.\nHe sat down.\n"
        analysis = analyze_outer_quote_regions(real)
        self.assertEqual(
            "quote_presegmented",
            quote_regions_decision("auto", real, analysis)[1])

    def test_ascii_double_hyphen_dash_lines_segment_and_route_via_shared_rule(self):
        """#2395: pass_quality shares dialogue_spans' ASCII `--` dash rule."""
        from three_pass_generate import quote_regions_decision

        # One weak `--` line is not enough evidence to certify a quote-free
        # chunk (#2395 review 1 blocker 2); main abstains on the same chunk.
        for chunk in (
            "-- Hola --dijo Ana--. Vamos.",
            "-- Oui, répondit-il.",
            "-- Non, dit-elle. -- Jamais.",
            "He paused.\n  --Hola --dijo Ana--. Vamos.",
            "The train -- late again -- finally came.\n-- All aboard!",
            "He paused--then walked away.\n-- Hello.",
            "He paused.\n---\n-- Hello.",
            "He paused.\n- Bullet item\n-- Hello.",
        ):
            with self.subTest(single_weak_line=chunk):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertEqual([], analysis["regions"])
                self.assertNotIn("unverified_dash", analysis)
                self.assertEqual((None, None),
                                 quote_regions_decision("auto", chunk, analysis))

        # Two `--`-opened lines establish the convention in the chunk, so the
        # shared rule certifies them exactly as it does for `—`.
        for chunk, expected in (
            ("-- Hola --dijo Ana--. Vamos.\n-- Bien, dijo Luis.",
             [("SPOKEN", "Hola"), ("NARRATOR", "dijo Ana"),
              ("SPOKEN", "Vamos."), ("SPOKEN", "Bien,"),
              ("NARRATOR", "dijo Luis.")]),
            ("-- Oui, répondit-il.\n-- Bien sûr.",
             [("SPOKEN", "Oui,"), ("NARRATOR", "répondit-il."),
              ("SPOKEN", "Bien sûr.")]),
            ("-- Non, dit-elle. -- Jamais.\n-- Tant pis.",
             [("SPOKEN", "Non,"), ("NARRATOR", "dit-elle."),
              ("SPOKEN", "Jamais."), ("SPOKEN", "Tant pis.")]),
            ("He paused.\n  --Hola --dijo Ana--. Vamos.\n  --Adiós.",
             [("NARRATOR", "He paused."), ("SPOKEN", "Hola"),
              ("NARRATOR", "dijo Ana"), ("SPOKEN", "Vamos."),
              ("SPOKEN", "Adiós.")]),
            ("He paused--then walked away.\n-- Hello.\n-- Goodbye.",
             [("NARRATOR", "He paused--then walked away."),
              ("SPOKEN", "Hello."), ("SPOKEN", "Goodbye.")]),
            ("He paused.\n---\n-- Hello.\n-- Goodbye.",
             [("NARRATOR", "He paused.\n---"), ("SPOKEN", "Hello."),
              ("SPOKEN", "Goodbye.")]),
            ("He paused.\n- Bullet item\n-- Hello.\n-- Goodbye.",
             [("NARRATOR", "He paused.\n- Bullet item"),
              ("SPOKEN", "Hello."), ("SPOKEN", "Goodbye.")]),
        ):
            with self.subTest(certified=chunk):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertNotIn("unverified_dash", analysis)
                self.assertEqual(
                    expected,
                    [(row["type"], row["text"]) for row in analysis["regions"]],
                )
                regions, resolution = quote_regions_decision("auto", chunk, analysis)
                self.assertEqual("quote_presegmented", resolution)
                self.assertEqual(
                    expected,
                    [(row["type"], row["text"]) for row in regions],
                )
        # Unverified interior `--` routes to the model and checks opening speech.
        ambiguous_chunk = ("He paused.\n-- Wait -- I forgot the keys.\n"
                           "-- Fine.")
        analysis = analyze_outer_quote_regions(ambiguous_chunk)
        self.assertTrue(analysis.get("unverified_dash"))
        self.assertEqual((None, None), quote_regions_decision("auto", ambiguous_chunk, analysis))
        wrong = [{"type": "NARRATOR", "text": "He paused. Wait I forgot the keys. Fine."}]
        self.assertFalse(validate_segment_quality(
            ambiguous_chunk, wrong, quote_analysis=analysis)["passed"])
        correct = [
            {"type": "NARRATOR", "text": "He paused."},
            {"type": "SPOKEN", "text": "Wait"},
            {"type": "NARRATOR", "text": "I forgot the keys."},
            {"type": "SPOKEN", "text": "Fine."},
        ]
        report = validate_segment_quality(ambiguous_chunk, correct, quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])
        # Narration-only chunks with prose `--`, `---`, or `- ` do not become dash dialogue.
        for plain in (
            "The train -- late again -- finally came.",
            "He paused--then walked away.",
            "---\nChapter Two\n---",
            "- First item\n- Second item",
        ):
            with self.subTest(plain=plain):
                self.assertEqual([], analyze_outer_quote_regions(plain)["regions"])


if __name__ == "__main__":
    unittest.main()
