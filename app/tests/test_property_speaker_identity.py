"""Property tests for the speaker identity key that every merge decision uses.

``_identity_key`` is the single answer to "are these two spellings the same
character?", so a wrong fold merges two voices into one or splits one character
into two. The Unicode rules it implements are canonical equivalence (NFC/NFD),
case-insensitivity, folding spacing and punctuation, and keeping the combining
marks that are part of a letter while folding the optional diacritics that are
still combining marks. Each of those is a property over every name, not a list
of examples, and the hand-written list in ``test_speaker_identity`` cannot cover
the combinations.

Deliberately out of scope, tracked in #2378: a PRECOMPOSED accented letter is
not folded (``_identity_key("José") == "josé"`` while
``_identity_key("Jose") == "jose"``). NFC composes before the mark fold, so the
optional-diacritic property here uses marks that stay decomposed after NFC;
folding precomposed letters is a production change and not part of this PR.

Deterministic and bounded for CI: ``tests/hypothesis_profile.py`` (30 examples,
derandomized, no database, no deadline).

Deliberately out of scope: the nasal-sign table (anusvara, candrabindu and the
spellings mapped onto them) is covered separately by #2358 (now merged). These
properties use vowel signs, viramas and the Thai tone marks that the key keeps
with their base, so they stay independent of the nasal-sign table.

The generated text is synthetic; no book or person data is used.
"""
import sys
import unicodedata
import unittest

from hypothesis import assume, given, strategies as st

from tests import hypothesis_profile
from speaker_identity import _identity_key

# Several scripts. Caseless scripts (Devanagari, Thai, CJK, Hangul, Arabic,
# Hebrew) make the case property test the no-op case too.
_NAME_WORDS = (
    "ada", "bram", "cora", "dmitri", "eleni", "farid", "gita", "hugo",
    "isla", "jonas", "kai", "lena", "mateo", "nadia", "oskar", "priya",
    "quinn", "rosa", "soren", "tara", "umberto", "vera", "wren", "yusuf",
    "zara", "straße", "Müller", "Øyvind", "Αλέξης", "Θεοδώρα",
    "Дмитрий", "Наташа", "राम", "सीता", "मोहन", "सूर्य", "अर्जुन",
    "ดาว", "ฟ้า", "ความเงียบ", "កុលាប", "ສີຟ້າ", "தமிழ்", "శ్రీ",
    "ཀི", "夜", "風", "서울", "바다", "ميخائيل", "שרה",
)
# Spacing and punctuation fold, and so do the invisible characters that already
# reached a speaker label (#2316).
_SEPARATORS = (" ", "  ", "-", "'", "’", ".", "\u200b", "\u00ad", "")
# Accents inserted into whole words for the canonical/case properties. Whether
# the mark survives the fold depends on whether NFC composes it with its base
# (#2378); those properties only compare re-encodings of one string, so either
# outcome is fine there. The dedicated property below uses non-composing pairs.
_ACCENTS = ("\u0301", "\u0300", "\u0308", "\u0327", "\u0342", "\u0483")

# Optional diacritics that stay combining marks after NFC, so the fold actually
# sees a mark to drop rather than a precomposed letter it keeps (#2378). A base
# with a precomposed accented form (a+acute -> á, w+diaeresis -> ẅ) is excluded
# on purpose.
_NON_COMPOSING_DIACRITICS = (
    ("q", "\u0301"), ("q", "\u0303"), ("x", "\u0300"), ("j", "\u0327"),
    ("c", "\u0303"), ("ж", "\u0301"), ("θ", "\u0308"), ("ب", "\u064e"),
)

# A base letter plus ONE required mark: vowel signs and viramas of the
# abugidas, plus the Thai tone marks (mai ek, mai tai khu) that the key keeps
# with their base. Nasal signs are excluded on purpose (#2358 owns their table).
_REQUIRED_SYLLABLES = (
    ("क", "\u093f"), ("क", "\u0940"), ("क", "\u0941"), ("क", "\u0947"),
    ("क", "\u094d"), ("क", "\u093e"), ("र", "\u093f"), ("म", "\u0941"),
    ("न", "\u093e"), ("क", "\u0942"), ("क", "\u0948"), ("क", "\u094b"),
    ("ก", "\u0e34"), ("ก", "\u0e35"), ("ก", "\u0e48"), ("ท", "\u0e35"),
    ("ด", "\u0e34"), ("ก", "\u0e47"),
    ("ក", "\u17b7"), ("ក", "\u17bb"), ("ཀ", "\u0f72"), ("ཀ", "\u0f74"),
    ("ກ", "\u0eb4"), ("ກ", "\u0eb5"), ("க", "\u0bbe"), ("க", "\u0bc6"),
    ("క", "\u0c3f"), ("త", "\u0c3f"), ("ರ", "\u0ccd"), ("ಕ", "\u0cbe"),
)


@st.composite
def name_word(draw):
    """A word from a real script, sometimes carrying an optional accent."""
    value = draw(st.sampled_from(_NAME_WORDS))
    if draw(st.booleans()):
        at = draw(st.integers(0, len(value)))
        value = value[:at] + draw(st.sampled_from(_ACCENTS)) + value[at:]
    return value


@st.composite
def speaker_name(draw):
    """One to three words joined by spacing/punctuation that must fold."""
    words = [draw(name_word()) for _ in range(draw(st.integers(1, 3)))]
    return draw(st.sampled_from(_SEPARATORS)).join(words)


class CanonicalEquivalenceTests(unittest.TestCase):
    @given(speaker_name())
    def test_canonical_re_encodings_share_one_key(self, value):
        expected = _identity_key(value)
        # NFKC is only a re-encoding while a character has no compatibility
        # decomposition. `①` and `ﬁ` are different spellings of the stored
        # name, not re-encodings of it, so they are outside this property.
        assume(unicodedata.normalize("NFKC", value) == unicodedata.normalize("NFC", value))
        for variant in (unicodedata.normalize("NFC", value),
                        unicodedata.normalize("NFD", value),
                        unicodedata.normalize("NFKC", value)):
            self.assertEqual(expected, _identity_key(variant), repr(variant))

    @given(speaker_name())
    def test_case_variants_share_one_key(self, value):
        expected = _identity_key(value)
        for variant in (value.lower(), value.upper(), value.casefold(),
                        value.title(), value.swapcase()):
            self.assertEqual(expected, _identity_key(variant), repr(variant))


class SeparatorTests(unittest.TestCase):
    @given(st.lists(st.sampled_from(_NAME_WORDS), min_size=2, max_size=3))
    def test_spacing_and_punctuation_do_not_change_the_key(self, words):
        # Every separator a speaker label has carried must fold: the same name
        # typed `ada-bram`, `ada. bram`, `ada'bram` or with an invisible
        # character is one identity. Comparing against the empty separator pins
        # that none of them survives in the key (the S5 mutant, which keeps
        # `-`, `.` and `'`, fails every separator but "" and " ").
        expected = _identity_key("".join(words))
        for separator in _SEPARATORS:
            self.assertEqual(expected, _identity_key(separator.join(words)),
                             repr(separator))


class OptionalDiacriticTests(unittest.TestCase):
    def test_fixture_diacritics_stay_decomposed_after_nfc(self):
        # The property below is only about marks NFC cannot compose with their
        # base. If a pair gained a precomposed form, the mark would turn into an
        # accented letter that the fold keeps (#2378) and the test would stop
        # exercising the optional-mark fold.
        for base, mark in _NON_COMPOSING_DIACRITICS:
            with self.subTest(base=base, mark=mark):
                self.assertIn(mark, unicodedata.normalize("NFC", base + mark))

    @given(st.sampled_from(_NON_COMPOSING_DIACRITICS),
           st.lists(st.sampled_from(_NAME_WORDS), max_size=2))
    def test_optional_diacritics_do_not_change_the_key(self, diacritic, others):
        # A diacritic that is still a combining mark is optional for Latin,
        # Greek, Cyrillic and Arabic names, so `q́` is the name `q`. The S4
        # mutant (keep every Mn/Mc mark) makes the two keys differ here while
        # every example-based test still passes.
        base, mark = diacritic
        without = " ".join(others + [base + "ra"])
        with_mark = " ".join(others + [base + mark + "ra"])
        self.assertEqual(_identity_key(without), _identity_key(with_mark))


class RequiredMarkTests(unittest.TestCase):
    @given(st.sampled_from(_REQUIRED_SYLLABLES),
           st.lists(st.sampled_from(_NAME_WORDS), max_size=2))
    def test_removing_a_required_mark_changes_the_key(self, syllable, others):
        # A vowel sign or virama is part of the letter in these scripts: a name
        # with it is a different name from the same name without it, which is
        # what stopped `राम`/`रमा` sharing a voice (#2347). #2358 is about nasal
        # signs only, so these marks are the stable ground to pin.
        base, mark = syllable
        with_mark = " ".join([base + mark] + list(others))
        without_mark = " ".join([base] + list(others))
        self.assertNotEqual(_identity_key(with_mark), _identity_key(without_mark))


class ProfileTests(unittest.TestCase):
    def test_this_modules_property_tests_use_the_bounded_profile(self):
        configured = hypothesis_profile.given_test_settings(sys.modules[__name__])
        expected = {
            "CanonicalEquivalenceTests.test_canonical_re_encodings_share_one_key",
            "CanonicalEquivalenceTests.test_case_variants_share_one_key",
            "SeparatorTests.test_spacing_and_punctuation_do_not_change_the_key",
            "OptionalDiacriticTests.test_optional_diacritics_do_not_change_the_key",
            "RequiredMarkTests.test_removing_a_required_mark_changes_the_key",
        }
        self.assertEqual(expected, set(configured))
        for name, active in configured.items():
            self.assertIs(hypothesis_profile.ACTIVE_SETTINGS, active, name)


if __name__ == "__main__":
    unittest.main()
