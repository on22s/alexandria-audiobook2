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
from collections import Counter, defaultdict, deque
import logging
import re
import unicodedata

from silent_word_characters import (SILENT_WORD_CHARACTERS,
                                    remove_silent_word_characters)

PAIRED = [
    ('"', '"'),
    ("“", "”"),      # curly “ ” (English, Dutch, Turkish, ...)
    ("„", "“"),      # low-high „ “ (German, Czech, Slovak)
    ("„", "”"),      # low-high „ ” (Polish, Hungarian, Croatian, Baltics)
    ("「", "」"),      # 「 」
    ("『", "』"),      # 『 』
    ("＂", "＂"),      # full-width straight
    ("«", "»"),      # guillemets (French, Russian, Italian, ...)
    ("»", "«"),      # reversed guillemets (Danish)
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
    # German low-high is secondary speech in an otherwise non-German book; it
    # is included in PAIRED only when dense enough to be that book's convention.
    # get_other_speech_spans skips pairs already found by _paired_spans.
    re.compile(r"„([^„“\n]+)“"),
    re.compile(r"„([^„”\n]+)”"),
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


def _ambiguous_mark_roles(text):
    """-> (closer offsets, opener offsets) for marks shared between pairs.

    `“` closes `„…“` but opens `“…”`. The resolution walks the marks once with
    an explicit count of pending `„` openers, so a padded or newline-containing
    English quote is treated exactly like an unpadded one: edge whitespace is
    never evidence of an orphan. A `“` closes German when

      * no `”` follows it, or only whitespace sits before that `”` (an orphan),
        and a `„` is still pending; or
      * it has real English content but a later `“` remains to close the German
        pair (English nested inside German, e.g. „He said “Hi” to me.“).

    Otherwise it opens English. Every lookup is by position; measured sources
    see zero copied characters. Straight pairs are excluded: their mark is both
    opener and closer by definition, so it is never ambiguous.

    A stray `„` is recognised when its candidate `“` has real English content
    but no later German close-capable mark in the segment: an unpadded gap
    means the `“` opens English, while a padded one means an orphan `”` after
    narration and the `“` closes German.
    """
    if "„" not in text:
        # No shared mark: every `“` is unambiguously an English opener, and
        # this scan would otherwise cost ~41 ms on a 1.4 MB English book.
        return set(), set()
    all_marks = [(m.start(), m.group()) for m in re.finditer(
        "[" + "".join(re.escape(mark) for mark in QUOTE_MARKS) + "]", text)]
    positions = [pos for pos, _ in all_marks]
    mark_at = dict(all_marks)
    quotes = [pos for pos, mark in all_marks if mark == "“"]

    def next_english_mark(pos):
        """Next mark that can pair with `“`, skipping self-paired straight ones.

        In `„He said “it's "5" wide” to me.“` the `"` marks pair among
        themselves; letting them decide the next mark misreads the following
        `“` as a German closer and fragments the utterance.
        """
        index = bisect_right(positions, pos)
        while index < len(positions) and mark_at[positions[index]] in ('"', '＂'):
            index += 1
        return positions[index] if index < len(positions) else None

    def can_close_german(pos):
        """A `“` can close a pending `„`: no `”`, or only whitespace to it."""
        closer = next_english_mark(pos)
        if closer is None or mark_at[closer] != "”":
            return True
        for at in range(pos + 1, closer):
            if not text[at].isspace():
                return False
        return True

    closer_candidate = {pos: can_close_german(pos) for pos in quotes}
    # A later close-capable `“` only justifies nesting an English pair if it
    # belongs to the SAME German segment — an intervening `„` starts a new one.
    # Without this, `„Red“ narration ”stray „Blue.“` merges two speeches and
    # labels the narration between them as spoken.
    de_openers = [pos for pos, mark in all_marks if mark == "„"]
    segment = {pos: bisect_right(de_openers, pos) for pos in quotes}
    closer_indices = defaultdict(list)
    for index, pos in enumerate(quotes):
        if closer_candidate[pos]:
            closer_indices[segment[pos]].append(index)

    def later_closer_in_segment(pos, after_index):
        candidates = closer_indices.get(bisect_right(de_openers, pos), ())
        found = bisect_left(candidates, after_index)
        return found < len(candidates)

    closes, opens = set(), set()
    de_stack = []
    for pos, mark in all_marks:
        if mark == "„":
            de_stack.append(pos)
        elif mark == "“":
            closer = next_english_mark(pos)
            has_closer = closer is not None and mark_at[closer] == "”"
            if not closer_candidate[pos] and has_closer:
                # Real English content: nested only if a later `“` in this same
                # German segment can still close the pair. With no later `“`,
                # a real pending `„` (text directly after it) is closed here;
                # only a whitespace-padded `„` is a stray mark.
                after = bisect_right(quotes, pos)
                stray = False
                if after == len(quotes):
                    if de_stack:
                        # A space before `“` means the pending `„` was a stray
                        # mark („ narration “Real speech.”); a real German
                        # closer follows the speech text directly (da.“).
                        stray = pos > 0 and text[pos - 1].isspace()
                        if not stray:
                            opener = de_stack[-1] + 1
                            stray = opener >= len(text) or text[opener].isspace()
                        opens_english = stray
                    else:
                        opens_english = True
                elif later_closer_in_segment(pos, after):
                    opens_english = True
                else:
                    # No later German closer in this segment. A padded gap
                    # (`” narration ”`) means this `“` closes German; an
                    # unpadded one is real English speech after a stray `„`.
                    stray = not (closer == pos + 1
                                 or text[pos + 1].isspace()
                                 or text[closer - 1].isspace())
                    opens_english = stray
            else:
                opens_english = False
                stray = False
            if opens_english or not de_stack:
                opens.add(pos)
                if stray and de_stack:
                    # The pending `„` was a stray mark, not a real opener.
                    de_stack.pop()
            else:
                closes.add(pos)
                de_stack.pop()
    return closes, opens


MIN_CONVENTION_PAIRS = 5

_CLOSE_FOLLOW = r"[\s,.;:!?…]"


def _count_guillemet_shapes(text, opener, closer):
    """-> how many marks are shaped like this direction's opener or closer.

    An opener begins a line or sits after whitespace with text right after it;
    a closer sits right after text, or ends its line, with only punctuation or
    whitespace after it. A mark padded on both sides mid-line (`Menu » Next`)
    is neither, so it casts no vote.
    """
    opened = re.findall(
        rf"(?m)^[ \t]*{opener}|(?<!\S){opener}(?=\S)", text)
    closed = re.findall(
        rf"(?m)(?<=\S){closer}(?={_CLOSE_FOLLOW}|$)|{closer}[ \t,.;:!?…]*$", text)
    return len(opened) + len(closed)


def get_guillemet_direction(text):
    """-> (opener, closer) for the text's guillemets, or (None, None).

    A text uses one direction: `«…»` (French, Russian, ...) or `»…«` (Danish,
    German chevrons). Pairing both directions independently makes every closer
    an opener too and labels the narration between speeches as spoken.

    Decided by how the marks sit against the text, not by which comes first:
    one stray mark before the first speech (a breadcrumb `Menu » Chapitre`)
    otherwise flipped the direction for the whole book and inverted every
    speech into narration. The first mark decides only a tie, which keeps the
    old behaviour where the shapes say nothing (`« Bonjour »` padded on both
    sides mid-line). Pure read; the one place both modules ask this question.
    """
    first_open, first_close = text.find("«"), text.find("»")
    if first_open == -1 and first_close == -1:
        return None, None
    forward = _count_guillemet_shapes(text, "«", "»")
    reverse = _count_guillemet_shapes(text, "»", "«")
    if forward == reverse:
        forward_first = first_open != -1 and (
            first_close == -1 or first_open < first_close)
        return ("«", "»") if forward_first else ("»", "«")
    return ("«", "»") if forward > reverse else ("»", "«")


def _is_opener_shaped(text, position):
    """Is the mark at `position` followed by text and preceded by whitespace?

    A straight mark cannot say whether it opens or closes. One padded on both
    sides (`a " thing`) or glued to a word on its left is not where a speech
    begins.
    """
    before = text[position - 1] if position else "\n"
    after = text[position + 1] if position + 1 < len(text) else "\n"
    return before.isspace() and not after.isspace()


def _is_glued_opener_shaped(text, position):
    """Is the mark at `position` glued to the reply it starts, not a closer?

    A mark is closer-shaped when text precedes it and a word boundary
    follows. A speech that follows narration can start the same way, because
    the narration ends in punctuation instead of a space: after a dash,
    colon, bracket, slash, semicolon, ellipsis, symbol or a full stop without
    a space (`he said—"...no`, `:"…fine"`, `("...ok"`, `he said."...No."`).

    Shape, not a separator list: the mark opens when it is glued to what
    follows and that is a reply - a word, or an ellipsis, full stop or
    apostrophe run. A word after it needs no letter or digit before the mark
    (`he said"s` is narration, not a close); a punctuation-led reply may
    follow a letter too (`he said"...no`), because a closer glued straight to
    a word is not a word boundary and so was never closer-shaped anyway.
    """
    if position <= 0 or position + 1 >= len(text):
        return False
    before = text[position - 1]
    after = text[position + 1]
    if after.isspace():
        return False
    if after.isalpha():
        return not before.isalnum()
    return after in ".…'"


def _skip_spaces(text, index):
    """-> offset of the first non-space at or after `index`."""
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


_CLOSERS_BY_OPENER = {}
for _pair_opener, _pair_closer in PAIRED:
    _CLOSERS_BY_OPENER.setdefault(_pair_opener, set()).add(_pair_closer)
# Every mark a convention pairs, so a measurement can end with a different
# quote character than the inch mark that opened it (`6" and 8”`, `6" and 8»`).
_PAIRED_MARKS = frozenset(mark for pair in PAIRED for mark in pair)


def _closer_marks(opener):
    """-> the mark characters that can close a span this opener began."""
    return _CLOSERS_BY_OPENER.get(opener, set())


_NUMBER_CATEGORIES = frozenset(("Nd", "No"))
# Words that join two measurements (`6" by 8"`) or name the unit after one.
_MEASURE_LINKS = frozenset(("by", "x", "and", "to"))
_MEASURE_UNITS = frozenset((
    "wide", "long", "tall", "high", "deep", "thick", "thin", "broad",
    "across", "square", "cubic", "diameter", "radius",
    "inch", "inches", "foot", "feet", "ft", "cm", "mm", "meter", "meters",
    "metre", "metres", "kilometer", "kilometre", "mile", "miles", "pound",
    "pounds", "lb", "lbs", "oz", "kg", "gram", "grams", "g", "litre",
    "liter", "litres", "liters", "gallon", "gallons"))


def is_number_char(char):
    """Is `char` a digit for spellings like `6"`, `3½"` and `8²"`?

    Unicode `Nd` and `No` cover ASCII digits, `½`/`¼` and `²`; a CJK numeral
    such as `三` is a letter (`Lo`) and is not one.
    """
    return unicodedata.category(char) in _NUMBER_CATEGORIES


# A one-entry cache keyed by text identity. Two threads alternating texts
# evict each other, so each rescans (measured: 0.50 s for 4 threads against
# 0.30 s serial); the result stays correct, only the scan repeats. Threading a
# per-call index down would remove the rescan and these two module globals, but
# each predicate (`is_inch_mark`, `_paired_spans`, pass_quality's analyzer)
# would have to carry it; the single-entry cache is kept because it is inert
# correctness-wise and the cost is one paragraph scan per text (#2318 review).
_blank_line_cache = (None, ())


def _paragraph_end(text, position):
    """-> offset of the blank line that ends the paragraph holding `position`."""
    global _blank_line_cache
    cached_text, offsets = _blank_line_cache
    if cached_text is not text:
        # One scan per text, then bisect. Searching from each digit mark was
        # quadratic on a text with no blank line at all.
        offsets = tuple(match.start() for match in BLANK_LINE.finditer(text))
        _blank_line_cache = (text, offsets)
    index = bisect_right(offsets, position)
    return offsets[index] if index < len(offsets) else len(text)


def _ends_paragraph(text, position):
    """Does the paragraph holding `position` end at or after it?"""
    tail = text[position + 1:]
    return not tail.strip() or bool(BLANK_LINE.match(tail))


def _paragraph_start(text, position):
    """-> offset where the paragraph holding `position` begins."""
    index = position
    while index > 0:
        found = text.rfind("\n", 0, index)
        if found == -1:
            return 0
        probe = found + 1
        while probe < position and text[probe] in " \t":
            probe += 1
        if probe < position and text[probe] in "\r\n":
            return probe + 1
        index = found
    return 0


def _odd_earlier_marks(text, position, mark):
    """Does this paragraph hold an unmatched earlier `mark` before `position`?

    Counted over marks that are not number-preceded, since a number-preceded
    mark is never an opener and may itself be an inch mark.
    """
    start = _paragraph_start(text, position)
    count = 0
    found = text.find(mark, start, position)
    while found != -1:
        if found == 0 or not is_number_char(text[found - 1]):
            count += 1
        found = text.find(mark, found + 1, position)
    return count % 2 == 1


_mark_index_cache = (None, {})


def _mark_index(text, mark):
    """-> (all offsets, offsets not number-preceded) of `mark`, cached per text.

    Indexed once per text and tested by bisect. Searching forward from every
    digit mark was quadratic on a text whose paragraph holds no plain closer.
    One entry keyed by text identity, so threads alternating texts rescan
    rather than share; correct, and the rescan is one pass per text.
    """
    global _mark_index_cache
    cached_text, index = _mark_index_cache
    if cached_text is not text:
        index = {}
        _mark_index_cache = (text, index)
    entry = index.get(mark)
    if entry is None:
        every, plain = [], []
        found = text.find(mark)
        while found != -1:
            every.append(found)
            if found == 0 or not is_number_char(text[found - 1]):
                plain.append(found)
            found = text.find(mark, found + 1)
        entry = (tuple(every), tuple(plain))
        index[mark] = entry
    return entry


def _next_mark(text, mark, start, end, plain_only=False):
    """-> first `mark` offset in [start, end), optionally not number-preceded."""
    every, plain = _mark_index(text, mark)
    offsets = plain if plain_only else every
    index = bisect_left(offsets, start)
    return offsets[index] if index < len(offsets) and offsets[index] < end else -1


def _next_plain_mark(text, marks, start, end):
    """-> first occurrence of any of `marks` in [start, end) not number-preceded."""
    best = -1
    for mark in marks:
        found = _next_mark(text, mark, start, end, plain_only=True)
        if found != -1 and (best == -1 or found < best):
            best = found
    return best


def _next_any_mark(text, marks, start, end):
    """-> first occurrence of any of `marks` in [start, end), digit or not."""
    best = -1
    for mark in marks:
        found = _next_mark(text, mark, start, end)
        if found != -1 and (best == -1 or found < best):
            best = found
    return best


def _number_ends_at(text, index):
    """-> offset just past the number starting at `index`, or -1 if none.

    A decimal separator is part of the number only when a digit follows it,
    so `8.5` and `8,5` are one number while `8, next` is not.
    """
    end = index
    while end < len(text):
        if is_number_char(text[end]):
            end += 1
        elif (text[end] in ",."
              and end + 1 < len(text) and is_number_char(text[end + 1])):
            end += 2
        else:
            break
    return end if end > index else -1


def _number_ends_at_a_mark(text, index):
    """Does a number starting at `index` end at a quote mark (`8"`)?

    The second half of a measurement: a linked number (`6" by 8"`, `6" and
    8”`) or one that follows a unit word (`6" wide, 8"`). Without the mark
    the number is narration (`5" and 3 of them nodded`).
    """
    number_end = _number_ends_at(text, index)
    return (number_end != -1 and number_end < len(text)
            and text[number_end] in _PAIRED_MARKS)


def _unit_word_ends_measurement(text, index):
    """Does the unit word at `index` finish the measurement inside its clause?

    The same requirement the link branch has: what follows must confirm the
    reading. Confirmed by the end of the text, a quote mark, or a comma, `×`
    or link word whose number a quote mark follows (`6" wide and 8"`). Any
    other word, or a sentence end with text after it, starts narration
    (`Room 12" deep in the cellar`, `Room 12" deep. The shelf was 8" deep.`),
    so the `12"` mark must close the speech (#2318).
    """
    index = _skip_spaces(text, index)
    if index >= len(text):
        return True
    if text[index] in _PAIRED_MARKS:
        return True
    if text[index] in ",×":
        return _number_ends_at_a_mark(text, _skip_spaces(text, index + 1))
    end = index
    while end < len(text) and text[end].isalpha():
        end += 1
    if text[index:end].casefold() in _MEASURE_LINKS:
        return _number_ends_at_a_mark(text, _skip_spaces(text, end))
    return False


def is_measurement_continuation(text, position):
    """Does the text after this number-preceded mark still read as a measurement?

    Consulted when no plain closer is left in the paragraph (every later one
    is number-preceded). Unambiguous evidence: a unit word that finishes the
    measurement in its own clause (`6" wide`, `6" wide and 8"`), or a second
    number linked by `by`/`x`/`×`/`and`/`,`/`to` that is itself followed by a
    quote mark (`6" by 8"`, and the mixed `6" and 8”`). A link word and a
    number in narration (`5" and 3 of them nodded`) are not evidence, because
    no quote mark follows the second number; that closes the speech (#2318).
    A unit word followed by narration (`Room 12" deep in the cellar`, `Room
    12" deep. The shelf was 8" deep.`) is not evidence either, and closes the
    speech. Anything else closes it (`12" she said`).
    """
    index = _skip_spaces(text, position + 1)
    if index >= len(text):
        return False
    if text[index] in ",×":
        return _number_ends_at_a_mark(text, _skip_spaces(text, index + 1))
    end = index
    while end < len(text) and text[end].isalpha():
        end += 1
    word = text[index:end].casefold()
    if word in _MEASURE_LINKS:
        return _number_ends_at_a_mark(text, _skip_spaces(text, end))
    if word in _MEASURE_UNITS:
        return _unit_word_ends_measurement(text, end)
    return False


def is_inch_mark(text, position, mark, inside_speech=False, opener=None):
    """Is the number-preceded `mark` at `position` a measurement, not a delimiter?

    Outside speech a measurement is a measurement anywhere, including at the
    end of a line (`The board was 6"`). A paragraph-final mark is a reprise
    closer instead when the paragraph holds an odd number of earlier plain
    marks (`"Part two ends 5"`), which is how a continued speech prints.

    Inside open speech the number may instead be the end of the sentence, so
    the next mark decides:

      - the same mark (a self-paired `"`): an inch mark unless the next
        non-number-preceded same mark is closer-shaped (text before it, word
        boundary after it), because that one would then have to open;
      - a different opener (`“`, `„`, `«`, `「`, ...): an inch mark unless
        that opener's own non-number-preceded closer comes before any new
        opener of it.

    When every later closer is number-preceded, none of them can close the
    speech, and `is_measurement_continuation` decides from the text after the
    mark; otherwise this mark closes.

    A mark with no later closer before the next opener (or the paragraph end)
    can be the only way to close the speech, so it closes whatever word
    follows. A speech continued into the next paragraph is not distinguished:
    the next paragraph opens with the same mark whether it continues this
    speech or begins a new one, so a number-preceded mark with no later closer
    always closes (#2314).

    One predicate for `_paired_spans`, pass_quality's analyzer and
    `_has_quote_delimiter`, and the three-pass repair (#2314).
    """
    if position <= 0 or not is_number_char(text[position - 1]):
        return False
    if mark == "“":
        # German low-high (`„…“`) shares its closer `“` with the English
        # opener `“…”`, so deciding a digit-preceded `“` needs the role
        # classifier and this predicate to share one decision; scoped out to
        # keep German low-high identical to main (#2357).
        return False
    after = position + 1
    while after < len(text) and text[after].isspace():
        after += 1
    if not inside_speech and after < len(text) and text[after] == mark:
        # `12" "Next` and `12""Next`: the mark before the new quote is an
        # inch mark, not that quote's delimiter.
        return True
    if inside_speech and opener is not None and opener != mark:
        paragraph_end = _paragraph_end(text, position)
        closers = _closer_marks(opener)
        next_opener = _next_mark(text, opener, position + 1, paragraph_end)
        if opener in closers:
            # A self-paired opener (＂…＂) cannot tell its own marks apart, so
            # any later one may be the closer.
            bound = paragraph_end
        else:
            # A closer after the next opener of this speech belongs to that
            # speech, so only marks before it can close this one.
            bound = paragraph_end if next_opener == -1 else next_opener
        next_closer = _next_plain_mark(
            text, closers, position + 1, bound)
        if next_closer != -1 and _is_glued_opener_shaped(text, next_closer):
            # A mark that opens the next speech cannot close this one, and
            # neither can anything after it (`“Room 5” he said. Later, ”no”
            # came back.`, #2318).
            next_closer = -1
        if next_closer == -1:
            later = _next_any_mark(text, closers, position + 1, bound)
            if later == -1:
                # Nothing else can close this speech, so this mark closes,
                # whatever word follows (`“It was 1945” long ago.`).
                return False
            # Only number-preceded marks are left; one of them must become
            # the closer, and the text after this one decides
            # (`6” by 8”` keeps it, `12” she said` closes).
            return is_measurement_continuation(text, position)
        if next_closer == position + 1 and text[next_closer] == mark:
            # `“12””`: the second same mark is the real closer and this one is
            # its decoration, as main reads it.
            return False
        return next_opener == -1 or next_closer < next_opener
    if position + 1 >= len(text) or text[position + 1] in "\r\n":
        if inside_speech:
            return False
        if (_ends_paragraph(text, position)
                and _odd_earlier_marks(text, position, mark)):
            # A paragraph that re-opened the quote has no closer but this one,
            # so it still closes. With balanced earlier marks it cannot. The
            # parity caller cannot see depth at all; the analyzer reaches this
            # only outside speech, where the same imbalance is the only clue
            # that a CRLF paragraph re-opened its quote (#2314).
            return False
        return True
    if not inside_speech:
        return True
    paragraph_end = _paragraph_end(text, position)
    next_position = _next_plain_mark(text, (mark,), position + 1, paragraph_end)
    if next_position == -1:
        if _next_mark(text, mark, position + 1, paragraph_end) == -1:
            # This mark is the only thing that can close the speech, so it
            # does.
            return False
        return is_measurement_continuation(text, position)
    # Closer-shaped means text before the mark and a word boundary after it.
    # A mark glued to a reply that follows a dash, colon, bracket, slash,
    # semicolon, ellipsis, symbol or no-space full stop is an opener whatever
    # follows it (`—"...no`, `:"…fine"`, `("...ok"`, `he said."...No."`), so
    # it decides nothing here and this mark closes as always.
    if _is_glued_opener_shaped(text, next_position):
        return False
    following = next_position + 1
    return (not text[next_position - 1].isspace()
            and (following >= len(text) or not text[following].isalnum()))


def _paired_spans(text):
    spans = []
    german_ranges = []
    used_de = set()  # a `„` consumed by one closer convention is not reused
    guillemet_opener = get_guillemet_direction(text)[0]
    breaks = starts = None
    masks = {}
    # `“` closes „…“ but also opens “…”. Resolve each occurrence once, then skip
    # a closer-offset when looking for openers and vice versa.
    closes, opens = _ambiguous_mark_roles(text)

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
        if opener in ("«", "»") and opener != guillemet_opener:
            continue
        before = len(spans)
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
                # A mark immediately after a number can be an inch mark (5"
                # wide), outside a span or inside one; the predicate answers
                # from the mark's own context. Parity is all this pairing
                # has, so a paragraph-final mark stays a reprise closer while
                # the paragraph holds an unmatched earlier mark.
                if is_inch_mark(text, position, opener,
                                len(positions) % 2 == 1):
                    continue
                positions.append(position)
            # A mark that begins a paragraph re-opens this quote, so it can
            # never be a closer. Precompute the next non-reopening mark once;
            # the old inner scan restarted at every opener and was quadratic
            # on a straight-quote reprise chain.
            paragraph_starts = prepare()[1]
            all_breaks = [at for at, _after in prepare()[0]]
            next_plain = [len(positions)] * len(positions)
            last = len(positions)
            for candidate_index in range(len(positions) - 1, -1, -1):
                next_plain[candidate_index] = last
                if positions[candidate_index] not in paragraph_starts:
                    last = candidate_index

            index = 0
            while index + 1 < len(positions):
                start = positions[index]
                candidate_index = next_plain[index]
                while candidate_index < len(positions) and positions[candidate_index] - start <= 1:
                    candidate_index = next_plain[candidate_index]
                if candidate_index >= len(positions):
                    index += 1
                    continue
                end = positions[candidate_index]
                if not _is_unbroken(narration_breaks(opener), start, end):
                    # Every later mark crosses that same break too.
                    index += 1
                    continue
                if (not _is_opener_shaped(text, start)
                        and not _is_unbroken(all_breaks, start, end)):
                    # A reprise break looks the same after a real opener and
                    # after a stray mark in narration (`a " thing.\n\n"Go,"`).
                    # Only an opener-shaped mark may carry a pair across one;
                    # otherwise the narration up to the next speech is spoken.
                    index += 1
                    continue
                spans.append((start + 1, end))
                index = candidate_index + 1
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
            def next_opener(search_from):
                candidate = text.find(opener, search_from)
                while candidate != -1 and (candidate in closes or candidate in used_de):
                    candidate = text.find(opener, candidate + 1)
                return candidate

            def next_closer(search_from):
                """-> the next closer offset that neither opens English nor is an inch mark, or -1.

                A mark the role classifier calls an English opener (`opens`,
                populated only for `“`) is skipped. For every convention other
                than German low-high (`„…“`, where `is_inch_mark` returns False
                so `main`'s behaviour is preserved, #2357), a number-preceded
                inch mark is skipped while a real closer remains ahead.
                """
                candidate = text.find(closer, search_from)
                while candidate != -1 and (
                        candidate in opens
                        or is_inch_mark(text, candidate, closer, True,
                                        opener=opener)):
                    candidate = text.find(closer, candidate + 1)
                return candidate

            position = next_opener(0)
            end, bad_breaks = 0, None
            while position != -1:
                # Rejected openers can share one distant closer. Cache that
                # forward search and inspect offsets without copying every
                # remaining interior substring.
                if end <= position:
                    end = next_closer(position + 1)
                if end == -1:
                    break
                if (opener == "„" and closer == "”"
                        and text.find("“", position + 1, end) != -1):
                    # An English “ opener before this ” means the mark belongs
                    # to English speech, not to a Polish low-high pair.
                    position = next_opener(position + 1)
                    continue
                if bad_breaks is None:
                    bad_breaks = narration_breaks(opener)
                if position + 1 < end and _is_unbroken(bad_breaks, position, end):
                    spans.append((position + 1, end))
                    if opener == "„":
                        used_de.add(position)
                    position = next_opener(end + 1)
                else:
                    position = next_opener(position + 1)
        if opener == "„":
            german_ranges.append((before, len(spans)))
    if german_ranges and len(spans) >= MIN_CONVENTION_PAIRS:
        count = sum(end - start for start, end in german_ranges)
        others = len(spans) - count
        if count and (count < MIN_CONVENTION_PAIRS or count < others):
            # A lone low-high quote in a book whose dialogue uses another mark
            # is usually a sign, foreign term or title — not speech. Surface it
            # as other speech instead of giving it a character voice.
            for start, end in reversed(german_ranges):
                del spans[start:end]
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
    if counts[best] < MIN_CONVENTION_PAIRS:
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
    pairs = sorted(_paired_spans(text))
    closed = {stop for _begin, stop in pairs}
    merged = []
    for begin, stop in pairs:
        if merged and begin <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], stop))
        else:
            merged.append((begin, stop))
    starts = [begin for begin, _stop in merged]

    def inside_primary(start, end):
        index = bisect_right(starts, start) - 1
        return index >= 0 and merged[index][1] >= end

    spans = [(m.start(1), m.end(1)) for pattern in OTHER_PAIRED for m in pattern.finditer(text)
             if BLANK_LINE.search(text, m.start(1), m.end(1)) is None
             and not inside_primary(m.start(1), m.end(1))]
    if convention != "dash_lines":
        spans += [(m.start(1), m.end(1)) for m in DASH_LINE.finditer(text)]
    if convention != "paired_quotes":
        spans += pairs
    else:
        # An opening mark with no partner: speech to the end of its paragraph.
        # Membership is one bisect into the merged pairs, not a scan of all of
        # them per opener, which made well-formed books quadratic. The mark that
        # CLOSES a real pair is skipped: `“` closes `„…“`, so without this every
        # German speech ended in a phantom "unclosed opener".
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
    return re.sub(r"\s+", " ", _QUOTE_MARKS_RE.sub(
        "", remove_silent_word_characters(value or ""))).strip()


def _normalize_with_offsets(value):
    """Return normalized text plus each character's offset in the raw text."""
    characters, offsets = [], []
    pending_space = None
    for index, character in enumerate(value or ""):
        if character in QUOTE_MARKS or character in SILENT_WORD_CHARACTERS:
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
_SILENT_IN_WORD = "".join(SILENT_WORD_CHARACTERS)
LABEL_BEFORE_QUOTE = re.compile(
    # A label may begin with an invisible character (#2316); allow those before
    # the name and fold the captured name below.
    r'(?:^|\n)[ \t' + _SILENT_IN_WORD + r']*([^\W\d_][\w.\'’'
    + _SILENT_IN_WORD +
    r'-]{1,20}(?:[ \t]+[^\W\d_][\w.\'’'
    + _SILENT_IN_WORD +
    r'-]{1,20}){0,2})'
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
    known = {remove_silent_word_characters(' '.join(name.split())).casefold()
             for name in speaker_names
             if isinstance(name, str) and name.strip()}
    hits = [(m.end(), remove_silent_word_characters(" ".join(m.group(1).split())))
            for m in LABEL_BEFORE_QUOTE.finditer(text)]
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
