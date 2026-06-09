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
from openai import OpenAI

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


def _entry_speaker(e):
    return (e.get("speaker") or e.get("type") or "").strip()


def _entry_text(e):
    return (e.get("text") or "").strip()


def _name_tokens(name):
    """Lowercased word tokens of a name, ignoring parenthetical qualifiers and short stopwords."""
    base = re.sub(r"\(.*?\)", " ", name)  # drop "(INTERNAL)" etc.
    toks = re.findall(r"[A-Za-z']{3,}", base.lower())
    return [t for t in toks if t not in {"the", "and", "voice", "echo"}]


def collect_context(entries, max_per_speaker=6, max_cooccur=40):
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

    cooccur = []
    seen = set()
    for e in entries:
        txt = _entry_text(e)
        if not txt or len(txt) > 600:
            continue
        low = txt.lower()
        hits = {sp for tok, sp in token_to_speaker.items() if re.search(rf"\b{re.escape(tok)}", low)}
        if len(hits) >= 2:
            key = txt[:120]
            if key not in seen:
                seen.add(key)
                cooccur.append(txt[:400])
                if len(cooccur) >= max_cooccur:
                    break

    return speakers, samples, cooccur


def find_nicknames(client, model_name, entries, existing_aliases=None,
                   max_tokens=2000, temperature=0.2):
    """Ask the LLM to discover nickname/alias relationships. Returns (aliases, evidence)."""
    speakers, samples, cooccur = collect_context(entries)
    if len(speakers) < 2:
        return {}, {}

    existing_aliases = existing_aliases or {}
    parts = []
    if existing_aliases:
        parts.append("EXISTING ALIASES (stay consistent):")
        parts.append(json.dumps(existing_aliases, ensure_ascii=False))
        parts.append("")
    parts.append("SPEAKER LABELS + SAMPLE LINES:")
    for sp in speakers:
        parts.append(f'- "{sp}": ' + " | ".join(samples[sp]))
    if cooccur:
        parts.append("\nCONTEXT PASSAGES (multiple names co-occur — alias evidence):")
        parts.extend(f"- {c}" for c in cooccur)
    parts.append("\nReturn the JSON now.")
    user_prompt = "\n".join(parts)

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
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        data = json.loads(m.group(0)) if m else {}
    except Exception as e:
        print(f"Nickname discovery failed: {e}")
        return {}, {}

    raw_aliases = data.get("aliases", data) if isinstance(data, dict) else {}
    evidence = data.get("evidence", {}) if isinstance(data, dict) else {}

    # Case-insensitive resolution back to the actual speaker-label spelling, since
    # the model often echoes prose casing (e.g. "Betty") not the label ("BETTY").
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

    config = {}
    config_path = os.path.join(base, "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            print(f"Warning: failed to load config.json: {e}")
    llm = config.get("llm", {})
    client = OpenAI(base_url=llm.get("base_url", "http://localhost:11434/v1"),
                    api_key=llm.get("api_key", "local"))
    model_name = llm.get("model_name", "local-model")
    print(f"Using model: {model_name}")

    existing = {}
    if args.append and os.path.exists(aliases_path):
        try:
            with open(aliases_path, "r", encoding="utf-8") as f:
                existing = json.load(f) or {}
        except (json.JSONDecodeError, ValueError):
            existing = {}

    aliases, evidence = find_nicknames(client, model_name, entries, existing_aliases=existing)

    if aliases:
        print(f"\nFound {len(aliases)} nickname/alias mapping(s):")
        for variant, canonical in aliases.items():
            why = evidence.get(variant, "")
            print(f"  '{variant}' -> '{canonical}'" + (f"   ({why})" if why else ""))
    else:
        print("\nNo new nicknames found.")

    merged = dict(existing)
    merged.update(aliases)
    with open(aliases_path, "w", encoding="utf-8") as f:
        json.dump(merged, f, indent=2, ensure_ascii=False)
    print(f"\nAlias file saved to: {aliases_path} ({len(merged)} total entries)")
    print("Task find_nicknames completed successfully.")


if __name__ == "__main__":
    main()
