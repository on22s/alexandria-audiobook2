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
import re

PAIRED = [
    ('"', '"'),
    ("“", "”"),      # curly “ ”
    ("「", "」"),      # 「 」
    ("『", "』"),      # 『 』
]
# An em or en dash opening a line, the Continental convention.
DASH_LINE = re.compile(r"^[ \t]*[—–][ \t]*(\S.*)$", re.M)
# Script form: a speaker label, a colon, then the line.
LABEL_LINE = re.compile(r"^[ \t]*([A-Z][A-Z .'’-]{1,30}):[ \t]+(\S.*)$", re.M)


def _paired_spans(text):
    spans = []
    for opener, closer in PAIRED:
        if opener == closer:
            # Straight quotes cannot be told apart, so pair them in order and
            # ignore an unmatched trailing one rather than running to the end
            # of the book.
            positions = [m.start() for m in re.finditer(re.escape(opener), text)]
            for start, end in zip(positions[0::2], positions[1::2]):
                if not 2 < end - start < 2000:
                    continue
                # A SPAN MAY NOT CROSS A PARAGRAPH BREAK. Straight quotes are
                # identical opening and closing, so they can only be paired by
                # position - and in a book that mixes “ ” with " ", one
                # unmatched straight mark shifts every pair after it and the
                # "span" swallows the narration between two unrelated quotes.
                # That is not hypothetical: it marked `I was taken aback.` as
                # spoken in mushoku18 and inflated the misattribution rate.
                #
                # Dialogue does not span a blank line. Narration between two
                # quotes always does.
                if "\n\n" in text[start:end]:
                    continue
                spans.append((start + 1, end))
        else:
            for match in re.finditer(
                    re.escape(opener) + r"([^" + re.escape(closer) + r"]{2,2000})"
                    + re.escape(closer), text):
                spans.append((match.start(1), match.end(1)))
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


def spoken_spans(text, convention=None):
    """-> sorted, non-overlapping (start, end) offsets of spoken text."""
    convention = convention or detect_convention(text)
    if convention is None:
        return []
    if convention == "paired_quotes":
        spans = _paired_spans(text)
    elif convention == "dash_lines":
        spans = [(m.start(1), m.end(1)) for m in DASH_LINE.finditer(text)]
    else:
        spans = [(m.start(2), m.end(2)) for m in LABEL_LINE.finditer(text)]
    spans.sort()
    merged = []
    for start, end in spans:
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _normalize(value):
    return re.sub(r"\s+", " ", re.sub(r'["“”「」『』]',
                                      "", value or "")).strip()


def mark_entries(entries, source_text, convention=None):
    """-> a NEW list of entries carrying `spoken` and `source_span`.

    Entries are matched forward through the source, in order, because the same
    short line ("Oh.") occurs many times and the first match is rarely the
    right one. A line that cannot be located is left unmarked rather than
    guessed: `spoken` absent means "not established", which is a different
    claim from `spoken: false`.
    """
    spans = spoken_spans(source_text, convention)
    # Printed speaker labels, keyed by where their quote begins. Only for books
    # that use the convention as a rule: three stray matches in a book that
    # does not would attribute three lines from noise.
    labels = (dict(speaker_labels(source_text))
              if uses_speaker_labels(source_text) else {})
    marked, cursor = [], 0
    for entry in entries:
        row = dict(entry)
        needle = _normalize(str(entry.get("text", "")))[:120]
        if needle:
            where = source_text.find(needle, cursor)
            if where == -1:                      # the model rewrote it, or the
                where = source_text.find(needle)  # order slipped; try anywhere
            if where != -1:
                cursor = where + len(needle)
                end = where + len(needle)
                overlap = any(start < end and where < stop
                              for start, stop in spans)
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
    r'(?:^|\n)[ \t]*([A-Z][\w.\'’-]{1,20}(?:[ \t]+[A-Z][\w.\'’-]{1,20}){0,2})'
    r'[ \t]+(?=[“"「『])')


def speaker_labels(text, minimum_repeats=3):
    """-> [(quote_start, name)] for quotes introduced by a printed speaker name.

    A name must recur before it is believed. One capitalised word before a
    quote is a coincidence - "Suddenly “Get down!"" would qualify - but a token
    that introduces three or more quotes across a book is how that book prints
    its speakers. This is the cheapest possible attribution signal and it is
    exact where it applies, so it must not be extended by guessing: a book
    without the convention gets an empty list, not a weak one.
    """
    # Words that open a sentence far more often than they name a speaker.
    # "The" cleared the three-repeat bar in mushoku23 and would have claimed
    # three lines for a character called The.
    NOT_NAMES = {"The", "A", "An", "And", "But", "Then", "So", "He", "She",
                 "It", "They", "We", "I", "Suddenly", "After", "Before",
                 "When", "While", "As", "At", "In", "On", "With", "That",
                 "This", "There", "Here", "Now", "Just", "Even", "If"}
    hits = [(m.end(), " ".join(m.group(1).split())) for m in
            LABEL_BEFORE_QUOTE.finditer(text)]
    hits = [(pos, name) for pos, name in hits
            if name.split()[0] not in NOT_NAMES]
    counts = {}
    for _, name in hits:
        counts[name] = counts.get(name, 0) + 1
    return [(pos, name) for pos, name in hits
            if counts[name] >= minimum_repeats]


def uses_speaker_labels(text, threshold=0.3):
    """Does this book print the speaker before the line, as a rule?"""
    spans = spoken_spans(text)
    if not spans:
        return False
    labelled = speaker_labels(text)
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
