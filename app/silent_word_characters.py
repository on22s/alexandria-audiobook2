"""Zero-width characters that carry no lexical meaning (#2316).

A soft hyphen (U+00AD) is an optional line-break point inside a word; a word
joiner (U+2060) is the opposite, a no-break point. Neither is a word boundary
and neither is pronounceable, so both are deleted rather than replaced by a
space: a word containing U+00AD stays one word and ``a`` + U+2060 + ``b``
stays one token.

U+200B ZERO WIDTH SPACE is the opposite case: it *is* a word boundary. It is
not deleted, because that would fuse the two words it separates. It is mapped
to an ordinary space for tokenisation instead (``WORD_BOUNDARIES``).

Deliberately in neither set, because each has a lexical use:

- ZWNJ (``ZERO_WIDTH_NON_JOINER``) is orthographic in Persian: "می" + ZWNJ +
  "روم" is one word, so the word comparison keeps it inside the token. For
  counting how many copies the book holds, though, it is an optional
  separator: the same line may be written with a space or without it.
- ZWJ (``ZERO_WIDTH_JOINER``) is a glyph-joining hint with no lexical value;
  a Devanagari conjunct spelled with and without it is the same word, so
  comparison deletes it. ``literal_fidelity`` keeps both inside a token
  instead, because a literal word is verified byte for byte.

A byte-order mark (U+FEFF) is dropped at import rather than inside a word;
bidi controls (U+200E/F, U+202A-E, U+2066-9) carry direction; variation
selectors change the glyph.
"""

SOFT_HYPHEN = "\u00ad"
WORD_JOINER = "\u2060"
ZERO_WIDTH_SPACE = "\u200b"
ZERO_WIDTH_NON_JOINER = "\u200c"
ZERO_WIDTH_JOINER = "\u200d"
SILENT_WORD_CHARACTERS = (SOFT_HYPHEN, WORD_JOINER)
WORD_BOUNDARIES = (ZERO_WIDTH_SPACE,)

_SILENT_WORD_TRANSLATION = str.maketrans({char: None for char in SILENT_WORD_CHARACTERS})
_BOUNDARY_TRANSLATION = str.maketrans({char: " " for char in WORD_BOUNDARIES})


def remove_silent_word_characters(text):
    """Delete soft hyphens and word joiners, keeping the words they sit in."""
    if type(text) is not str:
        text = str(text or "")
    return text.translate(_SILENT_WORD_TRANSLATION)


def map_word_boundaries_to_spaces(text):
    """Replace U+200B with a space so it separates words instead of fusing them."""
    if type(text) is not str:
        text = str(text or "")
    return text.translate(_BOUNDARY_TRANSLATION)


def remove_silent_word_characters_with_offsets(text):
    """Delete the characters as above and report each survivor's raw offset.

    A comparison needs both views: the folded string to match, and the raw
    positions so a span found in the folded string still points into the
    untouched text (attribution proofs, transcript alignment).
    """
    if type(text) is not str:
        text = str(text or "")
    kept, offsets = [], []
    for index, character in enumerate(text):
        if character in SILENT_WORD_CHARACTERS:
            continue
        kept.append(character)
        offsets.append(index)
    return "".join(kept), offsets
