import os
import sys
import json
import re
import time
import difflib
import subprocess
import argparse
from openai import OpenAI
from review_prompts import REVIEW_SYSTEM_PROMPT, REVIEW_USER_PROMPT
from generate_script import clean_json_string, repair_json_array, salvage_json_entries
from lmstudio_settings import apply_lmstudio_settings, get_lmstudio_status
from utils import file_lock, atomic_json_write


# ── GPU VRAM watchdog ────────────────────────────────────────────────────────
# A long batch review run can grow VRAM usage (KV cache + other GPU apps) until
# the driver runs out of memory and crashes the whole display server. Before
# each batch we check headroom and pause/abort rather than let that happen.

VRAM_WARN_THRESHOLD = 0.90  # pause once VRAM usage crosses this fraction
VRAM_MAX_WAIT = 180         # seconds to wait for headroom before giving up
VRAM_POLL_INTERVAL = 15     # seconds between checks while waiting


def get_vram_usage():
    """Return (used_bytes, total_bytes) for GPU 0 via rocm-smi, or None if unavailable."""
    try:
        result = subprocess.run(
            ["rocm-smi", "--showmeminfo", "vram", "--json"],
            capture_output=True, text=True, timeout=5
        )
        data = json.loads(result.stdout)
        card = next(iter(data.values()))
        used = int(card["VRAM Total Used Memory (B)"])
        total = int(card["VRAM Total Memory (B)"])
        if total <= 0:
            return None
        return used, total
    except Exception:
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
    while waited < VRAM_MAX_WAIT:
        time.sleep(VRAM_POLL_INTERVAL)
        waited += VRAM_POLL_INTERVAL
        usage = get_vram_usage()
        if usage is None:
            return True
        used, total = usage
        if used / total < VRAM_WARN_THRESHOLD:
            print(f"  VRAM back to {used/total:.0%} - resuming.")
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

    failed_batches = sorted(data["failed_batches"])
    batch_lengths = data["batch_lengths"]
    completed_batches = data["completed_batches"]
    if failed_batches and len(batch_lengths) == completed_batches:
        retry_from = failed_batches[0]
        keep_entries = sum(batch_lengths[:retry_from - 1])
        all_corrected = data["all_corrected"][:keep_entries]
        data["all_corrected"] = all_corrected
        data["completed_batches"] = retry_from - 1
        data["batch_lengths"] = batch_lengths[:retry_from - 1]
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
    tmp_path = path + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp_path, path)


def clear_checkpoint(output_path):
    path = _checkpoint_path(output_path)
    if os.path.exists(path):
        os.remove(path)


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
    # Hold the lock across the whole read-modify-write so this can't lose an
    # update to (or clobber) a concurrent write from the UI, e.g. "apply cast
    # to multiple books" writing the same companion voice_config.json.
    with file_lock(voice_config_path):
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
                atomic_json_write(cfg, voice_config_path)
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


_MAX_HIGHLIGHT_POOL = 500
_HIGHLIGHT_SNIPPET_LEN = 300


def diff_entries(original, corrected, highlight_pool=None):
    """Compare original and corrected entries, return a summary dict.

    If highlight_pool is given (a dict with "text" and "speaker" lists), append
    notable per-entry before/after snippets to it for the "diff preview" report,
    capped at _MAX_HIGHLIGHT_POOL each so a huge book can't blow up memory.
    """
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
        orig_text = orig.get("text", "")
        corr_text = corr.get("text", "")
        orig_speaker = orig.get("speaker") or ""
        corr_speaker = corr.get("speaker") or ""

        if orig_text != corr_text:
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
            stats["speaker_changed"] += 1
            if highlight_pool is not None and len(highlight_pool["speaker"]) < _MAX_HIGHLIGHT_POOL:
                highlight_pool["speaker"].append({
                    "text": (corr_text or orig_text)[:_HIGHLIGHT_SNIPPET_LEN],
                    "before": orig_speaker or "(unknown)",
                    "after": corr_speaker or "(unknown)",
                })
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

    # Make sure LM Studio is loaded with VRAM-safe settings (8192 ctx,
    # parallel 1) regardless of what's currently loaded - covers the case
    # where LM Studio was restarted or a different model/config was used last.
    ok, msg = apply_lmstudio_settings(model_name, ideal=True)
    if ok:
        print(f"LM Studio: {msg}")
    else:
        # The reload failed, but if the model happens to already be loaded
        # with VRAM-safe settings (e.g. a previous run applied them and the
        # reload here was just redundant), there's nothing to worry about.
        status = get_lmstudio_status(model_name)
        if status["loaded"] and status["optimized"]:
            print(f"LM Studio: could not reload ({msg}), but {model_name} is "
                  f"already loaded with VRAM-safe settings - continuing.")
        else:
            print(f"LM Studio: WARNING - could not apply VRAM-safe settings ({msg}). "
                  f"The model may be running with a higher 'parallel'/context-length "
                  f"configuration, which uses more VRAM per request and increases the "
                  f"risk of an out-of-memory crash. The VRAM watchdog below will still "
                  f"pause batches if usage gets too high, but if you hit OOM, restart "
                  f"LM Studio and re-run.")

    client = OpenAI(base_url=base_url, api_key=api_key)

    all_corrected = []
    total_stats = {
        "text_changed": 0,
        "speaker_changed": 0,
        "instruct_changed": 0,
        "entries_added": 0,
        "entries_removed": 0,
        "batches_failed": 0,
        "batches_skipped_vram": 0,
    }
    highlight_pool = {"text": [], "speaker": []}
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

            if not wait_for_vram_headroom():
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
                print(f"  FAILED — keeping original entries for batch {batch_index} (will retry on next resume)")
                all_corrected.extend(batch)
                total_stats["batches_failed"] += 1
                previous_tail = batch[-2:] if len(batch) >= 2 else batch
                batch_lengths.append(len(batch))
                failed_batches.append(batch_index)
                save_checkpoint(output_path, batch_index, total_batches, batch_size,
                                args.context_window, all_corrected, total_stats, previous_tail,
                                batch_lengths, failed_batches)
                continue

            passed, orig_text, corr_text, ratio = check_text_loss(batch, corrected, threshold=0.95, upper_bound=1.15)
            if not passed:
                print(f"  WARNING: Text length mismatch (loss or gain)! Word ratio: {ratio:.2f} (acceptable range: 0.95-1.15)")
                print(f"  Original words: {len(orig_text.split())}, Corrected words: {len(corr_text.split())}")
                print(f"  Keeping original entries for batch {batch_index} to prevent data corruption (will retry on next resume).")
                all_corrected.extend(batch)
                total_stats["batches_failed"] += 1
                previous_tail = batch[-2:] if len(batch) >= 2 else batch
                batch_lengths.append(len(batch))
                failed_batches.append(batch_index)
                save_checkpoint(output_path, batch_index, total_batches, batch_size,
                                args.context_window, all_corrected, total_stats, previous_tail,
                                batch_lengths, failed_batches)
                continue

            stats = diff_entries(batch, corrected, highlight_pool)
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
        for offset_idx, batch in enumerate(remaining_batches):
            i = completed_batches + 1 + offset_idx

            if not wait_for_vram_headroom():
                for remaining in remaining_batches[offset_idx:]:
                    unreviewed_remainder.extend(remaining)
                total_stats["batches_skipped_vram"] = total_batches - i + 1
                vram_aborted = True
                break

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
                print(f"  FAILED — keeping original entries for batch {i} (will retry on next resume)")
                all_corrected.extend(batch)
                total_stats["batches_failed"] += 1
                previous_tail = batch[-2:] if len(batch) >= 2 else batch
                batch_lengths.append(len(batch))
                failed_batches.append(i)
                save_checkpoint(output_path, i, total_batches, batch_size,
                                args.context_window, all_corrected, total_stats, previous_tail,
                                batch_lengths, failed_batches)
                continue

            # Text-loss safety check
            passed, orig_text, corr_text, ratio = check_text_loss(batch, corrected)
            if not passed:
                print(f"  WARNING: Text length mismatch (loss or gain)! Word ratio: {ratio:.2f} (acceptable range: 0.95-1.05)")
                print(f"  Original words: {len(orig_text.split())}, Corrected words: {len(corr_text.split())}")
                print(f"  Keeping original entries for batch {i} to prevent data corruption (will retry on next resume).")
                all_corrected.extend(batch)
                total_stats["batches_failed"] += 1
                previous_tail = batch[-2:] if len(batch) >= 2 else batch
                batch_lengths.append(len(batch))
                failed_batches.append(i)
                save_checkpoint(output_path, i, total_batches, batch_size,
                                args.context_window, all_corrected, total_stats, previous_tail,
                                batch_lengths, failed_batches)
                continue

            # Diff stats
            stats = diff_entries(batch, corrected, highlight_pool)
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
            batch_lengths.append(len(corrected))
            save_checkpoint(output_path, i, total_batches, batch_size,
                            args.context_window, all_corrected, total_stats, previous_tail,
                            batch_lengths, failed_batches)

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

    output_entries = all_corrected + unreviewed_remainder

    # Write corrected script
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(output_entries, f, indent=2, ensure_ascii=False)

    # Checkpoint is only needed while a run is incomplete; clear it once the
    # full review (and any post-processing) has finished successfully.
    if not vram_aborted:
        clear_checkpoint(output_path)

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
    print(f"Review complete: {len(entries)} -> {len(output_entries)} entries")
    print(f"  Text changed:    {total_stats['text_changed']}")
    print(f"  Speaker changed: {total_stats['speaker_changed']}")
    print(f"  Instruct changed:{total_stats['instruct_changed']}")
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
    print("Task review completed successfully.")


if __name__ == "__main__":
    main()
