"""Alternative ways of ASKING the attribution question, as entries_providers
for three_pass_generate.attribute_batch (issue #522 follow-up, GOALS 1.2).

Each variant changes only the user prompt the model sees; the output
contract ([{n, speaker}], the JSON schema, the index-head check and the
text freeze) is untouched, so the gates that make attribution safe run
exactly as in production. The ideas come from the reasoning-trace probe
(2026-09-14) and from Michel et al. 2025 (NAACL, "Evaluating LLMs for
Quotation Attribution"), whose prompt is the published state of the art on
PDNC: quotes marked in running text, a character list WITH aliases, a
three-step instruction, and the previous chunk's predictions as context.

- aliases:     roster line carries each character's aliases
               ("HARUHIRO (also: HARU)").
- passage:     the window is shown as running text with SPOKEN lines
               marked |n|"..."|n| instead of a JSON list of entries.
- incremental: the speakers decided for the previous window are shown
               first, so a scene that spans two windows keeps its cast.
- michel:      all three together.
"""
import json

from generate_script import call_llm_for_entries
from three_pass_generate import build_attribute_request

VARIANTS = ("default", "aliases", "passage", "incremental", "michel")

PASSAGE_INSTRUCTION = (
    "The passage below is continuous text from the book. Spoken lines are marked "
    "|n|\"...\"|n| with their index n; everything unmarked is narration and is "
    "never attributed.\n"
    "Step 1: reading in order, decide who speaks each marked line, using the "
    "narration around it, who is addressed, and what the line says.\n"
    "Step 2: match each speaker to a name on the roster (or NARRATOR / UNKNOWN).\n"
    "Step 3: return one {\"n\", \"speaker\"} object per input entry, including the "
    "narration entries (their speaker is NARRATOR), in index order. Do not repeat "
    "any text and do not explain.")


def roster_line(roster, alias_groups=None):
    """Roster with each name's aliases, from alias groups that mention it."""
    parts = []
    for name in roster:
        extra = []
        for group in alias_groups or []:
            if name in group:
                extra += [a for a in group if a != name and a not in extra]
        parts.append(f"{name} (also: {', '.join(sorted(extra))})" if extra else name)
    return ", ".join(parts) or "(none yet)"


def passage_text(frozen_batch, neighbor_contexts=None):
    """The batch as running prose. The harness sends only the SPOKEN lines and
    carries the narration in each entry's previous/next context, so those are
    interleaved here; without them the four variant arms of 2026-09-14 saw
    dialogue with no narration at all and scored 25% against 63%."""
    neighbor_contexts = neighbor_contexts or [{} for _ in frozen_batch]
    out, seen = [], set()

    def narration(ctx_entry):
        text = (ctx_entry or {}).get("text") if isinstance(ctx_entry, dict) else None
        if text and ctx_entry.get("type") == "NARRATOR" and text not in seen:
            seen.add(text)
            out.append(text)

    for i, e in enumerate(frozen_batch):
        ctx = neighbor_contexts[i] if i < len(neighbor_contexts) else {}
        narration(ctx.get("previous_context"))
        if e["type"] == "SPOKEN":
            out.append(f'|{i}|"{e["text"]}"|{i}|')
        else:
            out.append(f"[{i}] {e['text']}")
        narration(ctx.get("next_context"))
    return "\n\n".join(out)


def make_provider(variant, alias_groups=None):
    """-> an entries_provider for attribute_batch; keeps per-run memory for
    the incremental variant."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown prompt variant {variant!r}; expected one of {VARIANTS}")
    memory = {"previous": []}

    def provider(client, model_name, sys_prompt, user_prompt, params, log_name, label,
                 max_retries, validate_entries, attempt_observer, frozen_batch,
                 roster=None, neighbor_contexts=None, **_ignored):
        roster = list(roster or [])
        if variant in ("aliases", "michel"):
            roster_str = roster_line(roster, alias_groups)
        else:
            roster_str = ", ".join(roster) or "(none yet)"
        if variant in ("passage", "michel"):
            body = f"{PASSAGE_INSTRUCTION}\n\nROSTER: {roster_str}\n\nPASSAGE:\n{passage_text(frozen_batch, neighbor_contexts)}"
        else:
            _, canonical = build_attribute_request(frozen_batch, params, roster, neighbor_contexts)
            body = canonical.replace(f"ESTABLISHED ROSTER: {', '.join(roster) or '(none yet)'}",
                                     f"ESTABLISHED ROSTER: {roster_str}")
        if variant in ("incremental", "michel") and memory["previous"]:
            prev = "; ".join(f"{i}: {s}" for i, s in memory["previous"])
            body = ("Speakers already decided for the lines immediately before this passage "
                    f"(most recent last): {prev}\n\n") + body
        named = call_llm_for_entries(
            client, model_name, sys_prompt, body, params, log_name=log_name, label=label,
            max_retries=max_retries, validate_entries=validate_entries,
            attempt_observer=attempt_observer)
        if named:
            spoken = [(i, item.get("speaker")) for i, (f, item) in enumerate(zip(frozen_batch, named))
                      if f["type"] == "SPOKEN" and item.get("speaker")]
            memory["previous"] = spoken[-8:]
        return named

    provider.variant = variant
    return provider
