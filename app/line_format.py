"""Compact one-line-per-entry codec for annotated-script output.

WHY THIS EXISTS. generate_script's failure mode is truncation, not malformed
syntax: a chunk is rejected by MIN_SOURCE_TOKEN_RECALL when the model stops
covering the source, and every entry currently pays for a JSON wrapper -
{"speaker": ..., "text": ..., "instruct": ...} - around text it must reproduce
verbatim anyway. Those wrapper tokens buy nothing the delimiter does not.

FIELD ORDER IS LOAD-BEARING. `text` is last and parsing splits with maxsplit=2,
so a '|' inside the prose cannot corrupt the record - it simply stays in the
text where it belongs. Putting `text` anywhere but last would make the
delimiter a hazard rather than a separator.

STRICT BY DEFAULT. parse_entries raises on a malformed line. A parser that
quietly drops what it cannot read turns a visible generation failure into a
silently short script that still passes as a result - the failure class Rule 21
was written about. `salvage_entries` is the explicit lenient path, and it
reports what it skipped rather than swallowing it, mirroring the existing
salvage_json_entries in generate_script.
"""

DELIMITER = "|"
FIELDS = ("speaker", "instruct", "text")

# str.splitlines() also breaks on VT, FF, FS, GS, RS, NEL, LS and PS. Those are
# data inside `text`, not record breaks: a Gutenberg page break (\x0c) copied
# verbatim into a passage must not strand its tail on a pipe-less line. Only
# CR/LF frame records, and the producer escapes every other separator.
_ESCAPES = {
    "\n": "n", "\r": "r", "\v": "v", "\f": "f",
    "\x1c": "u001c", "\x1d": "u001d", "\x1e": "u001e",
    "\x85": "u0085", "\u2028": "u2028", "\u2029": "u2029",
}
_UNESCAPES = {token: char for char, token in _ESCAPES.items()}


def _split_records(text):
    """Split on CR/LF only, so other Unicode line separators stay in `text`.

    CRLF and bare CR remain record breaks (existing replies may use either),
    but FF, VT, NEL, LS, PS, FS, GS and RS do not - the producer escapes them
    and a model can emit them raw inside a passage it is copying.
    """
    return str(text).replace("\r\n", "\n").replace("\r", "\n").split("\n")


class LineFormatError(ValueError):
    """A line could not be read as an entry."""


def _escape(value):
    text = str(value if value is not None else "").replace("\\", "\\\\")
    for char, token in _ESCAPES.items():
        text = text.replace(char, "\\" + token)
    return text


def _unescape(value):
    out, i = [], 0
    while i < len(value):
        if value[i] == "\\" and i + 1 < len(value):
            if value[i + 1] == "\\":
                out.append("\\"); i += 2; continue
            token = next((t for t in _UNESCAPES if value.startswith(t, i + 1)), None)
            if token is not None:
                out.append(_UNESCAPES[token]); i += 1 + len(token); continue
        out.append(value[i]); i += 1
    return "".join(out)


def format_entries(entries):
    """Render entries as one `speaker|instruct|text` line each."""
    lines = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise LineFormatError("entry is not an object: %r" % (entry,))
        for field in FIELDS[:2]:
            if DELIMITER in _escape(entry.get(field)):
                raise LineFormatError("%s contains the record delimiter: %r" % (field, entry.get(field)))
        lines.append(DELIMITER.join(_escape(entry.get(f)) for f in FIELDS))
    return "\n".join(lines)


def _parse_line(line):
    parts = line.split(DELIMITER, 2)
    if len(parts) < 3:
        raise LineFormatError(
            "expected 'speaker%sinstruct%stext', got %r" % (DELIMITER, DELIMITER, line))
    speaker, instruct, text = (_unescape(p) for p in parts)
    if not speaker.strip():
        raise LineFormatError("entry has no speaker: %r" % (line,))
    return {"speaker": speaker.strip(), "text": text, "instruct": instruct.strip()}


def parse_entries(text):
    """Parse the compact format. Raises LineFormatError on any bad line."""
    return [_parse_line(line) for line in _split_records(text) if line.strip()]


def salvage_entries(text):
    """Lenient parse -> (entries, skipped) where skipped holds the bad lines.

    The caller decides what a partial read means; this never hides one.
    """
    entries, skipped = [], []
    for line in _split_records(text):
        if not line.strip():
            continue
        try:
            entries.append(_parse_line(line))
        except LineFormatError as exc:
            skipped.append({"line": line, "error": str(exc)})
    return entries, skipped
