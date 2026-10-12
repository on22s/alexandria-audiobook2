"""Compact Pass 1 (#744): numbered source units in, [{n, type}] labels out.

Opt-in (`pass1_format: "compact"`, default "ordinary"). This is the configuration
the #744 campaign evaluated (experiment branch exp/compact-pass1-20261004,
`make_compact_segmenter`): chunks the quote parser settles are untouched; every
other chunk gets ONE compact request at the Pass 1 temperature, its labels are
rebuilt into ordinary {type, text} entries, and the shipped segment gate must
accept them. Any problem returns [] so the caller falls back to the ordinary
Pass 1 path for that chunk - nothing is defaulted silently.

`split_units`, `build_compact_prompt`, `parse_compact` and `entries_from_labels`
are copied unchanged from the experiment so production matches what was
measured. The request itself goes through generate_script.call_llm_for_entries
(Rule 15: one call path - logging, budgets, attempt telemetry, transports).
"""
import json
import re
from dataclasses import replace

# The evaluated harness capped compact output at 4,096 tokens; a label per
# unit needs far less, so the cap only stops a runaway reply.
COMPACT_MAX_TOKENS = 4096

COMPACT_SYSTEM = (
    "You label sentences of a book for a TTS system. Output ONLY a valid JSON array, no markdown, no explanation.\n"
    "Each input line is `N<number>: <text>`. For EVERY line return {\"n\": <number>, \"type\": \"SPOKEN\" or \"NARRATOR\"}.\n"
    "- SPOKEN: the line is words a character says aloud (inside quotation marks).\n"
    "- NARRATOR: everything else (description, action, thought, attribution such as 'he said').\n"
    "A line that mixes both: label it by the larger share of its words. Return one element per line, in order, no text.")


def split_units(chunk):
    """Chunk -> ordered units: paragraph breaks always cut; a sentence end cuts only outside a quote
    (including the sentence end that sits just inside a closing quote mark). Quote marks stay in the unit text."""
    terminators, closers = ".!?\u2026", "\u201d\"\u2019')]"
    units = []
    for para in re.split(r"\n\s*\n", chunk):
        para = para.strip()
        if not para:
            continue
        depth, start = 0, 0
        for i, ch in enumerate(para):
            if ch == "\u201c":
                depth += 1
            elif ch == "\u201d":
                depth = max(0, depth - 1)
            elif ch == '"':
                depth = 0 if depth else 1
            nxt = para[i + 1] if i + 1 < len(para) else ""
            ends_sentence = depth == 0 and nxt.isspace() and (
                ch in terminators or (ch in closers and i > 0 and para[i - 1] in terminators + closers))
            if ends_sentence:
                unit = para[start:i + 1].strip()
                if unit:
                    units.append(unit)
                start = i + 1
        tail = para[start:].strip()
        if tail:
            units.append(tail)
    return units


def norm(t):
    return re.sub(r"\W+", "", t or "").lower()


def is_mixed_unit(unit, analyze):
    """True when the unit holds both quoted and unquoted words (a 2-label scheme cannot represent it)."""
    return len({r["type"] for r in analyze(unit)["regions"]}) > 1


def build_compact_prompt(units):
    body = "\n".join(f"N{i}: {u}" for i, u in enumerate(units, 1))
    return COMPACT_SYSTEM, "Label every line.\n\n" + body


def parse_compact(content, count):
    """-> (types list of length count with None for missing, problems). Tolerant of think blocks and fences."""
    text = re.sub(r"<think>.*?</think>", "", content or "", flags=re.S).strip()
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("["), text.rfind("]")
    types, problems = [None] * count, []
    if start < 0 or end <= start:
        return types, ["no_json_array"]
    try:
        rows = json.loads(text[start:end + 1])
    except ValueError:
        return types, ["bad_json"]
    for row in rows:
        try:
            n, t = int(row["n"]), str(row["type"]).upper()
        except (KeyError, TypeError, ValueError):
            problems.append("bad_row"); continue
        if t not in ("SPOKEN", "NARRATOR") or not 1 <= n <= count:
            problems.append("bad_label_or_n"); continue
        if types[n - 1] is not None:
            problems.append("duplicate_n"); continue
        types[n - 1] = t
    if any(t is None for t in types):
        problems.append("missing_units")
    return types, problems


def entries_from_labels(units, types, analyze):
    """Model labels per unit -> [{type,text}] with quote delimiters removed, like the shipped Pass 1 output.
    A unit that mixes quoted and unquoted words keeps the quote parser's own regions (the model gave one label for two kinds of text)."""
    entries = []
    for unit, label in zip(units, types):
        regions = analyze(unit)["regions"]
        if not regions:
            entries.append({"type": label, "text": unit.strip()})
        elif len({r["type"] for r in regions}) > 1:
            entries.extend({"type": r["type"], "text": r["text"]} for r in regions)
        else:
            entries.append({"type": label, "text": " ".join(r["text"] for r in regions)})
    merged = []
    for e in entries:
        if merged and merged[-1]["type"] == e["type"] == "NARRATOR":
            merged[-1]["text"] += " " + e["text"]
        else:
            merged.append(e)
    return merged


def get_compact_entries(rows, units, analyze):
    """Model rows -> {entries, unresolved}, for call_llm_for_entries' transform hook.
    Re-checks the rows with the evaluated parse_compact so the accept/reject
    rules are exactly the experiment's."""
    types, problems = parse_compact(json.dumps(rows, ensure_ascii=False), len(units))
    if problems:
        return {"entries": [], "unresolved": problems}
    return {"entries": entries_from_labels(units, types, analyze), "unresolved": []}


def segment_chunk_compact(client, model_name, chunk, params, attempt_sink=None):
    """One compact attempt over a chunk the quote parser did not settle.

    Returns accepted [{type, text}] entries, or [] so the caller falls back to
    the ordinary path. Single attempt (max_retries=0), as evaluated."""
    from generate_script import call_llm_for_entries
    from pass_quality import analyze_outer_quote_regions
    import three_pass_generate as TPG

    units = split_units(chunk)
    if not units:
        return []
    sys_prompt, user_prompt = build_compact_prompt(units)
    ceiling = TPG.resolve_completion_ceiling(
        max(1, len(chunk.split())), params, reasoning_allowance=params.reasoning_allowance)
    cap = min(params.max_tokens, ceiling, COMPACT_MAX_TOKENS)
    bounded = replace(
        params, max_tokens=cap, hard_max_tokens=min(params.hard_max_tokens, cap),
        request_output_ceiling=min(params.request_output_ceiling or cap, cap),
        temperature=(params.segment_temperature
                     if params.segment_temperature is not None else params.temperature))

    def observe(attempt):
        attempt["pass"] = "segment_compact"
        if attempt_sink is not None:
            attempt_sink.append(attempt)

    return call_llm_for_entries(
        client, model_name, sys_prompt, user_prompt, bounded,
        log_name="llm_responses.log", label="SEGMENT_COMPACT", max_retries=0,
        transform_entries=lambda rows: get_compact_entries(rows, units, analyze_outer_quote_regions),
        validate_entries=lambda entries: TPG.validate_segment_for_params(chunk, entries, params),
        attempt_observer=observe) or []
