"""#2331: speech in another quote convention nested inside single-quoted speech.

In a book whose dialogue uses single curly quotes, a chunk that contains a
nested double-quoted phrase was segmented inside out: the nested phrase became
the only speech and the real dialogue became narration. The quote decision
accepted that inverted split and the segment gate rejected the correct one with
mixed_quote_region.

A full single-quote pairing model is out of scope. This fix abstains: when a
double-quoted phrase sits inside matched single-quoted speech, the double quotes
are inner speech, not the chunk's outer convention, so the model is asked. The
same analysis is the gate, so the correct split still has to pass.

The narrow decision is deliberate (rev1 review):
  - only a single-quoted span that nests a paired quote is adopted; a quoted
    title in narration stays narration;
  - nothing is adopted when the chunk already pairs double quotes outside every
    single span, so a double-quoted book keeps main's analysis byte for byte;
  - the gate allows a nested quote mark by POSITION inside an adopted span, not
    by character, so a row that keeps the real outer delimiters still fails.

The rev2 review narrowed it further:
  - in a targeted chunk the gate also accepts a SPOKEN row equal to a complete
    single-quoted span the analysis did not adopt, so the model's correct split
    of sibling speeches passes; "quotes" then abstains from certifying the
    analysis split (`quote_forced`) because it has no model to ask;
  - a possessive after the nested quote (`… the dogs’ bones are here,’`) is not
    the closer;
  - a single-quoted quotation nested inside the speech (`… to ‘Bob’ then …`)
    does not end the outer span; unresolved nesting abstains from adoption;
  - inch marks are measurements, not nested quotes, in both directions;
  - the paragraph search is bounded, and the straight elision guard is tested
    without a second outside double pair.

The rev3 review closed the issue's own `…s’` case and bounded the scan:
  - the possessive lookahead stops at a candidate sibling speech opener, so
    `‘Yes’ she said. ‘He said “run”,’ he said.` finds both spans, abstains, and
    the truthful four-entry split passes the gate on both paths;
  - a failed range that nests a paired quote marks the chunk uncertain:
    `auto`/`lexical` abstain and `quotes` returns `quote_forced`, never
    `quote_presegmented` (the dropped-closer repro, and the British
    multi-paragraph limit);
  - an adversarial all-opener input resumes after a failed range instead of
    rescanning every opener inside it;
  - the three guards that a single removal left green now have their own
    decision/predicate cases without an outside double pair.

The rev4 review repaired the span model itself:
  - a candidate opener that is itself a closed nested quotation is not a
    sibling speech, so `dogs’ “run” and ‘fort’` keeps one span; a candidate
    closed exactly at the later closer where a sentence terminator precedes it
    supports both readings, so the sibling reading is taken and the chunk is
    uncertain;
  - the possessive lookahead skips marks that fail the closer predicate, so
    `dogs’ “it’s loud”` and the straight versions keep one span;
  - in a targeted chunk the gate rejects main's inside-out row whose SPOKEN
    text is only the nested pair while NARRATOR reproduces the span's words
    (32 chunks × 2 gate paths);
  - the uncertainty net ignores a stray straight `'` after sentence
    punctuation and before `-`/`"`, so the three PDNC fixture strings return to
    main's behaviour;
  - the innermost opener of a failed range is recorded as a failed range too,
    so a pair past the outer 500-character bound still abstains;
  - a targeted chunk can also be uncertain, and then the gate is the adopted
    span's, not main's.

The rev5 review closed the possessive INSIDE the nested pair:
  - `“the dogs’ bones”` straddles the `…s’` mark, so the pair was wholly in
    neither half; the lookahead now sees it across the mark and keeps the span,
    so the truthful row passes both gate paths and main's inside-out row does
    not;
  - the no-terminator sibling still drops both spans and stays `quote_forced`;
    Limits now describes that measured behaviour instead of claiming the span
    continues;
  - the `own == -1` sibling branch is pinned by a mixed-convention chunk.
"""
import hashlib
import io
import json
import os
import unittest
from unittest import mock

from pass_quality import (analyze_outer_quote_regions, validate_segment_quality)
try:  # Importable on the pre-fix tree, so the differential digest can be checked there.
    from pass_quality import get_single_quoted_speech_spans
except ImportError:  # pragma: no cover - only on origin/main before #2331
    get_single_quoted_speech_spans = None
import pass_quality as pq
from dialogue_spans import PAIRED
import three_pass_generate as tp


FIRST = "\u2018She told me \u201crun\u201d and I ran,\u2019 said Anna. Ben left."
FIRST_CORRECT = [
    {"type": "SPOKEN", "text": "She told me \u201crun\u201d and I ran,"},
    {"type": "NARRATOR", "text": "said Anna. Ben left."},
]
FIRST_INVERTED = [
    {"type": "NARRATOR", "text": "\u2018She told me "},
    {"type": "SPOKEN", "text": "run"},
    {"type": "NARRATOR", "text": " and I ran,\u2019 said Anna. Ben left."},
]

SECOND = ("\u2018She told me \u201crun\u201d and I ran, and then I hid by the river "
          "until the morning came,\u2019 said Anna.\n\n"
          "\u2018Were you afraid?\u2019 asked Ben.\n\n"
          "\u2018A little,\u2019 said Anna. She looked at the door.")
# Only one span nests a double quote, so only it is adopted; the later plain
# single-quoted speeches stay in the analyzer's narration, but the gate accepts
# the model's correct split of them (rev2 finding 1).
SECOND_NARROW = [
    {"type": "SPOKEN",
     "text": "She told me run and I ran, and then I hid by the river until the morning came,"},
    {"type": "NARRATOR",
     "text": ("said Anna.\n\n"
              "\u2018Were you afraid?\u2019 asked Ben.\n\n"
              "\u2018A little,\u2019 said Anna. She looked at the door.")},
]
SECOND_CORRECT = [
    {"type": "SPOKEN",
     "text": ("She told me \u201crun\u201d and I ran, and then I hid by the river "
              "until the morning came,")},
    {"type": "NARRATOR", "text": "said Anna."},
    {"type": "SPOKEN", "text": "Were you afraid?"},
    {"type": "NARRATOR", "text": "asked Ben."},
    {"type": "SPOKEN", "text": "A little,"},
    {"type": "NARRATOR", "text": "said Anna. She looked at the door."},
]

# rev3 finding 1: an s-final closer before a targeted sibling speech. The
# lookahead used to see the nested pair before the sibling's closer and treat
# `Yes’` as a possessive, then lose both spans, so main's inside-out split was
# certified.
SIBLING_AFTER_S = "\u2018Yes\u2019 she said. \u2018He said \u201crun\u201d,\u2019 he said."
SIBLING_AFTER_S_STRAIGHT = "'Yes' she said. 'He said \"run\",' he said."
SIBLING_AFTER_S_TRUTHFUL = [
    {"type": "SPOKEN", "text": "Yes"},
    {"type": "NARRATOR", "text": "she said."},
    {"type": "SPOKEN", "text": "He said \u201crun\u201d,"},
    {"type": "NARRATOR", "text": "he said."},
]
SIBLING_AFTER_S_STRAIGHT_TRUTHFUL = [
    {"type": "SPOKEN", "text": "Yes"},
    {"type": "NARRATOR", "text": "she said."},
    {"type": "SPOKEN", "text": "He said \"run\","},
    {"type": "NARRATOR", "text": "he said."},
]
SIBLING_AFTER_S_REGIONS = [
    {"type": "NARRATOR", "text": "\u2018Yes\u2019 she said."},
    {"type": "SPOKEN", "text": "He said run,"},
    {"type": "NARRATOR", "text": "he said."},
]
SIBLING_AFTER_S_INVERTED = [
    {"type": "NARRATOR", "text": "\u2018Yes\u2019 she said. \u2018He said"},
    {"type": "SPOKEN", "text": "run"},
    {"type": "NARRATOR", "text": ",\u2019 he said."},
]
SIBLING_AFTER_S_STRAIGHT_INVERTED = [
    {"type": "NARRATOR", "text": "'Yes' she said. 'He said \""},
    {"type": "SPOKEN", "text": "run"},
    {"type": "NARRATOR", "text": "\",' he said."},
]

# rev3 finding 1(b): a dropped closer before a targeted sibling leaves an
# unresolvable range that nests a paired quote. Nothing is adopted, so the gate
# stays main's, but the chunk must no longer be certified as presegmented.
DROPPED_CLOSER = "\u2018Hello, she said. \u2018He said \u201crun\u201d,\u2019 he said."
DROPPED_CLOSER_REGIONS = [
    {"type": "NARRATOR", "text": "\u2018Hello, she said. \u2018He said"},
    {"type": "SPOKEN", "text": "run"},
    {"type": "NARRATOR", "text": ",\u2019 he said."},
]

# rev3 finding 3: a narration quotation whose only double quotes are nested
# inside it is treated as speech by the decided heuristic. Both the head split
# and main's split are wrong for narration; the truthful whole-NARRATOR row is
# still rejected. Kept, pinned, and listed in Limits.
NARRATION_ONLY_NESTED = "He read \u2018the sign said \u201cclosed\u201d\u2019 twice."
NARRATION_ONLY_NESTED_REGIONS = [
    {"type": "NARRATOR", "text": "He read"},
    {"type": "SPOKEN", "text": "the sign said closed"},
    {"type": "NARRATOR", "text": "twice."},
]
# What origin/main produces for the same chunk (documented in Limits, not
# asserted: the split is wrong too).
MAIN_NARRATION_ONLY_NESTED_REGIONS = [
    {"type": "NARRATOR", "text": "He read \u2018the sign said"},
    {"type": "SPOKEN", "text": "closed"},
    {"type": "NARRATOR", "text": "\u2019 twice."},
]

# rev3 finding 2: guards whose earlier cases were masked by a second outside
# double pair. Each case is a decision difference from its single mutant.
APOSTROPHE_OPENER_GUARD = "He said Anna's \u201cbook\u201d was here, and James' too."
TRAILING_SPACE_OPENER_GUARD = "He said \u2018 \u201crun\u201d and left,\u2019 she said."
INCH_OPENER_PROBE = '6" x"'

STRAIGHT = "'She told me \"run\" and I ran,' said Anna. Ben left."
STRAIGHT_CORRECT = [
    {"type": "SPOKEN", "text": "She told me \"run\" and I ran,"},
    {"type": "NARRATOR", "text": "said Anna. Ben left."},
]
STRAIGHT_INVERTED = [
    {"type": "NARRATOR", "text": "'She told me "},
    {"type": "SPOKEN", "text": "run"},
    {"type": "NARRATOR", "text": " and I ran,' said Anna. Ben left."},
]

CONTROL = "\u2018She told me and I ran,\u2019 said Anna. Ben left."
CONTROL_CORRECT = [
    {"type": "SPOKEN", "text": "She told me and I ran,"},
    {"type": "NARRATOR", "text": "said Anna. Ben left."},
]

DOUBLE_BOOK = "\u201cShe said \u2018run\u2019 and left,\u201d said Anna. Ben left."

# rev1 finding 1: the single span sits inside double-quoted speech, so the
# decision never adopts it. The gate must not whitelist its marks either.
A_CHUNK = "\u201cHe asked \u2018did she say \u201cyes\u201d?\u2019 loudly,\u201d she said."
A_REGIONS = [
    {"type": "SPOKEN", "text": "He asked \u2018did she say yes?\u2019 loudly,"},
    {"type": "NARRATOR", "text": "she said."},
]
A_TRUTHFUL = [
    {"type": "SPOKEN",
     "text": "He asked \u2018did she say \u201cyes\u201d?\u2019 loudly,"},
    {"type": "NARRATOR", "text": "she said."},
]
A_KEEPS_OUTER = [
    {"type": "SPOKEN",
     "text": "\u201cHe asked \u2018did she say \u201cyes\u201d?\u2019 loudly,\u201d"},
    {"type": "NARRATOR", "text": "she said."},
]

# rev1 finding 2: a quoted title in narration is not speech.
TITLE = "\u2018He said \u201crun\u201d and left,\u2019 she said. Later \u2018Hamlet\u2019 was banned."
TITLE_REGIONS = [
    {"type": "SPOKEN", "text": "He said run and left,"},
    {"type": "NARRATOR", "text": "she said. Later \u2018Hamlet\u2019 was banned."},
]
TITLE_TRUTHFUL = [
    {"type": "SPOKEN", "text": "He said \u201crun\u201d and left,"},
    {"type": "NARRATOR", "text": "she said. Later \u2018Hamlet\u2019 was banned."},
]

# rev1 finding 3: a possessive or elision before the real closer.
POSSESSIVE_CURLY = "\u2018The dogs\u2019 \u201cbones\u201d are here,\u2019 she said."
POSSESSIVE_STRAIGHT = "'The dogs' \"bones\" are here,' she said."
ELISION = "\u2018We played rock \u2019n\u2019 roll and \u201crock\u201d music,\u2019 he said."
CLOSER_CASES = {
    "possessive curly": (POSSESSIVE_CURLY,
                         "The dogs\u2019 \u201cbones\u201d are here,",
                         "The dogs\u2019 bones are here,",
                         "she said."),
    "possessive straight": (POSSESSIVE_STRAIGHT,
                            "The dogs' \"bones\" are here,",
                            "The dogs' bones are here,",
                            "she said."),
    "elision": (ELISION,
                "We played rock \u2019n\u2019 roll and \u201crock\u201d music,",
                "We played rock \u2019n\u2019 roll and rock music,",
                "he said."),
}

# rev1 finding 4: the single span is a quotation in a double-quoted book's
# narration. The chunk already has depth-0 double speech, so nothing changes.
NARRATION_IN_DOUBLE_BOOK = ("He read \u2018the sign said \u201cclosed\u201d\u2019 twice. "
                            "\u201cLet me in,\u201d said Anna.")
NARRATION_IN_DOUBLE_BOOK_REGIONS = [
    {"type": "NARRATOR", "text": "He read \u2018the sign said"},
    {"type": "SPOKEN", "text": "closed"},
    {"type": "NARRATOR", "text": "\u2019 twice."},
    {"type": "SPOKEN", "text": "Let me in,"},
    {"type": "NARRATOR", "text": "said Anna."},
]

# rev1 finding 5: an inch mark inside an adopted span must stay content and
# must not swallow the attribution.
INCH = "\u2018He is 6\" wide and said \u201chi\u201d,\u2019 she said."
INCH_REGIONS = [
    {"type": "SPOKEN", "text": "He is 6\" wide and said hi,"},
    {"type": "NARRATOR", "text": "she said."},
]
INCH_TRUTHFUL = [
    {"type": "SPOKEN", "text": "He is 6\" wide and said \u201chi\u201d,"},
    {"type": "NARRATOR", "text": "she said."},
]

# rev2 finding 2: a possessive AFTER the nested quote is not the closer.
POSSESSIVE_AFTER = "\u2018He said \u201crun\u201d, and the dogs\u2019 bones are here,\u2019 she said."
POSSESSIVE_AFTER_STRAIGHT = "'He said \"run\", and the dogs' bones are here,' she said."
POSSESSIVE_AFTER_CASES = {
    "curly": (POSSESSIVE_AFTER,
              "He said \u201crun\u201d, and the dogs\u2019 bones are here,",
              "He said run, and the dogs\u2019 bones are here,",
              "He said run, and the dogs",
              "bones are here,\u2019 she said."),
    "straight": (POSSESSIVE_AFTER_STRAIGHT,
                 "He said \"run\", and the dogs' bones are here,",
                 "He said run, and the dogs' bones are here,",
                 "He said run, and the dogs",
                 "bones are here,' she said."),
}

# rev2 finding 3: a single-quoted quotation nested inside the speech.
NESTED_SINGLE_A = "\u2018He said \u201crun\u201d to \u2018Bob\u2019 then left,\u2019 she said."
NESTED_SINGLE_A_TRUTHFUL = [
    {"type": "SPOKEN",
     "text": "He said \u201crun\u201d to \u2018Bob\u2019 then left,"},
    {"type": "NARRATOR", "text": "she said."},
]
NESTED_SINGLE_B = "\u2018The word \u2018run\u2019 means \u201cmove fast\u201d,\u2019 she said."
NESTED_SINGLE_B_TRUTHFUL = [
    {"type": "SPOKEN",
     "text": "The word \u2018run\u2019 means \u201cmove fast\u201d,"},
    {"type": "NARRATOR", "text": "she said."},
]
# One inner opener, so the depth cannot be resolved; no adoption (abstain).
NESTED_SINGLE_AMBIGUOUS = "\u2018He said \u201crun\u201d to \u2018Bob then left,\u2019 she said."
AMBIGUOUS_CASES = {
    "inner speech": (
        NESTED_SINGLE_AMBIGUOUS,
        [{"type": "NARRATOR", "text": "\u2018He said"},
         {"type": "SPOKEN", "text": "run"},
         {"type": "NARRATOR", "text": "to \u2018Bob then left,\u2019 she said."}]),
    # An inner span that DOES nest a double quote must not be promoted to
    # top-level speech just because the outer opener never closed.
    "inner nesting a double quote": (
        "\u2018He said \u2018did she say \u201cyes\u201d\u2019 then left, she said.",
        [{"type": "NARRATOR", "text": "\u2018He said \u2018did she say"},
         {"type": "SPOKEN", "text": "yes"},
         {"type": "NARRATOR", "text": "\u2019 then left, she said."}]),
}

# rev2 finding 4: an inch mark is a measurement, not a nested quote.
INCH_OUTSIDE = ("\u2018She said \u201crun\u201d,\u2019 Anna said. "
                "The sign is 6\" wide and the post 8\" tall.")
INCH_OUTSIDE_REGIONS = [
    {"type": "SPOKEN", "text": "She said run,"},
    {"type": "NARRATOR",
     "text": "Anna said. The sign is 6\" wide and the post 8\" tall."},
]
INCH_ONLY = "He noted \u20186\" by 8\"\u2019 on the card."
INCH_ONLY_REGIONS = [{"type": "NARRATOR", "text": INCH_ONLY}]

# rev2 finding 6: a straight elision with no second outside double pair.
# Without the guard `'tis` opens a span that closes at a later apostrophe-shaped
# mark either way; with it, both keep main's split.
STRAIGHT_ELISION_ALONE_CASES = {
    "word before closer": (
        "He said 'tis \u201clate\u201d now, and James' book came.",
        [{"type": "NARRATOR", "text": "He said 'tis"},
         {"type": "SPOKEN", "text": "late"},
         {"type": "NARRATOR", "text": "now, and James' book came."}]),
    "review sentence": (
        "He said 'tis \u201clate\u201d now, and 'Hello,' she said.",
        [{"type": "NARRATOR", "text": "He said 'tis"},
         {"type": "SPOKEN", "text": "late"},
         {"type": "NARRATOR", "text": "now, and 'Hello,' she said."}]),
}

# rev2 finding 7: `'Tis`/`'Twas` are ambiguous with the elision apostrophe, so
# main's behaviour is kept (Limits).
STRAIGHT_TIS_REGIONS = [
    {"type": "NARRATOR", "text": "'Tis"},
    {"type": "SPOKEN", "text": "fine"},
    {"type": "NARRATOR", "text": ",' she said."},
]

# rev1 finding 6: paragraph-bounded single quotes are a decided limitation.
BRITISH = ("\u2018She told me \u201crun\u201d and I ran,\n\n"
           "and I kept running,\u2019 said Anna. Ben left.")
BRITISH_REGIONS = [
    {"type": "NARRATOR", "text": "\u2018She told me"},
    {"type": "SPOKEN", "text": "run"},
    {"type": "NARRATOR",
     "text": "and I ran,\n\nand I kept running,\u2019 said Anna. Ben left."},
]
SPLIT_HEAD = "\u2018She told me \u201crun\u201d and I ran,"
SPLIT_HEAD_REGIONS = [
    {"type": "NARRATOR", "text": "\u2018She told me"},
    {"type": "SPOKEN", "text": "run"},
    {"type": "NARRATOR", "text": "and I ran,"},
]
SPLIT_TAIL = " and I kept running,\u2019 said Anna."
SPLIT_TAIL_REGIONS = []

# rev1 finding 7: conventions derived from PAIRED.
GUILLEMETS = "\u2018He said \u00abbonjour\u00bb twice,\u2019 she said."
CORNER_BRACKETS = "\u2018He said \u300c\u3053\u3093\u306b\u3061\u306f\u300d twice,\u2019 she said."

# rev4 finding 1: a nested single-quoted quotation is not a sibling speech.
# The sibling-opener bound used to cut the span at `dogs’`, so head certified
# the inverted split. Curly quotes only: straight quotes are accidentally
# immune because the opener and closer are the same character.
POSSESSIVE_NESTED = "\u2018The dogs\u2019 \u201crun\u201d and \u2018fort\u2019 are here,\u2019 she said."
POSSESSIVE_NESTED_TRUTHFUL = [
    {"type": "SPOKEN", "text": "The dogs\u2019 \u201crun\u201d and \u2018fort\u2019 are here,"},
    {"type": "NARRATOR", "text": "she said."},
]
POSSESSIVE_NESTED_INVERTED = [
    {"type": "NARRATOR", "text": "\u2018The dogs\u2019"},
    {"type": "SPOKEN", "text": "run"},
    {"type": "NARRATOR", "text": "and \u2018fort\u2019 are here,\u2019 she said."},
]
POSSESSIVE_AFTER_NESTED = ("\u2018He said \u201crun\u201d, and the dogs\u2019 \u2018fort\u2019 "
                           "is here,\u2019 she said.")
POSSESSIVE_AFTER_NESTED_TRUTHFUL = [
    {"type": "SPOKEN",
     "text": "He said \u201crun\u201d, and the dogs\u2019 \u2018fort\u2019 is here,"},
    {"type": "NARRATOR", "text": "she said."},
]
POSSESSIVE_AFTER_NESTED_INVERTED = [
    {"type": "SPOKEN", "text": "He said run, and the dogs"},
    {"type": "NARRATOR", "text": "\u2018fort\u2019 is here,\u2019 she said."},
]

# rev4 finding 2: a possessive before a nested quote that contains an
# apostrophe. `later` used to stop at the apostrophe in `it’s`, so the span
# closed at `dogs’` and the inverted split was certified.
def _nested_apostrophe_case(quote_open, quote_close, phrase):
    """`…s’` before a DOUBLE-quoted phrase that itself holds an apostrophe."""
    chunk = ("%sThe dogs%s \"%s\" are here,%s she said."
             % (quote_open, quote_close, phrase, quote_close))
    truthful = ("The dogs%s \"%s\" are here," % (quote_close, phrase))
    analysed = "The dogs%s %s are here," % (quote_close, phrase)
    inverted = [
        {"type": "NARRATOR", "text": "%sThe dogs%s" % (quote_open, quote_close)},
        {"type": "SPOKEN", "text": phrase},
        {"type": "NARRATOR",
         "text": "are here,%s she said." % quote_close},
    ]
    return chunk, truthful, analysed, inverted


NESTED_APOSTROPHE_CASES = {}
for _name, _phrase in (("it's", "it\u2019s loud"),
                       ("don't", "don\u2019t run"),
                       ("I'm", "I\u2019m fine")):
    NESTED_APOSTROPHE_CASES[_name] = _nested_apostrophe_case(
        "\u2018", "\u2019", _phrase)
for _name, _phrase in (("straight it's", "it's loud"),
                       ("straight don't", "don't run"),
                       ("straight I'm", "I'm fine")):
    NESTED_APOSTROPHE_CASES[_name] = _nested_apostrophe_case("'", "'", _phrase)

# rev4 finding 3: the speech's opening words recur in the attribution, so the
# gate's role check found them in the narrator text and accepted main's
# inside-out row. A targeted chunk now treats the adopted span's own text as
# the speech, so the nested pair cannot be the only spoken row.
REPEATED_ATTRIBUTION_PRONOUNS = ("he", "she")
REPEATED_ATTRIBUTION_VERBS = ("said", "asked", "replied", "whispered")
REPEATED_ATTRIBUTION_OBJECTS = ("run", "go", "wait", "hurry")

# rev4 finding 6: resuming at the failed-range end hid a nested failed range.
# Outer opener fails at 501; the inner opener at 499 and its range contain the
# pair `“y”`, so the chunk must be uncertain.
NESTED_FAILED_RANGE = ("\u2018" + "a " * 249
                       + "\u2018x \u201cy\u201d more words and then it ended here ok.")

# rev4 finding 10: one adopted span plus one unresolved range that nests a
# pair. The analysis is targeted (not main's) and uncertain at the same time.
NESTED_AND_UNCERTAIN = ("\u2018He said \u201crun\u201d,\u2019 she said. "
                        "\u2018Then she said \u201cgo\u201d")
NESTED_AND_UNCERTAIN_INVERTED = [
    {"type": "NARRATOR", "text": "\u2018He said"},
    {"type": "SPOKEN", "text": "run"},
    {"type": "NARRATOR", "text": ",\u2019 she said. \u2018Then she said \u201cgo\u201d"},
]

# rev5 finding 1: a plural possessive INSIDE the nested pair straddles the
# `…s’` mark, so neither `opener+1..mark` nor `mark+1..later` contained the
# pair. The span closed at the possessive, `auto`/`quotes` certified main's
# inside-out split, and the truthful row failed both gate paths.
POSSESSIVE_INSIDE_WORDS = (("dogs", "bones"), ("boys", "toys"),
                           ("girls", "books"), ("Joneses", "house"),
                           ("soldiers", "rifles"), ("players", "names"),
                           ("guards", "orders"))


def _inside_out_row(chunk, open_mark, close_mark):
    """-> main's inverted split: SPOKEN is only the nested pair."""
    start = chunk.index(open_mark)
    end = chunk.index(close_mark, start + 1)
    return [
        {"type": "NARRATOR", "text": chunk[:start]},
        {"type": "SPOKEN", "text": chunk[start + 1:end]},
        {"type": "NARRATOR", "text": chunk[end + 1:]},
    ]


def _possessive_inside_case(word, noun, outer=("\u2018", "\u2019"),
                            nested=("\u201c", "\u201d"), possessive="\u2019"):
    """-> (chunk, truthful row, main's inside-out row) for `… “the dogs’ …”`."""
    chunk = ("%sHe read %sthe %s%s %s%s aloud,%s she said."
             % (outer[0], nested[0], word, possessive, noun, nested[1],
                outer[1]))
    truthful = [{
        "type": "SPOKEN",
        "text": "He read %sthe %s%s %s%s aloud,"
                % (nested[0], word, possessive, noun, nested[1])},
        {"type": "NARRATOR", "text": "she said."}]
    return chunk, truthful, _inside_out_row(chunk, nested[0], nested[1])


POSSESSIVE_INSIDE_CASES = {}
for _word, _noun in POSSESSIVE_INSIDE_WORDS:
    POSSESSIVE_INSIDE_CASES[_word] = _possessive_inside_case(_word, _noun)
# The straight convention: the possessive and the outer marks are both `'`.
POSSESSIVE_INSIDE_CASES["straight"] = _possessive_inside_case(
    "dogs", "bones", ("'", "'"), ('"', '"'), "'")
POSSESSIVE_INSIDE_CASES["guillemets"] = _possessive_inside_case(
    "dogs", "bones", nested=("\u00ab", "\u00bb"))
POSSESSIVE_INSIDE_CASES["corner brackets"] = _possessive_inside_case(
    "dogs", "bones", nested=("\u300c", "\u300d"))

# rev5 finding 1, with a sibling single quotation beside the nested pair.
SIBLING_POSSESSIVE_INSIDE = ("\u2018He said \u201cthe dogs\u2019 bones\u201d to "
                             "\u2018Bob\u2019,\u2019 she said.")
SIBLING_POSSESSIVE_INSIDE_TRUTHFUL = [
    {"type": "SPOKEN",
     "text": "He said \u201cthe dogs\u2019 bones\u201d to \u2018Bob\u2019,"},
    {"type": "NARRATOR", "text": "she said."},
]

# rev5 finding 2: a sibling speech opened after `…s’` with no sentence
# punctuation between. The lookahead takes the nested reading, so the outer
# span never closes and BOTH spans are dropped. Limits must describe that.
NO_TERMINATOR_SIBLING = "\u2018Yes\u2019 she said \u2018He said \u201crun\u201d,\u2019 he said."
NO_TERMINATOR_SIBLING_REGIONS = [
    {"type": "NARRATOR", "text": "\u2018Yes\u2019 she said \u2018He said"},
    {"type": "SPOKEN", "text": "run"},
    {"type": "NARRATOR", "text": ",\u2019 he said."},
]

# rev5 nit 3: a mixed-convention sibling whose straight opener has no closer of
# its own. The `own == -1` branch makes it a sibling, so `Yes’` closes and the
# `Yes` span exists. Removing the branch drops that span (the decisions stay
# the same), so the span list is what pins the branch.
MIXED_CONVENTION_SIBLING = "\u2018Yes\u2019 she said. 'He said \u201crun\u201d,\u2019 he said."
MIXED_CONVENTION_SIBLING_REGIONS = [
    {"type": "NARRATOR", "text": "\u2018Yes\u2019 she said. 'He said"},
    {"type": "SPOKEN", "text": "run"},
    {"type": "NARRATOR", "text": ",\u2019 he said."},
]

# rev4 finding 4: the three fixture strings where a stray straight apostrophe
# after sentence punctuation and before `-`/`"` opened a failed range. Main's
# split is correct, so the uncertainty net must not fire.
STRAY_APOSTROPHE_FIXTURES = (
    ("attribution_gold_pdnc_theawakening.json", 404, "prev_context"),
    ("attribution_gold_pdnc_theawakening.json", 405, "prev_context"),
    ("attribution_gold_pdnc_emma.json", 1485, "prev_context"),
)
STRAY_APOSTROPHE_WORDS = ("applicants.", "composed?", "guests.", "visitors?",
                          "readers.", "friends?")
STRAY_APOSTROPHE_TAILS = ("--", "\"")
STRAY_APOSTROPHE_PROSE = ("Bad enough as it is.",
                          "Quite new, in fact, as it happened.")


def _stray_apostrophe_chunks():
    # 6 words x 2 tails x 2 sentences = the review's 24 variants.
    chunks = []
    for prose in STRAY_APOSTROPHE_PROSE:
        for word in STRAY_APOSTROPHE_WORDS:
            for tail in STRAY_APOSTROPHE_TAILS:
                chunks.append(
                    "%s'%s%s \"run,\" said Anna. Ben left."
                    % (word, tail, prose))
    return chunks


# Quote shapes the repository already exercises: #624 run-on quotes, #2314 and
# #2318 inch marks, German low-high, Polish, guillemets, Japanese, full-width,
# straight, multi-paragraph speech, and nested single quotes in a double-quoted
# book. Every one is a double-quoted chunk, so #2331 must not move any of them.
DOUBLE_QUOTED_FIXTURES = (
    "Alice stood. \"Hello.\" Alice waited. \"Goodbye.\" Alice left.",
    "\u201cHello,\u201d she said. \u201cGoodbye.\u201d",
    "\u201cI can't go,\u201d she said. \u201cI won't.\u201d",
    "\u201cFirst,\u201d she said.\n\n\u201cSecond,\u201d he said.",
    "\u201cShe said \u2018run\u2019 and left,\u201d said Anna. Ben left.",
    "\u201cShe said \u2018it is Anna\u2019s book\u2019 and left,\u201d said Ben.",
    "\u201eGuten Tag,\u201c sagte er. \u201eWie geht es?\u201c",
    "\u201eDzie\u0144 dobry,\u201d powiedzia\u0142.",
    "\u00abBonjour,\u00bb dit-elle. \u00ab\u00c7a va?\u00bb",
    "\u300c\u304a\u306f\u3088\u3046\u300d\u3068\u5f7c\u306f\u8a00\u3063\u305f\u3002\u300c\u5143\u6c17\uff1f\u300d",
    "\uff02Hello\uff02 she said. \uff02Goodbye.\uff02",
    "He is 6\" wide and said \u201cHello.\u201d",
    "He is 6\u201d wide and said \u201cHello.\u201d",
    "\u201cWait!\u201dHaha!\u201dHahaha!\u201d she cried.",
    "\u201cHello.\u201dHaha!\u201d she cried.",
    "She murmured I see\u2026\u201d, keeping her eyes lowered. \u201cThen she left.\u201d",
    "\u201c\u2018Nested single quotes\u2019 are decoration here,\u201d he said.",
)


def _decision_and_gate(chunk):
    """The user-visible results #2331 must not move for double-quoted chunks."""
    analysis = analyze_outer_quote_regions(chunk)
    decisions = []
    for mode in ("auto", "quotes", "lexical"):
        entries, resolution = tp.quote_regions_decision(mode, chunk, analysis)
        decisions.append({"mode": mode, "entries": entries, "resolution": resolution})
    report = validate_segment_quality(chunk, analysis["regions"],
                                      quote_analysis=analysis)
    decisions.append({"gate": {"passed": report["passed"],
                               "findings": report["findings"]}})
    # The gate recomputes the analysis when no quote_analysis is passed; that
    # path must agree with the analysis-backed one (#2331 review).
    analysis_free = validate_segment_quality(chunk, analysis["regions"])
    decisions.append({"gate_without_analysis": {
        "passed": analysis_free["passed"],
        "findings": analysis_free["findings"],
        "quote_gate": analysis_free["quote_gate"]}})
    return decisions


def _differential_corpus():
    """Chunks outside #2331's targeted shape; every result must match main.

    The targeted shape (a single-quoted span that nests a paired quote in a
    chunk with no other paired speech) is covered by the behaviour tests above,
    not here.
    """
    corpus = list(DOUBLE_QUOTED_FIXTURES)
    names = ("Anna", "Ben", "Cara", "Dan", "Eve", "Finn", "Gus", "Hana")
    verbs = ("said", "asked", "replied", "whispered")
    lines = ("Hello.", "Where did you go?", "I ran to the river.", "Don't wait.")
    replies = ("I stayed.", "A little.", "Not yet.", "Come back.")
    titles = ("Hamlet", "the sign", "the rule")
    possessives = ("bones", "books", "words")
    guillemet_pairs = ("\u00abbonjour\u00bb", "\u300c\u3053\u3093\u306b\u3061\u306f\u300d",
                       "\u300e\u304a\u306f\u3088\u3046\u300f")
    templates = (
        # double-quoted speech with nested single quotes (single-in-double)
        "\u201c{first} \u2018{title}\u2019 {reply}\u201d {verb} {name}.",
        "\u201c{first}\u201d {verb} {name}. The book \u2018{title}\u2019 was banned.",
        # possessives and inch marks inside double-quoted speech
        "\u201cThe dogs\u2019 {noun} are here,\u201d {verb} {name}.",
        "\u201cIt is {number}\" wide,\u201d {verb} {name}.",
        # guillemets and corner brackets inside double-quoted speech
        "\u201cHe said {pair} twice,\u201d {verb} {name}.",
        # run-on quotes and multi-paragraph double speech
        "\u201c{first}\u201d {verb} {name}.\n\n\u201c{reply}\u201d {verb} {other}.",
        "\u201cWait!\u201dHaha!\u201d {verb} {name}.",
        # single-quoted speech with no nested pair (unchanged, not targeted)
        "\u2018{first}\u2019 {verb} {name}. {other} left.",
        "\u2018The dogs\u2019 {noun} are here,\u2019 {verb} {name}.",
        "\u2018It is {number}\" wide,\u2019 {verb} {name}.",
        "\u2018Anna\u2019s {noun} are here,\u2019 {verb} {name}.",
        # a quotation in the narration of a double-quoted book (finding 4)
        "He read \u2018the sign said \u201cclosed\u201d\u2019 twice. \u201c{first}\u201d {verb} {name}.",
        # single span inside double speech (finding 1)
        "\u201cHe asked \u2018did she say \u201cyes\u201d?\u2019 loudly,\u201d {verb} {name}.",
    )
    index = 0
    while len(corpus) < 260:
        first = lines[index % len(lines)]
        reply = replies[(index // len(lines)) % len(replies)]
        verb = verbs[index % len(verbs)]
        name = names[(index // 2) % len(names)]
        other = names[(index // 3 + 1) % len(names)]
        template = templates[index % len(templates)]
        corpus.append(template.format(
            first=first, reply=reply, verb=verb, name=name, other=other,
            title=titles[index % len(titles)],
            noun=possessives[index % len(possessives)],
            number=index % 13 + 2,
            pair=guillemet_pairs[index % len(guillemet_pairs)]))
        index += 1
    # British multi-paragraph single quotes and a head split at the paragraph
    # bound are NOT here: their unresolvable range nests a paired quote, so the
    # rev3 safety net deliberately abstains on them (pinned in
    # test_british_multiparagraph_and_split_chunks_are_pinned_limits). The two
    # shapes below stay byte-identical to main.
    corpus.extend([SPLIT_TAIL, NARRATION_IN_DOUBLE_BOOK])
    return corpus


def _differential_digest():
    payload = [_decision_and_gate(chunk) for chunk in _differential_corpus()]
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


class SingleQuotedSpeechTests(unittest.TestCase):
    def assertions_for_issue_chunk(self, chunk, correct, inverted,
                                   expected_regions=None):
        analysis = analyze_outer_quote_regions(chunk)
        # Behaviour first: on origin/main these decisions are the inverted
        # presegmentation, so the test fails on the behaviour rather than on a
        # missing analysis key (#2331 review 3).
        for mode in ("auto", "lexical"):
            with self.subTest(mode=mode):
                entries, resolution = tp.quote_regions_decision(mode, chunk, analysis)
                self.assertIsNone(entries)
                self.assertIsNone(resolution)
        report = validate_segment_quality(chunk, correct, quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])
        self.assertNotIn("mixed_quote_region",
                         {finding["code"] for finding in report["findings"]})
        bad = validate_segment_quality(chunk, inverted, quote_analysis=analysis)
        self.assertFalse(bad["passed"])
        self.assertIn("quote_region_misclassified",
                      {finding["code"] for finding in bad["findings"]})
        quote_entries, quote_resolution = tp.quote_regions_decision(
            "quotes", chunk, analysis)
        self.assertEqual("quote_presegmented", quote_resolution)
        # "quotes" has no model to ask: it keeps the correct single-quote
        # split, never the inverted one.
        self.assertNotEqual([entry["type"] for entry in inverted],
                            [entry["type"] for entry in quote_entries])
        self.assertEqual(analysis["regions"], quote_entries)
        self.assertEqual("SPOKEN", quote_entries[0]["type"])
        self.assertEqual("NARRATOR", quote_entries[-1]["type"])
        self.assertTrue(analysis.get("nested_single_quoted_speech"))
        self.assertEqual(len(analysis.get("adopted_single_quote_spans") or ()), 1)
        if expected_regions is not None:
            self.assertEqual(expected_regions, analysis["regions"])

    def test_first_issue_example_is_not_presegmented_inside_out(self):
        self.assertions_for_issue_chunk(
            FIRST, FIRST_CORRECT, FIRST_INVERTED,
            [{"type": "SPOKEN", "text": "She told me run and I ran,"},
             {"type": "NARRATOR", "text": "said Anna. Ben left."}])

    def test_second_issue_example_is_not_presegmented_inside_out(self):
        # Rev2 finding 1: only the span that nests a double quote is adopted,
        # so the analyzer's own split is narrow. The issue's correct three-
        # speech split must pass the gate, and "quotes" must not certify the
        # narrow split as quote_presegmented.
        analysis = analyze_outer_quote_regions(SECOND)
        # Behaviour before the new analysis keys (#2331 review 3).
        for mode in ("auto", "lexical"):
            self.assertEqual((None, None),
                             tp.quote_regions_decision(mode, SECOND, analysis))
        quote_entries, quote_resolution = tp.quote_regions_decision(
            "quotes", SECOND, analysis)
        self.assertEqual("quote_forced", quote_resolution)
        # The correct split passes both gate paths. Before the rev2 fix the
        # two sibling speeches failed quote_region_misclassified.
        for label, kwargs in (
                ("analysis-backed", {"quote_analysis": analysis}),
                ("analysis-free", {})):
            with self.subTest(gate=label):
                report = validate_segment_quality(SECOND, SECOND_CORRECT, **kwargs)
                self.assertTrue(report["passed"], report["findings"])
        # The analyzer's narrow split is incomplete, not wrong, and still
        # passes; "quotes" merely refuses to vouch for it now.
        report = validate_segment_quality(SECOND, SECOND_NARROW,
                                          quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])
        self.assertTrue(analysis.get("nested_single_quoted_speech"))
        self.assertEqual(SECOND_NARROW, analysis["regions"])
        self.assertEqual(
            ["Were you afraid?", "A little,"],
            [SECOND[start:end]
             for _opener, start, end, _closer in
             analysis.get("unadopted_single_quote_spans") or ()])
        self.assertEqual(analysis["regions"], quote_entries)

    def test_a_sibling_speech_after_an_s_final_closer(self):
        # Rev3 finding 1(a): `Yes’` before `‘He said “run”,’` is the closer, not
        # a possessive: the possessive lookahead must stop at the sibling
        # opener. Before the fix both spans were lost and the inverted split
        # was certified. Behaviour is asserted before the new keys (#2331
        # review 3).
        for label, chunk, truthful, inverted in (
                ("curly", SIBLING_AFTER_S, SIBLING_AFTER_S_TRUTHFUL,
                 SIBLING_AFTER_S_INVERTED),
                ("straight", SIBLING_AFTER_S_STRAIGHT,
                 SIBLING_AFTER_S_STRAIGHT_TRUTHFUL,
                 SIBLING_AFTER_S_STRAIGHT_INVERTED)):
            with self.subTest(label=label):
                analysis = analyze_outer_quote_regions(chunk)
                for mode in ("auto", "lexical"):
                    self.assertEqual(
                        (None, None),
                        tp.quote_regions_decision(mode, chunk, analysis))
                self.assertFalse(
                    validate_segment_quality(chunk, inverted,
                                             quote_analysis=analysis)["passed"])
                for gate_label, kwargs in (
                        ("analysis-backed", {"quote_analysis": analysis}),
                        ("analysis-free", {})):
                    with self.subTest(gate=gate_label):
                        report = validate_segment_quality(chunk, truthful, **kwargs)
                        self.assertTrue(report["passed"], report["findings"])
                entries, resolution = tp.quote_regions_decision(
                    "quotes", chunk, analysis)
                self.assertEqual("quote_forced", resolution)
                self.assertEqual(analysis["regions"], entries)
                # Now the analysis keys: both spans exist, only the one that
                # nests the double quote is adopted, the sibling is unadopted.
                spans = get_single_quoted_speech_spans(chunk)
                self.assertEqual(2, len(spans))
                self.assertTrue(analysis.get("nested_single_quoted_speech"))
                adopted = analysis.get("adopted_single_quote_spans") or ()
                self.assertEqual(1, len(adopted))
                self.assertEqual(spans[1], adopted[0])
                self.assertEqual(1, len(analysis.get("unadopted_single_quote_spans")))
                self.assertEqual(spans[0],
                                 (analysis.get("unadopted_single_quote_spans")
                                  or ())[0])
                self.assertEqual("Yes", chunk[spans[0][1]:spans[0][2]])

    def test_a_dropped_closer_with_a_nested_quote_is_uncertain(self):
        # Rev3 finding 1(b): the unresolvable range contains a paired double
        # quote, so the chunk is uncertain. Nothing is adopted and the gate
        # stays main's, but auto/lexical abstain and quotes never certifies it.
        analysis = analyze_outer_quote_regions(DROPPED_CLOSER)
        for mode in ("auto", "lexical"):
            self.assertEqual((None, None),
                             tp.quote_regions_decision(mode, DROPPED_CLOSER,
                                                       analysis))
        entries, resolution = tp.quote_regions_decision(
            "quotes", DROPPED_CLOSER, analysis)
        self.assertEqual("quote_forced", resolution)
        self.assertNotEqual("quote_presegmented", resolution)
        self.assertEqual(DROPPED_CLOSER_REGIONS, entries)
        self.assertTrue(analysis.get("uncertain_single_quoted_speech"))
        self.assertFalse(analysis.get("nested_single_quoted_speech"))
        self.assertEqual([], analysis.get("adopted_single_quote_spans"))
        # The gate result is main's: the analysis split still passes, and the
        # unresolved chunk is not made gate-uncertain.
        self.assertTrue(validate_segment_quality(
            DROPPED_CLOSER, DROPPED_CLOSER_REGIONS,
            quote_analysis=analysis)["passed"])

    def test_a_narration_quotation_with_only_nested_quotes_is_a_limit(self):
        # Rev3 finding 3: kept deliberately. The single span nests a paired
        # quote, so the decided heuristic calls it speech. Head's split is
        # NARRATOR / SPOKEN / NARRATOR (main's is NARRATOR / SPOKEN / NARRATOR
        # with the inverted middle); both are wrong for a narration quotation,
        # and the truthful whole-NARRATOR row is still rejected.
        analysis = analyze_outer_quote_regions(NARRATION_ONLY_NESTED)
        self.assertEqual(NARRATION_ONLY_NESTED_REGIONS, analysis["regions"])
        self.assertNotEqual(MAIN_NARRATION_ONLY_NESTED_REGIONS,
                            analysis["regions"])
        for mode in ("auto", "lexical"):
            self.assertEqual(
                (None, None),
                tp.quote_regions_decision(mode, NARRATION_ONLY_NESTED,
                                          analysis))
        entries, resolution = tp.quote_regions_decision(
            "quotes", NARRATION_ONLY_NESTED, analysis)
        self.assertEqual("quote_presegmented", resolution)
        self.assertEqual(NARRATION_ONLY_NESTED_REGIONS, entries)
        whole_narration = [{"type": "NARRATOR",
                            "text": NARRATION_ONLY_NESTED}]
        for gate_label, kwargs in (
                ("analysis-backed", {"quote_analysis": analysis}),
                ("analysis-free", {})):
            with self.subTest(gate=gate_label):
                report = validate_segment_quality(
                    NARRATION_ONLY_NESTED, whole_narration, **kwargs)
                self.assertFalse(report["passed"])
                self.assertIn(
                    "crosses_quote_boundary",
                    {finding["code"] for finding in report["findings"]})
        self.assertTrue(analysis.get("nested_single_quoted_speech"))

    def test_the_masked_opener_guards_are_discriminated(self):
        # Rev3 finding 2: each of these cases has no outside double pair, so a
        # single guard removal changes the decision or the predicate.
        analysis = analyze_outer_quote_regions(APOSTROPHE_OPENER_GUARD)
        self.assertEqual([], get_single_quoted_speech_spans(
            APOSTROPHE_OPENER_GUARD))
        self.assertFalse(analysis.get("nested_single_quoted_speech"))
        self.assertEqual(
            (analysis["regions"], "quote_presegmented"),
            tp.quote_regions_decision("auto", APOSTROPHE_OPENER_GUARD, analysis))

        analysis = analyze_outer_quote_regions(TRAILING_SPACE_OPENER_GUARD)
        self.assertEqual([], get_single_quoted_speech_spans(
            TRAILING_SPACE_OPENER_GUARD))
        self.assertFalse(analysis.get("nested_single_quoted_speech"))
        self.assertEqual(
            (analysis["regions"], "quote_presegmented"),
            tp.quote_regions_decision("auto", TRAILING_SPACE_OPENER_GUARD,
                                      analysis))

        # Opener-side inch check only: the closer-side check would mask its
        # removal on `6" by 8"`, so probe a pair the closer cannot settle.
        self.assertFalse(pq._contains_paired_quote(INCH_OPENER_PROBE))

    def test_openers_inside_a_failed_range_are_skipped(self):
        # Rev3 finding 4: after a failed range the scan resumes past it instead
        # of testing every opener inside it (O(n * 500) on all-opener input).
        # Count the paragraph search, which only an outer opener performs:
        # skipping yields one per failed range, not one per opener.
        searched = []

        class Spy:
            def __init__(self, pattern):
                self.pattern = pattern

            def search(self, text, pos=0, endpos=None):
                searched.append(pos)
                if endpos is None:
                    return self.pattern.search(text, pos)
                return self.pattern.search(text, pos, endpos)

        with mock.patch.object(pq, "_BLANK_LINE", Spy(pq._BLANK_LINE)):
            spans = get_single_quoted_speech_spans("\u2018a " * 2000)
        self.assertEqual([], spans)
        self.assertLess(len(searched), 50,
                        "every opener inside a failed range was rescanned")

    def test_quotes_forced_keeps_the_gate_log_line(self):
        # Rev3 finding 9: the early return skipped the gate and its telemetry.
        # The resolution and regions stay `quote_forced`, but a gate that does
        # not pass must still print the same warning the fall-through prints.
        analysis = analyze_outer_quote_regions(SECOND)
        failed = {"passed": False,
                  "findings": [{"code": "mixed_quote_region"}]}
        with mock.patch.object(tp, "validate_segment_quality",
                               return_value=failed):
            output = io.StringIO()
            with mock.patch("sys.stdout", output):
                entries, resolution = tp.quote_regions_decision(
                    "quotes", SECOND, analysis)
        self.assertEqual("quote_forced", resolution)
        self.assertEqual(SECOND_NARROW, entries)
        self.assertIn("segment gate not met", output.getvalue())
        self.assertIn("mixed_quote_region", output.getvalue())

    def test_possessive_after_the_nested_quote_does_not_close_the_span(self):
        # Rev2 finding 2: the possessive guard must also look back at the
        # nested quote, not only forward to a later one.
        for label, (chunk, speech, analysis_speech,
                    wrong_speech, wrong_narration) in POSSESSIVE_AFTER_CASES.items():
            with self.subTest(label=label):
                analysis = analyze_outer_quote_regions(chunk)
                # Behaviour first (#2331 review 3).
                report = validate_segment_quality(
                    chunk, [{"type": "SPOKEN", "text": speech},
                            {"type": "NARRATOR", "text": "she said."}],
                    quote_analysis=analysis)
                self.assertTrue(report["passed"], report["findings"])
                # The old split at the possessive close is now wrong.
                wrong = [
                    {"type": "SPOKEN", "text": wrong_speech},
                    {"type": "NARRATOR", "text": wrong_narration},
                ]
                bad = validate_segment_quality(chunk, wrong,
                                               quote_analysis=analysis)
                self.assertFalse(bad["passed"], "certifies a truncated span")
                self.assertTrue(analysis.get("nested_single_quoted_speech"))
                self.assertEqual(1, len(analysis.get("adopted_single_quote_spans")))
                self.assertEqual(
                    [{"type": "SPOKEN", "text": analysis_speech},
                     {"type": "NARRATOR", "text": "she said."}],
                    analysis["regions"])

    def test_nested_single_quoted_span_inside_the_adopted_span(self):
        # Rev2 finding 3: `‘…‘…’…’` must adopt the OUTER span, not stop at the
        # inner closer.
        for label, chunk, truthful, analysis_speech in (
                ("nested speech", NESTED_SINGLE_A, NESTED_SINGLE_A_TRUTHFUL,
                 "He said run to \u2018Bob\u2019 then left,"),
                ("quoted word", NESTED_SINGLE_B, NESTED_SINGLE_B_TRUTHFUL,
                 "The word \u2018run\u2019 means move fast,")):
            with self.subTest(label=label):
                analysis = analyze_outer_quote_regions(chunk)
                # Behaviour first (#2331 review 3).
                report = validate_segment_quality(chunk, truthful,
                                                  quote_analysis=analysis)
                self.assertTrue(report["passed"], report["findings"])
                self.assertTrue(analysis.get("nested_single_quoted_speech"))
                self.assertEqual(1, len(analysis.get("adopted_single_quote_spans")))
                self.assertEqual(
                    [{"type": "SPOKEN", "text": analysis_speech},
                     {"type": "NARRATOR", "text": "she said."}],
                    analysis["regions"])

    def test_unresolved_nested_single_quote_abstains_from_adoption(self):
        # Rev2 finding 3: one inner opener and no outer closer after it cannot
        # be resolved, so nothing is adopted and the chunk keeps main's split.
        # Rev3: the failed range nests a paired double quote in both cases, so
        # the chunk is uncertain and quotes no longer certifies it.
        for label, (chunk, regions) in AMBIGUOUS_CASES.items():
            with self.subTest(label=label):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertFalse(analysis.get("nested_single_quoted_speech"))
                self.assertEqual([], analysis.get("adopted_single_quote_spans"))
                self.assertEqual([], analysis.get("unadopted_single_quote_spans") or [])
                self.assertEqual(regions, analysis["regions"])
                for mode in ("auto", "lexical"):
                    self.assertEqual((None, None),
                                     tp.quote_regions_decision(mode, chunk,
                                                               analysis))
                self.assertEqual(
                    (regions, "quote_forced"),
                    tp.quote_regions_decision("quotes", chunk, analysis))
                self.assertTrue(analysis.get("uncertain_single_quoted_speech"))

    def test_inch_marks_are_measurements_not_nested_quotes(self):
        # Rev2 finding 4, both directions: the inch rule decides a pair.
        self.assertFalse(pq._contains_paired_quote('6" by 8"'))
        self.assertTrue(pq._contains_paired_quote('6" wide and said \u201chi\u201d'))
        # (a) outside the span: two inch marks must not disable the fix.
        analysis = analyze_outer_quote_regions(INCH_OUTSIDE)
        self.assertTrue(analysis.get("nested_single_quoted_speech"))
        self.assertEqual(1, len(analysis.get("adopted_single_quote_spans")))
        self.assertEqual(INCH_OUTSIDE_REGIONS, analysis["regions"])
        report = validate_segment_quality(
            INCH_OUTSIDE,
            [{"type": "SPOKEN", "text": "She said \u201crun\u201d,"},
             {"type": "NARRATOR",
              "text": "Anna said. The sign is 6\" wide and the post 8\" tall."}],
            quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])
        self.assertEqual(
            (INCH_OUTSIDE_REGIONS, "quote_presegmented"),
            tp.quote_regions_decision("quotes", INCH_OUTSIDE, analysis))
        # (b) inside the span: inch marks alone are not a nested quote, so a
        # narration-only chunk is not adopted as speech.
        analysis = analyze_outer_quote_regions(INCH_ONLY)
        self.assertFalse(analysis.get("nested_single_quoted_speech"))
        self.assertEqual([], analysis.get("adopted_single_quote_spans"))
        self.assertEqual(INCH_ONLY_REGIONS, analysis["regions"])
        entries, resolution = tp.quote_regions_decision("quotes", INCH_ONLY,
                                                        analysis)
        self.assertEqual("quote_forced", resolution)
        self.assertEqual(INCH_ONLY_REGIONS, entries)
        report = validate_segment_quality(INCH_ONLY, INCH_ONLY_REGIONS,
                                          quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])

    def test_the_paragraph_search_is_bounded(self):
        # Rev2 finding 5: each opener's paragraph search must stop at the
        # 500-character span bound instead of rescanning the rest of the chunk
        # (0.97 s for a 100 KB quote-heavy chunk before the bound).
        chunk = "\u2018Hello,\u2019 she said. " * 400
        bounded = []

        class Spy:
            def __init__(self, pattern):
                self.pattern = pattern

            def search(self, text, pos=0, endpos=None):
                bounded.append((pos, endpos))
                if endpos is None:
                    return self.pattern.search(text, pos)
                return self.pattern.search(text, pos, endpos)

        with mock.patch.object(pq, "_BLANK_LINE", Spy(pq._BLANK_LINE)):
            spans = get_single_quoted_speech_spans(chunk)
        self.assertTrue(spans)
        self.assertTrue(bounded)
        for pos, endpos in bounded:
            self.assertIsNotNone(endpos, "unbounded paragraph search")
            self.assertLessEqual(endpos, pos + pq._SINGLE_QUOTE_MAX_SPAN)

    def test_straight_elision_guard_without_an_outside_pair(self):
        # Rev2 finding 6: the earlier straight-elision cases all had a second
        # outside double pair, which masked the elision regex.
        for label, (chunk, regions) in STRAIGHT_ELISION_ALONE_CASES.items():
            with self.subTest(label=label):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertFalse(analysis.get("nested_single_quoted_speech"))
                self.assertEqual([], analysis.get("adopted_single_quote_spans"))
                self.assertEqual(regions, analysis["regions"])

    def test_tis_and_twas_straight_openers_keep_main_behaviour(self):
        # Rev2 finding 7: a straight `'Tis`/`'Twas` is indistinguishable from
        # the elision apostrophe, so main's behaviour is pinned (Limits).
        for label, chunk, narration in (
                ("tis", "'Tis \"fine\",' she said.", "'Tis"),
                ("twas", "'Twas \"fine\",' she said.", "'Twas")):
            with self.subTest(label=label):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertFalse(analysis.get("nested_single_quoted_speech"))
                self.assertEqual([], analysis.get("adopted_single_quote_spans"))
                self.assertEqual(
                    [{"type": "NARRATOR", "text": narration},
                     {"type": "SPOKEN", "text": "fine"},
                     {"type": "NARRATOR", "text": ",' she said."}],
                    analysis["regions"])

    def test_straight_quote_version_is_not_presegmented_inside_out(self):
        self.assertions_for_issue_chunk(
            STRAIGHT, STRAIGHT_CORRECT, STRAIGHT_INVERTED,
            [{"type": "SPOKEN", "text": "She told me run and I ran,"},
             {"type": "NARRATOR", "text": "said Anna. Ben left."}])

    def test_the_same_text_without_nested_quotes_is_unchanged(self):
        analysis = analyze_outer_quote_regions(CONTROL)
        self.assertFalse(analysis.get("nested_single_quoted_speech"))
        self.assertEqual([], analysis["regions"])
        self.assertEqual([], analysis.get("adopted_single_quote_spans"))
        for mode in ("auto", "lexical"):
            self.assertEqual((None, None),
                             tp.quote_regions_decision(mode, CONTROL, analysis))
        report = validate_segment_quality(CONTROL, CONTROL_CORRECT,
                                          quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])

    def test_a_double_quoted_book_with_nested_single_quotes_is_unchanged(self):
        analysis = analyze_outer_quote_regions(DOUBLE_BOOK)
        self.assertFalse(analysis.get("nested_single_quoted_speech"))
        self.assertEqual([], analysis.get("adopted_single_quote_spans"))
        self.assertEqual(
            [{"type": "SPOKEN", "text": "She said \u2018run\u2019 and left,"},
             {"type": "NARRATOR", "text": "said Anna. Ben left."}],
            analysis["regions"])
        entries, resolution = tp.quote_regions_decision("auto", DOUBLE_BOOK, analysis)
        self.assertEqual("quote_presegmented", resolution)
        self.assertEqual(analysis["regions"], entries)
        report = validate_segment_quality(DOUBLE_BOOK, analysis["regions"],
                                          quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])

    def test_unanadopted_span_does_not_whitelist_a_double_quoted_chunk(self):
        # Finding 1, repro A: the span is inside the double speech, so the
        # decision never adopts it and its marks are not content for the gate.
        analysis = analyze_outer_quote_regions(A_CHUNK)
        self.assertFalse(analysis.get("nested_single_quoted_speech"))
        self.assertEqual([], analysis.get("adopted_single_quote_spans"))
        self.assertEqual(A_REGIONS, analysis["regions"])
        entries, resolution = tp.quote_regions_decision("auto", A_CHUNK, analysis)
        self.assertEqual("quote_presegmented", resolution)
        self.assertEqual(A_REGIONS, entries)
        for label, row in (("truthful", A_TRUTHFUL), ("keeps outer", A_KEEPS_OUTER)):
            with self.subTest(label=label):
                report = validate_segment_quality(A_CHUNK, row, quote_analysis=analysis)
                self.assertFalse(report["passed"], "main rejects this row")
                self.assertIn("mixed_quote_region",
                              {finding["code"] for finding in report["findings"]})

    def test_a_quoted_title_in_narration_is_not_adopted_as_speech(self):
        # Finding 2: only the span that nests the double quote is speech.
        analysis = analyze_outer_quote_regions(TITLE)
        self.assertTrue(analysis.get("nested_single_quoted_speech"))
        spans = analysis.get("adopted_single_quote_spans")
        self.assertEqual(1, len(spans))
        self.assertEqual((0, 1), (spans[0][0], spans[0][1]))
        self.assertEqual("He said \u201crun\u201d and left,",
                         TITLE[spans[0][1]:spans[0][2]])
        self.assertEqual(TITLE_REGIONS, analysis["regions"])
        self.assertNotIn("Hamlet",
                         " ".join(region["text"] for region in analysis["regions"]
                                  if region["type"] == "SPOKEN"))
        for mode in ("auto", "lexical"):
            self.assertEqual((None, None),
                             tp.quote_regions_decision(mode, TITLE, analysis))
        report = validate_segment_quality(TITLE, TITLE_TRUTHFUL,
                                          quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])

    def test_a_title_in_a_non_targeted_chunk_stays_rejection_safe(self):
        # Rev2 finding 1's boundary: the unadopted-span gate allowance applies
        # only to a targeted chunk. Here the single-quoted title is narration
        # in a double-quoted book (no single span is adopted), so a model row
        # that calls it SPOKEN is still rejected.
        chunk = "He said \u201crun.\u201d Later \u2018Hamlet\u2019 was banned."
        analysis = analyze_outer_quote_regions(chunk)
        self.assertFalse(analysis.get("nested_single_quoted_speech"))
        self.assertEqual([], analysis.get("adopted_single_quote_spans"))
        self.assertEqual([], analysis.get("unadopted_single_quote_spans") or [])
        for label, entries in (
                ("split around the title",
                 [{"type": "NARRATOR", "text": "He said run. Later"},
                  {"type": "SPOKEN", "text": "Hamlet"},
                  {"type": "NARRATOR", "text": "was banned."}]),
                ("title merged into speech",
                 [{"type": "SPOKEN", "text": "Hamlet"},
                  {"type": "NARRATOR", "text": "He said run. Later was banned."}])):
            with self.subTest(label=label):
                report = validate_segment_quality(chunk, entries,
                                                  quote_analysis=analysis)
                self.assertFalse(report["passed"])
                self.assertIn("quote_region_misclassified",
                              {finding["code"] for finding in report["findings"]})

    def test_gate_allows_nested_marks_by_position_not_by_character(self):
        analysis = analyze_outer_quote_regions(FIRST)
        # The correct split reproduces the adopted span's content verbatim.
        good = validate_segment_quality(FIRST, FIRST_CORRECT, quote_analysis=analysis)
        self.assertTrue(good["passed"], good["findings"])
        # The same characters, but the outer delimiters are still there: the
        # two outermost marks are outside the adopted span, so this is mixed.
        keeps_outer = [
            {"type": "SPOKEN",
             "text": "\u201cShe told me \u201crun\u201d and I ran,\u201d"},
            {"type": "NARRATOR", "text": "said Anna. Ben left."},
        ]
        report = validate_segment_quality(FIRST, keeps_outer,
                                          quote_analysis=analysis)
        self.assertFalse(report["passed"])
        self.assertIn("mixed_quote_region",
                      {finding["code"] for finding in report["findings"]})
        # An extra double-quoted phrase after the adopted content is not part
        # of the span either, even though the characters are allowed in it.
        extra = [
            {"type": "SPOKEN",
             "text": "She told me \u201crun\u201d and I ran, \u201cextra\u201d"},
            {"type": "NARRATOR", "text": "said Anna. Ben left."},
        ]
        report = validate_segment_quality(FIRST, extra, quote_analysis=analysis)
        self.assertFalse(report["passed"])
        self.assertIn("mixed_quote_region",
                      {finding["code"] for finding in report["findings"]})

    def test_possessive_and_elision_before_the_real_closer(self):
        # Finding 3: `dogs’`, `boys'` and `rock ’n’ roll` must not truncate the
        # span before the nested double quote.
        for label, (chunk, speech, analysis_speech, narration) in CLOSER_CASES.items():
            with self.subTest(label=label):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertTrue(analysis.get("nested_single_quoted_speech"))
                self.assertEqual([{"type": "SPOKEN", "text": analysis_speech},
                                  {"type": "NARRATOR", "text": narration}],
                                 analysis["regions"])
                # The truthful split keeps the nested quote marks.
                report = validate_segment_quality(
                    chunk, [{"type": "SPOKEN", "text": speech},
                            {"type": "NARRATOR", "text": narration}],
                    quote_analysis=analysis)
                self.assertTrue(report["passed"], report["findings"])

    def test_narration_quotation_in_a_double_quoted_book_is_unchanged(self):
        # Finding 4: the chunk already has depth-0 double speech, so no single
        # span is adopted and decision and gate stay main's.
        analysis = analyze_outer_quote_regions(NARRATION_IN_DOUBLE_BOOK)
        self.assertFalse(analysis.get("nested_single_quoted_speech"))
        self.assertEqual([], analysis.get("adopted_single_quote_spans"))
        self.assertEqual(NARRATION_IN_DOUBLE_BOOK_REGIONS, analysis["regions"])
        for mode in ("auto", "quotes", "lexical"):
            entries, resolution = tp.quote_regions_decision(
                mode, NARRATION_IN_DOUBLE_BOOK, analysis)
            self.assertEqual("quote_presegmented", resolution)
            self.assertEqual(NARRATION_IN_DOUBLE_BOOK_REGIONS, entries)
        report = validate_segment_quality(
            NARRATION_IN_DOUBLE_BOOK, NARRATION_IN_DOUBLE_BOOK_REGIONS,
            quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])

    def test_an_inch_mark_inside_an_adopted_span_stays_content(self):
        # Finding 5: `6"` is not a nested opener, so it must not add depth and
        # swallow the attribution.
        analysis = analyze_outer_quote_regions(INCH)
        self.assertTrue(analysis.get("nested_single_quoted_speech"))
        self.assertEqual([], analysis["repairs"])
        self.assertEqual(INCH_REGIONS, analysis["regions"])
        report = validate_segment_quality(INCH, INCH_TRUTHFUL,
                                          quote_analysis=analysis)
        self.assertTrue(report["passed"], report["findings"])

    def test_guillemets_and_corner_brackets_nested_in_single_quotes(self):
        # Finding 7: `_DOUBLE_QUOTE_PAIRS` is PAIRED, not a hand copy.
        self.assertEqual(tuple(PAIRED), pq._DOUBLE_QUOTE_PAIRS)
        for label, chunk, truthful, analysis_text in (
                ("guillemets", GUILLEMETS,
                 "He said \u00abbonjour\u00bb twice,",
                 "He said bonjour twice,"),
                ("corner brackets", CORNER_BRACKETS,
                 "He said \u300c\u3053\u3093\u306b\u3061\u306f\u300d twice,",
                 "He said \u3053\u3093\u306b\u3061\u306f twice,")):
            with self.subTest(label=label):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertTrue(analysis.get("nested_single_quoted_speech"))
                self.assertEqual([{"type": "SPOKEN", "text": analysis_text},
                                  {"type": "NARRATOR", "text": "she said."}],
                                 analysis["regions"])
                report = validate_segment_quality(
                    chunk, [{"type": "SPOKEN", "text": truthful},
                            {"type": "NARRATOR", "text": "she said."}],
                    quote_analysis=analysis)
                self.assertTrue(report["passed"], report["findings"])

    def test_british_multiparagraph_and_split_chunks_are_pinned_limits(self):
        # Finding 6: both are standard typography this decided fix does not
        # reach. Pin the behaviour so a future change is deliberate. Since rev3
        # the paragraph-bound failure records an uncertain range when a paired
        # quote nests inside it, so auto/lexical abstain and quotes returns the
        # analysis regions as quote_forced: main's inverted split is no longer
        # certified. The analysis regions themselves stay main's.
        for chunk, regions in ((BRITISH, BRITISH_REGIONS),
                               (SPLIT_HEAD, SPLIT_HEAD_REGIONS),
                               (SPLIT_TAIL, SPLIT_TAIL_REGIONS)):
            with self.subTest(chunk=chunk[:20]):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertFalse(analysis.get("nested_single_quoted_speech"))
                self.assertEqual([], analysis.get("adopted_single_quote_spans"))
                self.assertEqual(regions, analysis["regions"])
                if chunk in (BRITISH, SPLIT_HEAD):
                    self.assertTrue(analysis.get("uncertain_single_quoted_speech"))
                    for mode in ("auto", "lexical"):
                        self.assertEqual(
                            (None, None),
                            tp.quote_regions_decision(mode, chunk, analysis))
                    self.assertEqual(
                        (regions, "quote_forced"),
                        tp.quote_regions_decision("quotes", chunk, analysis))
                else:
                    self.assertFalse(analysis.get("uncertain_single_quoted_speech"))

    def test_apostrophes_elisions_and_wrong_direction_do_not_open_speech(self):
        cases = {
            # Curly ’ is the apostrophe, so it can never open: elisions and
            # possessives are here for the straight-quote versions too.
            "curly elision": "He said \u2019tis \u201clate\u201d now.",
            "curly em": "He said \u2019em \u201clate\u201d now.",
            "curly nineties": "The \u201990s \u201cmusic\u201d was loud.",
            "straight elision": "He said 'tis \u201clate\u201d now and James' \u201cbook\u201d came.",
            "straight em": "He said 'em \u201clate\u201d now and James' \u201cbook\u201d came.",
            "straight nineties": "The '90s \u201cmusic\u201d was loud and James' \u201cbook\u201d came.",
            "possessive": "Anna's \u201cbook\u201d and James' \u201cpen\u201d came.",
            "plural possessive": "The Joneses\u2019 \u201cbook\u201d and James' \u201cpen\u201d came.",
            # A straight possessive must not open a span that then adopts the
            # double-quoted phrase (the opener guard is what stops it).
            "straight possessive before quote": "James' \u201cbook\u201d is here, and 'Hello,' she said.",
            # A contraction apostrophe must not open a span either.
            "straight contraction before quote": "He can't say \u201cno\u201d, and 'Hello,' she said.",
            # A ‘ used as the closer of a low-high ‚…‘ pair is not an opener.
            "wrong direction": "\u201aHallo\u2018 \u201cbook\u201d and James\u2019 \u201cpen\u201d came.",
        }
        for label, chunk in cases.items():
            with self.subTest(label=label):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertFalse(analysis.get("nested_single_quoted_speech"))
                # The double quotes stay the only convention, as on main.
                self.assertGreater(len(analysis["regions"]), 1)

    def test_an_unclosed_opener_does_not_run_past_its_paragraph_or_500_chars(self):
        # A closer-shaped apostrophe (followed by a space) waits beyond both
        # bounds; only an unbounded search would pair with it.
        past_paragraph = "\u2018He said \u201crun\u201d\n\nand James\u2019 book came."
        past_limit = ("\u2018He said \u201crun\u201d" + (" word" * 120)
                      + " James\u2019 book came.")
        for label, chunk in (("paragraph", past_paragraph), ("length", past_limit)):
            with self.subTest(label=label):
                if get_single_quoted_speech_spans is None:
                    self.fail("get_single_quoted_speech_spans is missing on this tree")
                spans = get_single_quoted_speech_spans(chunk)
                self.assertEqual([], spans)
                self.assertFalse(analyze_outer_quote_regions(chunk).get(
                    "nested_single_quoted_speech"))

    def test_a_nested_single_quoted_quotation_is_not_a_sibling_bound(self):
        # Rev4 finding 1: a candidate opener with its own closer is a closed
        # nested quotation, not a new sibling speech, so it must not end the
        # possessive lookahead. The two repros are curly only: the straight
        # version is accidentally immune because opener and closer coincide.
        for label, chunk, truthful, inverted in (
                ("possessive before", POSSESSIVE_NESTED,
                 POSSESSIVE_NESTED_TRUTHFUL, POSSESSIVE_NESTED_INVERTED),
                ("possessive after", POSSESSIVE_AFTER_NESTED,
                 POSSESSIVE_AFTER_NESTED_TRUTHFUL,
                 POSSESSIVE_AFTER_NESTED_INVERTED)):
            with self.subTest(label=label):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertTrue(analysis.get("nested_single_quoted_speech"))
                self.assertEqual(1, len(analysis.get("adopted_single_quote_spans")))
                # The nested quotation is not a sentence: the nested reading is
                # unambiguous, so the chunk is not made uncertain.
                self.assertFalse(analysis.get("uncertain_single_quoted_speech"))
                for mode in ("auto", "lexical"):
                    self.assertEqual(
                        (None, None),
                        tp.quote_regions_decision(mode, chunk, analysis))
                entries, resolution = tp.quote_regions_decision(
                    "quotes", chunk, analysis)
                self.assertEqual("quote_presegmented", resolution)
                self.assertEqual(analysis["regions"], entries)
                for gate_label, kwargs in (
                        ("analysis-backed", {"quote_analysis": analysis}),
                        ("analysis-free", {})):
                    with self.subTest(gate=gate_label):
                        report = validate_segment_quality(
                            chunk, truthful, **kwargs)
                        self.assertTrue(report["passed"], report["findings"])
                        bad = validate_segment_quality(chunk, inverted, **kwargs)
                        self.assertFalse(bad["passed"], "certifies the inverted split")

    def test_a_sibling_reading_that_disagrees_is_uncertain(self):
        # Rev4 finding 1's fallback: when the candidate opener is closed exactly
        # at the later closer and a sentence terminator precedes it, the nested
        # (possessive) and sibling readings both fit. The scan keeps the sibling
        # reading so the spans still split, but the chunk is never certified.
        analysis = analyze_outer_quote_regions(SIBLING_AFTER_S)
        self.assertTrue(analysis.get("nested_single_quoted_speech"))
        self.assertTrue(analysis.get("uncertain_single_quoted_speech"))
        # Without a sentence terminator the nested reading is unambiguous and
        # POSSESSIVE_NESTED stays out of the uncertainty net (covered above).

    def test_a_possessive_before_a_nested_quote_with_an_apostrophe(self):
        # Rev4 finding 2: `later` skips marks that fail the closer predicate,
        # so the apostrophe in `it’s`/`don’t`/`I’m` no longer ends the span.
        for label, (chunk, speech, analysed, inverted) in (
                NESTED_APOSTROPHE_CASES.items()):
            with self.subTest(label=label):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertTrue(analysis.get("nested_single_quoted_speech"))
                self.assertEqual(1, len(analysis.get("adopted_single_quote_spans")))
                self.assertEqual(
                    [{"type": "SPOKEN", "text": analysed},
                     {"type": "NARRATOR", "text": "she said."}],
                    analysis["regions"])
                truthful = [{"type": "SPOKEN", "text": speech},
                            {"type": "NARRATOR", "text": "she said."}]
                for gate_label, kwargs in (
                        ("analysis-backed", {"quote_analysis": analysis}),
                        ("analysis-free", {})):
                    with self.subTest(gate=gate_label):
                        report = validate_segment_quality(
                            chunk, truthful, **kwargs)
                        self.assertTrue(report["passed"], report["findings"])
                for mode in ("auto", "lexical"):
                    self.assertEqual(
                        (None, None),
                        tp.quote_regions_decision(mode, chunk, analysis))
                for gate_label, kwargs in (
                        ("analysis-backed", {"quote_analysis": analysis}),
                        ("analysis-free", {})):
                    with self.subTest(inverted_gate=gate_label):
                        self.assertFalse(
                            validate_segment_quality(
                                chunk, inverted, **kwargs)["passed"],
                            "certifies the inverted split")

    def test_a_plural_possessive_inside_the_nested_quote_keeps_the_span(self):
        # Rev5 finding 1: the pair straddles the `…s’` mark, so the lookahead
        # checks the whole `opener+1..later` range. Every case adopts one span,
        # auto/lexical abstain, "quotes" certifies the truthful split, the
        # truthful row passes both gate paths, and main's inside-out row does
        # not. The guillemets-only analysis-free path is the documented
        # `_QUOTE_CHARS` limit: that gate skips instead of rejecting.
        cases = dict(POSSESSIVE_INSIDE_CASES)
        cases["sibling quotation"] = (
            SIBLING_POSSESSIVE_INSIDE, SIBLING_POSSESSIVE_INSIDE_TRUTHFUL,
            _inside_out_row(SIBLING_POSSESSIVE_INSIDE, "\u201c", "\u201d"))
        for label, (chunk, truthful, inverted) in cases.items():
            with self.subTest(label=label):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertTrue(analysis.get("nested_single_quoted_speech"))
                self.assertFalse(analysis.get("uncertain_single_quoted_speech"))
                self.assertEqual(
                    1, len(analysis.get("adopted_single_quote_spans")))
                if get_single_quoted_speech_spans is not None:
                    self.assertEqual(
                        1, len(get_single_quoted_speech_spans(chunk)))
                for mode in ("auto", "lexical"):
                    self.assertEqual(
                        (None, None),
                        tp.quote_regions_decision(mode, chunk, analysis))
                entries, resolution = tp.quote_regions_decision(
                    "quotes", chunk, analysis)
                self.assertEqual("quote_presegmented", resolution)
                self.assertEqual(
                    [{"type": entry["type"], "text": entry["text"]}
                     for entry in analysis["regions"]], entries)
                for gate_label, kwargs in (
                        ("analysis-backed", {"quote_analysis": analysis}),
                        ("analysis-free", {})):
                    with self.subTest(gate=gate_label):
                        report = validate_segment_quality(
                            chunk, truthful, **kwargs)
                        self.assertTrue(report["passed"], report["findings"])
                        bad = validate_segment_quality(chunk, inverted, **kwargs)
                        if (gate_label == "analysis-free"
                                and label == "guillemets"):
                            # `_QUOTE_CHARS` omits «» on main too: this path
                            # records a skipped gate, not a vouched split.
                            self.assertTrue(
                                bad["quote_gate"].startswith("skipped:"),
                                bad["quote_gate"])
                            continue
                        self.assertFalse(
                            bad["passed"],
                            "certifies main's inside-out split")

    def test_a_no_terminator_sibling_drops_both_spans_and_is_quote_forced(self):
        # Rev5 finding 2: with no sentence punctuation the lookahead takes the
        # nested reading, so the outer span never closes. Both spans are
        # dropped (not continued), the chunk is uncertain, auto/lexical
        # abstain, and "quotes" returns main's split as quote_forced.
        analysis = analyze_outer_quote_regions(NO_TERMINATOR_SIBLING)
        self.assertEqual([], analysis.get("adopted_single_quote_spans"))
        self.assertEqual([], analysis.get("unadopted_single_quote_spans"))
        self.assertFalse(analysis.get("nested_single_quoted_speech"))
        self.assertTrue(analysis.get("uncertain_single_quoted_speech"))
        if get_single_quoted_speech_spans is not None:
            self.assertEqual([], get_single_quoted_speech_spans(
                NO_TERMINATOR_SIBLING))
        for mode in ("auto", "lexical"):
            self.assertEqual(
                (None, None),
                tp.quote_regions_decision(mode, NO_TERMINATOR_SIBLING,
                                          analysis))
        entries, resolution = tp.quote_regions_decision(
            "quotes", NO_TERMINATOR_SIBLING, analysis)
        self.assertEqual("quote_forced", resolution)
        self.assertEqual(NO_TERMINATOR_SIBLING_REGIONS, entries)
        self.assertNotIn("mixed_quote_region",
                         {finding["code"] for finding in
                          validate_segment_quality(
                              NO_TERMINATOR_SIBLING,
                              NO_TERMINATOR_SIBLING_REGIONS,
                              quote_analysis=analysis)["findings"]})

    def test_a_mixed_convention_sibling_pins_the_no_own_closer_branch(self):
        # Rev5 nit 3: keeping the `own == -1` branch. The straight opener of
        # the sibling has no closer of its own, so it is a sibling and `Yes’`
        # closes; the `Yes` span exists. Removing the branch drops that span,
        # so this assertion is the mutation's discriminator. Either way no
        # wrong split is certified: "quotes" stays quote_forced.
        analysis = analyze_outer_quote_regions(MIXED_CONVENTION_SIBLING)
        if get_single_quoted_speech_spans is not None:
            self.assertEqual(
                [(0, 1, 4, 4)],
                get_single_quoted_speech_spans(MIXED_CONVENTION_SIBLING))
        self.assertEqual([], analysis.get("adopted_single_quote_spans"))
        self.assertTrue(analysis.get("uncertain_single_quoted_speech"))
        for mode in ("auto", "lexical"):
            self.assertEqual(
                (None, None),
                tp.quote_regions_decision(mode, MIXED_CONVENTION_SIBLING,
                                          analysis))
        entries, resolution = tp.quote_regions_decision(
            "quotes", MIXED_CONVENTION_SIBLING, analysis)
        self.assertEqual("quote_forced", resolution)
        self.assertEqual(MIXED_CONVENTION_SIBLING_REGIONS, entries)

    def test_the_gate_rejects_the_repeated_attribution_inside_out_row(self):
        # Rev4 finding 3: the speech's opening words recur in the attribution,
        # so the old role check found main's inside-out row inside the narrator
        # text and accepted it. The 32 chunks × 2 gate paths must now be
        # rejected, and the
        # truthful row must still pass on both gate paths.
        for pronoun in REPEATED_ATTRIBUTION_PRONOUNS:
            for verb in REPEATED_ATTRIBUTION_VERBS:
                for obj in REPEATED_ATTRIBUTION_OBJECTS:
                    with self.subTest(pronoun=pronoun, verb=verb, obj=obj):
                        chunk = ("\u2018%s %s \u201c%s\u201d,\u2019 %s %s. "
                                 "Ben left." % (pronoun.capitalize(), verb, obj,
                                                pronoun, verb))
                        analysis = analyze_outer_quote_regions(chunk)
                        open_mark = chunk.index("\u201c")
                        close_mark = chunk.index("\u201d")
                        inverted = [
                            {"type": "NARRATOR", "text": chunk[:open_mark]},
                            {"type": "SPOKEN", "text": obj},
                            {"type": "NARRATOR", "text": chunk[close_mark + 1:]},
                        ]
                        truthful = [
                            {"type": "SPOKEN",
                             "text": "%s %s \u201c%s\u201d," % (
                                 pronoun.capitalize(), verb, obj)},
                            {"type": "NARRATOR",
                             "text": "%s %s. Ben left." % (pronoun, verb)},
                        ]
                        for gate_label, kwargs in (
                                ("analysis-backed", {"quote_analysis": analysis}),
                                ("analysis-free", {})):
                            with self.subTest(gate=gate_label):
                                report = validate_segment_quality(
                                    chunk, inverted, **kwargs)
                                self.assertFalse(report["passed"],
                                                 "32 chunks × 2 gate paths must fail")
                                self.assertIn(
                                    "quote_region_misclassified",
                                    {finding["code"]
                                     for finding in report["findings"]})
                                good = validate_segment_quality(
                                    chunk, truthful, **kwargs)
                                self.assertTrue(good["passed"], good["findings"])
        # The review's exact repro.
        chunk = "\u2018She said \u201crun\u201d,\u2019 she said. Ben left."
        analysis = analyze_outer_quote_regions(chunk)
        inverted = [
            {"type": "NARRATOR", "text": "\u2018She said"},
            {"type": "SPOKEN", "text": "run"},
            {"type": "NARRATOR", "text": ",\u2019 she said. Ben left."},
        ]
        self.assertFalse(validate_segment_quality(
            chunk, inverted, quote_analysis=analysis)["passed"])

    def test_a_nested_failed_range_marks_the_chunk_uncertain(self):
        # Rev4 finding 6: the scan resumes at the outer failed range's end but
        # still records the openers that started inside it, so a pair in the
        # inner range is not hidden.
        self.assertEqual(543, len(NESTED_FAILED_RANGE))
        analysis = analyze_outer_quote_regions(NESTED_FAILED_RANGE)
        self.assertTrue(analysis.get("uncertain_single_quoted_speech"))
        self.assertFalse(analysis.get("nested_single_quoted_speech"))
        for mode in ("auto", "lexical"):
            self.assertEqual(
                (None, None),
                tp.quote_regions_decision(mode, NESTED_FAILED_RANGE, analysis))
        self.assertEqual(
            (analysis["regions"], "quote_forced"),
            tp.quote_regions_decision("quotes", NESTED_FAILED_RANGE, analysis))

    def test_the_uncertainty_net_ignores_a_stray_apostrophe_in_double_quoted_prose(self):
        # Rev4 finding 4: a straight `'` after sentence punctuation and before
        # `-`/`"` is not a speech opener, so the three fixture strings and the
        # synthetic family keep main's decisions.
        root = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))))
        for name, index, key in STRAY_APOSTROPHE_FIXTURES:
            with self.subTest(fixture=name, index=index):
                with open(os.path.join(root, "app", "fixtures", name),
                          encoding="utf-8") as handle:
                    text = json.load(handle)["entries"][index][key]
                analysis = analyze_outer_quote_regions(text)
                self.assertFalse(analysis.get("uncertain_single_quoted_speech"))
                entries, resolution = tp.quote_regions_decision(
                    "auto", text, analysis)
                self.assertIsNotNone(entries)
                self.assertTrue(str(resolution).startswith("quote_presegmented"))
        for chunk in _stray_apostrophe_chunks():
            with self.subTest(chunk=chunk[:24]):
                analysis = analyze_outer_quote_regions(chunk)
                self.assertFalse(analysis.get("uncertain_single_quoted_speech"))
                self.assertIsNotNone(
                    tp.quote_regions_decision("auto", chunk, analysis)[0])

    def test_a_targeted_chunk_can_also_be_uncertain(self):
        # Rev4 finding 10: one adopted span plus one unresolved range that
        # nests a pair. The gate uses the adopted span, so this is not main's
        # result; only the report's "gate stays main's for uncertain chunks"
        # sentence was wrong.
        analysis = analyze_outer_quote_regions(NESTED_AND_UNCERTAIN)
        self.assertTrue(analysis.get("nested_single_quoted_speech"))
        self.assertTrue(analysis.get("uncertain_single_quoted_speech"))
        self.assertEqual(1, len(analysis.get("adopted_single_quote_spans")))
        for mode in ("auto", "lexical"):
            self.assertEqual(
                (None, None),
                tp.quote_regions_decision(mode, NESTED_AND_UNCERTAIN, analysis))
        entries, resolution = tp.quote_regions_decision(
            "quotes", NESTED_AND_UNCERTAIN, analysis)
        self.assertEqual("quote_forced", resolution)
        self.assertEqual(analysis["regions"], entries)
        # The adopted span is the gate's, so main's inside-out first speech is
        # rejected on both paths and the analysis row still passes.
        analysis_row = [{"type": entry["type"], "text": entry["text"]}
                        for entry in analysis["regions"]]
        for gate_label, kwargs in (
                ("analysis-backed", {"quote_analysis": analysis}),
                ("analysis-free", {})):
            with self.subTest(gate=gate_label):
                bad = validate_segment_quality(
                    NESTED_AND_UNCERTAIN, NESTED_AND_UNCERTAIN_INVERTED, **kwargs)
                self.assertFalse(bad["passed"])
                good = validate_segment_quality(
                    NESTED_AND_UNCERTAIN, analysis_row, **kwargs)
                self.assertTrue(good["passed"], good["findings"])

    def test_double_quoted_decisions_and_gate_are_unchanged(self):
        # Differential guard: 260 synthetic chunks plus the quote shapes the
        # repository already tests must produce byte-identical decisions and
        # gate reports to origin/main e71775d33 (the merged base; the digest
        # was produced from that tree, with the British/split-head family held
        # out because rev3 deliberately makes it abstain). Both the
        # analysis-backed gate and the analysis-free gate are hashed.
        self.assertEqual(
            "755aab93213e4edd375e288d6d9ff01f61ce1567a39197a0e3b5dc560ace6a53",
            _differential_digest())


if __name__ == "__main__":
    unittest.main()
