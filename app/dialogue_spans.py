"""Where is the spoken text, according to the book itself?

WHY THIS EXISTS. Generation is told to drop the outermost dialogue quotes -
`text` is what the TTS voice says, and a voice should not say punctuation. The
instruction is followed unevenly (22%, 16% and 1% retention across three
books), and either way the FACT that a line was speech is destroyed rather than
recorded. Everything downstream then has to re-infer it from prose the model
already rewrote: today's attribution scorer guesses from quote coverage, and it
guessed wrong three times before it was trusted.

So the map is built from the SOURCE, before any model touches it, and travels
with the entries as data. Two consequences worth having:

  - it does not depend on the model complying with anything;
  - the attribution question becomes "who said this known line", which is the
    formulation that reaches 90.6% on PDNC with an 8B model, rather than
    "segment this prose and also tell me who spoke".

QUOTE MARKS ARE ONE CONVENTION, NOT THE CONVENTION. Authors also mark speech
with an em dash at the start of a line, or with a script-style `NAME:` prefix.
The convention is detected per book rather than assumed, because assuming one
is how a book with 6,925 quote marks in its source was recorded here as "a book
that does not mark dialogue".
"""
from bisect import bisect_left, bisect_right
from collections import Counter, deque
import logging
import re

PAIRED = [
    ('"', '"'),
    ("“", "”"),      # curly “ ”
    ("„", "“"),      # German/Polish/Czech low-high „ “
    ("「", "」"),      # 「 」
    ("『", "』"),      # 『 』
    ("＂", "＂"),      # full-width straight
]
# An em or en dash opening a line, the Continental convention.
DASH_LINE = re.compile(r"^[ \t]*[—–][ \t]*(\S.*)$", re.M)
# A possible turn after narration, with a second dash introducing its tag.
# Its formatting establishes uncertainty, not speech or a speaker identity.
INTERIOR_DASH_TURN = re.compile(
    r"[.!?。！？…][ \t]+[—–][ \t]*(\S[^—–\r\n]*?[,!?！？…])[ \t]+[—–][ \t]+\S")
# Script form: a speaker label, a colon, then the line.
LABEL_LINE = re.compile(r"^[ \t]*([A-Z][A-Z .'’-]{1,30}):[ \t]+(\S.*)$", re.M)


# Speech marked in a way the book does not mainly use. A line found inside one
# lies outside the detected quotes without being narration: ‘single’ quotes in
# a “double”-quote book, a dash-opened line, «guillemets», or an opening mark
# the author never closed. `’` is also the apostrophe, so a ‘…’ pair closes
# only at a `’` that no letter follows ("‘I don’t know,’ she said").
OTHER_PAIRED = [
    re.compile(r"‘((?:[^‘’\n]|’(?=\w))+)’(?!\w)"),
    re.compile(r"«([^«»]+)»"),
    re.compile(r"‹([^‹›\n]+)›"),
    # Low-high single marks (‚…‘). The opener is never an apostrophe, so it
    # anchors the pair on its own; the closer is the next ‘ after it.
    re.compile(r"‚([^‚‘\n]+)‘"),
]
# A mark here can ALSO close a different pair - `“` closes `„…“` - so a caller
# must skip an occurrence that closes a real pair. get_other_speech_spans does.
UNCLOSED_OPENERS = {"“": "”", "「": "」", "『": "』", "„": "“"}

# Marks stripped before an entry's text is matched against the source: the
# delimiters this module pairs, so a line whose quotes the model dropped still
# locates. ‘ and ’ are absent on purpose - ’ is the apostrophe, and removing it
# breaks every contraction.
QUOTE_MARKS = '"“”„「」『』＂'
_QUOTE_MARKS_RE = re.compile(f"[{QUOTE_MARKS}]")
# Where a quote can START, for the one place that must recognise a mark without
# pairing it (a printed speaker name sits immediately before one). Derived from
# PAIRED so a newly paired convention cannot be forgotten here.
QUOTE_OPENERS = "".join(dict.fromkeys(opener for opener, _closer in PAIRED))

# A blank line however the source spells it: LF, CRLF or CR, with spaces or tabs
# on the empty line. `"\n\n"` alone was blind to every file whose blank lines
# carry a trailing space, which left the cross-paragraph refusal below silently
# optional on exactly those books.
BLANK_LINE = re.compile(r"(?:\r\n|\r|\n)[ \t]*(?:\r\n|\r|\n)")


def _paragraph_breaks(text):
    """-> [(blank-line offset, offset of the first non-space character after)].

    A paired quote legitimately runs across a paragraph break when the next
    paragraph re-opens it with the same mark - that is how a multi-paragraph
    speech prints. Anything else after the break is narration. Indexed once for
    the whole text and tested by bisect, because scanning every break inside
    every rejected candidate is what makes a chain of unclosed openers
    quadratic. Offsets only: a one-character slice per break copied the entire
    break list on a pathological text, which is a measured property here.
    """
    breaks, search = [], 0
    while True:
        found = BLANK_LINE.search(text, search)
        if found is None:
            return breaks
        at, after = found.start(), found.end()
        while after < len(text) and text[after] in " \t\r\n":
            after += 1
        breaks.append((at, after))
        search = at + 1


def _next_blank_line(text, start):
    """-> offset of the first blank line at or after `start`, or -1."""
    found = BLANK_LINE.search(text, start)
    return -1 if found is None else found.start()


def _is_unbroken(breaks, position, end):
    """Does no narration break sit strictly inside this candidate pair?"""
    index = bisect_right(breaks, position)
    return index == len(breaks) or breaks[index] >= end - 1


def _paired_spans(text):
    spans = []
    breaks = starts = None
    masks = {}

    def prepare():
        """-> (breaks, offsets where a paragraph begins), built once per call.

        Lazy on purpose: a text whose openers never close completes no pair and
        so pays no paragraph scan at all, which the unclosed-opener cost test
        pins.
        """
        nonlocal breaks, starts
        if breaks is None:
            breaks = _paragraph_breaks(text)
            starts = {after for _at, after in breaks}
        return breaks, starts

    def narration_breaks(opener):
        """-> offsets of the breaks this opener does NOT re-open."""
        mask = masks.get(opener)
        if mask is None:
            mask = [at for at, after in prepare()[0]
                    if not text.startswith(opener, after)]
            masks[opener] = mask
        return mask

    for opener, closer in PAIRED:
        if opener == closer:
            # Straight quotes cannot be told apart, so they can only be paired
            # by position - and in a book that mixes “ ” with " ", one
            # unmatched straight mark shifts every pair after it and the
            # "span" swallows the narration between two unrelated quotes. That
            # is not hypothetical: it marked `I was taken aback.` as spoken in
            # mushoku18 and inflated the misattribution rate.
            #
            # So a straight pair answers the SAME question a distinct-mark pair
            # does: does a narration break sit between these two marks? A break
            # the next paragraph re-opens with this same mark is a continued
            # speech, not a close. The old blanket "no blank line in any
            # straight pair" answered a different question, and lost the first
            # paragraph of every continued speech with it.
            positions = []
            for match in re.finditer(re.escape(opener), text):
                position = match.start()
                # Outside a quoted span, a mark immediately after a number is
                # an inch mark. Inside speech, numeric endings still close it.
                if len(positions) % 2 == 0 and position and text[position - 1].isdigit():
                    continue
                positions.append(position)
            index = 0
            while index + 1 < len(positions):
                start = positions[index]
                end = None
                for candidate_index in range(index + 1, len(positions)):
                    candidate = positions[candidate_index]
                    if candidate - start <= 1:
                        continue
                    if candidate in prepare()[1]:
                        # The mark opens the paragraph after a blank line, so it
                        # re-opens this same quote and the speech continues.
                        continue
                    if not _is_unbroken(narration_breaks(opener), start, candidate):
                        # Every later mark crosses that same break too.
                        break
                    end = candidate
                    break
                if end is None:
                    index += 1
                    continue
                spans.append((start + 1, end))
                index = bisect_right(positions, end)
        else:
            # One rule for every pair, short or long: a blank line inside it
            # must be followed by the same mark and nothing else. A long speech
            # is fine (#1533, 9 of Dorian Gray's speeches run past 2,000
            # characters), and so is a speech whose paragraphs each re-open the
            # quotation. A break followed by narration is a separate speech, so
            # a pair reaching across it has swallowed narration - and its length
            # says nothing about that, which is why no pair is exempt. A
            # rejected pair advances to the next opener, as the old bounded
            # regex did.
            position = text.find(opener)
            end, bad_breaks = 0, None
            while position != -1:
                # Rejected openers can share one distant closer. Cache that
                # forward search and inspect offsets without copying every
                # remaining interior substring.
                if end <= position:
                    end = text.find(closer, position + 1)
                if end == -1:
                    break
                if bad_breaks is None:
                    bad_breaks = narration_breaks(opener)
                if position + 1 < end and _is_unbroken(bad_breaks, position, end):
                    spans.append((position + 1, end))
                    position = text.find(opener, end + 1)
                else:
                    position = text.find(opener, position + 1)
    return spans


def detect_convention(text):
    """-> the name of the dialogue convention this text uses, or None.

    Counted rather than guessed: whichever marker actually carries the
    dialogue wins, and a book that uses none returns None so the caller can
    say "cannot tell" instead of returning an empty map that reads as "no
    dialogue".
    """
    counts = {
        "paired_quotes": len(_paired_spans(text)),
        "dash_lines": len(DASH_LINE.findall(text)),
        "label_lines": len(LABEL_LINE.findall(text)),
    }
    best = max(counts, key=counts.get)
    if counts[best] < 5:
        return None
    return best


def validate_spoken_spans(spans, text):
    """Raise unless every detected span is a real, non-empty source range.

    Called on the spans as each convention found them, BEFORE `spoken_spans`
    sorts and merges. The merge repairs order and overlap by construction, so a
    check placed after it could never fail - it would only describe the merge.
    Overlap is legal here, because a book may mark one line with two
    conventions at once; only the range itself is refused. Pure read.
    """
    for start, end in spans:
        if type(start) is not int or type(end) is not int:
            raise ValueError("Speech span offsets must be integers")
        if not 0 <= start < end <= len(text):
            raise ValueError(
                f"Invalid speech span {start}:{end} in {len(text)} characters")
    return True


def spoken_spans(text, convention=None):
    """-> sorted, non-overlapping (start, end) offsets of spoken text."""
    if convention is None:
        convention = detect_convention(text)
    if convention is None:
        return []
    if convention == "paired_quotes":
        spans = _paired_spans(text)
    elif convention == "dash_lines":
        spans = [(m.start(1), m.end(1)) for m in DASH_LINE.finditer(text)]
    elif convention == "label_lines":
        spans = [(m.start(2), m.end(2)) for m in LABEL_LINE.finditer(text)]
    else:
        raise ValueError(f"Unknown dialogue convention: {convention!r}")
    validate_spoken_spans(spans, text)
    spans.sort()
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def get_other_speech_spans(text, convention):
    """-> (start, end) offsets of speech marked other than by `convention`.

    Only used to stop calling such a line narration; it is never marked spoken
    from this, because which convention carries a book's dialogue is counted
    by detect_convention, not guessed here. Spans do not cross a blank line.
    """
    if convention is None:
        return []
    spans = [(m.start(1), m.end(1)) for pattern in OTHER_PAIRED for m in pattern.finditer(text)
             if BLANK_LINE.search(text, m.start(1), m.end(1)) is None]
    if convention != "dash_lines":
        spans += [(m.start(1), m.end(1)) for m in DASH_LINE.finditer(text)]
    if convention != "paired_quotes":
        spans += _paired_spans(text)
    else:
        # An opening mark with no partner: speech to the end of its paragraph.
        # Membership is one bisect into the merged pairs, not a scan of all of
        # them per opener, which made well-formed books quadratic.
        pairs = sorted(_paired_spans(text))
        # The mark that CLOSES a real pair, by offset. `“` closes `„…“`, so without
        # this every German speech ended in a phantom "unclosed opener"
        # covering the rest of its paragraph.
        closed = {stop for _begin, stop in pairs}
        merged = []
        for begin, stop in pairs:
            if merged and begin <= merged[-1][1]:
                merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
            else:
                merged.append((begin, stop))
        starts = [begin for begin, _stop in merged]
        for opener, closer in UNCLOSED_OPENERS.items():
            paragraph_break = None
            for match in re.finditer(re.escape(opener), text):
                start = match.start() + 1
                if text.startswith(closer, start):  # “” is closed, just empty
                    continue
                if match.start() in closed:
                    continue
                index = bisect_right(starts, start) - 1
                if index >= 0 and merged[index][1] >= start:
                    continue
                # Openers are ordered: reuse this paragraph's end, including
                # the absence of another break, as _paired_spans does.
                if paragraph_break is None or 0 <= paragraph_break < start:
                    paragraph_break = _next_blank_line(text, start)
                spans.append((start, len(text) if paragraph_break == -1 else paragraph_break))
    return spans


def get_interior_dash_ambiguities(text, convention):
    """Return candidate utterance offsets without classifying them as speech."""
    if convention != "dash_lines":
        return []
    return [(match.start(1), match.end(1)) for match in INTERIOR_DASH_TURN.finditer(text)
            if any(character.isalpha() for character in match.group(1))]


def _normalize(value):
    return re.sub(r"\s+", " ", _QUOTE_MARKS_RE.sub("", value or "")).strip()


def _normalize_with_offsets(value):
    """Return normalized text plus each character's offset in the raw text."""
    characters, offsets = [], []
    pending_space = None
    for index, character in enumerate(value or ""):
        if character in QUOTE_MARKS:
            continue
        if character.isspace():
            if characters and pending_space is None:
                pending_space = index
            continue
        if pending_space is not None and characters:
            characters.append(" ")
            offsets.append(pending_space)
        pending_space = None
        characters.append(character)
        offsets.append(index)
    return "".join(characters), offsets


class SourceSpanError(ValueError):
    def __init__(self, reason_code, *, fragment_number=None, entry_number=None):
        self.reason_code = reason_code
        self.fragment_number = fragment_number
        self.entry_number = entry_number
        super().__init__(f'Source span verification failed: reason={reason_code}; '
                         f'entry={entry_number}; fragment={fragment_number}')


def get_source_span_fragments(source_text, original_text, source_span, fragments):
    """Map an ordered partition within one established raw-source interval."""
    if not isinstance(source_text, str):
        raise SourceSpanError('source_unavailable', fragment_number=1)
    if (not isinstance(source_span, (list, tuple))
            or len(source_span) != 2 or any(type(offset) is not int for offset in source_span)):
        raise SourceSpanError('invalid_span', fragment_number=1)
    start, end = source_span
    if not 0 <= start < end <= len(source_text):
        raise SourceSpanError('span_out_of_bounds', fragment_number=1)
    normalized, offsets = _normalize_with_offsets(source_text[start:end])
    needles = [_normalize(fragment) for fragment in fragments]
    if not normalized or normalized != _normalize(original_text):
        raise SourceSpanError('source_text_mismatch', fragment_number=1)
    cursor = 0
    for number, needle in enumerate(needles, 1):
        if not needle or not normalized.startswith(needle, cursor):
            raise SourceSpanError('fragment_partition_mismatch', fragment_number=number)
        cursor += len(needle)
        if number < len(needles):
            if normalized[cursor:cursor + 1] != ' ':
                raise SourceSpanError('fragment_partition_mismatch', fragment_number=number)
            cursor += 1
    if not needles or cursor != len(normalized):
        raise SourceSpanError('fragment_partition_mismatch', fragment_number=len(needles) or 1)
    spans, cursor, previous_end = [], 0, start
    for number, needle in enumerate(needles, 1):
        left = start + offsets[cursor]
        right = start + offsets[cursor + len(needle) - 1] + 1
        if (not start <= left < right <= end or left < previous_end
                or _normalize(source_text[left:right]) != needle):
            raise SourceSpanError('fragment_offsets_unverified', fragment_number=number)
        spans.append([left, right])
        previous_end = right
        cursor += len(needle) + 1
    return spans


def get_source_match_positions(source, needles):
    """Index all exact pattern occurrences in one shared source traversal."""
    edges, failures, outputs, terminal = [{}], [0], [0], [None]
    positions = {needle: [] for needle in needles if needle}
    for needle in positions:
        node = 0
        for character in needle:
            child = edges[node].get(character)
            if child is None:
                child = len(edges)
                edges[node][character] = child
                edges.append({})
                failures.append(0)
                outputs.append(0)
                terminal.append(None)
            node = child
        terminal[node] = needle
    queue = deque(edges[0].values())
    while queue:
        node = queue.popleft()
        for character, child in edges[node].items():
            fallback = failures[node]
            while fallback and character not in edges[fallback]:
                fallback = failures[fallback]
            failures[child] = edges[fallback].get(character, 0)
            failed = failures[child]
            outputs[child] = failed if terminal[failed] is not None else outputs[failed]
            queue.append(child)
    node = 0
    for index, character in enumerate(source):
        while node and character not in edges[node]:
            node = failures[node]
        node = edges[node].get(character, 0)
        matched = node if terminal[node] is not None else outputs[node]
        while matched:
            needle = terminal[matched]
            positions[needle].append(index - len(needle) + 1)
            matched = outputs[matched]
    return positions


def mark_entries(entries, source_text, convention=None, speaker_names=None):
    """-> a NEW list of entries carrying `spoken` and `source_span`.

    Entries are matched forward through the source, in order, because the same
    short line ("Oh.") occurs many times and the first match is rarely the
    right one. A line that cannot be located is left unmarked rather than
    guessed: `spoken` absent means "not established", which is a different
    claim from `spoken: false`.
    """
    convention = detect_convention(source_text) if convention is None else convention
    spans = spoken_spans(source_text, convention)
    ambiguities = get_interior_dash_ambiguities(source_text, convention)
    other_speech = get_other_speech_spans(source_text, convention)
    entries = list(entries)
    if speaker_names is None:
        speaker_names = {str(entry.get('speaker') or '') for entry in entries}
    speaker_names = tuple(speaker_names)
    # Printed speaker labels, keyed by where their quote begins. Only for books
    # that use the convention as a rule: three stray matches in a book that
    # does not would attribute three lines from noise.
    labels = (dict(speaker_labels(source_text, speaker_names=speaker_names))
              if uses_speaker_labels(source_text, speaker_names=speaker_names) else {})
    normalized_source, raw_offsets = _normalize_with_offsets(source_text)
    marked, cursor = [], 0
    used_positions = {}
    match_positions = None
    for entry_index, entry in enumerate(entries):
        row = dict(entry)
        row.pop('source_speaker', None)
        row.pop('source_span', None)
        row.pop('spoken', None)
        row.pop('dialogue_ambiguity', None)
        needle = _normalize(str(entry.get("text", "")))
        if needle:
            if match_positions is None:
                normalized_where = normalized_source.find(needle, cursor)
                if normalized_where == -1:
                    match_positions = get_source_match_positions(
                        normalized_source,
                        (_normalize(str(item.get("text", "")))
                         for item in entries[entry_index:]))
            if match_positions is not None:
                occurrences = match_positions.get(needle, [])
                position = bisect_left(occurrences, cursor)
                used = used_positions.get(needle, set())
                normalized_where = next((where for where in occurrences[position:]
                                         if where not in used), -1)
                if normalized_where == -1:
                    normalized_where = next((where for where in occurrences
                                             if where not in used), -1)
            if normalized_where != -1:
                used_positions.setdefault(needle, set()).add(normalized_where)
                cursor = normalized_where + len(needle)
                where = raw_offsets[normalized_where]
                end = raw_offsets[normalized_where + len(needle) - 1] + 1
                overlap = any(start < end and where < stop
                              for start, stop in spans)
                if not overlap and any(start < end and where < stop
                                       for start, stop in ambiguities):
                    row["dialogue_ambiguity"] = "interior_dash_turn"
                elif not overlap and any(start < end and where < stop
                                         for start, stop in other_speech):
                    # Outside the main quotes, inside speech marked another
                    # way: not established narration, so never voiced as it.
                    row["dialogue_ambiguity"] = "other_convention"
                else:
                    row["spoken"] = bool(overlap)
                row["source_span"] = [where, end]
                # THE AUTHOR'S OWN ANSWER, where the book prints one. Checked
                # against the model on arc4_volume10wn: they agree on 2,909 of
                # 2,967 lines, and every disagreement is the model misspelling
                # the printed name - "LONG HAILED GIRL" for "Long Haired
                # Girl". So this is not a second opinion, it is the source.
                printed = next((labels[p] for p in (where - 1, where - 2, where)
                                if p in labels), None)
                if printed:
                    row["source_speaker"] = printed
        marked.append(row)
    return marked


def apply_dialogue_map(entries, source_text, speaker_names=None):
    """Return source-mapped entries and reproducible mapping measurements."""
    convention = detect_convention(source_text)
    if not convention:
        return {"entries": list(entries), "convention": None,
                "located": 0, "spoken": 0, "ambiguous": 0, "speaker_changes": [],
                "narration_changes": []}
    mapped = mark_entries(entries, source_text, convention, speaker_names=speaker_names)
    mapped, speaker_changes = apply_source_speakers(mapped)
    mapped, narration_changes = apply_source_narration(mapped)
    ambiguous = sum(1 for entry in mapped if entry.get("dialogue_ambiguity"))
    if ambiguous:
        kinds = Counter(entry["dialogue_ambiguity"] for entry in mapped if entry.get("dialogue_ambiguity"))
        logging.getLogger(__name__).warning(
            "Dialogue map: %s entries need review (%s); spoken status is unestablished.",
            ambiguous, ", ".join(f"{count} {kind}" for kind, count in sorted(kinds.items())))
    return {
        "entries": mapped,
        "convention": convention,
        "located": sum(1 for entry in mapped if "source_span" in entry),
        "spoken": sum(1 for entry in mapped if entry.get("spoken")),
        "ambiguous": ambiguous,
        "speaker_changes": speaker_changes,
        "narration_changes": narration_changes,
    }


def get_source_narration(entry):
    """True/False when the source says narration/speech, None when unestablished."""
    spoken = entry.get("spoken")
    return not spoken if type(spoken) is bool else None


def apply_source_narration(entries):
    """-> (entries, changes) with source-confirmed narration voiced by NARRATOR.

    #1534: a model-split chunk can type narration as speech and the naming pass
    then gives it a character. Where the source's quote marks place the line
    outside every quotation, that is the fact and the character is the guess,
    so the speaker becomes NARRATOR and the change is recorded. A printed
    speaker label is the source too, so a line carrying one is left alone.
    """
    updated, changes = [], []
    for entry in entries:
        row = dict(entry)
        current = str(row.get("speaker") or "")
        if (get_source_narration(row) is True and current != "NARRATOR"
                and not row.get("source_speaker")):
            row["speaker"] = "NARRATOR"
            changes.append({"type": "source_narration", "before": current,
                            "after": "NARRATOR"})
        updated.append(row)
    return updated, changes

# A NAME IMMEDIATELY BEFORE THE QUOTE. Web-novel transcripts often print the
# speaker and then the line:
#
#     Subaru “Say… Petra, isn't this kinda close?”
#     Petra  “No? Is there some problem, Subaru?”
#
# arc4_volume10wn is written this way throughout, which is why its dialogue is
# attributed almost perfectly (2 errors in 3,044 lines) while mushoku18 - blank
# lines and no tag at all, five speech verbs in the whole book - sits near 50%.
# The model is not doing worse on one book; the book is handing it the answer.
#
# Where that answer is printed, copying it is free and deterministic, and it
# needs no model call at all.
LABEL_BEFORE_QUOTE = re.compile(
    r'(?:^|\n)[ \t]*([^\W\d_][\w.\'’-]{1,20}(?:[ \t]+[^\W\d_][\w.\'’-]{1,20}){0,2})'
    r'[ \t]+(?=[' + QUOTE_OPENERS + '])')


def speaker_labels(text, minimum_repeats=3, speaker_names=()):
    """-> [(quote_start, name)] for quotes introduced by a printed speaker name.

    A name must recur before it is believed. One capitalised word before a
    quote is a coincidence - "Suddenly “Get down!"" would qualify - but a token
    that introduces three or more quotes across a book is how that book prints
    its speakers. This is the cheapest possible attribution signal and it is
    exact where it applies, so it must not be extended by guessing: a book
    without the convention gets an empty list, not a weak one. A corroborated
    speaker inventory is required; repetition alone cannot establish a name.
    """
    # Repetition establishes formatting, not identity: sentence adverbs can
    # recur before quotes too. Require a corroborated cast name rather than
    # trying to enumerate every word that could be narration.
    known = {' '.join(name.split()).casefold() for name in speaker_names
             if isinstance(name, str) and name.strip()}
    hits = [(m.end(), " ".join(m.group(1).split())) for m in
            LABEL_BEFORE_QUOTE.finditer(text)]
    hits = [(pos, name) for pos, name in hits
            if name.casefold() in known]
    counts = {}
    for _, name in hits:
        counts[name] = counts.get(name, 0) + 1
    return [(pos, name) for pos, name in hits
            if counts[name] >= minimum_repeats]


def uses_speaker_labels(text, threshold=0.3, speaker_names=()):
    """Does this book print the speaker before the line, as a rule?"""
    spans = spoken_spans(text)
    if not spans:
        return False
    labelled = speaker_labels(text, speaker_names=speaker_names)
    return len(labelled) >= threshold * len(spans)


def apply_source_speakers(entries):
    """-> (entries, changes) with printed speaker labels made authoritative.

    Only where the book printed a name next to the line. Everywhere else the
    entry is untouched: this signal is exact where it exists and silent where
    it does not, and stretching it further would trade its one virtue for
    coverage.
    """
    updated, changes = [], []
    for entry in entries:
        row = dict(entry)
        printed = row.get("source_speaker")
        if printed:
            current = str(row.get("speaker") or "")
            canonical = printed.upper()
            if canonical != current.upper():
                row["speaker"] = canonical
                changes.append({"type": "printed_speaker_label",
                                "before": current, "after": canonical})
        updated.append(row)
    return updated, changes
