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
                if 2 < end - start < 2000:
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
        marked.append(row)
    return marked
