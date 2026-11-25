"""Zero-width characters that carry no lexical meaning (#2316).

A soft hyphen (U+00AD) is an optional line-break point inside a word; a word
joiner (U+2060) is the opposite, a no-break point. Neither is a word boundary
and neither is pronounceable, so both are deleted rather than replaced by a
space: a word containing U+00AD stays one word and ``a`` + U+2060 + ``b``
stays one token.

Deliberately not removed here: ZWNJ (U+200C) and ZWJ (U+200D) are lexical in
Persian, Indic scripts and emoji sequences; bidi controls (U+200E/F,
U+202A-E, U+2066-9) carry direction; variation selectors change the glyph.
"""

SOFT_HYPHEN = "\u00ad"
WORD_JOINER = "\u2060"
SILENT_WORD_CHARACTERS = (SOFT_HYPHEN, WORD_JOINER)

_SILENT_WORD_TRANSLATION = str.maketrans({char: None for char in SILENT_WORD_CHARACTERS})


def remove_silent_word_characters(text):
    """Delete soft hyphens and word joiners, keeping the words they sit in."""
    if type(text) is not str:
        text = str(text or "")
    return text.translate(_SILENT_WORD_TRANSLATION)
