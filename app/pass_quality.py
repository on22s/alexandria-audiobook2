"""Validators for the three-pass generation flow (segment / attribute /
instruct). Segment reuses recall_core for source-fidelity scoring; attribute and
instruct enforce a hard per-entry text freeze (they may only add fields)."""

import copy
import hashlib
import json
import math
import re
import unicodedata

from recall_core import tokens, ngrams, counter_recall
from review_script import normalize_text
from silent_word_characters import (SILENT_WORD_CHARACTERS,
                                    remove_silent_word_characters,
                                    remove_silent_word_characters_with_offsets)
# Import the fidelity thresholds and introduced-character check from the
# single-pass gate so pass 1 tracks any recalibration there automatically
# (e.g. the 0.84->0.82 near-miss floor history) instead of drifting.
from chunk_quality import (MIN_SOURCE_TOKEN_RECALL, MIN_ORDERED_TRIGRAM_RECALL,
                           MIN_OUTPUT_SOURCE_RATIO, MAX_OUTPUT_SOURCE_RATIO,
                           get_introduced_non_ascii_letters,
                           get_unsupported_cyrillic_characters, get_missing_source_runs)
from script_preflight import find_adjacent_duplicate_blocks, find_seam_duplicate_narration

_VALID_SEGMENT_TYPES = {"NARRATOR", "SPOKEN"}
# Derive every quote-mark set from dialogue_spans' single PAIRED table so the
# source-map and the generator's segmenter cannot drift apart.
from dialogue_spans import (PAIRED as _PAIRED, QUOTE_MARKS as _DS_QUOTE_MARKS,
                            dash_line_unverified_prefixes as _dash_prefixes,
                            dash_line_unverified_ranges as _dash_ranges,
                            get_guillemet_direction as _get_guillemet_direction,
                            is_inch_mark as _is_inch_mark,
                            is_number_char as _is_number_char,
                            split_dash_line as _split_dash_line)

_QUOTE_CHARS = set(_DS_QUOTE_MARKS) | {'”'}
# The marks analyze_outer_quote_regions opens a spoken region on; "does this
# chunk contain any" is the question the quotes-only refusal asks.
QUOTE_MARKS = tuple(dict.fromkeys(opener for opener, _closer in _PAIRED))
_CURLY_AND_JAPANESE_OPEN = set(QUOTE_MARKS) - {'"', '＂', '«', '»'}
# A finished sentence before a second opening quote marks a run-on quote (#624).
_SENTENCE_END = set('.!?…。！？')
_CURLY_AND_JAPANESE_CLOSE = ({closer for _opener, closer in _PAIRED
                              if closer not in ('"', '＂', '«', '»')} | {'”'})
_SELF_PAIRED = {'"', '＂'}


_REPORTING_VERBS = (
    r"(?:murmured|whispered|said|replied|answered|asked|cried|shouted|exclaimed)"
)
# English-only, and deliberately separate from
# `dialogue_spans._DASH_REPORTING_VERBS`: this one repairs a reporting verb
# beside PAIRED quotes (prefix/suffix inference, #1216), where the verb list
# must stay conservative and monolingual. The dash-tag decision has its own
# multilingual, closed table there (#2390 review 3 nit 10). Changing one does
# not change the other, by design.
_REPORTING_VERB_TAIL = re.compile(
    rf"\b{_REPORTING_VERBS},?\s+([^\n]{{1,120}})$", re.IGNORECASE)
_SOURCE_LABEL_TAIL = re.compile(r"(?:^|\n\n)([^\n]{1,60})$")
_METADATA_PARAGRAPH = re.compile(
    r"^(?:Light Novel Adaptation found in|Original Web Novel Chapter|"
    r"Original Translation by)\b", re.IGNORECASE)


def _is_metadata_paragraph(current):
    paragraph = "".join(current).rsplit("\n\n", 1)[-1].strip()
    return bool(_METADATA_PARAGRAPH.match(paragraph))


def _pop_source_label(current):
    value = "".join(current).rstrip()
    match = _SOURCE_LABEL_TAIL.search(value)
    if not match:
        return None
    candidate = remove_silent_word_characters(match.group(1).strip().rstrip(":"))
    words = candidate.split()
    if (not 1 <= len(words) <= 5 or
            not all(word == "???" or word[:1].isupper() for word in words) or
            (len(words) > 2 and candidate.isupper()) or
            any(char in candidate for char in ".!,;")):
        return None
    del current[match.start(1):]
    return candidate


def analyze_outer_quote_regions(text, initial_depth=0, allow_open_end=False):
    """Split source into narrator/spoken regions using outer quote boundaries.

    Curly quotes may be nested. Some source dialogue also uses an inner opening
    curly quote but shares the paragraph's only closing mark with the outer
    quote; in that case the inner mark is treated as decoration, not a new
    nesting level. Quote delimiters are intentionally omitted from speakable
    text; a straight mark after a number is an inch mark, not a delimiter, and
    stays in the region.
    """
    regions, current, pending_source_label = [], [], None
    depth, saw_quote, repairs = initial_depth, bool(initial_depth), []
    open_stack = []  # `“` closes „…“ but opens “…”; track which mark is open
    outer_opener = None  # the mark that opened depth 1, for the inch predicate
    german_inner = False  # inside an English quote nested in a German speech
    skip_until = -1  # consumed by a line-dash dialogue region
    unverified_dash = False  # dash line had interior break without tag evidence

    # One guillemet direction per text: an opposite-direction closer must not
    # be parsed as an opener (HIGH in the app-wide review). Decided by the
    # same function the source map uses, so the two cannot disagree.
    guillemet_open, guillemet_close = _get_guillemet_direction(text)
    openers = set(_CURLY_AND_JAPANESE_OPEN)
    closers = set(_CURLY_AND_JAPANESE_CLOSE)
    if guillemet_open:
        openers.add(guillemet_open)
        closers.add(guillemet_close)
    # A dash-only book uses line dashes for dialogue; a book with any quote
    # marks uses those and treats a stray dash as narration punctuation.
    dash_is_dialogue = not any(mark in text for mark in QUOTE_MARKS)
    quote_positions = [i for i, c in enumerate(text) if c in _QUOTE_CHARS]
    quote_at = {i: c for i, c in enumerate(text) if c in _QUOTE_CHARS}

    def next_quote_after(index):
        from bisect import bisect_right
        at = bisect_right(quote_positions, index)
        return quote_at[quote_positions[at]] if at < len(quote_positions) else None

    def flush():
        nonlocal pending_source_label
        part = "".join(current).strip()
        if part:
            entry = {"type": "SPOKEN" if depth else "NARRATOR", "text": part}
            if depth and pending_source_label:
                entry["source_label"] = pending_source_label
                pending_source_label = None
            regions.append(entry)
        current.clear()

    for index, char in enumerate(text):
        if index < skip_until:
            continue
        if (dash_is_dialogue and not depth and char in "—–"
                and not text[text.rfind("\n", 0, index) + 1:index].strip(" \t")):
            # Continental line-dash dialogue: split at evidenced tags via the
            # single shared rule in dialogue_spans (#2329).
            flush()
            line_end = text.find("\n", index)
            if line_end == -1:
                line_end = len(text)
            parts, ambiguous = _split_dash_line(text, index + 1, line_end)
            if ambiguous:
                unverified_dash = True
            for part_type, part_start, part_end in parts:
                part_text = text[part_start:part_end].strip()
                if part_text:
                    regions.append({"type": part_type, "text": part_text})
                    saw_quote = True
            skip_until = line_end
            continue
        if char == "\n" and text[index:index + 2] == "\n\n" and depth:
            flush()
            current.append(char)
            continue
        if not depth and open_stack:
            # Nothing is open outside speech. A generic close (`”` ending a
            # Polish `„…”`) leaves its opener here, and a stale `„` made the
            # next English “…” read as a nested German quote: its speech went
            # to NARRATOR, or the narration before it to SPOKEN.
            open_stack.clear()
        if char == "“" and open_stack and open_stack[-1] == "„":
            # `“` closes German, but also opens English. If a `”` follows it
            # next, this is an English quote nested inside the German speech.
            if next_quote_after(index) == "”":
                # An English quote nested in a German speech is decoration:
                # neither mark changes the region depth.
                german_inner = True
                continue
            if depth == 1:
                flush()
            depth -= 1
            if depth == 0:
                outer_opener = None
            open_stack.pop()
            continue
        if char in openers or char in _SELF_PAIRED:
            paragraph_start = text.rfind("\n\n", 0, index)
            paragraph_start = paragraph_start + 2 if paragraph_start >= 0 else 0
        if char in openers:
            # Source credits sometimes quote a chapter title. It is metadata,
            # not unattributed dialogue, and must remain narrator material.
            if depth == 0 and _is_metadata_paragraph(current):
                saw_quote = True
                continue
            if depth == 0:
                # Printed-label attribution is a convention of “ ” / 「 」 /
                # 『 』 (and straight quotes). For low-high, full-width and
                # guillemets a preceding capital word is a reporting verb
                # ("Powiedział", "Nein"), not a speaker name.
                if char in ('"', '“', '「', '『'):
                    pending_source_label = _pop_source_label(current)
                flush()
                depth = 1
                outer_opener = char
                saw_quote = True
            elif not text[paragraph_start:index].strip():
                # Publishers repeat an opening quote at the start of each
                # paragraph in one continuous, multi-paragraph speech.
                saw_quote = True
            elif char == '“':
                head = "".join(current).rstrip()
                if depth == 1 and head and head[-1] in _SENTENCE_END and text[index - 1].isspace():
                    # The source dropped a closing quote: a second opening
                    # quote after a finished sentence and a space starts a
                    # NEW speech (#624). Nesting it swallowed the narration
                    # up to the next closer as SPOKEN, rejecting the right
                    # labelling and accepting the wrong one.
                    flush()
                    repairs.append({"code": "inferred_missing_close_quote",
                                    "position": index})
                    pending_source_label = None
                    saw_quote = True
                    continue
                paragraph_tail = text[index + 1:].split("\n\n", 1)[0]
                if paragraph_tail.count('”') >= 2:
                    depth += 1
            else:
                depth += 1
            if char in ("„", "“") or char == guillemet_open:
                open_stack.append(char)
            continue
        if german_inner and char == '”':
            german_inner = False
            continue
        if char in closers and depth:
            # A closer-shaped mark after a number can be an inch mark instead;
            # the shared predicate settles it from the open speech's opener
            # (#2314).
            if _is_inch_mark(text, index, char, True, opener=outer_opener):
                current.append(char)
                saw_quote = True
                continue
            if char == '”' and depth == 1 and not german_inner:
                paragraph_tail = text[index + 1:].split("\n\n", 1)[0]
                next_close = paragraph_tail.find('”')
                next_open = paragraph_tail.find('“')
                if (next_close >= 0 and (next_open < 0 or next_close < next_open)
                        and not _is_inch_mark(text, index + 1 + next_close, '”')):
                    # Stylized cries may use one opening mark and decorative
                    # closers between fragments: “Ha!”Haha!”Hahaha!” An inch
                    # mark later in the narration is not one of those closers,
                    # so the speech closes at this mark (6” falls in narration).
                    continue
            if char == '”' and open_stack and open_stack[-1] == '“':
                # Close the nested English pair before continuing.
                if depth == 1:
                    flush()
                depth -= 1
                if depth == 0:
                    outer_opener = None
                open_stack.pop()
                continue
            if char == guillemet_close and open_stack and open_stack[-1] == guillemet_open:
                if depth == 1:
                    flush()
                depth -= 1
                if depth == 0:
                    outer_opener = None
                open_stack.pop()
                continue
            if depth == 1:
                flush()
            depth -= 1
            if depth == 0:
                outer_opener = None
            continue
        if char == '”' and _is_inch_mark(text, index, char):
            # Outside speech a `”` after a number is an inch mark (`6” wide`);
            # it neither closes nor needs the unmatched-closer repair (#2314).
            current.append(char)
            saw_quote = True
            continue
        if char == '”':
            # Bounded recovery for a dropped opening delimiter in prose such as
            # "she murmured I see…”, keeping her eyes lowered". Only a short,
            # same-line phrase immediately following a reporting verb is safe
            # enough to infer; all other stray closers remain visible so the
            # quality gate fails closed.
            match = _REPORTING_VERB_TAIL.search("".join(current))
            if match:
                spoken = match.group(1).strip()
                del current[match.start(1):]
                flush()
                regions.append({"type": "SPOKEN", "text": spoken})
                repairs.append({"code": "inferred_missing_open_quote",
                                "offset": index, "text": spoken})
                saw_quote = True
                continue
            repairs.append({"code": "ignored_unmatched_close_quote",
                            "offset": index})
            saw_quote = True
            continue
        if char in _SELF_PAIRED:
            # A straight mark after a number can be an inch mark (6" wide),
            # not a delimiter; the shared predicate also settles the
            # inside-speech case. It is text, so it stays in the region
            # (#2314).
            if _is_inch_mark(text, index, char, bool(depth),
                             opener=outer_opener):
                current.append(char)
                saw_quote = True
                continue
            if depth == 0 and _is_metadata_paragraph(current):
                saw_quote = True
                continue
            if depth and not text[paragraph_start:index].strip():
                saw_quote = True
                continue
            if depth == 0:
                pending_source_label = _pop_source_label(current)
            flush()
            if depth:
                depth = 0
                outer_opener = None
            else:
                depth = 1
                outer_opener = char
            saw_quote = True
            continue
        current.append(char)
    if depth and not allow_open_end:
        flush()
        repairs.append({"code": "inferred_missing_close_quote",
                        "offset": len(text)})
        depth = 0
        outer_opener = None
    else:
        flush()
    complete = depth == 0 or allow_open_end
    result = {"regions": regions if saw_quote and complete else [],
              "repairs": repairs, "initial_depth": initial_depth,
              "final_depth": depth}
    if unverified_dash:
        result["unverified_dash"] = True
        # The gate can still vouch for the opening speech of each unverified
        # line (#2329 finding 5); the rest of the line is unchecked.
        result["unverified_dash_prefixes"] = _dash_prefixes(text)
    return result


def split_outer_quote_regions(text):
    """Return only regions for callers that do not need repair telemetry."""
    return analyze_outer_quote_regions(text)["regions"]


_LEXICAL_LABEL = r"(?:word|term|title|grade|rank|concept|skill|spell)"
_REPORTING_SUBJECT = (
    r"(?:(?i:he|she|they|i|we|you)|"
    r"(?i:the)\s+(?:[\w’'-]+\s+){0,3}[\w’'-]+|"
    r"[A-Z][\w’'-]*(?:\s+[A-Z][\w’'-]*){0,3})"
)
_DIALOGUE_QUOTE_PREFIX = re.compile(
    rf"\b{_REPORTING_VERBS}\b(?:\s+[\w’'-]+ly)?"
    rf"(?:\s+(?:the\s+)?{_LEXICAL_LABEL})?\s*[,;:]?\s*$", re.IGNORECASE)
_ACTIVE_CALLED_QUOTE_PREFIX = re.compile(
    r"\b(?:(?i:i|you|he|she|we|they)|"
    r"[A-Z][\w’'-]*(?:\s+[A-Z][\w’'-]*){0,3})"
    r"(?:\s+[\w’'-]+ly)?\s+called\s*[,;:]?\s*$")
_CALLED_QUOTE_PREFIX = re.compile(r"\bcalled\s*[,;:]?\s*$", re.IGNORECASE)
_DIALOGUE_QUOTE_SUFFIX = re.compile(
    rf"^\s*[,;:]?\s*(?:{_REPORTING_SUBJECT}\s+"
    rf"(?:[\w’'-]+ly\s+)?(?i:{_REPORTING_VERBS})\b|"
    rf"(?i:{_REPORTING_VERBS})\s+{_REPORTING_SUBJECT}\b)")
_LEXICAL_RELATION_CONTEXT = re.compile(
    r"(?:\b(?:known|classified)\s+as|\breferred\s+to\s+as|"
    r"\b(?:rated|ranked)(?:\s+as)?|"
    r"\b(?:called|named)|\bmeans)"
    r"(?:\s+(?:a|an|the))?\s*$", re.IGNORECASE)
_LEXICAL_LABEL_CONTEXT = re.compile(
    rf"\b(?:the\s+)?{_LEXICAL_LABEL}(?:\s+(?:is|was|of))?\s*$",
    re.IGNORECASE)
_LEXICAL_USAGE_CONTEXT = re.compile(
    r"(?:\b(?:his|her|their)|\b(?:use|uses|using))"
    r"\s*$", re.IGNORECASE)
_LEXICAL_QUOTE_SUFFIX = re.compile(
    r"^[ \t]+(?:magic|skill|spell|ability|technique|concept)\b")
_QUOTED_CLAUSE_END = re.compile(r"[.!?…。！？,;:，、]\s*$")


def _get_quote_role(prefix, quoted_text, suffix):
    """Return a role only when grammar strongly identifies a lexical quote."""
    if (_DIALOGUE_QUOTE_PREFIX.search(prefix) or
            _ACTIVE_CALLED_QUOTE_PREFIX.search(prefix) or
            (_CALLED_QUOTE_PREFIX.search(prefix) and
             _QUOTED_CLAUSE_END.search(quoted_text)) or
            _DIALOGUE_QUOTE_SUFFIX.search(suffix)):
        return None
    if (_LEXICAL_RELATION_CONTEXT.search(prefix) or
            _LEXICAL_LABEL_CONTEXT.search(prefix) or
            _LEXICAL_USAGE_CONTEXT.search(prefix)):
        return "LEXICAL_QUOTE"
    if (not _QUOTED_CLAUSE_END.search(quoted_text) and
            _LEXICAL_QUOTE_SUFFIX.search(suffix)):
        return "LEXICAL_QUOTE"
    return None


def classify_lexical_quote_regions(source_text, quote_analysis):
    """Classify only strongly signalled quoted reference terms as narration."""
    regions = []
    quoted = list(re.finditer(r'["“「『]([^"”」』\n]+)["”」』]', source_text))
    quote_cursor = 0
    for region in quote_analysis.get("regions", []):
        updated = dict(region)
        if region.get("type") == "SPOKEN":
            target = normalize_text(region.get("text") or "")
            for match in quoted:
                if match.start() < quote_cursor:
                    continue
                if normalize_text(match.group(1)) != target:
                    continue
                prefix = source_text[max(0, match.start() - 100):match.start()]
                suffix = source_text[match.end():match.end() + 100]
                quote_role = _get_quote_role(prefix, match.group(1), suffix)
                if quote_role:
                    updated["type"] = "NARRATOR"
                    updated["quote_role"] = quote_role
                quote_cursor = match.end()
                break
        regions.append(updated)
    return {**quote_analysis, "regions": regions}


def _split_quote_regions(source_text):
    """Return normalized text regions outside and inside outer dialogue quotes."""
    regions = split_outer_quote_regions(source_text)
    outside = [entry["text"] for entry in regions
               if entry["type"] == "NARRATOR"]
    inside = [entry["text"] for entry in regions
              if entry["type"] == "SPOKEN"]
    return outside, inside


def _number_mark_token(text, position):
    """-> the number and mark ending at `position`, e.g. `12"` or `8.5”`."""
    start = position
    while start > 0 and (_is_number_char(text[start - 1])
                         or text[start - 1] in ".,"):
        start -= 1
    return text[start:position + 1]


def _region_has_number_mark(region, token):
    """Does `region` hold `token` as a whole number and mark?

    A plain substring test matched `5"` inside `15"`, so an inch-mark row was
    treated as still present only because a longer measurement elsewhere
    contained the same digits, and the finding became crosses_quote_boundary
    instead of mixed_quote_region (#2318). The boundary is the one
    `_number_mark_token` uses to extend a number: another digit or `.`/`,`
    directly before the match makes it part of that longer number.
    """
    start = region.find(token)
    while start != -1:
        before = region[start - 1] if start else ""
        if not before or (before not in ".," and not _is_number_char(before)):
            return True
        start = region.find(token, start + 1)
    return False


def _has_quote_delimiter(text, region_texts):
    """True unless every quote character in `text` is a kept inch mark.

    An inch mark after a number (`6" wide`, `6” wide`) is speakable text, so
    a row carrying only those is not mixed (#2314). It counts as text only
    when the same number+mark appears in the analyzed region, which keeps
    measurements and drops delimiters: a row that kept a real closer after a
    digit has no such counterpart and stays mixed, as it was before #2314.
    """
    for position, char in enumerate(text):
        if char not in _QUOTE_CHARS:
            continue
        token = _number_mark_token(text, position)
        if (_is_inch_mark(text, position, char)
                and any(_region_has_number_mark(region, token)
                        for region in region_texts)):
            continue
        return True
    return False


def _region_contains_entry(regions, entry_text):
    needle = normalize_text(entry_text).split()
    if not needle:
        return True
    for region in regions:
        haystack = normalize_text(region).split()
        if any(haystack[i:i + len(needle)] == needle
               for i in range(len(haystack) - len(needle) + 1)):
            return True
    return False


def _contiguous_spoken_groups(entries):
    """-> runs of consecutive SPOKEN entries as one text per run.

    A NARRATOR entry between two SPOKEN ones breaks the run: the model put
    narration there, so the words are no longer contiguous dialogue.
    """
    groups, current = [], []
    for entry in entries:
        if isinstance(entry, dict) and entry.get("type") == "SPOKEN":
            current.append(str(entry.get("text") or ""))
        elif current:
            groups.append(" ".join(current))
            current = []
    if current:
        groups.append(" ".join(current))
    return groups


def _entry_in_unverified_tail(entry_text, source_text, ranges):
    """Is this entry inside text the segmenter could not vouch for?

    Only containment of the ENTRY inside the tail is left to the model: a
    model may sentence-split an unverified tail, and each piece is unchecked.
    A tail inside the entry is not skipped (#2390 review 4 finding 1): an
    entry that merges a confident line with an unverified tail still contains
    vouched text, so the quote-region gate must validate it, or a confident
    tag in the merged entry would escape validation. That is the same class as
    the rev3 chunk-wide downgrade, one entry narrower.
    """
    needle = normalize_text(entry_text)
    if not needle:
        return True
    for start, end in ranges:
        tail = normalize_text(source_text[start:end])
        if tail and needle in tail:
            return True
    return False


def _quote_region_findings(source_text, entries, quote_analysis=None,
                           quoted_must_be_spoken=True,
                           unquoted_must_be_narrator=True):
    """Return (findings, quote_gate) with telemetry local to this validation.

    NOT A FINDING - TELEMETRY. A narration-only chunk legitimately contains no
    quotes and must still pass, so the skip cannot block. It is recorded in the
    report as `quote_gate` instead, which is the difference between "checked
    and clean" and "did not check".

    THE EARLY RETURN USED TO BE SILENT. A source with no quote characters got
    `[]`, which every caller reads as "no problems found" - indistinguishable
    from a book that passed. This gate only understands paired quotes, but
    `dialogue_spans` knows two other conventions a novel can use (a leading
    em-dash, and a script-style `NAME:` label), and on such a book the gate
    would quietly check nothing at all.

    Measured 2026-08-20 over all 85 sources on this machine - the light
    novels, the saved library and PDNC - every one uses paired quotes, so this
    is latent rather than live. It is fixed anyway because a guard that
    disables itself without saying so is the shape that has already cost this
    project three separate measurements ([[Rule 8]]).
    """
    unverified_prefixes = None
    unverified_ranges = []
    if quote_analysis and quote_analysis.get("unverified_dash"):
        unverified_prefixes = (quote_analysis.get("unverified_dash_prefixes")
                               or _dash_prefixes(source_text))
        # The downgrade is per LINE, not per chunk: only the ambiguous line's
        # unverified tail is unchecked, and every confident line in the same
        # chunk keeps full region validation (#2390 review 3 finding 3).
        unverified_ranges = _dash_ranges(source_text)
    if quote_analysis and not quote_analysis.get("regions"):
        quote_analysis = None
    if not quote_analysis and unverified_prefixes is None and \
            not any(char in source_text for char in _QUOTE_CHARS):
        # A dash book with no evidenced split is unchecked, as before. An
        # unverified dash line is not: its opening speech is still checked.
        unverified_prefixes = _dash_prefixes(source_text) or None
        if unverified_prefixes is None:
            try:
                from dialogue_spans import detect_convention
                convention = detect_convention(source_text)
            except Exception as exc:                        # noqa: BLE001
                return [], "skipped:error:" + type(exc).__name__
            return [], "skipped:" + (convention or "none_detected")
    findings = []
    if unverified_prefixes is not None:
        # Only the text before the first interior break is unambiguous; the
        # model owns the rest of an unverified line (#2329 finding 5). A model
        # may split that opening speech into consecutive SPOKEN entries, so the
        # prefix passes when its words lie in order across them (#2390 review 2).
        for prefix in unverified_prefixes:
            if not prefix or any(_region_contains_entry([group], prefix)
                                 for group in _contiguous_spoken_groups(entries)):
                continue
            entry_number = next(
                (number for number, entry in enumerate(entries, 1)
                 if isinstance(entry, dict)
                 and _region_contains_entry([str(entry.get("text") or "")],
                                            prefix)), 1)
            findings.append({
                "code": "quote_region_misclassified", "entry_number": entry_number,
                "message": "The opening speech of an unverified dash line must be SPOKEN; the rest of the line is unchecked."})
    if not quote_analysis:
        if unverified_prefixes is not None:
            return findings, "ran"
        outside, inside = _split_quote_regions(source_text)
    else:
        source_regions = quote_analysis["regions"]
        outside = [entry["text"] for entry in source_regions
                   if entry["type"] == "NARRATOR"]
        inside = [entry["text"] for entry in source_regions
                  if entry["type"] == "SPOKEN"]
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            continue
        entry_type = entry.get("type")
        enforce_entry = (quoted_must_be_spoken if entry_type == "NARRATOR"
                         else unquoted_must_be_narrator)
        if not enforce_entry:
            continue
        text = str(entry.get("text") or "")
        if unverified_ranges and _entry_in_unverified_tail(
                text, source_text, unverified_ranges):
            continue
        expected = inside if entry.get("type") == "SPOKEN" else outside
        other = outside if entry.get("type") == "SPOKEN" else inside
        if _has_quote_delimiter(text, expected + other):
            findings.append({"code": "mixed_quote_region", "entry_number": number,
                             "message": "An entry combines quote delimiters with narration or dialogue; split at the quote boundary."})
            continue
        in_expected = _region_contains_entry(expected, text)
        if not in_expected and _region_contains_entry(other, text):
            findings.append({"code": "quote_region_misclassified", "entry_number": number,
                             "message": "SPOKEN text must originate inside quotes and NARRATOR text outside quotes."})
        elif not in_expected:
            findings.append({"code": "crosses_quote_boundary", "entry_number": number,
                             "message": "Entry text crosses a dialogue quote boundary; split narration and spoken text."})
    return findings, "ran"


def _introduced_character_findings(source_text, output_text, entries, quote_analysis=None,
                                   quoted_must_be_spoken=True,
                                   unquoted_must_be_narrator=True):
    """Mirror the single-pass gate's introduced-character + adjacent-duplicate
    checks so pass 1 has the same fidelity bar (unsupported_cyrillic,
    unsupported_unicode_character, source_unsupported_duplicate). Text-only, so
    it is shape-agnostic across {type,text} and {speaker,text,instruct}."""
    findings = []
    unsupported = get_unsupported_cyrillic_characters(source_text, output_text)
    if unsupported:
        findings.append({"code": "unsupported_cyrillic", "characters": unsupported,
                         "message": "Response introduced Cyrillic characters absent from the source."})
    introduced = get_introduced_non_ascii_letters(source_text, output_text)
    if introduced:
        findings.append({"code": "unsupported_unicode_character",
                         "characters": [{"character": char, "codepoint": f"U+{ord(char):04X}",
                                         "name": unicodedata.name(char, "UNKNOWN")}
                                        for char in introduced],
                         "message": "Response introduced non-ASCII letters absent from the source."})
    entry_texts = [" ".join(str(e.get("text") or "").split()).casefold()
                   if isinstance(e, dict) else "" for e in entries]
    for duplicate in find_adjacent_duplicate_blocks(entry_texts, source_text):
        if duplicate["severity"] == "blocking":
            findings.append({"code": "source_unsupported_duplicate",
                             "entry_numbers": duplicate["entry_numbers"],
                             "message": "An adjacent repeated block is not repeated in the source."})
    # Role-specific accounting is only sound when both quote restrictions
    # force source and output roles to agree. Under a permissive policy a
    # quoted source occurrence can be emitted inside a narrator row, so use
    # compatible whole-source / whole-output counts instead.
    role_specific = quoted_must_be_spoken and unquoted_must_be_narrator
    source_narration_text = None
    if role_specific:
        if (quote_analysis or {}).get("unverified_dash"):
            quote_regions = []
        else:
            quote_regions = (quote_analysis or {}).get("regions")
            if not quote_regions:
                analyzed = analyze_outer_quote_regions(source_text)
                quote_regions = [] if analyzed.get("unverified_dash") else analyzed.get("regions", [])
        source_narration_text = " ".join(region["text"] for region in quote_regions
                                         if region.get("type") == "NARRATOR") or source_text
    for duplicate in find_seam_duplicate_narration(
            source_text, entries, quote_analysis=quote_analysis,
            role_specific=role_specific, source_narration_text=source_narration_text):
        if duplicate["severity"] == "blocking":
            findings.append({"code": "source_unsupported_duplicate",
                             "entry_numbers": duplicate["entry_numbers"],
                             "message": duplicate["message"],
                             "details": duplicate["details"]})
    return findings


def validate_segment_quality(source_text, entries, quote_analysis=None, *,
                             quoted_must_be_spoken=True,
                             unquoted_must_be_narrator=True):
    """Fidelity gate for pass 1 output [{type, text}]. Same recall/trigram math
    as the single-pass gate, but validates the segment shape (type in
    {NARRATOR, SPOKEN}) rather than speaker/instruct."""
    findings = []
    if not isinstance(entries, list) or not entries:
        return _report(0, 0, 0.0, 0.0, 0.0,
                       [{"code": "missing_entries", "message": "Response contains no entries."}])
    output_parts = []
    for number, entry in enumerate(entries, 1):
        if not isinstance(entry, dict):
            findings.append({"code": "invalid_entry", "entry_number": number,
                             "message": "Entry must be a JSON object."})
            continue
        missing = [k for k in ("type", "text") if k not in entry]
        if missing:
            findings.append({"code": "missing_fields", "entry_number": number,
                             "fields": missing, "message": "Entry is missing required fields."})
        if not isinstance(entry.get("type"), str) or entry["type"] not in _VALID_SEGMENT_TYPES:
            findings.append({"code": "invalid_type", "entry_number": number,
                             "value": entry.get("type"),
                             "message": "Each entry's type must be NARRATOR or SPOKEN."})
        text = str(entry.get("text") or "")
        if not text.strip():
            findings.append({"code": "empty_text", "entry_number": number,
                             "message": "Entry contains no speakable text."})
        output_parts.append(text)

    source_tokens = tokens(source_text)
    if any(isinstance(entry, dict) and entry.get("source_label") for entry in entries):
        source_regions = (quote_analysis or analyze_outer_quote_regions(source_text)).get("regions", [])
        if any(region.get("source_label") for region in source_regions):
            source_tokens = tokens(" ".join(region["text"] for region in source_regions))
    output_text = " ".join(output_parts)
    output_tokens = tokens(output_text)
    sc, oc = len(source_tokens), len(output_tokens)
    recall = counter_recall(source_tokens, output_tokens)
    trigram = counter_recall(ngrams(source_tokens, 3), ngrams(output_tokens, 3))
    ratio = oc / sc if sc else (1.0 if not oc else 0.0)
    if sc and recall < MIN_SOURCE_TOKEN_RECALL:
        findings.append({"code": "low_source_token_recall", "value": round(recall, 4),
                         "minimum": MIN_SOURCE_TOKEN_RECALL,
                         "message": "Too much source text is absent from the response."})
    if sc >= 3 and trigram < MIN_ORDERED_TRIGRAM_RECALL:
        findings.append({"code": "low_ordered_trigram_recall", "value": round(trigram, 4),
                         "minimum": MIN_ORDERED_TRIGRAM_RECALL,
                         "message": "Too many ordered source phrases are absent."})
    if sc and not MIN_OUTPUT_SOURCE_RATIO <= ratio <= MAX_OUTPUT_SOURCE_RATIO:
        findings.append({"code": "output_source_ratio", "value": round(ratio, 4),
                         "minimum": MIN_OUTPUT_SOURCE_RATIO, "maximum": MAX_OUTPUT_SOURCE_RATIO,
                         "message": "Output length is implausible for the source chunk."})
    # 90% recall still lets a whole clause disappear (#1921): report it so the
    # model is asked again with the missing words named.
    missing_runs = get_missing_source_runs(source_tokens, output_tokens) if sc else []
    if missing_runs:
        findings.append({"code": "missing_source_run",
                         "value": sum(span["token_count"] for span in missing_runs),
                         "message": "A run of source words is absent from the response."})
    quote_findings, quote_gate = _quote_region_findings(
        source_text, entries, quote_analysis,
        quoted_must_be_spoken=quoted_must_be_spoken,
        unquoted_must_be_narrator=unquoted_must_be_narrator)
    findings.extend(quote_findings)
    findings.extend(_introduced_character_findings(
        source_text, output_text, entries, quote_analysis=quote_analysis,
        quoted_must_be_spoken=quoted_must_be_spoken,
        unquoted_must_be_narrator=unquoted_must_be_narrator))
    report = _report(sc, oc, recall, trigram, ratio, findings,
                     quote_gate=quote_gate)
    report["missing_source_spans"] = missing_runs
    return report


def _report(source_count, output_count, recall, trigram, ratio, findings,
            quote_gate="ran"):
    return {
        "passed": not findings,
        # "ran" or "skipped:<convention>". A reader comparing two clean reports
        # can otherwise not tell which of them actually had its dialogue
        # checked, and the quote gate only understands paired quotes.
        "quote_gate": quote_gate,
        "metrics": {
            "source_tokens": source_count, "output_tokens": output_count,
            "source_token_recall": round(recall, 4),
            "ordered_trigram_recall": trigram,
            "output_source_ratio": round(ratio, 4),
        },
        "findings": findings,
    }


def get_alignment_counts(frozen_entries, response_entries):
    """Expected and returned row counts, recorded on an alignment finding so the
    retry policy can tell an overlong response from a truncated one."""
    return {"expected_entries": len(frozen_entries),
            "returned_entries": (len(response_entries)
                                 if isinstance(response_entries, list) else None)}


def coerce_index(value):
    """Return an unambiguous JSON index as int, matching model transport (#623).

    Integral floats and digit strings are accepted where an index is required;
    bools stay bool so callers can reject them explicitly.
    """
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        stripped = value.strip()
        if stripped.isdecimal():
            try:
                return int(stripped)
            except ValueError:
                return value
    return value


def index_head_check(frozen_entries, response_entries):
    """Return (ok, reason, ordered) for the immutable-index contract.

    Passes 2 and 3 provide a locally assigned integer ``n`` and accept exactly
    one response for every index. Text is never accepted back from the model.
    Older responses may still include ``head``; it is ignored because requiring
    the model to reproduce an anchor made otherwise-correct attribution fail on
    punctuation and repeated line openings."""
    k = len(frozen_entries)
    if not isinstance(response_entries, list) or len(response_entries) != k:
        got = len(response_entries) if isinstance(response_entries, list) else "non-list"
        return False, f"expected {k} entries, got {got}", None
    by_index = {}
    for item in response_entries:
        if not isinstance(item, dict):
            return False, "entry is not a JSON object", None
        n = coerce_index(item.get("n"))
        if isinstance(n, bool) or not isinstance(n, int) or not (0 <= n < k):
            return False, f"entry has a missing or out-of-range index n={n!r}", None
        if n in by_index:
            return False, f"duplicate index n={n}", None
        by_index[n] = item
    ordered = [by_index[i] for i in range(k)]
    return True, "", ordered


def get_attribution_shift_feedback_report(frozen_entries, response_entries, report):
    """Copy a failed native report, explaining a possible adjacent label shift.

    This is retry wording only. Neither the returned narration label nor an
    apparent speech tag establishes the quote's speaker; acceptance is unchanged.
    """
    result = copy.deepcopy(report)
    if report.get("passed") is not False:
        return result
    aligned, _, _ = index_head_check(frozen_entries, response_entries)
    if not aligned:
        return result
    failures = {(finding.get("code"), finding.get("entry_number"))
                for finding in report.get("findings", [])}
    for index in range(len(frozen_entries) - 1):
        if (frozen_entries[index].get("type") != "SPOKEN"
                or frozen_entries[index + 1].get("type") != "NARRATOR"
                or ("spoken_not_named", index + 1) not in failures
                or ("narrator_renamed", index + 2) not in failures):
            continue
        hint = (f"Possible adjacent-row label displacement: output n={index} labels "
                f"the spoken line; output n={index + 1} labels narration and must "
                f"stay NARRATOR. Re-evaluate who speaks n={index} from the passage "
                f"and put that answer on n={index}. Do not assume the previously "
                "proposed narration label is correct. Keep all other fields "
                "under the existing schema.")
        for finding in result["findings"]:
            if (finding.get("code") == "spoken_not_named"
                    and finding.get("entry_number") == index + 1
                    and hint not in finding["message"]):
                finding["message"] += " " + hint
    return result


_VALIDATION_EXPECTED = {
    "speaker_not_in_source": "an attested character name from the source text",
    "speaker_is_entry_type": "a character name, not the entry type",
    "spoken_not_named": "a character name, not NARRATOR or an empty value",
    "narrator_renamed": "NARRATOR",
    "missing_instruct": "a non-empty delivery instruction",
    "invalid_gap": "one of normal, beat, cut_off, trailing",
    "missing_action": "a non-empty playable verb",
    "missing_delivery": "a non-empty acoustic delivery note",
    "invalid_answers": "null or the index of an earlier entry in this request",
}

# Pass 3 delivery-direction modes (#1258); "off" is the shipped instruct-only contract.
DELIVERY_DIRECTIONS = ("off", "gaps", "directed", "action_only")
GAP_LABELS = ("normal", "beat", "cut_off", "trailing")


# Entry-type tokens the model must never return as a speaker name. NARRATOR is
# absent on purpose: it is both a type and a legitimate speaker.
ENTRY_TYPE_NAMES = frozenset({"SPOKEN", "NARRATION", "NARRATIVE", "DIALOGUE"})

# The roster line shows each character as "EMMA (also: EMMA WOODHOUSE, MISS
# WOODHOUSE)" (attribution_prompt_variants.roster_line). Rule 6 of that prompt
# says answer with the main form; a model that copies the whole entry instead
# hands back a string the prose never contains, is_attested_name rejects it,
# the batch burns its retries and the line ships as UNKNOWN. Measured
# 2026-09-20: the A3B IQ3_XXS adapter did it on 79 of 663 PDNC rows (12%),
# IQ1_M on 52. The head is a name WE wrote, so undoing the echo is exact.
_ROSTER_ALIAS_ECHO = re.compile(r"^(.*\S)\s*\(also:.*\)\s*$", re.DOTALL)


def strip_roster_alias_echo(speaker):
    """The speaker with a copied "(also: ...)" roster tail removed; unchanged
    otherwise. Pure: returns a new string, never rewrites the entry."""
    if not isinstance(speaker, str):
        return speaker
    match = _ROSTER_ALIAS_ECHO.match(speaker)
    return match.group(1) if match else speaker


MIN_NAME_ATTESTATIONS = 2
# Short sources may legitimately write a real name once; they must still
# supply that occurrence rather than admitting every proposed name.
MIN_SOURCE_FOR_ATTESTATION = 5000
NAME_ATTESTATION_ATTESTED = "attested"
NAME_ATTESTATION_UNATTESTED = "unattested"
NAME_ATTESTATION_UNVERIFIABLE = "unverifiable"
# Measured on public-domain Japanese (Aozora: Kokoro, Ningen Shikkaku, Rashomon)
# and Apache-2.0 Chinese (WP reconstructed): T=3 keeps 93.9% ZH / 96.2% JA
# (>=2-char) cast while cutting random 3-char substring hits 1.8x-3.8x vs T=2
# (Kokoro 28.4->15.7%, 1.81x; Ningen 23.9->12.1%, 1.97x; WP full 19.3->8.7%,
# 2.22x; WP 15k 7.6->2.0%, 3.8x; Rashomon 16.3->6.2%, 2.63x).
#
# Uncased names use max(T, min_attestations) at every source length: unlike
# cased names they do not drop to 1 on a short (< MIN_SOURCE_FOR_ATTESTATION)
# source, because one literal occurrence in an unspaced script cannot be told
# from any substring. Consequence: in a short CJK chapter a real name written
# once or twice is "unverifiable" - the output gate accepts it (no retry), but
# the roster never receives it, so a short book whose whole cast is mentioned
# only once or twice keeps an empty roster (same roster outcome as main, which
# admitted no uncased name at all).
UNCASED_MIN_NAME_ATTESTATIONS = 3
_UNSPACED_SCRIPT_RE = re.compile(
    r"[\u3005\u3040-\u30ff\u31f0-\u31ff\u3400-\u4dbf\u4e00-\u9fff"
    r"\uf900-\ufaff\uff65-\uff9f\U00020000-\U0002ffff"
    r"\u0e00-\u0e7f\u0e80-\u0eff\u1000-\u109f\u1780-\u17ff]"
)
_ARABIC_CHAR_RE = re.compile(r"[\u0600-\u06ff\u0750-\u077f\u08a0-\u08ff]")
_HEBREW_CHAR_RE = re.compile(r"[\u0590-\u05ff]")
_HANGUL_CHAR_RE = re.compile(r"[\u1100-\u11ff\u3130-\u318f\uac00-\ud7a3]")
# Unvocalized proclitic prefixes: Arabic (wa/fa + bi/li/ka) and Hebrew (ve/she + be/le/ke/me/ha).
_ARABIC_PREFIX_RE = re.compile(r"(?:[وف]?[بلك]|[وف])$")
_HEBREW_PREFIX_RE = re.compile(r"(?:[וש]?[בלכמה]|[וש])$")
# Korean honorifics and postpositional particles attached without a space.
_KOREAN_SUFFIX_RE = re.compile(
    r"(?:씨|님|군|양)?"
    r"(?:께서|에게서|에게|한테서|한테|으로부터|로부터|으로|부터|까지|보다|처럼|하고|이랑|마저|조차|라고|이라|라서|여서|이다|였다|[이가은는을를의에도만로와과랑야아께])?"
    r"[은는도만]?"
)
NAME_TITLE_PREFIXES = frozenset({
    "mr", "mrs", "ms", "miss", "dr", "doctor", "prof", "professor",
    "capt", "captain",
    "king", "queen", "prince", "princess", "lord", "lady", "sir", "dame",
    "duke", "duchess", "count", "countess", "baron", "baroness", "father",
    "mother", "brother", "sister", "officer", "sergeant", "general", "colonel",
    "major", "lieutenant", "rev", "reverend", "pastor", "bishop", "st", "saint",
    "the", "his", "her",
})
# A married-woman title names the wife, not the man whose name follows it:
# "Mrs. John Vale" is Fanny, so the title-stripped "John Vale" must not become
# an identity key for John. These are the married-woman entries of the title
# data above; add a title here when it joins the same convention.
_MARRIED_WOMAN_TITLES = frozenset({"mrs"})
# Prediction words the tag does not carry that name a DIFFERENT person from the
# tag's plain name, so the prefix/suffix identity join must refuse them
# (#2388 gate 5, variant V6):
#   - generational words: junior, senior, young, old, little distinguish a son,
#     an elder or a child from the bare name ("John Vale Junior" is not the
#     father "John Vale");
#   - foreign married-woman titles: frau, madame, mme, señora/senora, signora
#     name the wife, like the English "mrs" handled by _MARRIED_WOMAN_TITLES.
# An extra title that only qualifies the SAME person is deliberately absent:
# "MARQIS DE GRIERS" against a "De Griers" tag is measured and must keep
# joining. Keys are compared through _get_title_key (casefold, trailing ".").
_DIFFERENT_PERSON_EXTRA_WORDS = frozenset({
    "junior", "senior", "young", "old", "little",
    "frau", "madame", "mme", "señora", "senora", "signora",
})


# These words are routinely capitalised by sentence position alone. They may
# still be character names, but need an occurrence outside that position or an
# explicit reporting clause. A supplied cast continues to bypass this gate.
_NON_PERSON_NAME_WORDS = frozenset({
    "a", "an", "the", "he", "she", "it", "they", "we", "you", "i",
    "this", "that", "these", "those",
})
_SENTENCE_OPENING_WORDS = frozenset({
    "and", "but", "or", "however", "still", "yet", "then",
    "therefore", "thus", "meanwhile", "nevertheless", "nonetheless", "otherwise",
})
_NUMBER_WORDS = frozenset({
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine",
    "ten", "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen",
    "seventeen", "eighteen", "nineteen", "twenty", "hundred", "thousand",
})
_INTERJECTION_NAME = re.compile(r"(?:a+h+|o+h+|u+h+|h+m+|h+a+|w+o+w+)", re.IGNORECASE)


def is_weak_sentence_name(name):
    """Whether sentence-position capitals are ambiguous for this label."""
    words = re.findall(r"[^\W\d_]+|\d+", name.casefold())
    return (not words or
            (len(words) == 1 and
             (words[0] in _SENTENCE_OPENING_WORDS or
              bool(_INTERJECTION_NAME.fullmatch(words[0])))) or
            all(word in _NUMBER_WORDS or word.isdigit() for word in words))


def is_name_context(match, source_text):
    """Distinguish forced capitals from name use for an ambiguous label."""
    prefix = source_text[max(0, match.start() - 120):match.start()].rstrip(' \t\r')
    suffix = source_text[match.end():match.end() + 120]
    # Both 'Still said' and 'said Still' identify a reporting subject, including
    # a name at the start of a paragraph or after a closing dialogue quote.
    if (re.match(rf"\s+(?:[\w’'-]+ly\s+)?{_REPORTING_VERBS}\b", suffix,
                 re.IGNORECASE) or
            re.search(rf"\b{_REPORTING_VERBS}\s*$", prefix, re.IGNORECASE)):
        return True
    return bool(prefix and prefix[-1] not in _SENTENCE_END | _QUOTE_CHARS | {"\n"})


def get_name_attestation_threshold(source_text, min_attestations=MIN_NAME_ATTESTATIONS):
    """Retain the long-source gate while requiring evidence in short sources."""
    return 1 if len(source_text) < MIN_SOURCE_FOR_ATTESTATION else min_attestations


def _is_word_char(ch):
    return ch.isalnum() or ch == "_" or unicodedata.category(ch) in ("Mn", "Mc")


def _has_cased_letter(text):
    return any(unicodedata.category(ch) in ("Lu", "Ll", "Lt") for ch in text)


def _is_uncased_letter_name(text):
    return (any(unicodedata.category(ch).startswith("L") for ch in text)
            and not _has_cased_letter(text))


def _is_single_letter_uncased_name(text):
    """One letter (combining marks ignored), all uncased: 的, 他, 민, ر."""
    return (_is_uncased_letter_name(text)
            and sum(1 for ch in text
                    if unicodedata.category(ch).startswith("L")) == 1)


def _count_uncased_occurrences(name, source_text):
    """Literal occurrences of an uncased name, with the script heuristics.

    KNOWN RESIDUALS, pinned by tests.test_unattested_speakers so that changing
    these heuristics is deliberate:
      - A spaced-script word that begins with a proclitic and CONTAINS a
        candidate name counts as an occurrence: Arabic "مال" in "كمال",
        "تاب" in "كتاب"; Hebrew "בן" in "לבן" (white), "בד" in "כבד" (heavy).
      - The proclitic lookback is two characters, so a three-character chain
        misses a real prefix: "كتاب" in "بالكتاب" (bi-al-...) and "أحمد" in
        "بفأحمد" count 0.
      - The Korean suffix alternation covers common honorifics and particles,
        not every agglutinated ending: "민수" in "민수만의" and "민수라는"
        counts 0.
      - Vocalized Arabic/Hebrew (harakat/niqqud) is not normalised, so those
        spellings count 0.
    """
    if _UNSPACED_SCRIPT_RE.search(name):
        return sum(1 for _ in re.finditer(re.escape(name), source_text))
    arabic = bool(_ARABIC_CHAR_RE.match(name))
    hebrew = bool(_HEBREW_CHAR_RE.match(name))
    hangul = bool(_HANGUL_CHAR_RE.search(name))
    count = 0
    for found in re.finditer(re.escape(name), source_text):
        start, end = found.start(), found.end()
        if start > 0 and _is_word_char(source_text[start - 1]):
            prefix_re = _ARABIC_PREFIX_RE if arabic else (_HEBREW_PREFIX_RE if hebrew else None)
            prefix = prefix_re.search(source_text[max(0, start - 2):start]) if prefix_re else None
            prefix_start = start - len(prefix.group()) if prefix else -1
            if not prefix or (prefix_start > 0 and _is_word_char(source_text[prefix_start - 1])):
                continue
        if end < len(source_text) and _is_word_char(source_text[end]):
            if not hangul:
                continue
            tail = _KOREAN_SUFFIX_RE.match(source_text, end)
            tail_end = tail.end() if tail else end
            if tail_end == end or (tail_end < len(source_text) and _is_word_char(source_text[tail_end])):
                continue
        count += 1
    return count


def get_name_attestation_verdict(name, source_text, min_attestations=MIN_NAME_ATTESTATIONS):
    """Three-way attestation verdict: ``attested``, ``unattested``, or ``unverifiable``.

    Names containing any cased letter (including Latin+CJK mixed names) or no
    letters at all keep the capital-ratio rule and return only ``attested`` or
    ``unattested``. Names whose letters are all uncased (Japanese, Chinese,
    Korean, Arabic, Hebrew, Devanagari, ...) cannot be judged by case: 0 exact
    occurrences is ``unattested`` (rejected), >= threshold is ``attested``
    (accepted and roster-eligible), and in between is ``unverifiable`` (not
    rejected as invented by the output gate, not admitted to the roster).

    A one-letter uncased name is NEVER ``attested`` by occurrence count: it is
    ``unattested`` at 0 and ``unverifiable`` above that. Any plausible character
    recurs far past the threshold in a real book (79.9% of the distinct Han
    characters in a full Chinese novel reach 3, and 的/他 reached 196/113), so
    counting cannot separate a real one-character name from a particle. The
    output gate accepts ``unverifiable`` and the roster refuses it; a supplied
    cast bypasses both gates at the caller (``known_names`` / roster seed), so a
    cast one-character name still ships.

    The single implementation for both gates (output validation and roster
    admission) and for ``is_attested_name``.
    """
    if not source_text or not name:
        return NAME_ATTESTATION_ATTESTED
    # A soft hyphen or word joiner is invisible inside a word (#2316): the
    # model's "SONYA" and the book's "Son<SHY>ya" are the same attestation, and
    # a name that came back from the model carrying one is the same name.
    name = remove_silent_word_characters(name)
    source_text = remove_silent_word_characters(source_text)
    if not name:
        # A non-empty name made only of invisible characters cannot be attested
        # (#2316). Without this it skipped every check and could be stored as a
        # SPOKEN speaker that folded to "". An actually empty name stays a no-op.
        return NAME_ATTESTATION_UNATTESTED
    if _is_uncased_letter_name(name):
        count = _count_uncased_occurrences(name, source_text)
        if count == 0:
            return NAME_ATTESTATION_UNATTESTED
        if _is_single_letter_uncased_name(name):
            # A single letter is never attested by counting (see docstring).
            # At most "unverifiable": the output gate accepts it, the roster
            # refuses it. A supplied cast is handled before this gate.
            return NAME_ATTESTATION_UNVERIFIABLE
        threshold = max(UNCASED_MIN_NAME_ATTESTATIONS, min_attestations)
        return (NAME_ATTESTATION_ATTESTED if count >= threshold
                else NAME_ATTESTATION_UNVERIFIABLE)
    # Reporting verbs after a pronoun ("He said") are not personal-name
    # evidence. Explicit cast names are handled by the caller before this gate.
    if name.casefold().strip(".?!…") in _NON_PERSON_NAME_WORDS:
        return NAME_ATTESTATION_UNATTESTED
    min_attestations = get_name_attestation_threshold(source_text, min_attestations)
    # Match case-insensitively and judge by the first letter, because str.title()
    # capitalises after every non-letter: "BRI-CHAN".title() is "Bri-Chan" but
    # the book writes "Bri-chan". That spelling mismatch rejected three real
    # grimgar03 characters - Bri-chan (55 mentions), Zodiac-kun, Barbara-sensei -
    # as inventions, which is every honorific-suffixed name in a translated work.
    occurrences = list(re.finditer(r"(?<!\w)" + re.escape(name) + r"(?!\w)",
                                   source_text, re.IGNORECASE))
    capitalized = sum(1 for found in occurrences if found.group()[:1].isupper())
    lowercase = len(occurrences) - capitalized
    if is_weak_sentence_name(name):
        capitalized = sum(1 for found in occurrences
                          if found.group()[:1].isupper() and
                          is_name_context(found, source_text))
        # Do not let the full-name fallback turn a countdown into a person.
        return (NAME_ATTESTATION_ATTESTED
                if capitalized >= min_attestations and capitalized > lowercase * 2
                else NAME_ATTESTATION_UNATTESTED)
    if capitalized >= min_attestations and capitalized > lowercase * 2:
        return NAME_ATTESTATION_ATTESTED
    # A full name the book writes once - "Ian Fairytale" at his introduction,
    # "Ian" on every later page - is that character, not an invention (#622:
    # IAN FAIRYTALE was rejected while IAN passed). The full form must be in
    # the text and the first personal component must clear the gate on its own;
    # a shared title or role cannot supply that evidence. An invented
    # pairing of a real first name (IAN HUMPHREY) or a common word (FUTURE ME)
    # still fails.
    words = name.split()
    if len(words) > 1 and capitalized >= 1:
        # One title-stripping helper (Rule 15), shared with the tag checks.
        words = _get_speaker_title_and_words(name)[1]
        if words and _has_cased_letter(words[0]):
            return get_name_attestation_verdict(words[0], source_text, min_attestations)
    return NAME_ATTESTATION_UNATTESTED


def is_attested_name(name, source_text, min_attestations=MIN_NAME_ATTESTATIONS):
    """Whether a speaker name is written as a name in the source."""
    return (get_name_attestation_verdict(name, source_text, min_attestations)
            == NAME_ATTESTATION_ATTESTED)


_SILENT_IN_WORD = "".join(SILENT_WORD_CHARACTERS)
_TAG_WORD = r"[^\W\d_][\w’'" + _SILENT_IN_WORD + r"-]*"
_TAG_NAME = rf"{_TAG_WORD}(?:[ \t]+{_TAG_WORD}){{0,3}}?"
_TAG_ADVERB = r"(?-i:[a-z][\w’'" + _SILENT_IN_WORD + r"-]*ly)"
_TAG_OBJECT_PRONOUNS = frozenset({
    "him", "her", "them", "me", "us", "you", "himself", "herself",
})
# One source for the object-pronoun alternation (Rule 15).
_TAG_OBJECT = (rf"(?:(?-i:{'|'.join(sorted(_TAG_OBJECT_PRONOUNS))})"
               rf"|{_TAG_NAME})")
# Dedicated verb list for the speech-tag check (#2332): do not widen
# _REPORTING_VERBS, which drives quote recovery, lexical quotes and weak-name
# attestation context.
_SOURCE_TAG_VERBS = (
    r"(?:murmured|murmurs|whispered|whispers|said|says|replied|replies|"
    r"answered|answers|asked|asks|cried|cries|shouted|shouts|exclaimed|exclaims|"
    r"called|calls|muttered|mutters|added|adds|continued|continues|"
    r"observed|observes|remarked|remarks|rejoined|rejoins|"
    r"returned|returns|retorted|retorts|repeated|repeats|resumed|resumes|"
    r"interrupted|interrupts|inquired|inquires|enquired|enquires|"
    r"protested|protests|declared|declares|insisted|insists|"
    r"demanded|demands|explained|explains|faltered|falters|"
    r"stammered|stammers|responded|responds|urged|urges|pleaded|pleads|begged|begs)"
)
_SOURCE_SPEECH_TAG = re.compile(
    rf"^[ \t]*(?:[,;][ \t]*)?(?:"
    rf"(?P<before>{_TAG_NAME})(?:[ \t]+(?P<adv_before>{_TAG_ADVERB}))?[ \t]+"
    rf"(?:{_SOURCE_TAG_VERBS}(?:[ \t]+(?P<adv_after>{_TAG_ADVERB}))?(?:[ \t]+to[ \t]+(?P<obj_b>{_TAG_OBJECT}))?|"
    rf"(?:told|tells)[ \t]+(?P<obj_t>{_TAG_OBJECT})(?:[ \t]+(?P<adv_after_t>{_TAG_ADVERB}))?)|"
    rf"{_SOURCE_TAG_VERBS}[ \t]+(?P<after>{_TAG_NAME})"
    rf"(?:[ \t]+{_TAG_ADVERB})?(?:[ \t]+to[ \t]+(?P<obj_a>{_TAG_OBJECT}))?)"
    r"(?=[ \t]*[,.!?:;])", re.IGNORECASE)
_TAG_QUOTE_PAIRS = frozenset(_PAIRED)
# Openers the next-quote guard recognises after a subject-first tag. ‘ is not
# a PAIRED convention (’ is also the apostrophe), but Gutenberg texts use it
# for the next quotation.
_TAG_NEXT_QUOTE_OPENERS = frozenset(QUOTE_MARKS) | {"‘"}
# Gutenberg texts are hard-wrapped, so the tag's comma and the next quotation
# can be separated by one line break or by one blank line (#2388 gate 3).
_TAG_NEXT_QUOTE_WRAP = re.compile(r"[ \t]*\r?\n(?:[ \t]*\r?\n)?[ \t]*")


def _get_title_key(word):
    """One title-token normalizer, shared by tag-name rejection and stripping."""
    return word.casefold().rstrip(".")


def _is_valid_tag_name(value):
    return bool(
        value
        and value.casefold() not in _NON_PERSON_NAME_WORDS
        and _get_title_key(value) not in NAME_TITLE_PREFIXES
        and not any(word.casefold().endswith(("'s", "’s", "'", "’"))
                    for word in value.split())
        and all(word[:1].isupper() for word in value.split())
    )


def _resolve_alias_canonical(name, aliases, alias_index=None):
    """Follow speaker_aliases using speaker_identity's normalized key."""
    if not aliases or not name:
        return name
    from speaker_identity import get_speaker_identity_key, get_speaker_label_index
    index = get_speaker_label_index(aliases.keys()) if alias_index is None else alias_index
    current, seen = name, set()
    key = get_speaker_identity_key(current)
    while key and key not in seen and key in index:
        seen.add(key)
        current = aliases[index[key]]
        key = get_speaker_identity_key(current)
    return current


def _get_speaker_title_and_words(label):
    """Split a label into its leading titles and the remaining words."""
    words = remove_silent_word_characters(str(label or "")).split()
    titles = []
    while words and _get_title_key(words[0]) in NAME_TITLE_PREFIXES:
        titles.append(_get_title_key(words[0]))
        words = words[1:]
    return titles, words


def _is_same_tag_identity(speaker, expected, established_pool):
    """True when speaker and expected are compatible forms of the same character.

    Distinct established roster labels with different personal words (e.g.
    MARY MUSGROVE vs MRS MUSGROVE) and labels with conflicting titles (MR. vs
    MRS.) still contradict, as does a prediction whose extra word is a
    generational or foreign married-woman marker (JOHN VALE JUNIOR, FRAU JOHN
    VALE) against the tag's plain name.
    """
    from speaker_identity import get_speaker_identity_key, resolve_speaker_label
    if resolve_speaker_label(speaker, [expected]):
        return True
    sp_titles, sp_raw_words = _get_speaker_title_and_words(speaker)
    exp_titles, exp_raw_words = _get_speaker_title_and_words(expected)
    sp_words = [key for key in (get_speaker_identity_key(word)
                                for word in sp_raw_words) if key]
    exp_words = [key for key in (get_speaker_identity_key(word)
                                 for word in exp_raw_words) if key]
    if not sp_words or not exp_words:
        return False
    if sp_titles and exp_titles and sp_titles != exp_titles:
        return False
    if sp_words == exp_words:
        # "Mrs. John Vale" is the wife, not the man named after the title. When
        # only one side carries a married-woman title, the same personal words
        # do not make the two labels the same character (#2388 gate 2).
        sp_married = bool(_MARRIED_WOMAN_TITLES.intersection(sp_titles))
        exp_married = bool(_MARRIED_WOMAN_TITLES.intersection(exp_titles))
        return sp_married == exp_married
    if resolve_speaker_label(expected, established_pool):
        return False
    if (exp_titles and len(exp_words) == 1 and not sp_titles
            and len(sp_words) == 1):
        # A title plus a bare surname cannot name a predicted bare first name:
        # the surname may belong to any character of that family. Abstain
        # rather than call the two different characters (#2388).
        return True
    if len(exp_words) > len(sp_words):
        # exp_words are the tag's personal words and sp_words the prediction's
        # (validate_attribution passes the predicted label first). A tag that
        # carries MORE name words than the prediction is a different or more
        # specific label - a wife's foreign title ("Frau John Vale"), a
        # generational word ("John Vale Junior", "Young John Vale") or a
        # first name plus surname against a bare first name - not the same
        # identity (#2388 gate 3). The shorter-tag join below still lets an
        # unestablished "Miss Vale" label join a predicted "LUCY VALE".
        return False
    if any(_get_title_key(word) in _DIFFERENT_PERSON_EXTRA_WORDS
           for word in sp_words if word not in exp_words):
        # The prediction carries a word the tag's name does not. A generational
        # word ("JOHN VALE JUNIOR", "YOUNG JOHN VALE") or a foreign married-
        # woman title ("FRAU JOHN VALE") makes it a different person from the
        # tag's plain name; the shorter-tag join below would otherwise accept
        # it as a suffix/prefix match (#2388 gate 5, variant V6). An extra
        # honorific ("MARQIS DE GRIERS" vs "De Griers") is not listed and
        # keeps joining.
        return False
    short, full = ((exp_words, sp_words) if len(exp_words) <= len(sp_words)
                   else (sp_words, exp_words))
    return full[:len(short)] == short or full[-len(short):] == short


# Quotation continuations (#2333). A quotation that follows a bare speech tag
# in the same paragraph ("Go," Nita said. "Now.") inherits that tag's speaker
# only when the quotation carries no tag of its own. The continuation matcher
# is a BARE tag: one clause, anchored, with no object or second name. It is
# built from the same verb/adverb/name vocabulary as the direct speech-tag
# check (#2388), with the direct rule's `to <name>` / `told <name>` object form
# deliberately absent (a bare tag has no object). Only the evidence role
# differs.
_CONTINUATION_TAG_CORE = (
    rf"^[ \t]*(?:[,;][ \t]*)?(?:"
    rf"(?P<before>{_TAG_NAME})"
    rf"(?:[ \t]+{_TAG_ADVERB}[ \t]+(?P<verb_a>{_SOURCE_TAG_VERBS})|"
    rf"[ \t]+(?P<verb_b>{_SOURCE_TAG_VERBS})(?:[ \t]+{_TAG_ADVERB})?)|"
    rf"(?P<verb_c>{_SOURCE_TAG_VERBS})[ \t]+(?P<after>{_TAG_NAME})(?:[ \t]+{_TAG_ADVERB})?)"
)
_BARE_CONTINUATION_TAG = re.compile(
    _CONTINUATION_TAG_CORE + r"[ \t]*[,.!?:;][ \t]*$", re.IGNORECASE)
# Clause-shaped tags use reporting verbs the direct tag rule deliberately does
# not carry (began, went on, cut in, put in). This list is used ONLY to decide
# that a quotation carries its own tag and must abstain; it never creates
# evidence. The evidence verb list stays _SOURCE_TAG_VERBS (#2388, Rule 15).
# added/adds/continued/continues repeat that list on purpose: the direct rule's
# adverb slot accepts only a lowercase -ly adverb, so "she added later," (a
# common non-ly adverb) is not a direct tag and this clause form is what keeps
# the quotation from inheriting A's speaker (PDNC AHandfulOfDust-01969~3157).
_CONTINUATION_OWN_TAG_VERBS = (
    r"(?:began|begun|starts?|started|went on|goes on|going on|"
    r"cut in|cuts in|broke in|breaks in|broke off|breaks off|"
    r"carried on|carries on|added|adds|continued|continues|"
    r"put in|puts in)"
)
_CONTINUATION_TAG_PRONOUNS = frozenset({
    "he", "she", "they", "it", "we", "you", "i",
})
_CONTINUATION_TAG_PRONOUN_ALT = "|".join(sorted(_CONTINUATION_TAG_PRONOUNS))
# A clause subject is a named/articled subject: a capitalised name ("Kit") or
# a determiner/possessive plus up to three words ("the man", "his mother",
# "the old man"). _TAG_NAME already caps the subject at four words. A bare
# lowercase non-determiner ("somebody") does not count, so the guard is not
# simply "any subject".
_CONTINUATION_OWN_TAG_SUBJECT_DETERMINERS = frozenset({
    "the", "a", "an", "his", "her", "their", "my", "your", "our", "its",
})
# A bare reply after a question is very often the other speaker, so a
# continuation that follows a question abstains instead of inheriting.
_CONTINUATION_QUESTION_VERBS = frozenset({
    "asked", "asks", "inquired", "inquires", "enquired", "enquires",
    "demanded", "demands",
})
# The same four-word subject cap as _TAG_NAME, then a clause verb. The dash
# introducer belongs here: "No"-Kit began. is still the quotation's own tag.
# A comma-delimited interjection may sit between subject and verb
# ("Kit, however, said."); the verb there may be a clause verb or one the
# direct rule already carries.
_CONTINUATION_CLAUSE_VERBS = (
    rf"(?:{_SOURCE_TAG_VERBS}|{_CONTINUATION_OWN_TAG_VERBS})"
)
_CONTINUATION_OWN_TAG_CLAUSE = re.compile(
    rf"^[ \t]*(?:[,;\u2014\u2013-][ \t]*)?"
    rf"(?:(?P<pronoun>{_CONTINUATION_TAG_PRONOUN_ALT})|(?P<subject>{_TAG_NAME}))"
    rf"(?:[ \t]+(?:{_CONTINUATION_OWN_TAG_VERBS})\b"
    rf"|[ \t]*,[ \t]*(?P<interject>{_TAG_WORD}"
    rf"(?:[ \t]+{_TAG_WORD}){{0,2}})[ \t]*,"
    rf"[ \t]*(?:{_CONTINUATION_CLAUSE_VERBS})\b)", re.IGNORECASE)
# Verb-first clause tags are the same own-tag shape inverted ("began Kit.",
# "put in Kit."). A direct reporting verb in that position is already the
# direct rule's verb-first form, so only the clause verbs are needed here.
_CONTINUATION_OWN_TAG_INVERTED = re.compile(
    rf"^[ \t]*(?:[,;\u2014\u2013-][ \t]*)?"
    rf"(?:{_CONTINUATION_OWN_TAG_VERBS})[ \t]+(?P<subject>{_TAG_NAME})"
    rf"(?:[ \t]+{_TAG_ADVERB})?(?=[ \t]*[,.!?:;])", re.IGNORECASE)
# One title vocabulary (#2388's NAME_TITLE_PREFIXES, Rule 15). Prose writes
# several of its titles as an abbreviation with a period ("Mrs.", "Dr.",
# "St.", "Prof.", "Rev.", "Capt."); _TAG_WORD cannot cross that period, so a
# leading dotted title is flattened before the own-tag shapes are matched. The
# flattened text is only tested; it never becomes evidence.
_CONTINUATION_TITLE_ALT = "|".join(sorted(NAME_TITLE_PREFIXES,
                                          key=lambda value: (-len(value), value)))
_CONTINUATION_DOTTED_TITLE = re.compile(
    rf"^[ \t]*(?:[,;\u2014\u2013-][ \t]*)?(?P<title>{_CONTINUATION_TITLE_ALT})"
    r"\.[ \t]+", re.IGNORECASE)
# A tag attached by a dash directly after the closing mark is the quotation's
# own tag for the continuation guard. It must NOT become direct-tag evidence:
# #2388 disclosed the em-dash gap and this PR leaves it in place.
_DASH_TAG_INTRODUCER = re.compile(r"^[ \t]*[\u2014\u2013-][ \t]*")


def _flatten_continuation_dotted_title(tail):
    """Drop the period of a leading honorific so #2388's tag shapes match.

    "Mrs. Bennet said." becomes "Mrs Bennet said." The title text itself is
    unchanged, so the same vocabulary decides the own-tag question; the
    period is the only thing _TAG_WORD cannot cross. Returns `tail` unchanged
    when it does not start with a capitalised dotted title.
    """
    match = _CONTINUATION_DOTTED_TITLE.match(tail)
    if match is None or not match.group("title")[:1].isupper():
        return tail
    # Skip only the period; the whitespace after it stays.
    period = match.end("title")
    return tail[:period] + tail[period + 1:]


def _quote_ends_with_question(text):
    """Whether a quotation's own text ends in a question mark (incl. ?!)."""
    text = text.rstrip()
    return text.endswith("?") or text.endswith("?!")


def _get_continuation_tag_verb(match):
    """The speech verb a bare continuation tag carries, casefolded."""
    for group in ("verb_a", "verb_b", "verb_c"):
        value = match.group(group)
        if value:
            return value.casefold()
    return None


def _has_continuation_own_tag(tail):
    """Whether the text after a closing quote carries that quotation's tag.

    True when `tail` matches the direct-tag rule ("Kit said.", "Kit muttered.",
    "Kit said to Nita."), the same rule behind a dash introducer
    ("No"-Kit said.), a clause-shaped tag ("Kit began.", "Kit went on.",
    "Kit, however, said."), or that clause inverted ("began Kit."). A dotted
    honorific ("Mrs. Bennet said.") is flattened first, because _TAG_WORD
    cannot cross its period. Any hit means the quotation belongs to that tag,
    so it must not inherit the previous quotation's speaker; it also means the
    dash/clause form is not evidence.
    """
    tail = _flatten_continuation_dotted_title(tail)
    if _SOURCE_SPEECH_TAG.match(tail):
        return True
    dash = _DASH_TAG_INTRODUCER.match(tail)
    if dash and _SOURCE_SPEECH_TAG.match(tail[dash.end():]):
        return True
    clause = _CONTINUATION_OWN_TAG_CLAUSE.match(tail)
    if clause is None:
        return _CONTINUATION_OWN_TAG_INVERTED.match(tail) is not None
    if clause.group("pronoun"):
        return True
    subject = clause.group("subject").split()
    return (subject[0][:1].isupper()
            or subject[0].casefold() in _CONTINUATION_OWN_TAG_SUBJECT_DETERMINERS)


# An action beat that names another character is not evidence that the
# quotation continues the earlier tag: "No." Kit crossed his arms. Pronouns and
# determiner phrases ("She", "the man") are not names, so "No." She shook her
# head. keeps the inherited speaker; a beat naming A's own speaker adds
# nothing, so it keeps it too.
_CONTINUATION_ACTION_BEAT = re.compile(
    rf"^[ \t]*(?:[,;\u2014\u2013-][ \t]*)?(?P<subject>{_TAG_WORD})"
    rf"(?=[ \t]+{_TAG_WORD}"
    rf"|,[ \t]*{_TAG_WORD}(?:[ \t]+{_TAG_WORD}){{0,2}}[ \t]*,[ \t]*{_TAG_WORD})")


def _get_continuation_action_beat_subject(tail):
    """The character an action-beat clause after the quotation names, or None."""
    flat = _flatten_continuation_dotted_title(tail)
    match = _CONTINUATION_ACTION_BEAT.match(flat)
    if match is None:
        return None
    first = match.group("subject")
    key = first.casefold()
    if (not first[:1].isupper() or key in _CONTINUATION_TAG_PRONOUNS
            or key in _CONTINUATION_OWN_TAG_SUBJECT_DETERMINERS
            or key in _SENTENCE_OPENING_WORDS):
        return None
    if _get_title_key(first) in NAME_TITLE_PREFIXES:
        # "Mrs Bennet crossed her arms." (the period was flattened above).
        rest = flat[match.end():].split()
        return f"{first} {rest[0]}" if rest else None
    return first


def get_attribution_source_digest(source_text):
    """Bind UTF-8 source, abstaining from repair for unencodable strings."""
    try:
        return hashlib.sha256(source_text.encode("utf-8")).hexdigest()
    except UnicodeError:
        return None


def get_attribution_snapshot_digest(value):
    """A strict JSON snapshot identity, or None when repair cannot bind it."""
    active = set()
    remaining = 100000

    def is_json(item, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 64:
            return False
        kind = type(item)
        if item is None or kind in (bool, int, str):
            return True
        if kind is float:
            return math.isfinite(item)
        if kind not in (list, dict) or id(item) in active:
            return False
        active.add(id(item))
        try:
            if kind is dict:
                return all(type(key) is str and is_json(child, depth + 1)
                           for key, child in item.items())
            return all(is_json(child, depth + 1) for child in item)
        finally:
            active.remove(id(item))

    if not is_json(value):
        return None
    try:
        raw = json.dumps(value, sort_keys=True, ensure_ascii=False,
                         allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError):
        return None
    return hashlib.sha256(raw).hexdigest()


def get_attribution_identity_digest(entry):
    return get_attribution_snapshot_digest({key: entry.get(key)
                                            for key in ("type", "text")})


def _get_silent_folded_source_spans(source_text, needle_text):
    """Raw source spans of every needle, folding only the silent characters.

    Comparison ignores U+00AD/U+2060 (#2316); the returned offsets still point
    into the untouched source, because attribution proofs bind a raw-source
    digest and exact-shift repairs write offsets that must index it.
    """
    from dialogue_spans import get_source_match_positions
    folded, offsets = remove_silent_word_characters_with_offsets(source_text)
    needle = remove_silent_word_characters(needle_text)
    if not needle:
        return []
    positions = get_source_match_positions(folded, [needle]).get(needle, [])
    return [(offsets[position], offsets[position + len(needle) - 1] + 1)
            for position in positions]


def get_attribution_tag_proofs(entries, source_text, established_names=(),
                               speaker_aliases=None):
    """Map batch indexes to uniquely resolved tags on the quotations they follow.

    Repeated text and incomplete quotation spans are deliberately uncheckable.
    Source matching is one shared traversal per batch, outside the retry loop.

    Returns ``(proofs, continuation_evidence)``. A continuation entry receives
    only the speaker name for a quotation that continues a paragraph's bare
    speech tag (``“Go,” Nita said. “Now.”``). Continuations never produce a
    full proof: they cannot feed the exact-shift repair, which needs
    reporting-tag/name offsets. Callers that need them pass both maps to
    get_attribution_tag_evidence_projection.
    """
    if not source_text:
        return {}, {}
    from dialogue_spans import get_source_match_positions, spoken_spans
    from speaker_identity import get_speaker_identity_key, get_speaker_label_index
    aliases = speaker_aliases or {}
    alias_index = get_speaker_label_index(aliases.keys())
    established_names = set(established_names)
    # Identity keys already owned by another label or alias target. A
    # title-stripped form is indexed only when it does not claim a different
    # character's key: "Lady John Vale" is John's wife, so indexing "JOHN VALE"
    # for her would make his own tag abstain (#2388 gate 1). The comparison is
    # by resolved identity, so another spelling of the same character is safe.
    claimed_keys = {}
    for other in sorted(set(established_names) | set(aliases)
                        | set(aliases.values()), key=str):
        other_key = get_speaker_identity_key(other)
        if other_key:
            claimed_keys.setdefault(
                other_key,
                get_speaker_identity_key(
                    _resolve_alias_canonical(other, aliases, alias_index)))
    identities = {}
    for label in set(established_names) | set(aliases):
        canonical = _resolve_alias_canonical(label, aliases, alias_index)
        canonical_key = get_speaker_identity_key(canonical)
        titles, words = _get_speaker_title_and_words(label)
        forms = [label]
        if words:
            forms.extend([words[0], words[-1]])
            if len(words) > 1 and not _MARRIED_WOMAN_TITLES.intersection(titles):
                stripped = " ".join(words)
                claimed = claimed_keys.get(get_speaker_identity_key(stripped))
                if claimed is None or claimed == canonical_key:
                    forms.append(stripped)
            # Keep the title with the first personal component (Lord Henry),
            # but never treat a title alone as a character identity.
            title_count = len(label.split()) - len(words)
            if title_count:
                forms.append(" ".join(label.split()[:title_count + 1]))
        for key in get_speaker_label_index(forms):
            identities.setdefault(key, set()).add(canonical)
    spans = spoken_spans(source_text, "paired_quotes")
    quote_bounds = {}
    span_index = {}
    for span_pos, (start, end) in enumerate(spans):
        content = source_text[start:end]
        content_start = start + len(content) - len(content.lstrip(" \t"))
        content_end = end - len(content) + len(content.rstrip(" \t"))
        quote_bounds[remove_silent_word_characters(
            source_text[content_start:content_end])] = (content_start, content_end)
        span_index[(content_start, content_end)] = span_pos
    # One folded traversal for the whole batch (#2316), so repeated retries do
    # not rescan the source and an occurrence count still spans every entry.
    folded_source, source_offsets = remove_silent_word_characters_with_offsets(source_text)
    folded_needles = [remove_silent_word_characters(entry.get("text", "").strip())
                      for entry in entries if entry.get("type") == "SPOKEN"]
    positions = get_source_match_positions(
        folded_source, [needle for needle in folded_needles if needle])
    source_digest = get_attribution_source_digest(source_text)
    context_digest = get_attribution_snapshot_digest([sorted(established_names), aliases])

    def resolve_name_speaker(name):
        """Speaker a tag or beat name resolves to, or None when unsure."""
        name = remove_silent_word_characters(name)
        if not _is_valid_tag_name(name):
            return None
        key = next(iter(get_speaker_label_index([name])), None)
        candidates = identities.get(key, set())
        if len(candidates) == 1:
            return next(iter(candidates))
        if not candidates and is_attested_name(name, source_text):
            # A source-attested character need not have spoken in an earlier
            # batch. Ambiguous existing identities must still abstain.
            return name.upper()
        return None

    def resolve_tag_speaker(match):
        """Speaker named by a direct or continuation tag, or None when unsure."""
        return resolve_name_speaker(match.group("before") or match.group("after"))

    evidence = {}
    continuation_evidence = {}
    for index, entry in enumerate(entries):
        if entry.get("type") != "SPOKEN":
            continue
        text = entry.get("text", "").strip()
        needle = remove_silent_word_characters(text)
        matches = positions.get(needle, [])
        if len(matches) != 1:
            continue
        start = source_offsets[matches[0]]
        end = source_offsets[matches[0] + len(needle) - 1] + 1
        if quote_bounds.get(needle) != (start, end):
            continue
        before = source_text[max(0, start - 8):start].rstrip(" \t")
        after = source_text[end:end + 160].lstrip(" \t")
        if not before or not after or (before[-1], after[0]) not in _TAG_QUOTE_PAIRS:
            continue
        tail = after[1:].split("\n", 1)[0]
        tag = _SOURCE_SPEECH_TAG.match(tail)
        if tag:
            # At most one optional -ly adverb, before or after the verb.
            if tag.group("adv_before") and (tag.group("adv_after")
                                            or tag.group("adv_after_t")):
                continue
            if (text and text[-1] in _SENTENCE_END and tag.group("before")
                    and not tail.lstrip(" \t").startswith((",", ";"))):
                # Read past the tag from the source, not from the line-truncated
                # tail, so a single hard line wrap can be seen (#2388 gate 2).
                rest = after[1:][tag.end():].lstrip(" \t")
                after_punct = (rest[1:].lstrip(" \t")
                               if rest and rest[0] in (",", ":") else "")
                wrap = _TAG_NEXT_QUOTE_WRAP.match(after_punct)
                if wrap:
                    after_punct = after_punct[wrap.end():]
                if after_punct and after_punct[0] in _TAG_NEXT_QUOTE_OPENERS:
                    continue
            obj = tag.group("obj_b") or tag.group("obj_a") or tag.group("obj_t")
            if obj is not None:
                obj_clean = remove_silent_word_characters(obj)
                if (obj_clean not in _TAG_OBJECT_PRONOUNS
                        and not _is_valid_tag_name(obj_clean)):
                    continue
            speaker = resolve_tag_speaker(tag)
            if speaker is None:
                continue
            # Identity uses only fields the original evidence decision reads.
            # Opaque unrelated row metadata must not break the legacy projection.
            opener = max(0, start - 8) + len(before) - 1
            suffix = source_text[end:end + 160]
            close = end + len(suffix) - len(after)
            base = close + 1
            group = "before" if tag.group("before") else "after"
            evidence[index] = {
                "speaker": speaker, "source_sha256": source_digest,
                "entry_identity_sha256": get_attribution_identity_digest(entry),
                "quote_bounds": [start, end], "opener_offset": opener,
                "closing_offset": close,
                "reporting_tag_bounds": [base + tag.start(), base + tag.end()],
                "name_bounds": [base + tag.start(group), base + tag.end(group)],
                "resolver_context_sha256": context_digest,
            }
            # The quotation carries its own tag, even when its name did not
            # resolve: it must never inherit the previous quotation's speaker.
            continue
        # No direct tag after this quotation. A dash-attached tag or a
        # clause-shaped tag ("Kit began.", "Kit went on.") is still the
        # quotation's own tag, so it abstains instead of inheriting one.
        if _has_continuation_own_tag(tail):
            continue
        # Otherwise a bare tag between this quotation and the previous one in
        # the same paragraph (``"Go," Nita said. "Now."``) names this
        # continuation's speaker. This is validation-only name evidence: it
        # never produces a proof, so it cannot reach the exact-shift repair.
        span_pos = span_index.get((start, end))
        if span_pos is None or span_pos == 0:
            continue
        prev_start, prev_end = spans[span_pos - 1]
        cur_start = spans[span_pos][0]
        if (prev_start <= 0 or prev_end >= len(source_text) or cur_start <= 0
                or (source_text[prev_start - 1], source_text[prev_end])
                not in _TAG_QUOTE_PAIRS):
            continue
        between = source_text[prev_end + 1:cur_start - 1]
        if "\n" in between or "\r" in between:
            continue
        cont_tag = _BARE_CONTINUATION_TAG.match(between)
        if not cont_tag:
            continue
        # A bare reply after a question is very often the other speaker. A
        # question quotation (including ?! and a closing mark that follows the
        # ?) or a question tag verb therefore abstains instead of inheriting.
        if _quote_ends_with_question(source_text[prev_start:prev_end]):
            continue
        if _get_continuation_tag_verb(cont_tag) in _CONTINUATION_QUESTION_VERBS:
            continue
        speaker = resolve_tag_speaker(cont_tag)
        if speaker is None:
            continue
        beat = _get_continuation_action_beat_subject(tail)
        if beat is not None and resolve_name_speaker(beat) != speaker:
            # The quotation is followed by a clause about another character, so
            # it is an action beat, not evidence that it continues the tag.
            continue
        continuation_evidence[index] = speaker
    return evidence, continuation_evidence


def get_attribution_tag_evidence_projection(proofs, continuation_evidence=None):
    """Legacy name evidence from proofs plus continuation-only speakers.

    Continuations deliberately carry no proof record, so their speakers can
    only be merged here. Keeping the merge in one place stops each caller from
    rebuilding it, and keeps a continuation out of the proof map the
    exact-shift repair consumes (Rule 15).
    """
    evidence = {index: proof["speaker"] for index, proof in proofs.items()}
    evidence.update(continuation_evidence or {})
    return evidence


def get_attribution_tag_evidence(entries, source_text, established_names=(),
                                 speaker_aliases=None):
    """Legacy name evidence, projected from the shared source proof."""
    proofs, continuations = get_attribution_tag_proofs(
        entries, source_text, established_names, speaker_aliases)
    return get_attribution_tag_evidence_projection(proofs, continuations)


_ATTRIBUTION_REPAIR_UNSAFE_FIELDS = frozenset({
    "gender", "age_group", "ageless", "speaker_gender", "speaker_age_group",
    "speaker_ageless", "traits", "speaker_traits", "reason", "reasoning",
    "explanation", "confidence", "evidence", "judge", "judgement", "why",
})


def get_exact_attribution_shift_repair(frozen_entries, response_entries, source_text,
                                      proofs, tag_evidence, snapshot_digest,
                                      known_names=(), established_names=(),
                                      speaker_aliases=None, speaker_traits=False):
    """Prepare copied exact adjacent shifts; acceptance still needs the full provider gate.

    The caller computes proofs/projection once per batch and retains the raw
    answer. No pending correction survives a failed whole native gate.
    """
    aliases = speaker_aliases or {}
    context = [frozen_entries, sorted(established_names), sorted(known_names), aliases]
    unchanged = {"entries": response_entries}
    if (speaker_traits or not source_text or snapshot_digest is None
            or get_attribution_snapshot_digest(context) != snapshot_digest):
        return unchanged
    ok, _reason, ordered = index_head_check(frozen_entries, response_entries)
    if not ok or get_attribution_snapshot_digest(response_entries) is None:
        return unchanged
    from speaker_identity import get_speaker_label_index, resolve_speaker_label
    source_digest = get_attribution_source_digest(source_text)
    if source_digest is None:
        return unchanged
    alias_index = get_speaker_label_index(aliases.keys())
    resolver_digest = get_attribution_snapshot_digest([
        sorted(set(established_names) | set(known_names)), aliases])
    proposed = copy.deepcopy(ordered)
    changes = []
    for index, proof in proofs.items():
        if (type(index) is not int or not 0 <= index < len(frozen_entries) - 1
                or frozen_entries[index].get("type") != "SPOKEN"
                or frozen_entries[index + 1].get("type") != "NARRATOR"):
            continue
        left, right = ordered[index:index + 2]
        if (left.get("speaker") != "NARRATOR"
                or not isinstance(right.get("speaker"), str)
                or (set(left) | set(right) | set(frozen_entries[index])
                    | set(frozen_entries[index + 1])) & _ATTRIBUTION_REPAIR_UNSAFE_FIELDS
                or proof.get("source_sha256") != source_digest
                or proof.get("entry_identity_sha256") != get_attribution_identity_digest(frozen_entries[index])
                or proof.get("resolver_context_sha256") != resolver_digest):
            continue
        name = right["speaker"].strip().upper()
        expected = _resolve_alias_canonical(proof["speaker"], aliases, alias_index)
        if (name in ("", "UNKNOWN", "NARRATOR")
                or not resolve_speaker_label(
                    _resolve_alias_canonical(name, aliases, alias_index), [expected])):
            continue
        text = frozen_entries[index + 1].get("text", "")
        if not isinstance(text, str) or not text or text != text.strip(" \t"):
            continue
        begin = proof["closing_offset"] + 1
        while begin < len(source_text) and source_text[begin] in " \t":
            begin += 1
        end = proof["reporting_tag_bounds"][1]
        while end < len(source_text) and source_text[end] in " \t":
            end += 1
        matches = _get_silent_folded_source_spans(source_text, text)
        if (end >= len(source_text) or source_text[end] not in ",.!?:;"
                or len(matches) != 1 or matches[0][0] != begin
                or matches[0][1] < end + 1):
            continue
        proposed[index]["speaker"] = expected
        proposed[index + 1]["speaker"] = "NARRATOR"
        changes.append({"code": "exact_adjacent_attribution_shift", "indexes": [index, index + 1],
                        "speaker": expected, "source_sha256": source_digest,
                        "snapshot_sha256": snapshot_digest, "quote_bounds": proof["quote_bounds"],
                        "reporting_tag_bounds": proof["reporting_tag_bounds"],
                        "closing_offset": proof["closing_offset"],
                        "resolver_context_sha256": proof["resolver_context_sha256"],
                        "name_bounds": proof["name_bounds"],
                        "narration_bounds": [begin, matches[0][1]]})
    if not changes:
        return unchanged
    quality = validate_attribution(frozen_entries, proposed, source_text,
        known_names=known_names, established_names=established_names,
        speaker_aliases=aliases, tag_evidence=tag_evidence)
    if not quality["passed"]:
        return unchanged
    return {"entries": proposed, "accepted_metadata": {
        "repairs": changes, "answer_sha256": get_attribution_snapshot_digest(proposed)}}


def validate_attribution(frozen_entries, response_entries, source_text=None,
                         known_names=None, tag_evidence=None, speaker_aliases=None,
                         established_names=None):
    """Pass 2 gate. Verifies the index+head alignment, then requires every SPOKEN
    span to have a non-empty speaker other than NARRATOR, and every NARRATOR span
    to stay NARRATOR. `known_names` (a supplied cast, upper-case) skips only the
    speaker_not_in_source check: a cast list may name a character the text never
    capitalises ("THE STRANGER") or names once ("SILAS DURGAN")."""
    known_names = known_names or frozenset()
    ok, reason, ordered = index_head_check(frozen_entries, response_entries)
    if not ok:
        return {"passed": False,
                "findings": [{"code": "alignment_violated", "message": reason,
                              **get_alignment_counts(frozen_entries, response_entries)}]}
    established_names = set(established_names or ()) | set(known_names)
    if tag_evidence is None:
        tag_evidence = get_attribution_tag_evidence(
            frozen_entries, source_text, established_names, speaker_aliases)
    aliases = speaker_aliases or {}
    alias_index = None
    established_pool = None
    findings = []
    for i, (frozen, item) in enumerate(zip(frozen_entries, ordered), 1):
        source_line = frozen.get("text", "")
        raw_speaker = strip_roster_alias_echo(item.get("speaker"))
        speaker = raw_speaker.strip() if isinstance(raw_speaker, str) else ""
        if frozen.get("type") == "SPOKEN":
            # The prompt shows each entry as {"n", "type", "text"}, so the model
            # sometimes echoes the type back as the speaker. "SPOKEN" is
            # non-empty and is not NARRATOR, so it satisfied the checks below
            # and shipped as a character name - 326 entries in one book, and
            # present in 8 of 9 books measured. UNKNOWN is deliberately not
            # rejected here: it is the placeholder stabilize_speaker_identities
            # assigns for genuinely unresolved speakers.
            if (source_text and speaker
                    and speaker.upper() not in ("UNKNOWN", "NARRATOR")
                    and speaker.upper() not in known_names
                    and get_name_attestation_verdict(speaker, source_text)
                    == NAME_ATTESTATION_UNATTESTED):
                # The roster gate filters what goes IN; nothing filtered what
                # came OUT. The model invented FUTURE_ME - the protagonist's
                # future self, a phrase the book never capitalises - and
                # shipped it on 250 entries, 13% of one book. With MAILMAN,
                # ARUMANFI and SWORD_GOD_GARU_FARION that was 279 entries,
                # 14.6% of the book, attributed to names not in the text.
                message = (
                    "The speaker does not appear in the source text."
                    if _is_uncased_letter_name(remove_silent_word_characters(speaker))
                    else ("The speaker does not appear as a name in the source text "
                          "(a name must be written capitalised at least "
                          f"{'once' if get_name_attestation_threshold(source_text) == 1 else 'twice'}, "
                          "or be a full name the text writes whose personal name is).")
                )
                findings.append({"code": "speaker_not_in_source",
                                 "entry_number": i,
                                 "value": speaker,
                                 "source_line": source_line,
                                 "expected": _VALIDATION_EXPECTED["speaker_not_in_source"],
                                 "message": message})
                continue
            if speaker.upper() in ENTRY_TYPE_NAMES:
                findings.append({"code": "speaker_is_entry_type",
                                 "entry_number": i,
                                 "value": speaker,
                                 "source_line": source_line,
                                 "expected": _VALIDATION_EXPECTED["speaker_is_entry_type"],
                                 "message": "The speaker repeats the entry type "
                                            "instead of naming a character."})
                continue
            expected_speaker = tag_evidence.get(i - 1)
            if expected_speaker and speaker.upper() not in ("UNKNOWN", "NARRATOR", ""):
                from speaker_identity import get_speaker_label_index, resolve_speaker_label
                if alias_index is None:
                    alias_index = get_speaker_label_index(aliases.keys())
                    established_pool = established_names | set(aliases.values())
                canonical = _resolve_alias_canonical(speaker.upper(), aliases, alias_index)
                expected_canonical = _resolve_alias_canonical(expected_speaker, aliases, alias_index)
                if (resolve_speaker_label(canonical, established_pool)
                        and not _is_same_tag_identity(canonical, expected_canonical, established_pool)):
                    findings.append({"code": "speaker_contradicts_source_tag",
                                     "entry_number": i, "value": speaker,
                                     "source_line": source_line,
                                     "expected": expected_speaker,
                                     "message": "The source speech tag "
                                                f"names {expected_speaker}, not {speaker}."})
            if not speaker or speaker.upper() == "NARRATOR":
                findings.append({"code": "spoken_not_named", "entry_number": i,
                                 "returned": speaker,
                                 "source_line": source_line,
                                 "expected": _VALIDATION_EXPECTED["spoken_not_named"],
                                 "message": "A spoken line was not assigned a character name."})
        else:  # NARRATOR (or any non-SPOKEN)
            if speaker.upper() != "NARRATOR":
                findings.append({"code": "narrator_renamed", "entry_number": i,
                                 "value": speaker,
                                 "source_line": source_line,
                                 "expected": _VALIDATION_EXPECTED["narrator_renamed"],
                                 "message": "A narrator line must keep the speaker NARRATOR."})
    return {"passed": not findings, "findings": findings}


def validate_instruct(prior_entries, response_entries, delivery_direction="off"):
    """Pass 3 gate. Verifies the index+head alignment (speaker/text are supplied
    by the frozen entry, not the model, so they cannot change), and requires a
    non-empty instruct on every entry. A delivery-direction mode (#1258) also
    requires its fields: a known gap, and in the directed modes an action, a
    delivery note (directed only; action_only discards it) and an `answers`
    that is null or an earlier entry of this request, in place of the instruct."""
    ok, reason, ordered = index_head_check(prior_entries, response_entries)
    if not ok:
        return {"passed": False,
                "findings": [{"code": "alignment_violated", "message": reason,
                              **get_alignment_counts(prior_entries, response_entries)}]}
    if delivery_direction not in DELIVERY_DIRECTIONS:
        raise ValueError(f"unknown delivery direction {delivery_direction!r}")
    directed = delivery_direction in ("directed", "action_only")
    findings = []

    def add(code, i, prior, message):
        findings.append({"code": code, "entry_number": i,
                         "source_line": prior.get("text", ""),
                         "expected": _VALIDATION_EXPECTED[code], "message": message})

    def is_text(value):
        return isinstance(value, str) and any(ch.isalnum() for ch in value)

    for i, (prior, item) in enumerate(zip(prior_entries, ordered), 1):
        if not directed and not is_text(item.get("instruct")):
            add("missing_instruct", i, prior, "Every entry needs a non-empty instruct.")
        if delivery_direction != "off" and item.get("gap") not in GAP_LABELS:
            add("invalid_gap", i, prior, "Every entry needs a gap from the allowed set.")
        if not directed:
            continue
        if not is_text(item.get("action")):
            add("missing_action", i, prior, "Every entry needs an action.")
        if delivery_direction == "directed" and not is_text(item.get("delivery")):
            add("missing_delivery", i, prior, "Every entry needs a delivery note.")
        if "answers" not in item:
            add("invalid_answers", i, prior,
                "Every directed entry needs an answers key (null when there is no reply).")
            continue
        answers = coerce_index(item.get("answers"))
        if answers is not None and (isinstance(answers, bool) or not isinstance(answers, int)
                                    or not 0 <= answers < i - 1):
            add("invalid_answers", i, prior,
                "answers must be null or the index of an earlier entry in this request.")
    return {"passed": not findings, "findings": findings}
