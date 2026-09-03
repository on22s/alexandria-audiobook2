"""How a model's reply becomes entries — one object per wire format.

WHY AN OBJECT AND NOT A BRANCH. call_llm_for_entries carries every retry rule
this project has paid for: length-escalation, near-miss recall, deterministic
repair, the trigram floor, the fingerprint that refuses a prompt the model has
already rejected. A second format must reuse all of it, so the format is
injected as four hooks rather than forked into a parallel pipeline that would
drift (Rule 15).

THE FOUR HOOKS, in the order call_llm_for_entries uses them:
  extract(text)       -> the payload region, or "" when the reply has none
  raw_fallback(text)  -> a looser payload after retries are exhausted, or None
  parse(payload)      -> entries, or [] if unreadable
  salvage(payload)    -> entries from a partial payload, last resort

Both codecs return [] rather than raising, because the caller treats an empty
list as "this attempt failed" and already knows what to do with it.
"""


class ResponseCodec:
    def __init__(self, name, extract, parse, salvage, raw_fallback):
        self.name = name
        self.extract = extract
        self.parse = parse
        self.salvage = salvage
        self.raw_fallback = raw_fallback


def _json_codec():
    from generate_script import (clean_json_string, repair_json_array,
                                 salvage_json_entries)

    def raw_fallback(text):
        # clean_json_string deliberately rejects ambiguous multi-array
        # structure; the raw array region can still hold complete objects and
        # is safe only because the quality gate must still accept it in full.
        start = text.find("[")
        return text[start:] if start != -1 else None

    return ResponseCodec("json", clean_json_string, repair_json_array,
                         salvage_json_entries, raw_fallback)


def _line_codec():
    import line_format

    def extract(text):
        """Keep the lines that look like records; drop fences and commentary.

        A model that prefixes prose must not fail the whole attempt when the
        records are present underneath it - the JSON path is equally forgiving
        about surrounding text.
        """
        kept = []
        for line in str(text).splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("```"):
                continue
            if stripped.count(line_format.DELIMITER) >= 2:
                kept.append(stripped)
        return "\n".join(kept)

    def parse(payload):
        entries, _ = line_format.salvage_entries(payload)
        return entries

    def salvage(payload):
        entries, _ = line_format.salvage_entries(payload)
        return entries

    return ResponseCodec("lines", extract, parse, salvage, lambda text: None)


def _freeform_codec():
    """Stage 1 of the two-step path: keep the reply, parse nothing.

    WHY A CODEC AND NOT A SECOND CALLER. Stage 1 still needs every retry rule
    call_llm_for_entries owns - length escalation, the repeat-prompt
    fingerprint, the attempt observer. Writing a bare chat call beside it would
    be a Rule 15 parallel copy that drifts. So stage 1 goes through the same
    function with a codec that wraps the whole reply in one sentinel entry;
    an empty reply yields [] and is retried exactly like unparseable JSON.
    """
    def extract(text):
        return str(text or "").strip()

    def parse(payload):
        return [{FREEFORM_KEY: payload}] if payload and payload.strip() else []

    return ResponseCodec("freeform", extract, parse, parse, lambda text: None)


FREEFORM_KEY = "_freeform"


def get_codec(name):
    if name == "json":
        return _json_codec()
    if name == "lines":
        return _line_codec()
    if name == "freeform":
        return _freeform_codec()
    raise ValueError("unknown output format: %r" % (name,))


LINE_HEADER = ("You are a script writer converting books into audiobook scripts "
               "for an advanced TTS system. Output ONLY one record per line "
               "using the pipe format below — no JSON, no markdown, no "
               "explanations.")

LINE_FORMAT_BLOCK = """FORMAT — one record per line, exactly two pipes, no header row:
NARRATOR|Quiet, tense narration.|The room had gone cold. Elena stood by the window, arms folded, watching the last light drain from the sky.
ELENA|Firm quiet authority, low and controlled, voice tight with restrained anger.|Tell me the truth.
MARCUS|Defensive evasion, flat and guarded, forcing calm he does not feel.|There is nothing to tell.
NARRATOR|Neutral, even narration.|Marcus could not meet her gaze. He had always been a poor liar, and they both knew it.

FIELDS, in this order, separated by "|":
- speaker: Character name in UPPERCASE. "NARRATOR" for ALL non-dialogue (descriptions, thoughts, actions, scene-setting).
- instruct: 1-2 sentence voice direction (~8-15 words) for the TTS engine.
- text: Exactly what the TTS voice says. It comes LAST, so a "|" inside it is harmless — never escape it, and never add a trailing pipe.
"""


class PromptShapeError(ValueError):
    """The shipped prompt no longer has the shape this transform rewrites."""


def build_line_format_prompt(system_prompt):
    """Rewrite only the format-spec region of the shipped system prompt.

    The RULES section is where the real instructions live and it is identical
    for both arms, so it is NOT duplicated into a second file - a copy would
    drift and the A/B would silently stop comparing like with like. This
    raises rather than returning a half-converted prompt: an arm that still
    says "Output ONLY valid JSON" while being parsed as lines would look like
    a format failure and would be recorded as one.
    """
    if not system_prompt:
        raise PromptShapeError("no system prompt to convert")
    start = system_prompt.find("FORMAT:")
    rules = system_prompt.find("RULES:")
    if start == -1 or rules == -1 or rules <= start:
        raise PromptShapeError(
            "expected 'FORMAT:' before 'RULES:' in the system prompt")
    head, tail = system_prompt[:start], system_prompt[rules:]
    if "JSON" not in head:
        raise PromptShapeError("expected the JSON instruction in the header")
    first_newline = head.find("\n")
    remainder = head[first_newline:] if first_newline != -1 else "\n"
    converted = LINE_HEADER + remainder + LINE_FORMAT_BLOCK + "\n" + tail
    if "valid JSON array" in converted:
        raise PromptShapeError("a JSON instruction survived the conversion")
    return converted
