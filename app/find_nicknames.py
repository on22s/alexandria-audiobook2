"""Discover nickname / alias relationships between characters in an annotated script.

Unlike the speaker de-dupe pass in review_script.py (which only sees a few sample
lines per speaker label), this pass gathers richer *context* — narration and dialog
where two character names co-occur — so it can find non-obvious aliases such as a
pet name the cast uses for someone (e.g. "Betty" for "Beatrice").

It writes a flat alias map { "ALIAS": "CANONICAL", ... } to an aliases file. The
review pass then loads that file and applies the mappings deterministically, so you
can "re-run review with the nicknames in mind". The file is plain JSON and meant to
be human-editable before you re-run the review.
"""
import os
import sys
import json
import re
import argparse
from openai import OpenAI, OpenAIError
from utils import safe_load_json, atomic_json_write

# Reuse the group/narrator guards so we never propose collapsing two characters.
from review_script import _is_group_label


NICKNAME_SYSTEM_PROMPT = (
    "You are an expert at character identity resolution in narrated fiction. "
    "You are given the distinct SPEAKER labels in an audiobook script, sample lines for "
    "each, and CONTEXT passages where multiple character names appear together. Your job "
    "is to find labels that are NICKNAMES, pet names, titles, or alternate names for the "
    "SAME single character, and map each such alias to the character's canonical name.\n\n"
    "Use the context as evidence — e.g. narration like 'Beatrice, or Betty as he called her', "
    "or one character addressing another by a familiar name. Prefer evidence in the text over "
    "guessing.\n\n"
    "Rules:\n"
    "- Only propose a mapping when the context (or unambiguous naming) supports it.\n"
    "- NEVER map a label that denotes MULTIPLE characters (e.g. 'RAM AND REM', 'TWINS', "
    "'EMILIA/PUCK') — skip those entirely.\n"
    "- NEVER map NARRATOR.\n"
    "- The canonical should be the clearest proper name (usually the full/most common name).\n"
    "- If EXISTING ALIASES are given, stay consistent with them.\n\n"
    'Respond with ONLY JSON of the form {"aliases": {"Betty": "BEATRICE"}, '
    '"evidence": {"Betty": "narration: \'Beatrice, whom Subaru called Betty\'"}}. '
    "Map only aliases that need changing; omit canonicals and anything uncertain. No prose, no markdown."
)


# Rough English chars-per-token, used only to budget the prompt so it fits the
# model's context window (the VRAM-safe LM Studio default is 8192).
_CHARS_PER_TOKEN = 3.5


def _prompt_char_budget(context_length, max_tokens, system_chars):
    """Char budget for the user prompt so system+user+reply fit context_length.

    Reserves the reply (`max_tokens`) plus a margin, converts the remaining
    token room to chars, and subtracts the (fixed) system prompt length.
    """
    input_tokens = max(512, context_length - max_tokens - 512)
    return max(1000, int(input_tokens * _CHARS_PER_TOKEN) - system_chars)


def _entry_speaker(e):
    return (e.get("speaker") or e.get("type") or "").strip()


def _entry_text(e):
    return (e.get("text") or "").strip()


def _name_tokens(name):
    """Lowercased word tokens of a name, ignoring parenthetical qualifiers and short stopwords."""
    base = re.sub(r"\(.*?\)", " ", name)  # drop "(INTERNAL)" etc.
    toks = re.findall(r"[A-Za-z']{3,}", base.lower())
    return [t for t in toks if t not in {"the", "and", "voice", "echo"}]


def collect_context(entries, max_per_speaker=6, max_cooccur=300):
    """Return (speakers, samples, cooccurrence_snippets).

    cooccurrence_snippets are entry texts mentioning >=2 distinct character name tokens —
    the strongest textual evidence for an alias relationship.
    """
    samples = {}
    for e in entries:
        sp, txt = _entry_speaker(e), _entry_text(e)
        if not sp or not txt:
            continue
        lines = samples.setdefault(sp, [])
        if len(lines) < max_per_speaker:
            lines.append(txt[:200])

    speakers = sorted(samples.keys())

    # Map each non-narrator speaker to its leading name token for co-occurrence scanning
    token_to_speaker = {}
    for sp in speakers:
        if sp.upper() == "NARRATOR":
            continue
        for tok in _name_tokens(sp):
            token_to_speaker.setdefault(tok, sp)

    # Pre-compile one word-boundary pattern per token (once, not per entry) and
    # test each independently. A combined alternation with findall would be
    # faster but is NOT equivalent: findall consumes a match, so when one
    # speaker's token is a prefix of another's at the same position (e.g. "beat"
    # vs "beatrice"), only the longer would register and a real co-occurrence
    # would be dropped. Independent searches credit every token that appears.
    token_patterns = [(re.compile(rf"\b{re.escape(tok)}"), sp)
                      for tok, sp in token_to_speaker.items()]

    cooccur = []
    seen = set()
    for e in entries:
        txt = _entry_text(e)
        if not txt or len(txt) > 600:
            continue
        low = txt.lower()
        hits = {sp for pat, sp in token_patterns if pat.search(low)}
        if len(hits) >= 2:
            key = txt[:120]
            if key not in seen:
                seen.add(key)
                cooccur.append(txt[:400])
                if len(cooccur) >= max_cooccur:
                    break

    return speakers, samples, cooccur


def _parse_alias_response(raw, speakers):
    """Normalize one LLM response into (aliases, evidence) maps.

    Resolves model casing back to the real speaker label, drops self/NARRATOR/
    group mappings, and keeps only variants that actually appear as a label.
    """
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    data = json.loads(m.group(0)) if m else {}
    raw_aliases = data.get("aliases", data) if isinstance(data, dict) else {}
    evidence = data.get("evidence", {}) if isinstance(data, dict) else {}

    label_by_norm = {sp.strip().lower(): sp for sp in speakers}
    aliases = {}
    for variant, canonical in (raw_aliases or {}).items():
        if not isinstance(variant, str) or not isinstance(canonical, str):
            continue
        variant, canonical = variant.strip(), canonical.strip()
        if not variant or not canonical:
            continue
        actual_variant = label_by_norm.get(variant.lower())
        if not actual_variant:  # only aliases that actually appear as a label
            continue
        # Prefer an existing label spelling for the canonical when one matches
        canonical = label_by_norm.get(canonical.lower(), canonical)
        if actual_variant == canonical:
            continue
        if actual_variant.upper() == "NARRATOR" or canonical.upper() == "NARRATOR":
            continue
        if _is_group_label(actual_variant) and not _is_group_label(canonical):
            print(f"  [skip] '{actual_variant}' is a combined/group label")
            continue
        aliases[actual_variant] = canonical
    return aliases, evidence


def _chunk_evidence(cooccur, evidence_budget):
    """Pack co-occurrence passages into char-budgeted chunks (>=1 chunk always)."""
    chunks, cur, cur_len = [], [], 0
    for c in cooccur:
        line = f"- {c}"
        if cur and cur_len + len(line) + 1 > evidence_budget:
            chunks.append(cur)
            cur, cur_len = [], 0
        cur.append(line)
        cur_len += len(line) + 1
    if cur:
        chunks.append(cur)
    return chunks or [[]]


def find_nicknames(client, model_name, entries, existing_aliases=None,
                   max_tokens=2000, temperature=0.2, context_length=8192):
    """Discover nickname/alias relationships. Returns (aliases, evidence).

    The full speaker roster is sent with every request, but the co-occurrence
    evidence is split into chunks that each fit `context_length` (default 8192,
    the VRAM-safe LM Studio setting). This lets the model see ALL the evidence
    across several safe-sized calls instead of overflowing the context window
    (the old single-call approach failed large-cast books with an n_ctx error).
    Aliases found in earlier chunks are fed forward so later chunks stay
    consistent, and all results are merged.
    """
    speakers, samples, cooccur = collect_context(entries)
    if len(speakers) < 2:
        return {}, {}

    existing_aliases = dict(existing_aliases or {})
    budget = _prompt_char_budget(context_length, max_tokens, len(NICKNAME_SYSTEM_PROMPT))

    # Roster block (every speaker + scaled sample lines) goes in every call, so
    # cap it at ~half the budget and leave the rest for a chunk of evidence.
    roster_budget = int(budget * 0.5)
    per_speaker = max(1, min(6, roster_budget // max(1, len(speakers)) // 140))
    roster_lines = ["SPEAKER LABELS + SAMPLE LINES:"]
    for sp in speakers:
        roster_lines.append(f'- "{sp}": ' + " | ".join(samples[sp][:per_speaker]))
    roster_block = "\n".join(roster_lines)
    if len(roster_block) > budget:  # extreme cast - truncate roster as last resort
        roster_block = roster_block[:budget]

    evidence_budget = max(500, budget - len(roster_block))
    chunks = _chunk_evidence(cooccur, evidence_budget)
    if len(chunks) > 1:
        print(f"  Splitting {len(cooccur)} evidence passages into {len(chunks)} "
              f"context-safe chunk(s) for {context_length}-token model.")

    all_aliases, all_evidence = {}, {}
    for ci, ev_lines in enumerate(chunks):
        parts = []
        accumulated = {**existing_aliases, **all_aliases}
        if accumulated:
            parts.append("EXISTING ALIASES (stay consistent):")
            parts.append(json.dumps(accumulated, ensure_ascii=False))
            parts.append("")
        parts.append(roster_block)
        if ev_lines:
            parts.append("\nCONTEXT PASSAGES (multiple names co-occur — alias evidence):")
            parts.extend(ev_lines)
        parts.append("\nReturn the JSON now.")
        user_prompt = "\n".join(parts)

        if len(chunks) > 1:
            print(f"  Evidence chunk {ci + 1}/{len(chunks)}...")
        try:
            resp = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": NICKNAME_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            raw = resp.choices[0].message.content or ""
            aliases, evidence = _parse_alias_response(raw, speakers)
        except (json.JSONDecodeError, AttributeError, IndexError, OpenAIError) as e:
            print(f"Nickname discovery failed on chunk {ci + 1}/{len(chunks)}: {e}")
            continue
        all_aliases.update(aliases)
        all_evidence.update(evidence)

    return all_aliases, all_evidence


def main():
    parser = argparse.ArgumentParser(description="Discover character nickname/alias mappings")
    parser.add_argument("--input", help="Script JSON to scan (default: ../annotated_script.json)")
    parser.add_argument("--aliases-file", help="Where to write the alias map (default: ../character_aliases.json)")
    parser.add_argument("--append", action="store_true", help="Merge into the existing aliases file instead of replacing")
    args = parser.parse_args()

    base = os.path.dirname(os.path.abspath(__file__))
    script_path = args.input or os.path.join(base, "..", "annotated_script.json")
    aliases_path = args.aliases_file or os.path.join(base, "..", "character_aliases.json")

    if not os.path.exists(script_path):
        print(f"Error: script not found: {script_path}")
        sys.exit(1)

    with open(script_path, "r", encoding="utf-8") as f:
        entries = json.load(f)
    print(f"Scanning {len(entries)} entries for character nicknames...")

    config_path = os.path.join(base, "config.json")
    config = safe_load_json(config_path, default={})
    llm = config.get("llm", {})
    client = OpenAI(base_url=llm.get("base_url", "http://localhost:11434/v1"),
                    api_key=llm.get("api_key", "local"))
    model_name = llm.get("model_name", "local-model")
    context_length = int(llm.get("context_length", 8192) or 8192)
    print(f"Using model: {model_name}")

    existing = {}
    if args.append:
        existing = safe_load_json(aliases_path, default={}) or {}

    aliases, evidence = find_nicknames(client, model_name, entries,
                                       existing_aliases=existing,
                                       context_length=context_length)

    if aliases:
        print(f"\nFound {len(aliases)} nickname/alias mapping(s):")
        for variant, canonical in aliases.items():
            why = evidence.get(variant, "")
            print(f"  '{variant}' -> '{canonical}'" + (f"   ({why})" if why else ""))
    else:
        print("\nNo new nicknames found.")

    merged = dict(existing)
    merged.update(aliases)
    atomic_json_write(merged, aliases_path)
    print(f"\nAlias file saved to: {aliases_path} ({len(merged)} total entries)")
    print("Task find_nicknames completed successfully.")


if __name__ == "__main__":
    main()
