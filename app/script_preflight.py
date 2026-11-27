"""Read-only, deterministic checks for annotated audiobook scripts."""

import difflib
import re
import shlex
import sys
import unicodedata
from bisect import bisect_left, bisect_right, insort
from collections import Counter
from functools import lru_cache
from heapq import heappop, heappush

from silent_word_characters import (
    SILENT_WORD_CHARACTERS, WORD_BOUNDARIES, ZERO_WIDTH_JOINER,
    ZERO_WIDTH_NON_JOINER, remove_silent_word_characters)
from speaker_identity import _normalize_speaker_label
from source_normalization import iter_source_paragraph_ranges
from speech_text import get_speech_normalization
from utils import get_unsafe_text_controls
from alexandria_alignment import is_same_number, is_spelled_number


_CYRILLIC_RE = re.compile(r"[\u0400-\u04ff]")
# `\w` includes UNDERSCORE, and Project Gutenberg marks italics with it:
# the source says "explain _myself_", so the token was "_myself_" and never
# matched the model's "myself". 23 of the 28 public-domain novels use the
# convention, so every one of them was being measured against a source whose
# emphasised words could not be matched.
#
# Alice in Wonderland was discarded over exactly this - all 919 entries, all
# 25 chunks generated - because a duplicated pair scored "not in the source"
# when the only difference was two underscores the model correctly dropped.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_FRONT_MATTER_RE = re.compile(
    r"\b(?:copyright|all rights reserved|isbn(?:-1[03])?|table of contents)\b",
    re.IGNORECASE,
)
_NARRATION_RE = re.compile(
    r"\b(?:he|she|they|subaru|emilia)\s+(?:said|asked|replied|thought|looked|felt|was|were)\b",
    re.IGNORECASE,
)
_SCRIPT_PREFIXES = ("LATIN", "CYRILLIC", "HIRAGANA", "KATAKANA", "CJK",
                    "ARABIC", "HEBREW", "HANGUL", "GREEK", "THAI")

# Two digit TOKENS can be two fragments of ONE numeral ("1 000", "5-6"), and
# the duplicate gate must not credit either fragment. Crossing a line is not
# joining a numeral, though: a digit that opens a line after a line that ended
# in a number is a new line, not the far half of a number (#2304 gate 3).
# \n, \r, U+2028 and U+2029 are the project's line endings; \x0b, \x0c, \x85
# and the three C0 information separators \x1c-\x1e break lines in every
# reader (and in Python's ``str.splitlines``) although the paragraph iterator
# treats them as horizontal space, so no numeral joins across any of them.
_NUMBER_GAP_LINE_BREAKS = "\n\r\u2028\u2029\x0b\x0c\x85\x1c\x1d\x1e"
# Range and clock marks join two digit tokens into one numeral ONLY when the
# mark touches both digits. A SPACED mark is sentence punctuation, not a range
# or a clock: "1914 — 5" is a year and a separate number, and "12: 5" is a
# count and a new sentence. A slash follows the same rule as every other mark:
# glued "5/6" is a ratio and joins, while a spaced "12 / 5" (a ratio or quoted
# verse) is punctuation. Glued hyphen, en dash, em dash, figure dash, minus,
# Gutenberg's double hyphen, ASCII tilde, wave dash, fullwidth tilde and slash
# are ranges; a glued colon is a clock. (#2304 gate 4 L1/L2, gate 5 L3 - one
# glued-only rule for every mark.)
_NUMERAL_GAP_MARKS = frozenset((
    "-", "--", "\u2013", "\u2014", "\u2012", "\u2212",
    "~", "\u301c", "\uff5e", "/", ":"))
# Whitespace that is NOT a line break: the same-line spacing the mark rule
# rejects. Kept as its own list so narrowing the line-break guard cannot turn
# a break into a horizontal space. Python's ``str.strip`` removes \x1f as
# whitespace too, so it is spacing here; leaving it out made a mark beside it
# look glued (#2304 gate 6 N1).
_HORIZONTAL_GAP_SPACES = frozenset(
    " \t\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008"
    "\u2009\u200a\u202f\u205f\u3000\x1f")


def _number_gap_line_breaks(gap):
    """The line-break sequences inside a gap, CRLF counted as one break.

    The characters are the project's line endings plus the vertical controls
    and C0 information separators readers break on (#2304 gate 3, gate 6 L1).
    ``\r\n`` is one ending even though it is two characters, so a single
    Windows wrap is one break and not the blank line a page number needs.
    """
    breaks = []
    index = 0
    while index < len(gap):
        if gap.startswith("\r\n", index):
            breaks.append("\r\n")
            index += 2
            continue
        if gap[index] in _NUMBER_GAP_LINE_BREAKS:
            breaks.append(gap[index])
        index += 1
    return breaks


def _raw_number_gap(source_text, comparison_text, start, stop, gap,
                    word_boundaries, offsets):
    """The untouched source gap when the comparison offsets still point at it.

    The comparison key maps a word boundary (U+200B) to an ordinary space, so
    an invisible boundary between a digit and its mark looks exactly like real
    spacing there. The raw slice decides it: ``5\\u200b-6`` is a glued range
    whose mark a reader sees touching the digit. Folding can change the source
    length anywhere in the book - before OR after this gap - so the comparison
    offsets are not source offsets; ``offsets`` (``_comparison_offset_map``)
    maps each one back. The slice is returned only when folding it reproduces
    the comparison gap exactly; otherwise the caller falls back to the
    comparison gap's spacing.
    """
    if source_text == comparison_text or offsets is None:
        return None
    raw = source_text[offsets[start]:offsets[stop]]
    if _comparison_text(raw, word_boundaries=word_boundaries) != gap:
        return None
    return raw


_COMBINING_MARK_WORD_PATTERN = None
# Apostrophe-like characters that are the same typography to a reader: ASCII
# apostrophe, curly single quotes, grave/acute accents and a modifier letter.
_APOSTROPHE_VARIANTS = "'\u2018\u2019\u0060\u00b4\u02bc"
_APOSTROPHE_FOLD = str.maketrans({character: "'" for character in _APOSTROPHE_VARIANTS})
_INTRAWORD_APOSTROPHE_RE = re.compile(r"(?<=\w)'(?=\w)")
_INTRAWORD_APOSTROPHE_VARIANT_RE = re.compile(
    rf"(?<=\w)[{re.escape(_APOSTROPHE_VARIANTS)}](?=\w)")
_FORMAT_TRANSLATIONS = None


def ensure_format_translations():
    """Build the two comparison views of Unicode format characters (category Cf).

    ONE definition of which Cf characters break a word and which only vanish
    (#2316's ``silent_word_characters``). ``WORD_BOUNDARIES`` (U+200B) is a word
    break: the "space" view maps it to a space, so "Go\\u200bhome" and "Go home"
    tokenise the same way, while the "fused" view deletes it, so the same source
    also matches "Gohome". ``SILENT_WORD_CHARACTERS`` (U+00AD, U+2060) are
    deleted in both views: they sit inside a word.

    ZWNJ (``ZERO_WIDTH_NON_JOINER``) is lexical (#2316), so it is kept inside
    the token rather than deleted; ZWJ (``ZERO_WIDTH_JOINER``) is a
    glyph-joining hint and is deleted, because a Devanagari conjunct spelled
    with and without it is the same word. The remaining Cf characters (BOM,
    bidi controls) are in neither of #2316's sets because they are
    directional. The duplicate key keeps its previous treatment of them -
    deletion, which joins, never breaks, a word - so this change is only
    about the boundary characters (#2293).

    Built once, from the Unicode database, because a per-character category
    lookup costs about 0.2 s per megabyte.
    """
    global _FORMAT_TRANSLATIONS
    if _FORMAT_TRANSLATIONS is None:
        spaced = {character: " " for character in WORD_BOUNDARIES}
        fused = {character: None for character in WORD_BOUNDARIES}
        for character in SILENT_WORD_CHARACTERS:
            spaced[character] = None
            fused[character] = None
        for character in (ZERO_WIDTH_JOINER,):
            spaced[character] = None
            fused[character] = None
        for code in range(0x110000):
            character = chr(code)
            if character in spaced or character in (
                    ZERO_WIDTH_NON_JOINER, ZERO_WIDTH_JOINER):
                continue
            if unicodedata.category(character) != "Cf":
                continue
            spaced[character] = None
            fused[character] = None
        _FORMAT_TRANSLATIONS = (str.maketrans(spaced), str.maketrans(fused))
    return _FORMAT_TRANSLATIONS


def remove_format_characters(text, *, word_boundaries="fused"):
    """Delete the Cf characters that vanish from a comparison key.

    ``word_boundaries="space"`` keeps a boundary character as a word break;
    "fused" deletes it so the two words it separated compare as one. ZWNJ is
    lexical and kept in both views. Comparison only; stored text, reported
    offsets and hashes are untouched.
    """
    spaced, fused = ensure_format_translations()
    return text.translate(spaced if word_boundaries == "space" else fused)


def ensure_combining_mark_word_pattern():
    """Return a regex that treats a combining mark as part of the word it follows.

    _WORD_RE cannot match a mark, so Thai "แล้ว" splits into "แล" and "ว"; the
    split then lands differently in source and output and a faithful re-split
    fuses differently. Built once from the Unicode database, because a
    per-character category lookup costs about 0.2 s per megabyte.
    """
    global _COMBINING_MARK_WORD_PATTERN
    if _COMBINING_MARK_WORD_PATTERN is None:
        ranges, start, previous = [], None, None
        for code in range(0x110000):
            if 0xD800 <= code <= 0xDFFF:
                continue
            if unicodedata.category(chr(code))[0] != "M":
                if start is not None:
                    ranges.append((start, previous))
                    start = previous = None
                continue
            if start is None:
                start = previous = code
            elif code == previous + 1:
                previous = code
            else:
                ranges.append((start, previous))
                start = previous = code
        if start is not None:
            ranges.append((start, previous))
        marks = "".join("\\U%08x" % first if first == last
                        else "\\U%08x-\\U%08x" % (first, last)
                        for first, last in ranges)
        _COMBINING_MARK_WORD_PATTERN = re.compile(
            r"[^\W_](?:[^\W_]|[%s]|%s)*"
            % (marks, re.escape(ZERO_WIDTH_NON_JOINER)), re.UNICODE)
    return _COMBINING_MARK_WORD_PATTERN


def _character_script(character):
    name = unicodedata.name(character, "")
    return next((prefix for prefix in _SCRIPT_PREFIXES if name.startswith(prefix)), None)


def get_unicode_scripts(text):
    """Read the Unicode script identities of this exact text snapshot."""
    return frozenset(script for char in str(text or "")
                     if (script := _character_script(char)))


def audit_unicode_text(text, source_text=None, *, source_scripts=None):
    """Describe Unicode scripts and source-unsupported characters without mutation."""
    text = str(text or "")
    scripts = sorted(get_unicode_scripts(text))
    if source_scripts is None:
        source_scripts = get_unicode_scripts(source_text)
    introduced = (sorted(set(scripts) - source_scripts) if source_text is not None
                  else sorted(set(scripts) - {"LATIN"}))
    controls = get_unsafe_text_controls(text)
    mixed = []
    for match in _WORD_RE.finditer(text):
        word_scripts = sorted(get_unicode_scripts(match.group()))
        if "LATIN" in word_scripts and len(word_scripts) > 1:
            mixed.append({"text": match.group(), "scripts": word_scripts,
                          "offset": match.start()})
    return {"normalization": "NFC", "is_nfc": text == unicodedata.normalize("NFC", text),
            "scripts": scripts, "introduced_scripts": introduced,
            "replacement_character_count": text.count("\ufffd"),
            "unsafe_controls": controls,
            "format_controls": sorted({f"U+{ord(char):04X}" for char in text
                                       if unicodedata.category(char) == "Cf"}),
            "mixed_script_words": mixed}


def is_possible_misattributed_narration(text, speaker):
    return (bool(speaker) and speaker.casefold() not in {"narrator", "narration"}
            and bool(_NARRATION_RE.search(text)))


def _normalize(value):
    # One order everywhere (#2316): delete the silent characters first, then
    # NFC, so a base letter and a combining mark on either side of one compose.
    return " ".join(unicodedata.normalize(
        "NFC", remove_silent_word_characters(value)).split()).casefold()


def get_normalized_word_tokens(value):
    """Read immutable word tokens using the existing preflight normalization."""
    return tuple(_WORD_RE.findall(_normalize(value)))


def _normalize_words(value):
    return " ".join(get_normalized_word_tokens(value))


def _comparison_text(text, *, word_boundaries="space"):
    # Transformation shared by the one comparison key. NFKC folds compatibility
    # typography ("ＯＫ" -> "OK"); Cf characters are invisible, so a
    # WORD_BOUNDARIES character is a word break in the default "space" view and
    # the fused view deletes it (#2316, #2293); every apostrophe variant folds
    # to ASCII before the intraword rule. Line endings stay intact here because
    # get_comparison_tokens_and_gaps decides from the project's paragraph
    # definition whether a break lets its neighbours fuse. Comparison only:
    # stored text, reported offsets and hashes are untouched.
    text = str(text or "")
    if text.isascii():
        if "`" in text:
            text = text.replace("`", "'")
        text = text.lower()
        if "'" in text:
            text = _INTRAWORD_APOSTROPHE_RE.sub("", text)
        return text
    text = remove_format_characters(
        unicodedata.normalize("NFKC", text.translate(_APOSTROPHE_FOLD)),
        word_boundaries=word_boundaries)
    # NFC before and after casefold: deleting a silent character can expose a
    # composable base+mark, and casefold itself can produce or decompose a mark
    # ("İ".casefold() == "i\u0307", Greek iota subscript), so NFC after folding
    # is what makes one word spelled two ways comparable.
    text = unicodedata.normalize(
        "NFC", unicodedata.normalize("NFC", text).casefold())
    if "'" in text:
        text = _INTRAWORD_APOSTROPHE_RE.sub("", text)
    return text


def _comparison_offset_map(source_text, comparison_text, word_boundaries):
    """Map each comparison-text offset back to the source offset it came from.

    Folding is not length-preserving: NFKC expands a ligature (``ﬁ`` ->
    ``fi``), casefold expands ``ß`` to ``ss``, and an intraword apostrophe is
    deleted, so a comparison offset is not a source offset. The map is built
    one character cluster at a time (a character with the combining marks that
    can compose with it) so NFC sees the same cluster the whole text did. It is
    accepted only when it reproduces the comparison text's length exactly;
    otherwise None is returned and the caller keeps the comparison spacing
    rather than risk a wrong slice. A character that composes only with a
    neighbour the cluster walk treats as a separate character - halfwidth
    katakana with a voiced mark U+FF9E/U+FF9F (Lm, not M) and conjoining Hangul
    jamo - is one such case: the map gives up (an under-block) rather than
    guess. Only the offsets have to be right: a context-dependent character
    fold (a word-final Greek sigma) maps to the right source position even when
    its character differs.
    """
    positions = []
    folded_parts = []
    index = 0
    length = len(source_text)
    while index < length:
        stop = index + 1
        # Only a non-ASCII character can be a combining mark; skip the
        # category lookup for the common ASCII text.
        while (stop < length and source_text[stop] > "\x7f"
               and (unicodedata.combining(source_text[stop])
                    or unicodedata.category(source_text[stop]).startswith("M"))):
            stop += 1
        folded = _folded_comparison_cluster(source_text[index:stop],
                                            word_boundaries)
        folded_parts.append(folded)
        positions.extend([index] * len(folded))
        index = stop
    folded_text = "".join(folded_parts)
    if len(folded_text) != len(comparison_text):
        # The intraword apostrophe is removed only after folding, so a
        # per-cluster fold keeps one that the whole text deleted.
        removed = {match.start()
                   for match in _INTRAWORD_APOSTROPHE_RE.finditer(folded_text)}
        if not removed or len(folded_text) - len(removed) != len(comparison_text):
            return None
        positions = [position for offset, position in enumerate(positions)
                     if offset not in removed]
    if len(positions) != len(comparison_text):
        return None
    return positions + [len(source_text)]


@lru_cache(maxsize=8192)
def _folded_comparison_cluster(cluster, word_boundaries):
    """One cluster's fold, cached: a book repeats few distinct clusters."""
    return _comparison_text(cluster, word_boundaries=word_boundaries)


def _comparison_word_pattern(text):
    # _WORD_RE cannot match a mark, so ASCII keeps it. A mark belongs to the
    # word it follows, so a mark inside a word never moves a token boundary.
    # Marks stay in the token, not dropped: Thai ก่า and ก้า are different
    # words, as are pointed and unpointed Hebrew.
    return _WORD_RE if text.isascii() else ensure_combining_mark_word_pattern()


def get_comparison_word_view(text, *, word_boundaries="space"):
    """THE comparison key; the duplicate gate and word coverage both use it."""
    text = _comparison_text(text, word_boundaries=word_boundaries)
    return " ".join(_comparison_word_pattern(text).findall(text))


def _gap_is_one_paragraph(gap):
    """Whether a whitespace gap between two tokens stays inside one paragraph.

    The project's paragraph iterator is the only definition: a gap that holds a
    paragraph break (a blank line or U+2029) becomes two spans once a word
    follows it, while a soft wrap stays one. A gap of ASCII spaces cannot hold
    a break - a paragraph break needs a line ending - so the common case never
    calls the iterator.

    An invisible character does not make a blank line a wrap. ZWNJ is kept in
    the comparison key while every other Cf character has already been deleted,
    so a line holding only one of them used to be a wrap in one spelling and a
    paragraph break in the other. Cf characters are not whitespace to the
    iterator, so they are removed before the question is asked: a blank line is
    blank whatever invisible character it carries.
    """
    if not gap.strip(" "):
        return True
    visible = gap if gap.isascii() else "".join(
        character for character in gap
        if unicodedata.category(character) != "Cf")
    return len(list(iter_source_paragraph_ranges(visible + "x"))) == 1


def get_comparison_tokens_and_gaps(text, *, word_boundaries="space",
                                   comparison_text=None, token_spans=None):
    """Comparison-key tokens and whether each gap may fuse its two neighbours.

    ``token_spans``, when given, receives each token's ``(start, end)`` offset
    in the comparison text, so a caller can ask what characters sit beside a
    token (the number rule's whole-numeral test needs exactly that).

    A gap is fusable only when it holds whitespace (or a ZWNJ, treated as an
    optional separator) and no paragraph break: punctuation keeps two lines
    apart even though the key drops the punctuation itself, and the words on
    either side of a paragraph break are separate lines. The break decision is
    the project's ONE paragraph definition, ``iter_source_paragraph_ranges``: a
    single line ending (\\n, \\r\\n, \\r, U+2028) is a soft wrap and its words
    may fuse for capacity, while a blank line or U+2029 ends a paragraph and
    never fuses (#2293). Other Unicode whitespace (\\x0b, \\x0c, \\x1c-\\x1e,
    \\x85) is not a line ending to that definition, so it is treated like the
    horizontal space it is - but when one of them sits between two line
    endings, the gap is a blank line and does not fuse.
    """
    text = (_comparison_text(text, word_boundaries=word_boundaries)
            if comparison_text is None else comparison_text)
    tokens, fusable = [], []
    previous_end = 0
    for match in _comparison_word_pattern(text).finditer(text):
        if tokens:
            gap = text[previous_end:match.start()]
            fusable.append(all(
                character.isspace() or character == ZERO_WIDTH_NON_JOINER
                for character in gap) and _gap_is_one_paragraph(gap))
        else:
            fusable.append(False)
        tokens.append(match.group())
        if token_spans is not None:
            token_spans.append(match.span())
        previous_end = match.end()
    return tokens, fusable


def _build_boundary_insensitive_matcher(source_text, *, comparison_text=None):
    tokens, fusable = get_comparison_tokens_and_gaps(
        source_text, comparison_text=comparison_text)
    fused_parts = []
    start_to_index = {}
    end_to_index = {}
    bad_boundaries = []
    offset = 0
    for index, token in enumerate(tokens):
        if index and not fusable[index]:
            bad_boundaries.append(offset)
        stripped = token.replace(ZERO_WIDTH_NON_JOINER, "")
        start_to_index[offset] = index
        fused_parts.append(stripped)
        offset += len(stripped)
        end_to_index[offset] = index + 1
    fused_source = "".join(fused_parts)

    def match_occurrences(phrase):
        target = "".join(phrase.split()).replace(ZERO_WIDTH_NON_JOINER, "")
        if not target:
            return 0, Counter(), set()
        count = 0
        whole_tokens = Counter()
        claimed_indices = set()
        search_from = 0
        while True:
            # The fused source is scanned in C; Python only validates a hit's
            # two ends and the punctuation/line-break gaps inside it.
            start = fused_source.find(target, search_from)
            if start < 0:
                return count, whole_tokens, claimed_indices
            stop = start + len(target)
            boundary = bisect_right(bad_boundaries, start)
            tok_start = start_to_index.get(start)
            tok_stop = end_to_index.get(stop)
            if (tok_start is not None and tok_stop is not None
                    and (boundary == len(bad_boundaries)
                         or bad_boundaries[boundary] >= stop)):
                count += 1
                if tok_stop - tok_start == 1:
                    whole_tokens[tokens[tok_start]] += 1
                claimed_indices.update(range(tok_start, tok_stop))
                search_from = stop
            else:
                search_from = start + 1

    return match_occurrences


def get_boundary_insensitive_counter(source_text):
    """Count source copies of a line however its word boundaries are spelled.

    ONE rule: a line is the same line with a space, with U+200B, or fused, so a
    copy is counted when consecutive source tokens concatenate to the phrase
    and both ends sit on token boundaries. Tokens fuse across whitespace within
    a paragraph - including a soft-wrapped line ending - but punctuation and a
    paragraph break (a blank line or U+2029) still separate lines, and copies
    may not overlap, so capacity can never exceed the number of source copies
    the book actually carries. ZWNJ is an optional separator here: the primary
    key keeps it inside its word, but a line written with "می" + ZWNJ + "روم"
    and one written "میروم" (or "می روم") are the same line for capacity (#2293).
    """
    match_occurrences = _build_boundary_insensitive_matcher(source_text)
    return lambda phrase: match_occurrences(phrase)[0]


def _finding(severity, code, message, entry_numbers=None, **details):
    result = {"severity": severity, "code": code, "message": message}
    if entry_numbers:
        result["entry_numbers"] = entry_numbers
    if details:
        result["details"] = details
    return result


# ONE definition of how much decode damage a source may carry, used by every
# path that generates a script. Before this existed the same question had three
# different answers: generate_script graded it, three_pass_generate refused any
# count, and the per-entry audit blocked at any count - so a repaired book
# generated single-pass and was refused three-pass, for the same input.
#
# 0.5% sits between index18 corrupt (1.40%) and index18 after deterministic
# repair (0.26%), so a mis-decoded file is still rejected and a repaired one
# runs. Raising it is a decision about accepting damaged prose, and belongs
# here where every caller sees the same number.
MAX_REPLACEMENT_SHARE = 0.005


def replacement_repair_hint(source_path=None):
    """The exact command that fixes a mis-decoded source.

    A refusal that only states a percentage leaves the reader to discover that
    a repairer exists. Both generators refuse for the same reason, so they say
    the same thing, from here.
    """
    target = source_path or "<source.txt>"
    shell_hint = ""
    command = "cd app && env/bin/python repair_source_encoding.py --apply -- " + shlex.quote(str(target))
    if sys.platform == "win32":
        shell_hint = " in PowerShell"
        executable = "'" + sys.executable.replace("'", "''") + "'"
        argument = "'" + str(target).replace("'", "''") + "'"
        command = f"cd app; & {executable} repair_source_encoding.py --apply -- {argument}"
    return (
        "This is a decoding error, not lost content: the file was read with "
        "the wrong codec and every non-ASCII character became U+FFFD. Repair "
        f"it with{shell_hint}\n"
        f"    {command}\n"
        "which writes <source>.repaired.txt beside the original, leaves the "
        "original untouched, and reports every substitution. It refuses to "
        "write if a substitution would break sentence structure."
    )


def replacement_load_is_acceptable(count, length):
    """True when a text's replacement characters are within policy.

    Callers must not re-derive this. A gate that answers the same question a
    second time is a gate that will eventually answer it differently.
    """
    if not count:
        return True
    return (count / max(1, length)) <= MAX_REPLACEMENT_SHARE


def get_source_phrase_occurrences(source_normalized, phrase_normalized):
    """Count literal whole-token phrases in word-normalized source text."""
    if not source_normalized or not phrase_normalized:
        return 0
    pattern = r"(?<!\S)" + re.escape(phrase_normalized) + r"(?!\S)"
    return sum(1 for _match in re.finditer(pattern, source_normalized))


def get_source_phrase_counter(source_normalized):
    """Return a counter bound to this immutable source; index only when queried."""
    positions = None

    def ensure_occurrence_count(phrase_normalized):
        nonlocal positions
        if not source_normalized or not phrase_normalized:
            return 0
        if phrase_normalized != " ".join(phrase_normalized.split()):
            return get_source_phrase_occurrences(source_normalized, phrase_normalized)
        if positions is None:
            positions = {}
            for match in re.finditer(r"\S+", source_normalized):
                positions.setdefault(match.group(), []).append(match.start())
        count = 0
        next_start = 0
        first_word = phrase_normalized.split(" ", 1)[0]
        for start in positions.get(first_word, []):
            end = start + len(phrase_normalized)
            if (start >= next_start and source_normalized.startswith(phrase_normalized, start)
                    and (end == len(source_normalized) or source_normalized[end].isspace())):
                count += 1
                next_start = end
        return count

    return ensure_occurrence_count


def source_occurrences_for_text(source_normalized, text_normalized,
                                minimum_tokens=5, *, count_phrase=None):
    """How many times the source carries this entry's content.

    WHY NOT A PLAIN `count`. Two things break exact matching, and both are
    normal rather than defects:

      * Entries are not contiguous spans - narration sits between two spoken
        lines, so joining them finds nothing.
      * The model modernises archaic orthography. Carroll writes "ca'n't";
        the model emits "can't", so Alice's line matches her own book zero
        times while being an obviously faithful rendering.

    Exact matching therefore reports 0 - "the model invented this" - for text
    that is plainly in the book. Alice in Wonderland generated all 25 chunks
    and was discarded, all 919 entries, over two lines that differ from the
    source by one apostrophe.

    So: use the LONGEST window of the entry that the source actually
    contains, and report how often that window occurs. Longest is the most
    distinctive, which keeps the count meaningful - a short common phrase
    would appear everywhere and make a duplication look faithful.

    Entries shorter than `minimum_tokens` require their complete exact phrase;
    longer entries require a matching window of at least `minimum_tokens` words.
    """
    count_phrase = count_phrase or get_source_phrase_counter(source_normalized)
    tokens = text_normalized.split()
    if not tokens or not source_normalized:
        return 0
    if len(tokens) < minimum_tokens:
        return count_phrase(" ".join(tokens))
    for size in range(len(tokens), minimum_tokens - 1, -1):
        best = 0
        for start in range(0, len(tokens) - size + 1):
            window = " ".join(tokens[start:start + size])
            best = max(best, count_phrase(window))
        if best:
            return best
    return 0


def get_source_block_occurrences(source_normalized, block, *, count_phrase=None):
    """Count contiguous support, then require support for every emitted line."""
    count_phrase = count_phrase or get_source_phrase_counter(source_normalized)
    contiguous = count_phrase(_normalize_words(" ".join(block)))
    if source_normalized and not contiguous:
        return min((source_occurrences_for_text(
            source_normalized, _normalize_words(text), count_phrase=count_phrase)
                    for text in block), default=0)
    return contiguous


def _build_number_spelling_digit_index(tokens, whole_number):
    """Index whole digit tokens by the words beside them.

    A source window that spells a needle with one digit differs at exactly that
    token: every other token matches. So its start is the digit's position
    minus the needle's offset, and the digit sits beside the needle words that
    surround it. Keyed by one neighbour (a digit at the window's first or last
    position) or both (an interior digit), this derives candidate starts in time
    proportional to the needle's spelled positions instead of scanning every
    position of its first word (#2304 P1). Acceptance still walks every indexed
    digit beside a matching needle word, so it also grows with how common that
    neighbour word is in the book. The candidate set is identical to
    "every source start plus every digit": an accepted window contains exactly
    one digit, at a needle word that ``is_spelled_number`` accepts, so the
    index yields it and adds nothing that can pass the exact-window check.
    """
    by_next, by_previous, by_pair = {}, {}, {}
    for index, token in enumerate(tokens):
        if not token.isdigit() or not whole_number[index]:
            continue
        previous = tokens[index - 1] if index else None
        following = tokens[index + 1] if index + 1 < len(tokens) else None
        if following is not None:
            by_next.setdefault(following, []).append(index)
        if previous is not None:
            by_previous.setdefault(previous, []).append(index)
        if previous is not None and following is not None:
            by_pair.setdefault((previous, following), []).append(index)
    return by_next, by_previous, by_pair


def _number_spelling_digit_lines(tokens, fusable, words, digit_index):
    """Non-overlapping source windows that spell ``words`` with one whole digit.

    ``words`` must carry no digit: the number rule only asks for a digit-free
    needle, so the one difference is always the source digit. Ordering and the
    non-overlap rule are the caller's: starts are taken in source order, and an
    accepted window consumes its span so copies cannot overlap.
    """
    lines = Counter()
    if len(words) < 2:
        return lines
    last = len(words) - 1
    by_next, by_previous, by_pair = digit_index
    starts = set()
    # Only a spelled-number word can be the one difference. Asking for its
    # neighbours' digit positions keeps the work independent of how often the
    # needle's first word occurs in the book.
    for position, word in enumerate(words):
        if not is_spelled_number([word]):
            continue
        if position == 0:
            starts.update(by_next.get(words[1], ()))
        elif position == last:
            starts.update(index - position for index
                          in by_previous.get(words[position - 1], ()))
        else:
            starts.update(index - position for index in by_pair.get(
                (words[position - 1], words[position + 1]), ()))
    next_free = 0
    span = len(words)
    for start in sorted(starts):
        if start < next_free or start + span > len(tokens):
            continue
        window = tokens[start:start + span]
        differences = [index for index, (left, right)
                       in enumerate(zip(words, window)) if left != right]
        if (len(differences) != 1
                or not all(fusable[position]
                           for position in range(start + 1, start + span))):
            continue
        digits = window[differences[0]]
        spelled = words[differences[0]]
        if (digits.isdigit() and is_spelled_number([spelled])
                and is_same_number([spelled], [digits])):
            next_free = start + span
            lines[" ".join(window)] += 1
    return lines


def find_contained_source_duplicates(entries, source_text, *, source_normalized=None):
    """Reject proven excess quotation copies; never delete or trust greedy mapping.

    Compare distinct whole-token source occurrences with quoted occurrences in
    larger entries plus separate character entries. Plain narrative rendering
    can introduce words, so other containment is review-only. Known expansions
    add conservative capacity; unknown number/sound rendering cannot prove loss.
    Span coordinates below are word-normalized, not raw source offsets.
    """
    from dialogue_spans import spoken_spans

    canonical = get_comparison_word_view

    source_text = str(source_text or "")
    source_is_ascii = source_text.isascii()
    # A boundary character is non-ASCII, so only a non-ASCII source can carry
    # one; an ASCII source keeps the old single key (and the old output key).
    source_has_boundaries = (
        not source_is_ascii
        and any(character in source_text for character in WORD_BOUNDARIES))
    # When the source treats a boundary character as a word break, an output
    # that keeps it does too. Otherwise the output keeps the previous treatment
    # of deleting it, so an ASCII source's behaviour is unchanged. (#2293)
    output_boundaries = "space" if source_has_boundaries else "fused"
    if len(source_text) + sum(len(e.get("text", "")) for e in entries
                              if isinstance(e, dict) and isinstance(e.get("text"), str)) > 4000000:
        return [_finding("manual_review", "contained_duplicate_budget_exhausted",
                         "Quotation provenance input exceeded its size budget; review without deleting text.",
                         [], character_budget=4000000)]
    texts = [canonical(e.get("text"), word_boundaries=output_boundaries)
             if isinstance(e, dict) else "" for e in entries]
    character_rows = {i for i, e in enumerate(entries) if isinstance(e, dict)
                      and str(e.get("speaker") or e.get("type") or "").casefold()
                      not in {"", "narrator", "narration"}}
    quoted = [[canonical(e["text"][a:b], word_boundaries=output_boundaries)
               for a, b in spoken_spans(e["text"], "paired_quotes")]
              if isinstance(e, dict) and isinstance(e.get("text"), str) else [] for e in entries]
    # Token Aho-Corasick: scan each source/output region once, not once per
    # character phrase. Suffix matches have a fixed budget; exhaustion abstains.
    from collections import deque
    needles = list(dict.fromkeys(texts[i] for i in sorted(character_rows) if texts[i]))
    if not needles:
        return []
    # A line may be spelled more than one way in the source itself ("the cat"
    # and "thecat" in one book), so no single source key sees every copy. The
    # spelling-independent matcher is built lazily on the first needle that
    # exceeds its per-view capacity, so clean books pay nothing for it.
    match_boundary_copies = None
    needle_starts = {needle.split()[0] for needle in needles}
    # The audit word view was tokenized with the mark-dropping _WORD_RE, so it
    # is only trustworthy for an ASCII source with no apostrophe variant: there
    # casefold cannot add a mark, NFKC cannot change an ASCII character, and the
    # apostrophe guard covers the one other difference. Any other source is
    # rebuilt through canonical so a mark casefold produced ("İ" -> "i\u0307")
    # or typography is not dropped from the source while the needle keeps it.
    source_comparison_text = None
    if (source_normalized is not None
            and source_is_ascii
            and not _INTRAWORD_APOSTROPHE_VARIANT_RE.search(source_text)):
        source = source_normalized
    else:
        source_comparison_text = _comparison_text(source_text)
        source = " ".join(
            _comparison_word_pattern(source_comparison_text).findall(
                source_comparison_text))
    # A boundary character may be rendered either way by the model: the source
    # "Go\u200bhome" is the same single line as "Go home" and as "Gohome". Count
    # support under both keys and, per needle, keep the larger count as its
    # capacity, so a duplicate is proven whichever spelling was written and a
    # faithful single copy stays clean. A source without a boundary character
    # has one key and skips the extra scan entirely.
    source_keys = [source]
    if source_has_boundaries:
        fused_source = canonical(source_text, word_boundaries="fused")
        if fused_source != source:
            source_keys.append(fused_source)
    unspent_counts = output_words = output_positions = run_groups = total_runs = None
    run_begins = max_run_words = output_word_counts = None
    out_flat = output_starts = offset_to_word = None
    source_tokens = source_positions = None
    digit_words_unemitted = None
    number_line_view = None

    def ensure_unspent():
        """Tokenize the output once and index every unemitted-source-word run.

        A run is a span of consecutive output words whose concatenation equals
        an unemitted source word exactly ("somit" explains "mit" only where
        "so" and "mit" are adjacent). Only a word that is a prefix of an
        unemitted token can begin a run, so an output that never re-splits a
        source word does no per-token work. For a startable word the candidates
        are the sorted unemitted tokens carrying it as a prefix; each is a run
        when the characters between its word boundaries equal it. The
        concatenation is tested as one string slice, never grown word by word,
        so a very long unspaced token stays a single comparison instead of a
        quadratic walk.
        """
        nonlocal unspent_counts, output_words, output_positions, run_groups, total_runs
        nonlocal run_begins, max_run_words, output_word_counts
        nonlocal out_flat, output_starts, offset_to_word
        if unspent_counts is not None:
            return
        output_words = " ".join(texts).split()
        output_word_counts = Counter(output_words)
        unspent_counts = Counter(source.split()) - output_word_counts
        output_positions = {}
        for position, word in enumerate(output_words):
            if word in needle_starts:
                output_positions.setdefault(word, []).append(position)
        tokens = sorted(unspent_counts)
        run_groups, total_runs, max_run_words = [], 0, 0
        if not tokens:
            return
        prefix_tokens = {}

        def tokens_with_prefix(word):
            found = prefix_tokens.get(word)
            if found is None:
                found = []
                index = bisect_left(tokens, word)
                while index < len(tokens) and tokens[index].startswith(word):
                    found.append(tokens[index])
                    index += 1
                prefix_tokens[word] = found
            return found

        startable = {word for word in output_word_counts if tokens_with_prefix(word)}
        if not startable:
            return
        output_starts = [0]
        total = 0
        for word in output_words:
            total += len(word)
            output_starts.append(total)
        offset_to_word = {offset: index for index, offset in enumerate(output_starts)}
        out_flat = "".join(output_words)
        for begin, word in enumerate(output_words):
            if word not in startable:
                continue
            start = output_starts[begin]
            remaining = len(out_flat) - start
            row = []
            for token in tokens_with_prefix(word):
                size = len(token)
                if size > remaining:
                    continue
                stop = offset_to_word.get(start + size)
                if stop is None or stop - begin < 2:
                    continue
                if out_flat.startswith(token, start):
                    row.append((stop, token))
            if row:
                row.sort()
                run_groups.append((begin, row))
                total_runs += len(row)
                max_run_words = max(max_run_words, row[-1][0] - begin)
        run_begins = [begin for begin, _row in run_groups]

    def ensure_source_tokens():
        nonlocal source_tokens, source_positions
        if source_tokens is not None:
            return
        source_tokens = source.split()
        source_positions = {}
        for index, token in enumerate(source_tokens):
            source_positions.setdefault(token, []).append(index)

    def expand_run(begin, stop, index, occurrence_start, occurrence_stop):
        """Render one occurrence through consecutive source tokens.

        The run pins output words [begin, stop) to source token `index`. Each
        further unit consumes whole output words equal to the next source token
        exactly; a unit of two or more words is a fused token the word scan
        cannot see. A needle may straddle two fused tokens or a fused token and
        a whole word, so the window grows from the first touched unit to cover
        the occurrence. Returns (start, stop, fused tokens, fused spans, unit
        boundaries) or None when the surrounding output does not reproduce the
        source tokens in order.
        """
        start_word, stop_word = begin, stop
        left_index = right_index = index
        fused, spans, units = [], [], [(begin, stop, index)]
        if stop - begin >= 2:
            fused.append(source_tokens[index])
            spans.append((begin, stop))
        while start_word > occurrence_start:
            if left_index == 0:
                return None
            target = source_tokens[left_index - 1]
            end = output_starts[start_word]
            previous = offset_to_word.get(end - len(target))
            if previous is None or previous >= start_word:
                return None
            if not out_flat.startswith(target, output_starts[previous]):
                return None
            left_index -= 1
            units.append((previous, start_word, left_index))
            if start_word - previous >= 2:
                fused.append(target)
                spans.append((previous, start_word))
            start_word = previous
        while stop_word < occurrence_stop:
            if right_index + 1 >= len(source_tokens):
                return None
            target = source_tokens[right_index + 1]
            start = output_starts[stop_word]
            following = offset_to_word.get(start + len(target))
            if following is None or following <= stop_word:
                return None
            if not out_flat.startswith(target, start):
                return None
            right_index += 1
            units.append((stop_word, following, right_index))
            if following - stop_word >= 2:
                fused.append(target)
                spans.append((stop_word, following))
            stop_word = following
        units.sort()
        return start_word, stop_word, tuple(fused), tuple(spans), units

    def unspent_token_support(needle, *, counted_whole_tokens=None,
                              boundary_claimed=None):
        """Copies of a needle that an unemitted source word explains.

        A needle fused into a longer source word is invisible to the word scan.
        An unemitted source word explains the needle only where consecutive
        output words concatenate to it exactly ("somit" explains "mit" beside
        "so" and nothing else); a word emitted elsewhere cannot. A whole-word
        occurrence may also straddle two unemitted tokens, or an unemitted token
        and a whole word: the window covering it must reproduce a consecutive
        source-token sequence, with every fused token rendered by adjacent
        output words and every whole word by itself. Windows may not overlap and
        each unemitted source copy is used once, so reuse cannot hide a genuine
        repeat. Tokens already counted into capacity by the spelling-independent
        counter are excluded so a fused whole-token copy is never counted twice.
        """
        ensure_unspent()
        if not unspent_counts or not run_groups:
            return 0
        available_counts = (unspent_counts - counted_whole_tokens
                            if counted_whole_tokens else unspent_counts)
        if not available_counts:
            return 0
        words = output_words
        phrase = needle.split()
        length = len(phrase)
        occurrences, last = [], 0
        for position in output_positions.get(phrase[0], ()):
            if position >= last and words[position:position + length] == phrase:
                occurrences.append(position)
                last = position + length
        if not occurrences:
            return 0
        ends = [position + length for position in occurrences]
        # Each run is worth the occurrences fully inside it. A dense occurrence
        # array costs two lookups per run; when occurrences are sparse, two
        # bisects are cheaper than building one. Choose per needle.
        if total_runs * 4 > len(words):
            ends_at = [0] * (len(words) + 2)
            for end in ends:
                ends_at[end] += 1
            for position in range(1, len(ends_at)):
                ends_at[position] += ends_at[position - 1]

            def value_of(begin, stop):
                return ends_at[stop] - ends_at[begin + length - 1]
        else:
            def value_of(begin, stop):
                return (bisect_right(ends, stop)
                        - bisect_right(ends, begin + length - 1))
        raw = []
        # Only a run whose span covers an occurrence can be worth anything. A
        # run is at most max_run_words long, so its begin lies in this window;
        # bisecting the sorted begins keeps a many-needle book linear in the
        # runs near each needle, not in the whole book.
        lo = bisect_left(run_begins, occurrences[0] - max_run_words)
        hi = bisect_right(run_begins, occurrences[-1] + length - 1)
        for index in range(lo, hi):
            begin, row = run_groups[index]
            for stop, token in row:
                if not available_counts[token]:
                    continue
                value = value_of(begin, stop)
                if value:
                    raw.append((begin, stop, (token,), value))
        if length >= 2:
            # A multi-word occurrence can straddle a source token boundary, where
            # no single run contains it. A run can also be straddled on both
            # sides at once, so one fused copy explains the occurrence before it
            # and the occurrence after it. Grow one window from each run that is
            # straddled, covering every such occurrence, and keep the windows
            # that reproduce consecutive source tokens.
            windows = {}
            claimed = None
            for index in range(lo, hi):
                begin, row = run_groups[index]
                for stop, token in row:
                    if not available_counts[token]:
                        continue
                    first = bisect_right(ends, begin)
                    after = bisect_left(occurrences, stop)
                    cover_start = cover_stop = None
                    for other in range(first, after):
                        other_start, other_end = occurrences[other], ends[other]
                        if begin <= other_start and other_end <= stop:
                            continue
                        if cover_start is None or other_start < cover_start:
                            cover_start = other_start
                        if cover_stop is None or other_end > cover_stop:
                            cover_stop = other_end
                    if cover_start is None:
                        continue
                    if source_tokens is None:
                        ensure_source_tokens()
                    if claimed is None:
                        # Source copies already counted into capacity (either
                        # by the primary-key scan or by the spelling-independent
                        # counter when it raised capacity) must not be credited
                        # a second time through a straddle window.
                        if boundary_claimed is not None:
                            claimed = set(boundary_claimed)
                        else:
                            claimed = set()
                            source_last = 0
                            for source_start in source_positions.get(phrase[0], ()):
                                if (source_start >= source_last
                                        and source_tokens[source_start:source_start + length] == phrase):
                                    claimed.update(range(source_start, source_start + length))
                                    source_last = source_start + length
                    for position in source_positions.get(token, ()):
                        expanded = expand_run(begin, stop, position,
                                              cover_start, cover_stop)
                        if expanded is None:
                            continue
                        window_start, window_stop, fused, spans, units = expanded
                        if not fused or (window_start, window_stop, fused) in windows:
                            continue
                        value = 0
                        window_low = bisect_left(occurrences, window_start)
                        window_high = bisect_right(occurrences, window_stop - length)
                        for other in range(window_low, window_high):
                            other_start, other_end = occurrences[other], ends[other]
                            if not any(unit_start < other_end and unit_stop > other_start
                                       for unit_start, unit_stop in spans):
                                continue
                            touched = {source_index for unit_start, unit_stop, source_index
                                       in units
                                       if unit_start < other_end and unit_stop > other_start}
                            if touched & claimed:
                                continue
                            value += 1
                        if value:
                            windows[(window_start, window_stop, fused)] = value
            for (begin, stop, fused), value in windows.items():
                raw.append((begin, stop, fused, value))
        raw.sort(key=lambda item: (item[0], item[1]))
        candidates, groups = [], []
        for item in raw:
            if groups and groups[-1][0] == item[0]:
                groups[-1][1].append(len(candidates))
            else:
                groups.append((item[0], [len(candidates)]))
            candidates.append(item)

        def tokens_fit(usage, tokens):
            return all(usage[token] + count <= available_counts[token]
                       for token, count in Counter(tokens).items())

        def add_usage(usage, tokens):
            for token, count in Counter(tokens).items():
                usage[token] += count

        def exact():
            suffix = [0] * (len(candidates) + 1)
            for index in range(len(candidates) - 1, -1, -1):
                suffix[index] = suffix[index + 1] + candidates[index][3]
            best = 0

            def search(index, next_free, usage, covered):
                nonlocal best
                if covered + suffix[index] <= best:
                    return
                if covered > best:
                    best = covered
                if index == len(candidates):
                    return
                begin, stop, tokens, value = candidates[index]
                if begin >= next_free and tokens_fit(usage, tokens):
                    add_usage(usage, tokens)
                    search(index + 1, stop, usage, covered + value)
                    for token, count in Counter(tokens).items():
                        usage[token] -= count
                search(index + 1, next_free, usage, covered)

            search(0, 0, Counter(), 0)
            return best

        if len(candidates) <= 16:
            return exact()

        def solve(excluded_indices=frozenset(), excluded_tokens=frozenset()):
            """Best coverage ignoring token counts, and the runs that give it.

            Candidates arrive in position order, so pushing each to its stop and
            draining a stop-ordered heap answers "best coverage ending at or
            before this word" without sorting every candidate.
            """
            heap, best_before, pick_before = [], 0, -1
            predecessor = {}
            for begin, group in groups:
                while heap and heap[0][0] <= begin:
                    _stop, covered, index = heappop(heap)
                    if covered > best_before:
                        best_before, pick_before = covered, index
                for index in group:
                    if index in excluded_indices or any(
                            token in excluded_tokens for token in candidates[index][2]):
                        continue
                    predecessor[index] = pick_before
                    heappush(heap, (candidates[index][1],
                                    best_before + candidates[index][3], index))
            best, pick = 0, -1
            while heap:
                _stop, covered, index = heappop(heap)
                if covered > best:
                    best, pick = covered, index
            chain = []
            while pick != -1:
                chain.append(pick)
                pick = predecessor[pick]
            return best, chain

        def over_used(chain):
            usage = Counter()
            for index in chain:
                usage.update(candidates[index][2])
            return {token for token, count in usage.items()
                    if count > available_counts[token]}

        total, chain = solve()
        excess = over_used(chain)
        if not excess:
            return total
        best_valid = 0
        # Dropping copies from the DP selection leaves a valid, disjoint set.
        kept, remaining = [], Counter()
        for index in sorted(chain, key=lambda index: -candidates[index][3]):
            tokens = candidates[index][2]
            if tokens_fit(remaining, tokens):
                add_usage(remaining, tokens)
                kept.append(index)
        best_valid = sum(candidates[index][3] for index in kept)
        # A greedy in position order: longest run first at each free position.
        next_free, usage, covered = 0, Counter(), 0
        for begin, group in groups:
            if begin < next_free:
                continue
            chosen = None
            for index in group:
                if tokens_fit(usage, candidates[index][2]):
                    chosen = index
            if chosen is None:
                continue
            add_usage(usage, candidates[chosen][2])
            covered += candidates[chosen][3]
            next_free = candidates[chosen][1]
        best_valid = max(best_valid, covered)
        if len(candidates) <= 200000:
            # A greedy by descending coverage, latest position first.
            buckets = {}
            for index, (_begin, _stop, _tokens, value) in enumerate(candidates):
                buckets.setdefault(value, []).append(index)
            chosen_stops, usage, covered = [], Counter(), 0
            for value in sorted(buckets, reverse=True):
                for index in reversed(buckets[value]):
                    begin, stop, tokens, _value = candidates[index]
                    if not tokens_fit(usage, tokens):
                        continue
                    slot = bisect_left(chosen_stops, (begin, -1))
                    if (slot and chosen_stops[slot - 1][1] > begin) or (
                            slot < len(chosen_stops) and chosen_stops[slot][0] < stop):
                        continue
                    add_usage(usage, tokens)
                    insort(chosen_stops, (begin, stop))
                    covered += value
            best_valid = max(best_valid, covered)
            # Retry the DP without one over-used run, then without every run of
            # every over-used token; either can recover a shorter correct
            # assignment that the unconstrained DP's first answer hid.
            tried = 0
            for index in chain:
                if tried >= 4:
                    break
                if any(token in excess for token in candidates[index][2]):
                    tried += 1
                    recovered, recovered_chain = solve(frozenset((index,)))
                    if not over_used(recovered_chain):
                        best_valid = max(best_valid, recovered)
            recovered, recovered_chain = solve(excluded_tokens=frozenset(excess))
            if not over_used(recovered_chain):
                best_valid = max(best_valid, recovered)
        return best_valid
    transitions, failure, terminal, suffix = [{}], [0], [None], [0]
    lengths = []
    for ident, needle in enumerate(needles):
        words = needle.split()
        lengths.append(len(words))
        state = 0
        for word in words:
            if word not in transitions[state]:
                if len(transitions) >= 50000:
                    return [_finding("manual_review", "contained_duplicate_budget_exhausted",
                                     "Quotation provenance index exceeded its node budget; review without deleting text.",
                                     [], node_budget=50000)]
                transitions[state][word] = len(transitions)
                transitions.append({}); failure.append(0); terminal.append(None); suffix.append(0)
            state = transitions[state][word]
        terminal[state] = ident
    queue = deque(transitions[0].values())
    while queue:
        state = queue.popleft()
        for word, child in transitions[state].items():
            queue.append(child)
            fallback = failure[state]
            while fallback and word not in transitions[fallback]:
                fallback = failure[fallback]
            failure[child] = transitions[fallback].get(word, 0)
            parent = failure[child]
            suffix[child] = parent if terminal[parent] is not None else suffix[parent]
    source_counts = [0] * len(needles)
    output_counts = [0] * len(needles)
    quote_counts = [0] * len(needles)
    source_spans = [[] for _ in needles]
    carriers = [set() for _ in needles]
    separate = [[] for _ in needles]
    needle_ids = {needle: i for i, needle in enumerate(needles)}
    for row in sorted(character_rows):
        if texts[row]:
            separate[needle_ids[texts[row]]].append(row)
    remaining = 200000
    longest = max(lengths)
    offsets = [0] * longest

    def scan(text, counts, row=None, quotation=False, spans=None):
        nonlocal remaining
        state = 0
        last_end = {}
        # Shared offset ring bounds memory by longest phrase, not book size.
        for position, word_match in enumerate(re.finditer(r"\S+", text)):
            word = word_match.group()
            offsets[position % longest] = word_match.start()
            while state and word not in transitions[state]:
                state = failure[state]
            state = transitions[state].get(word, 0)
            hit = state
            while hit:
                ident = terminal[hit]
                if ident is not None:
                    remaining -= 1
                    if remaining < 0:
                        return False
                    start = position - lengths[ident] + 1
                    if start >= last_end.get(ident, 0):
                        last_end[ident] = position + 1
                        # Only strictly larger entries can carry a nested copy.
                        if row is None or len(texts[row]) > len(needles[ident]):
                            counts[ident] += 1
                            if row is None:
                                span_rows = source_spans if spans is None else spans
                                if len(span_rows[ident]) < 8:
                                    span_rows[ident].append([offsets[start % longest], word_match.end()])
                            elif not quotation:
                                carriers[ident].add(row)
                        elif not quotation:
                            counts[ident] += 1
                hit = suffix[hit]
        return True

    complete = scan(source_keys[0], source_counts)
    for variant in source_keys[1:]:
        if not complete:
            break
        variant_counts = [0] * len(needles)
        variant_spans = [[] for _ in needles]
        complete = scan(variant, variant_counts, spans=variant_spans)
        for ident, count in enumerate(variant_counts):
            if count > source_counts[ident]:
                source_counts[ident] = count
                # Spans stay indexed to the PRIMARY key: a variant key is a
                # different string, so its offsets would point into the wrong
                # text when reported as source_word_normalized_spans. The
                # throwaway list above keeps the variant scan from writing them.
    for row, text in enumerate(texts):
        if not complete:
            break
        complete = scan(text, output_counts, row)
        for part in quoted[row]:
            if not complete:
                break
            complete = scan(part, quote_counts, row, True)
    if not complete:
        return [_finding("manual_review", "contained_duplicate_budget_exhausted",
                         "Quotation provenance scan exceeded its match budget; review without deleting text.",
                         [], match_budget=200000)]
    expansions = (
        (r"\bchapter\s+i\b", "one"), (r"\bdr\.", "doctor"),
        (r"\bmr\.", "mister"), (r"&", "and"), (r"\b3rd\b", "third"),
    )
    expansion_sites = [(word, len(re.findall(pattern, source_text, re.I)))
                       for pattern, word in expansions]
    # Both of these are needle-independent, so scan the whole source once.
    source_has_bracket = bool(re.search(r"[\[<]", source_text))
    unknown_rendering = bool(re.search(r"\d+", re.sub(r"\b3rd\b", "", source_text, flags=re.I))
                             or re.search(r"\bchapter\s+(?!i\b)[ivxlcdm]+\b", source_text, re.I)
                             or source_has_bracket)

    def get_digit_words_unemitted():
        """True when a source number may have been spoken instead of written."""
        nonlocal digit_words_unemitted
        if digit_words_unemitted is not None:
            return digit_words_unemitted
        ensure_unspent()
        # Find digits first, then widen to the whole source word: scanning
        # every word for a digit costs more than the rest of this call on a
        # large book, and a word without a digit can never be one of these.
        spans = set()
        for match in re.finditer(r"\d", source):
            start, end = match.start(), match.end()
            while start and not source[start - 1].isspace():
                start -= 1
            while end < len(source) and not source[end].isspace():
                end += 1
            spans.add((start, end))
        digit_words = Counter(source[start:end] for start, end in spans)
        digit_words.pop("3rd", None)
        unknown = any(output_word_counts[word] < count
                      for word, count in digit_words.items())
        if not unknown:
            source_words = " ".join(source.split())
            output_text = " ".join(output_words)
            for numeral in set(re.findall(
                    r"\bchapter\s+((?!i\b)[ivxlcdm]+)\b", source_text, re.I)):
                phrase = "chapter " + numeral.casefold()
                if get_source_phrase_occurrences(output_text, phrase) < get_source_phrase_occurrences(source_words, phrase):
                    unknown = True
                    break
        digit_words_unemitted = unknown
        return unknown

    def number_rendering_unknown(needle):
        """True when this needle could be the words a number was rendered as.

        A bracket has no spelling proof, so it keeps the whole-source rule. A
        digit or Roman chapter numeral can only have become words where its own
        source word was not emitted as written, and it can only explain an extra
        copy of THIS needle when the needle is itself a spelled number, or when
        the source carries this same line with the number written as digits
        (counted into capacity by ``number_spelling_source_copies``, so an extra
        copy beyond those still blocks). A duplicated line of ordinary words
        that the source never writes with a digit is not the rendering of a
        number elsewhere, so it keeps blocking. A known expansion ("3rd" ->
        "third") is not a digit word at all.
        """
        if source_has_bracket:
            return True
        return get_digit_words_unemitted() and is_spelled_number(needle.split())

    def number_spelling_source_copies(needle):
        """Source capacity this line gains from copies with a digit number.

        "Twenty minutes" and "20 minutes" are one line written two ways: when
        the book holds one copy of each, a script that spells both copies out
        ("Twenty minutes" twice) is faithful, and a third copy still exceeds the
        book. Only ONE token may differ; it must be digits on one side and a
        spelled number on the other, both must parse to the same value through
        the number tables that already exist in ``alexandria_alignment``, and
        the words must be one line - the same comparison tokens and gap rule the
        boundary counter uses, so punctuation and a paragraph break still
        separate lines. Copies may not overlap, like every other capacity count.

        The covered spelling is one English cardinal word: zero through
        nineteen and the tens. Spellings that need more than one token
        (hyphenated "twenty-one", "one hundred", "nineteen ninety") and
        ordinals, Roman numerals, decimals and non-English spellings do not
        parse as one token here and still block (documented under Limits).

        The digit must be a WHOLE source number. A tokenizer turns "3.5" into
        "3", "5", "1,000" into "1", "000", "1 000" into "1", "000", and "5-6"
        or "5:30" into two digit tokens; crediting the "5" of "3.5" would let
        it spell "Five minutes" and accept a genuine duplicate, so a digit
        joined to another digit by a numeral separator is not capacity. That
        covers `.`/`,` glued to a far digit, a thousands SPACE (space, NBSP,
        thin or figure space - joined only when the far group is exactly three
        digits, so "1999 5" is a year and a separate number), and a range or
        clock mark glued between digits. A sentence's "5." or a list's "5," is
        a whole number, not a fragment.

        A mark between digits joins ONLY when it touches both digits. Glued
        `5-6`, `5--6`, en dash, em dash, figure dash, minus, ASCII tilde, wave
        dash, fullwidth tilde, `5:30` and `5/6` are one numeral in two tokens,
        so neither piece is whole. A SPACED mark is sentence punctuation, not a
        separator: `1914 — 5`, `12 — 5`, `12: 5` and a spaced slash (`12 / 5`,
        a ratio or quoted verse) keep the far digit whole (#2304 gate 4 L1/L2,
        gate 5 L3 - one glued-only rule for every mark).

        A LINE BREAK IS NEVER A NUMERAL SEPARATOR - for any mark or break
        type. A digit that starts a line after a line ending in a number opens
        a new line, not the far half of a number, so a gap containing `\\n`,
        `\\r`, U+2028, U+2029, `\\x0b`, `\\x0c`, `\\x85`, `\\x1c`, `\\x1d` or
        `\\x1e` never joins. A false
        block stops the user's audiobook while a missed duplicate does not, so
        a range wrapped across a line break (`5-\\n6`) is credited as two whole
        numbers and a duplicate of it can pass (documented under Limits). A
        faithful book with "Chapter 12" above "5 minutes later" stays clean,
        and a blank line keeps a "- 12 -" page number apart for every break
        type (#2304 gate 3; gate 5 governing rule).

        A digit copy the script also emits as digits is already that digit
        needle's own capacity, so only copies beyond the digit line's emissions
        are surplus capacity for the spelled line. Without that, one source copy
        could serve a digit emission and a spelled emission at once and a
        genuine extra copy would be hidden. Per digit line that subtraction is
        clamped at zero: an over-emitted digit line must not erase the spelled
        finding (capacity 0 skips it) or produce a negative count.
        """
        nonlocal number_line_view
        words = needle.split()
        if len(words) < 2:
            return 0
        if not any(is_spelled_number([word]) for word in words):
            # An accepted window needs a needle word that is a spelled number,
            # so a needle without one has no digit spelling at all.
            return 0
        if number_line_view is None:
            # Tokens and gaps come from one comparison view; the spans let the
            # whole-numeral test look at the characters beside each digit.
            comparison_source = (
                source_comparison_text if source_comparison_text is not None
                else _comparison_text(source_text,
                                      word_boundaries=output_boundaries))
            spans = []
            tokens, fusable = get_comparison_tokens_and_gaps(
                source_text, word_boundaries=output_boundaries,
                comparison_text=comparison_source, token_spans=spans)
            whole_number = [True] * len(tokens)
            # The raw-gap rescue needs comparison offsets mapped back to source
            # offsets; build that map lazily, once, and only if a spaced mark is
            # actually found.
            raw_gap_offsets = None
            raw_gap_offsets_ready = False
            for index, token in enumerate(tokens):
                if not token.isdigit():
                    continue
                start, end = spans[index]
                before = comparison_source[start - 1] if start else ""
                after = (comparison_source[end]
                         if end < len(comparison_source) else "")
                before_before = comparison_source[start - 2] if start > 1 else ""
                after_after = (comparison_source[end + 1]
                               if end + 1 < len(comparison_source) else "")
                # A numeral can also split into two digit TOKENS when the
                # separator is not glued to both digits: "1 000" (also NBSP and
                # thin space), "5-6" and "5:30". Either token is a fragment of
                # one numeral, so neither is whole. A separator with a digit on
                # its far side ("3.5", "1,000") is the same case one character
                # in; a trailing "5." or "5," has no far digit and stays whole.
                joined = False
                for other in (index - 1, index + 1):
                    if not (0 <= other < len(tokens)):
                        continue
                    if not tokens[other].isdigit():
                        continue
                    if other < index:
                        gap_start, gap_end = spans[other][1], start
                        right = index
                    else:
                        gap_start, gap_end = end, spans[other][0]
                        right = other
                    gap = comparison_source[gap_start:gap_end]
                    # A line break is never a numeral separator, for any mark
                    # or break type. A digit that opens a line after a line
                    # ending in a number opens a NEW line, not the far half of
                    # one numeral, so a range wrapped across a break ("5-\n6")
                    # is two whole numbers - an accepted under-block, because a
                    # false block stops the audiobook and a missed duplicate
                    # does not (#2304 gate 5 governing rule).
                    if _number_gap_line_breaks(gap):
                        continue
                    stripped = gap.strip()
                    # A range/clock mark joins only when it touches both
                    # digits. A spaced mark is sentence punctuation: "1914 — 5"
                    # is a year and a separate number, "12: 5" a count and a
                    # new sentence, "12 / 5" a ratio or quoted verse (#2304
                    # gate 4 L1/L2, gate 5 L3).
                    if stripped in _NUMERAL_GAP_MARKS:
                        spaced = any(character in _HORIZONTAL_GAP_SPACES
                                     for character in gap)
                        if spaced:
                            # An invisible word boundary is a space in the
                            # comparison key but not on the page: "5\u200b-6"
                            # is a glued range. Decide on the raw gap; folding
                            # may have changed the source length anywhere, so
                            # map the comparison offsets back first.
                            if not raw_gap_offsets_ready:
                                raw_gap_offsets = (
                                    None if source_text == comparison_source
                                    else _comparison_offset_map(
                                        source_text, comparison_source,
                                        output_boundaries))
                                raw_gap_offsets_ready = True
                            raw_gap = _raw_number_gap(
                                source_text, comparison_source, gap_start,
                                gap_end, gap, output_boundaries,
                                raw_gap_offsets)
                            if raw_gap is None or any(
                                    character in _HORIZONTAL_GAP_SPACES
                                    for character in raw_gap):
                                continue
                        joined = True
                        break
                    # Whitespace joins only a thousands group, whose far half
                    # is exactly three digits ("1 000", "12 345 678"); "In
                    # 1999 5 minutes later" is a year and a separate number.
                    if not stripped and len(tokens[right]) == 3:
                        joined = True
                        break
                whole_number[index] = not (
                    before.isdigit() or after.isdigit()
                    or (before in ".," and before_before.isdigit())
                    or (after in ".," and after_after.isdigit())
                    or joined)
            number_line_view = (tokens, fusable, _build_number_spelling_digit_index(
                tokens, whole_number))
        tokens, fusable, digit_index = number_line_view
        digit_lines = _number_spelling_digit_lines(
            tokens, fusable, words, digit_index)
        # A digit copy the script also emits as digits is already capacity for
        # that digit spelling (the digit needle counts its own source copies).
        # Only the copies beyond those emissions can carry a spelled emission;
        # otherwise one source copy serves both a digit and a spelled emission,
        # and a genuine extra copy is hidden. Clamp each line at zero: more
        # emissions than copies is not negative capacity, and letting it reduce
        # the sum would drop the spelled finding or report an impossible count.
        return sum(max(0, copies - output_counts[needle_ids[line]])
                   if line in needle_ids else copies
                   for line, copies in digit_lines.items())

    findings = []
    for ident, needle in enumerate(needles):
        if not needle:
            continue
        capacity = source_counts[ident]
        added_capacity = sum(count for word, count in expansion_sites if word in needle.split())
        all_emitted = output_counts[ident]
        counted_whole_tokens = None
        boundary_claimed = None
        # Only an excess count needs the extra scan: the spelling-independent
        # count can raise capacity to the true number of source copies, never
        # above it, so a needle already within its per-view capacity cannot
        # become a finding because of it.
        if all_emitted > capacity + added_capacity:
            # ONE RULE: capacity is the number of non-overlapping source copies
            # that spell this line when a space, a U+200B and a ZWNJ are all
            # optional separators, so every output spelling of one line gets
            # one capacity.
            if match_boundary_copies is None:
                match_boundary_copies = _build_boundary_insensitive_matcher(
                    source_text, comparison_text=source_comparison_text)
            boundary_count, whole_tokens, claimed_indices = match_boundary_copies(needle)
            if boundary_count > capacity:
                capacity = boundary_count
                counted_whole_tokens = whole_tokens
                boundary_claimed = claimed_indices
        if (all_emitted > capacity + added_capacity
                and not carriers[ident] and not re.search(r"\d", needle)
                and get_digit_words_unemitted()):
            # A line the source also writes with a digit for one of the needle's
            # number words is another copy of the same line, so it is capacity,
            # not an excuse for an unlimited number of emissions. A third copy
            # of "Twenty minutes" still blocks.
            capacity += number_spelling_source_copies(needle)
        if not capacity:
            # A needle the source does not carry at all. A wrapped source copy
            # between ordinary characters cannot land here: the
            # spelling-independent count above fuses a soft wrap and raises
            # capacity for it, and it runs whenever the emitted count exceeds
            # the per-view capacity. A wrap immediately before a combining mark
            # is a known limit and is pinned by its own test.
            continue
        if all_emitted <= capacity + added_capacity:
            continue
        if not carriers[ident] and not separate[ident]:
            continue
        proven_emitted = quote_counts[ident] + len(separate[ident])
        # Carrier findings keep the whole-source rendering rule exactly as it
        # was. A separate-entry finding gets the narrower digit proof, except
        # when the needle itself carries a digit: its spoken form may already
        # sit in the source as another copy ("three" beside "3"), so the digit
        # count cannot be trusted to prove an extra copy.
        rendering_unknown = (unknown_rendering if carriers[ident] or re.search(r"\d", needle)
                             else number_rendering_unknown(needle))
        ambiguous = rendering_unknown or proven_emitted <= capacity + added_capacity
        unknown_capacity = ambiguous
        support = 0
        # A source word the output never emitted can squeeze a repetition into
        # one token ("はいはい") that a faithful script splits into separate
        # entries. Only a separate-entry finding is explained this way; the split
        # must appear adjacent in the output, and an extra copy still blocks.
        if not ambiguous and not carriers[ident]:
            support = unspent_token_support(
                needle,
                counted_whole_tokens=counted_whole_tokens,
                boundary_claimed=boundary_claimed,
            )
            ambiguous = all_emitted <= capacity + added_capacity + support
        findings.append(_finding(
            "manual_review" if ambiguous else "blocking", "contained_source_duplicate",
            "Contained phrase repetition needs review of its rendering/source provenance."
            if ambiguous else "A character line exceeds its distinct source support, either "
            "repeated as separate entries or also inside a larger entry. Regenerate losslessly; "
            "do not delete legitimate source repeats.",
            sorted(i + 1 for i in carriers[ident].union(separate[ident])), phrase=needle,
            source_occurrences=capacity, possible_expansion_occurrences=added_capacity,
            output_quotation_occurrences=proven_emitted, all_text_occurrences=all_emitted,
            rendering_capacity_unknown=unknown_capacity,
            unemitted_source_split_support=support,
            source_word_normalized_spans=source_spans[ident],
            source_spans_truncated=capacity > len(source_spans[ident]),
        ))
    return findings


def find_adjacent_duplicate_blocks(texts, source_text, *, source_normalized=None, count_phrase=None):
    # Callers pass comparison-normalized entry text, but a soft hyphen or word
    # joiner must not make two copies of one block compare unequal (#2316).
    texts = [remove_silent_word_characters(text) for text in texts]
    findings = []
    occupied = set()
    if source_normalized is None:
        source_normalized = _normalize_words(source_text)
    count_phrase = count_phrase or get_source_phrase_counter(source_normalized)
    for block_size in range(5, 1, -1):
        index = 0
        while index + (2 * block_size) <= len(texts):
            left = texts[index:index + block_size]
            right = texts[index + block_size:index + (2 * block_size)]
            positions = set(range(index, index + (2 * block_size)))
            if (positions.isdisjoint(occupied) and left == right and
                    all(len(text) >= 8 for text in left)):
                # TWO WAYS TO ASK "does the source contain this block?", and
                # the contiguous one alone is wrong.
                #
                # Entries are not contiguous spans of the source - a dialogue
                # entry is followed in the book by narration the next entry
                # skips. So joining two entries and searching for that string
                # finds nothing whenever anything sits between them, and a
                # plain duplication scores 0, which reads as "the model
                # invented this" when it means "these two lines are not
                # adjacent in the book".
                #
                # Alice in Wonderland: the Caterpillar's "Explain yourself!"
                # and Alice's reply were emitted twice. Each line occurs once
                # in the source; joined, they occur zero times, because "said
                # Alice" sits between them. The book generated all 25 chunks,
                # the repair knew how to fix it, and it was thrown away as
                # unresolvable - 919 entries discarded over two lines.
                #
                # So fall back to the per-entry minimum: if every line in the
                # block is in the source, the block is duplicated (removable).
                # Only a line the source lacks entirely is an invention.
                contiguous = count_phrase(_normalize_words(" ".join(left)))
                source_occurrences = get_source_block_occurrences(
                    source_normalized, left, count_phrase=count_phrase)
                # A block the SOURCE itself repeats is faithful
                # transcription, not a defect. grimgar03 opens with its
                # title eight times; the whole-book gate rejected the
                # finished book for reproducing it, after all 49 chunks had
                # generated cleanly. script_repair.py already made this
                # distinction for the repair path - having it in only one of
                # the two places is how the same book failed twice for the
                # same reason (Rule 15: one decision, one implementation).
                faithful = source_occurrences >= 2
                findings.append(_finding(
                    "manual_review" if faithful else "blocking",
                    "adjacent_duplicate_block",
                    f"Entries {index + 1}-{index + block_size} are repeated "
                    + ("immediately, and the source repeats them too."
                       if faithful else "immediately."),
                    list(range(index + 1, index + (2 * block_size) + 1)),
                    block_size=block_size,
                    source_occurrences=source_occurrences,
                    contiguous_block_occurrences=contiguous,
                ))
                occupied.update(positions)
                index += 2 * block_size
            else:
                index += 1
    return findings


# A quoted lexical term can be copied at a split seam in three shapes: its own
# short narrator row, the head of the following narrator row, or twice inside
# one narrator row. The adjacent-block check cannot see a short copy, and at
# book chunk size the recall/trigram/ratio metrics barely move. Candidate
# phrases come from source provenance (bracketed reference terms and lexical
# quote regions) plus bounded narrator seam fragments, so this never becomes
# an unbounded scan of every word phrase in the chunk.
MAX_SEAM_DUPLICATE_WORDS = 10
MAX_SEAM_DUPLICATE_CHARS = 120
MAX_SEAM_DUPLICATE_CANDIDATES = 128
_BRACKETED_TERM = re.compile(r"\[([^\[\]\n]{1,80})\]")
# Quoted reference terms are lexical-quote evidence too. Scan only the short
# paired-quote contents: dialogue rows are excluded from the finding, so this
# cannot turn a repeated spoken line into a narration duplicate.
_QUOTED_TERM = re.compile(r'["“「『«]([^"”」』»\n]{1,80})["”」』»]')


def _is_narration_row(entry):
    if not isinstance(entry, dict):
        return False
    role = str(entry.get("speaker") or entry.get("type") or "")
    return not role or role.casefold() in {"narrator", "narration"}


def _phrase_near_edge(words, phrase_words, side):
    """Whole-phrase match within the bounded window of an entry edge."""
    if not phrase_words or not words:
        return False
    region = words[-MAX_SEAM_DUPLICATE_WORDS:] if side == "end" else words[:MAX_SEAM_DUPLICATE_WORDS]
    size = len(phrase_words)
    return any(region[i:i + size] == phrase_words for i in range(len(region) - size + 1))


def get_seam_duplicate_candidates(source_text, entries, *, quote_analysis=None):
    """Bounded short phrases grounded in source quotes or narrator row edges."""
    candidates = []

    def add(value):
        phrase = _normalize_words(value)
        if (phrase and phrase not in candidates
                and len(phrase.split()) <= MAX_SEAM_DUPLICATE_WORDS
                and len(phrase) <= MAX_SEAM_DUPLICATE_CHARS
                and len(candidates) < MAX_SEAM_DUPLICATE_CANDIDATES):
            candidates.append(phrase)

    for match in _BRACKETED_TERM.finditer(str(source_text or "")):
        add(match.group(1))
    for match in _QUOTED_TERM.finditer(str(source_text or "")):
        add(match.group(1))
    for region in (quote_analysis or {}).get("regions", []):
        if region.get("quote_role") and region.get("type") == "NARRATOR":
            add(region.get("text"))
    for entry in entries:
        if not _is_narration_row(entry):
            continue
        words = _normalize_words(entry.get("text")).split()
        if words:
            add(" ".join(words[:MAX_SEAM_DUPLICATE_WORDS]))
            add(" ".join(words[-MAX_SEAM_DUPLICATE_WORDS:]))
    return candidates


def find_seam_duplicate_narration(source_text, entries, *, quote_analysis=None,
                                  role_specific=True, source_narration_text=None,
                                  source_normalized=None, count_phrase=None):
    """Find a short narration quote copied near a split, with source support.

    Pass 1 sometimes splits a sentence at a quoted lexical term (a title, a
    skill, a name) and emits that term a second time: as its own NARRATOR row,
    at the head of the next row, or twice inside one row. ``find_adjacent_
    duplicate_blocks`` starts at two repeated rows and the aggregate fidelity
    metrics stay clean at chunk size, so none of them see the extra copy
    (issue #2143).

    Candidates are short phrases with verified source provenance, not every
    word in the output. A finding needs both a locally duplicated shape and a
    strictly greater whole-token output count than the source. A phrase the
    source itself repeats that many times is faithful transcription and is
    left alone. Dialogue rows (SPOKEN or a character speaker) are excluded
    when ``role_specific`` is true: their contained-quote provenance is
    ``find_contained_source_duplicates``'s job. When a permissive quote policy
    allows a quoted source occurrence to be emitted inside a narrator row,
    source and output roles are not comparable, so both sides are counted in
    full. No fidelity threshold is changed here.
    """
    # Role-specific accounting is only sound when the run's quote policy
    # forces source and output roles to agree.
    allowance_text = (source_narration_text if role_specific and source_narration_text is not None
                      else source_text)
    if (role_specific and source_narration_text is not None) or source_normalized is None:
        source_normalized = _normalize_words(allowance_text)
    count_phrase = count_phrase or get_source_phrase_counter(source_normalized)
    texts = [_normalize_words(entry.get("text")) if isinstance(entry, dict) else ""
             for entry in entries]
    narration = [_is_narration_row(entry) for entry in entries] if role_specific else [True] * len(entries)
    word_lists = [text.split() for text in texts]
    findings = []
    for phrase in get_seam_duplicate_candidates(source_text, entries,
                                                quote_analysis=quote_analysis):
        source_count = count_phrase(phrase)
        if source_count < 1:
            # No verified source correspondence: abstain rather than invent
            # a duplicate verdict from a phrase the source cannot confirm.
            continue
        counts = [get_source_phrase_occurrences(text, phrase) if narration[i] else 0
                  for i, text in enumerate(texts)]
        emitted = sum(counts)
        if emitted <= source_count:
            continue
        phrase_words = phrase.split()
        numbers = {number for number, count in enumerate(counts, 1) if count >= 2}
        for index in range(len(texts) - 1):
            if (counts[index] and counts[index + 1]
                    and _phrase_near_edge(word_lists[index], phrase_words, "end")
                    and _phrase_near_edge(word_lists[index + 1], phrase_words, "start")):
                numbers.update((index + 1, index + 2))
        if not numbers:
            continue
        findings.append(_finding(
            "blocking", "seam_duplicate_narration",
            "A short quoted term is copied near a split seam or inside one "
            "entry; the source does not carry the extra copy.",
            sorted(numbers),
            phrase=phrase, source_occurrences=source_count, output_occurrences=emitted))
    return findings


def find_adjacent_near_duplicate_entries(texts, source_text, minimum_ratio=0.90, *, exact_findings=None, source_normalized=None, count_phrase=None):
    """Adjacent entry pairs that are near-duplicates of each other but not
    supported by the source - likely model re-generation at a seam.

    Non-blocking evidence only: findings are always ``manual_review``, never
    a rejection - a fallback net for paraphrased seam repeats that slip past
    the exact ``find_adjacent_duplicate_blocks`` check. Deliberately NOT
    wired into ``chunk_quality.validate_chunk_quality`` - any finding there
    sets ``passed: False`` and triggers a retry, which would turn this
    non-blocking net into a blocking gate.
    """
    findings = []
    occupied = set()
    if source_normalized is None:
        source_normalized = _normalize_words(source_text)
    count_phrase = count_phrase or get_source_phrase_counter(source_normalized)
    if exact_findings is None:
        exact_findings = find_adjacent_duplicate_blocks(
            texts, source_text, source_normalized=source_normalized, count_phrase=count_phrase)
    for finding in exact_findings:
        occupied.update(number - 1 for number in finding["entry_numbers"])

    for index in range(len(texts) - 1):
        if index in occupied or (index + 1) in occupied:
            continue
        first, second = texts[index], texts[index + 1]
        if len(first) < 8 or len(second) < 8:
            continue
        first_words = _normalize_words(first).split()
        second_words = _normalize_words(second).split()
        if len(first_words) < 5 or len(second_words) < 5:
            continue
        matcher = difflib.SequenceMatcher(None, first_words, second_words, autojunk=False)
        # Both quick ratios are upper bounds; preserve candidates at the threshold.
        if matcher.real_quick_ratio() < minimum_ratio or matcher.quick_ratio() < minimum_ratio:
            continue
        ratio = matcher.ratio()
        if ratio < minimum_ratio:
            continue

        if source_text:
            # Identical text used twice needs two occurrences in the source to be
            # "genuinely repeated prose"; one occurrence can't back both uses.
            required_occurrences = 2 if first_words == second_words else 1
            first_supported = count_phrase(_normalize_words(first)) >= required_occurrences
            second_supported = count_phrase(_normalize_words(second)) >= required_occurrences
            if first_supported and second_supported:
                continue
            source_checked = True
            source_supported = [first_supported, second_supported]
        else:
            source_checked = False
            source_supported = None

        findings.append(_finding(
            "manual_review", "adjacent_near_duplicate",
            f"Entries {index + 1}-{index + 2} are near-duplicates of each other.",
            [index + 1, index + 2],
            similarity=round(ratio, 4),
            source_checked=source_checked,
            source_supported=source_supported,
        ))
    return findings


def get_invalid_entry_fields(entry):
    """Return present script fields that cannot be consumed as strings."""
    return [field for field in ("text", "speaker", "instruct")
            if field in entry and not isinstance(entry[field], str)]


def audit_script(entries, source_text=None, is_generic_speaker_fn=None):
    """Return deterministic findings without modifying ``entries``."""
    findings = []
    if not isinstance(entries, list):
        findings.append(_finding("blocking", "invalid_script", "The script root must be a JSON array."))
        return _build_report(0, findings)

    texts = []
    instructions = []
    source_scripts = None
    for index, entry in enumerate(entries, start=1):
        if not isinstance(entry, dict):
            findings.append(_finding("blocking", "invalid_entry", "Entry must be a JSON object.", [index]))
            texts.append("")
            continue

        raw_text = entry.get("text")
        raw_speaker = entry.get("speaker")
        raw_instruct = entry.get("instruct")
        for field in get_invalid_entry_fields(entry):
            findings.append(_finding("blocking", "invalid_field_type",
                                     f"Entry {field} must be a string.", [index],
                                     field=field))
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        speaker = raw_speaker.strip() if isinstance(raw_speaker, str) else ""
        instruct = raw_instruct.strip() if isinstance(raw_instruct, str) else ""
        texts.append(_normalize(text))
        instructions.append(_normalize(instruct))

        if not text:
            findings.append(_finding("blocking", "empty_text", "Entry has no speakable text.", [index]))
        # One emptiness rule with `stabilize_speaker_identities`: a label made
        # only of whitespace and/or silent word characters is missing.
        if not _normalize_speaker_label(speaker):
            findings.append(_finding("blocking", "missing_speaker", "Entry has no speaker.", [index]))
        if not instruct:
            findings.append(_finding("manual_review", "missing_instruction", "Entry has no delivery instruction.", [index]))
        if source_scripts is None:
            source_scripts = get_unicode_scripts(source_text)
        unicode_report = audit_unicode_text(text, source_text, source_scripts=source_scripts)
        if unicode_report["introduced_scripts"]:
            findings.append(_finding(
                "blocking", "introduced_unicode_script",
                "Entry contains a writing system absent from the source.",
                [index], scripts=unicode_report["introduced_scripts"],
            ))
        if unicode_report["mixed_script_words"]:
            findings.append(_finding("blocking", "mixed_script_word",
                                     "A word combines multiple writing systems.", [index],
                                     words=unicode_report["mixed_script_words"]))
        if unicode_report["unsafe_controls"]:
            findings.append(_finding("blocking", "unsafe_unicode_character",
                                     "Entry contains unsafe control characters.",
                                     [index], unicode=unicode_report))
        elif unicode_report["replacement_character_count"]:
            # Non-blocking: the SOURCE gate already decided whether this book's
            # damage is acceptable. Re-refusing here would mean a source we
            # admitted could never produce an entry we accept - which is how
            # index18 passed the front door and then failed at chunk 31.
            findings.append(_finding("manual_review", "replacement_character",
                                     "Entry contains replacement characters "
                                     "the source gate already accepted.",
                                     [index], unicode=unicode_report))
        for field, value in (("speaker", speaker), ("instruct", instruct)):
            controls = get_unsafe_text_controls(value)
            if controls:
                findings.append(_finding("blocking", "unsafe_unicode_character",
                                         f"Entry {field} contains unsafe control characters.",
                                         [index], field=field, unsafe_controls=controls))
        if index <= 30 and _FRONT_MATTER_RE.search(text):
            findings.append(_finding("manual_review", "front_matter", "Possible publication front matter.", [index]))
        if is_possible_misattributed_narration(text, speaker):
            findings.append(_finding(
                "manual_review", "possible_misattributed_narration",
                "Third-person narration may be assigned to a character.", [index], speaker=speaker,
            ))
        if speaker and is_generic_speaker_fn and is_generic_speaker_fn(speaker):
            findings.append(_finding(
                "manual_review", "generic_speaker", "Generic speaker label needs book-local review.",
                [index], speaker=speaker,
            ))
        speech = get_speech_normalization(text)
        if speech["risk_categories"]:
            findings.append(_finding(
                "manual_review", "nonprose_speech_risk",
                "Non-prose content has elevated TTS error risk; review the "
                "spoken preview and validate the generated audio.",
                [index], categories=speech["risk_categories"],
                normalized_preview=speech["text"],
                transformations=speech["transformations"],
                validation={
                    "recommended": True,
                    "method": "targeted_transcription_or_human_listening",
                    "status": "not_run",
                    "reason": "Script preflight does not generate or transcribe audio.",
                },
            ))

    source_tokens = get_normalized_word_tokens(source_text)
    source_normalized = " ".join(source_tokens)
    count_phrase = get_source_phrase_counter(source_normalized)
    exact_findings = find_adjacent_duplicate_blocks(
        texts, source_text, source_normalized=source_normalized, count_phrase=count_phrase)
    findings.extend(exact_findings)
    findings.extend(find_contained_source_duplicates(
        entries, source_text, source_normalized=source_normalized))
    findings.extend(find_adjacent_near_duplicate_entries(
        texts, source_text, exact_findings=exact_findings,
        source_normalized=source_normalized, count_phrase=count_phrase))
    del source_normalized, count_phrase

    nonempty_instructions = [value for value in instructions if value]
    if len(nonempty_instructions) >= 20:
        uniqueness = len(set(nonempty_instructions)) / len(nonempty_instructions)
        if uniqueness >= 0.95:
            findings.append(_finding(
                "informational", "high_instruction_uniqueness",
                "Nearly every entry has a unique delivery instruction.",
                uniqueness=round(uniqueness, 4),
            ))

    if source_text:
        script_words = Counter(
            word for text in texts
            for word in _WORD_RE.findall(text))
        source_words = Counter(source_tokens)
        del source_tokens
        source_total = sum(source_words.values())
        matched = sum(min(count, script_words.get(word, 0))
                      for word, count in source_words.items())
        coverage = matched / source_total if source_total else 1.0
        if source_total and coverage < 0.85:
            # Measure again with the comparison key, tokenizing BOTH sides the
            # same way (combining marks kept, boundary characters fused, ZWNJ
            # treated as an optional separator) so a faithful spelling
            # difference (U+200B, ZWJ, ZWNJ, apostrophe fold, combining marks)
            # never triggers low_source_word_coverage. Only runs when the
            # primary view fell below the threshold, so healthy books pay
            # nothing for it.
            for word_boundaries in ("fused", "space"):
                if (word_boundaries == "space"
                        and not any(character in source_text
                                    for character in WORD_BOUNDARIES)
                        and not any(
                            isinstance(entry, dict) and isinstance(entry.get("text"), str)
                            and any(character in entry["text"]
                                    for character in WORD_BOUNDARIES)
                            for entry in entries)):
                    continue
                comparison_source = Counter(
                    get_comparison_word_view(
                        source_text, word_boundaries=word_boundaries
                    ).replace(ZERO_WIDTH_NON_JOINER, "").split())
                comparison_script = Counter(
                    word for entry in entries
                    if isinstance(entry, dict) and isinstance(entry.get("text"), str)
                    for word in get_comparison_word_view(
                        entry["text"], word_boundaries=word_boundaries
                    ).replace(ZERO_WIDTH_NON_JOINER, "").split())
                view_total = sum(comparison_source.values())
                matched = sum(min(count, comparison_script.get(word, 0))
                              for word, count in comparison_source.items())
                coverage = max(coverage, matched / view_total if view_total else 1.0)
                if coverage >= 0.85:
                    break
        if source_total and coverage < 0.85:
            findings.append(_finding(
                "manual_review", "low_source_word_coverage",
                "Annotated text has low word coverage relative to the source.",
                coverage=round(coverage, 4),
            ))

    return _build_report(len(entries), findings)


def _build_report(entry_count, findings):
    counts = {severity: 0 for severity in ("blocking", "manual_review", "informational")}
    for finding in findings:
        counts[finding["severity"]] += 1
    return {
        "entry_count": entry_count,
        "counts": counts,
        "can_apply_repairs": counts["blocking"] == 0,
        "findings": findings,
    }
