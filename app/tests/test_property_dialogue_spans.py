"""Property tests for the source dialogue map and the analyzer that must agree.

Both ``dialogue_spans`` (the source map) and
``pass_quality.analyze_outer_quote_regions`` (the segmenter) decide which
characters the book's own quote marks make speech, and they kept breaking on
inputs that a hand-written example list did not contain: a digit next to a
straight inch mark (`6"`), a curly inch mark (`6”`), a paragraph break inside a
quote, and the same shape reappeared with another separator (#2318). The #2318
shapes that used to invert are drawn here, in narration and inside speech:
`The board is 6" wide`, `„Es ist 6" breit“`, `«C'est 6" de large»`,
`「6"の板」`, the full-width `6＂`, and the foot mark `5'`. Those are
combinations of a convention, digits, wraps and invisible characters, which is
what a property test draws instead of enumerates.

Deterministic and bounded for CI: ``tests/hypothesis_profile.py`` (30 examples,
derandomized, no example database, no deadline).

Known limits deliberately excluded, with the shape they come from:

* German low-high ``„…“`` with a digit before the closing ``“`` is scoped out of
  #2318 and tracked in #2357. The well-formed generator never puts a digit
  immediately before a quote mark, and the inch-mark generator uses the German
  closer only after a comma, so the German ``6“`` shape is not generated.
* The ``”.”`` shape (a closer glued to a full stop) and roman/prime numerals
  ``Ⅻ``/``6′`` are pinned as known limits in #2318. Narration can start with a
  number (it may begin `1945 …`), but no numeral or prime mark is generated, so
  neither shape is produced.

The generated text is synthetic; no book text is used.
"""
import sys
import unittest
from collections import namedtuple

from hypothesis import given, strategies as st

from tests import hypothesis_profile
from dialogue_spans import (DASH_LINE, LABEL_LINE, MIN_CONVENTION_PAIRS,
                            PAIRED, _paired_spans, detect_convention,
                            get_other_speech_spans, spoken_spans,
                            validate_spoken_spans)
from pass_quality import analyze_outer_quote_regions

DialogueCase = namedtuple("DialogueCase",
                          "opener closer text speeches narrations")

# Several scripts, so a character class or a case rule cannot pass unnoticed.
_WORDS = (
    "river", "quietly", "morning", "the", "she", "said", "wait", "cold",
    "night", "letter", "window", "door", "answer", "silence", "promise",
    "река", "утро", "Дмитрий", "Наташа", "молчание",
    "θάλασσα", "φως", "Αλέξης", "Θεοδώρα",
    "राम", "सीता", "मोहन", "अर्जुन",
    "ดาว", "ฟ้า", "ความเงียบ",
    "夜", "風", "静", "서울", "바다",
    "ملاك", "שרה",
)
# Numbers for the inch-mark shapes both readers have to leave alone. Narration
# may start with one of these; nothing puts one directly before a quote mark.
_NARRATION_EXTRA = ("7", "1945", "3", "12")
# A zero-width space, a zero-width non-joiner, a soft hyphen and a word joiner:
# the invisible characters that already broke speaker labels and identity.
_SILENT = ("\u200b", "\u200c", "\u00ad", "\u2060")
_END_PUNCTUATION = (".", "!", "?", "…", ",")
_INNER_PUNCTUATION = (",", ";", ":", "—", "–", "...")
_MEASURE_WORDS = ("wide", "long", "tall", "deep", "high")


@st.composite
def word(draw):
    """A word from a real script, sometimes carrying an invisible character."""
    value = draw(st.sampled_from(_WORDS))
    if draw(st.booleans()) and len(value) > 1:
        at = draw(st.integers(1, len(value) - 1))
        value = value[:at] + draw(st.sampled_from(_SILENT)) + value[at:]
    return value


@st.composite
def speech_text(draw):
    """Speech that never ends in a digit (the #2357 / inch-mark shape)."""
    tokens = [draw(word()) for _ in range(draw(st.integers(2, 4)))]
    if draw(st.booleans()) and len(tokens) > 1:
        at = draw(st.integers(1, len(tokens) - 1))
        tokens.insert(at, draw(st.sampled_from(_INNER_PUNCTUATION)))
    text = " ".join(tokens)
    if draw(st.booleans()):
        text += draw(st.sampled_from(_END_PUNCTUATION))
    return text


@st.composite
def narration_text(draw):
    """Narration with digits, ending in a sentence mark and no quote mark."""
    words = [draw(st.sampled_from(_WORDS + _NARRATION_EXTRA))
             for _ in range(draw(st.integers(1, 4)))]
    return " ".join(words) + draw(st.sampled_from((".", "!", "?", "…")))


@st.composite
def well_formed_dialogue(draw, convention=None):
    """``<open>speech<close> narration.`` repeated, one paragraph per line."""
    opener, closer = convention or draw(st.sampled_from(PAIRED))
    lines, speeches, narrations = [], [], []
    for _ in range(draw(st.integers(MIN_CONVENTION_PAIRS, MIN_CONVENTION_PAIRS + 2))):
        speech, narration = draw(speech_text()), draw(narration_text())
        lines.append(f"{opener}{speech}{closer} {narration}")
        speeches.append(speech)
        narrations.append(narration)
    return DialogueCase(opener, closer, "\n\n".join(lines), speeches, narrations)


@st.composite
def inch_mark_dialogue(draw, convention):
    """Every line carries a digit next to a straight quote, in speech and in narration.

    The speech alternates an inch mark (`5"`, or the full-width `5＂` in a
    full-width book) with a foot mark (`5'`), so both #2318 shapes are present.
    The narration always carries an inch mark. A book whose convention is
    German low-high still gets these marks only inside speech or before the
    line break, never next to its `“` closer (#2357).
    """
    opener, closer = convention
    inch = "＂" if opener == "＂" else '"'
    lines, speeches, narrations = [], [], []
    for _ in range(draw(st.integers(MIN_CONVENTION_PAIRS, MIN_CONVENTION_PAIRS + 2))):
        number = draw(st.integers(1, 999))
        unit = draw(st.sampled_from(_MEASURE_WORDS))
        if draw(st.booleans()):
            speech = f"It is {number}' {unit},"
        else:
            speech = f"The board is {number}{inch} {unit},"
        narration = f"he measured {draw(st.integers(1, 999))}{inch} {unit}."
        lines.append(f"{opener}{speech}{closer} {narration}")
        speeches.append(speech)
        narrations.append(narration)
    return DialogueCase(opener, closer, "\n\n".join(lines), speeches, narrations)


def spoken_texts(text, spans):
    return [text[start:end] for start, end in spans]


def analyzer_spoken(text):
    result = analyze_outer_quote_regions(text)
    return ([region["text"] for region in result["regions"]
             if region["type"] == "SPOKEN"], result["repairs"])


def hard_wrapped(value, raw_index):
    """-> `value` with one space replaced by a single newline, where possible."""
    spaces = [index for index, char in enumerate(value) if char == " "]
    if spaces:
        at = spaces[raw_index % len(spaces)]
        return value[:at] + "\n" + value[at + 1:]
    if len(value) < 3:
        # No interior position to wrap; this case still runs on other draws.
        return value
    at = (raw_index + 1) % (len(value) - 1) + 1
    return value[:at] + "\n" + value[at:]


def _well_formed_convention_property(convention):
    @given(well_formed_dialogue(convention=convention))
    def property_(test_case, case):
        test_case.assert_well_formed_yields(case)
    return property_


def _inch_mark_convention_property(convention):
    @given(inch_mark_dialogue(convention))
    def property_(test_case, case):
        test_case.assert_well_formed_yields(case)
    return property_


# Built at module scope so ``given_test_settings`` inspects every per-convention
# ``@given`` property (a stray ``@settings`` on either factory fails ProfileTests).
_WELL_FORMED_CONVENTION_PROPERTIES = {
    convention: _well_formed_convention_property(convention)
    for convention in PAIRED
}
_INCH_MARK_CONVENTION_PROPERTIES = {
    convention: _inch_mark_convention_property(convention)
    for convention in PAIRED
}


class WellFormedDialogueTests(unittest.TestCase):
    def assert_well_formed_yields(self, case):
        """``<open>speech<close> narration.`` -> exactly the speeches."""
        self.assertEqual("paired_quotes", detect_convention(case.text))
        self.assertEqual(case.speeches, spoken_texts(case.text, spoken_spans(case.text)))
        spoken, repairs = analyzer_spoken(case.text)
        self.assertEqual(case.speeches, spoken)
        self.assertEqual([], repairs)

    def test_every_supported_convention_yields_exactly_its_speeches(self):
        # One property per convention, so no convention depends on sampling to
        # be covered. German low-high needs MIN_CONVENTION_PAIRS speeches
        # before the module keeps it as the book's convention. subTest names a
        # failing convention and lets the other eight still run.
        for convention, property_ in _WELL_FORMED_CONVENTION_PROPERTIES.items():
            with self.subTest(convention=convention):
                property_(self)

    def test_inch_marks_are_text_in_speech_and_narration(self):
        # #2318: a digit next to a straight quote is a measurement, not a
        # delimiter. Both readers are asserted against the known speeches, so
        # losing the inch-mark decision in either one fails here even when the
        # two still agree with each other.
        for convention, property_ in _INCH_MARK_CONVENTION_PROPERTIES.items():
            with self.subTest(convention=convention):
                property_(self)

    @given(well_formed_dialogue())
    def test_the_two_functions_agree_which_text_is_spoken(self, case):
        # The source map and the segmenter are two independent readers of the
        # same marks. They disagreed on inch marks for several rounds (#2318),
        # so this pins agreement rather than each one's private output.
        spans = spoken_spans(case.text)
        spoken, _repairs = analyzer_spoken(case.text)
        self.assertEqual(spoken_texts(case.text, spans), spoken)

    @given(well_formed_dialogue(), st.integers(min_value=0), st.booleans())
    def test_a_single_newline_is_a_soft_wrap(self, case, raw_index, inside_speech):
        # A single newline is a soft wrap, not a paragraph break: sources are
        # hard-wrapped, and wrapping must not move a word between voices. The
        # wrap inside a speech is the discriminating shape - the narration
        # after the closer is outside the quote for both readers, so wrapping
        # only there cannot catch a reader that treats every newline as a
        # paragraph boundary.
        index = raw_index % len(case.speeches)
        if inside_speech:
            speeches = list(case.speeches)
            speeches[index] = hard_wrapped(speeches[index], raw_index)
            narrations = case.narrations
        else:
            speeches = case.speeches
            narrations = list(case.narrations)
            narrations[index] = hard_wrapped(narrations[index], raw_index)
        wrapped = "\n\n".join(f"{case.opener}{speech}{case.closer} {body}"
                              for speech, body in zip(speeches, narrations))

        self.assertEqual(speeches, spoken_texts(wrapped, spoken_spans(wrapped)))
        spoken, repairs = analyzer_spoken(wrapped)
        self.assertEqual(speeches, spoken)
        self.assertEqual([], repairs)


class InvariantTests(unittest.TestCase):
    """Invariants that must hold for ANY text, well formed or not."""

    # Every delimiter, number, line ending, invisible character and dash the
    # two readers treat specially, plus ordinary letters to separate them.
    ALPHABET = ("abcXYZ.,;:!?…—–-'\""
                "0123456789\t\n\r"
                + "".join(dict.fromkeys(mark for pair in PAIRED for mark in pair))
                + "".join(_SILENT))

    @given(st.text(alphabet=ALPHABET, max_size=120))
    def test_spans_stay_inside_the_source_in_order_and_disjoint(self, text):
        spans = spoken_spans(text)
        self.assertEqual(sorted(spans), spans)
        previous_end = 0
        for start, end in spans:
            self.assertLessEqual(previous_end, start)
            self.assertLess(start, end)
            self.assertLessEqual(end, len(text))
            previous_end = end

        # Before the sort-and-merge repair: every convention's own answer is
        # still a real source range (overlap is legal there). Validating the
        # merged output instead would only describe the merge, which the module
        # already guarantees by construction.
        self.assertIs(True, validate_spoken_spans(_paired_spans(text), text))
        self.assertIs(True, validate_spoken_spans(
            [(match.start(1), match.end(1)) for match in DASH_LINE.finditer(text)],
            text))
        self.assertIs(True, validate_spoken_spans(
            [(match.start(2), match.end(2)) for match in LABEL_LINE.finditer(text)],
            text))
        # #2360: an unclosed opener with no paragraph body used to come back as
        # the zero-length (start, start) range this validator refuses. The
        # property now covers that shape instead of excluding it.
        self.assertIs(True, validate_spoken_spans(
            get_other_speech_spans(text, "paired_quotes"), text))

    @given(st.text(alphabet=ALPHABET, max_size=120))
    def test_analyzer_regions_and_repairs_stay_inside_the_source(self, text):
        result = analyze_outer_quote_regions(text)
        self.assertEqual(0, result["final_depth"])
        for region in result["regions"]:
            self.assertIn(region["type"], ("SPOKEN", "NARRATOR"))
            self.assertTrue(region["text"])
            self.assertEqual(region["text"].strip(), region["text"])
        for repair in result["repairs"]:
            self.assertIn(repair["code"], ("inferred_missing_close_quote",
                                           "inferred_missing_open_quote",
                                           "ignored_unmatched_close_quote"))
            offset = repair.get("offset", repair.get("position"))
            self.assertIsNotNone(offset)
            self.assertTrue(0 <= offset <= len(text))


class ProfileTests(unittest.TestCase):
    def test_this_modules_property_tests_use_the_bounded_profile(self):
        configured = hypothesis_profile.given_test_settings(sys.modules[__name__])
        expected = {
            "InvariantTests.test_spans_stay_inside_the_source_in_order_and_disjoint",
            "InvariantTests.test_analyzer_regions_and_repairs_stay_inside_the_source",
            "WellFormedDialogueTests.test_the_two_functions_agree_which_text_is_spoken",
            "WellFormedDialogueTests.test_a_single_newline_is_a_soft_wrap",
        }
        for convention in PAIRED:
            expected.add(f"_WELL_FORMED_CONVENTION_PROPERTIES[{convention!r}]")
            expected.add(f"_INCH_MARK_CONVENTION_PROPERTIES[{convention!r}]")
        self.assertEqual(expected, set(configured))
        for name, active in configured.items():
            self.assertIs(hypothesis_profile.ACTIVE_SETTINGS, active, name)


if __name__ == "__main__":
    unittest.main()
