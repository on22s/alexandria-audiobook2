"""A card's saved voice candidates: the one place a voice becomes a candidate.

Invariant (#1297): every voice a card holds is also one of its saved
candidates, and `active_candidate` names it. Designing, generating a persona
and pressing "Use" all go through get_entry_with_current_candidate, so the voice
being replaced is always recoverable from the list.
"""
import copy
from datetime import datetime, timezone
import hashlib
import json

from tts import voice_is_set

# Fields that belong to the character, not to one voice: a saved candidate
# never carries them, so using it later cannot rewind them.
CANDIDATE_EXCLUDED = {"versions", "candidates", "active_version", "active_candidate",
                      "style_timeline", "narrator_strategy", "alias_of", "ready",
                      "persona_status", "voice_status", "age_group", "persona_state",
                      "version_timeline"}
# Bookkeeping that belongs to the saved candidate, never to the voice: "Use"
# does not copy it onto the card, and it is not part of a voice's identity.
CANDIDATE_METADATA = {"candidate_id", "saved_at", "favorite", "rank"}


def get_voice_fields(entry):
    """The fields that make up the voice itself, for comparing two voices."""
    return {key: value for key, value in entry.items()
            if key not in CANDIDATE_EXCLUDED and key not in CANDIDATE_METADATA}


def get_voice_candidate(entry):
    """-> the voice `entry` holds now as a saved candidate, or None when no voice is set.

    The id is a hash of the voice itself, so saving the same voice twice gives
    the same id and never duplicates it."""
    if not isinstance(entry, dict) or not voice_is_set(entry):
        return None
    snapshot = copy.deepcopy(get_voice_fields(entry))
    identity = json.dumps(snapshot, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return {**snapshot, "candidate_id": "design-" + hashlib.sha256(identity).hexdigest()[:12]}


def get_saved_at(now=None):
    """The UTC time a candidate is saved, as the list shows it (#1298)."""
    return (now or datetime.now(timezone.utc)).strftime("%Y-%m-%dT%H:%M:%SZ")


def get_entry_with_current_candidate(entry, now=None):
    """-> a copy of `entry` whose current voice is among its candidates and active.

    A voice already saved keeps its place and its saved_at time (the list is in
    saving order); a newly saved one records when (#1298). An entry with no
    voice comes back unchanged."""
    candidate = get_voice_candidate(entry)
    if candidate is None:
        return copy.deepcopy(entry)
    updated = copy.deepcopy(entry)
    candidates = [item for item in updated.get("candidates") or [] if isinstance(item, dict)]
    # Already saved means the same voice, under whatever id: an auto-suggested
    # LoRA candidate is keyed by its adapter id, not by this hash.
    voice = get_voice_fields(candidate)
    saved = next((item for item in candidates if get_voice_fields(item) == voice), None)
    if saved is None:
        saved = {**candidate, "saved_at": get_saved_at(now)}
        candidates.append(saved)
    updated["candidates"] = candidates
    updated["active_candidate"] = saved["candidate_id"]
    return updated


# A character's seed is assigned per speaker on purpose; a candidate saved
# without one does not clear it.
KEPT_WHEN_ABSENT = {"seed"}


def get_entry_using_candidate(entry, candidate, kept=()):
    """-> a copy of `entry` whose voice is exactly `candidate`'s, active.

    Voice fields the candidate does not have are cleared, so the card holds
    that voice and nothing left over from the previous one; `kept` names
    fields the caller protects (a state version keeps its own description)."""
    kept = set(kept)
    updated = {key: copy.deepcopy(value) for key, value in entry.items()
               if key in kept or key in KEPT_WHEN_ABSENT or key not in get_voice_fields(entry)
               or key in candidate}
    updated.update({key: copy.deepcopy(value) for key, value in candidate.items()
                    if key not in CANDIDATE_METADATA and key not in kept})
    updated["active_candidate"] = candidate["candidate_id"]
    return updated
