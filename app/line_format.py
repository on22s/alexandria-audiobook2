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


class LineFormatError(ValueError):
    """A line could not be read as an entry."""


def _escape(value):
    return (str(value if value is not None else "")
            .replace("\\", "\\\\").replace("\n", "\\n").replace("\r", ""))


def _unescape(value):
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            if nxt == "n":
                out.append("\n"); i += 2; continue
            if nxt == "\\":
                out.append("\\"); i += 2; continue
        out.append(ch); i += 1
    return "".join(out)


def format_entries(entries):
    """Render entries as one `speaker|instruct|text` line each."""
    lines = []
    for entry in entries:
        if not isinstance(entry, dict):
            raise LineFormatError("entry is not an object: %r" % (entry,))
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
    return [_parse_line(line) for line in str(text).splitlines() if line.strip()]


def salvage_entries(text):
    """Lenient parse -> (entries, skipped) where skipped holds the bad lines.

    The caller decides what a partial read means; this never hides one.
    """
    entries, skipped = [], []
    for line in str(text).splitlines():
        if not line.strip():
            continue
        try:
            entries.append(_parse_line(line))
        except LineFormatError as exc:
            skipped.append({"line": line, "error": str(exc)})
    return entries, skipped
