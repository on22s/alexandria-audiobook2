"""Explicit, auditable normalization for known source-text corruption."""

from bisect import bisect_right
from functools import lru_cache
import re
import unicodedata


KNOWN_SOURCE_CORRUPTIONS = {"саге": "care", "пар": "nap"}
_KNOWN_RE = re.compile(r"(?<!\w)(?:" + "|".join(
    re.escape(value) for value in KNOWN_SOURCE_CORRUPTIONS) + r")(?!\w)", re.IGNORECASE)
_ILLUSTRATION_CAPTION_RE = re.compile(
    r"Illustration from Volume\s+\d+\s*,\s*coloring by\s+[^\n]{1,100}?\s*"
    r"\(source\)\s*", re.IGNORECASE)

_WORD_RE = re.compile(r"[^\W_]+(?:[’'][^\W_]+)?", re.UNICODE)
EXTREME_PHRASE_REPEAT_MIN = 20
EXTREME_PHRASE_REPEAT_KEEP = 3
EXTREME_PHRASE_MAX_WORDS = 5



def iter_source_paragraph_ranges(text):
    """Yield lossless paragraph spans, attaching separators to their preceding text.

    CRLF is atomic; a single line ending is a wrap, while U+2029 explicitly
    ends a paragraph. Blank lines may contain any Unicode whitespace.
    """
    newline = r"(?:\r\n|\r(?!\n)|[\n\u2028\u2029])"
    paragraph_break = r"(?:" + newline + r"(?:[^\S\r\n\u2028\u2029]*" + newline + r")+|\u2029)"
    start = 0
    for match in re.finditer(paragraph_break, text):
        yield start, match.end()
        start = match.end()
    if start < len(text):
        yield start, len(text)


def get_located_source_changes(text, changes):
    """Return new evidence records using one newline index for this stage."""
    if not changes:
        return []
    starts = [0, *(match.end() for match in re.finditer("\n", text))]
    located = []
    for change in changes:
        offset = change['offset']
        line = bisect_right(starts, offset)
        located.append({**change, 'line': line, 'column': offset - starts[line - 1] + 1})
    return located

def normalize_known_source_corruptions(text):
    """Return normalized text and location evidence without mutating its source file."""
    changes = []
    repair_start = 0

    def replace(match):
        before = match.group(0)
        after = KNOWN_SOURCE_CORRUPTIONS[before.casefold()]
        if before[:1].isupper():
            after = after.capitalize()
        offset = repair_start + match.start()
        changes.append({"offset": offset, "before": before, "after": after})
        return after

    # Start replacement at the first match, preserving absolute evidence
    # offsets while avoiding a second scan of the preceding prefix.
    first = _KNOWN_RE.search(text) if type(text) is str else None
    needs_repair = type(text) is not str or first
    if needs_repair and is_predominantly_latin_source(text):
        repair_start = first.start() if first is not None else 0
        normalized = (text[:repair_start] + _KNOWN_RE.sub(replace, text[repair_start:])
                      if repair_start else _KNOWN_RE.sub(replace, text))
    else:
        normalized = text

    caption_changes = []

    def remove_caption(match):
        offset = match.start()
        caption_changes.append({"offset": offset,
                        "before": match.group(0), "after": "",
                        "rule": "illustration_caption"})
        return ""

    cleaned = _ILLUSTRATION_CAPTION_RE.sub(remove_caption, normalized)
    return cleaned, (get_located_source_changes(text, changes)
                     + get_located_source_changes(normalized, caption_changes))


def normalize_extreme_phrase_repetitions(text):
    """Collapse only measured token-loop traps, returning location evidence.

    Ordinary emphasis and stutters stay untouched. Corpus calibration found a
    legitimate maximum of 13 contiguous repeats, while runs of 25, 42 and 43
    made the generation model loop to its token ceiling. A repeated unit may
    contain one to five words, but separators must be whitespace so unrelated
    prose cannot be joined into a match.
    """
    tokens = list(_WORD_RE.finditer(text))
    words = [match.group(0).casefold() for match in tokens]
    whitespace = [text[tokens[index - 1].end():tokens[index].start()].isspace()
                  for index in range(1, len(tokens))]
    candidates = [None] * len(tokens)
    size = len(tokens)
    if EXTREME_PHRASE_MAX_WORDS >= 1:
        streak = 1
        for start in range(size - 1, -1, -1):
            if start + 1 < size and whitespace[start] and words[start] == words[start + 1]:
                streak += 1
            else:
                streak = 1
            if streak >= EXTREME_PHRASE_REPEAT_MIN:
                candidates[start] = (tokens[start].start(), tokens[start + streak - 1].end(), 1, streak)
    for width in range(2, EXTREME_PHRASE_MAX_WORDS + 1):
        runs = [1] * size
        for start in range(size - width, -1, -1):
            offset = start + width
            if (offset + width <= size and whitespace[offset - 1]
                    and words[start] == words[offset]
                    and words[start + 1:offset] == words[offset + 1:offset + width]):
                runs[start] += runs[offset]
            repeats = runs[start]
            if repeats < EXTREME_PHRASE_REPEAT_MIN:
                continue
            end_index = start + repeats * width - 1
            candidate = (tokens[start].start(), tokens[end_index].end(), width, repeats)
            previous = candidates[start]
            if previous is None or candidate[1] > previous[1]:
                candidates[start] = candidate

    selected = []
    for candidate in candidates:
        if candidate is None or (selected and candidate[0] < selected[-1][1]):
            continue
        selected.append(candidate)
    if not selected:
        return text, []

    changes = []
    pieces = []
    cursor = 0
    for start, end, width, repeats in selected:
        repeated = text[start:end]
        repeated_tokens = list(_WORD_RE.finditer(repeated))
        kept_end = repeated_tokens[width * EXTREME_PHRASE_REPEAT_KEEP - 1].end()
        replacement = repeated[:kept_end] + "…"
        pieces.extend((text[cursor:start], replacement))
        changes.append({
            "offset": start,
            "before": repeated,
            "after": replacement,
            "rule": "extreme_phrase_repetition",
            "phrase_words": width,
            "repetitions": repeats,
        })
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces), get_located_source_changes(text, changes)


# Cyrillic letters visually identical to Latin ones in upright fonts, per the
# Unicode TR39 confusables data (https://www.unicode.org/Public/security/latest/
# confusables.txt), vendored as a small explicit dict rather than a dependency
# so every possible rewrite stays enumerable and auditable.
_HOMOGLYPH_MAP = {
    "а": "a", "е": "e", "о": "o", "р": "p", "с": "c", "у": "y", "х": "x",
    "і": "i", "ѕ": "s", "ј": "j",
    "А": "A", "В": "B", "Е": "E", "К": "K", "М": "M", "Н": "H", "О": "O",
    "Р": "P", "С": "C", "Т": "T", "У": "Y", "Х": "X", "Ѕ": "S", "І": "I",
    "Ј": "J",
    # Italic-form confusables observed in this corpus's actual corrupted
    # sources (KNOWN_SOURCE_CORRUPTIONS: "саге" -> "care", "пар" -> "nap").
    "г": "r", "п": "n",
}
_CYRILLIC_CHAR_RE = re.compile(r"[Ѐ-ӿ]")
_CYRILLIC_WORD_RE = re.compile(r"\w*[Ѐ-ӿ]\w*", re.UNICODE)
# A genuinely bilingual/Cyrillic text must never be rewritten; stray OCR
# corruption in a Latin book stays far below this share of letters.
MAX_HOMOGLYPH_CYRILLIC_RATIO = 0.005


def is_predominantly_latin_source(text):
    """Use the existing stray-corruption limit against actual Latin letters."""
    letters = 0
    non_latin = 0
    for char in text:
        if char.isalpha():
            letters += 1
            if not unicodedata.name(char, "").startswith("LATIN "):
                non_latin += 1
    return bool(letters and non_latin / letters < MAX_HOMOGLYPH_CYRILLIC_RATIO)


def normalize_homoglyph_words(text):
    """Map whole words of Cyrillic lookalike characters back to Latin,
    returning normalized text and location evidence without mutating the
    source file. Complements normalize_known_source_corruptions (which runs
    first and handles exact known words): a word is rewritten only when the
    document is overwhelmingly Latin and every Cyrillic character in the
    word has a homoglyph mapping - one unmappable character leaves the whole
    word untouched."""
    # The word matcher requires Cyrillic; plain text needs no language scan.
    # Keep the original path for subclasses, where re.sub returns a base str.
    if type(text) is str and not _CYRILLIC_CHAR_RE.search(text):
        return text, []
    if not is_predominantly_latin_source(text):
        return text, []

    changes = []

    def replace(match):
        word = match.group(0)
        if any(char not in _HOMOGLYPH_MAP
               for char in _CYRILLIC_CHAR_RE.findall(word)):
            return word
        after = "".join(_HOMOGLYPH_MAP.get(char, char) for char in word)
        offset = match.start()
        changes.append({"offset": offset, "before": word, "after": after, "rule": "homoglyph"})
        return after

    normalized = _CYRILLIC_WORD_RE.sub(replace, text)
    return normalized, get_located_source_changes(text, changes)


_FRONT_MATTER_ANCHOR = re.compile(
    r"Original (?:Web Novel|Light Novel) Chapter\s*[―—-]\s*(?:In)?[Cc]omplete\.\s*\n+"
    r"Original Translation by [^\n]+\.\s*\n+"
)


def strip_known_front_matter(text):
    """Strip a known fan-compiler's non-narrative front matter (translator's
    note + table of contents) when present, returning the story text and
    evidence of what was removed without mutating the source file.

    Scoped to one observed, stable compiler template (confirmed across 5
    "wn" uploads): a "Manifesto." translator's essay and chapter listing,
    ending right before the first "Original ... Chapter - Complete." /
    "Original Translation by ..." marker pair, which is always immediately
    followed by the real chapter 1 prose. This content isn't dialogue or
    narration and was measured live to break chunk generation (near-zero
    recall no matter how many times retried or split) since the annotation
    model has no idea how to handle it. Returns the text unchanged (and
    None) whenever the shape doesn't match, rather than guessing.
    """
    if not text.lstrip("﻿ \t\r\n").startswith("Manifesto."):
        return text, None
    match = _FRONT_MATTER_ANCHOR.search(text)
    if not match:
        return text, None
    return text[match.end():], {"removed_chars": match.end(),
                                 "removed_lines": text.count("\n", 0, match.end())}


_REPLACEMENT = "�"


def neutralize_lossy_residue(text, substitute=None):
    """Use the canonical repair policy by default; explicit substitutes opt in.

    The default must not consume destroyed characters before the shared repair
    engine can inspect them. Ambiguous long runs remain visible, as they do in
    repair_source_encoding. The count records actual replacement characters
    removed, rather than asserting that all damaged sites were repaired.
    """
    if _REPLACEMENT not in text:
        return text, 0
    if substitute is not None:
        return text.replace(_REPLACEMENT, substitute), text.count(_REPLACEMENT)
    from repair_source_encoding import repair
    repaired, _, _ = repair(text)
    return repaired, text.count(_REPLACEMENT) - repaired.count(_REPLACEMENT)


# Match metadata-shaped lines, not publisher keywords mentioned in prose.
_PUBLISHER_SIGNATURES = re.compile(
    r"^\s*(?:©|\(c\)\s*\d{4}|copyright(?:\s*$|\s+(?:©|\d{4}))|"
    r"all rights reserved|first published in|"
    r"english translation (?:rights|©|copyright)|"
    r"this book is a work of fiction|"
    r"scanning,? uploading,? and distribution|"
    r"ISBN(?:-1[03])?\s*:?[ \t]+[0-9][0-9 -]{8,}"
    r"(?:\s*\((?:ebook|e-book|paperback|hardcover)\))?[. ]*$|ebook edition|"
    r"printed in the united states|"
    r"(?:yenpress\.com|j-novel\.club)\s*$|"
    r"(?:yen (?:on|press)|j-novel club|kadokawa|hayakawa|seven seas|vertical, inc)"
    r"(?:\s+(?:LLC|Corporation|Publishing|Entertainment))?[., ]*$|"
    r"library of congress(?: cataloging-in-publication(?: data)?)?[. ]*$|cataloging-in-publication|"
    r"translation by |cover art by |illustration by |"
    r"(?:subjects|identifiers|classification|description|names|title)\s*:|"
    r"(?:LCCN|LCGFT|CYAC|LCC\s+P)\b|"
    r"(?:table of )?contents\s*$|cover page\s*$|landmarks\s*$|"
    r"color illustrations\s*$|(?:front|back)\s*matter\s*$)",
    re.IGNORECASE)

# A colophon paragraph is short. Long prose that happens to mention a publisher
# is story, not front matter.
_MAX_COLOPHON_PARAGRAPH_CHARS = 400
# Colophon blocks sit at the very ends. Never strip from the middle of a book.
# A colophon runs 10-30 paragraphs regardless of book length, so the share
# alone is too tight on a short book and the floor alone too loose on a long
# one; the scan window is the larger of the two, capped at half the book so a
# head and tail scan can never meet.
_MAX_COLOPHON_SHARE = 0.10
_MIN_COLOPHON_WINDOW = 40


# An epub extractor flattens a table of contents into one long paragraph, so
# the length cap above would exclude it. Repeated chapter markers or bullets
# identify it regardless of length - narrative prose does not enumerate
# "Chapter 1 Chapter 2 Chapter 3".
_TOC_MARKERS = re.compile(r"chapter\s+[0-9IVXLC]+|•|\u25a0\s*\d", re.IGNORECASE)
_MIN_TOC_MARKERS = 3


def _is_navigation_paragraph(text):
    return len(_TOC_MARKERS.findall(text)) >= _MIN_TOC_MARKERS


def _is_colophon_paragraph(paragraph):
    text = paragraph.strip()
    if not text:
        return False
    if any(mark in text for mark in ('"', "“", "”", "「", "」")):
        return False
    if _is_navigation_paragraph(text):
        return True
    if len(text) > _MAX_COLOPHON_PARAGRAPH_CHARS:
        return False
    return bool(_PUBLISHER_SIGNATURES.search(text))


def _colophon_run(paragraphs, limit, reverse=False):
    """Length of the colophon run anchored at one end.

    Tolerates ordinary lines between signature lines - a colophon interleaves
    the publisher's address and the book's title with its legal text - but only
    while a signature line keeps appearing, so the run cannot creep into prose.
    """
    order = range(len(paragraphs) - 1, -1, -1) if reverse else range(len(paragraphs))
    run, last_signature = 0, -1
    for step, index in enumerate(order):
        if step >= limit:
            break
        if not paragraphs[index].strip():
            # Blank paragraphs are neutral: an epub extractor leaves runs of
            # them inside a colophon, and counting them as evidence against
            # broke the run five blanks in.
            continue
        if _is_colophon_paragraph(paragraphs[index]):
            last_signature = step
            run = step + 1
        elif (len(paragraphs[index].strip()) > _MAX_COLOPHON_PARAGRAPH_CHARS
              or any(mark in paragraphs[index] for mark in ('"', "“", "”", "「", "」"))
              or paragraphs[index].strip().rstrip(
                  '"\'”’»」』)）]】').endswith((".", "!", "?", "。", "！", "？"))):
            break                      # real prose: stop here
        elif step - last_signature > 4:
            break                      # drifted too far from the last signature
    return run if last_signature >= 0 else 0


def strip_publisher_matter(text):
    """Remove a publisher colophon from the head and tail of a book.

    Official epubs carry a copyright page, and often a closing colophon, that
    is not narration but was being read aloud: one book opened with 16 entries
    of copyright notice, translator credit and a New York street address,
    another closed with 12 including a URL and an ebook edition number.

    Distinct from strip_known_front_matter, which handles one fan-compiler's
    "Manifesto." template. This is the publisher pattern, and it appears at
    both ends rather than only the front.

    Only runs anchored at an end are removed, never a match in the middle, and
    the whole book is never stripped. Returns the text and what was removed.
    """
    ranges = list(iter_source_paragraph_ranges(text))
    paragraphs = [text[start:end] for start, end in ranges]
    if len(paragraphs) < 8:
        return text, {"front_paragraphs": 0, "back_paragraphs": 0}
    limit = min(len(paragraphs) // 2,
                max(_MIN_COLOPHON_WINDOW,
                    int(len(paragraphs) * _MAX_COLOPHON_SHARE)))
    front = _colophon_run(paragraphs, limit)
    back = _colophon_run(paragraphs, limit, reverse=True)
    if front + back > len(paragraphs) * 0.8:
        # Nearly all of it looks like a colophon: a broken or truncated source,
        # not a book with front matter. Leave it for the caller's gate to
        # reject rather than silently emptying it.
        return text, {"front_paragraphs": 0, "back_paragraphs": 0}
    start = ranges[front][0]
    end = ranges[len(ranges) - back - 1][1]
    return text[start:end], {"front_paragraphs": front, "back_paragraphs": back}


_NORMALIZATION_STAGE_ORDER = {
    "stripped_apostrophe": 0,
    None: 1,
    "illustration_caption": 2,
    "homoglyph": 3,
    "extreme_phrase_repetition": 4,
}


@lru_cache(maxsize=256)
def _recorded_change_pattern(before_norm):
    """Exact whitespace-flexible pattern for a recorded change's ``before`` text."""
    parts = [re.escape(part) for part in before_norm.split(" ")]
    pattern = r"\s+".join(parts)
    if re.match(r"[^\W_]", before_norm[0], re.UNICODE):
        pattern = r"(?<![^\W_])" + pattern
    if re.match(r"[^\W_]", before_norm[-1], re.UNICODE):
        pattern = pattern + r"(?![^\W_])"
    return re.compile(pattern, re.UNICODE)


def _get_repetition_unit(before_norm, phrase_words=None):
    """The repeated token sequence of a recorded repetition run, or None.

    The record's own ``before`` text is authoritative; ``phrase_words`` only
    narrows the search. A run that is not a whole number of identical units is
    not a recorded repetition change and is refused.
    """
    tokens = before_norm.split(" ")
    if len(tokens) < 2:
        return None
    if type(phrase_words) is int:
        widths = (phrase_words,)
    else:
        widths = range(1, EXTREME_PHRASE_MAX_WORDS + 1)
    for width in widths:
        if width < 1 or width > EXTREME_PHRASE_MAX_WORDS:
            continue
        if len(tokens) % width or len(tokens) // width < EXTREME_PHRASE_REPEAT_MIN:
            continue
        if all(tokens[position].casefold() == tokens[position % width].casefold()
               for position in range(len(tokens))):
            return " ".join(tokens[:width])
    return None


@lru_cache(maxsize=256)
def _recorded_unit_neighbour_patterns(unit):
    """Patterns that find one more repetition of ``unit`` on either side.

    Case-insensitive because the repetition normalizer compares casefolded
    words: "hello Hello hello ..." is one run there, so it is one run here.
    """
    pattern = _recorded_change_pattern(unit).pattern
    return (re.compile(r"(?:" + pattern + r")\s*$", re.IGNORECASE | re.UNICODE),
            re.compile(r"\s+(?:" + pattern + r")", re.IGNORECASE | re.UNICODE))


def _is_whole_repetition_run(text, match, unit):
    """True when ``match`` covers a whole repetition run, not part of a longer one.

    A recorded 20-repetition change must not consume the first 20 of a 21-
    repetition run: the normalizer records each run's actual length, so a
    partial match is an unrecorded difference and must stay refused.
    """
    before_pattern, after_pattern = _recorded_unit_neighbour_patterns(unit)
    if before_pattern.search(text[:match.start()]):
        return False
    return after_pattern.match(text[match.end():]) is None


def get_valid_recorded_normalizations(changes):
    """Validate recorded source normalizations and return them in pipeline stage order.

    Returns ``[(original_index, normalized_before, normalized_after, unit), ...]``
    where ``unit`` is the repeated token sequence for an
    ``extreme_phrase_repetition`` record and None otherwise. Unrecognized
    rules, malformed shapes, forged entries that do not match their rule's
    contract, and exact duplicate records (same offset, rule, before and
    after) are rejected so an unrecorded mismatch is never admitted and one
    recorded change cannot authorize two occurrences.
    """
    if not isinstance(changes, (list, tuple)):
        return []
    ordered = []
    seen = set()
    for index, change in enumerate(changes):
        if not isinstance(change, dict):
            continue
        rule = change.get("rule")
        if rule not in _NORMALIZATION_STAGE_ORDER:
            continue
        before = change.get("before")
        after = change.get("after")
        if not isinstance(before, str) or not isinstance(after, str) or before == after:
            continue
        identity = (change.get("offset"), rule, before, after)
        if identity in seen:
            continue
        seen.add(identity)
        before_norm = " ".join(before.split())
        if not before_norm:
            continue
        after_norm = " ".join(after.split()) if after else ""
        if before_norm == after_norm:
            continue
        unit = None
        if rule is None:
            expected = KNOWN_SOURCE_CORRUPTIONS.get(before_norm.casefold())
            if expected is None or after_norm.casefold() != expected:
                continue
        elif rule == "extreme_phrase_repetition":
            if (not after_norm.endswith("…") or len(after_norm) < 2
                    or not before_norm.startswith(after_norm[:-1])
                    or normalize_extreme_phrase_repetitions(before_norm)[0] != after_norm):
                continue
            unit = _get_repetition_unit(before_norm, change.get("phrase_words"))
            if unit is None:
                continue
        elif rule == "homoglyph":
            if "".join(_HOMOGLYPH_MAP.get(ch, ch) for ch in before_norm) != after_norm:
                continue
        elif rule == "illustration_caption":
            if after_norm != "" or _ILLUSTRATION_CAPTION_RE.fullmatch(before_norm) is None:
                continue
        elif rule == "stripped_apostrophe":
            if before_norm.count(" ") != 1 or before_norm.replace(" ", "'") != after_norm:
                continue
        ordered.append((_NORMALIZATION_STAGE_ORDER[rule], index, before_norm, after_norm, unit))
    # Longest first within a stage: a shorter recorded run must never win a
    # match that a longer recorded run owns (the whole-run check above also
    # refuses a partial match, so this is a deterministic tie-break).
    ordered.sort(key=lambda item: (item[0], -len(item[2].split(" ")), -len(item[2]), item[1]))
    return [(index, before_norm, after_norm, unit)
            for _stage, index, before_norm, after_norm, unit in ordered]


def map_recorded_source_normalizations(text, changes, *, consumed=()):
    """Map ``text`` through unconsumed recorded source normalizations.

    One shared mapping helper (Rule 15) for recorded preprocessing rewrites
    (``extreme_phrase_repetition``, ``homoglyph``, ``illustration_caption``,
    ``stripped_apostrophe``, and known source corruptions). ``consumed`` is
    input-only: each record whose index is already there is skipped, and the
    caller commits the returned ``used_indices`` only once the mapped span
    matches (Rule 17). Records are matched by their recorded text, not their
    ``offset``: offsets index the generation source at that preprocessing
    stage (an extracted ``.txt``), while this helper receives an EPUB spine
    segment, whose coordinates differ. ``offset`` is part of record identity
    only, so a byte-identical copy of a valid record with a changed or missing
    ``offset`` is not distinguished here; it still authorizes only a collapse
    the normalizer itself produces on that exact text.
    """
    if not isinstance(text, str) or not text or not changes:
        return text, ()
    used_already = set(consumed or ())
    current = text
    used = []
    for index, before_norm, after_norm, unit in get_valid_recorded_normalizations(changes):
        if index in used_already:
            continue
        match = None
        for candidate in _recorded_change_pattern(before_norm).finditer(current):
            if unit is None or _is_whole_repetition_run(current, candidate, unit):
                match = candidate
                break
        if match is None:
            continue
        current = current[:match.start()] + after_norm + current[match.end():]
        used.append(index)
    if used:
        current = " ".join(current.split())
    return current, tuple(used)
