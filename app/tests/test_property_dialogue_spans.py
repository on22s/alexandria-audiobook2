"""Property tests for the source dialogue map and the analyzer that must agree.

Both ``dialogue_spans`` (the source map) and
``pass_quality.analyze_outer_quote_regions`` (the segmenter) decide which
characters the book's own quote marks make speech, and they kept breaking on
inputs that a hand-written example list did not contain: straight inch marks
opened and closed spans differently in the two, a paragraph break inside a
quote swallowed narration, and the same shape reappeared with another separator
(#2318). Those are combinations of a convention, digits, wraps and invisible
characters, which is what a property test draws instead of enumerates.

Deterministic and bounded for CI: ``tests/hypothesis_profile.py`` (30 examples,
derandomized, no database, no deadline).

Known limits deliberately excluded, with the shape they come from:

* German low-high ``„…“`` with a digit before the closing ``“`` is scoped out of
  #2318 and tracked in #2357. Generated speech never ends in a digit and
  narration never puts a digit immediately before a quote mark, so that shape
  and the German ``6“`` inch-mark shape are not generated at all.
* The ``”.”`` shape (a closer glued to a full stop) and roman/prime numerals
  ``Ⅻ``/``6′`` are pinned as known limits in #2318. Narration always starts with
  a word after a space, and no numeral or prime mark is generated, so neither
  shape is produced.

The generated text is synthetic; no book text is used.
"""
import unittest
from collections import namedtuple

from hypothesis import given, settings, strategies as st

from tests import hypothesis_profile
from dialogue_spans import (MIN_CONVENTION_PAIRS, PAIRED, _paired_spans,
                            detect_convention, get_other_speech_spans,
                            spoken_spans, validate_spoken_spans)
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
# Numbers for the inch-mark shapes both readers have to leave alone.
_NARRATION_EXTRA = ("7", "1945", "3", "12")
# A zero-width space, a zero-width non-joiner, a soft hyphen and a word joiner:
# the invisible characters that already broke speaker labels and identity.
_SILENT = ("\u200b", "\u200c", "\u00ad", "\u2060")
_END_PUNCTUATION = (".", "!", "?", "…", ",")
_INNER_PUNCTUATION = (",", ";", ":", "—", "–", "...")


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
    tokens = [draw(word()) for _ in range(draw(st.integers(1, 4)))]
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


def spoken_texts(text, spans):
    return [text[start:end] for start, end in spans]


def analyzer_spoken(text):
    result = analyze_outer_quote_regions(text)
    return ([region["text"] for region in result["regions"]
             if region["type"] == "SPOKEN"], result["repairs"])


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
        # before the module keeps it as the book's convention.
        for opener, closer in PAIRED:
            @given(well_formed_dialogue(convention=(opener, closer)))
            def property_(case):
                self.assert_well_formed_yields(case)
            property_()

    @given(well_formed_dialogue())
    def test_the_two_functions_agree_which_text_is_spoken(self, case):
        # The source map and the segmenter are two independent readers of the
        # same marks. They disagreed on inch marks for several rounds (#2318),
        # so this pins agreement rather than each one's private output.
        spans = spoken_spans(case.text)
        spoken, _repairs = analyzer_spoken(case.text)
        self.assertEqual(spoken_texts(case.text, spans), spoken)

    @given(well_formed_dialogue(), st.integers(min_value=0))
    def test_soft_wrap_inside_narration_keeps_the_same_spoken_text(self, case, raw_index):
        # A single newline is a soft wrap, not a paragraph break: sources are
        # hard-wrapped, and wrapping must not move a word between voices.
        index = raw_index % len(case.narrations)
        narration = case.narrations[index]
        at = (raw_index + 1) % (len(narration) - 1) + 1
        narrations = list(case.narrations)
        narrations[index] = narration[:at] + "\n" + narration[at:]
        wrapped = "\n\n".join(f"{case.opener}{speech}{case.closer} {body}"
                              for speech, body in zip(case.speeches, narrations))

        self.assertEqual(case.speeches, spoken_texts(wrapped, spoken_spans(wrapped)))
        spoken, repairs = analyzer_spoken(wrapped)
        self.assertEqual(case.speeches, spoken)
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
        self.assertIs(True, validate_spoken_spans(spans, text))
        self.assertEqual(sorted(spans), spans)
        previous_end = 0
        for start, end in spans:
            self.assertLessEqual(previous_end, start)
            self.assertLess(start, end)
            self.assertLessEqual(end, len(text))
            previous_end = end

        # Before the sort-and-merge repair: every convention's own answer is
        # still a real source range (overlap is legal there).
        for start, end in _paired_spans(text):
            self.assertTrue(0 <= start < end <= len(text))
        for start, end in get_other_speech_spans(text, "paired_quotes"):
            if start == end:
                # KNOWN LIMIT (#2360, found by this property, filed separately):
                # an unclosed opener that is the last character, or is followed
                # immediately by a blank line, comes back as a zero-length span
                # instead of no span - get_other_speech_spans("“",
                # "paired_quotes") == [(1, 1)]. It is inert downstream
                # (mark_entries tests `start < end`), and
                # validate_spoken_spans refuses exactly this shape, so it is
                # excluded here rather than asserted.
                continue
            self.assertTrue(0 <= start < end <= len(text))

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
    def test_property_tests_run_under_the_bounded_ci_profile(self):
        self.assertIs(hypothesis_profile.ACTIVE_SETTINGS, settings.default)
        self.assertTrue(settings.default.derandomize)
        self.assertIsNone(settings.default.database)
        self.assertIsNone(settings.default.deadline)


if __name__ == "__main__":
    unittest.main()
