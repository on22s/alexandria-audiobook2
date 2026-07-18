import os
import sys
import json
import re
import time
import difflib
import subprocess
import argparse
import threading
from concurrent.futures import ThreadPoolExecutor
from openai import OpenAI
from config_settings import load_app_config
from llm_bench import get_cached_or_benchmarked_concurrency
from review_prompts import REVIEW_SYSTEM_PROMPT, REVIEW_USER_PROMPT
from generate_script import LLMGenParams, call_llm_for_entries
from lmstudio_settings import ensure_ideal_settings, get_current_status, get_effective_max_tokens
from utils import file_lock, atomic_json_write, safe_load_json, run_rocm_smi_json, extract_json_object, warn_unparseable_llm_json, get_runtime_data_dir, get_app_config_path


# ── GPU VRAM watchdog ────────────────────────────────────────────────────────
# A long batch review run can grow VRAM usage (KV cache + other GPU apps) until
# the driver runs out of memory and crashes the whole display server. Before
# each batch we check headroom and pause/abort rather than let that happen.

VRAM_WARN_THRESHOLD = 0.90  # pause once VRAM usage crosses this fraction
VRAM_MAX_WAIT = 180         # seconds to wait for headroom before giving up
VRAM_POLL_INTERVAL = 15     # seconds between checks while waiting


def get_vram_usage():
    """Return (worst_used_bytes, worst_total_bytes) for the most constrained GPU.

    On multi-GPU systems, returns the card with the highest VRAM utilization ratio
    so the headroom check doesn't pass on GPU 0 while GPU 1 is saturated.
    Supports both AMD (rocm-smi) and NVIDIA (nvidia-smi) GPUs.
    """
    # Try AMD first
    data = run_rocm_smi_json(["--showmeminfo", "vram"])
    if data is not None:
        worst_ratio = 0.0
        worst_used = 0
        worst_total = 0

        for card_data in data.values():
            if not isinstance(card_data, dict):
                continue
            try:
                used = int(card_data.get("VRAM Total Used Memory (B)", 0))
                total = int(card_data.get("VRAM Total Memory (B)", 0))
                if total <= 0:
                    continue
                ratio = used / total
                # Track the GPU with the highest utilization ratio
                if ratio > worst_ratio:
                    worst_ratio = ratio
                    worst_used = used
                    worst_total = total
            except (ValueError, TypeError):
                continue

        if worst_total > 0:
            return worst_used, worst_total
    
    # Try NVIDIA
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.used,memory.total", "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            worst_ratio = 0.0
            worst_used = 0
            worst_total = 0
            
            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split(',')
                if len(parts) != 2:
                    continue
                try:
                    # nvidia-smi returns values in MiB
                    used_mib = float(parts[0].strip())
                    total_mib = float(parts[1].strip())
                    used = int(used_mib * 1024 * 1024)  # Convert to bytes
                    total = int(total_mib * 1024 * 1024)
                    if total <= 0:
                        continue
                    ratio = used / total
                    if ratio > worst_ratio:
                        worst_ratio = ratio
                        worst_used = used
                        worst_total = total
                except (ValueError, TypeError):
                    continue
            
            if worst_total > 0:
                return worst_used, worst_total
    except Exception:
        pass  # nvidia-smi not available either
    
    return None


def wait_for_vram_headroom():
    """Pause if GPU VRAM usage is above VRAM_WARN_THRESHOLD, polling until it drops
    or VRAM_MAX_WAIT elapses. Returns True if safe to proceed, False if VRAM is
    still saturated after waiting (caller should save progress and stop)."""
    usage = get_vram_usage()
    if usage is None:
        return True  # rocm-smi unavailable (e.g. non-AMD GPU) - don't block on it

    used, total = usage
    if used / total < VRAM_WARN_THRESHOLD:
        return True

    print(f"  WARNING: GPU VRAM at {used/total:.0%} ({used/1e9:.1f}/{total/1e9:.1f} GB) "
          f"- pausing to avoid an OOM crash...")
    waited = 0
    last_reported = 0
    while waited < VRAM_MAX_WAIT:
        time.sleep(VRAM_POLL_INTERVAL)
        waited += VRAM_POLL_INTERVAL
        
        # Report progress every 30 seconds so user knows we're still waiting
        if waited - last_reported >= 30:
            print(f"  Still waiting for VRAM to drop... ({waited}s elapsed, max {VRAM_MAX_WAIT}s)")
            last_reported = waited
        
        usage = get_vram_usage()
        if usage is None:
            return True
        used, total = usage
        if used / total < VRAM_WARN_THRESHOLD:
            print(f"  VRAM back to {used/total:.0%} after {waited}s - resuming.")
            return True

    print(f"  VRAM still at {used/total:.0%} after {VRAM_MAX_WAIT}s - "
          f"stopping early to avoid a crash. Progress so far will be saved.")
    return False


# ── Resume support ───────────────────────────────────────────────────────────
# A long batch review run that stops early (VRAM, crash, kill) checkpoints its
# progress so the next run can pick up where it left off instead of redoing
# every batch from scratch.

def _checkpoint_path(output_path):
    return output_path + ".review_checkpoint.json"


def load_checkpoint(output_path, total_batches, batch_size, context_window):
    """Return checkpoint dict if one exists and matches this run's parameters.

    If earlier batches failed (and were filled in with their original,
    unreviewed entries as a fallback), rewind the checkpoint to just before
    the earliest such failure so those batches get retried on this run
    instead of being permanently skipped. The rewind only runs when
    `batch_lengths` fully covers `completed_batches` - true for any
    checkpoint written entirely by this version; older checkpoints (missing
    these fields) just keep their previous resume-from-completed behavior.

    `total_batches` can legitimately drift between runs - earlier batches may
    have added/removed entries (splits/merges), changing the total entry
    count - so a mismatch here no longer discards the checkpoint. Only
    `batch_size`/`context_window` (which the resumed batching logic can't
    safely mix mid-run) do that. The caller re-derives this run's
    `total_batches` from `len(all_corrected)` and the current entry count.
    """
    path = _checkpoint_path(output_path)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
    if (data.get("batch_size") != batch_size or
            data.get("context_window") != context_window):
        print("Found a review checkpoint but parameters changed - starting fresh.")
        return None
    if data.get("total_batches") != total_batches:
        print("Note: entry count changed since the checkpoint was saved "
              "(earlier batches added/removed entries) - resuming from the "
              "saved progress anyway.")

    data.setdefault("batch_lengths", [])
    data.setdefault("failed_batches", [])
    data.setdefault("total_stats", {})
    data["total_stats"].setdefault("batches_skipped_vram", 0)
    data["total_stats"].setdefault("entries_changed", 0)

    failed_batches = sorted(data["failed_batches"])
    batch_lengths = data["batch_lengths"]
    completed_batches = data["completed_batches"]
    if failed_batches and len(batch_lengths) == completed_batches:
        retry_from = failed_batches[0]
        # Keep entries from all batches before the first failed one
        # retry_from is 1-indexed, so batch_lengths[:retry_from - 1] gives us
        # the lengths of batches 1 through (retry_from - 1)
        keep_count = max(0, retry_from - 1)
        keep_entries = sum(batch_lengths[:keep_count])
        all_corrected = data["all_corrected"][:keep_entries]
        data["all_corrected"] = all_corrected
        data["completed_batches"] = keep_count
        data["batch_lengths"] = batch_lengths[:keep_count]
        data["previous_tail"] = all_corrected[-2:] if all_corrected else None
        data["total_stats"]["batches_failed"] = max(
            0, data["total_stats"].get("batches_failed", 0) - len(failed_batches))
        data["failed_batches"] = []
        plural = "es" if len(failed_batches) != 1 else ""
        print(f"Found {len(failed_batches)} previously-failed batch{plural} - "
              f"rewinding checkpoint to retry starting at batch {retry_from}.")

    return data


def save_checkpoint(output_path, completed_batches, total_batches, batch_size,
                     context_window, all_corrected, total_stats, previous_tail,
                     batch_lengths, failed_batches):
    path = _checkpoint_path(output_path)
    data = {
        "completed_batches": completed_batches,
        "total_batches": total_batches,
        "batch_size": batch_size,
        "context_window": context_window,
        "all_corrected": all_corrected,
        "total_stats": total_stats,
        "previous_tail": previous_tail,
        "batch_lengths": batch_lengths,
        "failed_batches": failed_batches,
    }
    # Use atomic_json_write for consistent cross-platform behavior with Windows retry logic
    try:
        atomic_json_write(data, path)
    except OSError as e:
        # If checkpoint save fails (e.g., disk full, permission denied),
        # log the error but don't crash the review process
        print(f"WARNING: Failed to save checkpoint: {e}. Review will continue but resume may not work.")


def clear_checkpoint(output_path):
    """Remove checkpoint file with file_lock to prevent race conditions."""
    path = _checkpoint_path(output_path)
    if os.path.exists(path):
        try:
            # Use file_lock to coordinate with concurrent reads/writes
            with file_lock(path):
                os.remove(path)
        except (TimeoutError, OSError) as e:
            # If the file is still there, removal genuinely failed (lock
            # contention or e.g. permission error) - warn, since a stale
            # checkpoint can make the next run resume from the wrong place.
            if os.path.exists(path):
                print(f"WARNING: Failed to clear checkpoint {path}: {e}. "
                      f"A stale checkpoint may cause the next run to resume incorrectly.")


def _load_resume_state(output_path, total_batches_estimate, batch_size, context_window,
                        all_corrected, total_stats):
    """Load a checkpoint (if any) and compute where this run should resume from.

    Shared by main()'s contextual and non-contextual review loops, which
    otherwise duplicate this checkpoint-load + resume_offset bookkeeping.

    Returns (all_corrected, total_stats, previous_tail, completed_batches,
    batch_lengths, failed_batches, resume_offset, checkpoint).
    """
    previous_tail = None
    completed_batches = 0
    batch_lengths = []
    failed_batches = []
    checkpoint = load_checkpoint(output_path, total_batches_estimate, batch_size, context_window)
    if checkpoint:
        completed_batches = checkpoint["completed_batches"]
        all_corrected = checkpoint["all_corrected"]
        total_stats = checkpoint["total_stats"]
        previous_tail = checkpoint["previous_tail"]
        batch_lengths = checkpoint["batch_lengths"]
        failed_batches = checkpoint["failed_batches"]

    # Resume from where `all_corrected` actually leaves off, not from
    # `completed_batches * batch_size` - earlier batches may have
    # added/removed entries, so those two can diverge. `entries` is the
    # previous run's output (all_corrected + unreviewed remainder), so
    # `entries[:resume_offset] == all_corrected`.
    resume_offset = len(all_corrected)
    return (all_corrected, total_stats, previous_tail, completed_batches,
            batch_lengths, failed_batches, resume_offset, checkpoint)


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
                    max_tokens=2000, temperature=0.2, context_length=None):
    """Ask the LLM to merge speaker labels that are the same character.
    Does not mutate `entries` - the caller applies the returned changes. If
    `registry_path` is given, the shared canonical names there are fed to the
    model and updated, so the same character keeps one canonical name across
    multiple books (a series).
    Returns (mapping, renamed_count, changes), where changes is a list of
    (index, key, new_value) tuples to apply to the caller's own entries list."""
    samples = _collect_speaker_samples(entries)
    speakers = sorted(samples.keys())
    if len(speakers) < 2:
        return {}, 0, []

    # Load existing alias map / cross-book canonical registry. Entries here are
    # treated as KNOWN aliases and applied deterministically (this is how a
    # nickname file from find_nicknames.py gets honored on a re-run).
    registry = {}
    if registry_path and os.path.exists(registry_path):
        try:
            with open(registry_path, "r", encoding="utf-8") as f:
                registry = json.load(f) or {}
        except (json.JSONDecodeError, ValueError, OSError) as e:
            print(f"Warning: corrupted alias registry at {registry_path}, resetting to empty: {e}")
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
        messages = [
            {"role": "system", "content": SPEAKER_DEDUPE_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ]
        response = client.chat.completions.create(
            model=model_name,
            messages=messages,
            max_tokens=get_effective_max_tokens(
                max_tokens, context_length, messages, hard_max=12000),
            temperature=temperature,
        )
        raw = response.choices[0].message.content or ""
        mapping = extract_json_object(raw)
        if mapping is None:
            warn_unparseable_llm_json("speaker-merge", raw, "applying known aliases only")
            mapping = {}
    except Exception as e:
        # LLM unavailable — still apply the known aliases from the file
        print(f"  Speaker dedupe LLM step failed ({e}); applying known aliases only.")
        if not forced_map:
            return {}, 0, []

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
        return {}, 0, []

    renamed = 0
    changes = []
    for i, e in enumerate(entries):
        sp = (e.get("speaker") or e.get("type") or "").strip()
        if sp in clean_map:
            key = "speaker" if "speaker" in e else "type"
            changes.append((i, key, clean_map[sp]))
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
            # Use file_lock to prevent concurrent writes from corrupting the registry
            with file_lock(registry_path):
                atomic_json_write(registry, registry_path)
        except (OSError, TimeoutError) as e:
            print(f"  Warning: could not update alias registry: {e}")

    return clean_map, renamed, changes


def _remap_voice_config(voice_config_path, mapping):
    """Rename keys in a voice_config.json so configured voices follow renamed speakers."""
    if not voice_config_path or not os.path.exists(voice_config_path) or not mapping:
        return 0
    # Hold the lock across the whole read-modify-write so this can't lose an
    # update to (or clobber) a concurrent write from the UI, e.g. "apply cast
    # to multiple books" writing the same companion voice_config.json.
    try:
        with file_lock(voice_config_path):
            cfg = safe_load_json(voice_config_path)
            if cfg is None:
                return 0
            moved = 0
            changed = False
            for variant, canonical in mapping.items():
                if variant in cfg:
                    # If canonical doesn't exist yet, just move it
                    if canonical not in cfg:
                        cfg[canonical] = cfg[variant]
                        moved += 1
                    else:
                        # Canonical already exists - merge variant's config into it
                        # Preserve variant-specific settings by updating only missing keys
                        existing = cfg[canonical]
                        variant_cfg = cfg[variant]
                        if isinstance(existing, dict) and isinstance(variant_cfg, dict):
                            # Update existing with variant's values, preferring existing for conflicts
                            for key, value in variant_cfg.items():
                                if key not in existing:
                                    existing[key] = value
                        elif isinstance(variant_cfg, dict):
                            # canonical's entry is corrupted/non-dict - variant's dict wins
                            cfg[canonical] = variant_cfg
                        # else: neither side is a usable dict - keep canonical's existing value
                        moved += 1
                    del cfg[variant]
                    changed = True  # even a delete-only change must be persisted
            if changed:
                try:
                    atomic_json_write(cfg, voice_config_path)
                except OSError as e:
                    print(f"  Warning: failed to write {voice_config_path}: {e}")
                    moved = 0
    except TimeoutError as e:
        print(f"  Warning: could not lock {voice_config_path} for speaker remap ({e}); skipping.")
        return 0
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


def review_batch(client, model_name, batch_entries, batch_num, total_batches, params,
                 previous_tail=None, source_context=None, max_retries=2,
                 attempt_observer=None):
    """Send a batch of script entries through the LLM for review and correction.

    Returns the corrected entries, or None if every attempt failed (so the caller
    can keep the originals and retry on the next resume).
    """
    sys_prompt = params.system_prompt or REVIEW_SYSTEM_PROMPT
    usr_template = params.user_prompt_template or REVIEW_USER_PROMPT

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

    entries = call_llm_for_entries(
        client, model_name, sys_prompt, user_prompt, params,
        log_name="review_responses.log",
        label=f"BATCH {batch_num}/{total_batches}",
        max_retries=max_retries,
        attempt_observer=attempt_observer,
    )
    return entries or None


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
        # Use regex split to handle all Unicode whitespace properly
        text = normalize_text(e.get("text", ""))
        words = re.split(r'\s+', text.strip())
        orig_words.extend([w for w in words if w])  # Filter empty strings

    corr_words = []
    for e in corrected_entries:
        text = normalize_text(e.get("text", ""))
        words = re.split(r'\s+', text.strip())
        corr_words.extend([w for w in words if w])

    if not orig_words:
        return True, "", "", 1.0

    orig_joined = " ".join(orig_words)
    corr_joined = " ".join(corr_words)

    ratio = len(corr_words) / len(orig_words) if orig_words else 1.0

    if upper_bound is None:
        upper_bound = 1.0 + (1.0 - threshold)
    passed = threshold <= ratio <= upper_bound
    return passed, orig_joined, corr_joined, ratio


_MAX_HIGHLIGHT_POOL = 500
_HIGHLIGHT_SNIPPET_LEN = 300
_HIGHLIGHT_CONTEXT_LEN = 160


def get_failed_section(batch, zero_based_start, length, category, word_ratio=None):
    """Return structured metadata for one batch whose original entries were kept."""
    section = {
        "batch": batch,
        "entry_start": zero_based_start + 1,
        "entry_end": zero_based_start + length,
        "category": category,
    }
    if word_ratio is not None:
        section["word_ratio"] = round(word_ratio, 4)
    return section


def diff_entries(original, corrected, highlight_pool=None, entry_offset=0):
    """Compare original and corrected entries, return a summary dict.

    If highlight_pool is given (a dict with "text" and "speaker" lists), append
    notable per-entry before/after snippets to it for the "diff preview" report,
    capped at _MAX_HIGHLIGHT_POOL each so a huge book can't blow up memory.

    Entries are aligned by their text before paired fields are compared. This
    keeps a split or merge from making every later entry look changed merely
    because its list index moved.
    """
    stats = {
        "text_changed": 0,
        "speaker_changed": 0,
        "instruct_changed": 0,
        "entries_changed": 0,
        "entries_added": 0,
        "entries_removed": 0,
        "entries_original": len(original),
        "entries_corrected": len(corrected),
    }
    changed_entry_indices = set()

    def compare_pair(orig, corr, orig_index):
        orig_text = orig.get("text", "")
        corr_text = corr.get("text", "")
        orig_speaker = orig.get("speaker") or ""
        corr_speaker = corr.get("speaker") or ""

        if orig_text != corr_text:
            changed_entry_indices.add(orig_index)
            stats["text_changed"] += 1
            if highlight_pool is not None and len(highlight_pool["text"]) < _MAX_HIGHLIGHT_POOL:
                ratio = difflib.SequenceMatcher(None, orig_text, corr_text).ratio()
                highlight_pool["text"].append({
                    "speaker": corr_speaker or orig_speaker or "Narrator",
                    "before": orig_text[:_HIGHLIGHT_SNIPPET_LEN],
                    "after": corr_text[:_HIGHLIGHT_SNIPPET_LEN],
                    "magnitude": round(1.0 - ratio, 4),
                })
        if orig_speaker != corr_speaker:
            changed_entry_indices.add(orig_index)
            stats["speaker_changed"] += 1
            if highlight_pool is not None and len(highlight_pool["speaker"]) < _MAX_HIGHLIGHT_POOL:
                speaker_change = {
                    "text": (corr_text or orig_text)[:_HIGHLIGHT_SNIPPET_LEN],
                    "before": orig_speaker or "(unknown)",
                    "after": corr_speaker or "(unknown)",
                    "entry_number": entry_offset + orig_index + 1,
                    "context_before": (
                        original[orig_index - 1].get("text", "")[:_HIGHLIGHT_CONTEXT_LEN]
                        if orig_index > 0 else ""
                    ),
                    "context_after": (
                        original[orig_index + 1].get("text", "")[:_HIGHLIGHT_CONTEXT_LEN]
                        if orig_index + 1 < len(original) else ""
                    ),
                }
                if (orig_speaker.casefold() == "narrator" and
                        corr_speaker.casefold() != "narrator"):
                    speaker_change["manual_review_reason"] = (
                        "Narrator-to-character changes alter which voice reads the line."
                    )
                highlight_pool["speaker"].append(speaker_change)
        if orig.get("instruct") != corr.get("instruct"):
            changed_entry_indices.add(orig_index)
            stats["instruct_changed"] += 1

    original_texts = [entry.get("text", "") for entry in original]
    corrected_texts = [entry.get("text", "") for entry in corrected]
    matcher = difflib.SequenceMatcher(None, original_texts, corrected_texts, autojunk=False)
    for tag, orig_start, orig_end, corr_start, corr_end in matcher.get_opcodes():
        if tag == "equal":
            for offset, (orig, corr) in enumerate(zip(
                    original[orig_start:orig_end], corrected[corr_start:corr_end])):
                compare_pair(orig, corr, orig_start + offset)
            continue
        if tag == "delete":
            stats["entries_removed"] += orig_end - orig_start
            continue
        if tag == "insert":
            stats["entries_added"] += corr_end - corr_start
            continue

        original_block = original[orig_start:orig_end]
        corrected_block = corrected[corr_start:corr_end]
        paired = min(len(original_block), len(corrected_block))
        for offset, (orig, corr) in enumerate(zip(
                original_block[:paired], corrected_block[:paired])):
            compare_pair(orig, corr, orig_start + offset)
        stats["entries_removed"] += len(original_block) - paired
        stats["entries_added"] += len(corrected_block) - paired

    stats["entries_changed"] = len(changed_entry_indices)
    return stats


def main():
    parser = argparse.ArgumentParser(description="Review and fix annotated audiobook script")
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
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app_dir = os.path.dirname(__file__)
    data_dir = get_runtime_data_dir(root)
    default_script = os.path.join(data_dir, "annotated_script.json")
    script_path = args.input or default_script
    output_path = args.output or script_path
    if not os.path.exists(script_path):
        print(f"Error: script not found: {script_path}. Generate a script first.")
        sys.exit(1)

    with open(script_path, "r", encoding="utf-8") as f:
        entries = json.load(f)

    print(f"Loaded {len(entries)} script entries for review")

    # Load config
    config_path = get_app_config_path(data_dir, root, app_dir)
    if not os.path.exists(config_path):
        print("Warning: config.json not found. Using defaults.")
    config = load_app_config(config_path)

    llm_config = config.get("llm", {})
    base_url = llm_config.get("base_url", "http://localhost:11434/v1")
    api_key = llm_config.get("api_key", "local")
    model_name = llm_config.get("model_name", "local-model")

    # Load custom review prompts or use defaults from review_prompts.txt
    prompts_config = config.get("prompts") or {}
    review_sys = prompts_config.get("review_system_prompt") or REVIEW_SYSTEM_PROMPT
    review_usr = prompts_config.get("review_user_prompt") or REVIEW_USER_PROMPT

    generation_config = config.get("generation") or {}
    batch_size = generation_config.get("review_batch_size", 25)
    max_tokens = generation_config.get("max_tokens", 8000)
    temperature = generation_config.get("temperature", 0.4)
    top_p = generation_config.get("top_p", 0.8)
    top_k = generation_config.get("top_k", 20)
    min_p = generation_config.get("min_p", 0)
    presence_penalty = generation_config.get("presence_penalty", 0.0)
    banned_tokens = generation_config.get("banned_tokens", [])

    # Constant across every batch in both review modes - build once.
    gen_params = LLMGenParams(
        system_prompt=review_sys,
        user_prompt_template=review_usr,
        max_tokens=max_tokens,
        temperature=temperature,
        top_p=top_p,
        top_k=top_k,
        min_p=min_p,
        presence_penalty=presence_penalty,
        banned_tokens=banned_tokens,
    )

    print(f"Connecting to: {base_url}")
    print(f"Using model: {model_name}")
    print(f"Batch size: {batch_size} entries, Max tokens: {max_tokens}")
    if banned_tokens:
        print(f"Banned tokens: {banned_tokens}")

    # A remote LM Studio (e.g. on a Thunder Compute instance) is loaded and
    # managed elsewhere; the local `lms` CLI and the local-GPU VRAM watchdog
    # don't apply, so skip them entirely when the endpoint isn't local.
    llm_mode = config.get("llm_mode", "local")
    is_remote, lm_status, heal_msg = ensure_ideal_settings(
        llm_mode, base_url, model_name, ssh_alias=config.get("llm_remote_ssh"))
    print(heal_msg)
    gen_params.context_length = lm_status.get("context_length")
    gen_params.hard_max_tokens = 32768

    client = OpenAI(base_url=base_url, api_key=api_key)

    wave_size = get_cached_or_benchmarked_concurrency(
        config_path, llm_mode, base_url, model_name, client,
        ssh_alias=config.get("llm_remote_ssh"), status=lm_status)
    if wave_size > 1:
        print(f"Using concurrency: {wave_size}")

    # Re-verify settings are still optimized right before review starts, to
    # catch drift between the initial heal and now (e.g. a slow concurrency
    # benchmark, or - for remote - TTL/idle expiry, since
    # apply_remote_lmstudio_settings intentionally doesn't pin a TTL).
    pre_review_status = get_current_status(llm_mode, base_url, model_name,
                                            ssh_alias=config.get("llm_remote_ssh"))
    if pre_review_status["loaded"] and not pre_review_status["optimized"]:
        label = "Remote LM Studio" if is_remote else "LM Studio"
        print(f"WARNING: {label} model '{model_name}' is loaded but NOT optimized.")
        if not is_remote:
            print("This may cause OOM crashes during batch review. Consider restarting LM Studio")
            print("with VRAM-safe settings or reducing batch size.")

    all_corrected = []
    total_stats = {
        "text_changed": 0,
        "speaker_changed": 0,
        "instruct_changed": 0,
        "entries_changed": 0,
        "entries_added": 0,
        "entries_removed": 0,
        "batches_failed": 0,
        "batches_skipped_vram": 0,
    }
    highlight_pool = {"text": [], "speaker": []}
    failed_sections = []
    vram_aborted = False

    if args.context_window and args.context_window > 0:
        window = max(1, args.context_window)
        total_batches_estimate = max(1, (len(entries) + batch_size - 1) // batch_size)
        print(f"Contextual review mode enabled: batching ~{batch_size} entries per LLM call with +/-{window} neighbors")

        (all_corrected, total_stats, previous_tail, completed_batches, batch_lengths,
         failed_batches, resume_offset, checkpoint) = _load_resume_state(
            output_path, total_batches_estimate, batch_size, args.context_window,
            all_corrected, total_stats)
        num_remaining_batches = len(range(resume_offset, len(entries), batch_size))
        total_batches = max(1, completed_batches + num_remaining_batches)
        if checkpoint:
            print(f"Resuming from checkpoint: {completed_batches}/{total_batches} batches already reviewed.")

        unreviewed_remainder = []
        for offset_idx, start in enumerate(range(resume_offset, len(entries), batch_size)):
            batch_index = completed_batches + 1 + offset_idx

            end = min(len(entries), start + batch_size)
            batch = entries[start:end]
            before = entries[max(0, start - window):start]
            after = entries[end:min(len(entries), end + window)]

            if not is_remote and not wait_for_vram_headroom():
                unreviewed_remainder = entries[start:]
                total_stats["batches_skipped_vram"] = total_batches - batch_index + 1
                vram_aborted = True
                break

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
                client, model_name, batch, batch_index, total_batches, gen_params,
                previous_tail=None,  # contextual mode uses explicit before/after window instead
                source_context="\n".join(contextual_lines),
            )

            if corrected is None:
                print(f"  FAILED — keeping original entries for batch {batch_index} (will retry on next resume)")
                all_corrected.extend(batch)
                total_stats["batches_failed"] += 1
                previous_tail = batch[-2:] if len(batch) >= 2 else batch
                batch_lengths.append(len(batch))
                failed_batches.append(batch_index)
                failed_sections.append(get_failed_section(
                    batch_index, start, len(batch), "review_failed"))
                save_checkpoint(output_path, batch_index, total_batches, batch_size,
                                args.context_window, all_corrected, total_stats, previous_tail,
                                batch_lengths, failed_batches)
                continue

            passed, orig_text, corr_text, ratio = check_text_loss(batch, corrected, threshold=0.95, upper_bound=1.05)
            if not passed:
                print(f"  WARNING: Text length mismatch (loss or gain)! Word ratio: {ratio:.2f} (acceptable range: 0.95-1.05)")
                print(f"  Original words: {len(orig_text.split())}, Corrected words: {len(corr_text.split())}")
                print(f"  Keeping original entries for batch {batch_index} to prevent data corruption (will retry on next resume).")
                all_corrected.extend(batch)
                total_stats["batches_failed"] += 1
                previous_tail = batch[-2:] if len(batch) >= 2 else batch
                batch_lengths.append(len(batch))
                failed_batches.append(batch_index)
                failed_sections.append(get_failed_section(
                    batch_index, start, len(batch), "text_length_mismatch", ratio))
                save_checkpoint(output_path, batch_index, total_batches, batch_size,
                                args.context_window, all_corrected, total_stats, previous_tail,
                                batch_lengths, failed_batches)
                continue

            stats = diff_entries(batch, corrected, highlight_pool, entry_offset=start)

            total_stats["entries_added"] += stats["entries_added"]
            total_stats["entries_removed"] += stats["entries_removed"]

            total_stats["text_changed"] += stats["text_changed"]
            total_stats["speaker_changed"] += stats["speaker_changed"]
            total_stats["instruct_changed"] += stats["instruct_changed"]
            total_stats["entries_changed"] += stats["entries_changed"]

            changes = stats["text_changed"] + stats["speaker_changed"] + stats["instruct_changed"]
            if changes > 0 or stats["entries_added"] or stats["entries_removed"]:
                print(f"  Changes: {stats['text_changed']} text, {stats['speaker_changed']} speaker, {stats['instruct_changed']} instruct", end="")
                if stats["entries_added"] or stats["entries_removed"]:
                    print(f", +{stats['entries_added']}/-{stats['entries_removed']} entries")
                else:
                    print()
            else:
                print("  No changes")

            all_corrected.extend(corrected)
            previous_tail = corrected[-2:] if len(corrected) >= 2 else corrected
            batch_lengths.append(len(corrected))
            save_checkpoint(output_path, batch_index, total_batches, batch_size,
                            args.context_window, all_corrected, total_stats, previous_tail,
                            batch_lengths, failed_batches)
    else:
        total_batches_estimate = (len(entries) + batch_size - 1) // batch_size if entries else 0
        print(f"Split into {total_batches_estimate} batches of ~{batch_size} entries")

        (all_corrected, total_stats, previous_tail, completed_batches, batch_lengths,
         failed_batches, resume_offset, checkpoint) = _load_resume_state(
            output_path, total_batches_estimate, batch_size, args.context_window,
            all_corrected, total_stats)
        remaining_entries = entries[resume_offset:]
        remaining_batches = [remaining_entries[i:i + batch_size] for i in range(0, len(remaining_entries), batch_size)]
        total_batches = completed_batches + len(remaining_batches)
        if checkpoint:
            print(f"Resuming from checkpoint: {completed_batches}/{total_batches} batches already reviewed.")

        unreviewed_remainder = []
        # Guards the per-batch VRAM check below: wave_size>1 batches run
        # concurrently, so a single pre-wave check (the old approach) can't
        # see VRAM consumed by sibling batches still starting up within the
        # same wave. vram_lock serializes each batch's own live check (so it
        # reflects whatever siblings dispatched moments earlier already
        # claimed); vram_abort lets one batch's sustained-saturation timeout
        # tell not-yet-started siblings/waves to stop too.
        vram_abort = threading.Event()
        vram_lock = threading.Lock()
        for wave_start in range(0, len(remaining_batches), wave_size):
            wave_batches = remaining_batches[wave_start:wave_start + wave_size]
            wave_indices = [completed_batches + 1 + wave_start + j for j in range(len(wave_batches))]
            # Every batch in this wave gets the SAME previous_tail (from the end of
            # the last wave) - they can't see each other's corrections yet, only
            # results from earlier, already-finished waves. Mirrors find_nicknames.py.
            wave_tail = previous_tail

            if len(wave_batches) > 1:
                print(f"\nReviewing batches {wave_indices[0]}-{wave_indices[-1]}/{total_batches} "
                      f"({sum(len(b) for b in wave_batches)} entries, {len(wave_batches)} concurrent)...")
            else:
                print(f"\nReviewing batch {wave_indices[0]}/{total_batches} ({len(wave_batches[0])} entries)...")

            def _run_one(item):
                i, batch = item
                if not is_remote:
                    with vram_lock:
                        if vram_abort.is_set():
                            return "VRAM_SKIP"
                        if not wait_for_vram_headroom():
                            vram_abort.set()
                            return "VRAM_SKIP"
                return review_batch(
                    client, model_name, batch, i, total_batches, gen_params,
                    previous_tail=wave_tail,
                    source_context=None,  # Mode 2: would pass source text chunk here
                )

            with ThreadPoolExecutor(max_workers=len(wave_batches)) as executor:
                wave_results = list(executor.map(_run_one, zip(wave_indices, wave_batches)))

            for i, batch, corrected in zip(wave_indices, wave_batches, wave_results):
                if corrected == "VRAM_SKIP":
                    # Keep the skipped batch at its original position even if
                    # concurrent workers acquired the VRAM lock out of order.
                    all_corrected.extend(batch)
                    continue

                if corrected is None:
                    print(f"  FAILED — keeping original entries for batch {i} (will retry on next resume)")
                    all_corrected.extend(batch)
                    total_stats["batches_failed"] += 1
                    previous_tail = batch[-2:] if len(batch) >= 2 else batch
                    batch_lengths.append(len(batch))
                    failed_batches.append(i)
                    entry_start = resume_offset + (i - completed_batches - 1) * batch_size + 1
                    failed_sections.append(get_failed_section(
                        i, entry_start - 1, len(batch), "review_failed"))
                    save_checkpoint(output_path, i, total_batches, batch_size,
                                    args.context_window, all_corrected, total_stats, previous_tail,
                                    batch_lengths, failed_batches)
                    continue

                # Text-loss safety check (same bounds as contextual mode for consistency)
                passed, orig_text, corr_text, ratio = check_text_loss(batch, corrected, threshold=0.95, upper_bound=1.05)
                if not passed:
                    print(f"  WARNING: Text length mismatch (loss or gain)! Word ratio: {ratio:.2f} (acceptable range: 0.95-1.05)")
                    print(f"  Original words: {len(orig_text.split())}, Corrected words: {len(corr_text.split())}")
                    print(f"  Keeping original entries for batch {i} to prevent data corruption (will retry on next resume).")
                    all_corrected.extend(batch)
                    total_stats["batches_failed"] += 1
                    previous_tail = batch[-2:] if len(batch) >= 2 else batch
                    batch_lengths.append(len(batch))
                    failed_batches.append(i)
                    entry_start = resume_offset + (i - completed_batches - 1) * batch_size + 1
                    failed_sections.append(get_failed_section(
                        i, entry_start - 1, len(batch), "text_length_mismatch", ratio))
                    save_checkpoint(output_path, i, total_batches, batch_size,
                                    args.context_window, all_corrected, total_stats, previous_tail,
                                    batch_lengths, failed_batches)
                    continue

                # Diff stats
                batch_start = resume_offset + (i - completed_batches - 1) * batch_size
                stats = diff_entries(batch, corrected, highlight_pool, entry_offset=batch_start)

                total_stats["entries_added"] += stats["entries_added"]
                total_stats["entries_removed"] += stats["entries_removed"]

                total_stats["text_changed"] += stats["text_changed"]
                total_stats["speaker_changed"] += stats["speaker_changed"]
                total_stats["instruct_changed"] += stats["instruct_changed"]
                total_stats["entries_changed"] += stats["entries_changed"]

                changes = stats["text_changed"] + stats["speaker_changed"] + stats["instruct_changed"]
                if changes > 0 or stats["entries_added"] or stats["entries_removed"]:
                    print(f"  Changes: {stats['text_changed']} text, {stats['speaker_changed']} speaker, {stats['instruct_changed']} instruct", end="")
                    if stats["entries_added"] or stats["entries_removed"]:
                        print(f", +{stats['entries_added']}/-{stats['entries_removed']} entries")
                    else:
                        print()
                else:
                    print(f"  No changes")

                all_corrected.extend(corrected)
                previous_tail = corrected[-2:] if len(corrected) >= 2 else corrected
                batch_lengths.append(len(corrected))
                save_checkpoint(output_path, i, total_batches, batch_size,
                                args.context_window, all_corrected, total_stats, previous_tail,
                                batch_lengths, failed_batches)

            if vram_abort.is_set():
                # Some batches in this wave may have already succeeded before
                # the abort was detected (kept above, in all_corrected) -
                # only the genuinely-skipped ones and every later, untried
                # wave count as unreviewed.
                skipped_count = sum(1 for r in wave_results if r == "VRAM_SKIP")
                for remaining in remaining_batches[wave_start + len(wave_batches):]:
                    unreviewed_remainder.extend(remaining)
                    skipped_count += 1
                total_stats["batches_skipped_vram"] = skipped_count
                vram_aborted = True
                break

    # Post-processing: merge consecutive NARRATOR entries with same instruct.
    # This is purely local (no LLM calls), so it's safe to run on the
    # successfully-reviewed entries even after a VRAM abort - only the
    # unreviewed_remainder (entries that never went through review) is left
    # untouched and appended below.
    merge_narrators_enabled = generation_config.get("merge_narrators", False)
    narrator_merges = 0
    speakers_merged = 0
    if merge_narrators_enabled:
        pre_merge_count = len(all_corrected)
        all_corrected, narrator_merges = merge_consecutive_narrators(all_corrected, max_merged_length=800)
        if narrator_merges > 0:
            print(f"\nPost-processing: merged {narrator_merges} consecutive narrator entries "
                  f"({pre_merge_count} -> {len(all_corrected)} entries)")
    else:
        print("\nNarrator merging: disabled (enable in Setup > Advanced)")

    # Speaker de-duplication: merge aliases that are the same character.
    # Skipped on a VRAM abort since this step makes additional LLM calls,
    # which is exactly what we're trying to avoid when VRAM is already low.
    if vram_aborted:
        print("\nSkipping speaker alias resolution (stopped early due to low GPU VRAM).")
    elif args.dedupe_speakers:
        print("\nResolving character aliases (merging duplicate names)...")
        dedupe_map, speakers_merged, dedupe_changes = dedupe_speakers(
            client, model_name, all_corrected, registry_path=args.alias_registry,
            context_length=lm_status.get("context_length")
        )
        for idx, key, new_value in dedupe_changes:
            all_corrected[idx][key] = new_value
        if speakers_merged > 0:
            print(f"Merged {len(dedupe_map)} alias label(s), updating {speakers_merged} entries.")
            moved = _remap_voice_config(args.remap_voice_config, dedupe_map)
            if moved:
                print(f"Remapped {moved} voice config entr(y/ies) to canonical names.")
        else:
            print("No duplicate character names found.")

    output_entries = all_corrected + unreviewed_remainder

    # Write corrected script atomically so a crash mid-write doesn't corrupt the output
    atomic_json_write(output_entries, output_path)

    # Checkpoint is only needed while a run is incomplete; clear it once the
    # full review (and any post-processing) has finished successfully.
    if not vram_aborted and not failed_batches:
        clear_checkpoint(output_path)

    # Delete chunks.json so editor regenerates — only when we reviewed the
    # working script (batch review of saved scripts must not touch it).
    if os.path.abspath(output_path) == os.path.abspath(default_script):
        chunks_path = os.path.join(data_dir, "chunks.json")
        if os.path.exists(chunks_path):
            os.remove(chunks_path)
            print("Cleared old chunks.json")

    # Final summary
    total_changes = (total_stats["text_changed"] + total_stats["speaker_changed"] +
                     total_stats["instruct_changed"] + total_stats["entries_added"] +
                     total_stats["entries_removed"] + narrator_merges + speakers_merged)

    print(f"\n{'='*60}")
    print(f"Review complete: {len(entries)} -> {len(output_entries)} entries")
    print(f"  Text changed:    {total_stats['text_changed']}")
    print(f"  Speaker changed: {total_stats['speaker_changed']}")
    print(f"  Instruct changed:{total_stats['instruct_changed']}")
    print(f"  Entries changed: {total_stats['entries_changed']}")
    print(f"  Entries added:   {total_stats['entries_added']}")
    print(f"  Entries removed: {total_stats['entries_removed']}")
    print(f"  Narrators merged:{narrator_merges}")
    print(f"  Speakers merged: {speakers_merged}")
    if total_stats["batches_failed"] > 0:
        print(f"  Batches failed:  {total_stats['batches_failed']}")
    if total_stats["batches_skipped_vram"] > 0:
        print(f"  Batches skipped (low GPU VRAM): {total_stats['batches_skipped_vram']}")
    print(f"  Total changes:   {total_changes}")
    print(f"{'='*60}")

    if failed_sections:
        failure_details = {
            "sections": failed_sections,
            "original_entries_preserved": True,
            "checkpoint_retained": os.path.exists(_checkpoint_path(output_path)),
            "retry_from_batch": min(section["batch"] for section in failed_sections),
        }
        print(f"FAILED_SECTIONS_JSON: {json.dumps(failure_details, ensure_ascii=False)}")

    # Emit a compact JSON line with the most notable before/after examples so the
    # web UI can show a "diff preview" without re-diffing the whole script.
    diff_highlights = {
        "text_rewrites": sorted(highlight_pool["text"], key=lambda h: h["magnitude"], reverse=True)[:5],
        "speaker_changes": highlight_pool["speaker"][:5],
    }
    if diff_highlights["text_rewrites"] or diff_highlights["speaker_changes"]:
        print(f"DIFF_PREVIEW_JSON: {json.dumps(diff_highlights, ensure_ascii=False)}")

    if vram_aborted:
        print("Stopped early: GPU VRAM stayed too high to safely continue.")
        print("The remaining entries were saved unreviewed. Free up VRAM "
              "(e.g. close other GPU apps) and re-run to review the rest.")
    elif total_changes == 0:
        print("No issues found -- script looks clean.")
    else:
        print(f"Fixed {total_changes} issues across {total_batches} batches.")

    print(f"Output saved to: {output_path}")
    if vram_aborted:
        print("Task review completed with an early VRAM-abort - some entries were saved unreviewed.")
    elif total_stats["batches_failed"] > 0:
        print(f"Task review completed with {total_stats['batches_failed']} batch failure(s) - those entries were saved unreviewed.")
    else:
        print("Task review completed successfully.")


if __name__ == "__main__":
    main()
