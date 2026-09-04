"""Attribution in two steps: reason first, serialise second.

THE QUESTION. When Qwen3.8 fails attribution it fails at the wrapper, not the
thinking - 81 of 93 unanswered rows are the model writing its own analysis
where the JSON belongs, and reaching correct conclusions inside that prose. So
does letting it reason freely, then converting, name the speaker MORE OFTEN?

WHY THIS IS A DIFFERENT EXPERIMENT FROM twostep_ab_20260903. That one ran
generate_script, which has no gold and scores TEXT RECALL - it can say whether
a script is faithful, never whether a speaker is right. This runs the
attribution path against attribution_gold_*, so correctness is measurable.

IT REPLACES ONLY THE LLM CALL. attribute_batch keeps its text freeze
(validate_attribution), its index binding (index_head_check) and its exhaustion
path; this provider slots into the one place the model is asked. An alternative
strategy therefore cannot skip the checks that make the answer trustworthy, and
both arms are scored by identical code (Rule 15).

STAGE 2 SEES ONLY STAGE 1'S TEXT plus the entry heads it must label. It is not
given the passage, so it cannot quietly redo stage 1's job and turn this into a
one-pass arm wearing two hats.
"""
import re

FREEFORM_INSTRUCTION = (
    "Work out who speaks each numbered entry. Think it through in plain prose: "
    "quote the cues you are using, weigh alternatives where the text is "
    "ambiguous, and end each entry with a line of the form\n"
    "  ENTRY <n>: <SPEAKER NAME>\n"
    "using a name from the roster exactly as written, or UNKNOWN. Do not "
    "output JSON. Do not repeat the passage."
)

CONVERSION_SYSTEM = (
    "You convert an analyst's notes into JSON. You are not deciding anything.\n"
    "Output ONLY a JSON array, one object per entry, in entry order:\n"
    '  [{"n": 0, "head": "<first words of the entry>", "speaker": "<NAME>"}]\n'
    "\n"
    "RULES:\n"
    "1. Take each speaker from the notes' 'ENTRY <n>: <NAME>' lines.\n"
    "2. If the notes never settle an entry, use UNKNOWN. Do not invent one.\n"
    "3. Emit exactly one object per entry asked for, in order, none skipped.\n"
    "4. Copy each head verbatim from the entry list you are given.\n"
)

ENTRY_DECISION = re.compile(r"^\s*ENTRY\s+(\d+)\s*:\s*(.+?)\s*$", re.M | re.I)


def parse_decisions(text):
    """-> {entry index: speaker} from stage 1's own summary lines.

    Used for reporting how often stage 1 reached a decision at all, which is
    the number that separates 'the reasoning failed' from 'the conversion lost
    it'. Not used to score; scoring goes through the same validator as the
    one-pass arm.
    """
    out = {}
    for m in ENTRY_DECISION.finditer(text or ""):
        try:
            out[int(m.group(1))] = m.group(2).strip()
        except ValueError:
            continue
    return out


def entries_from_decisions(frozen_batch, notes):
    """Serialise stage 1's own ENTRY lines with a regex - no second model.

    This is the cheapest possible stage 2 and the purest test of the question
    "does the reasoning get the speaker right": if a deterministic parse of the
    model's own conclusions scores well, then nothing about the reasoning needs
    a serialiser at all, and any failure of the one-pass arm was the wrapper.
    Undecided entries become UNKNOWN rather than being dropped, so the
    denominator is unchanged.
    """
    decided = parse_decisions(notes)
    out = []
    for n, entry in enumerate(frozen_batch):
        head = " ".join(str(entry.get("text") or "").split()[:6])
        out.append({"n": n, "head": head,
                    "speaker": decided.get(n, "UNKNOWN")})
    return out


def build_provider(stats=None, mode="two_step", stage2_client=None,
                   stage2_model=None):
    """Return an entries_provider for attribute_batch.

    mode="two_step"    stage 1 reasons, a MODEL serialises
    mode="stage1_only" stage 1 reasons, a REGEX serialises (no second call)

    stage2_client / stage2_model let the serialiser be a DIFFERENT model from
    the reasoner - the point being that if 3.8's reasoning is the good part,
    nothing requires 3.8 to also emit the JSON.
    """
    """Return an entries_provider for attribute_batch.

    `stats`, when given, accumulates per-batch counters: how many entries stage
    1 decided, and whether stage 2 produced anything. That is the diagnostic
    that tells reasoning failure apart from serialisation failure - the
    distinction the generate_script two-step run could not make, because
    validation happens only after stage 2 and every failure was labelled there.
    """
    from generate_script import call_llm_for_entries
    from response_codecs import FREEFORM_KEY, get_codec

    def provider(client, model_name, sys_prompt, user_prompt, params,
                 log_name="llm_responses.log", label="ATTRIBUTE",
                 max_retries=3, validate_entries=None, attempt_observer=None,
                 frozen_batch=None, **kwargs):
        stage1 = call_llm_for_entries(
            client, model_name, sys_prompt + "\n\n" + FREEFORM_INSTRUCTION,
            user_prompt, params, log_name=log_name,
            label=f"{label} stage1", max_retries=max_retries,
            attempt_observer=attempt_observer, codec=get_codec("freeform"))
        if not stage1:
            if stats is not None:
                stats.append({"stage1_decided": 0, "stage1_empty": True})
            return []
        notes = stage1[0].get(FREEFORM_KEY) or ""
        decided = parse_decisions(notes)
        if stats is not None:
            stats.append({"stage1_decided": len(decided), "stage1_empty": False})
        if mode == "stage1_only":
            if frozen_batch is None:
                return []
            entries = entries_from_decisions(frozen_batch, notes)
            # Still validated by attribute_batch's own validator.
            if validate_entries is not None:
                report = validate_entries(entries)
                if not (report or {}).get("passed"):
                    return []
            return entries
        convert_user = ("NOTES:\n" + notes.strip() + "\n\n"
                        "ENTRIES TO LABEL:\n" + user_prompt.strip())
        return call_llm_for_entries(
            stage2_client or client, stage2_model or model_name,
            CONVERSION_SYSTEM, convert_user, params,
            log_name=log_name, label=f"{label} stage2",
            max_retries=max_retries, validate_entries=validate_entries,
            attempt_observer=attempt_observer, codec=get_codec("json"))

    return provider
