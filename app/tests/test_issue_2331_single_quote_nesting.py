"""#2331: speech in another quote convention nested inside single-quoted speech.

In a book whose dialogue uses single curly quotes, a chunk that contains a
nested double-quoted phrase was segmented inside out: the nested phrase became
the only speech and the real dialogue became narration. The quote decision
accepted that inverted split and the segment gate rejected the correct one with
mixed_quote_region.

A full single-quote pairing model is out of scope. This fix abstains: when a
double-quoted phrase sits inside matched single-quoted speech, the double quotes
are inner speech, not the chunk's outer convention, so the model is asked. The
same analysis is the gate, so the correct split still has to pass.

The narrow decision is deliberate (rev1 review):
  - only a single-quoted span that nests a paired quote is adopted; a quoted
    title in narration stays narration;
  - nothing is adopted when the chunk already pairs double quotes outside every
    single span, so a double-quoted book keeps main's analysis byte for byte;
  - the gate allows a nested quote mark by POSITION inside an adopted span, not
    by character, so a row that keeps the real outer delimiters still fails.
"""
import hashlib
import json
import unittest

from pass_quality import (analyze_outer_quote_regions, validate_segment_quality)
try:  # Importable on the pre-fix tree, so the differential digest can be checked there.
    from pass_quality import get_single_quoted_speech_spans
except ImportError:  # pragma: no cover - only on origin/main before #2331
    get_single_quoted_speech_spans = None
import pass_quality as pq
from dialogue_spans import PAIRED
import three_pass_generate as tp


FIRST = "\u2018She told me \u201crun\u201d and I ran,\u2019 said Anna. Ben left."
FIRST_CORRECT = [
    {"type": "SPOKEN", "text": "She told me \u201crun\u201d and I ran,"},
    {"type": "NARRATOR", "text": "said Anna. Ben left."},
]
FIRST_INVERTED = [
    {"type": "NARRATOR", "text": "\u2018She told me "},
    {"type": "SPOKEN", "text": "run"},
    {"type": "NARRATOR", "text": " and I ran,\u2019 said Anna. Ben left."},
]

SECOND = ("\u2018She told me \u201crun\u201d and I ran, and then I hid by the river "
          "until the morning came,\u2019 said Anna.\n\n"
          "\u2018Were you afraid?\u2019 asked Ben.\n\n"
          "\u2018A little,\u2019 said Anna. She looked at the door.")
# Only one span nests a double quote, so only it is adopted; the later plain
# single-quoted speeches keep main's narration (Limits).
SECOND_NARROW = [
    {"type": "SPOKEN",
     "text": "She told me run and I ran, and then I hid by the river until the morning came,"},
    {"type": "NARRATOR",
     "text": ("said Anna.\n\n"
              "\u2018Were you afraid?\u2019 asked Ben.\n\n"
              "\u2018A little,\u2019 said Anna. She looked at the door.")},
]

STRAIGHT = "'She told me \"run\" and I ran,' said Anna. Ben left."
STRAIGHT_CORRECT = [
    {"type": "SPOKEN", "text": "She told me \"run\" and I ran,"},
    {"type": "NARRATOR", "text": "said Anna. Ben left."},
]
STRAIGHT_INVERTED = [
    {"type": "NARRATOR", "text": "'She told me "},
    {"type": "SPOKEN", "text": "run"},
    {"type": "NARRATOR", "text": " and I ran,' said Anna. Ben left."},
]

CONTROL = "\u2018She told me and I ran,\u2019 said Anna. Ben left."
CONTROL_CORRECT = [
    {"type": "SPOKEN", "text": "She told me and I ran,"},
    {"type": "NARRATOR", "text": "said Anna. Ben left."},
]

DOUBLE_BOOK = "\u201cShe said \u2018run\u2019 and left,\u201d said Anna. Ben left."

# rev1 finding 1: the single span sits inside double-quoted speech, so the
# decision never adopts it. The gate must not whitelist its marks either.
A_CHUNK = "\u201cHe asked \u2018did she say \u201cyes\u201d?\u2019 loudly,\u201d she said."
A_REGIONS = [
    {"type": "SPOKEN", "text": "He asked \u2018did she say yes?\u2019 loudly,"},
    {"type": "NARRATOR", "text": "she said."},
]
A_TRUTHFUL = [
    {"type": "SPOKEN",
     "text": "He asked \u2018did she say \u201cyes\u201d?\u2019 loudly,"},
    {"type": "NARRATOR", "text": "she said."},
]
A_KEEPS_OUTER = [
    {"type": "SPOKEN",
     "text": "\u201cHe asked \u2018did she say \u201cyes\u201d?\u2019 loudly,\u201d"},
    {"type": "NARRATOR", "text": "she said."},
]

# rev1 finding 2: a quoted title in narration is not speech.
TITLE = "\u2018He said \u201crun\u201d and left,\u2019 she said. Later \u2018Hamlet\u2019 was banned."
TITLE_REGIONS = [
    {"type": "SPOKEN", "text": "He said run and left,"},
    {"type": "NARRATOR", "text": "she said. Later \u2018Hamlet\u2019 was banned."},
]
TITLE_TRUTHFUL = [
    {"type": "SPOKEN", "text": "He said \u201crun\u201d and left,"},
    {"type": "NARRATOR", "text": "she said. Later \u2018Hamlet\u2019 was banned."},
]

# rev1 finding 3: a possessive or elision before the real closer.
POSSESSIVE_CURLY = "\u2018The dogs\u2019 \u201cbones\u201d are here,\u2019 she said."
POSSESSIVE_STRAIGHT = "'The dogs' \"bones\" are here,' she said."
ELISION = "\u2018We played rock \u2019n\u2019 roll and \u201crock\u201d music,\u2019 he said."
CLOSER_CASES = {
    "possessive curly": (POSSESSIVE_CURLY,
                         "The dogs\u2019 \u201cbones\u201d are here,",
                         "The dogs\u2019 bones are here,",
                         "she said."),
    "possessive straight": (POSSESSIVE_STRAIGHT,
                            "The dogs' \"bones\" are here,",
                            "The dogs' bones are here,",
                            "she said."),
    "elision": (ELISION,
                "We played rock \u2019n\u2019 roll and \u201crock\u201d music,",
                "We played rock \u2019n\u2019 roll and rock music,",
                "he said."),
}

# rev1 finding 4: the single span is a quotation in a double-quoted book's
# narration. The chunk already has depth-0 double speech, so nothing changes.
NARRATION_IN_DOUBLE_BOOK = ("He read \u2018the sign said \u201cclosed\u201d\u2019 twice. "
                            "\u201cLet me in,\u201d said Anna.")
NARRATION_IN_DOUBLE_BOOK_REGIONS = [
    {"type": "NARRATOR", "text": "He read \u2018the sign said"},
    {"type": "SPOKEN", "text": "closed"},
    {"type": "NARRATOR", "text": "\u2019 twice."},
    {"type": "SPOKEN", "text": "Let me in,"},
    {"type": "NARRATOR", "text": "said Anna."},
]

# rev1 finding 5: an inch mark inside an adopted span must stay content and
# must not swallow the attribution.
INCH = "\u2018He is 6\" wide and said \u201chi\u201d,\u2019 she said."
INCH_REGIONS = [
    {"type": "SPOKEN", "text": "He is 6\" wide and said hi,"},
    {"type": "NARRATOR", "text": "she said."},
]
INCH_TRUTHFUL = [
    {"type": "SPOKEN", "text": "He is 6\" wide and said \u201chi\u201d,"},
    {"type": "NARRATOR", "text": "she said."},
]

# rev1 finding 6: paragraph-bounded single quotes are a decided limitation.
BRITISH = ("\u2018She told me \u201crun\u201d and I ran,\n\n"
           "and I kept running,\u2019 said Anna. Ben left.")
BRITISH_REGIONS = [
    {"type": "NARRATOR", "text": "\u2018She told me"},
    {"type": "SPOKEN", "text": "run"},
    {"type": "NARRATOR",
     "text": "and I ran,\n\nand I kept running,\u2019 said Anna. Ben left."},
]
SPLIT_HEAD = "\u2018She told me \u201crun\u201d and I ran,"
SPLIT_HEAD_REGIONS = [
    {"type": "NARRATOR", "text": "\u2018She told me"},
    {"type": "SPOKEN", "text": "run"},
    {"type": "NARRATOR", "text": "and I ran,"},
]
SPLIT_TAIL = " and I kept running,\u2019 said Anna."
SPLIT_TAIL_REGIONS = []

# rev1 finding 7: conventions derived from PAIRED.
GUILLEMETS = "\u2018He said \u00abbonjour\u00bb twice,\u2019 she said."
CORNER_BRACKETS = "\u2018He said \u300c\u3053\u3093\u306b\u3061\u306f\u300d twice,\u2019 she said."

# Quote shapes the repository already exercises: #624 run-on quotes, #2314 and
# #2318 inch marks, German low-high, Polish, guillemets, Japanese, full-width,
# straight, multi-paragraph speech, and nested single quotes in a double-quoted
# book. Every one is a double-quoted chunk, so #2331 must not move any of them.
DOUBLE_QUOTED_FIXTURES = (
    "Alice stood. \"Hello.\" Alice waited. \"Goodbye.\" Alice left.",
    "\u201cHello,\u201d she said. \u201cGoodbye.\u201d",
    "\u201cI can't go,\u201d she said. \u201cI won't.\u201d",
    "\u201cFirst,\u201d she said.\n\n\u201cSecond,\u201d he said.",
    "\u201cShe said \u2018run\u2019 and left,\u201d said Anna. Ben left.",
    "\u201cShe said \u2018it is Anna\u2019s book\u2019 and left,\u201d said Ben.",
    "\u201eGuten Tag,\u201c sagte er. \u201eWie geht es?\u201c",
    "\u201eDzie\u0144 dobry,\u201d powiedzia\u0142.",
    "\u00abBonjour,\u00bb dit-elle. \u00ab\u00c7a va?\u00bb",
    "\u300c\u304a\u306f\u3088\u3046\u300d\u3068\u5f7c\u306f\u8a00\u3063\u305f\u3002\u300c\u5143\u6c17\uff1f\u300d",
    "\uff02Hello\uff02 she said. \uff02Goodbye.\uff02",
    "He is 6\" wide and said \u201cHello.\u201d",
    "He is 6\u201d wide and said \u201cHello.\u201d",
    "\u201cWait!\u201dHaha!\u201dHahaha!\u201d she cried.",
    "\u201cHello.\u201dHaha!\u201d she cried.",
    "She murmured I see\u2026\u201d, keeping her eyes lowered. \u201cThen she left.\u201d",
    "\u201c\u2018Nested single quotes\u2019 are decoration here,\u201d he said.",
)


def _decision_and_gate(chunk):
    """The user-visible results #2331 must not move for double-quoted chunks."""
    analysis = analyze_outer_quote_regions(chunk)
    decisions = []
    for mode in ("auto", "quotes", "lexical"):
        entries, resolution = tp.quote_regions_decision(mode, chunk, analysis)
        decisions.append({"mode": mode, "entries": entries, "resolution": resolution})
    report = validate_segment_quality(chunk, analysis["regions"],
                                      quote_analysis=analysis)
    decisions.append({"gate": {"passed": report["passed"],
                               "findings": report["findings"]}})
    # The gate recomputes the analysis when no quote_analysis is passed; that
    # path must agree with the analysis-backed one (#2331 review).
    analysis_free = validate_segment_quality(chunk, analysis["regions"])
    decisions.append({"gate_without_analysis": {
        "passed": analysis_free["passed"],
        "findings": analysis_free["findings"],
        "quote_gate": analysis_free["quote_gate"]}})
    return decisions


def _differential_corpus():
    """Chunks outside #2331's targeted shape; every result must match main.

    The targeted shape (a single-quoted span that nests a paired quote in a
    chunk with no other paired speech) is covered by the behaviour tests above,
    not here.
    """
    corpus = list(DOUBLE_QUOTED_FIXTURES)
    names = ("Anna", "Ben", "Cara", "Dan", "Eve", "Finn", "Gus", "Hana")
    verbs = ("said", "asked", "replied", "whispered")
    lines = ("Hello.", "Where did you go?", "I ran to the river.", "Don't wait.")
    replies = ("I stayed.", "A little.", "Not yet.", "Come back.")
    titles = ("Hamlet", "the sign", "the rule")
    possessives = ("bones", "books", "words")
    guillemet_pairs = ("\u00abbonjour\u00bb", "\u300c\u3053\u3093\u306b\u3061\u306f\u300d",
                       "\u300e\u304a\u306f\u3088\u3046\u300f")
    templates = (
        # double-quoted speech with nested single quotes (single-in-double)
        "\u201c{first} \u2018{title}\u2019 {reply}\u201d {verb} {name}.",
        "\u201c{first}\u201d {verb} {name}. The book \u2018{title}\u2019 was banned.",
        # possessives and inch marks inside double-quoted speech
        "\u201cThe dogs\u2019 {noun} are here,\u201d {verb} {name}.",
        "\u201cIt is {number}\" wide,\u201d {verb} {name}.",
        # guillemets and corner brackets inside double-quoted speech
        "\u201cHe said {pair} twice,\u201d {verb} {name}.",
        # run-on quotes and multi-paragraph double speech
        "\u201c{first}\u201d {verb} {name}.\n\n\u201c{reply}\u201d {verb} {other}.",
        "\u201cWait!\u201dHaha!\u201d {verb} {name}.",
        # single-quoted speech with no nested pair (unchanged, not targeted)
        "\u2018{first}\u2019 {verb} {name}. {other} left.",
        "\u2018The dogs\u2019 {noun} are here,\u2019 {verb} {name}.",
        "\u2018It is {number}\" wide,\u2019 {verb} {name}.",
        "\u2018Anna\u2019s {noun} are here,\u2019 {verb} {name}.",
        # a quotation in the narration of a double-quoted book (finding 4)
        "He read \u2018the sign said \u201cclosed\u201d\u2019 twice. \u201c{first}\u201d {verb} {name}.",
        # single span inside double speech (finding 1)
        "\u201cHe asked \u2018did she say \u201cyes\u201d?\u2019 loudly,\u201d {verb} {name}.",
        # British multi-paragraph single quotes (finding 6)
        "\u2018She told me \u201crun\u201d and I ran,\n\nand I kept running,\u2019 {verb} {name}.",
    )
    index = 0
    while len(corpus) < 260:
        first = lines[index % len(lines)]
        reply = replies[(index // len(lines)) % len(replies)]
        verb = verbs[index % len(verbs)]
        name = names[(index // 2) % len(names)]
        other = names[(index // 3 + 1) % len(names)]
        template = templates[index % len(templates)]
        corpus.append(template.format(
            first=first, reply=reply, verb=verb, name=name, other=other,
            title=titles[index % len(titles)],
            noun=possessives[index % len(possessives)],
            number=index % 13 + 2,
            pair=guillemet_pairs[index % len(guillemet_pairs)]))
        index += 1
    # Chunks split at a source boundary (finding 6): neither half is targeted.
    corpus.extend([SPLIT_HEAD, SPLIT_TAIL, BRITISH, NARRATION_IN_DOUBLE_BOOK])
    return corpus


def _differential_digest():
    payload = [_decision_and_gate(chunk) for chunk in _differential_corpus()]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class SingleQuotedSpeechTests(unittest.TestCase):
    def assertions_for_issue_chunk(self, chunk, correct, inverted,
                                   expected_regions=None):
        analysis = analyze_outer_quote_regions(chunk)
        self.assertTrue(analysis.get("nested_single_quoted_speech"))
        self.assertEqual(len(analysis.get("adopted_single_quote_spans") or ()), 1)
        if expected_regions is not None:
            self.assertEqual(expected_regions, analysis["regions"])
        for mode in ("auto", "lexical"):
            with self.subTest(mode=mode):
                entries, resolution = tp.quote_regions_decision(mode, chunk, analysis)
                self.assertIsNone(entries)
                self.assertIsNone(resolution)
        quote_entries, quote_resolution = tp.quote_regions_decision(
            "quotes", chunk, analysis)
        self.assertEqual("quote_presegmented", quote_resolution)
        # "quotes" has no model to ask: it keeps the correct single-quote
        # split, never the inverted one.
        self.assertNotEqual([entry["type"] for entry in inverted],
                            [entry["type"] for entry in quote_entries])
        self.assertEqual(analysis["regions"], quote_entries)
        self.assertEqual("SPOKEN", quote_entries[0]["type"])
        self.assertEqual("NARRATOR", quote_entries[-1]["type"])
        report = validate_segment_quality(chunk, correct, quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])
        self.assertNotIn("mixed_quote_region",
                         {finding["code"] for finding in report["findings"]})
        bad = validate_segment_quality(chunk, inverted, quote_analysis=analysis)
        self.assertFalse(bad["passed"])
        self.assertIn("quote_region_misclassified",
                      {finding["code"] for finding in bad["findings"]})

    def test_first_issue_example_is_not_presegmented_inside_out(self):
        self.assertions_for_issue_chunk(
            FIRST, FIRST_CORRECT, FIRST_INVERTED,
            [{"type": "SPOKEN", "text": "She told me run and I ran,"},
             {"type": "NARRATOR", "text": "said Anna. Ben left."}])

    def test_second_issue_example_is_not_presegmented_inside_out(self):
        # Only the span that nests a double quote is adopted (finding 2), so
        # the later plain single-quoted speeches stay narration as on main.
        analysis = analyze_outer_quote_regions(SECOND)
        self.assertTrue(analysis.get("nested_single_quoted_speech"))
        self.assertEqual(SECOND_NARROW, analysis["regions"])
        for mode in ("auto", "lexical"):
            self.assertEqual((None, None),
                             tp.quote_regions_decision(mode, SECOND, analysis))
        report = validate_segment_quality(SECOND, SECOND_NARROW,
                                          quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])

    def test_straight_quote_version_is_not_presegmented_inside_out(self):
        self.assertions_for_issue_chunk(
            STRAIGHT, STRAIGHT_CORRECT, STRAIGHT_INVERTED,
            [{"type": "SPOKEN", "text": "She told me run and I ran,"},
             {"type": "NARRATOR", "text": "said Anna. Ben left."}])

    def test_the_same_text_without_nested_quotes_is_unchanged(self):
        analysis = analyze_outer_quote_regions(CONTROL)
        self.assertFalse(analysis.get("nested_single_quoted_speech"))
        self.assertEqual([], analysis["regions"])
        self.assertEqual([], analysis.get("adopted_single_quote_spans"))
        for mode in ("auto", "lexical"):
            self.assertEqual((None, None),
                             tp.quote_regions_decision(mode, CONTROL, analysis))
        report = validate_segment_quality(CONTROL, CONTROL_CORRECT,
                                          quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])

    def test_a_double_quoted_book_with_nested_single_quotes_is_unchanged(self):
        analysis = analyze_outer_quote_regions(DOUBLE_BOOK)
        self.assertFalse(analysis.get("nested_single_quoted_speech"))
        self.assertEqual([], analysis.get("adopted_single_quote_spans"))
        self.assertEqual(
            [{"type": "SPOKEN", "text": "She said \u2018run\u2019 and left,"},
             {"type": "NARRATOR", "text": "said Anna. Ben left."}],
            analysis["regions"])
        entries, resolution = tp.quote_regions_decision("auto", DOUBLE_BOOK, analysis)
        self.assertEqual("quote_presegmented", resolution)
        self.assertEqual(analysis["regions"], entries)
        report = validate_segment_quality(DOUBLE_BOOK, analysis["regions"],
                                          quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])

    def test_unanadopted_span_does_not_whitelist_a_double_quoted_chunk(self):
        # Finding 1, repro A: the span is inside the double speech, so the
        # decision never adopts it and its marks are not content for the gate.
        analysis = analyze_outer_quote_regions(A_CHUNK)
        self.assertFalse(analysis.get("nested_single_quoted_speech"))
        self.assertEqual([], analysis.get("adopted_single_quote_spans"))
        self.assertEqual(A_REGIONS, analysis["regions"])
        entries, resolution = tp.quote_regions_decision("auto", A_CHUNK, analysis)
        self.assertEqual("quote_presegmented", resolution)
        self.assertEqual(A_REGIONS, entries)
        for label, row in (("truthful", A_TRUTHFUL), ("keeps outer", A_KEEPS_OUTER)):
            with self.subTest(label=label):
                report = validate_segment_quality(A_CHUNK, row, quote_analysis=analysis)
                self.assertFalse(report["passed"], "main rejects this row")
                self.assertIn("mixed_quote_region",
                              {finding["code"] for finding in report["findings"]})

    def test_a_quoted_title_in_narration_is_not_adopted_as_speech(self):
        # Finding 2: only the span that nests the double quote is speech.
        analysis = analyze_outer_quote_regions(TITLE)
        self.assertTrue(analysis.get("nested_single_quoted_speech"))
        spans = analysis.get("adopted_single_quote_spans")
        self.assertEqual(1, len(spans))
        self.assertEqual((0, 1), (spans[0][0], spans[0][1]))
        self.assertEqual("He said \u201crun\u201d and left,",
                         TITLE[spans[0][1]:spans[0][2]])
        self.assertEqual(TITLE_REGIONS, analysis["regions"])
        self.assertNotIn("Hamlet",
                         " ".join(region["text"] for region in analysis["regions"]
                                  if region["type"] == "SPOKEN"))
        for mode in ("auto", "lexical"):
            self.assertEqual((None, None),
                             tp.quote_regions_decision(mode, TITLE, analysis))
        report = validate_segment_quality(TITLE, TITLE_TRUTHFUL,
                                          quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])

    def test_gate_allows_nested_marks_by_position_not_by_character(self):
        analysis = analyze_outer_quote_regions(FIRST)
        # The correct split reproduces the adopted span's content verbatim.
        good = validate_segment_quality(FIRST, FIRST_CORRECT, quote_analysis=analysis)
        self.assertTrue(good["passed"], good["findings"])
        # The same characters, but the outer delimiters are still there: the
        # two outermost marks are outside the adopted span, so this is mixed.
        keeps_outer = [
            {"type": "SPOKEN",
             "text": "\u201cShe told me \u201crun\u201d and I ran,\u201d"},
            {"type": "NARRATOR", "text": "said Anna. Ben left."},
        ]
        report = validate_segment_quality(FIRST, keeps_outer,
                                          quote_analysis=analysis)
        self.assertFalse(report["passed"])
        self.assertIn("mixed_quote_region",
                      {finding["code"] for finding in report["findings"]})
        # An extra double-quoted phrase after the adopted content is not part
        # of the span either, even though the characters are allowed in it.
        extra = [
            {"type": "SPOKEN",
             "text": "She told me \u201crun\u201d and I ran, \u201cextra\u201d"},
            {"type": "NARRATOR", "text": "said Anna. Ben left."},
        ]
        report = validate_segment_quality(FIRST, extra, quote_analysis=analysis)
        self.assertFalse(report["passed"])
        self.assertIn("mixed_quote_region",
                      {finding["code"] for finding in report["findings"]})

    def test_possessive_and_elision_before_the_real_closer(self):
        # Finding 3: `dogs’`, `boys'` and `rock ’n’ roll` must not truncate the
        # span before the nested double quote.
        for label, (chunk, speech, analysis_speech, narration) in CLOSER_CASES.items():
            with self.subTest(label=label):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertTrue(analysis.get("nested_single_quoted_speech"))
                self.assertEqual([{"type": "SPOKEN", "text": analysis_speech},
                                  {"type": "NARRATOR", "text": narration}],
                                 analysis["regions"])
                # The truthful split keeps the nested quote marks.
                report = validate_segment_quality(
                    chunk, [{"type": "SPOKEN", "text": speech},
                            {"type": "NARRATOR", "text": narration}],
                    quote_analysis=analysis)
                self.assertTrue(report["passed"], report["findings"])

    def test_narration_quotation_in_a_double_quoted_book_is_unchanged(self):
        # Finding 4: the chunk already has depth-0 double speech, so no single
        # span is adopted and decision and gate stay main's.
        analysis = analyze_outer_quote_regions(NARRATION_IN_DOUBLE_BOOK)
        self.assertFalse(analysis.get("nested_single_quoted_speech"))
        self.assertEqual([], analysis.get("adopted_single_quote_spans"))
        self.assertEqual(NARRATION_IN_DOUBLE_BOOK_REGIONS, analysis["regions"])
        for mode in ("auto", "quotes", "lexical"):
            entries, resolution = tp.quote_regions_decision(
                mode, NARRATION_IN_DOUBLE_BOOK, analysis)
            self.assertEqual("quote_presegmented", resolution)
            self.assertEqual(NARRATION_IN_DOUBLE_BOOK_REGIONS, entries)
        report = validate_segment_quality(
            NARRATION_IN_DOUBLE_BOOK, NARRATION_IN_DOUBLE_BOOK_REGIONS,
            quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])

    def test_an_inch_mark_inside_an_adopted_span_stays_content(self):
        # Finding 5: `6"` is not a nested opener, so it must not add depth and
        # swallow the attribution.
        analysis = analyze_outer_quote_regions(INCH)
        self.assertTrue(analysis.get("nested_single_quoted_speech"))
        self.assertEqual([], analysis["repairs"])
        self.assertEqual(INCH_REGIONS, analysis["regions"])
        report = validate_segment_quality(INCH, INCH_TRUTHFUL,
                                          quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])

    def test_guillemets_and_corner_brackets_nested_in_single_quotes(self):
        # Finding 7: `_DOUBLE_QUOTE_PAIRS` is PAIRED, not a hand copy.
        self.assertEqual(tuple(PAIRED), pq._DOUBLE_QUOTE_PAIRS)
        for label, chunk, truthful, analysis_text in (
                ("guillemets", GUILLEMETS,
                 "He said \u00abbonjour\u00bb twice,",
                 "He said bonjour twice,"),
                ("corner brackets", CORNER_BRACKETS,
                 "He said \u300c\u3053\u3093\u306b\u3061\u306f\u300d twice,",
                 "He said \u3053\u3093\u306b\u3061\u306f twice,")):
            with self.subTest(label=label):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertTrue(analysis.get("nested_single_quoted_speech"))
                self.assertEqual([{"type": "SPOKEN", "text": analysis_text},
                                  {"type": "NARRATOR", "text": "she said."}],
                                 analysis["regions"])
                report = validate_segment_quality(
                    chunk, [{"type": "SPOKEN", "text": truthful},
                            {"type": "NARRATOR", "text": "she said."}],
                    quote_analysis=analysis)
                self.assertTrue(report["passed"], report["findings"])

    def test_british_multiparagraph_and_split_chunks_are_pinned_limits(self):
        # Finding 6: both are standard typography this decided fix does not
        # reach. Pin main's behaviour so a future change is deliberate.
        for chunk, regions in ((BRITISH, BRITISH_REGIONS),
                               (SPLIT_HEAD, SPLIT_HEAD_REGIONS),
                               (SPLIT_TAIL, SPLIT_TAIL_REGIONS)):
            with self.subTest(chunk=chunk[:20]):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertFalse(analysis.get("nested_single_quoted_speech"))
                self.assertEqual([], analysis.get("adopted_single_quote_spans"))
                self.assertEqual(regions, analysis["regions"])

    def test_apostrophes_elisions_and_wrong_direction_do_not_open_speech(self):
        cases = {
            # Curly ’ is the apostrophe, so it can never open: elisions and
            # possessives are here for the straight-quote versions too.
            "curly elision": "He said \u2019tis \u201clate\u201d now.",
            "curly em": "He said \u2019em \u201clate\u201d now.",
            "curly nineties": "The \u201990s \u201cmusic\u201d was loud.",
            "straight elision": "He said 'tis \u201clate\u201d now and James' \u201cbook\u201d came.",
            "straight em": "He said 'em \u201clate\u201d now and James' \u201cbook\u201d came.",
            "straight nineties": "The '90s \u201cmusic\u201d was loud and James' \u201cbook\u201d came.",
            "possessive": "Anna's \u201cbook\u201d and James' \u201cpen\u201d came.",
            "plural possessive": "The Joneses\u2019 \u201cbook\u201d and James' \u201cpen\u201d came.",
            # A straight possessive must not open a span that then adopts the
            # double-quoted phrase (the opener guard is what stops it).
            "straight possessive before quote": "James' \u201cbook\u201d is here, and 'Hello,' she said.",
            # A contraction apostrophe must not open a span either.
            "straight contraction before quote": "He can't say \u201cno\u201d, and 'Hello,' she said.",
            # A ‘ used as the closer of a low-high ‚…‘ pair is not an opener.
            "wrong direction": "\u201aHallo\u2018 \u201cbook\u201d and James\u2019 \u201cpen\u201d came.",
        }
        for label, chunk in cases.items():
            with self.subTest(label=label):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertFalse(analysis.get("nested_single_quoted_speech"))
                # The double quotes stay the only convention, as on main.
                self.assertGreater(len(analysis["regions"]), 1)

    def test_an_unclosed_opener_does_not_run_past_its_paragraph_or_500_chars(self):
        # A closer-shaped apostrophe (followed by a space) waits beyond both
        # bounds; only an unbounded search would pair with it.
        past_paragraph = "\u2018He said \u201crun\u201d\n\nand James\u2019 book came."
        past_limit = ("\u2018He said \u201crun\u201d" + (" word" * 120)
                      + " James\u2019 book came.")
        for label, chunk in (("paragraph", past_paragraph), ("length", past_limit)):
            with self.subTest(label=label):
                if get_single_quoted_speech_spans is None:
                    self.fail("get_single_quoted_speech_spans is missing on this tree")
                spans = get_single_quoted_speech_spans(chunk)
                self.assertEqual([], spans)
                self.assertFalse(analyze_outer_quote_regions(chunk).get(
                    "nested_single_quoted_speech"))

    def test_double_quoted_decisions_and_gate_are_unchanged(self):
        # Differential guard: 260 synthetic chunks plus the quote shapes the
        # repository already tests must produce byte-identical decisions and
        # gate reports to origin/main b5989368e (the digest was produced from
        # that tree). Both the analysis-backed gate and the analysis-free gate
        # are hashed.
        self.assertEqual(
            "6fe1feffb008b342fa340825893c2235b298fd3562c03408bc4a7a5c1a75ab8a",
            _differential_digest())


if __name__ == "__main__":
    unittest.main()
