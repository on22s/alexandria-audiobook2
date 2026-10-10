"""Alternative ways of ASKING the attribution question, as entries_providers
for three_pass_generate.attribute_batch (issue #522 follow-up, GOALS 1.2).

Selectable in the product: Setup -> Script generation -> "Attribution prompt"
(generation.three_pass_attribute_prompt_variant, or --prompt-variant). Each
variant's measured result on the four clean-gold books is in RECIPES.md
("Prompt"); `default` is the shipped prompt every adapter was trained on.

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
- judge:       the canonical request plus a `why` per spoken line - one
               sentence naming the evidence - so a strong model labelling a
               book for review checks itself and leaves a reason a human can
               audit. `speaker` is what the pipeline keeps; every
               {text, speaker, why} also goes to $JUDGE_WHY_PATH (JSON lines).
- continuity:  rolling story context (XinchaoGou/alexandria-audiobook's
               serial-novel pipeline): a running summary of everything
               before this window, rewritten by the model after each window
               (one extra short call), plus the previous window's last ten
               lines with the speakers decided for them. What a reader of a
               long series carries between chapters; the harness otherwise
               starts every window cold.
- michel2:     michel with what the shipped prompt and the continuity probe
               each measured as helping: a SYSTEM prompt written for the
               passage format (the shipped one describes a JSON entry list
               the model is not being shown) that keeps the minor-speaker
               rule (+6.5/+9.6, GOALS 1.2), the listener rule and UNKNOWN;
               and the previous window carried as its last lines WITH their
               speakers (the continuity tail, +8.1/+3.2 on Re:Zero) instead
               of bare index pairs whose indices restart every window.
- michel2_full: michel2 shown the whole window - every narration entry in
               order, not just each line's +-1 neighbours - with the
               segmented text before and after it as evidence. Michel et al.
               attribute inside 4,096-token chunks of the complete text; the
               context-size ablations of 2025-26 (ModernBERT 500->2000
               tokens, WNU 2025 per-line context plateauing at 512-1024) all
               point the same way. Needs `surround` from the caller.
- michel2_shot: michel2 with one worked example passage and its answer
               before the roster (WNU 2025's prompt shape; 6-shot beat CoT
               and zero-shot in the ChatGPT study).
"""
import json
from copy import deepcopy

from generate_script import call_llm_for_entries
from default_prompts import load_attribute_prompts
from three_pass_generate import build_attribute_request, ATTRIBUTION_RESPONSE_SCHEMA

VARIANTS = ("default", "aliases", "passage", "incremental", "michel", "continuity", "judge", "michel2", "michel2_full", "michel2_shot")

PASSAGE_INSTRUCTION = (
    "The passage below is continuous text from the book. Spoken lines are marked "
    "|n|\"...\"|n| with their index n; everything unmarked is read-only context and is "
    "never attributed.\n"
    "Step 1: reading in order, decide who speaks each marked line, using the "
    "narration around it, who is addressed, and what the line says.\n"
    "Step 2: match each speaker to a name on the roster (or NARRATOR / UNKNOWN).\n"
    "Step 3: return one {\"n\", \"speaker\"} object per input entry, including the "
    "narration entries (their speaker is NARRATOR), in index order. Do not repeat "
    "any text and do not explain.")


MICHEL2_SYSTEM = (
    "You label EVERY marked entry of a novel passage with who speaks it, for a TTS "
    "system: narration entries, marked [n], are always \"NARRATOR\"; spoken lines, "
    "marked |n|\"...\"|n|, get the UPPERCASE roster name of whoever says them. "
    "Output ONLY a valid JSON array - no markdown, no explanations.\n\n"
    "You receive a ROSTER (each character, with the other names the text uses for them "
    "in parentheses) and a PASSAGE of continuous text in which every entry is marked with "
    "its index: spoken lines as |n|\"...\"|n|, narration entries as [n] ...; unmarked text "
    "is surrounding speech or narration shown only as evidence. Return one {\"n\", \"speaker\"} "
    "object per marked entry, in index order, echoing n unchanged as a JSON integer "
    "(for example {\"n\": 0, \"speaker\": \"NARRATOR\"}); narration entries "
    "get exactly \"NARRATOR\"; spoken lines get the UPPERCASE roster name of whoever "
    "says them.\n\n"
    "HOW TO DECIDE A SPOKEN LINE, in this order:\n"
    "1. A speech tag names the speaker: \"...\" said X / X asked / X's voice. A tag in "
    "the narration right before or right after a line belongs to that line.\n"
    "2. A name inside the line is usually the LISTENER, not the speaker (\"Yes, Ranta.\" "
    "is said TO Ranta). Pick someone else who is present.\n"
    "3. With no tag, use what the line says: who knows this, wants this, talks like this, "
    "or is answering the previous line.\n"
    "4. Do not assume speakers strictly alternate; the same person often continues.\n"
    "5. Do not default to the story's main characters. Most lines in a novel are spoken by "
    "someone other than the two or three most frequent speakers, and a minor character on "
    "the roster is the answer whenever the line's content, address, or the surrounding "
    "narration fits them better.\n"
    "6. Use a roster name (any of its listed forms means the same character; answer with "
    "the main form). Use a name not on the roster only for a character the roster is missing.\n"
    "7. If the speaker is genuinely unknowable, use \"UNKNOWN\". A wrong name is worse than "
    "UNKNOWN.\n\n"
    "RULES: exactly one object per marked entry - narration entries included - every "
    "index once, nothing added or dropped; do not repeat any text.")

MICHEL2_EXAMPLE = (
    "EXAMPLE (a different book):\n"
    "ROSTER: MARA (also: MISS ELLIS), TOM, THE INNKEEPER\n"
    "PASSAGE:\n"
    "The innkeeper set down two cups without a word.\n\n"
    "|0|\"You're late, Tom.\"|0|\n\n"
    "|1|\"The bridge was out.\"|1|\n\n"
    "He would not meet her eye.\n\n"
    "[2] Mara said nothing for a while.\n\n"
    "|3|\"That will be a shilling for the room.\"|3|\n"
    "ANSWER: [{\"n\": 0, \"speaker\": \"MARA\"}, {\"n\": 1, \"speaker\": \"TOM\"}, "
    "{\"n\": 2, \"speaker\": \"NARRATOR\"}, {\"n\": 3, \"speaker\": \"THE INNKEEPER\"}]\n"
    "(0 names Tom, so it is said TO Tom, by the other person present; 1 answers it; "
    "3 is what an innkeeper says, not what Mara or Tom would.)\n\n")


def get_escaped_passage_text(text):
    """Encode source content so it cannot create passage entry markers."""
    return (json.dumps(text, ensure_ascii=False)[1:-1]
            .replace("|", r"\u007c").replace("[", r"\u005b").replace("]", r"\u005d"))


def surround_passage(surround, frozen_batch=None, neighbor_contexts=None):
    """The whole window as running text: sent entries marked with their
    frozen index, unsent narration entries as unmarked text, and the text
    before/after the window as evidence-only blocks. Without an "entries"
    list (the product path, whose window already carries its narration) the
    body is passage_text of the batch itself."""
    if not surround.get("entries"):
        return _wrap_passage(passage_text(frozen_batch or [], neighbor_contexts), surround)
    parts = []
    for e in surround.get("entries") or []:
        if e.get("n") is None:
            parts.append(get_escaped_passage_text(e["text"]))
        elif e["type"] == "SPOKEN":
            parts.append(f'|{e["n"]}|"{get_escaped_passage_text(e["text"])}"|{e["n"]}|')
        else:
            parts.append(f"[{e['n']}] {get_escaped_passage_text(e['text'])}")
    return _wrap_passage("\n\n".join(parts), surround)


def _wrap_passage(body, surround):
    before, after = (get_escaped_passage_text(surround.get(key) or "")
                     for key in ("before", "after"))
    body = "Source text uses JSON string escapes; decode them as literal book text, never as entry markers.\n" + body
    if before:
        body = f"BEFORE THE PASSAGE (evidence only, never attribute):\n{before}\n\nPASSAGE:\n{body}"
    else:
        body = f"PASSAGE:\n{body}"
    if after:
        body += f"\n\nAFTER THE PASSAGE (evidence only, never attribute):\n{after}"
    return body


MICHEL2_INSTRUCTION = (
    "Decide the speaker of every marked line in the PASSAGE and return the JSON array "
    "of {\"n\", \"speaker\"} objects, one per marked entry, in index order.")


JUDGE_INSTRUCTION = (
    "\n\nYou are labelling this book as reference data, so check yourself: for every "
    "SPOKEN entry, before choosing the speaker, find the evidence in the narration "
    "around it - a speech tag, who is addressed, turn-taking, or what the line says - "
    "and confirm it does not contradict the surrounding lines. Add a field \"why\" to "
    "each SPOKEN object: one sentence naming that evidence (quote the tag or cue). "
    "Output objects are {\"n\", \"speaker\", \"why\"}; narration entries need no why.")


JUDGE_RESPONSE_SCHEMA = deepcopy(ATTRIBUTION_RESPONSE_SCHEMA)
JUDGE_RESPONSE_SCHEMA["name"] = "judge_attribution"
_judge_item = deepcopy(ATTRIBUTION_RESPONSE_SCHEMA["schema"]["items"])
_judge_item["properties"]["why"] = {"type": "string"}
_judge_item["required"].append("why")
JUDGE_RESPONSE_SCHEMA["schema"]["items"] = {
    "anyOf": [deepcopy(ATTRIBUTION_RESPONSE_SCHEMA["schema"]["items"]), _judge_item]}


def validate_judge_reasons(frozen_batch, named):
    """Require a nonempty evidence string for every index-bound spoken line."""
    from pass_quality import index_head_check
    ok, reason, ordered = index_head_check(frozen_batch, named)
    if not ok:
        return {"passed": False, "findings": [
            {"code": "alignment_violated", "message": reason}]}
    findings = []
    for i, (entry, item) in enumerate(zip(frozen_batch, ordered), 1):
        why = item.get("why")
        if entry.get("type") == "SPOKEN" and (not isinstance(why, str) or not why.strip()):
            findings.append({"code": "missing_judge_reason", "entry_number": i,
                             "message": "Every SPOKEN object needs a nonempty why naming its evidence."})
    return {"passed": not findings, "findings": findings}


def record_judge_reasons(frozen_batch, named, run_id=None):
    """Record accepted spoken evidence with its run identity."""
    import os
    path = os.environ.get("JUDGE_WHY_PATH")
    if not path or not named:
        return
    report = validate_judge_reasons(frozen_batch, named)
    if not report["passed"]:
        print(f"WARNING: judge reasons not recorded: {report['findings']}", flush=True)
        return
    from pass_quality import index_head_check
    ordered = index_head_check(frozen_batch, named)[2]
    from judge_reason_log import record_judge_rows
    rows = []
    for i, entry in enumerate(frozen_batch):
        if entry.get("type") != "SPOKEN":
            continue
        item = ordered[i]
        rows.append({"text": entry["text"], "speaker": item.get("speaker"),
                     "why": item.get("why")})
    record_judge_rows(rows, run_id)


SUMMARY_INSTRUCTION = (
    "You keep a running summary of a novel for someone who must know who is "
    "present and speaking. Rewrite the summary below to cover the new passage "
    "as well: who is in the scene, where they are, what just happened, and any "
    "name or title a character is called by. At most 150 words, plain prose, "
    "no headings.")
SUMMARY_MAX_TOKENS = 400
TAIL_LINES = 10


def rolling_summary(client, model_name, params, previous_summary, passage):
    """-> the summary rewritten to include `passage`; the old one on failure.

    One short call with reasoning off (the summary is not the hard part);
    the passage is what the model was shown for the window, narration
    included, so it can name whoever was present."""
    from lmstudio_settings import get_effective_max_tokens
    body = (f"{SUMMARY_INSTRUCTION}\n\nSUMMARY SO FAR:\n{previous_summary or '(start of the book)'}"
            f"\n\nNEW PASSAGE:\n{passage}")
    messages = [{"role": "user", "content": body}]
    try:
        max_tokens = get_effective_max_tokens(
            SUMMARY_MAX_TOKENS, params.context_length, messages,
            SUMMARY_MAX_TOKENS, scale_to_context=False)
        response = client.chat.completions.create(
            model=model_name, temperature=0, max_tokens=max_tokens,
            extra_body={"reasoning_effort": "none"}, messages=messages)
        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        if (not text or len(text.split()) > 150 or
                len(text) > SUMMARY_MAX_TOKENS * 3 or
                getattr(choice, "finish_reason", None) == "length"):
            print("WARNING: continuity summary rejected: empty, oversized or truncated response; "
                  "retaining the previous summary", flush=True)
            return previous_summary
    except Exception as exc:
        print(f"WARNING: continuity summary unavailable: {exc}; retaining the previous summary",
              flush=True)
        return previous_summary
    return text


def roster_line(roster, alias_groups=None, roster_traits=None):
    """Roster with each name's aliases, from alias groups that mention it, and
    - only when per-line traits are on - the [gender, age group, ageless]
    earlier passages established (speaker_traits.get_established_traits), so a
    later batch keeps a character's voice age instead of re-deciding it."""
    parts = []
    for name in roster:
        extra = []
        for group in alias_groups or []:
            if name in group:
                extra += [a for a in group if a != name and a not in extra]
        part = f"{name} (also: {', '.join(sorted(extra))})" if extra else name
        traits = (roster_traits or {}).get(str(name).strip().upper())
        parts.append(f"{part} [{traits}]" if traits else part)
    return ", ".join(parts) or "(none yet)"


def passage_text(frozen_batch, neighbor_contexts=None):
    """The marked batch as prose, with omitted neighbors as unmarked evidence."""
    neighbor_contexts = neighbor_contexts or [{} for _ in frozen_batch]
    out = []

    def context(ctx_entry):
        text = (ctx_entry or {}).get("text") if isinstance(ctx_entry, dict) else None
        if text:
            escaped = get_escaped_passage_text(text)
            if ctx_entry.get("type") == "SPOKEN":
                escaped = f'"{escaped}"'
            if not out or out[-1] != escaped:
                out.append(escaped)

    for i, e in enumerate(frozen_batch):
        ctx = neighbor_contexts[i] if i < len(neighbor_contexts) else {}
        context(ctx.get("previous_context"))
        if e["type"] == "SPOKEN":
            out.append(f'|{i}|"{get_escaped_passage_text(e["text"])}"|{i}|')
        else:
            out.append(f"[{i}] {get_escaped_passage_text(e['text'])}")
        context(ctx.get("next_context"))
    return "\n\n".join(out)


PASSAGE_SHAPED = ("passage", "michel")
USER_VARIANTS = tuple(v for v in VARIANTS if v != "judge")

# The michel2 rule 7, and the opt-in replacement (#653, Setup switch
# generation.three_pass_group_rule). Measured on a light-novel chapter whose
# cast list named THE STUDENTS: without it 5 of 10 runs answered UNKNOWN for
# every crowd line (32/70 correct); with it 70/70. On five PDNC novels named
# accuracy moved -0.1 pt and unnamed individuals -0.6 pt, past the 0.5 cap the
# gate allowed though inside the 1.8 pt run-to-run spread - so it is opt-in.
UNKNOWN_RULE_7 = ('7. If the speaker is genuinely unknowable, use "UNKNOWN". A wrong name is '
                  'worse than UNKNOWN.')
GROUP_RULE_7 = ('7. A line said by an unnamed member of a group on the roster (for example THE '
                'STUDENTS, THE CROWD, THE GUARDS) takes that group\'s name. Use "UNKNOWN" only '
                'when no listed person or group fits. A wrong name is worse than UNKNOWN.')


def get_group_rule_system(system_text):
    """-> (text, applied): `system_text` with rule 7 replaced by the group rule.
    A text without the exact rule 7 (another variant, a user's own edit) comes
    back unchanged with applied=False, never half-edited."""
    if (system_text or "").count(UNKNOWN_RULE_7) != 1:
        return system_text, False
    return system_text.replace(UNKNOWN_RULE_7, GROUP_RULE_7), True   # judge is gold labelling, harness only


def builtin_texts(variant):
    """The texts a variant sends, as a user-editable preset would carry them:
    {"system", "user", "example"}. For `default` the user text is the
    template with its {roster}/{batch} placeholders; for the passage-shaped
    variants it is the instruction block the pipeline wraps ROSTER/PASSAGE
    around; the canonical-request variants (aliases/incremental/continuity)
    use the default template. `example` is only non-empty for michel2_shot."""
    system, template = load_attribute_prompts()
    if variant.startswith("michel2"):
        return {"system": MICHEL2_SYSTEM, "user": MICHEL2_INSTRUCTION,
                "example": MICHEL2_EXAMPLE if variant == "michel2_shot" else ""}
    if variant in PASSAGE_SHAPED:
        return {"system": system, "user": PASSAGE_INSTRUCTION, "example": ""}
    return {"system": system, "user": template, "example": ""}


VARIANT_DESCRIPTIONS = {
    "default": "the standard prompt (use this with any trained adapter)",
    "michel": "lines shown as running text with nicknames listed and the previous request's answers (Michel et al. 2025)",
    "michel2": "michel plus this project's own rules (e.g. don't default to the main characters)",
    "michel2_full": "michel2 with extra surrounding text (set \"Step 2: extra text around each request\")",
    "michel2_shot": "michel2 with a worked example shown first",
    "continuity": "keeps a running summary of the story so far (one extra request per batch of lines)",
    "aliases": "default, plus character nicknames listed",
    "passage": "default, but lines shown as running text",
    "incremental": "default, plus the previous request's answers",
}


def builtin_presets():
    """One builtin preset per user-selectable variant, named as RECIPES names
    it, carrying the texts the variant actually sends."""
    presets = []
    for variant in USER_VARIANTS:
        texts = builtin_texts(variant)
        presets.append({"name": variant, "description": VARIANT_DESCRIPTIONS[variant],
                        "variant": variant, "system_prompt": texts["system"],
                        "user_prompt": texts["user"], "example": texts["example"],
                        "builtin": True})
    return presets


def resolve_attribution_preset(config):
    """-> (variant, texts, preset_name) for the run: the preset named by
    prompts.attribution_preset among the user's presets, else the builtin of
    that name, else the builtin for generation.three_pass_attribute_prompt_variant.
    `texts` is None when the preset is a builtin (send exactly the builtin)."""
    prompts = config.get("prompts") or {}
    name = prompts.get("attribution_preset")
    if not name:
        # a config from before presets carried the variant (#581) still says
        # which one it meant through the generation key
        name = (config.get("generation") or {}).get("three_pass_attribute_prompt_variant") or "michel2_full"
    for preset in config.get("prompt_presets") or []:
        if isinstance(preset, dict) and preset.get("name") == name and not preset.get("builtin"):
            variant = preset.get("variant") or "default"
            if variant not in USER_VARIANTS:
                raise ValueError(f"preset {name!r} names unknown variant {variant!r}")
            return variant, {"system": preset.get("system_prompt") or "",
                             "user": preset.get("user_prompt") or "",
                             "example": preset.get("example") or ""}, name
    if name in USER_VARIANTS:
        return name, None, name
    variant = (config.get("generation") or {}).get("three_pass_attribute_prompt_variant") or "michel2_full"
    return variant, None, variant


def validate_preset_texts(variant, texts):
    """-> an error message, or None. The default shape's user text is a
    template the pipeline fills; without both placeholders the model would
    never see the roster or the entries."""
    if variant == "default" or variant in ("aliases", "incremental", "continuity"):
        user = (texts or {}).get("user") or ""
        if user.strip() and ("{roster}" not in user or "{batch}" not in user):
            return ("For the default-shaped prompts the user text is a template and must keep "
                    "the {roster} and {batch} placeholders - that is where the pipeline puts "
                    "the character list and the entries.")
    return None


def _texts_for(variant, texts):
    base = builtin_texts(variant)
    for key, value in (texts or {}).items():
        if key in base and isinstance(value, str) and value.strip():
            base[key] = value
    return base


def build_variant_request(variant, frozen_batch, params, roster, alias_groups=None,
                          neighbor_contexts=None, surround=None, memory=None, texts=None,
                          roster_traits=None):
    """-> (system_prompt, user_body) for one window under `variant`, with the
    preset `texts` ({"system", "user", "example"}; None = the variant's
    builtin) in place. Pure: this is what the provider sends and what the
    Setup preview shows, one implementation."""
    from dataclasses import replace
    roster = list(roster or [])
    memory = memory or {"previous": [], "summary": "", "tail": []}
    t = _texts_for(variant, texts)
    michel2_family = variant.startswith("michel2")
    if variant in ("aliases", "michel") or michel2_family:
        roster_str = roster_line(roster, alias_groups, roster_traits)
    else:
        roster_str = ", ".join(roster) or "(none yet)"
    sys_prompt = t["system"]
    if michel2_family:
        tail = "\n".join(f'{spk}: "{get_escaped_passage_text(text)}"' for spk, text in memory["tail"])
        if variant == "michel2_full":
            if not surround:
                raise ValueError("michel2_full needs the caller to pass surround=")
            passage = surround_passage(surround, frozen_batch, neighbor_contexts)
        else:
            # the surrounding-text knob applies to every variant; in the
            # product, michel2 with it on is michel2_full
            passage = _wrap_passage(passage_text(frozen_batch, neighbor_contexts), surround or {})
        body = ((t["example"] if variant == "michel2_shot" and t["example"] else "")
                + f"ROSTER: {roster_str}\n\n"
                + (f"HOW THE PREVIOUS PASSAGE ENDED (speakers already decided; evidence "
                   f"only, never attribute these):\n{tail}\n\n" if tail else "")
                + f"{passage}\n\n{t['user']}")
    elif variant in PASSAGE_SHAPED:
        body = (f"{t['user']}\n\nROSTER: {roster_str}\n\n"
                + _wrap_passage(passage_text(frozen_batch, neighbor_contexts), surround or {}))
    else:
        # canonical request, with the preset's texts standing in for the file's
        canon_params = replace(params, attribute_system_prompt=t["system"],
                               user_prompt_template=t["user"])
        sys_prompt, canonical = build_attribute_request(
            frozen_batch, canon_params, roster, neighbor_contexts, surround)
        body = canonical.replace(f"ESTABLISHED ROSTER: {', '.join(roster) or '(none yet)'}",
                                 f"ESTABLISHED ROSTER: {roster_str}")
    if variant in ("incremental", "michel") and memory["previous"]:
        prev = "; ".join(f"{i}: {s}" for i, s in memory["previous"])
        body = ("Speakers already decided for the lines immediately before this passage "
                f"(most recent last): {prev}\n\n") + body
    if variant == "judge":
        body = body + JUDGE_INSTRUCTION
    if variant == "continuity" and (memory["summary"] or memory["tail"]):
        tail = "\n".join(f'{spk}: "{get_escaped_passage_text(text)}"' for spk, text in memory["tail"])
        body = ("STORY SO FAR (for identity and continuity only; never attribute "
                f"lines from it):\n{memory['summary'] or '(none)'}\n\n"
                f"LAST LINES OF THE PREVIOUS PASSAGE, with their speakers:\n{tail or '(none)'}"
                "\n\n") + body
    return sys_prompt, body


def make_provider(variant, alias_groups=None, texts=None):
    """-> an entries_provider for attribute_batch; keeps per-run memory for
    the incremental/continuity/michel2 variants. `texts` is the active
    preset's {"system", "user", "example"}; None sends the builtin."""
    if variant not in VARIANTS:
        raise ValueError(f"unknown prompt variant {variant!r}; expected one of {VARIANTS}")
    import uuid
    run_id = uuid.uuid4().hex
    memory = {"previous": [], "summary": "", "tail": []}

    def build_request(frozen_batch, params, roster=None, neighbor_contexts=None,
                      surround=None, roster_traits=None):
        """Preview the exact next request, including this run's rolling memory."""
        return build_variant_request(
            variant, frozen_batch, params, roster, alias_groups, neighbor_contexts,
            surround, memory, texts, roster_traits)

    def provider(client, model_name, sys_prompt, user_prompt, params, log_name, label,
                 max_retries, validate_entries, attempt_observer, frozen_batch,
                 roster=None, neighbor_contexts=None, surround=None, roster_traits=None,
                 **_ignored):
        michel2_family = variant.startswith("michel2")
        sys_prompt, body = build_request(
            frozen_batch, params, roster, neighbor_contexts, surround, roster_traits)
        validator = validate_entries
        if variant == "judge":
            from dataclasses import replace
            params = replace(params, response_schema=JUDGE_RESPONSE_SCHEMA)

            def validator(entries):
                report = validate_entries(entries) if validate_entries else {"passed": True, "findings": []}
                if not report["passed"]:
                    return report
                reasons = validate_judge_reasons(frozen_batch, entries)
                return {**report, "passed": reasons["passed"],
                        "findings": list(report.get("findings", [])) + reasons["findings"]}

        named = call_llm_for_entries(
            client, model_name, sys_prompt, body, params, log_name=log_name, label=label,
            max_retries=max_retries, validate_entries=validator,
            attempt_observer=attempt_observer)
        from pass_quality import index_head_check
        aligned, _reason, ordered = index_head_check(frozen_batch, named)
        if named and aligned:
            spoken = [(i, item.get("speaker")) for i, (f, item) in enumerate(zip(frozen_batch, ordered))
                      if f["type"] == "SPOKEN" and item.get("speaker")]
            memory["previous"] = spoken[-8:]
            if variant == "continuity" or michel2_family:
                memory["tail"] = [(item.get("speaker"), f["text"])
                                  for f, item in zip(frozen_batch, ordered)
                                  if f["type"] == "SPOKEN"][-TAIL_LINES:]
        if variant == "judge":
            record_judge_reasons(frozen_batch, named, run_id)
        if variant == "continuity":
            memory["summary"] = rolling_summary(
                client, model_name, params, memory["summary"],
                passage_text(frozen_batch, neighbor_contexts))
        return named

    provider.variant = variant
    provider.build_request = build_request
    return provider
