import os
from core import llm_timeout_seconds
import sys
import json
import copy
import time
import re
import unicodedata
import argparse
import shutil
import tempfile
import hashlib
from contextlib import nullcontext
from book_state_transaction import (ensure_book_state, get_book_snapshot,
                                    require_book_snapshot_current, get_book_snapshot_token)
from config_settings import load_app_config
from llm_provider import make_run_client
from generate_script import LLMGenParams, call_llm_for_object, split_failed_chunk

from tts import TTSEngine, sanitize_filename, voice_is_set
from utils import atomic_json_write as _atomic_json_write, safe_load_json, extract_json_object, get_runtime_data_dir, get_app_config_path, character_voice_seed, file_lock
from persona_prompts import PERSONA_SYSTEM_PROMPT, PERSONA_USER_PROMPT, PERSONA_ADVANCED_PROMPT
from persona_validation import validate_persona_payload
from speaker_traits import get_persona_state_targets, get_persona_state_entries
from speaker_identity import (is_speaker_merge_allowed, resolve_speaker_label,
                              get_validated_alias_graph, get_safe_alias_proposals)
from lmstudio_settings import (ensure_ideal_settings, get_active_llm_config,
                               get_effective_max_tokens)


HONORIFIC_RE = re.compile(r'^(mr|mrs|ms|miss|dr|prof|sir|lady|lord)\.?\s+')


def normalize_speaker_name(name, strip_honorifics=True):
    """Fold a speaker label for comparison.

    TWO KINDS OF FOLDING, AND THEY ARE NOT EQUALLY SAFE. Case and punctuation
    can always be folded: no two characters are distinguished by capitalisation
    alone, and the live config relies on this - EMILIA/Emilia, REINHARD/
    Reinhard and nine other pairs are one character each.

    An HONORIFIC is different. For a married couple it is the only
    distinguishing token, and stripping it merged MR. BENNET with MRS. BENNET
    in Pride and Prejudice - along with the Hilberys, the Allens, the Halls and
    the Van der Luydens, six of twenty-eight books. Three characters resolved
    to one voice and nothing failed.

    `scoring.normalize` reached this conclusion for the evaluation path and
    folds punctuation only, commenting "this must not merge distinct
    characters". This is the production path catching up. The default is kept
    for callers doing loose alias heuristics; `_resolve_to_canonical` decides
    per roster whether stripping is safe.
    """
    if not isinstance(name, str):
        return ""
    s = unicodedata.normalize("NFC", name.strip().lower())
    if strip_honorifics:
        s = HONORIFIC_RE.sub('', s)
    s = ''.join(c for c in s if c.isalnum() or c.isspace()
                or unicodedata.category(c).startswith('M'))
    s = re.sub(r'\s+', ' ', s).strip()
    return s


def get_exact_alias_match(speaker, existing_names):
    """Return a shared exact identity match with protected labels preserved."""
    if not speaker:
        return None
    allowed = [name for name in existing_names
               if speaker.strip().casefold() == name.strip().casefold()
               or is_speaker_merge_allowed(speaker, name)]
    return resolve_speaker_label(speaker, allowed)


def honorifics_are_distinguishing(allowed):
    """True when stripping honorifics would merge two entries of this roster.

    Checked against the ACTUAL roster rather than assumed, because the answer
    differs per book: Pride and Prejudice has both Bennets, most books have
    neither.

    A collision only counts when KEEPING the honorific resolves it. The first
    version returned True on any post-normalization duplicate, which included
    pure case variants - the live config's EMILIA/Emilia and
    NOT-SATELLA/Not-Satella. Those collide with honorifics kept too, since case
    folding always applies, so honorifics buy nothing there; the book just paid
    stricter matching for every one of its characters.
    """
    stripped_seen, intact_seen = set(), set()
    collided = set()
    for name in allowed or ():
        stripped = normalize_speaker_name(name, strip_honorifics=True)
        intact = normalize_speaker_name(name, strip_honorifics=False)
        if not stripped:
            continue
        if stripped in stripped_seen and intact not in intact_seen:
            collided.add(stripped)
        stripped_seen.add(stripped)
        intact_seen.add(intact)
    return bool(collided)


def _token_jaccard(a: str, b: str, strip_honorifics=True) -> float:
    """Jaccard similarity on normalized name tokens.

    Takes the same honorific decision as its caller. With it stripped,
    'Miss Darcy' and 'MR DARCY' both reduce to {darcy} and score 1.0 - which
    is how a third character resolved onto the first Darcy in the roster even
    after step 1 was fixed.
    """
    norm_a = normalize_speaker_name(a, strip_honorifics=strip_honorifics)
    norm_b = normalize_speaker_name(b, strip_honorifics=strip_honorifics)
    if not norm_a or not norm_b:
        return 0.0
    tokens_a = set(norm_a.split())
    tokens_b = set(norm_b.split())
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a.intersection(tokens_b)
    union = tokens_a.union(tokens_b)
    return len(intersection) / len(union)


def _resolve_to_canonical(raw_name: str, allowed: list, threshold=0.4) -> str | None:
    """Map a raw name to the closest canonical label, or None.

    Tries:
    1. Exact match after normalization.
    2. Substring match with word boundary checks (not just any substring).
    3. Token Jaccard similarity with higher threshold.
    """
    if not raw_name:
        return None

    # If two roster entries differ ONLY by honorific, stripping it would merge
    # them - so for that roster, keep the honorific everywhere in this call.
    # Deciding per roster rather than globally keeps loose matching for the
    # books that need it, which is most of them.
    keep = honorifics_are_distinguishing(allowed)
    allowed = [name for name in allowed
               if raw_name.strip().casefold() == name.strip().casefold()
               or is_speaker_merge_allowed(raw_name, name)]
    norm = lambda n: normalize_speaker_name(n, strip_honorifics=not keep)

    norm_raw = norm(raw_name)
    if not norm_raw:
        return None

    # Step 1: Exact match after normalization
    for name in allowed:
        if norm(name) == norm_raw:
            return name

    # Step 2: Substring match with word boundaries (avoid 'john' matching 'johnson')
    pattern_raw_in_name = re.compile(r'\b' + re.escape(norm_raw) + r'\b')
    for name in allowed:
        norm_name = norm(name)
        if not norm_name:
            continue
        # Only match if one is a complete word within the other
        # Use word boundary regex to avoid partial matches like john/johnson
        if pattern_raw_in_name.search(norm_name) or re.search(r'\b' + re.escape(norm_name) + r'\b', norm_raw):
            return name

        # Short prefix/nickname match (e.g. 'ann' vs 'anna', len diff <= 2).
        # Require both names to have at least 3 chars so short names/initials
        # ('al', 'jo') don't spuriously match unrelated longer names ('allan', 'jonathan').
        if len(norm_raw) >= 3 and len(norm_name) >= 3:
            if (norm_name.startswith(norm_raw) and len(norm_name) - len(norm_raw) <= 2) or \
               (norm_raw.startswith(norm_name) and len(norm_raw) - len(norm_name) <= 2):
                return name

    # Step 3: Token Jaccard similarity with higher threshold
    best_name = None
    best_score = 0.0
    for name in allowed:
        score = _token_jaccard(raw_name, name, strip_honorifics=not keep)
        if score > best_score:
            best_score = score
            best_name = name

    if best_score >= threshold:
        return best_name

    return None


_NARRATOR_LABELS = frozenset({"NARRATOR", "NARRATION", "NARRATIVE"})


DEFAULT_CONTEXT_LINES = 8


def get_persona_context_limit(context_lines):
    """Normalize the documented sample count using the existing CLI bound."""
    return max(1, min(int(context_lines or DEFAULT_CONTEXT_LINES), 200))


def select_persona_context(lines, narrator_context, context_lines=DEFAULT_CONTEXT_LINES):
    """-> (sample_text, intro_blob) for the persona prompt: the first
    `context_lines` spoken lines and up to as many narrator lines (#522 12.1:
    10 / 25 / 50 / 100 or custom, from the Voices tab)."""
    n = get_persona_context_limit(context_lines)
    sample_text = "\n".join(lines[:n])
    intro = narrator_context[:n]
    intro_blob = "\n".join(intro) if intro else "(No nearby narrator intro lines found.)"
    return sample_text, intro_blob


def _collect_narrator_context(script, speaker, window=4):
    """Gather unique narrator lines within `window` entries (before and after) of any appearance.

    - Scans both before and after each appearance.
    - Looks at all appearances, not just the first.
    - Accepts any speaker labels in _NARRATOR_LABELS.
    """
    context_lines = []
    seen_lines = set()
    window = max(1, int(window or 4))

    # Find all indices of the speaker's appearances in the script
    speaker_indices = []
    for i, entry in enumerate(script):
        if _entry_speaker(entry) == speaker:
            speaker_indices.append(i)

    # For each appearance, look at the window around it
    for idx in speaker_indices:
        # Check before and after
        start_idx = max(0, idx - window)
        end_idx = min(len(script), idx + window + 1)
        for j in range(start_idx, end_idx):
            if j == idx:
                continue
            entry = script[j]
            entry_speaker = _entry_speaker(entry).upper()
            entry_text = _entry_text(entry)
            if entry_speaker in _NARRATOR_LABELS and entry_text:
                if entry_text not in seen_lines:
                    seen_lines.add(entry_text)
                    context_lines.append(entry_text)
                    if len(context_lines) >= window:
                        return context_lines

    return context_lines


def _persona_params(system_prompt, context_length, llm_config, max_tokens, temperature):
    llm_config = llm_config or {}
    return LLMGenParams(
        system_prompt=system_prompt, user_prompt_template="", max_tokens=max_tokens,
        temperature=temperature, context_length=context_length,
        provider_extra_body=llm_config.get("provider_extra_body"),
        structured_output=llm_config.get("structured_output", "auto"),
        api_retry_limit=llm_config.get("api_retry_limit"),
        retry_initial_delay_seconds=llm_config.get("retry_initial_delay_seconds", 1),
        retry_multiplier=llm_config.get("retry_multiplier", 2),
        retry_max_delay_seconds=llm_config.get("retry_max_delay_seconds", 30),
        retry_jitter=llm_config.get("retry_jitter", 0.2),
        on_api_exhaustion=llm_config.get("on_api_exhaustion", "fail"),
    )


def _persona_attempt_observer(record):
    """Keep shared retry diagnostics visible in the persona process log."""
    print(json.dumps({"persona_attempt": record}, ensure_ascii=False), flush=True)



class PersonaContextRecoveryError(RuntimeError):
    """Selected evidence could not be recovered on the active runtime."""


def request_persona_with_evidence(client, model_name, system_prompt, build_prompt,
                                  evidence, params, label):
    """Preserve fixed instructions and recover selected evidence in smaller calls."""
    def request(prompt):
        attempts = []

        def observe(attempt):
            attempts.append(attempt)
            _persona_attempt_observer(attempt)

        result = call_llm_for_object(
            client, model_name, system_prompt, prompt, params, label=label,
            validate_object=validate_persona_payload, max_retries=2, attempt_observer=observe)
        exhausted_context = (getattr(client, "switched", False) and any(
            attempt.get("error_category") == "context_budget"
            or attempt.get("finish_reason") == "length" for attempt in attempts))
        return result, exhausted_context

    def merge(drafts):
        if len(drafts) == 1:
            return drafts[0]
        prompt = (build_prompt([]) + "\n\nSupported partial persona drafts (not new source text):\n"
                  + json.dumps(drafts, ensure_ascii=False)
                  + "\nCombine all supported observations into one concise persona. "
                    "Keep ref_text from one draft. Return description and ref_text only.")
        result, _ = request(prompt)
        if isinstance(result, dict):
            return validate_persona_payload(result)
        if len(drafts) <= 2:
            raise PersonaContextRecoveryError(label + ": recovered drafts cannot be combined safely")
        middle = len(drafts) // 2
        reduced = [merge(drafts[:middle]), merge(drafts[middle:])]
        if len(json.dumps(reduced)) >= len(json.dumps(drafts)):
            raise PersonaContextRecoveryError(label + ": recovered drafts did not shrink")
        return merge(reduced)

    def recover(parts, required=False):
        result, exhausted_context = request(build_prompt(parts))
        if isinstance(result, dict):
            return validate_persona_payload(result)
        if not exhausted_context:
            if required:
                raise PersonaContextRecoveryError(label + ": an evidence request failed")
            return result
        if len(parts) > 1:
            middle = len(parts) // 2
            groups = [parts[:middle], parts[middle:]]
        elif parts:
            kind, text = parts[0]
            fragments = split_failed_chunk(text)
            groups = [[(kind, fragment)] for fragment in fragments]
        else:
            groups = []
        if not groups:
            raise PersonaContextRecoveryError(label + ": fixed instructions or minimum evidence exceed active context")
        return merge([recover(group, required=True) for group in groups])

    return recover(list(evidence))

def _resolve_aliases_batch(client, model_name, speakers_info, existing_names,
                           context_length=None, llm_config=None, comparison_names=None):
    """Resolve aliases for all speakers in a single one-shot LLM call.

    speakers_info is a dict:
    {
      "SPEAKER_NAME": {
         "sample_lines": [...],
         "narrator_context": [...]
      }
    }

    Returns a dict mapping each raw speaker label to its canonical group leader name.
    """
    if not speakers_info:
        return {}

    comparison_names = list(dict.fromkeys(comparison_names if comparison_names is not None
                                         else list(speakers_info) + list(existing_names)))
    prompt_items = []
    for speaker, info in speakers_info.items():
        samples = "\n".join(f"  - {line}" for line in info["sample_lines"][:3])
        context = "\n".join(f"  - {line}" for line in info["narrator_context"][:2])
        prompt_items.append(
            f"Speaker label: '{speaker}'\n"
            f"Nearby narrator context:\n{context or '  (None)'}\n"
            f"Sample spoken lines:\n{samples or '  (None)'}"
        )

    formatted_speakers = "\n\n---\n\n".join(prompt_items)
    candidates = "\n".join(f"- {name}" for name in comparison_names) if comparison_names else "(none)"

    prompt = (
        "You are an expert audiobook production assistant specializing in character identification.\n"
        "Below is a list of speaker labels found in a script, along with their sample lines and surrounding narrator context.\n\n"
        "Your task is to analyze these speakers globally and identify which labels represent the same character (aliases/variants) "
        "and which are truly distinct characters. Group them under a single canonical name (preferably the most complete or common name).\n\n"
        "Rules:\n"
        "1. Identify duplicates, minor spelling variations, honorific variations (e.g., 'Mr. Darcy' and 'Darcy'), and nickname vs full name relationships.\n"
        "2. For each input speaker label, specify its resolved canonical name.\n"
        "3. If a speaker is unique and has no other aliases, its canonical name should just be itself.\n"
        "4. You can also map a speaker label to one of the existing configured characters listed below if it is a match.\n\n"
        f"Comparison character labels (script and configured):\n{candidates}\n\n"
        "Return ONLY one JSON object where keys are the original input speaker labels, and values are their resolved canonical names.\n"
        "Example shape:\n"
        "{\n"
        "  \"DARCY\": \"MR. DARCY\",\n"
        "  \"ELIZABETH BENNET\": \"ELIZABETH BENNET\",\n"
        "  \"LIZZY\": \"ELIZABETH BENNET\"\n"
        "}\n\n"
        f"Speakers to analyze:\n\n{formatted_speakers}"
    )

    try:
        messages = [
            {"role": "system", "content": "You are a precise casting director. You output ONLY valid JSON."},
            {"role": "user", "content": prompt}
        ]
        params = _persona_params(messages[0]["content"], context_length, llm_config,
                                 max(1500, len(speakers_info) * 80), 0.1)
        attempts = []

        def observe(attempt):
            attempts.append(attempt)
            _persona_attempt_observer(attempt)

        result = call_llm_for_object(
            client, model_name, messages[0]["content"], messages[1]["content"], params,
            label="PERSONA ALIAS RESOLUTION", max_retries=2,
            attempt_observer=observe)
        if (not isinstance(result, dict) and getattr(client, "switched", False)
                and any(attempt.get("error_category") == "context_budget"
                        or attempt.get("finish_reason") == "length" for attempt in attempts)):
            targets = list(speakers_info.items())
            if len(targets) > 1:
                middle = len(targets) // 2
                parts = [dict(targets[:middle]), dict(targets[middle:])]
            else:
                speaker, info = targets[0]
                evidence = [("sample_lines", line) for line in info["sample_lines"][:3]]
                evidence += [("narrator_context", line) for line in info["narrator_context"][:2]]
                if len(evidence) > 1:
                    middle = len(evidence) // 2
                    groups = [evidence[:middle], evidence[middle:]]
                elif evidence:
                    kind, text = evidence[0]
                    texts = split_failed_chunk(text)
                    groups = [[(kind, part)] for part in (texts or [])]
                else:
                    groups = []
                parts = [{speaker: {kind: [text for field, text in group if field == kind]
                                   for kind in ("sample_lines", "narrator_context")}}
                         for group in groups]
            if parts:
                proposals = [_resolve_aliases_batch(
                    client, model_name, part, existing_names, context_length, llm_config,
                    comparison_names=comparison_names) for part in parts]
                return get_safe_alias_proposals(proposals, comparison_names)

        if isinstance(result, dict):
            # Normalize keys and values to match exact input names casing
            resolved = {}
            for k, v in result.items():
                if isinstance(k, str) and isinstance(v, str):
                    resolved[k.strip()] = v.strip()
            return resolved
    except Exception as e:
        print(f"Warning: Batch alias resolution failed: {e}")

    return {}


def pick_ref_text(lines):
    for ln in lines:
        if ln and len(ln.strip()) >= 12:
            return ln.strip()
    return next((ln.strip() for ln in lines if ln and ln.strip()), "")


def _as_list(value):
    if isinstance(value, list):
        return [v.strip() for v in value if isinstance(v, str) and v.strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _unique_extend(existing, values, limit=80):
    existing = list(existing[:max(0, limit)])
    seen = {str(v).strip().lower() for v in existing if str(v).strip()}
    for value in _as_list(values):
        if len(existing) >= limit:
            break
        key = value.lower()
        if key not in seen:
            existing.append(value)
            seen.add(key)
        if len(existing) >= limit:
            break
    return existing


def _entry_speaker(entry):
    return (entry.get("speaker") or entry.get("type") or "").strip()


def _entry_text(entry):
    return (entry.get("text") or "").strip()


def _batch_entries(script, batch_size):
    batch_size = max(1, int(batch_size or 40))
    for start in range(0, len(script), batch_size):
        yield start, script[start:start + batch_size]


def _json_preview(data, max_chars=12000):
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n...TRUNCATED..."


def _character_ref_path(ref_dir, speaker):
    safe = sanitize_filename(speaker or "unknown")
    return os.path.join(ref_dir, f"{safe}.json")


def _load_character_ref(ref_dir, speaker):
    path = _character_ref_path(ref_dir, speaker)
    default = {
        "name": speaker,
        "aliases": [],
        "features": [],
        "personality": [],
        "voice_clues": [],
        "relationships": [],
        "sample_lines": [],
        "observations": [],
    }
    return safe_load_json(path, default=default)


def _append_character_ref(ref_dir, speaker, batch_number, character_data):
    ref = _load_character_ref(ref_dir, speaker)
    ref["name"] = ref.get("name") or speaker
    ref["aliases"] = _unique_extend(ref.get("aliases", []), character_data.get("aliases", []))
    ref["features"] = _unique_extend(ref.get("features", []), character_data.get("features", []), limit=120)
    ref["personality"] = _unique_extend(ref.get("personality", []), character_data.get("personality", []), limit=120)
    ref["voice_clues"] = _unique_extend(ref.get("voice_clues", []), character_data.get("voice_clues", []), limit=120)
    ref["relationships"] = _unique_extend(ref.get("relationships", []), character_data.get("relationships", []), limit=120)
    ref["sample_lines"] = _unique_extend(ref.get("sample_lines", []), character_data.get("sample_lines", []), limit=50)

    observation = {
        "batch": batch_number,
        "features": _as_list(character_data.get("features", [])),
        "personality": _as_list(character_data.get("personality", [])),
        "voice_clues": _as_list(character_data.get("voice_clues", [])),
        "relationships": _as_list(character_data.get("relationships", [])),
        "evidence": character_data.get("evidence", []),
        "sample_lines": _as_list(character_data.get("sample_lines", [])),
    }
    ref.setdefault("observations", []).append(observation)
    ref["updated_at"] = int(time.time())
    _atomic_json_write(ref, _character_ref_path(ref_dir, speaker))
    return ref


def _build_batch_discovery_prompt(batch_start, batch, allowed_speakers):
    lines = []
    for offset, entry in enumerate(batch):
        speaker = _entry_speaker(entry)
        text = _entry_text(entry)
        if not speaker and not text:
            continue
        source_index = entry.get("_source_entry_index", batch_start + offset)
        lines.append(f"[{source_index}] {speaker}: {text}")

    allowed = "\n".join(f"- {name}" for name in allowed_speakers) if allowed_speakers else "(none)"
    batch_text = "\n".join(lines)
    return (
        "You are building character reference files for an audiobook voice generator.\n"
        "Read this batch of script entries and do only two things:\n"
        "1. discover which characters/speakers are present from the allowed labels;\n"
        "2. describe observed character features, personality, and voice-relevant clues.\n\n"
        "The ONLY valid character keys are listed below. Do not invent new names.\n"
        "Return ONLY one JSON object where each key is EXACTLY one of the allowed labels:\n"
        "{\n"
        "  \"SPEAKER_LABEL\": {\n"
        "    \"aliases\": [\"optional alternate names\"],\n"
        "    \"features\": [\"physical/social/role facts supported by this batch\"],\n"
        "    \"personality\": [\"personality traits supported by this batch\"],\n"
        "    \"voice_clues\": [\"age, gender, accent, timbre, pace, tone, delivery clues\"],\n"
        "    \"relationships\": [\"relationships to other characters if explicit\"],\n"
        "    \"evidence\": [{\"entry_index\": 0, \"quote\": \"short quote\"}],\n"
        "    \"sample_lines\": [\"good spoken sample lines for this character\"]\n"
        "  }\n"
        "}\n\n"
        f"Allowed speaker labels:\n{allowed}\n\n"
        f"Script batch:\n{batch_text}"
    )


def _fallback_batch_characters(batch, batch_start=0):
    by_speaker = {}
    for offset, entry in enumerate(batch):
        speaker = _entry_speaker(entry)
        text = _entry_text(entry)
        if not speaker:
            continue
        data = by_speaker.setdefault(speaker, {
            "name": speaker,
            "aliases": [],
            "features": [],
            "personality": [],
            "voice_clues": [],
            "relationships": [],
            "evidence": [],
            "sample_lines": [],
        })
        if text and len(data["sample_lines"]) < 3:
            data["sample_lines"].append(text)
        if text and len(data["evidence"]) < 3:
            data["evidence"].append({"entry_index": entry.get("_source_entry_index", batch_start + offset), "quote": text[:240]})
    return list(by_speaker.values())


def _compile_character_prompt(character_ref, prompt_template=None, reference_text=None, sample_limit=30):
    compact = {
        "name": character_ref.get("name", ""),
        "aliases": character_ref.get("aliases", [])[:20],
        "features": character_ref.get("features", [])[:80],
        "personality": character_ref.get("personality", [])[:80],
        "voice_clues": character_ref.get("voice_clues", [])[:80],
        "relationships": character_ref.get("relationships", [])[:60],
        "sample_lines": character_ref.get("sample_lines", [])[:sample_limit],
        "observations": character_ref.get("observations", [])[-30:],
    }
    reference_text = _json_preview(compact) if reference_text is None else reference_text
    if prompt_template:
        return prompt_template.format(character_ref=reference_text)
    return (
        "You are compiling an audiobook character reference into a final TTS voice persona.\n"
        "Use only supported observations. The final description should be practical for voice design.\n"
        "Return ONLY one JSON object with keys:\n"
        "- description: 2-4 sentences covering apparent age/gender if inferable, timbre, accent/dialect, pace, emotional baseline, personality, and delivery guidance.\n"
        "- ref_text: 1-2 representative spoken sentences from the character, or the best available sample line.\n\n"
        f"Character reference:\n{reference_text}"
    )


def _fallback_compiled_persona(character_ref):
    name = character_ref.get("name", "Character")
    parts = []
    for key in ("voice_clues", "personality", "features"):
        parts.extend(character_ref.get(key, [])[:5])
    description = f"{name} has a clear, natural audiobook voice."
    if parts:
        description = f"{name} should sound like: " + "; ".join(parts[:10]) + "."
    ref_text = pick_ref_text(character_ref.get("sample_lines", []))
    return description, ref_text


def _save_generated_preview(root, engine, voice_config, speaker, description, ref_text, book_id=None):
    try:
        wav_path, _ = engine.generate_voice_design(description=description, sample_text=ref_text)
    except Exception as e:
        print(f"Error generating voice preview for {speaker}: {e}")
        voice_entry = voice_config.get(speaker, {})
        voice_entry.update({"type": "design", "description": description, "ref_text": ref_text,
                            "persona_status": "generated"})
        voice_config[speaker] = voice_entry
        return False

    # Persona previews belong to a book, not to the permanent Designer manifest.
    # Unique generations also preserve references held by saved books/versions.
    preview_dir = None
    try:
        if book_id is None:
            state = safe_load_json(os.path.join(root, "state.json"), default={})
            book_id = state.get("active_book_id") or "active_book"
        book_key = sanitize_filename(book_id) + "_" + hashlib.sha256(
            str(book_id).encode("utf-8")).hexdigest()[:16]
        book_dir = os.path.join(root, "designed_voices", "persona", book_key)
        os.makedirs(book_dir, exist_ok=True)
        preview_dir = tempfile.mkdtemp(prefix=sanitize_filename(speaker) + "_", dir=book_dir)
        dest_path = os.path.join(preview_dir, "preview.wav")
        shutil.copy2(wav_path, dest_path)
        relative_path = os.path.relpath(dest_path, root).replace('\\', '/')
        _atomic_json_write({"book_id": book_id, "speaker": speaker,
                            "description": description, "ref_text": ref_text,
                            "preview": relative_path}, os.path.join(preview_dir, "meta.json"))
        voice_entry = copy.deepcopy(voice_config.get(speaker, {}))
        voice_entry.update({
            "type": "clone",
            "persona_status": "generated",
            "ref_audio": relative_path,
            "ref_text": ref_text,
            "description": description,
            "character_style": description,
            "seed": character_voice_seed(speaker),
        })
        voice_config[speaker] = voice_entry
    except Exception as e:
        if preview_dir is not None:
            shutil.rmtree(preview_dir)
        print(f"Error saving voice preview for {speaker}: {e}")
        return False
    finally:
        # generate_voice_design creates a unique, temporary Designer preview.
        # Only that owned file is disposable; existing book references survive.
        preview_root = os.path.realpath(os.path.join(root, 'designed_voices', 'previews'))
        generated = os.path.realpath(wav_path)
        if (os.path.dirname(generated) == preview_root
                and re.fullmatch(r'preview_[0-9a-f]{32}\.wav', os.path.basename(generated))):
            try:
                os.remove(generated)
            except FileNotFoundError:
                pass

    print(f"Persona generated and preview saved for {speaker}: {dest_path}")
    return True


def _parse_discovered_characters(parsed):
    """Normalize a discovery LLM response into a list of character dicts.

    Accepts either {"characters": [...]} or a {name: {...}} mapping.
    """
    characters = []
    if isinstance(parsed, dict):
        if "characters" in parsed and isinstance(parsed["characters"], list):
            characters = parsed["characters"]
        else:
            for key, val in parsed.items():
                if isinstance(val, dict):
                    val["name"] = key
                    characters.append(val)
    return characters


def get_validated_state_discovery(characters, batch, batch_start, allowed_speakers):
    """Reject sample/evidence rows invented or borrowed from another state."""
    sources = {row.get("_source_entry_index", batch_start + offset): row
               for offset, row in enumerate(batch)}
    output = []
    for character in characters:
        speaker = _resolve_to_canonical(str(character.get("name") or ""), allowed_speakers)
        if not speaker:
            continue
        evidence = [item for item in character.get("evidence", []) if isinstance(item, dict)
                    and type(item.get("entry_index")) is int and item["entry_index"] in sources
                    and isinstance(item.get("quote"), str) and item["quote"].strip()
                    and item["quote"] in _entry_text(sources[item["entry_index"]])]
        dialogue = [_entry_text(row) for row in batch if _entry_speaker(row) == speaker]
        samples = [line for line in _as_list(character.get("sample_lines", []))
                   if any(line in text for text in dialogue)]
        if not evidence:
            fallback = next((row for row in _fallback_batch_characters(batch, batch_start)
                             if row["name"] == speaker), None)
            if fallback:
                output.append(fallback)
            continue
        output.append({**character, "name": speaker, "evidence": evidence, "sample_lines": samples})
    return output


def _discover_batch_characters(client, model_name, prompt, batch, batch_number,
                               context_length=None, llm_config=None, batch_start=0,
                               allowed_speakers=None):
    """Run one discovery LLM call for a batch, falling back to speaker stubs on
    an empty/unparseable response or an API error. Returns a list of characters.
    """
    try:
        messages = [{"role": "system", "content": "You produce concise JSON only."},
                    {"role": "user", "content": prompt}]
        params = _persona_params(messages[0]["content"], context_length, llm_config, 4000, 0.2)
        attempts = []

        def observe(attempt):
            attempts.append(attempt)
            _persona_attempt_observer(attempt)

        parsed = call_llm_for_object(
            client, model_name, messages[0]["content"], messages[1]["content"], params,
            label=f"PERSONA DISCOVERY {batch_number}", max_retries=2,
            attempt_observer=observe)
        characters = _parse_discovered_characters(parsed)
        if (not characters and allowed_speakers is not None
                and getattr(client, "switched", False)
                and any(attempt.get("error_category") == "context_budget"
                        or attempt.get("finish_reason") == "length" for attempt in attempts)):
            if len(batch) > 1:
                middle = len(batch) // 2
                parts = [(batch_start, batch[:middle]), (batch_start + middle, batch[middle:])]
            elif batch:
                texts = split_failed_chunk(_entry_text(batch[0]))
                parts = [(batch_start, [{**batch[0], "text": text}]) for text in (texts or [])]
            else:
                parts = []
            if parts:
                characters = []
                for start, rows in parts:
                    part_prompt = _build_batch_discovery_prompt(start, rows, allowed_speakers)
                    characters.extend(_discover_batch_characters(
                        client, model_name, part_prompt, rows, batch_number,
                        context_length, llm_config, batch_start=start,
                        allowed_speakers=allowed_speakers))
                return characters
        if any("_source_entry_index" in row for row in batch):
            characters = get_validated_state_discovery(characters, batch, batch_start, allowed_speakers)
        if not characters:
            print(f"Warning: discovery batch {batch_number} returned no parseable characters; using speaker fallback.")
            characters = _fallback_batch_characters(batch, batch_start)
        return characters
    except Exception as e:
        print(f"Warning: discovery batch {batch_number} failed: {e}; using speaker fallback.")
        return _fallback_batch_characters(batch, batch_start)


def _write_batch_character_refs(ref_dir, characters, selected_speakers, batch_number):
    """Resolve discovered character names to canonical speakers and append each
    one's evidence to its per-character reference file.
    """
    for character in characters:
        if not isinstance(character, dict):
            continue
        speaker = str(character.get("name") or character.get("speaker") or character.get("speaker_label") or "").strip()
        if not speaker:
            continue

        # Map raw/fuzzy name to allowed canonical speaker labels
        canonical_speaker = _resolve_to_canonical(speaker, selected_speakers)
        if not canonical_speaker:
            continue

        character["name"] = canonical_speaker
        _append_character_ref(ref_dir, canonical_speaker, batch_number, character)


def _compile_persona(client, model_name, engine, voice_config, root, ref_dir, speaker,
                     samples, system_prompt, advanced_prompt, context_length=None,
                     llm_config=None, book_id=None, preview_saver=None, context_lines=DEFAULT_CONTEXT_LINES):
    """Compile one speaker's accumulated reference data into a final persona
    (description + ref_text) and generate its preview audio. A supplied
    preview_saver handles this call only; production uses its usual saver.
    """
    ref = _load_character_ref(ref_dir, speaker)
    sample_limit = 30  # Keep the existing bound for discovered reference lines.
    if not ref.get("sample_lines"):
        sample_limit = get_persona_context_limit(context_lines)
        ref["sample_lines"] = [line for line in samples.get(speaker, [])[:sample_limit] if line]
        _atomic_json_write(ref, _character_ref_path(ref_dir, speaker))

    print(f"Compiling persona for: {speaker}")
    description = ""
    ref_text = ""
    try:
        messages = [{"role": "system", "content": system_prompt or "You produce concise JSON only."},
                    {"role": "user", "content": _compile_character_prompt(ref, advanced_prompt, sample_limit=sample_limit)}]
        params = _persona_params(messages[0]["content"], context_length, llm_config, 600, 0.25)
        # Preserve the existing selected preview, including its explicit truncation marker.
        selected_reference = _compile_character_prompt(ref, "{character_ref}", sample_limit=sample_limit)

        def build_prompt(parts):
            reference = (selected_reference if parts == [("character_ref", selected_reference)]
                         else json.dumps({"name": speaker, "selected_reference_fragments": [
                             text for _, text in parts]}, ensure_ascii=False))
            return _compile_character_prompt(ref, advanced_prompt, reference_text=reference, sample_limit=sample_limit)

        parsed = request_persona_with_evidence(
            client, model_name, messages[0]["content"], build_prompt,
            [("character_ref", selected_reference)], params, f"PERSONA COMPILE {speaker}")
        if isinstance(parsed, dict):
            try:
                validated = validate_persona_payload(parsed)
                description, ref_text = validated["description"], validated["ref_text"]
            except ValueError as exc:
                print(f"Warning: persona integrity check failed for {speaker}: {exc}")
    except PersonaContextRecoveryError:
        raise
    except Exception as e:
        print(f"Warning: compile failed for {speaker}: {e}")

    if not description:
        description, ref_text = _fallback_compiled_persona(ref)
    if not ref_text:
        ref_text = pick_ref_text(samples.get(speaker, []))
    if not ref_text:
        ref_text = f"{speaker} speaks in a clear, natural voice."
    if not description:
        print(f"Warning: Empty compiled description for {speaker}, skipping")
        return False

    voice_entry = voice_config.get(speaker, {})
    voice_entry["persona_ref"] = os.path.relpath(_character_ref_path(ref_dir, speaker), root).replace('\\', '/')
    voice_config[speaker] = voice_entry
    save_preview = preview_saver if preview_saver is not None else _save_generated_preview
    if book_id is None:
        saved = save_preview(root, engine, voice_config, speaker, description, ref_text)
    else:
        saved = save_preview(root, engine, voice_config, speaker, description, ref_text, book_id=book_id)
    return saved


def get_recovered_persona(voice_entry, age_group):
    """Select a compatible saved persona without relabelling another age."""
    requested = age_group.strip()
    selected = voice_entry
    if requested:
        versions = voice_entry.get("versions")
        if isinstance(versions, dict) and isinstance(versions.get(requested), dict):
            selected = versions[requested]
            if selected.get("age_group", requested) != requested:
                raise ValueError(f"Saved persona version does not match age group {requested}")
        elif requested not in (voice_entry.get("active_version"), voice_entry.get("age_group")):
            raise ValueError(f"No recovered persona for age group {requested}; generate that version first")
    recovered = copy.deepcopy(voice_entry)
    recovered.update(validate_persona_payload(selected))
    if requested:
        recovered["active_version"] = requested
        recovered["age_group"] = requested
    return recovered


def _run_advanced_speaker_generation(script, selected_speakers, samples, voice_config, client, model_name, engine, root, args, system_prompt=None, advanced_prompt=None, context_length=None, llm_config=None, book_id=None):
    selected_speakers = list(selected_speakers)
    failures = []
    recovered_speaker = getattr(args, "recovered_speaker", "")
    if recovered_speaker in selected_speakers and isinstance(voice_config.get(recovered_speaker), dict):
        try:
            recovered = get_recovered_persona(voice_config[recovered_speaker], getattr(args, "age_group", ""))
            voice_config[recovered_speaker] = recovered
            print(f"Using recovered persona for {recovered_speaker}; skipping discovery and compilation.")
            if not _save_generated_preview(root, engine, voice_config, recovered_speaker,
                                           recovered["description"], recovered["ref_text"], book_id=book_id):
                failures.append(recovered_speaker)
        except Exception as error:
            print(f"Recovered persona failed for {recovered_speaker}: {error}")
            failures.append(recovered_speaker)
        selected_speakers.remove(recovered_speaker)
    if not selected_speakers:
        return failures
    if book_id is None:
        state = safe_load_json(os.path.join(root, "state.json"), default={})
        book_id = state.get("active_book_id") or "active_book"
    book_refs_dir = os.path.join(root, "persona_refs", sanitize_filename(book_id))
    os.makedirs(book_refs_dir, exist_ok=True)
    # A fresh run owns fresh references. Saved books and concurrent generations
    # may still point at previous files, so never delete or reuse their directory.
    ref_dir = tempfile.mkdtemp(prefix="generation_", dir=book_refs_dir)

    batch_size = max(1, int(args.batch_size or 40))
    batch_count = (len(script) + batch_size - 1) // batch_size
    batches = _batch_entries(script, batch_size)
    print(f"Advanced persona generation enabled.")
    print(f"Writing per-character reference files to: {ref_dir}")
    print(f"Processing {len(script)} script entries in {batch_count} batches of up to {max(1, int(args.batch_size or 40))}")

    # Phase 1: discover characters batch-by-batch, accumulating per-character refs.
    for batch_number, (batch_start, batch) in enumerate(batches, start=1):
        prompt = _build_batch_discovery_prompt(batch_start, batch, selected_speakers)
        print(f"Advanced discovery batch {batch_number}/{batch_count} ({len(batch)} entries)")
        characters = _discover_batch_characters(
            client, model_name, prompt, batch, batch_number, context_length, llm_config,
            batch_start=batch_start, allowed_speakers=selected_speakers)
        _write_batch_character_refs(ref_dir, characters, selected_speakers, batch_number)

    # Phase 2: compile each speaker's refs into a final persona + preview.
    print("Compiling character reference files into final voice personas.")
    for speaker in selected_speakers:
        try:
            if _compile_persona(client, model_name, engine, voice_config, root, ref_dir,
                                speaker, samples, system_prompt, advanced_prompt, context_length,
                                llm_config, book_id=book_id,
                                context_lines=getattr(args, "context_lines", DEFAULT_CONTEXT_LINES)) is False:
                failures.append(speaker)
        except Exception as error:
            print(f"Unhandled error for {speaker}: {error}")
            failures.append(speaker)
    return failures


def get_pending_state_targets(script, speaker, voice, new_only=False, version_id="", *, state_targets=None):
    targets = (get_persona_state_targets(script) if state_targets is None else state_targets).get(speaker, [])
    if version_id:
        targets = [target for target in targets if target["version_id"] == version_id]
        if not targets:
            raise ValueError("The selected character state is no longer in the script")
    if new_only:
        targets = [target for target in targets if not voice_is_set(
            (voice or {}).get("versions", {}).get(target["version_id"])) or
            (voice or {}).get("versions", {}).get(target["version_id"], {}).get("persona_state") != target]
    return targets


def run_advanced_persona_generation(script, selected_speakers, samples, voice_config,
                                   client, model_name, engine, root, args, **options):
    """Compile isolated settled-state evidence into existing voice versions."""
    publish = options.pop("state_publisher", None)
    all_targets = options.pop("state_targets", None)
    if all_targets is None:
        all_targets = get_persona_state_targets(script)
    failures = []
    regular = []
    for speaker in selected_speakers:
        if speaker not in all_targets or getattr(args, "age_group", ""):
            regular.append(speaker)
            continue
        voice = copy.deepcopy(voice_config.get(speaker) or {})
        targets = get_pending_state_targets(script, speaker, voice,
                    getattr(args, "new_only", False), getattr(args, "state_version", ""), state_targets=all_targets)
        print(f"State personas for {speaker}: {len(targets)} targets")
        for number, target in enumerate(targets, 1):
            version_id = target["version_id"]
            print(f"State {number}/{len(targets)}: {speaker} · {target['gender']} · {target['age_group']} · segment {target['state_number']}")
            entries = get_persona_state_entries(script, target)
            state_samples = {speaker: [_entry_text(row) for row in entries
                                      if _entry_speaker(row) == speaker]}
            # Never import another state's compiled observations or preview.
            state_config = {speaker: copy.deepcopy(voice.get("versions", {}).get(version_id) or {})}
            state_args = copy.copy(args)
            state_args.age_group = ""
            state_args.recovered_speaker = (getattr(args, "recovered_speaker", "")
                                            if getattr(args, "state_version", "") == version_id else "")
            state_options = dict(options)
            prompt_speaker = speaker.replace("{", "{{").replace("}", "}}")
            state_options["advanced_prompt"] = (options.get("advanced_prompt") or PERSONA_ADVANCED_PROMPT) + (
                f"\nThis evidence is exclusively {prompt_speaker}'s settled {target['gender']}, "
                f"{target['age_group']} state. Use only its supplied lines; do not mix other ages or states.\n")
            try:
                failed = _run_advanced_speaker_generation(entries, [speaker], state_samples,
                    state_config, client, model_name, engine, root, state_args, **state_options)
            except Exception as error:
                print(f"State persona failed for {speaker}/{version_id}: {error}")
                failed = [speaker]
            if failed:
                failures.append(f"{speaker}/{version_id}")
                continue
            generated = state_config[speaker]
            generated["persona_state"] = copy.deepcopy(target)
            generated["age_group"] = target["age_group"]
            generated["gender"] = target["gender"]
            generated.pop("active_candidate", None)
            generated["seed"] = character_voice_seed(f"{speaker}:{version_id}")
            generated["persona_status"] = "generated"
            generated["voice_status"] = "generated"
            voice.setdefault("versions", {})[version_id] = generated
            if target["state_number"] == 1 and not voice_is_set(voice):
                voice.update({key: copy.deepcopy(value) for key, value in generated.items()
                              if key not in {"persona_state", "versions", "version_timeline"}})
            voice_config[speaker] = copy.deepcopy(voice)
            if publish is not None:
                published = publish(voice_config, speaker, version_id)
                voice_config.clear()
                voice_config.update(published)
                if speaker not in voice_config:
                    raise ValueError("Character removed during state persona generation")
                voice = copy.deepcopy(voice_config[speaker])
    if regular:
        failures.extend(_run_advanced_speaker_generation(script, regular, samples,
            voice_config, client, model_name, engine, root, args, **options))
    return failures


# _atomic_json_write imported from utils


def get_validated_voice_alias_map(voice_config):
    """Return the validated alias graph from a voice configuration."""
    if not isinstance(voice_config, dict):
        raise ValueError("voice configuration must be an object")
    return get_validated_alias_graph({
        speaker: voice["alias_of"] for speaker, voice in voice_config.items()
        if isinstance(voice, dict) and voice.get("alias_of") not in (None, "")})


def save_generated_voice_config(path, generated, initial, roster, alias_proposals, book_snapshot=None):
    """Publish generated changes while preserving newer human edits."""
    book_guard = ensure_book_state(os.path.dirname(path)) if book_snapshot is not None else nullcontext()
    with book_guard, file_lock(path):
        if book_snapshot is not None:
            require_book_snapshot_current(os.path.dirname(path), book_snapshot)
        current = safe_load_json(path, default=None) if os.path.exists(path) else {}
        existing_aliases = get_validated_voice_alias_map(current)
        merged = copy.deepcopy(current)
        missing = object()
        for speaker, value in generated.items():
            before = initial.get(speaker, missing)
            latest = current.get(speaker, missing)
            if before is not missing and latest is missing:
                continue  # A human removed this entry during generation.
            if isinstance(value, dict):
                if latest is missing:
                    if speaker in alias_proposals:
                        continue
                    merged[speaker] = {key: copy.deepcopy(item) for key, item in value.items()
                                       if key != "alias_of"}
                elif isinstance(before, dict) and isinstance(latest, dict):
                    for field in set(before) | set(value):
                        if field == "alias_of" or (speaker in alias_proposals and field == "seed"):
                            continue
                        if field == "versions" and isinstance(value.get(field), dict):
                            previous = before.get(field) or {}
                            current_versions = latest.get(field) or {}
                            for version_id, version in value[field].items():
                                old_version = previous.get(version_id, missing)
                                if version != old_version and current_versions.get(version_id, missing) == old_version:
                                    merged[speaker].setdefault(field, {})[version_id] = copy.deepcopy(version)
                                elif version != old_version:
                                    print(f"[skip] {speaker}/{version_id}: version changed during generation")
                            continue
                        old = before.get(field, missing)
                        new = value.get(field, missing)
                        if new != old and latest.get(field, missing) == old:
                            if new is missing:
                                merged[speaker].pop(field, None)
                            else:
                                merged[speaker][field] = copy.deepcopy(new)
            elif latest == before and value != before:
                merged[speaker] = copy.deepcopy(value)
        proposals = {}
        for speaker, canonical in alias_proposals.items():
            if current.get(speaker, missing) != initial.get(speaker, missing):
                print(f"  [skip] alias proposal for '{speaker}': voice entry changed during generation")
                continue
            proposals[speaker] = canonical
        accepted = get_safe_alias_proposals([proposals], list(roster) + list(merged), existing_aliases)
        for speaker, canonical in accepted.items():
            voice = merged.setdefault(speaker, {})
            voice["alias_of"] = canonical
            voice.setdefault("seed", generated.get(speaker, {}).get("seed", -1))
        get_validated_voice_alias_map(merged)
        if merged != current or not os.path.exists(path):
            _atomic_json_write(merged, path)
    return merged


def main():
    parser = argparse.ArgumentParser(description="Generate personas for speakers in annotated script")
    parser.add_argument("--new-only", action="store_true", help="Only speakers without a voice yet (tts.voice_is_set)")
    parser.add_argument("--alias-check", action="store_true", help="Use LLM + heuristics to decide alias_of vs truly new character")
    parser.add_argument("--advanced", action="store_true", help="Batch the full script into per-character reference files before compiling voice personas")
    parser.add_argument("--batch-size", type=int, default=40, help="Script entries per advanced discovery batch")
    parser.add_argument("--speakers", default="", help="Optional comma-separated speaker allowlist")
    parser.add_argument("--book-token", default="", help="Expected active-book identity for targeted generation")
    parser.add_argument("--state-version", default="", help="Regenerate one settled state version from its own evidence")
    parser.add_argument("--age-group", default="", help="Optional age profile to store as a separate voice version")
    parser.add_argument("--recovered-speaker", default="", help="Use the saved persona for this speaker and resume preview generation")
    parser.add_argument("--narration-window", type=int, default=4, help="How many preceding narrator lines to include as intro context")
    parser.add_argument("--context-lines", type=int, default=DEFAULT_CONTEXT_LINES,
                        help="Sample spoken lines per character fed to the persona prompt; the narrator window grows to half of it")
    args = parser.parse_args()
    if args.state_version:
        if args.age_group:
            parser.error("--state-version cannot be combined with --age-group")
        args.advanced = True

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    app_dir = os.path.dirname(__file__)
    data_dir = get_runtime_data_dir(root)
    script_path = os.path.join(data_dir, "annotated_script.json")
    voice_config_path = os.path.join(data_dir, "voice_config.json")
    app_config_path = get_app_config_path(data_dir, root, app_dir)

    if not os.path.exists(script_path):
        print(f"Error: {script_path} not found. Generate script first.")
        sys.exit(1)

    with ensure_book_state(data_dir), file_lock(voice_config_path):
        book_snapshot = get_book_snapshot(data_dir)
    if args.book_token and get_book_snapshot_token(book_snapshot) != args.book_token:
        raise ValueError("Active book changed before persona generation started")
    script = json.loads(book_snapshot["script_bytes"])
    state_targets = get_persona_state_targets(script) if args.advanced or args.state_version else {}
    if args.state_version:
        allowed = {target["version_id"] for speaker, targets in state_targets.items()
                   if speaker in args.speakers.split(",") for target in targets}
        if args.state_version not in allowed:
            raise ValueError("Character state changed before persona generation started")

    # Collect sample lines per speaker + first-appearance narrator context
    samples = {}
    for entry in script:
        speaker = _entry_speaker(entry)
        if not speaker:
            continue
        samples.setdefault(speaker, []).append(entry.get("text", "").strip())

    narrator_context = {}
    context_lines = get_persona_context_limit(args.context_lines)
    window = max(1, int(args.narration_window or 4), context_lines // 2)
    for speaker in samples.keys():
        narrator_context[speaker] = _collect_narrator_context(script, speaker, window)

    # Load existing voice_config (preserve other fields)
    voice_config = copy.deepcopy(book_snapshot["voices"])
    original_aliases = get_validated_voice_alias_map(voice_config)
    initial_voice_config = copy.deepcopy(voice_config)

    selected_speakers = list(samples.keys())
    if args.new_only:
        from tts import voice_is_set
        selected_speakers = [s for s in selected_speakers if not voice_is_set(voice_config.get(s))
                             or (args.advanced and get_pending_state_targets(script, s, voice_config.get(s), True, state_targets=state_targets))]
    if args.speakers.strip():
        allow = {s.strip() for s in args.speakers.split(",") if s.strip()}
        selected_speakers = [s for s in selected_speakers if s in allow]

    if not selected_speakers:
        print("No speakers to process.")
        return

    if args.advanced:
        selected_speakers = [speaker for speaker in selected_speakers
                             if resolve_speaker_label(speaker, original_aliases) is None]
        if not selected_speakers:
            print("No unique speakers to process; existing aliases are preserved.")
            return

    # Load LLM config
    config = load_app_config(app_config_path)

    llm_cfg = get_active_llm_config(config)
    base_url = llm_cfg.get("base_url", "http://localhost:11434/v1")
    api_key = llm_cfg.get("api_key", "local")
    model_name = llm_cfg.get("model_name", "richardyoung/qwen3-14b-abliterated:Q8_0")
    llm_mode = config.get("llm_mode", "local")

    # Self-heal a stale/misconfigured local or remote LM Studio before making
    # any calls, mirroring review_script.py/find_nicknames.py. This file has
    # no VRAM watchdog or concurrency wave processing of its own (personas
    # are generated sequentially per speaker/batch), so only the self-heal
    # call applies here.
    _, lm_status, heal_msg = ensure_ideal_settings(
        llm_mode, base_url, model_name, ssh_alias=config.get("llm_remote_ssh"), api_key=api_key)
    print(heal_msg)

    client = make_run_client(config, llm_cfg, llm_timeout_seconds())

    # Load persona prompts from config, fall back to defaults
    prompts_cfg = config.get("prompts") or {}
    persona_system = prompts_cfg.get("persona_system_prompt") or PERSONA_SYSTEM_PROMPT
    persona_user = prompts_cfg.get("persona_user_prompt") or PERSONA_USER_PROMPT
    persona_advanced = prompts_cfg.get("persona_advanced_prompt") or PERSONA_ADVANCED_PROMPT
    age_instruction = ""
    if args.age_group.strip():
        age_instruction = (f"\nCreate this persona for the character's {args.age_group.strip()} age profile; "
                           "keep the same identity while adapting apparent age and delivery.\n")
        persona_user = persona_user + age_instruction
        persona_advanced = persona_advanced + age_instruction

    # Disable compile_codec for persona previews: compilation overhead
    # outweighs benefit for single generations, and subprocess context
    # can trigger HIP kernel errors on ROCm.
    tts_cfg = dict(config.get("tts", {}))
    tts_cfg["compile_codec"] = False
    engine = TTSEngine({"tts": tts_cfg})


    print(f"Processing {len(selected_speakers)} speakers")

    # Step 1: Pre-process exact normalized aliases only. Similar names can be
    # distinct people (for example NITA and NITA'S DAD), so fuzzy identity
    # decisions remain behind the explicit alias-check workflow below.
    resolved_aliases = {}
    remaining_speakers = []

    for speaker in selected_speakers:
        existing_names = [n for n in voice_config.keys() if n != speaker]
        heuristic_alias = get_exact_alias_match(speaker, existing_names)

        if heuristic_alias:
            print(f"Exact-name alias detected: {speaker} -> {heuristic_alias}")
            resolved_aliases[speaker] = heuristic_alias
        else:
            remaining_speakers.append(speaker)

    # Step 2: One-shot batch alias resolution for the remaining speakers (if alias-check is enabled)
    if args.alias_check and remaining_speakers:
        print(f"Running one-shot batch alias resolution for {len(remaining_speakers)} candidates...")
        
        # Split remaining speakers into chunks of 25 to prevent context/output token exhaustion
        chunk_size = 25
        batch_mapping = {}
        batch_proposals = []
        
        for idx in range(0, len(remaining_speakers), chunk_size):
            chunk = remaining_speakers[idx:idx + chunk_size]
            speakers_info = {}
            for speaker in chunk:
                speakers_info[speaker] = {
                    "sample_lines": samples.get(speaker, []),
                    "narrator_context": narrator_context.get(speaker, [])
                }
            
            # Use current configured names plus any previously resolved canonical names as existing references
            existing_configured = list(voice_config.keys()) + list(batch_mapping.values())
            
            print(f"Resolving alias batch {idx//chunk_size + 1} ({len(chunk)} speakers)...")
            chunk_mapping = _resolve_aliases_batch(client, model_name, speakers_info, existing_configured,
                                                   lm_status.get("context_length"), llm_cfg,
                                                   comparison_names=list(voice_config) + selected_speakers)
            batch_proposals.append(chunk_mapping)
            batch_mapping = get_safe_alias_proposals(
                batch_proposals, list(voice_config) + selected_speakers, original_aliases, flatten=False)

        for speaker in remaining_speakers:
            llm_key = get_exact_alias_match(speaker, batch_mapping)
            resolved_name = batch_mapping.get(llm_key) or speaker
            if resolved_name != speaker:
                # LLM identified this as an alias!
                all_possible = list(voice_config.keys()) + remaining_speakers
                canonical_target = get_exact_alias_match(resolved_name, all_possible)
                if canonical_target and canonical_target != speaker:
                    print(f"Batch LLM alias detected: {speaker} -> {canonical_target}")
                    resolved_aliases[speaker] = canonical_target
                else:
                    print(f"Batch LLM mapped '{speaker}' to '{resolved_name}' but couldn't resolve canonical spelling. Treating as new.")

    raw_aliases = dict(resolved_aliases)
    resolved_aliases = get_safe_alias_proposals(
        [resolved_aliases], list(voice_config) + selected_speakers, original_aliases)
    final_alias_proposals = {speaker: raw_aliases[speaker] for speaker in resolved_aliases}

    # Apply all resolved aliases to voice_config
    for speaker, alias_target in resolved_aliases.items():
        voice_entry = voice_config.get(speaker, {})
        voice_entry.update({
            "alias_of": alias_target,
            "seed": voice_entry.get("seed", -1),
        })
        voice_config[speaker] = voice_entry

    # Step 3: Generate personas for remaining truly unique speakers
    unique_speakers = [s for s in remaining_speakers
                       if s not in resolved_aliases
                       and resolve_speaker_label(s, original_aliases) is None]
    print(f"Generating personas for {len(unique_speakers)} unique speakers...")

    if args.advanced:
        publication_base = copy.deepcopy(initial_voice_config)

        def publish_states(generated, speaker, version_id):
            nonlocal publication_base
            current = save_generated_voice_config(voice_config_path, generated,
                        publication_base, samples.keys(), final_alias_proposals,
                        book_snapshot=book_snapshot)
            # Refresh only the completed target's version baseline. A user may
            # have edited/deleted a later target while this one was generating.
            previous_versions = copy.deepcopy(publication_base.get(speaker, {}).get("versions") or {})
            completed = current.get(speaker, {}).get("versions", {}).get(version_id)
            if completed is not None:
                previous_versions[version_id] = copy.deepcopy(completed)
            else:
                previous_versions.pop(version_id, None)
            refreshed = copy.deepcopy(publication_base)
            if speaker in current:
                refreshed[speaker] = copy.deepcopy(current[speaker])
                refreshed[speaker]["versions"] = previous_versions
            else:
                refreshed.pop(speaker, None)
            publication_base = refreshed
            return current

        failures = run_advanced_persona_generation(
            script=script,
            selected_speakers=unique_speakers,
            samples=samples,
            voice_config=voice_config,
            client=client,
            model_name=model_name,
            engine=engine,
            root=data_dir,
            args=args,
            system_prompt=persona_system,
            advanced_prompt=persona_advanced,
            context_length=lm_status.get("context_length"),
            llm_config=llm_cfg,
            book_id=book_snapshot["book_id"],
            state_publisher=publish_states,
            state_targets=state_targets,
        )
        if args.age_group.strip():
            for speaker in unique_speakers:
                if speaker in failures:
                    continue
                current = voice_config.get(speaker)
                if not isinstance(current, dict):
                    continue
                snapshot = {k: v for k, v in current.items()
                            if k not in {"versions", "candidates", "active_version", "active_candidate"}}
                snapshot["age_group"] = args.age_group.strip()
                current.setdefault("versions", {})[args.age_group.strip()] = snapshot
        try:
            save_generated_voice_config(voice_config_path, voice_config,
                                        initial_voice_config, samples.keys(), final_alias_proposals, book_snapshot=book_snapshot)
            print(f"Updated voice_config saved to {voice_config_path}")
        except Exception as e:
            print(f"Failed to save voice_config.json: {e}")
            raise
        if failures:
            raise RuntimeError("Persona generation failed for: " + ", ".join(failures))
        return

    failures = []

    for speaker in unique_speakers:
        lines = samples.get(speaker, [])
        try:
            print(f"Generating persona for: {speaker} ({len(lines)} lines samples)")

            sample_text, intro_blob = select_persona_context(
                lines, narrator_context.get(speaker, []), context_lines)

            user_prompt = persona_user.format(
                speaker=speaker,
                narrator_context=intro_blob,
                sample_lines=sample_text
            )

            messages = [
                {"role": "system", "content": persona_system},
                {"role": "user", "content": user_prompt}
            ]
            recovered = (speaker == args.recovered_speaker and
                         isinstance(voice_config.get(speaker), dict))
            if recovered:
                parsed = get_recovered_persona(voice_config[speaker], args.age_group)
                voice_config[speaker] = parsed
                print(f"Using recovered persona for {speaker}; skipping the LLM request.")
            else:
                params = _persona_params(messages[0]["content"], lm_status.get("context_length"),
                                         llm_cfg, 400, 0.3)
                evidence = [("sample_lines", line) for line in lines[:context_lines]]
                evidence += [("narrator_context", line) for line in
                             narrator_context.get(speaker, [])[:context_lines]]

                def build_prompt(parts):
                    return persona_user.format(
                        speaker=speaker,
                        narrator_context="\n".join(text for kind, text in parts
                                                  if kind == "narrator_context")
                        or "(No nearby narrator intro lines found.)",
                        sample_lines="\n".join(text for kind, text in parts if kind == "sample_lines"))

                parsed = request_persona_with_evidence(
                    client, model_name, messages[0]["content"], build_prompt, evidence,
                    params, f"PERSONA {speaker}")
            description = ""
            ref_text = ""
            if isinstance(parsed, dict):
                try:
                    validated = validate_persona_payload(parsed)
                    description, ref_text = validated["description"], validated["ref_text"]
                except ValueError as exc:
                    print(f"Warning: persona integrity check failed for {speaker}: {exc}")

            if not description:
                print(f"Warning: LLM did not return a valid persona for {speaker}; using fallback.")
                # Fallback to compiled fallback persona
                description, ref_text = _fallback_compiled_persona({
                    "name": speaker,
                    "sample_lines": lines
                })

            if not ref_text:
                ref_text = pick_ref_text(lines)

            # Generate and save voice preview
            if not _save_generated_preview(data_dir, engine, voice_config, speaker, description, ref_text,
                                           book_id=book_snapshot["book_id"]):
                failures.append(speaker)
                continue
            if args.age_group.strip() and isinstance(voice_config.get(speaker), dict):
                current = voice_config[speaker]
                snapshot = {k: v for k, v in current.items()
                            if k not in {"versions", "candidates", "active_version", "active_candidate"}}
                snapshot["age_group"] = args.age_group.strip()
                current.setdefault("versions", {})[args.age_group.strip()] = snapshot

        except Exception as e:
            print(f"Unhandled error for {speaker}: {e}")
            failures.append(speaker)

    # Persist voice_config
    try:
        save_generated_voice_config(voice_config_path, voice_config,
                                    initial_voice_config, samples.keys(), final_alias_proposals, book_snapshot=book_snapshot)
        print(f"Updated voice_config saved to {voice_config_path}")
    except Exception as e:
        print(f"Failed to save voice_config.json: {e}")
        raise

    if failures:
        raise RuntimeError("Persona generation failed for: " + ", ".join(failures))


if __name__ == '__main__':
    main()
