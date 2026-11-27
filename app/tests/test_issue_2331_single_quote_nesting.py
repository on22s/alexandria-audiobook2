"""#2331: double-quoted speech nested inside single-quoted speech.

In a book whose dialogue uses single curly quotes, a chunk that contains a
nested double-quoted phrase was segmented inside out: the nested phrase became
the only speech and the real dialogue became narration. The quote decision
accepted that inverted split and the segment gate rejected the correct one with
mixed_quote_region.

A full single-quote pairing model is out of scope. This fix abstains: when a
double-quoted phrase sits inside matched single-quoted speech, the double quotes
are inner speech, not the chunk's outer convention, so the model is asked. The
same analysis is the gate, so the correct split still has to pass.
"""
import hashlib
import json
import unittest

from pass_quality import (analyze_outer_quote_regions, validate_segment_quality)
try:  # Importable on the pre-fix tree, so the differential digest can be checked there.
    from pass_quality import single_quoted_speech_spans
except ImportError:  # pragma: no cover - only on origin/main before #2331
    single_quoted_speech_spans = None
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
SECOND_CORRECT = [
    {"type": "SPOKEN",
     "text": "She told me \u201crun\u201d and I ran, and then I hid by the river until the morning came,"},
    {"type": "NARRATOR", "text": "said Anna."},
    {"type": "SPOKEN", "text": "Were you afraid?"},
    {"type": "NARRATOR", "text": "asked Ben."},
    {"type": "SPOKEN", "text": "A little,"},
    {"type": "NARRATOR", "text": "said Anna. She looked at the door."},
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

MIXED = ("\u2018She told me \u201crun\u201d and I ran,\u2019 said Anna.\n\n"
         "\u201cWhere did you go?\u201d asked Cara.")
MIXED_CORRECT = [
    {"type": "SPOKEN", "text": "She told me \u201crun\u201d and I ran,"},
    {"type": "NARRATOR", "text": "said Anna."},
    {"type": "SPOKEN", "text": "Where did you go?"},
    {"type": "NARRATOR", "text": "asked Cara."},
]

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
    return decisions


def _double_quoted_corpus():
    corpus = list(DOUBLE_QUOTED_FIXTURES)
    names = ("Anna", "Ben", "Cara", "Dan", "Eve", "Finn", "Gus", "Hana")
    verbs = ("said", "asked", "replied", "whispered")
    lines = ("Hello.", "Where did you go?", "I ran to the river.", "Don't wait.")
    replies = ("I stayed.", "A little.", "Not yet.", "Come back.")
    templates = (
        "\u201c{first}\u201d {verb} {name}. \u201c{reply}\u201d {verb} {other}.",
        "\u201c{first}\u201d {verb} {name}. Alice waited. \u201c{reply}\u201d {verb} {other}.",
        "{name} stood. \u201c{first}\u201d {verb} {other}. \u201c{reply}\u201d {verb} {name}.",
        "\u201c{first}\u201d {verb} {name}.\n\n\u201c{reply}\u201d {verb} {other}.",
        "\u201c{first} {reply}\u201d {verb} {name}. \u201c{reply}\u201d {verb} {other}.",
        "\u201c{first}\u201d {verb} {name}. He is 6\" wide. \u201c{reply}\u201d {verb} {other}.",
    )
    index = 0
    while len(corpus) < 210:
        first = lines[index % len(lines)]
        reply = replies[(index // len(lines)) % len(replies)]
        verb = verbs[index % len(verbs)]
        name = names[(index // 2) % len(names)]
        other = names[(index // 3 + 1) % len(names)]
        template = templates[index % len(templates)]
        corpus.append(template.format(first=first, reply=reply, verb=verb,
                                      name=name, other=other))
        index += 1
    return corpus


def _differential_digest():
    payload = [_decision_and_gate(chunk) for chunk in _double_quoted_corpus()]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class SingleQuotedSpeechTests(unittest.TestCase):
    def assertions_for_issue_chunk(self, chunk, correct, inverted):
        analysis = analyze_outer_quote_regions(chunk)
        self.assertTrue(analysis.get("nested_single_quoted_speech"))
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
        self.assertions_for_issue_chunk(FIRST, FIRST_CORRECT, FIRST_INVERTED)

    def test_second_issue_example_is_not_presegmented_inside_out(self):
        self.assertions_for_issue_chunk(SECOND, SECOND_CORRECT, FIRST_INVERTED)

    def test_straight_quote_version_is_not_presegmented_inside_out(self):
        analysis = analyze_outer_quote_regions(STRAIGHT)
        self.assertTrue(analysis.get("nested_single_quoted_speech"))
        for mode in ("auto", "lexical"):
            self.assertEqual((None, None),
                             tp.quote_regions_decision(mode, STRAIGHT, analysis))
        report = validate_segment_quality(STRAIGHT, STRAIGHT_CORRECT,
                                          quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])
        bad = validate_segment_quality(STRAIGHT, STRAIGHT_INVERTED,
                                       quote_analysis=analysis)
        self.assertFalse(bad["passed"])

    def test_the_same_text_without_nested_quotes_is_unchanged(self):
        analysis = analyze_outer_quote_regions(CONTROL)
        self.assertFalse(analysis.get("nested_single_quoted_speech"))
        self.assertEqual([], analysis["regions"])
        for mode in ("auto", "lexical"):
            self.assertEqual((None, None),
                             tp.quote_regions_decision(mode, CONTROL, analysis))
        report = validate_segment_quality(CONTROL, CONTROL_CORRECT,
                                          quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])

    def test_a_double_quoted_book_with_nested_single_quotes_is_unchanged(self):
        analysis = analyze_outer_quote_regions(DOUBLE_BOOK)
        self.assertFalse(analysis.get("nested_single_quoted_speech"))
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

    def test_mixed_conventions_in_different_paragraphs_ask_and_split_both(self):
        analysis = analyze_outer_quote_regions(MIXED)
        self.assertTrue(analysis.get("nested_single_quoted_speech"))
        self.assertEqual(["SPOKEN", "NARRATOR", "SPOKEN", "NARRATOR"],
                         [region["type"] for region in analysis["regions"]])
        for mode in ("auto", "lexical"):
            self.assertEqual((None, None),
                             tp.quote_regions_decision(mode, MIXED, analysis))
        report = validate_segment_quality(MIXED, MIXED_CORRECT,
                                          quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])

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
                if single_quoted_speech_spans is None:
                    self.fail("single_quoted_speech_spans is missing on this tree")
                spans = single_quoted_speech_spans(chunk)
                self.assertEqual([], spans)
                self.assertFalse(analyze_outer_quote_regions(chunk).get(
                    "nested_single_quoted_speech"))

    def test_double_quoted_decisions_and_gate_are_unchanged(self):
        # Differential guard: 210 synthetic double-quoted chunks plus the quote
        # shapes the repository already tests must produce byte-identical
        # decisions and gate reports to origin/main c00850458 (the digest was
        # produced from that tree).
        self.assertEqual(
            "5ec0a554a4cffcb969e2d80a009628270d67f571a1864828f4bb4fa434097aad",
            _differential_digest())


if __name__ == "__main__":
    unittest.main()
