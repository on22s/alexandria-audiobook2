"""Explicit terminal-pause authoring syntax; ordinary text stays literal."""
import re

from export_limits import MAX_PAUSE_MS


FORMAT = "aa2-terminal-pause-v1"
RECEIPT_KEY = "narration_markup"
MAX_TEXT = 32768


class NarrationMarkupError(ValueError):
    def __init__(self, message, offset):
        self.offset = offset
        super().__init__(f"{message} (character {offset + 1}).")


def get_narration_markup_preview(text):
    if not isinstance(text, str) or len(text) > MAX_TEXT:
        raise NarrationMarkupError("Use a saved line of at most 32,768 characters", 0)
    spans, output = [], []
    cursor = start = 0
    pause = None
    while cursor < len(text):
        escaped = text.startswith(r"\[[", cursor)
        directive = text.startswith("[[", cursor)
        if not escaped and not directive:
            cursor += 1
            continue
        if start < cursor:
            segment = text[start:cursor]
            spans.append({"kind": "spoken", "start": start, "end": cursor, "text": segment})
            output.append(segment)
        if escaped:
            spans.append({"kind": "escape", "start": cursor, "end": cursor + 3, "text": "[["})
            output.append("[[")
            cursor += 3
            start = cursor
            continue
        end = text.find("]]", cursor + 2)
        if end < 0:
            raise NarrationMarkupError("Close the directive with ]] or escape literal [[ with a backslash", cursor)
        content = text[cursor + 2:end]
        match = re.fullmatch(r"pause:([0-9]{1,6})ms", content)
        if not match:
            raise NarrationMarkupError("Use [[pause:250ms]]; voice, nested and unknown directives are unsupported. Escape literal [[ with a backslash", cursor)
        duration = int(match.group(1))
        if duration > MAX_PAUSE_MS:
            raise NarrationMarkupError(f"Pause exceeds the {MAX_PAUSE_MS} ms limit", cursor)
        if text[end + 2:].strip():
            raise NarrationMarkupError("A pause must end the saved line. Split the line explicitly in Editor first", cursor)
        if not "".join(output).strip():
            raise NarrationMarkupError("A pause must follow spoken text", cursor)
        pause = duration
        spans.append({"kind": "pause", "start": cursor, "end": end + 2, "milliseconds": duration})
        cursor = end + 2
        start = cursor
    if start < len(text):
        segment = text[start:]
        spans.append({"kind": "spoken", "start": start, "end": len(text), "text": segment})
        output.append(segment)
    spoken = "".join(output)
    return {"format": FORMAT, "original_text": text, "spoken_text": spoken,
            "pause_after": pause, "spans": spans, "changed": spoken != text}
