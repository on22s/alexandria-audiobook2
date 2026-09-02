"""Do the hard part first, serialise it second.

THE IDEA. generate_script asks one call to segment prose, decide narration from
speech, name the speaker, write a voice direction AND emit valid JSON. Measured
2026-09-02: when Qwen3.8 fails here it fails at the wrapper, not the thinking -
81 of 93 unanswered rows are the model writing its own analysis where the JSON
belongs, and it reaches correct conclusions inside that prose. So split the
work: stage 1 annotates in whatever shape it likes, stage 2 turns that into
JSON and does nothing else.

WHAT THIS COSTS, STATED UP FRONT. Two calls per chunk instead of one, so it
must buy more than it spends. It also moves the risk rather than removing it:
stage 1 must still reproduce the source text, and stage 2 can now drop entries
while converting. The quality gate still scores the FINAL entries against the
ORIGINAL chunk, so neither failure can hide - but a chunk can now fail for a
reason that has nothing to do with the annotation being wrong.

STAGE 2 NEVER SEES THE SOURCE. It is given only stage 1's output, which keeps
it a serialisation task; handing it the chunk as well would let it quietly
re-do stage 1's job and the arms would stop being comparable.
"""

FREEFORM_BLOCK = """FORMAT — write the script plainly, one segment per paragraph, no JSON:

  SPEAKER: NARRATOR
  DIRECTION: Quiet, tense narration.
  TEXT: The room had gone cold. Elena stood by the window, arms folded.

  SPEAKER: ELENA
  DIRECTION: Firm quiet authority, low and controlled.
  TEXT: Tell me the truth.

Use exactly those three labels for every segment, in that order. Do not number
the segments. Do not add commentary before or after. Reproduce the author's
text verbatim in TEXT.
"""

CONVERSION_SYSTEM = (
    "You convert an already-annotated audiobook script into JSON. You are not "
    "editing it. Output ONLY a valid JSON array — no markdown, no explanation.\n"
    "\n"
    "Each segment becomes one object: {\"speaker\": ..., \"text\": ..., "
    "\"instruct\": ...}, taking speaker from SPEAKER, text from TEXT and "
    "instruct from DIRECTION.\n"
    "\n"
    "RULES:\n"
    "1. Copy every segment. Do not merge, split, reorder or drop any.\n"
    "2. Copy TEXT character for character. Do not fix, shorten or rephrase it.\n"
    "3. Keep speaker names exactly as given, in UPPERCASE.\n"
    "4. If a segment is malformed, still emit it with your best reading of its "
    "three fields rather than skipping it.\n"
)


class PromptShapeError(ValueError):
    """The shipped prompt no longer has the shape this transform rewrites."""


def build_freeform_prompt(system_prompt):
    """Stage 1's system prompt: the shipped RULES, a non-JSON output spec.

    Only the FORMAT/FIELDS region is replaced, exactly as the lines arm does,
    so the two arms differ in output shape and nothing else. Raises rather than
    returning a half-converted prompt - a stage-1 prompt still demanding JSON
    would make the two-step arm secretly a one-step arm.
    """
    if not system_prompt:
        raise PromptShapeError("no system prompt to convert")
    start = system_prompt.find("FORMAT:")
    rules = system_prompt.find("RULES:")
    if start == -1 or rules == -1 or rules <= start:
        raise PromptShapeError("expected 'FORMAT:' before 'RULES:'")
    head, tail = system_prompt[:start], system_prompt[rules:]
    first_newline = head.find("\n")
    remainder = head[first_newline:] if first_newline != -1 else "\n"
    header = ("You are a script writer converting books into audiobook scripts "
              "for an advanced TTS system. Work carefully; do not worry about "
              "machine-readable formatting.")
    converted = header + remainder + FREEFORM_BLOCK + "\n" + tail
    if "valid JSON array" in converted:
        raise PromptShapeError("a JSON instruction survived the conversion")
    return converted


def build_conversion_prompt(freeform_text):
    """Stage 2's user prompt: stage 1's annotation, nothing else."""
    if not freeform_text or not freeform_text.strip():
        raise PromptShapeError("stage 1 produced nothing to convert")
    return ("Convert this annotated script into the JSON array described in "
            "your instructions.\n\n" + freeform_text.strip())
