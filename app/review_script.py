import os
import sys
import json
import re
import argparse
from openai import OpenAI
from review_prompts import REVIEW_SYSTEM_PROMPT, REVIEW_USER_PROMPT
from generate_script import clean_json_string, repair_json_array, salvage_json_entries


# ── Speaker de-duplication (merge aliases that are the same character) ──────────

SPEAKER_DEDUPE_SYSTEM_PROMPT = (
    "You are an expert at character identity resolution in narrated fiction. "
    "You are given the list of distinct SPEAKER labels found in an audiobook script, "
    "with a few sample lines for each. Some labels refer to the SAME character by "
    "different names (e.g. a first name vs full name vs an epithet like 'the old man' "
    "vs a title). Your job is to group labels that denote the same character and choose "
    "one canonical name for each group.\n\n"
    "Rules:\n"
    "- Only merge labels you are confident are the same individual. When in doubt, keep them separate.\n"
    "- NEVER merge distinct characters just because their names are similar.\n"
    "- NEVER merge a label that denotes MULTIPLE characters speaking together "
    "(e.g. 'RAM AND REM', 'THE TWINS', 'EMILIA/PUCK (DUAL)', 'CROWD', 'CHORUS') into a single "
    "character. Leave any such combined/group label exactly as-is.\n"
    "- Disguises, nicknames, internal monologue, and alternate forms of ONE character DO merge "
    "(e.g. 'SUBARU (INTERNAL)'->'SUBARU', a nickname->the real name).\n"
    "- Keep NARRATOR as its own label; never merge it into a character.\n"
    "- Prefer the clearest proper name as the canonical (usually the most common full name).\n"
    "- If an EXISTING CANONICAL NAMES list is provided, reuse those exact canonical names "
    "when a label refers to one of them, so names stay consistent across books.\n\n"
    "Respond with ONLY a JSON object mapping every label that should change to its canonical "
    'name, like {"Kenji Sato": "KENJI", "the boy": "KENJI"}. Labels that are already canonical '
    "or have no merge must be omitted. No prose, no markdown."
)


def _is_group_label(name):
    """True if a speaker label denotes multiple characters speaking together
    (e.g. 'RAM AND REM', 'EMILIA/PUCK (DUAL)', 'TWINS', 'CROWD'). Such labels must
    never be merged into a single character — that would collapse two voices into one."""
    n = (name or "").upper()
    if "/" in n or "&" in n or "+" in n:
        return True
    if re.search(r"\bAND\b", n):
        return True
    if re.search(r"\b(DUAL|CHORUS|TWINS|CROWD|GROUP|UNISON|BOTH|EVERYONE|TOGETHER|VOICES)\b", n):
        return True
    return False


def _collect_speaker_samples(entries, max_per_speaker=4):
    samples = {}
    for e in entries:
        sp = (e.get("speaker") or e.get("type") or "").strip()
        txt = (e.get("text") or "").strip()
        if not sp or not txt:
            continue
        lines = samples.setdefault(sp, [])
        if len(lines) < max_per_speaker:
            lines.append(txt[:160])
    return samples


def dedupe_speakers(client, model_name, entries, registry_path=None,
                    max_tokens=2000, temperature=0.2):
    """Ask the LLM to merge speaker labels that are the same character.
    Applies the mapping to `entries` in place. If `registry_path` is given, the
    shared canonical names there are fed to the model and updated, so the same
    character keeps one canonical name across multiple books (a series).
    Returns (mapping, renamed_count)."""
    samples = _collect_speaker_samples(entries)
    speakers = sorted(samples.keys())
    if len(speakers) < 2:
        return {}, 0

    # Load existing alias map / cross-book canonical registry. Entries here are
    # treated as KNOWN aliases and applied deterministically (this is how a
    # nickname file from find_nicknames.py gets honored on a re-run).
    registry = {}
    if registry_path and os.path.exists(registry_path):
        try:
            with open(registry_path, "r", encoding="utf-8") as f:
                registry = json.load(f) or {}
        except (json.JSONDecodeError, ValueError, OSError):
            registry = {}
    existing_canonicals = sorted(set(registry.values())) if registry else []

    # Case-insensitive map from a label spelling to the actual label in this script
    label_by_norm = {sp.strip().lower(): sp for sp in samples}

    def _resolve_label(name):
        """Return the actual speaker label matching `name` (case-insensitively), or None."""
        return label_by_norm.get((name or "").strip().lower())

    # Pre-seed forced merges from the alias file for labels present in this script
    forced_map = {}
    for variant, canonical in registry.items():
        actual = _resolve_label(variant)
        if not actual or not canonical or actual == canonical:
            continue
        if actual.upper() == "NARRATOR" or canonical.upper() == "NARRATOR":
            continue
        if _is_group_label(actual) and not _is_group_label(canonical):
            continue
        forced_map[actual] = canonical

    catalog_lines = []
    for sp in speakers:
        sample_str = " | ".join(samples[sp])
        catalog_lines.append(f'- "{sp}": {sample_str}')
    user_parts = []
    if existing_canonicals:
        user_parts.append("EXISTING CANONICAL NAMES (reuse these exact spellings when applicable):")
        user_parts.append(", ".join(existing_canonicals))
        user_parts.append("")
    if forced_map:
        user_parts.append("KNOWN ALIASES (already confirmed — apply these and reuse the spelling):")
        user_parts.append(json.dumps(forced_map, ensure_ascii=False))
        user_parts.append("")
    user_parts.append("SPEAKER LABELS IN THIS SCRIPT:")
    user_parts.extend(catalog_lines)
    user_parts.append("\nReturn the JSON merge map now.")
    user_prompt = "\n".join(user_parts)

    mapping = {}
    try:
        response = client.chat.completions.create(
            model=model_name,
            messages=[
                {"role": "system", "content": SPEAKER_DEDUPE_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=max_tokens,
            temperature=temperature,
        )
        raw = response.choices[0].message.content or ""
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        mapping = json.loads(m.group(0)) if m else {}
    except Exception as e:
        # LLM unavailable — still apply the known aliases from the file
        print(f"  Speaker dedupe LLM step failed ({e}); applying known aliases only.")
        if not forced_map:
            return {}, 0

    # Sanitize: drop no-ops, self-maps, and any NARRATOR merges
    clean_map = {}
    for variant, canonical in mapping.items():
        if not isinstance(variant, str) or not isinstance(canonical, str):
            continue
        variant, canonical = variant.strip(), canonical.strip()
        if not variant or not canonical:
            continue
        actual = _resolve_label(variant)  # case-insensitive match to a real label
        if not actual or actual == canonical:
            continue
        if actual.upper() == "NARRATOR" or canonical.upper() == "NARRATOR":
            continue
        # Never collapse a combined/group label into a single character
        if _is_group_label(actual) and not _is_group_label(canonical):
            print(f"  [skip] '{actual}' looks like a combined/group speaker; not merging into '{canonical}'")
            continue
        clean_map[actual] = canonical

    # Known aliases from the file always win over (or add to) the LLM's suggestions
    clean_map.update(forced_map)

    if not clean_map:
        return {}, 0

    renamed = 0
    for e in entries:
        sp = (e.get("speaker") or e.get("type") or "").strip()
        if sp in clean_map:
            if "speaker" in e:
                e["speaker"] = clean_map[sp]
            else:
                e["type"] = clean_map[sp]
            renamed += 1

    for variant, canonical in clean_map.items():
        print(f"  [MERGE] '{variant}' -> '{canonical}'")

    # Persist updated registry for cross-book consistency. We only store real
    # alias->canonical pairs; canonical self-maps are redundant (each canonical
    # already appears as a value, which is what existing_canonicals reads) and
    # only clutter the user-facing alias editor with "NAME -> NAME" rows.
    if registry_path:
        for variant, canonical in clean_map.items():
            registry[variant] = canonical
        try:
            with open(registry_path, "w", encoding="utf-8") as f:
                json.dump(registry, f, indent=2, ensure_ascii=False)
        except OSError as e:
            print(f"  Warning: could not update alias registry: {e}")

    return clean_map, renamed


def _remap_voice_config(voice_config_path, mapping):
    """Rename keys in a voice_config.json so configured voices follow renamed speakers."""
    if not voice_config_path or not os.path.exists(voice_config_path) or not mapping:
        return 0
    try:
        with open(voice_config_path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    except (json.JSONDecodeError, ValueError, OSError):
        return 0
    moved = 0
    changed = False
    for variant, canonical in mapping.items():
        if variant in cfg:
            # Don't clobber an existing canonical config; only fill if absent
            if canonical not in cfg:
                cfg[canonical] = cfg[variant]
                moved += 1
            del cfg[variant]
            changed = True  # even a delete-only change must be persisted
    if changed:
        try:
            with open(voice_config_path, "w", encoding="utf-8") as f:
                json.dump(cfg, f, indent=2, ensure_ascii=False)
        except OSError:
            pass
    return moved


def _is_section_break(text):
    """Check if text looks like a chapter heading or section title."""
    stripped = text.strip()
    # "CHAPTER ONE", "CHAPTER II", "Chapter Three", etc.
    if re.match(r'(?i)^chapter\b', stripped):
        return True
    # All-caps short text = likely a title ("A SCANDAL IN BOHEMIA", "THE RED-HEADED LEAGUE")
    if stripped == stripped.upper() and len(stripped) < 80 and stripped.isascii():
        return True
    return False


def merge_consecutive_narrators(entries, max_merged_length=800):
    """Merge consecutive NARRATOR entries that share the same instruct value.

    Skips merging across section/chapter breaks. Caps merged text at
    max_merged_length characters to avoid creating overly long TTS entries.
    """
    if not entries:
        return entries, 0

    merged = []
    merges = 0
    i = 0
    while i < len(entries):
        entry = entries[i]

        if entry.get("speaker") != "NARRATOR" or _is_section_break(entry.get("text", "")):
            merged.append(entry)
            i += 1
            continue

        # Start a narrator run — accumulate consecutive NARRATORs with same instruct
        combined_text = entry["text"]
        instruct = entry.get("instruct", "")
        run_count = 1
        j = i + 1

        while j < len(entries):
            next_entry = entries[j]
            if next_entry.get("speaker") != "NARRATOR":
                break
            if next_entry.get("instruct", "") != instruct:
                break
            if _is_section_break(next_entry.get("text", "")):
                break
            candidate = combined_text + " " + next_entry["text"]
            if len(candidate) > max_merged_length:
                break
            combined_text = candidate
            run_count += 1
            j += 1

        merged.append({
            "speaker": "NARRATOR",
            "text": combined_text,
            "instruct": instruct
        })
        if run_count > 1:
            merges += run_count - 1
        i = j

    return merged, merges


def review_batch(client, model_name, batch_entries, batch_num, total_batches,
                 previous_tail=None, source_context=None, max_retries=2,
                 system_prompt=None, user_prompt_template=None,
                 max_tokens=8000, temperature=0.4, top_p=0.8, top_k=20,
                 min_p=0, presence_penalty=0.0, banned_tokens=None):
    """Send a batch of script entries through the LLM for review and correction."""
    sys_prompt = system_prompt or REVIEW_SYSTEM_PROMPT
    usr_template = user_prompt_template or REVIEW_USER_PROMPT

    # Build context
    context_parts = []
    context_parts.append(f"Batch {batch_num} of {total_batches}.")

    if previous_tail:
        context_parts.append("\nPrevious batch ended with:")
        for entry in previous_tail:
            context_parts.append(json.dumps(entry, ensure_ascii=False))

    # Optional extra context (e.g. source snippet or surrounding entries)
    if source_context:
        context_parts.append(f"\nADDITIONAL REVIEW CONTEXT:\n{source_context}")

    context = "\n".join(context_parts)
    batch_json = json.dumps(batch_entries, indent=2, ensure_ascii=False)
    user_prompt = usr_template.format(context=context, batch=batch_json)

    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": sys_prompt},
                    {"role": "user", "content": user_prompt}
                ],
                temperature=temperature,
                top_p=top_p,
                presence_penalty=presence_penalty,
                max_tokens=max_tokens,
                extra_body={
                    k: v for k, v in {
                        "top_k": top_k,
                        "min_p": min_p,
                        "banned_tokens": banned_tokens if banned_tokens else None,
                    }.items() if v is not None
                }
            )

            choice = response.choices[0]
            text = choice.message.content.strip()
            finish_reason = choice.finish_reason
            usage = getattr(response, 'usage', None)

            # Log raw response
            log_dir = os.path.join(os.path.dirname(__file__), "..", "logs")
            os.makedirs(log_dir, exist_ok=True)
            log_path = os.path.join(log_dir, "review_responses.log")
            with open(log_path, "a", encoding="utf-8") as lf:
                lf.write(f"\n{'='*80}\n")
                lf.write(f"BATCH {batch_num}/{total_batches} | attempt {attempt + 1} | finish_reason={finish_reason}\n")
                if usage:
                    lf.write(f"tokens: prompt={getattr(usage, 'prompt_tokens', '?')} completion={getattr(usage, 'completion_tokens', '?')}\n")
                lf.write(f"{'─'*80}\n")
                lf.write(text)
                lf.write(f"\n{'='*80}\n")

            print(f"  finish_reason={finish_reason}", end="")
            if usage:
                print(f" | tokens: prompt={getattr(usage, 'prompt_tokens', '?')} completion={getattr(usage, 'completion_tokens', '?')}", end="")
            print()

            if finish_reason == "length":
                print(f"  WARNING: Response was truncated (hit max_tokens={max_tokens}). Consider increasing max_tokens or reducing batch size.")

        except Exception as e:
            print(f"Error calling LLM API (attempt {attempt + 1}): {e}")
            if attempt < max_retries:
                continue
            return None

        # Clean and parse JSON response
        json_text = clean_json_string(text)

        if not json_text:
            print(f"Warning: Could not find JSON array in batch {batch_num} response (attempt {attempt + 1})")
            if attempt < max_retries:
                print("Retrying...")
                continue
            print(f"Response preview: {text[:300]}...")
            return None

        entries = repair_json_array(json_text)

        if entries and len(entries) > 0:
            if attempt > 0:
                print(f"  Succeeded on retry {attempt + 1}")
            return entries

        print(f"Warning: Could not parse batch {batch_num} response as JSON (attempt {attempt + 1})")

        if attempt < max_retries:
            print("Retrying...")

        # Last resort
        salvaged = salvage_json_entries(json_text)
        if salvaged:
            print(f"Regex-salvaged {len(salvaged)} entries from malformed response")
            return salvaged

    return None


def normalize_text(text):
    """Normalize text for comparison: lowercase, collapse whitespace, strip punctuation."""
    text = text.lower()
    text = re.sub(r'[^\w\s]', '', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def check_text_loss(original_entries, corrected_entries, threshold=0.95, upper_bound=None):
    """Check if corrected entries lost or gained significant text.

    Returns (passed, original_text, corrected_text, ratio).
    passed is True if the corrected word count ratio falls within
    [threshold, upper_bound]. If upper_bound is None, it defaults to
    1.0 + (1.0 - threshold), i.e. symmetric around 1.0.
    """
    orig_words = []
    for e in original_entries:
        orig_words.extend(normalize_text(e.get("text", "")).split())

    corr_words = []
    for e in corrected_entries:
        corr_words.extend(normalize_text(e.get("text", "")).split())

    if not orig_words:
        return True, "", "", 1.0

    orig_joined = " ".join(orig_words)
    corr_joined = " ".join(corr_words)

    ratio = len(corr_words) / len(orig_words) if orig_words else 1.0

    if upper_bound is None:
        upper_bound = 1.0 + (1.0 - threshold)
    passed = threshold <= ratio <= upper_bound
    return passed, orig_joined, corr_joined, ratio


def diff_entries(original, corrected):
    """Compare original and corrected entries, return a summary dict."""
    stats = {
        "text_changed": 0,
        "speaker_changed": 0,
        "instruct_changed": 0,
        "entries_original": len(original),
        "entries_corrected": len(corrected),
    }

    # Compare entry-by-entry up to the shorter length
    compare_len = min(len(original), len(corrected))
    for i in range(compare_len):
        orig = original[i]
        corr = corrected[i]
        if orig.get("text") != corr.get("text"):
            stats["text_changed"] += 1
        if orig.get("speaker") != corr.get("speaker"):
            stats["speaker_changed"] += 1
        if orig.get("instruct") != corr.get("instruct"):
            stats["instruct_changed"] += 1

    return stats


def main():
    parser = argparse.ArgumentParser(description="Review and fix annotated audiobook script")
    parser.add_argument("--source", help="Path to original source text for comparison (mode 2, not yet implemented)")
    parser.add_argument("--context-window", type=int, default=0,
                        help="If > 0, review each entry with +/- N neighboring entries for better segmentation and speaker fixes")
    parser.add_argument("--input", help="Path to the script JSON to review (default: ../annotated_script.json)")
    parser.add_argument("--output", help="Where to write the reviewed script (default: same as --input)")
    parser.add_argument("--dedupe-speakers", action="store_true",
                        help="Merge speaker labels that refer to the same character (alias resolution)")
    parser.add_argument("--alias-registry",
                        help="Path to a shared canonical-name JSON file so merges stay consistent across books")
    parser.add_argument("--remap-voice-config",
                        help="Path to a voice_config.json whose keys should follow renamed speakers")
    args = parser.parse_args()

    # Locate the script to review (default: working annotated_script.json)
    default_script = os.path.join(os.path.dirname(__file__), "..", "annotated_script.json")
    script_path = args.input or default_script
    output_path = args.output or script_path
    if not os.path.exists(script_path):
        print(f"Error: script not found: {script_path}. Generate a script first.")
        sys.exit(1)

    with open(script_path, "r", encoding="utf-8") as f:
        entries = json.load(f)

    print(f"Loaded {len(entries)} script entries for review")

    # Load source text if provided (mode 2 prep)
    source_text = None
    if args.source:
        if os.path.exists(args.source):
            with open(args.source, "r", encoding="utf-8") as f:
                source_text = f.read()
            print(f"Loaded source text: {len(source_text)} chars")
        else:
            print(f"Warning: Source file not found: {args.source}")

    # Load config
    config_path = os.path.join(os.path.dirname(__file__), "config.json")
    config = {}
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                config = json.load(f)
        except Exception as e:
            print(f"Warning: Failed to load config.json: {e}")
    else:
        print("Warning: config.json not found. Using defaults.")

    llm_config = config.get("llm", {})
    base_url = llm_config.get("base_url", "http://localhost:11434/v1")
    api_key = llm_config.get("api_key", "local")
    model_name = llm_config.get("model_name", "local-model")

    # Load custom review prompts or use defaults from review_prompts.txt
    prompts_config = config.get("prompts", {})
    review_sys = prompts_config.get("review_system_prompt") or REVIEW_SYSTEM_PROMPT
    review_usr = prompts_config.get("review_user_prompt") or REVIEW_USER_PROMPT

    generation_config = config.get("generation", {})
    batch_size = generation_config.get("review_batch_size", 25)
    max_tokens = generation_config.get("max_tokens", 8000)
    temperature = generation_config.get("temperature", 0.4)
    top_p = generation_config.get("top_p", 0.8)
    top_k = generation_config.get("top_k", 20)
    min_p = generation_config.get("min_p", 0)
    presence_penalty = generation_config.get("presence_penalty", 0.0)
    banned_tokens = generation_config.get("banned_tokens", [])

    print(f"Connecting to: {base_url}")
    print(f"Using model: {model_name}")
    print(f"Batch size: {batch_size} entries, Max tokens: {max_tokens}")
    if banned_tokens:
        print(f"Banned tokens: {banned_tokens}")

    client = OpenAI(base_url=base_url, api_key=api_key)

    all_corrected = []
    total_stats = {
        "text_changed": 0,
        "speaker_changed": 0,
        "instruct_changed": 0,
        "entries_added": 0,
        "entries_removed": 0,
        "batches_failed": 0,
    }

    if args.context_window and args.context_window > 0:
        window = max(1, args.context_window)
        total_batches = max(1, (len(entries) + batch_size - 1) // batch_size)
        print(f"Contextual review mode enabled: batching ~{batch_size} entries per LLM call with +/-{window} neighbors")

        previous_tail = None
        for batch_index, start in enumerate(range(0, len(entries), batch_size), 1):
            end = min(len(entries), start + batch_size)
            batch = entries[start:end]
            before = entries[max(0, start - window):start]
            after = entries[end:min(len(entries), end + window)]

            print(f"\nReviewing batch {batch_index}/{total_batches} ({len(batch)} entries)...")

            contextual_lines = [
                "Contextual batch review mode.",
                "The 'SCRIPT ENTRIES TO REVIEW' below is your TARGET BATCH.",
                "Use the following PREVIOUS and NEXT entries for context, but DO NOT include them in your output. Only return the corrected TARGET BATCH.",
            ]
            if before:
                contextual_lines.append("\n--- PREVIOUS ENTRIES (Context Only) ---")
                contextual_lines.extend(json.dumps(e, ensure_ascii=False) for e in before)
            if after:
                contextual_lines.append("\n--- NEXT ENTRIES (Context Only) ---")
                contextual_lines.extend(json.dumps(e, ensure_ascii=False) for e in after)

            corrected = review_batch(
                client, model_name, batch, batch_index, total_batches,
                previous_tail=None,  # contextual mode uses explicit before/after window instead
                source_context="\n".join(contextual_lines),
                system_prompt=review_sys,
                user_prompt_template=review_usr,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                min_p=min_p,
                presence_penalty=presence_penalty,
                banned_tokens=banned_tokens
            )

            if corrected is None:
                print(f"  FAILED — keeping original entries for batch {batch_index}")
                all_corrected.extend(batch)
                total_stats["batches_failed"] += 1
                previous_tail = batch[-2:] if len(batch) >= 2 else batch
                continue

            passed, orig_text, corr_text, ratio = check_text_loss(batch, corrected, threshold=0.95, upper_bound=1.15)
            if not passed:
                print(f"  WARNING: Text length mismatch (loss or gain)! Word ratio: {ratio:.2f} (acceptable range: 0.95-1.15)")
                print(f"  Original words: {len(orig_text.split())}, Corrected words: {len(corr_text.split())}")
                print(f"  Keeping original entries for batch {batch_index} to prevent data corruption.")
                all_corrected.extend(batch)
                total_stats["batches_failed"] += 1
                previous_tail = batch[-2:] if len(batch) >= 2 else batch
                continue

            stats = diff_entries(batch, corrected)
            entry_diff = len(corrected) - len(batch)

            if entry_diff > 0:
                total_stats["entries_added"] += entry_diff
            elif entry_diff < 0:
                total_stats["entries_removed"] += abs(entry_diff)

            total_stats["text_changed"] += stats["text_changed"]
            total_stats["speaker_changed"] += stats["speaker_changed"]
            total_stats["instruct_changed"] += stats["instruct_changed"]

            changes = stats["text_changed"] + stats["speaker_changed"] + stats["instruct_changed"]
            if changes > 0 or entry_diff != 0:
                print(f"  Changes: {stats['text_changed']} text, {stats['speaker_changed']} speaker, {stats['instruct_changed']} instruct", end="")
                if entry_diff > 0:
                    print(f", +{entry_diff} entries (split)")
                elif entry_diff < 0:
                    print(f", {entry_diff} entries (merge)")
                else:
                    print()
            else:
                print("  No changes")

            all_corrected.extend(corrected)
            previous_tail = corrected[-2:] if len(corrected) >= 2 else corrected
    else:
        # Split entries into batches
        batches = []
        for i in range(0, len(entries), batch_size):
            batches.append(entries[i:i + batch_size])

        total_batches = len(batches)
        print(f"Split into {total_batches} batches of ~{batch_size} entries")

        previous_tail = None

        for i, batch in enumerate(batches, 1):
            print(f"\nReviewing batch {i}/{total_batches} ({len(batch)} entries)...")

            corrected = review_batch(
                client, model_name, batch, i, total_batches,
                previous_tail=previous_tail,
                source_context=None,  # Mode 2: would pass source text chunk here
                system_prompt=review_sys,
                user_prompt_template=review_usr,
                max_tokens=max_tokens,
                temperature=temperature,
                top_p=top_p,
                top_k=top_k,
                min_p=min_p,
                presence_penalty=presence_penalty,
                banned_tokens=banned_tokens
            )

            if corrected is None:
                print(f"  FAILED — keeping original entries for batch {i}")
                all_corrected.extend(batch)
                total_stats["batches_failed"] += 1
                previous_tail = batch[-2:] if len(batch) >= 2 else batch
                continue

            # Text-loss safety check
            passed, orig_text, corr_text, ratio = check_text_loss(batch, corrected)
            if not passed:
                print(f"  WARNING: Text length mismatch (loss or gain)! Word ratio: {ratio:.2f} (acceptable range: 0.95-1.05)")
                print(f"  Original words: {len(orig_text.split())}, Corrected words: {len(corr_text.split())}")
                print(f"  Keeping original entries for batch {i} to prevent data corruption.")
                all_corrected.extend(batch)
                total_stats["batches_failed"] += 1
                previous_tail = batch[-2:] if len(batch) >= 2 else batch
                continue

            # Diff stats
            stats = diff_entries(batch, corrected)
            entry_diff = len(corrected) - len(batch)

            if entry_diff > 0:
                total_stats["entries_added"] += entry_diff
            elif entry_diff < 0:
                total_stats["entries_removed"] += abs(entry_diff)

            total_stats["text_changed"] += stats["text_changed"]
            total_stats["speaker_changed"] += stats["speaker_changed"]
            total_stats["instruct_changed"] += stats["instruct_changed"]

            changes = stats["text_changed"] + stats["speaker_changed"] + stats["instruct_changed"]
            if changes > 0 or entry_diff != 0:
                print(f"  Changes: {stats['text_changed']} text, {stats['speaker_changed']} speaker, {stats['instruct_changed']} instruct", end="")
                if entry_diff > 0:
                    print(f", +{entry_diff} entries (splits)")
                elif entry_diff < 0:
                    print(f", {entry_diff} entries (merges)")
                else:
                    print()
            else:
                print(f"  No changes")

            all_corrected.extend(corrected)
            previous_tail = corrected[-2:] if len(corrected) >= 2 else corrected

    # Post-processing: merge consecutive NARRATOR entries with same instruct
    merge_narrators_enabled = generation_config.get("merge_narrators", False)
    narrator_merges = 0
    if merge_narrators_enabled:
        pre_merge_count = len(all_corrected)
        all_corrected, narrator_merges = merge_consecutive_narrators(all_corrected, max_merged_length=800)
        if narrator_merges > 0:
            print(f"\nPost-processing: merged {narrator_merges} consecutive narrator entries "
                  f"({pre_merge_count} -> {len(all_corrected)} entries)")
    else:
        print("\nNarrator merging: disabled (enable in Setup > Advanced)")

    # Speaker de-duplication: merge aliases that are the same character
    speakers_merged = 0
    if args.dedupe_speakers:
        print("\nResolving character aliases (merging duplicate names)...")
        dedupe_map, speakers_merged = dedupe_speakers(
            client, model_name, all_corrected, registry_path=args.alias_registry
        )
        if speakers_merged > 0:
            print(f"Merged {len(dedupe_map)} alias label(s), updating {speakers_merged} entries.")
            moved = _remap_voice_config(args.remap_voice_config, dedupe_map)
            if moved:
                print(f"Remapped {moved} voice config entr(y/ies) to canonical names.")
        else:
            print("No duplicate character names found.")

    # Write corrected script
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(all_corrected, f, indent=2, ensure_ascii=False)

    # Delete chunks.json so editor regenerates — only when we reviewed the
    # working script (batch review of saved scripts must not touch it).
    if os.path.abspath(output_path) == os.path.abspath(default_script):
        chunks_path = os.path.join(os.path.dirname(__file__), "..", "chunks.json")
        if os.path.exists(chunks_path):
            os.remove(chunks_path)
            print("Cleared old chunks.json")

    # Final summary
    total_changes = (total_stats["text_changed"] + total_stats["speaker_changed"] +
                     total_stats["instruct_changed"] + total_stats["entries_added"] +
                     total_stats["entries_removed"] + narrator_merges + speakers_merged)

    print(f"\n{'='*60}")
    print(f"Review complete: {len(entries)} -> {len(all_corrected)} entries")
    print(f"  Text changed:    {total_stats['text_changed']}")
    print(f"  Speaker changed: {total_stats['speaker_changed']}")
    print(f"  Instruct changed:{total_stats['instruct_changed']}")
    print(f"  Entries added:   {total_stats['entries_added']}")
    print(f"  Entries removed: {total_stats['entries_removed']}")
    print(f"  Narrators merged:{narrator_merges}")
    print(f"  Speakers merged: {speakers_merged}")
    if total_stats["batches_failed"] > 0:
        print(f"  Batches failed:  {total_stats['batches_failed']}")
    print(f"  Total changes:   {total_changes}")
    print(f"{'='*60}")

    if total_changes == 0:
        print("No issues found -- script looks clean.")
    else:
        print(f"Fixed {total_changes} issues across {total_batches} batches.")

    print(f"Output saved to: {output_path}")
    print("Task review completed successfully.")


if __name__ == "__main__":
    main()
