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
from core import llm_timeout_seconds
import sys
import json
import re
import time
import argparse
from dataclasses import replace
from concurrent.futures import ThreadPoolExecutor
from config_settings import load_app_config
from llm_provider import make_run_client
from generate_script import LLMGenParams, call_llm_for_object, ensure_run_request_params
from utils import safe_load_json, file_lock, atomic_json_write, extract_json_object, warn_unparseable_llm_json, get_runtime_data_dir, get_app_config_path
from llm_bench import get_cached_or_benchmarked_concurrency
from lmstudio_settings import ensure_ideal_settings, get_active_llm_config, TokenBudgetError

# Reuse the group/narrator guards so we never propose collapsing two characters.
from speaker_identity import (is_speaker_merge_allowed, get_validated_alias_graph,
                              get_safe_alias_proposals)

# Reuse the codebase's one near-miss-label similarity check (Rule 15) — here it
# only *reports* dropped near-misses; alias resolution itself stays exact-match.
from speaker_identity import _uncertain_candidates, resolve_speaker_label, get_validated_alias_map


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


def _prompt_char_budget(context_length, max_tokens, system_chars):
    """Match the shared call budget's three chars/token and 512-token reserve."""
    budget = (context_length - max_tokens - 512) * 3 - system_chars
    if budget < 1:
        raise TokenBudgetError("Nickname system prompt and reply exceed the context; "
                               "reduce max_tokens or increase model context.")
    return budget


def _entry_speaker(e):
    return (e.get("speaker") or e.get("type") or "").strip()


def _entry_text(e):
    return (e.get("text") or "").strip()


def _name_tokens(name):
    """Lowercased word tokens of a name, ignoring parenthetical qualifiers and short stopwords."""
    base = re.sub(r"\(.*?\)", " ", name)  # drop "(INTERNAL)" etc.
    toks = re.findall(r"[^\W\d_]+(?:'[^\W\d_]*)*", base.lower())
    return [t for t in toks if (len(t) >= 3 or not t.isascii())
            and t not in {"the", "and", "voice", "echo"}]


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

    # Share prefixes across the cast, retaining every terminal for overlapping
    # tokens such as James and James'. Start only at Unicode word boundaries.
    token_tree = {}
    for token, speaker in token_to_speaker.items():
        node = token_tree
        for char in token:
            node = node.setdefault(char, {})
        node[None] = speaker
    word_starts = re.compile(r"\b(?=\w)")

    cooccur = []
    seen = set()
    for e in entries:
        txt = _entry_text(e)
        if not txt or len(txt) > 600:
            continue
        low = txt.lower()
        hits = set()
        for start in word_starts.finditer(low):
            node = token_tree
            for offset in range(start.start(), len(low)):
                node = node.get(low[offset])
                if node is None:
                    break
                following = low[offset + 1:offset + 2]
                if None in node and (not following or not (following.isalnum() or following == "_")):
                    hits.add(node[None])
            if len(hits) >= 2:
                break
        if len(hits) >= 2:
            key = txt[:120]
            if key not in seen:
                seen.add(key)
                cooccur.append(txt[:400])
                if len(cooccur) >= max_cooccur:
                    break

    return speakers, samples, cooccur


def _warn_near_miss_label(role, label, speakers):
    """Report (never act on) a model-proposed label that isn't an exact match
    but closely resembles a real speaker label, so a silently dropped alias is
    visible in the task log and human-fixable in the aliases file."""
    candidates = _uncertain_candidates(label, speakers)
    if candidates:
        best = candidates[0]
        print(f"  [near-miss] {role} '{label}' matches no label exactly; "
              f"closest is '{best['speaker']}' (similarity {best['similarity']}) - "
              f"not merged, exact matches only")


def _parse_alias_response(raw, speakers, existing_aliases=None):
    """Normalize one LLM response into (aliases, evidence) maps.

    Resolves model casing back to the real speaker label, drops self/NARRATOR/
    group mappings, and keeps only variants that actually appear as a label.
    """
    data = extract_json_object(raw)
    if data is None:
        warn_unparseable_llm_json("alias", raw, "treating as no aliases found")
        data = {}
    raw_aliases = data.get("aliases", data) if isinstance(data, dict) else {}
    if not isinstance(raw_aliases, dict):
        raise ValueError("nickname aliases must be an object")
    evidence = data.get("evidence", {}) if isinstance(data, dict) else {}
    if not isinstance(evidence, dict):
        warn_unparseable_llm_json("alias evidence", json.dumps(evidence),
                                 "dropping malformed evidence")
        evidence = {}

    aliases = {}
    proposals = []
    for variant, canonical in (raw_aliases or {}).items():
        if not isinstance(variant, str) or not isinstance(canonical, str):
            continue
        variant, canonical = variant.strip(), canonical.strip()
        if not variant or not canonical:
            continue
        actual_variant = resolve_speaker_label(variant, speakers)
        if not actual_variant:  # only aliases that actually appear as a label
            _warn_near_miss_label("variant", variant, speakers)
            continue
        # Prefer an existing label spelling for the canonical when one matches
        resolved_canonical = resolve_speaker_label(canonical, speakers)
        canonical = resolved_canonical if resolved_canonical is not None else canonical
        if actual_variant == canonical:
            continue
        if not is_speaker_merge_allowed(actual_variant, canonical):
            print("  [skip] protected speaker mapping")
            continue
        proposals.append({actual_variant: canonical})
        aliases[actual_variant] = canonical
    safe = get_safe_alias_proposals(proposals, speakers, existing_aliases)
    for variant, canonical in aliases.items():
        if variant not in safe and resolve_speaker_label(canonical, speakers) is None:
            _warn_near_miss_label("canonical", canonical, speakers)
    retained_evidence = {}
    conflicting_evidence = set()
    for label, explanation in evidence.items():
        if not isinstance(label, str):
            continue
        variant = resolve_speaker_label(label.strip(), speakers)
        if variant in safe and variant not in conflicting_evidence:
            previous = retained_evidence.get(variant)
            if previous is not None and previous != explanation:
                retained_evidence.pop(variant, None)
                conflicting_evidence.add(variant)
                continue
            retained_evidence[variant] = explanation
    return safe, retained_evidence


def _chunk_evidence(cooccur, evidence_budget):
    """Pack co-occurrence passages into char-budgeted chunks (>=1 chunk always)."""
    if evidence_budget < 4 and cooccur:
        raise TokenBudgetError("Nickname context has no room for evidence; increase model context.")
    chunks, cur, cur_len = [], [], 0
    for passage in cooccur:
        # Preserve every character while splitting passages too large for one call.
        for start in range(0, len(passage), max(1, evidence_budget - 3)):
            line = "- " + passage[start:start + evidence_budget - 3]
            if cur and cur_len + len(line) + 1 > evidence_budget:
                chunks.append(cur)
                cur, cur_len = [], 0
            cur.append(line)
            cur_len += len(line) + 1
    if cur:
        chunks.append(cur)
    return chunks or [[]]


def find_nicknames(client, model_name, entries, existing_aliases=None,
                   max_tokens=2000, temperature=0.2, context_length=8192,
                   concurrency=1, params=None):
    """Discover nickname/alias relationships. Returns (aliases, evidence).

    The full speaker roster is sent with every request, but the co-occurrence
    evidence is split into chunks that each fit `context_length` (default 8192,
    the VRAM-safe LM Studio setting). This lets the model see ALL the evidence
    across several safe-sized calls instead of overflowing the context window
    (the old single-call approach failed large-cast books with an n_ctx error).

    Chunks are processed `concurrency` at a time ("waves") instead of strictly
    one at a time, so a server that can serve multiple requests at once (e.g.
    `--parallel N`) actually gets used. Aliases found in earlier WAVES are fed
    forward so later waves stay consistent; chunks within the same wave can't
    see each other's results yet (fixed up once that wave finishes), all
    results are merged at the end.
    """
    if not isinstance(concurrency, int) or concurrency < 1:
        raise ValueError("Nickname concurrency must be a positive integer")
    trusted_aliases = get_validated_alias_graph({} if existing_aliases is None else existing_aliases)
    existing_aliases = trusted_aliases
    speakers, samples, cooccur = collect_context(entries)
    if len(speakers) < 2:
        return {}, {}

    # Keep only registry entries this call could actually use: _parse_alias_response
    # discards any variant that isn't one of this book's speaker labels, so a variant
    # from another book in the series is dead weight - and with a multi-book shared
    # registry (--append across a whole batch) that dead weight is what was blowing
    # the context budget below, since it's otherwise dumped into every chunk uncounted.
    speaker_set = {sp.strip().lower() for sp in speakers}
    existing_aliases = {k: v for k, v in (existing_aliases or {}).items()
                        if k.strip().lower() in speaker_set}
    budget = _prompt_char_budget(context_length, max_tokens, len(NICKNAME_SYSTEM_PROMPT))

    def get_prompt(roster, snapshot, ev_lines):
        parts = []
        if snapshot:
            parts.extend(["EXISTING ALIASES (stay consistent):",
                          json.dumps(snapshot, ensure_ascii=False), ""])
        parts.append(roster)
        if ev_lines:
            parts.append("\nCONTEXT PASSAGES (multiple names co-occur — alias evidence):")
            parts.extend(ev_lines)
        parts.append("\nReturn the JSON now.")
        return "\n".join(parts)

    roster_lines = ["SPEAKER LABELS + SAMPLE LINES:"] + [
        "- " + json.dumps(sp, ensure_ascii=False) for sp in speakers]
    roster_block = "\n".join(roster_lines)
    context_header_chars = len("\nCONTEXT PASSAGES (multiple names co-occur — alias evidence):\n")
    fixed_chars = len(get_prompt(roster_block, existing_aliases, []))
    if fixed_chars + context_header_chars + 4 > budget:
        raise TokenBudgetError("Nickname full roster and existing aliases exceed the context; "
                               "increase model context or reduce the reply allowance.")
    # Retain all labels; use only the space available for optional sample text.
    sample_room = max(0, min(budget // 2, budget - fixed_chars - context_header_chars - 4))
    per_speaker_chars = sample_room // len(speakers)
    if per_speaker_chars >= 3:
        for index, sp in enumerate(speakers, 1):
            roster_lines[index] += ": " + " | ".join(samples[sp])[:per_speaker_chars - 2]
        roster_block = "\n".join(roster_lines)
    evidence_budget = budget - len(get_prompt(roster_block, existing_aliases, [])) - context_header_chars
    chunks = _chunk_evidence(cooccur, evidence_budget)
    if len(chunks) > 1:
        print(f"  Splitting {len(cooccur)} evidence passages into {len(chunks)} "
              f"context-safe chunk(s) for {context_length}-token model.")
    call_params = replace(params or LLMGenParams(), max_tokens=max_tokens,
                          temperature=temperature, context_length=context_length,
                          hard_max_tokens=12000)

    def _process_chunk(item):
        ci, ev_lines, accumulated_snapshot, wave_roster, total_chunks = item
        def request(roster, lines, request_params, attempts):
            return call_llm_for_object(
                client, model_name, NICKNAME_SYSTEM_PROMPT,
                get_prompt(roster, accumulated_snapshot, lines), request_params,
                label=f"NICKNAMES {ci + 1}/{total_chunks}",
                attempt_observer=attempts.append)

        def adapt_to_runtime(request_params):
            # Keep every label and evidence character. Sample lines are optional
            # roster decoration; the co-occurrence evidence is not optional.
            minimal_roster = "\n".join(["SPEAKER LABELS + SAMPLE LINES:"] + [
                "- " + json.dumps(sp, ensure_ascii=False) for sp in speakers])
            active_budget = _prompt_char_budget(
                request_params.context_length, max_tokens, len(NICKNAME_SYSTEM_PROMPT))
            room = active_budget - len(get_prompt(minimal_roster, accumulated_snapshot, [])) - context_header_chars
            if room < 4:
                raise TokenBudgetError("Nickname full roster and aliases exceed the serving context")
            parts = _chunk_evidence([line[2:] for line in ev_lines], room)
            proposals, evidence = [], {}
            for lines in parts:
                parsed = request(minimal_roster, lines, request_params, [])
                if not isinstance(parsed, dict):
                    raise RuntimeError("Nickname adapted request failed; alias file was not updated")
                aliases, observations = _parse_alias_response(
                    json.dumps(parsed, ensure_ascii=False), speakers, trusted_aliases)
                proposals.append(aliases)
                evidence.update(observations)
            return get_safe_alias_proposals(proposals, speakers, trusted_aliases), evidence

        request_params = ensure_run_request_params(client, call_params)
        user_prompt = get_prompt(wave_roster, accumulated_snapshot, ev_lines)
        active_budget = _prompt_char_budget(
            request_params.context_length, max_tokens, len(NICKNAME_SYSTEM_PROMPT))
        if len(user_prompt) > active_budget:
            if request_params.context_length != call_params.context_length:
                return adapt_to_runtime(request_params)
            raise TokenBudgetError("Nickname prompt exceeds the context; increase model context.")
        if total_chunks > 1:
            print(f"  Evidence chunk {ci + 1}/{total_chunks}...")
        t0 = time.time()
        attempts = []
        result = request(wave_roster, ev_lines, request_params, attempts)
        print(f"  Evidence chunk {ci + 1}/{total_chunks} took {time.time() - t0:.1f}s")
        if not isinstance(result, dict):
            active_params = ensure_run_request_params(client, call_params)
            if (active_params.context_length != call_params.context_length
                    and any(attempt.get("error_category") == "context_budget" for attempt in attempts)):
                return adapt_to_runtime(active_params)
            raise RuntimeError(f"Nickname discovery failed on chunk {ci + 1}/{total_chunks}; "
                               "alias file was not updated")
        return _parse_alias_response(json.dumps(result, ensure_ascii=False), speakers, trusted_aliases)

    all_aliases, all_evidence = {}, {}
    pending_chunks = list(chunks)
    chunk_index = 0
    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        while pending_chunks:
            # Join each wave before snapshotting accumulated aliases for the next.
            snapshot = {**existing_aliases, **all_aliases}
            wave_roster = roster_block
            remaining_budget = budget - len(get_prompt(wave_roster, snapshot, [])) - context_header_chars
            if remaining_budget < 4:
                wave_roster = "\n".join(["SPEAKER LABELS + SAMPLE LINES:"] + [
                    "- " + json.dumps(sp, ensure_ascii=False) for sp in speakers])
                remaining_budget = budget - len(get_prompt(wave_roster, snapshot, [])) - context_header_chars
            if any(sum(len(line) + 1 for line in chunk) > remaining_budget for chunk in pending_chunks):
                passages = [line[2:] for chunk in pending_chunks for line in chunk]
                pending_chunks = _chunk_evidence(passages, remaining_budget)
            if len(get_prompt(wave_roster, snapshot, [])) > budget:
                raise TokenBudgetError("Nickname accumulated aliases exceed the context; increase model context.")
            total_chunks = chunk_index + len(pending_chunks)
            wave = pending_chunks[:concurrency]
            pending_chunks = pending_chunks[concurrency:]
            wave_items = [(chunk_index + ci, ev_lines, snapshot, wave_roster, total_chunks)
                          for ci, ev_lines in enumerate(wave)]
            results = list(executor.map(_process_chunk, wave_items))
            chunk_index += len(wave)
            all_aliases = get_safe_alias_proposals(
                [all_aliases] + [aliases for aliases, _ in results], speakers, trusted_aliases)
            for _, evidence in results:
                all_evidence.update(evidence)

    return all_aliases, all_evidence


def get_existing_aliases(path):
    if not os.path.exists(path):
        return {}
    current = safe_load_json(path, default=None)
    if not isinstance(current, dict):
        raise ValueError("Alias registry must contain a valid JSON object")
    return current


def save_discovered_aliases(path, aliases, roster=()):
    """Merge discovery into the latest registry without replacing human edits."""
    with file_lock(path):
        current = get_existing_aliases(path)
        safe = get_safe_alias_proposals([aliases], roster, current)
        merged = dict(current)
        merged.update(safe)
        if safe or not os.path.exists(path):
            atomic_json_write(merged, path)
    return merged


def main():
    parser = argparse.ArgumentParser(description="Discover character nickname/alias mappings")
    parser.add_argument("--input", help="Script JSON to scan (default: ../annotated_script.json)")
    parser.add_argument("--aliases-file", help="Where to write the alias map (default: ../character_aliases.json)")
    parser.add_argument("--append", action="store_true", help="Compatibility flag; existing aliases are always preserved")
    args = parser.parse_args()

    base = os.path.dirname(os.path.abspath(__file__))
    root = os.path.dirname(base)
    data_dir = get_runtime_data_dir(root)
    script_path = args.input or os.path.join(data_dir, "annotated_script.json")
    aliases_path = args.aliases_file or os.path.join(data_dir, "character_aliases.json")

    if not os.path.exists(script_path):
        print(f"Error: script not found: {script_path}")
        sys.exit(1)

    with open(script_path, "r", encoding="utf-8") as f:
        entries = json.load(f)
    if not isinstance(entries, list) or any(not isinstance(entry, dict) for entry in entries):
        print("Error: script must contain a JSON list of entry objects")
        sys.exit(1)
    print(f"Scanning {len(entries)} entries for character nicknames...")
    existing = {}
    if args.append and os.path.exists(aliases_path):
        try:
            with open(aliases_path, "r", encoding="utf-8") as f:
                existing = get_validated_alias_map(json.load(f))
        except (OSError, ValueError) as error:
            print(f"Error: invalid alias registry: {error}")
            sys.exit(1)

    config_path = get_app_config_path(data_dir, root, base)
    config = load_app_config(config_path)
    llm = get_active_llm_config(config)
    base_url = llm.get("base_url", "")
    client = make_run_client(config, llm, llm_timeout_seconds())
    model_name = llm.get("model_name", "local-model")
    llm_mode = config.get("llm_mode", "local")
    print(f"Using model: {model_name}")

    # Self-heal LM Studio's load settings every run before deciding what
    # context_length is safe to chunk evidence against - covers the case
    # where LM Studio (local or the remote Thunder instance) was restarted
    # since the last run. config["llm"] never actually stores a
    # context_length, so this replaces a previous hardcoded 8192 guess that
    # was disconnected from whatever was really loaded.
    _, status, heal_msg = ensure_ideal_settings(
        llm_mode, base_url, model_name, ssh_alias=config.get("llm_remote_ssh"), api_key=llm.get("api_key"))
    print(heal_msg)

    if status.get("loaded") and status.get("context_length"):
        context_length = status["context_length"]
    else:
        context_length = 4096  # conservative: LM Studio's own real-world default, not an optimistic guess
        if not status.get("loaded"):
            print(f"WARNING: model not loaded; falling back to a conservative "
                  f"context length of {context_length} for evidence chunk sizing.")
        else:
            print(f"WARNING: loaded model reported no context_length; falling back to a "
                  f"conservative {context_length} for evidence chunk sizing.")

    concurrency = get_cached_or_benchmarked_concurrency(
        config_path, llm_mode, base_url, model_name, client,
        ssh_alias=config.get("llm_remote_ssh"), status=status)
    if concurrency > 1:
        print(f"Using concurrency: {concurrency}")

    params = LLMGenParams(
        provider_extra_body=llm.get("provider_extra_body"),
        structured_output=llm.get("structured_output", "auto"),
        api_retry_limit=llm.get("api_retry_limit"),
        retry_initial_delay_seconds=llm.get("retry_initial_delay_seconds", 1),
        retry_multiplier=llm.get("retry_multiplier", 2),
        retry_max_delay_seconds=llm.get("retry_max_delay_seconds", 30),
        retry_jitter=llm.get("retry_jitter", 0.2),
        on_api_exhaustion=llm.get("on_api_exhaustion", "fail"))
    existing = get_existing_aliases(aliases_path)

    aliases, evidence = find_nicknames(client, model_name, entries,
                                       existing_aliases=existing,
                                       context_length=context_length,
                                       concurrency=concurrency, params=params)

    if aliases:
        print(f"\nFound {len(aliases)} nickname/alias mapping(s):")
        for variant, canonical in aliases.items():
            why = evidence.get(variant, "")
            print(f"  '{variant}' -> '{canonical}'" + (f"   ({why})" if why else ""))
    else:
        print("\nNo new nicknames found.")

    merged = save_discovered_aliases(aliases_path, aliases,
                                     roster=collect_context(entries, max_cooccur=0)[0])
    print(f"\nAlias file saved to: {aliases_path} ({len(merged)} total entries)")
    print("Task find_nicknames completed successfully.")


if __name__ == "__main__":
    main()
