from book_state_transaction import ensure_book_state, get_book_snapshot, get_book_snapshot_token
import copy
import asyncio
import gc
import json
import logging
import os
import re
import signal
import sys
import time
from typing import Dict, List, Optional, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException
from pydantic import BaseModel, Field, model_validator, field_validator
from config_settings import load_app_config
from voice_config_store import apply_voice_config_update, get_voice_config_revision, VoiceConfigConflict

from core import (
    CHUNKS_PATH,
    CAST_MAJOR_LINE_THRESHOLD,
    CONFIG_PATH,
    LLMConfigError,
    LORA_MODELS_MANIFEST,
    LORA_MODELS_DIR,
    SCRIPT_PATH,
    VOICE_CONFIG_PATH,
    VOICE_LIBRARY_PATH,
    _load_builtin_lora_manifest,
    _load_manifest,
    _load_voice_library,
    _make_library_entry,
    _make_llm_client,
    _norm_name,
    _script_line_counts, get_script_line_counts,
    _send_signal_tree,
    _warn_corrupted_json,
    check_global_gpu_lock,
    claim_gpu_task, schedule_claimed_background_task, run_claimed_task_worker,
    reserve_background_task, register_claimed_background_task, release_gpu_task_claim,
    get_active_book_id,
    get_cast_adapter_usage,
    get_cast_member_key,
    get_cast_storage_pool,
    get_trait_assignment_metadata,
    process_state,
    project_manager,
    run_process,
)
from lmstudio_settings import get_active_llm_config, get_current_status, get_effective_max_tokens
from voice_manifest import get_adapter_id_alias_map, get_adapter_manifest_rows
from tts import VERSION_OVERLAY_EXCLUDED, get_style_timeline_index, get_version_fields, resolve_narrator_voice_config, voice_category, voice_is_set
from speaker_traits import (get_age_distance, get_chunk_index_for_entry, get_library_age_group,
                            get_entry_speaker, get_normalized_age_group,
                            get_speaker_trait_summary, get_state_timeline, get_persona_state_targets, get_persona_state_chunk_indices, get_persona_state_entries,
                            find_state_target, get_state_version_identity, is_state_version_current)
from utils import (
    atomic_json_write,
    atomic_json_write_pair,
    character_voice_seed,
    extract_json_object,
    file_lock,
    safe_load_json,
    secure_filename,
)
from persona_validation import validate_persona_payload
from repair_voice_config import get_unseeded_voice_seed_changes, apply_unseeded_voice_seeds


logger = logging.getLogger("AlexandriaUI")
router = APIRouter()


NarratorStrategy = Literal["global", "focus", "chapter", "character", "gender", "age",
                           "gender_age", "character_gender", "character_age", "character_gender_age"]


class VoiceStylePoint(BaseModel):
    model_config = {"extra": "allow"}
    from_index: int = 0
    character_style: Optional[str] = ""

    @field_validator("from_index", mode="before")
    @classmethod
    def validate_from_index(cls, value):
        return get_style_timeline_index({"from_index": value})


class VoiceVersionPoint(BaseModel):
    """From chunk `from_index` on, the character speaks with voice version
    `version_id` (a settled age/gender state, #653); None = the main voice
    again, for a state that changes back."""
    from_index: int = 0
    version_id: Optional[str] = Field(default=None, min_length=1, max_length=80)

    @field_validator("from_index", mode="before")
    @classmethod
    def validate_from_index(cls, value):
        return get_style_timeline_index({"from_index": value})


class VoiceConfigItem(BaseModel):
    type: str = "custom"
    voice: Optional[str] = "Ryan"
    character_style: Optional[str] = ""
    default_style: Optional[str] = ""  # backward compat, prefer character_style
    seed: Optional[str] = "-1"
    ref_audio: Optional[str] = None
    ref_text: Optional[str] = None
    adapter_id: Optional[str] = None
    adapter_path: Optional[str] = None
    description: Optional[str] = ""  # voice description (for design type)
    members: Optional[List[str]] = None  # speaker names to voice at once (ensemble type)
    # "Alias of" in the Voices tab: speak with this other speaker's voice
    # (ProjectManager._resolve_alias). Undeclared, the field was dropped by
    # model_dump() on every save, so no alias set in the UI ever took effect.
    alias_of: Optional[str] = None
    # Character approved by the user for this book (#522 17.1); a UI flag,
    # nothing downstream reads it.
    ready: bool = False
    persona_status: str = "unreviewed"
    voice_status: str = "unassigned"
    active_version: Optional[str] = None
    active_candidate: Optional[str] = None
    age_group: Optional[str] = None
    versions: Dict[str, Dict] = Field(default_factory=dict)
    candidates: List[Dict] = Field(default_factory=list)
    narrator_strategy: Optional[NarratorStrategy] = None
    # Identity anchors that take over from a line onward (tts.active_character_style):
    # [{"from_index": N, "character_style": "..."}]. Set from the Editor.
    style_timeline: List[VoiceStylePoint] = Field(default_factory=list)
    version_timeline: List[VoiceVersionPoint] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_ensemble_members(self):
        if self.type == "ensemble" and not self.members:
            raise ValueError("ensemble voices must include at least one member")
        if self.type == "ensemble" and any(not member.strip() for member in self.members):
            raise ValueError("ensemble member names cannot be blank")
        return self

BOOK_TOKEN_PATTERN = r"^[0-9a-f]{64}$"


class StateRequestScope(BaseModel):
    """A request about one character state of the book the client is showing
    (#1040 review C35: one definition for every state request)."""
    state_version: Optional[str] = Field(default=None, min_length=1, max_length=80)
    book_token: Optional[str] = Field(default=None, pattern=BOOK_TOKEN_PATTERN)


class SuggestVoicesRequest(StateRequestScope):
    only_unset: bool = False  # only suggest for characters not already set to a lora/builtin_lora voice
    max_lines: int = 8        # how many sample dialogue lines per character to feed the matcher
    cast: Optional[str] = None
    characters: Optional[List[str]] = None

class VoiceSuggestionApplyRequest(BaseModel):
    character: str
    cast: Optional[str] = None
    suggestion: Dict

class VoiceSuggestionApplyBulkRequest(BaseModel):
    cast: Optional[str] = None
    suggestions: Dict[str, Dict]


class VoiceTargetScope(BaseModel):
    version_id: Optional[str] = Field(default=None, min_length=1, max_length=80)
    book_token: Optional[str] = Field(default=None, pattern=BOOK_TOKEN_PATTERN)


class GeneratePersonasRequest(StateRequestScope):
    advanced: bool = False
    batch_size: int = 40
    # Sample spoken lines per character in the persona prompt (#522 12.1).
    context_lines: int = Field(default=8, ge=1, le=200)
    speaker: Optional[str] = Field(default=None, max_length=200)
    age_group: Optional[str] = Field(default=None, max_length=40)
    # Only characters with no entry in voice_config.json (the ones /api/voices
    # reports as persona_pending); the rest keep their persona and preview.
    # Issue #602: a script regenerated with more chapters should not re-roll
    # every voice.
    new_only: bool = False


class PersonaRecoveryRequest(StateRequestScope):
    speaker: str = Field(min_length=1, max_length=200)
    persona_json: str = Field(min_length=2, max_length=10000)
    resume: bool = False


def _validate_persona_recovery(value: str) -> tuple[str, str]:
    try:
        parsed = extract_json_object(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise HTTPException(status_code=422, detail=f"Persona JSON is invalid: {exc}") from exc
    if not isinstance(parsed, dict):
        raise HTTPException(status_code=422, detail="Persona output must be a JSON object")
    try:
        normalized = validate_persona_payload(parsed)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return normalized["description"], normalized["ref_text"]

class VoiceVersionRequest(BaseModel):
    book_token: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    version_id: str = Field(min_length=1, max_length=80)
    age_group: str = Field(default="adult", max_length=40)
    config: Dict = Field(default_factory=dict)


class VoiceCandidateRequest(VoiceTargetScope):
    candidate_id: str = Field(min_length=1, max_length=80)
    config: Dict = Field(default_factory=dict)


class VoiceCandidateFavoriteRequest(VoiceTargetScope):
    favorite: bool


class NarratorStrategyRequest(BaseModel):
    strategy: NarratorStrategy
    book_token: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")


class NarratorPreviewRequest(BaseModel):
    strategy: NarratorStrategy
    focus_speaker: Optional[str] = Field(default=None, max_length=200)
    narrator_version: Optional[str] = Field(default=None, max_length=80)


class VoiceApprovalRequest(VoiceTargetScope):
    persona_status: Optional[str] = Field(default=None, pattern="^(unreviewed|generated|reviewed|approved|rejected)$")
    voice_status: Optional[str] = Field(default=None, pattern="^(unreviewed|generated|reviewed|approved|rejected)$")


class PersonaVoiceAuditRequest(VoiceTargetScope):
    persona_ref: Optional[str] = Field(default=None, max_length=200)
    persona_description: Optional[str] = Field(default=None, max_length=1000)
    voice_adapter_id: Optional[str] = Field(default=None, max_length=200)
    suggestion_reason: Optional[str] = Field(default=None, max_length=500)


def _mutate_voice_entry(speaker, mutator):
    with file_lock(VOICE_CONFIG_PATH):
        config = safe_load_json(VOICE_CONFIG_PATH, default={})
        entry = config.setdefault(speaker, {})
        mutator(entry)
        atomic_json_write(config, VOICE_CONFIG_PATH)
        return entry


def _mutate_book_voice_entry(speaker, mutator, book_token=None):
    with ensure_book_state(os.path.dirname(VOICE_CONFIG_PATH)):
        if book_token is not None:
            snapshot = get_book_snapshot(os.path.dirname(VOICE_CONFIG_PATH))
            if get_book_snapshot_token(snapshot) != book_token:
                raise HTTPException(status_code=409, detail="Active book changed; reload voices before saving")
        if speaker == "NARRATOR":
            speaker = get_script_narrator_speaker()
        _require_script_speaker(speaker)
        entry = _mutate_voice_entry(speaker, mutator)
        with file_lock(VOICE_CONFIG_PATH):
            config = safe_load_json(VOICE_CONFIG_PATH, default={})
            return entry, get_voice_config_revision(config)


def get_current_persona_state(speaker, version_id, script=None):
    """-> the current target, or 409. Pass `script` when it is already parsed (C32)."""
    if speaker == "NARRATOR":
        speaker = get_script_narrator_speaker()
    if script is None:
        script = safe_load_json(SCRIPT_PATH, default=[])
    target = find_state_target(get_persona_state_targets(script), speaker, version_id)
    if target is None:
        raise HTTPException(status_code=409, detail="Character state changed; reload voices")
    return target


STATE_VERSION_ID = re.compile(r"state_[0-9a-f]{24}")


def require_current_state_target(speaker, version_id, book_token, *, script=None, snapshot_token=None):
    """The one guard for every state request (generate, recover, suggest):
    the client is showing the active book, and the state still exists.
    -> the current target (#1040 review C28). Blocking: call from a thread."""
    if not book_token:
        raise HTTPException(status_code=409, detail="State actions require the current book token")
    if snapshot_token is None:
        data_dir = os.path.dirname(SCRIPT_PATH)
        with ensure_book_state(data_dir):
            snapshot_token = get_book_snapshot_token(
                get_book_snapshot(data_dir, allow_missing_script=True, include_voices=False))
    if snapshot_token != book_token:
        raise HTTPException(status_code=409, detail="Active book changed; reload voices")
    return get_current_persona_state(speaker, version_id, script=script)


def get_validated_voice_changes(speaker, before, after, *, check_timeline=False, allow_state_removal=False,
                                trusted_states=()):
    """Validate state invariants for every public voice-config write path.

    `persona_state` is server-owned: only generation and recovery set it
    (they pass `trusted_states`). A client may keep the stored value but never
    introduce or change one, and `state_<24 hex>` ids are reserved for state
    versions (#1040 review C6)."""
    old_versions, versions = before.get("versions") or {}, after.get("versions") or {}
    for version_id, version in versions.items():
        if not isinstance(version, dict) or version_id in trusted_states:
            continue
        old_state = (old_versions.get(version_id) or {}).get("persona_state")
        if version.get("persona_state") != old_state and "persona_state" in version:
            raise HTTPException(status_code=409, detail="Character-state identity is set by persona generation; reload voices")
        if STATE_VERSION_ID.fullmatch(version_id) and "persona_state" not in version and version_id not in old_versions:
            raise HTTPException(status_code=422, detail="Version ids of the form state_<id> are reserved for character states")
    # Only seeds this save writes are checked: a legacy invalid seed already
    # stored (e.g. by a voice-library apply) must not refuse every later save.
    seed_configs = [(after, before)] + [(version, old_versions.get(version_id) or {})
                                         for version_id, version in versions.items()]
    for config, previous in seed_configs:
        if isinstance(config, dict) and "seed" in config and (
                "seed" not in (previous or {}) or config["seed"] != previous["seed"]):
            value = config["seed"]
            if isinstance(value, bool) or not re.fullmatch(r"-?\d+", str(value)):
                raise HTTPException(status_code=422, detail="Voice seed must be an integer or -1")
    points = after.get("version_timeline") or []
    removed = set(old_versions) - set(versions)
    applied = {point.get("version_id") for point in points} | {after.get("active_version")}
    if removed & applied:
        raise HTTPException(status_code=409, detail="Voice version is applied; select another base version or clear its timeline points first")
    changed_states = [(version_id, version) for version_id, version in versions.items()
                      if isinstance(version, dict) and version != old_versions.get(version_id)
                      and ("persona_state" in version or "persona_state" in (old_versions.get(version_id) or {}))]
    changed_states.extend((version_id, old_versions[version_id]) for version_id in removed
                          if isinstance(old_versions[version_id], dict) and "persona_state" in old_versions[version_id])
    timeline_changed = check_timeline or points != (before.get("version_timeline") or [])
    if timeline_changed:
        missing = [point["version_id"] for point in points
                   if point.get("version_id") is not None and point["version_id"] not in versions]
        if missing:
            raise HTTPException(status_code=400, detail=f"Unknown voice version: {', '.join(missing)}")
    state_points = [point for point in points if point.get("version_id")
                    and "persona_state" in (versions.get(point["version_id"]) or {})] if timeline_changed else []
    if not changed_states and not state_points:
        return after
    script = safe_load_json(SCRIPT_PATH, default=[])
    targets = get_persona_state_targets(script)
    if speaker == "NARRATOR":
        speaker = get_script_narrator_speaker()
    by_id = {target["version_id"]: target for target in targets.get(speaker, [])}
    for version_id, version in changed_states:
        if allow_state_removal and version_id in removed:
            continue  # An unapplied state (current, stale or orphaned) can always be removed with the current book token.
        if version_id not in by_id or not is_state_version_current(version, by_id[version_id]):
            raise HTTPException(status_code=409, detail="State source changed; reload and regenerate that persona")
    if state_points:
        chunks = safe_load_json(CHUNKS_PATH, default=[])
        mapping = get_persona_state_chunk_indices(script, chunks, speaker, state_targets=targets)
        for point in state_points:
            target = by_id.get(point["version_id"])
            version = versions[point["version_id"]]
            if (target is None or not is_state_version_current(version, target)
                    or mapping.get(target["from_entry"]) != point["from_index"]):
                raise HTTPException(status_code=409, detail="State boundary is stale or lies inside an edited/merged chunk; rebuild chunks and review the timeline")
    return after


def get_validated_voice_target(entry, speaker, version_id, book_token):
    if book_token is None:
        raise HTTPException(status_code=409, detail="State actions require the current book token")
    version = (entry.get("versions") or {}).get(version_id)
    if not isinstance(version, dict):
        raise HTTPException(status_code=404, detail="Generate this state persona first")
    if "persona_state" in version:
        target = get_current_persona_state(speaker, version_id)
        if not is_state_version_current(version, target):
            raise HTTPException(status_code=409, detail="State persona belongs to an earlier script; regenerate it")
    return version


def _mutate_voice_target(speaker, mutator, version_id=None, book_token=None):
    """Apply a card action to exactly one book-bound voice version."""
    def apply(entry):
        target = get_validated_voice_target(entry, speaker, version_id, book_token) if version_id else entry
        mutator(target)
    entry, _revision = _mutate_book_voice_entry(speaker, apply, book_token)
    return entry["versions"][version_id] if version_id else entry


def get_script_speaker(entry):
    """Read the active script's modern or legacy speaker field."""
    return get_entry_speaker(entry)


def get_script_narrator_speaker():
    """Resolve only the supported modern and legacy narrator spellings."""
    _require_script_speaker(None)
    script = safe_load_json(SCRIPT_PATH, default=[])
    speakers = {get_script_speaker(entry) for entry in script}
    for name in ("NARRATOR", "Narrator"):
        if name in speakers:
            return name
    raise HTTPException(status_code=404, detail="Narrator is not present in the active script")


def _require_script_speaker(speaker):
    if not os.path.isfile(SCRIPT_PATH):
        raise HTTPException(status_code=422, detail="Generate or open an active script first")
    if speaker is None:
        return
    script = safe_load_json(SCRIPT_PATH, default=[])
    speakers = {get_script_speaker(entry) for entry in script}
    if speaker not in speakers:
        raise HTTPException(status_code=404, detail="Speaker is not present in the active script")


@router.get("/api/voices")
async def get_voices():
    return await asyncio.to_thread(_ensure_voice_listing)


def get_voice_rows(script_data, voice_config):
    """Build one backend eligibility/roster view for legacy and guarded reads."""
    roster = sorted({name for entry in script_data if (name := get_script_speaker(entry))})
    lines = {name: [] for name in roster}
    for entry in script_data:
        name = get_script_speaker(entry)
        if name in lines and isinstance(entry, dict):
            lines[name].append(entry)
    state_targets = get_persona_state_targets(script_data)
    rows = []
    for name in roster:
        row = {"name": name, "config": voice_config.get(name, {}),
               "persona_pending": not voice_is_set(voice_config.get(name))}
        # Per-line gender/age from pass 2, only when the run asked for them, so
        # a book generated without the switch gets exactly the old rows.
        if name in state_targets and not row["config"].get("alias_of"):
            saved = row["config"].get("versions", {})
            # Each state says whether its saved version is current, by the
            # server's own rule, so the cards never re-derive it (C37).
            row["persona_states"] = [{**target, "current": is_state_version_current(saved.get(target["version_id"]), target)}
                                     for target in state_targets[name]]
            row["persona_states_pending"] = any(
                not voice_is_set(saved.get(target["version_id"])) or not state["current"]
                for target, state in zip(state_targets[name], row["persona_states"]))
        traits = get_speaker_trait_summary(lines[name])
        if traits:
            row["traits"] = traits
        rows.append(row)
    return rows


def _ensure_voice_listing():
    with ensure_book_state(os.path.dirname(SCRIPT_PATH)):
        script_data = safe_load_json(SCRIPT_PATH, default=[]) if os.path.exists(SCRIPT_PATH) else []
        voice_config = safe_load_json(VOICE_CONFIG_PATH, default={}) if os.path.exists(VOICE_CONFIG_PATH) else {}
        return get_voice_rows(script_data, voice_config)


@router.post("/api/voices/{speaker}/versions")
async def save_voice_version(speaker: str, request: VoiceVersionRequest):
    def save_version(current):
        config = dict(request.config or {key: value for key, value in current.items()
                                         if key not in VERSION_OVERLAY_EXCLUDED})
        if config.get("type") not in {None, "custom", "clone", "design", "lora", "builtin_lora", "ensemble"}:
            raise HTTPException(status_code=422, detail="Unsupported voice version type")
        previous = (current.get("versions") or {}).get(request.version_id) or {}
        if "persona_state" in previous:
            # Saving over a state version keeps its server-owned state (C5).
            config["persona_state"] = previous["persona_state"]
        age = previous.get("age_group", request.age_group) if "persona_state" in previous and "age_group" not in request.model_fields_set else request.age_group
        updated = {**current, "versions": {**(current.get("versions") or {}),
                    request.version_id: {**config, "age_group": age}}}
        get_validated_voice_changes(speaker, current, updated)
        current["versions"] = updated["versions"]
    entry, _revision = await asyncio.to_thread(_mutate_book_voice_entry, speaker, save_version, request.book_token)
    return {"status": "saved", "speaker": speaker, "version_id": request.version_id,
            "versions": entry.get("versions", {})}


@router.delete("/api/voices/{speaker}/versions/{version_id}")
async def remove_voice_version(speaker: str, version_id: str, book_token: Optional[str] = None):
    def remove(entry):
        if book_token is None:
            raise HTTPException(status_code=409, detail="State actions require the current book token")
        if version_id not in (entry.get("versions") or {}):
            raise HTTPException(status_code=404, detail="Voice version not found")
        versions = {key: value for key, value in entry["versions"].items() if key != version_id}
        get_validated_voice_changes(speaker, entry, {**entry, "versions": versions}, allow_state_removal=True)
        entry["versions"] = versions
    entry, revision = await asyncio.to_thread(_mutate_book_voice_entry, speaker, remove, book_token)
    return {"status": "removed", "speaker": speaker, "version_id": version_id,
            "versions": entry.get("versions", {}), "revision": revision}


@router.post("/api/voices/{speaker}/versions/{version_id}/select")
async def select_voice_version(speaker: str, version_id: str):
    await asyncio.to_thread(_require_script_speaker, speaker)
    def select(entry):
        version = (entry.get("versions") or {}).get(version_id)
        if not isinstance(version, dict):
            raise HTTPException(status_code=404, detail="Voice version not found")
        if "persona_state" in version:
            # A state voice belongs to its part of the book: it is applied on
            # the voice timeline, never copied over the whole character (C3).
            raise HTTPException(status_code=409, detail="This is a character-state voice; apply it from Voice changes on its state card")
        after = {**entry, **get_version_fields(entry, version_id), "age_group": version.get("age_group"),
                 "active_version": version_id, "voice_status": "assigned"}
        get_validated_voice_changes(speaker, entry, after)
        entry.clear()
        entry.update(after)
    entry = await asyncio.to_thread(_mutate_voice_entry, speaker, select)
    return {"status": "selected", "speaker": speaker, "version_id": version_id,
            "config": entry}


@router.post("/api/voices/{speaker}/candidates")
async def add_voice_candidate(speaker: str, request: VoiceCandidateRequest):
    await asyncio.to_thread(_require_script_speaker, speaker)
    def add(entry):
        candidates = [c for c in entry.get("candidates", [])
                      if isinstance(c, dict) and c.get("candidate_id") != request.candidate_id]
        candidates.append({**request.config, "candidate_id": request.candidate_id})
        entry["candidates"] = candidates
    entry = await asyncio.to_thread(_mutate_voice_target, speaker, add, request.version_id, request.book_token)
    return {"status": "saved", "speaker": speaker, "candidates": entry.get("candidates", [])}


@router.post("/api/voices/{speaker}/candidates/{candidate_id}/select")
async def select_voice_candidate(speaker: str, candidate_id: str, request: VoiceTargetScope = VoiceTargetScope()):
    await asyncio.to_thread(_require_script_speaker, speaker)
    def select(entry):
        candidate = next((c for c in entry.get("candidates", [])
                          if isinstance(c, dict) and c.get("candidate_id") == candidate_id), None)
        if candidate is None:
            raise HTTPException(status_code=404, detail="Voice candidate not found")
        entry.update({k: v for k, v in candidate.items()
                      if k != "candidate_id" and (not request.version_id or k not in
                          {"persona_state", "description", "age_group", "gender", "versions", "version_timeline"})})
        entry["active_candidate"] = candidate_id
        entry["voice_status"] = "assigned"
    entry = await asyncio.to_thread(_mutate_voice_target, speaker, select, request.version_id, request.book_token)
    return {"status": "selected", "speaker": speaker, "candidate_id": candidate_id,
            "config": entry}


@router.delete("/api/voices/{speaker}/candidates/{candidate_id}")
async def delete_voice_candidate(speaker: str, candidate_id: str, version_id: Optional[str] = None, book_token: Optional[str] = None):
    await asyncio.to_thread(_require_script_speaker, speaker)
    def remove(entry):
        candidates = entry.get("candidates") or []
        if not any(isinstance(c, dict) and c.get("candidate_id") == candidate_id for c in candidates):
            raise HTTPException(status_code=404, detail="Voice candidate not found")
        entry["candidates"] = [c for c in candidates if c.get("candidate_id") != candidate_id]
        if entry.get("active_candidate") == candidate_id:
            entry.pop("active_candidate", None)
    await asyncio.to_thread(_mutate_voice_target, speaker, remove, version_id, book_token)
    return {"status": "deleted", "speaker": speaker, "candidate_id": candidate_id}


@router.post("/api/voices/{speaker}/candidates/{candidate_id}/favorite")
async def favorite_voice_candidate(speaker: str, candidate_id: str,
                                   request: VoiceCandidateFavoriteRequest):
    await asyncio.to_thread(_require_script_speaker, speaker)
    def update(entry):
        candidate = next((c for c in entry.get("candidates", [])
                          if isinstance(c, dict) and c.get("candidate_id") == candidate_id), None)
        if candidate is None:
            raise HTTPException(status_code=404, detail="Voice candidate not found")
        candidate["favorite"] = request.favorite
    entry = await asyncio.to_thread(_mutate_voice_target, speaker, update, request.version_id, request.book_token)
    return {"status": "saved", "speaker": speaker, "candidate_id": candidate_id,
            "favorite": next(c.get("favorite", False) for c in entry.get("candidates", [])
                              if c.get("candidate_id") == candidate_id)}


@router.post("/api/narrator/strategy")
async def save_narrator_strategy(request: NarratorStrategyRequest):
    entry, revision = await asyncio.to_thread(_mutate_book_voice_entry, "NARRATOR", lambda current: current.update({
        "narrator_strategy": request.strategy
    }), request.book_token)
    return {"status": "saved", "strategy": entry.get("narrator_strategy"), "revision": revision}


@router.post("/api/narrator/preview")
async def preview_narrator(request: NarratorPreviewRequest):
    speaker = await asyncio.to_thread(get_script_narrator_speaker)
    config = await asyncio.to_thread(safe_load_json, VOICE_CONFIG_PATH, default={})
    narrator = dict(config.get(speaker) or {})
    narrator["narrator_strategy"] = request.strategy
    config["NARRATOR"] = narrator
    resolved = resolve_narrator_voice_config("NARRATOR", config, {
        "focus_speaker": request.focus_speaker,
        "narrator_version": request.narrator_version,
    })
    selected = resolved.get("NARRATOR", {})
    return {"strategy": request.strategy, "focus_speaker": request.focus_speaker,
            "narrator_version": request.narrator_version,
            "selected": {"type": selected.get("type", "custom"),
                          "voice": selected.get("voice"),
                          "adapter_id": selected.get("adapter_id"),
                          "description": selected.get("description", "")}}


class StylePointRequest(VoiceStylePoint):
    from_index: int = Field(ge=0)
    character_style: str = Field(max_length=400)


@router.post("/api/voices/{speaker}/style_timeline")
async def add_style_point(speaker: str, request: StylePointRequest):
    """From this line on, the character sounds like `character_style` (an
    aged character, a time skip). Replaces a point at the same index."""
    await asyncio.to_thread(_require_script_speaker, speaker)
    def add(current):
        points = [p for p in (current.get("style_timeline") or [])
                  if isinstance(p, dict) and int(p.get("from_index", -1)) != request.from_index]
        if request.character_style.strip():
            points.append({"from_index": request.from_index,
                           "character_style": request.character_style.strip()})
        current["style_timeline"] = sorted(points, key=lambda p: int(p["from_index"]))
    entry = await asyncio.to_thread(_mutate_voice_entry, speaker, add)
    return {"status": "saved", "speaker": speaker, "style_timeline": entry.get("style_timeline", [])}


def get_library_voice_config(candidate):
    """The voice config a library LoRA voice is assigned with - the one shape
    _apply_voice_suggestions writes, reused for a state's version."""
    return {"type": candidate["type"], "adapter_id": candidate["adapter_id"],
            "adapter_path": (f"builtin_lora/{candidate['adapter_id']}"
                             if candidate["type"] == "builtin_lora"
                             else f"lora_models/{candidate['adapter_id']}")}


def get_adapter_users(voice_config):
    """-> {adapter_id: [speakers]} for every LoRA voice the book already uses,
    as a main voice or as a version."""
    users = {}
    for name, entry in (voice_config or {}).items():
        if not isinstance(entry, dict):
            continue
        configs = [entry, *[v for v in (entry.get("versions") or {}).values() if isinstance(v, dict)]]
        for config in configs:
            adapter_id = config.get("adapter_id")
            if adapter_id and config.get("type") in ("lora", "builtin_lora"):
                users.setdefault(adapter_id, [])
                if name not in users[adapter_id]:
                    users[adapter_id].append(name)
    return users


def get_chapter_label(script, index):
    """The chapter heading in force at a script line, from narration, or None."""
    for entry in reversed(script[:index + 1]):
        if isinstance(entry, dict) and get_script_speaker(entry) == "NARRATOR":
            found = re.findall(r"\b(?:Chapter \d+|Prologue|Epilogue)\b", entry.get("text") or "")
            if found:
                return found[-1]
    return None


def get_state_voice_sources(state, speaker, entry, candidates, users, limit=6):
    """Ranked voices for one settled state (owner order, 2026-10-03): the
    character's own matching versions, then UNUSED library voices, then ones
    other characters use; generating a version is offered only if none fit."""
    age = get_library_age_group(state["age_group"])
    gender = state["gender"]
    versions = []
    for version_id, version in sorted((entry.get("versions") or {}).items()):
        if not isinstance(version, dict):
            continue
        version_age = get_library_age_group(get_normalized_age_group(version.get("age_group")))
        version_gender = version.get("gender") or "unknown"
        if (get_age_distance(age, version_age) <= 1 and version_age != "unknown"
                and version_gender in ("unknown", gender)):
            versions.append({"version_id": version_id, "age_group": version.get("age_group")})
    ranked = _rank_heuristic_candidates("", candidates, gender if gender in ("male", "female") else None,
                                        age, filter_gender=True)
    by_id = {c["adapter_id"]: c for c in candidates}
    fitting = [by_id[i] for i in ranked
               if (by_id[i].get("gender") in ("unknown", gender) or gender not in ("male", "female"))
               and (age == "unknown" or (by_id[i].get("age_group", "unknown") != "unknown"
                                         and get_age_distance(age, by_id[i]["age_group"]) <= 1))]
    def describe(candidate):
        return {"adapter_id": candidate["adapter_id"], "name": candidate["name"],
                "gender": candidate.get("gender", "unknown"),
                "age_group": candidate.get("age_group", "unknown"),
                "used_by": [n for n in users.get(candidate["adapter_id"], []) if n != speaker],
                "config": get_library_voice_config(candidate)}
    fitting = [c for c in fitting if speaker not in users.get(c["adapter_id"], [])]
    unused = [describe(c) for c in fitting if not users.get(c["adapter_id"])][:limit]
    used = [describe(c) for c in fitting if users.get(c["adapter_id"])][:limit]
    return {"versions": versions, "library_unused": unused, "library_used": used,
            "offer_generate": not versions and not unused and not used}


@router.get("/api/voices/{speaker}/state_timeline")
async def get_voice_state_timeline(speaker: str):
    """The suggested voice timeline for a character whose settled gender/age
    changes (#653). Suggests only: nothing changes audio until it is applied."""
    def build():
        _require_script_speaker(speaker)
        script = safe_load_json(SCRIPT_PATH, default=[])
        chunks = safe_load_json(CHUNKS_PATH, default=[]) if os.path.isfile(CHUNKS_PATH) else []
        voice_config = safe_load_json(VOICE_CONFIG_PATH, default={})
        entry = voice_config.get(speaker) if isinstance(voice_config.get(speaker), dict) else {}
        # One pass, exact speaker identity: the same targets every state route
        # and the validator use (C14: "Lee" and "LEE" are different people).
        state_targets = get_persona_state_targets(script)
        targets = state_targets.get(speaker.strip(), [])
        states = [{key: target[key] for key in ("from_entry", "gender", "age_group")} for target in targets]
        if not states:
            return {"speaker": speaker, "states": [], "applied": entry.get("version_timeline", [])}
        strict_mapping = get_persona_state_chunk_indices(script, chunks, speaker, state_targets=state_targets)
        targets_by_entry = {target["from_entry"]: target for target in targets}
        candidates = _build_lora_candidates()
        users = get_adapter_users(voice_config)
        # Map the character's lines in order, so a line said twice ("Yes.")
        # maps to the chunk at ITS place in the book, not the first one.
        line_chunks, start = {}, 0
        for index, line in enumerate(script[:states[-1]["from_entry"] + 1]) if chunks else ():
            if get_script_speaker(line) == speaker.strip():
                chunk_index = get_chunk_index_for_entry(chunks, speaker, line.get("text"), start)
                if chunk_index is not None:
                    line_chunks[index], start = chunk_index, chunk_index
        out = []
        for state in states:
            line = script[state["from_entry"]]
            target = targets_by_entry.get(state["from_entry"])
            has_generated = target and target["version_id"] in (entry.get("versions") or {})
            chunk_index = strict_mapping.get(state["from_entry"]) if has_generated else line_chunks.get(state["from_entry"])
            sources = get_state_voice_sources(state, speaker, entry, candidates, users)
            sources["versions"] = [version for version in sources["versions"]
                if "persona_state" not in entry["versions"][version["version_id"]]
                or (target and version["version_id"] == target["version_id"]
                    and is_state_version_current(entry["versions"][version["version_id"]], target))]
            sources["offer_generate"] = not any(sources[key] for key in ("versions", "library_unused", "library_used"))
            if has_generated:
                sources["versions"].sort(key=lambda version: version["version_id"] != target["version_id"])
            out.append({**state, "from_index": chunk_index,
                        "chapter": get_chapter_label(script, state["from_entry"]),
                        "line": (line.get("text") or "")[:120],
                        "sources": sources, "state_version": target["version_id"] if target else None})
        return {"speaker": speaker, "states": out, "applied": entry.get("version_timeline", []),
                "chunks_built": bool(chunks)}
    return await asyncio.to_thread(build)


class VersionTimelineRequest(BaseModel):
    book_token: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    points: List[VoiceVersionPoint] = Field(max_length=50)


@router.post("/api/voices/{speaker}/version_timeline")
async def save_version_timeline(speaker: str, request: VersionTimelineRequest):
    """Apply a character's voice timeline. Every version must exist, or
    nothing is written."""
    def save(current):
        points = {p.from_index: {"from_index": p.from_index, "version_id": p.version_id}
                  for p in request.points}
        timeline = [points[i] for i in sorted(points)]
        get_validated_voice_changes(speaker, current, {**current, "version_timeline": timeline}, check_timeline=True)
        current["version_timeline"] = timeline
    entry, _revision = await asyncio.to_thread(_mutate_book_voice_entry, speaker, save, request.book_token)
    return {"status": "saved", "speaker": speaker, "version_timeline": entry.get("version_timeline", [])}


@router.delete("/api/voices/{speaker}/version_timeline")
async def clear_version_timeline(speaker: str, book_token: Optional[str] = None):
    def clear(current):
        current.pop("version_timeline", None)
    await asyncio.to_thread(_mutate_book_voice_entry, speaker, clear, book_token)
    return {"status": "cleared", "speaker": speaker}


@router.delete("/api/voices/{speaker}/style_timeline/{from_index}")
async def remove_style_point(speaker: str, from_index: int):
    await asyncio.to_thread(_require_script_speaker, speaker)
    def remove(current):
        current["style_timeline"] = [p for p in (current.get("style_timeline") or [])
                                     if isinstance(p, dict) and int(p.get("from_index", -1)) != from_index]
    entry = await asyncio.to_thread(_mutate_voice_entry, speaker, remove)
    return {"status": "saved", "speaker": speaker, "style_timeline": entry.get("style_timeline", [])}


@router.post("/api/voices/{speaker}/approval")
async def set_voice_approval(speaker: str, request: VoiceApprovalRequest):
    """Set persona and voice approval independently for a character."""
    await asyncio.to_thread(_require_script_speaker, speaker)
    if request.persona_status is None and request.voice_status is None:
        raise HTTPException(status_code=422, detail="At least one approval status is required")
    def update_status(current):
        if request.persona_status is not None:
            current["persona_status"] = request.persona_status
        if request.voice_status is not None:
            current["voice_status"] = request.voice_status
    entry = await asyncio.to_thread(_mutate_voice_target, speaker, update_status, request.version_id, request.book_token)
    return {"status": "saved", "speaker": speaker,
            "persona_status": entry.get("persona_status"),
            "voice_status": entry.get("voice_status")}


@router.post("/api/voices/{speaker}/persona-voice-audit")
async def update_persona_voice_audit(speaker: str, request: PersonaVoiceAuditRequest):
    """Allow a user to correct the provenance note for an assignment."""
    await asyncio.to_thread(_require_script_speaker, speaker)
    audit = {key: value.strip() for key, value in request.model_dump().items()
             if key not in VoiceTargetScope.model_fields and value is not None and value.strip()}
    if not audit:
        raise HTTPException(status_code=422, detail="At least one audit field is required")
    entry = await asyncio.to_thread(_mutate_voice_target, speaker, lambda current: current.update({
        "persona_voice_audit": {**(current.get("persona_voice_audit") or {}), **audit}
    }), request.version_id, request.book_token)
    return {"status": "saved", "speaker": speaker,
            "persona_voice_audit": entry.get("persona_voice_audit", {})}


@router.post("/api/generate_personas")
async def generate_personas(background_tasks: BackgroundTasks, request: GeneratePersonasRequest = GeneratePersonasRequest()):
    """Generate LLM-derived voice persona descriptions and VoiceDesign previews.

    This runs `app/generate_personas.py` which:
    - reads `annotated_script.json`,
    - asks the configured LLM to produce a short `description` and `ref_text` for each character,
    - uses the VoiceDesign model to synthesize a preview and saves it,
    - updates `voice_config.json` with a clone-style reference for each character.
    """
    await asyncio.to_thread(_require_script_speaker, request.speaker)
    if request.state_version:
        if not request.speaker or request.age_group or not request.book_token:
            raise HTTPException(status_code=422, detail="State regeneration requires a speaker and book token, without an age override")
        await asyncio.to_thread(require_current_state_target, request.speaker, request.state_version, request.book_token)
    check_global_gpu_lock("persona")

    process_state["persona"]["cancel"] = False

    # Unload TTS engine to free GPU for the subprocess
    if project_manager.engine is not None:
        logger.info("Unloading TTS engine for persona generation...")
        project_manager.engine = None
        gc.collect()

    command = [sys.executable, "-u", "generate_personas.py",
               "--context-lines", str(request.context_lines)]
    if request.speaker:
        command.extend(["--speaker", request.speaker])   # exact: names may contain commas
    if request.age_group:
        command.extend(["--age-group", request.age_group])
    if request.state_version:
        command.extend(["--state-version", request.state_version, "--book-token", request.book_token])
    if request.advanced or request.state_version:
        batch_size = max(1, min(int(request.batch_size or 40), 200))
        command.extend(["--advanced", "--batch-size", str(batch_size)])
    if request.new_only:
        command.append("--new-only")
    schedule_claimed_background_task(background_tasks, "persona", run_process, command, "persona")
    return {"status": "started", "advanced": bool(request.advanced or request.state_version)}


@router.post("/api/cancel_persona")
async def cancel_persona():
    if not process_state["persona"]["running"]:
        return {"status": "idle"}

    process_state["persona"]["cancel"] = True
    process_state["persona"]["logs"].append("[CANCEL] Cancellation requested")

    proc = process_state["persona"].get("process")
    if proc and proc.poll() is None:
        try:
            _send_signal_tree(proc, signal.SIGTERM)
        except (ProcessLookupError, OSError) as e:
            logger.warning(f"Failed to terminate persona process cleanly: {e}")

    return {"status": "cancelling"}


@router.post("/api/persona/recover")
async def recover_persona(background_tasks: BackgroundTasks, request: PersonaRecoveryRequest):
    """Validate and save one externally generated persona without rerunning the batch."""
    description, ref_text = _validate_persona_recovery(request.persona_json)

    if request.resume:
        check_global_gpu_lock("persona")

    if not os.path.exists(SCRIPT_PATH):
        raise HTTPException(status_code=422, detail="Generate or open an active script before recovery")
    data_dir = os.path.dirname(SCRIPT_PATH)

    def check_book():
        # Snapshot, parse and state lookup are blocking: off the event loop (C31).
        with ensure_book_state(data_dir):
            snapshot = get_book_snapshot(data_dir, allow_missing_script=True, include_voices=False)
            token = get_book_snapshot_token(snapshot)
            with open(SCRIPT_PATH, "r", encoding="utf-8") as f:
                script = json.load(f)
            if not isinstance(script, list):
                raise ValueError("active script must be a JSON array")
            speakers = {get_script_speaker(entry) for entry in script}
            if request.speaker not in speakers:
                raise HTTPException(status_code=422, detail="Speaker is not present in the active script")
            if request.state_version:
                require_current_state_target(request.speaker, request.state_version, request.book_token,
                                             script=script, snapshot_token=token)
            return token

    try:
        book_token = await asyncio.to_thread(check_book)
    except HTTPException:
        raise
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail=f"Active script is unavailable: {exc}") from exc

    def _save():
        with ensure_book_state(data_dir):
            current = get_book_snapshot(data_dir, allow_missing_script=True, include_voices=False)
            if get_book_snapshot_token(current) != book_token:
                raise HTTPException(status_code=409, detail="Active book changed; recover this persona again for the current book")
            with file_lock(VOICE_CONFIG_PATH):
                config = safe_load_json(VOICE_CONFIG_PATH, default={})
                base = config.get(request.speaker, {})
                before = copy.deepcopy(base)
                if request.state_version:
                    target = get_current_persona_state(request.speaker, request.state_version)
                    entry = copy.deepcopy(base.get("versions", {}).get(request.state_version) or {})
                    entry.update(get_state_version_identity(target))
                    entry["persona_status"] = "generated"
                else:
                    entry = base
                entry.update({"description": description, "character_style": description,
                              "ref_text": ref_text})
                if not entry.get("type"):
                    entry["type"] = "design"
                if request.state_version:
                    base.setdefault("versions", {})[request.state_version] = entry
                get_validated_voice_changes(request.speaker, before, base,
                                            trusted_states={request.state_version} if request.state_version else ())
                config[request.speaker] = base
                atomic_json_write(config, VOICE_CONFIG_PATH)

    claim_id = reserve_background_task("persona") if request.resume else None
    try:
        await asyncio.to_thread(_save)
        if request.resume:
            command = [sys.executable, "-u", "generate_personas.py", "--speaker", request.speaker,
                       "--recovered-speaker", request.speaker]
            if request.state_version:
                command.extend(["--advanced", "--state-version", request.state_version,
                                "--book-token", book_token])
            register_claimed_background_task(background_tasks, "persona", claim_id, run_process, command, "persona")
    except BaseException:
        if claim_id is not None:
            release_gpu_task_claim("persona", claim_id, pending_only=True)
        raise
    return {"status": "resuming" if request.resume else "saved", "speaker": request.speaker}

def _apply_voice_save(config_data, expected_revision=None, book_token=None):
    with ensure_book_state(os.path.dirname(VOICE_CONFIG_PATH)):
        def apply_updates(current_config):
            if book_token is not None:
                snapshot = get_book_snapshot(os.path.dirname(VOICE_CONFIG_PATH), allow_missing_script=True)
                if get_book_snapshot_token(snapshot) != book_token:
                    raise VoiceConfigConflict("Active book changed; reload voices before saving")
            updated = dict(current_config)
            for voice_name, config in config_data.items():
                existing = current_config.get(voice_name)
                metadata = dict(existing) if isinstance(existing, dict) else {}
                fields = config.model_dump(exclude_unset=isinstance(existing, dict))
                for key in ('style_timeline', 'version_timeline'):
                    if key in fields:
                        fields[key] = config.model_dump()[key]
                candidate = {**metadata, **fields}
                get_validated_voice_changes(voice_name, metadata, candidate, allow_state_removal=book_token is not None)
                updated[voice_name] = candidate
            return updated

        updated = apply_voice_config_update(VOICE_CONFIG_PATH, apply_updates,
                                            expected_revision=expected_revision)
        return {"status": "saved", "revision": get_voice_config_revision(updated),
                "book_token": book_token}


@router.post("/api/save_voice_config")
async def save_voice_config(config_data: Dict[str, VoiceConfigItem]):
    try:
        await asyncio.to_thread(_apply_voice_save, config_data)
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Voice config is busy (locked by another operation); please try again.")
    return {"status": "saved"}


class GuardedVoiceSaveRequest(BaseModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    book_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    voices: Dict[str, VoiceConfigItem]


def _ensure_voice_snapshot():
    with ensure_book_state(os.path.dirname(VOICE_CONFIG_PATH)), file_lock(VOICE_CONFIG_PATH):
        snapshot = get_book_snapshot(os.path.dirname(VOICE_CONFIG_PATH), allow_missing_script=True)
        script = json.loads(snapshot["script_bytes"]) if snapshot["script_bytes"] is not None else []
        if not isinstance(script, list):
            raise ValueError("Active script must contain an entry list")
        config = snapshot["voices"]
        return {"revision": get_voice_config_revision(config),
                "book_token": get_book_snapshot_token(snapshot), "book_id": snapshot["book_id"], "config": config,
                "voices": get_voice_rows(script, config),
                "seed_changes": get_unseeded_voice_seed_changes(config)}


@router.get("/api/voice_config/snapshot")
async def get_guarded_voice_snapshot():
    try:
        return await asyncio.to_thread(_ensure_voice_snapshot)
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Book or voices are busy; please try again.")


@router.post("/api/voice_config/save")
async def save_guarded_voice_config(request: GuardedVoiceSaveRequest):
    try:
        return await asyncio.to_thread(_apply_voice_save, request.voices,
                                       request.revision, request.book_token)
    except VoiceConfigConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Book or voices are busy; please try again.")


class VoiceSeedRepairRequest(BaseModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    book_token: str = Field(pattern=r"^[0-9a-f]{64}$")


def _apply_voice_seed_repair(request):
    with ensure_book_state(os.path.dirname(VOICE_CONFIG_PATH)):
        with file_lock(VOICE_CONFIG_PATH):
            snapshot = get_book_snapshot(os.path.dirname(VOICE_CONFIG_PATH), allow_missing_script=True)
            if get_book_snapshot_token(snapshot) != request.book_token:
                raise VoiceConfigConflict("Active book changed; reload voices before seeding")
            config = snapshot["voices"]
            if get_voice_config_revision(config) != request.revision:
                raise VoiceConfigConflict("Voice configuration changed; reload it before seeding")
            changes = get_unseeded_voice_seed_changes(config)
        backup = None
        updated = config
        if changes:
            backup = f"{VOICE_CONFIG_PATH}.bak-{time.time_ns()}"
            updated = apply_voice_config_update(VOICE_CONFIG_PATH, apply_unseeded_voice_seeds,
                expected=config, expected_revision=request.revision, backup_path=backup)
        return {"status": "saved" if changes else "unchanged", "changes": changes,
                "backup": os.path.basename(backup) if backup else None,
                "revision": get_voice_config_revision(updated), "book_token": request.book_token}


@router.post("/api/voice_config/seed_unseeded")
async def apply_voice_seed_repair(request: VoiceSeedRepairRequest):
    try:
        return await asyncio.to_thread(_apply_voice_seed_repair, request)
    except VoiceConfigConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Book or voices are busy; please try again.")


# --- Auto-suggest best LoRA voice per character -------------------------------

def _infer_lora_gender(model):
    """Best-effort declared/profile gender; pitch is not identity evidence."""
    g = (model.get("gender") or "").strip().lower()
    if g in ("male", "female"):
        return g
    name_id = f"{model.get('name', '')} {model.get('id', '')}".lower()
    if re.search(r"(?:^|[_\s-])f(?:$|[_\s-]|emale)", name_id):
        return "female"
    if re.search(r"(?:^|[_\s-])m(?:$|[_\s-]|ale)", name_id):
        return "male"
    desc = (model.get("description") or model.get("voice_profile") or "").lower()
    if any(w in desc for w in ("alto", "soprano", "mezzo", "feminine", "woman", "girl")):
        return "female"
    if any(w in desc for w in ("baritone", "tenor", "bass", "masculine", "man", "boy")):
        return "male"
    return "unknown"


def _infer_character_gender(text):
    """Rough gender guess for a character from persona/style/sample text via pronoun counts."""
    t = (text or "").lower()
    male = len(re.findall(r"\b(he|him|his|himself|man|men|boy|male|sir|mr|lord|king|father)\b", t))
    female = len(re.findall(r"\b(she|her|hers|herself|woman|women|girl|female|lady|mrs|ms|miss|queen|mother)\b", t))
    if male > female and male > 0:
        return "male"
    if female > male and female > 0:
        return "female"
    return "unknown"


AGE_GROUPS = ("child", "teen", "young_adult", "adult", "middle_aged", "elderly")


def _age_group_from_years(age):
    age = int(age)
    if age <= 12:
        return "child"
    if age <= 19:
        return "teen"
    if age <= 29:
        return "young_adult"
    if age <= 39:
        return "adult"
    if age <= 59:
        return "middle_aged"
    return "elderly"


def _infer_age_group(text):
    """Best-effort normalized apparent age from names, profiles, or dialogue."""
    value = (text or "").lower().replace("-", " ").replace("_", " ")
    numeric = re.search(r"\b(?:aged?\s+(\d{1,3})|(\d{1,3})\s+years?\s+old)\b", value)
    if not numeric and re.fullmatch(r"\s*\d{1,3}\s*", value):
        numeric = re.match(r"\s*(\d{1,3})", value)
    if numeric:
        years = next(group for group in numeric.groups() if group is not None)
        if 1 <= int(years) <= 120:
            return _age_group_from_years(years)
    decade = re.search(r"\b([1-9])0s\b", value)
    if decade:
        return _age_group_from_years(int(decade.group(1)) * 10 + 5)
    patterns = (
        ("child", r"\b(child|kid|little boy|little girl|preteen|under ?1[0-2])\b"),
        ("teen", r"\b(teen|teenage|adolescent)\b"),
        ("young_adult", r"\b(young adult|young man|young woman|twent(?:y|ies))\b"),
        ("middle_aged", r"\b(middle aged|middle age|forties|fifties)\b"),
        ("elderly", r"\b(elderly|old man|old woman|senior|sixties|seventies|eighties)\b"),
        ("adult", r"\b(adult|grown man|grown woman|thirties)\b"),
    )
    return next((group for group, pattern in patterns if re.search(pattern, value)), "unknown")


def _infer_lora_age(model):
    explicit = str(model.get("age_group") or model.get("age") or "").strip().lower().replace("-", "_").replace(" ", "_")
    if explicit in AGE_GROUPS:
        return explicit
    evidence = " ".join(str(model.get(k) or "") for k in ("age", "name", "id", "description", "voice_profile"))
    return _infer_age_group(evidence)


def _infer_character_traits(name, profile, lines):
    """Infer traits with evidence priority: label, then persona.

    DIALOGUE IS NOT EVIDENCE ABOUT ITS SPEAKER. `_infer_character_gender`
    counts gendered words, and the words a character SPEAKS describe whoever
    they are talking about. Measured on the live book: Subaru's own 412 lines
    contain 46 feminine tokens against 27 masculine - "she" x20, "her" x11,
    "girl" x10 - because he spends the book talking to and about Emilia, Felt
    and Satella. He was classified female. So were ROM and Reinhard; Emilia
    came out male. The signal is inverted for exactly the characters who speak
    most, which is the ones that matter.

    It was rated "low" confidence, which correctly kept it out of the HARD
    gender filter - but `get_voice_allocation` still applies a soft penalty of
    3 against candidates whose gender differs from the inferred one, so a wrong
    guess actively pushed correct-gender voices down the ranking.

    Dropping the source leaves gender "unknown" when only dialogue is
    available, and "unknown" is handled correctly everywhere: no hard filter,
    no soft penalty. Determining gender properly needs coreference - which
    pronoun refers to whom - and that is BookNLP's or the LLM's job, not a
    regex over the wrong text.
    """
    sources = (("character label", name, "high"),
               ("existing persona/style", profile, "medium"))
    result = {"gender": "unknown", "gender_confidence": "unknown",
              "age_group": "unknown", "age_confidence": "unknown", "trait_evidence": ""}
    evidence = []
    for source, text, confidence in sources:
        if result["gender"] == "unknown":
            gender = _infer_character_gender(text)
            if gender != "unknown":
                result.update(gender=gender, gender_confidence=confidence)
                evidence.append(f"{source}: {gender}")
        if result["age_group"] == "unknown":
            age = _infer_age_group(text)
            if age != "unknown":
                result.update(age_group=age, age_confidence=confidence)
                evidence.append(f"{source}: {age.replace('_', ' ')}")
    result["trait_evidence"] = "; ".join(evidence) or "No explicit gender or age evidence"
    result["local_trait_evidence"] = result["trait_evidence"]
    result["llm_trait_evidence"] = ""
    return result


def _age_distance(character_age, voice_age):
    if character_age == "unknown" or voice_age == "unknown":
        return 2
    return abs(AGE_GROUPS.index(character_age) - AGE_GROUPS.index(voice_age))


def _is_authoritative_confidence(confidence):
    return confidence in ("high", "medium")


def _is_stronger_authoritative_confidence(current, proposed):
    confidence_rank = {"unknown": 0, "low": 1, "medium": 2, "high": 3}
    return (_is_authoritative_confidence(proposed)
            and confidence_rank.get(proposed, 0) > confidence_rank.get(current, 0))


def _build_lora_candidates():
    """Downloaded built-in + user-trained adapters with normalized fields for matching."""
    candidates = []
    for m in _load_builtin_lora_manifest():
        if not m.get("downloaded", False):
            continue
        candidates.append({
            "adapter_id": m["id"],
            "name": m.get("name") or m["id"],
            "type": "builtin_lora",
            "gender": _infer_lora_gender(m),
            "age_group": _infer_lora_age(m),
            "description": m.get("description") or m.get("voice_profile") or "",
        })
    for m in get_adapter_manifest_rows(LORA_MODELS_DIR, LORA_MODELS_MANIFEST, _load_manifest):
        candidates.append({
            "adapter_id": m["id"],
            "name": m.get("name") or m["id"],
            "type": "lora",
            "previous_ids": m.get("previous_ids", []),
            "gender": _infer_lora_gender(m),
            "age_group": _infer_lora_age(m),
            "description": m.get("description") or m.get("voice_profile") or "",
        })
    return candidates


def get_lora_candidate_id_map(candidates):
    """Read all current/historical identities from one admitted catalog snapshot."""
    return get_adapter_id_alias_map([
        {"id": c["adapter_id"], "previous_ids": c.get("previous_ids", [])}
        for c in candidates])


def _select_representative_lines(lines: List[str], limit: int) -> List[str]:
    """Sample dialogue across the whole book rather than only its beginning."""
    if len(lines) <= limit:
        return lines
    if limit <= 1:
        return [lines[0]]
    indices = [round(i * (len(lines) - 1) / (limit - 1)) for i in range(limit)]
    return [lines[i] for i in dict.fromkeys(indices)]


def _rank_heuristic_candidates(profile: str, candidates: List[dict], preferred_gender=None,
                               preferred_age="unknown", filter_gender=True) -> List[str]:
    gender = preferred_gender if preferred_gender in ("male", "female") else _infer_character_gender(profile)
    pool = candidates
    if filter_gender and gender != "unknown":
        pool = [c for c in candidates if c.get("gender") == gender] or candidates
    words = set(re.findall(r"[a-z]{4,}", profile.lower()))
    ranked = sorted(pool, key=lambda c: (
        _age_distance(preferred_age, c.get("age_group", "unknown")),
        0 if c.get("favorite") else 1,
        -len(words & set(re.findall(r"[a-z]{4,}", c.get("description", "").lower()))),
        c["adapter_id"]))
    return [c["adapter_id"] for c in ranked]


def get_voice_allocation(profile, candidates, initial_ranked, traits,
                         existing_adapter, usage, priority):
    """Pure compatibility/reuse decision; the caller owns usage mutation."""
    cand_by_id = {c["adapter_id"]: c for c in candidates}
    hard_gender = (traits["gender"] != "unknown"
                   and _is_authoritative_confidence(traits["gender_confidence"]))
    hard_age = _is_authoritative_confidence(traits["age_confidence"])
    gender_matches = [c for c in candidates if c.get("gender") == traits["gender"]]
    gender_fallback = hard_gender and not gender_matches
    ranked = [adapter_id for adapter_id in initial_ranked if adapter_id in cand_by_id]
    for adapter_id in _rank_heuristic_candidates(
            profile, candidates, traits["gender"],
            traits["age_group"] if hard_age else "unknown",
            filter_gender=False):
        if adapter_id not in ranked:
            ranked.append(adapter_id)
    rank_order = {adapter_id: index for index, adapter_id in enumerate(ranked)}

    def allocation_score(adapter_id):
        candidate = cand_by_id[adapter_id]
        candidate_gender = candidate.get("gender", "unknown")
        hard_gender_tier = 0
        soft_gender_penalty = 0
        if hard_gender:
            if candidate_gender != traits["gender"]:
                hard_gender_tier = 1 if candidate_gender == "unknown" else 2
        elif traits["gender"] != "unknown" and candidate_gender != traits["gender"]:
            soft_gender_penalty = 1 if candidate_gender == "unknown" else 3
        distance = _age_distance(traits["age_group"], candidate.get("age_group", "unknown"))
        age_penalty = distance * (100 if hard_age else 1)
        reuse_penalty = 100 if priority == "major" else 2
        # A starred voice beats a non-favorite ranked above it (one rank step
        # is 10) but never a hard age or gender mismatch (100 / the tier).
        favorite_bonus = -25 if candidate.get("favorite") else 0
        compatibility_and_reuse = (
            rank_order[adapter_id] * 10 + age_penalty + soft_gender_penalty
            + favorite_bonus
            + usage.get(adapter_id, {}).get("character_count", 0) * reuse_penalty)
        return hard_gender_tier, compatibility_and_reuse, adapter_id

    ranked.sort(key=allocation_score)
    if existing_adapter in cand_by_id:
        chosen_id, is_new_identity = existing_adapter, False
    else:
        chosen_id = ranked[0]
        is_new_identity = True
    chosen = cand_by_id[chosen_id]
    existing_trait_mismatch = bool(existing_adapter and (
        (_is_authoritative_confidence(traits["gender_confidence"])
         and traits["gender"] != "unknown"
         and chosen.get("gender") not in (traits["gender"], "unknown"))
        or (_is_authoritative_confidence(traits["age_confidence"])
            and _age_distance(traits["age_group"], chosen.get("age_group", "unknown")) >= 3)))
    return chosen_id, ranked, is_new_identity, gender_fallback, existing_trait_mismatch


@router.post("/api/suggest_voices")
async def suggest_voices(request: SuggestVoicesRequest = SuggestVoicesRequest()):
    """Suggest the best-matching downloaded LoRA voice for each character based on
    the character's dialogue + persona, ranked by the configured LLM (heuristic fallback).

    Offloaded to threadpool via asyncio.to_thread to avoid blocking the event loop."""
    # Reserve the GPU slot for the duration of the (local-LLM) suggestion so it
    # can't run concurrently with TTS/review and trigger a VRAM OOM. The worker
    # releases ownership after completion even if the HTTP request is cancelled.
    return await run_claimed_task_worker("voices", _suggest_voices_impl, request)


def _suggest_voices_impl(request: SuggestVoicesRequest):
    # Sync implementation that makes a blocking LLM call and file I/O.
    # Called via asyncio.to_thread from the async endpoint above.
    data_dir = os.path.dirname(SCRIPT_PATH)
    with ensure_book_state(data_dir):
        if not os.path.exists(SCRIPT_PATH):
            raise HTTPException(status_code=400, detail="No script found. Generate a script first.")

        try:
            with open(SCRIPT_PATH, "r", encoding="utf-8") as f:
                script = json.load(f)
        except (json.JSONDecodeError, ValueError):
            raise HTTPException(status_code=400, detail="Script is not valid JSON.")

        # Existing config (for persona descriptions/styles + only_unset filtering)
        voice_config = {}
        if os.path.exists(VOICE_CONFIG_PATH):
            try:
                with open(VOICE_CONFIG_PATH, "r", encoding="utf-8") as f:
                    voice_config = json.load(f)
            except (json.JSONDecodeError, ValueError) as e:
                _warn_corrupted_json("voice config", VOICE_CONFIG_PATH, "treating as empty", e)
                voice_config = {}

        snapshot = get_book_snapshot(data_dir, allow_missing_script=True, include_voices=False)
        book_token = get_book_snapshot_token(snapshot)
        book_id = get_active_book_id()
        line_counts = _script_line_counts()
        if request.state_version:
            if len(request.characters or []) != 1:
                raise HTTPException(status_code=409, detail="State suggestions require the current book and one character")
            speaker = request.characters[0]
            target = require_current_state_target(speaker, request.state_version, request.book_token,
                                                  script=script, snapshot_token=book_token)
            version = voice_config.get(speaker, {}).get("versions", {}).get(request.state_version)
            if not is_state_version_current(version, target):
                raise HTTPException(status_code=409, detail="Generate the current state persona before suggesting voices")
            script = get_persona_state_entries(script, target)
            voice_config = {speaker: copy.deepcopy(version)}
            line_counts = get_script_line_counts(script)

    # Collect every per-character dialogue line so counts are accurate; sample
    # representative lines across the book only when building the prompt.
    samples = {}
    seen_lines = {}
    for entry in script:
        speaker = get_script_speaker(entry)
        if not speaker:
            continue
        raw_text = entry.get("text")
        text = raw_text.strip() if isinstance(raw_text, str) else ""
        if not speaker or not text:
            continue
        lines = samples.setdefault(speaker, [])
        seen = seen_lines.setdefault(speaker, set())
        if text not in seen:
            seen.add(text)
            lines.append(text)
    if not samples:
        return {"method": "none", "suggestions": {}, "message": "No characters found in script."}

    candidates = _build_lora_candidates()
    if not candidates:
        raise HTTPException(status_code=400, detail="No downloaded LoRA voices available. Download a built-in voice or train an adapter first.")

    line_limit = max(1, min(int(request.max_lines or 8), 30))
    lib = _load_voice_library()
    identities = get_lora_candidate_id_map(candidates)
    favorites = {identities.get(os.path.normcase(name), name)
                 for name in lib.get("favorites") or []}
    for c in candidates:
        c["favorite"] = c["adapter_id"] in favorites
    cast_name = (request.cast or "").strip() or None
    if cast_name and cast_name not in lib["casts"]:
        raise HTTPException(status_code=404, detail=f"Cast '{cast_name}' not found.")
    usage = get_cast_adapter_usage(lib, cast_name, identities)

    # Build profiles in importance order: narrator, then most dialogue lines.
    characters = {}
    requested = {name.strip() for name in (request.characters or []) if name and name.strip()}
    ordered_names = sorted(
        (name for name in samples if not requested or name in requested),
        key=lambda n: (0 if _norm_name(n) == "narrator" else 1, -len(samples[n]), _norm_name(n)))
    for speaker in ordered_names:
        lines = samples[speaker]
        if request.only_unset:
            existing = voice_config.get(speaker, {})
            if voice_category(existing) == "lora" and existing.get("adapter_id"):
                continue
        cfg = voice_config.get(speaker, {})
        persona_bits = [cfg.get("description") or "", cfg.get("character_style") or "", cfg.get("default_style") or ""]
        profile = " ".join(b for b in persona_bits if b)
        traits = _infer_character_traits(speaker, profile, lines)
        count = line_counts.get(speaker, len(lines))
        try:
            member_key = get_cast_member_key(speaker, book_id)
        except ValueError:
            member_key = None
        characters[speaker] = {
            "profile": profile,
            "lines": _select_representative_lines(lines, line_limit),
            "line_count": count,
            "priority": "major" if _norm_name(speaker) == "narrator" or count >= CAST_MAJOR_LINE_THRESHOLD else "minor",
            "member_key": member_key,
            **traits,
        }

    if not characters:
        return {"method": "none", "suggestions": {}, "message": "No characters to suggest (all already set)."}

    # When only filling unset roles, already-configured current-book roles are
    # fixed assignments and must contribute to reuse pressure unless the same
    # identity is already represented in the selected cast.
    if request.only_unset:
        cast_members = (lib.get("casts", {}).get(cast_name, {}).get("members", {})
                        if cast_name else {})
        for name, cfg in voice_config.items():
            adapter_id = (cfg or {}).get("adapter_id")
            if not adapter_id or voice_category(cfg) != "lora":
                continue
            adapter_id = identities.get(os.path.normcase(adapter_id), adapter_id)
            try:
                key = get_cast_member_key(name, book_id)
            except ValueError:
                continue
            if key in cast_members:
                continue
            item = usage.setdefault(adapter_id, {"character_count": 0, "total_lines": 0, "characters": []})
            item["character_count"] += 1
            item["total_lines"] += line_counts.get(name, 0)
            item["characters"].append(name)

    cand_by_id = {c["adapter_id"]: c for c in candidates}
    suggestions = {}
    rankings = {}
    style_by_name = {}
    reason_by_name = {}
    method = "heuristic"
    llm_warning = None

    # --- Try LLM ranking first ---
    try:
        # don't let a stuck model hang the worker thread forever
        client, model_name = _make_llm_client(timeout=120)

        voice_catalog = "\n".join(
            f'- id="{c["adapter_id"]}" | name="{c["name"][:50]}" | gender={c.get("gender", "unknown")} | age={c.get("age_group", "unknown")} | favorite={"yes" if c.get("favorite") else "no"} | series_use={usage.get(c["adapter_id"], {}).get("character_count", 0)} | description: {(c["description"] or "(none)")[:80]}'
            for c in candidates
        )
        system_prompt = (
            "You are a casting director matching narrated audiobook characters to available LoRA TTS voices. "
            "For each character, rank up to three fitting voice ids and write concise TTS delivery guidance based only on the book text. "
            "The style should describe cadence, energy, formality, confidence, and supported emotion; do not invent biography or accent. "
            "Infer gender and broad apparent age only when supported by the supplied book evidence. "
            "Known character gender must match voice gender; prefer the closest available age group. "
            "Among compatible voices, prefer one marked favorite=yes. "
            "Only use provided voice ids. Return every requested character in the structured response."
        )
        casting_schema = {
            "name": "audiobook_casting",
            "strict": True,
            "schema": {
                "type": "object",
                "properties": {
                    "characters": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "ranked_adapter_ids": {
                                    "type": "array", "items": {"type": "string"},
                                    "minItems": 1, "maxItems": 3,
                                },
                                "character_style": {"type": "string"},
                                "reason": {"type": "string"},
                                "character_gender": {"type": "string", "enum": ["male", "female", "unknown"]},
                                "age_group": {"type": "string", "enum": ["child", "teen", "young_adult", "adult", "middle_aged", "elderly", "unknown"]},
                                "trait_evidence": {"type": "string"},
                                "trait_confidence": {"type": "string", "enum": ["high", "medium", "low", "unknown"]},
                            },
                            "required": ["name", "ranked_adapter_ids", "character_style", "reason", "character_gender", "age_group", "trait_evidence", "trait_confidence"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["characters"],
                "additionalProperties": False,
            },
        }
        character_items = list(characters.items())
        full_cfg = load_app_config(CONFIG_PATH)
        llm_cfg = get_active_llm_config(full_cfg)
        status = get_current_status(
            full_cfg.get("llm_mode", "local"), llm_cfg.get("base_url", ""),
            model_name, (full_cfg.get("llm_remote_ssh") or "").strip(),
            use_cache=True, api_key=llm_cfg.get("api_key"))
        for start in range(0, len(character_items), 2):
            batch = character_items[start:start + 2]
            char_block = "\n\n".join(
                f'CHARACTER: {name}\nLines: {info["line_count"]} ({info["priority"]})\nCurrent trait estimate: gender={info["gender"]}, age={info["age_group"]}\nPersona/style: {(info["profile"] or "(none)")[:200]}\nSample lines:\n'
                + "\n".join(f'  - "{ln[:140]}"' for ln in info["lines"])
                for name, info in batch
            )
            user_prompt = f"AVAILABLE VOICES:\n{voice_catalog}\n\nCHARACTERS:\n{char_block}"
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            effective_max = get_effective_max_tokens(
                2600, status.get("context_length"), messages, hard_max=12000)
            response = client.chat.completions.create(
                model=model_name,
                messages=messages,
                response_format={"type": "json_schema", "json_schema": casting_schema},
                temperature=0.3,
                max_tokens=effective_max,
                timeout=120,
            )
            raw = response.choices[0].message.content or ""
            parsed = extract_json_object(raw)
            if parsed is None:
                finish_reason = response.choices[0].finish_reason
                logger.warning("Unparseable casting response (%s) preview: %s", finish_reason, raw[:500])
                raise ValueError(f"Could not parse a JSON object from casting batch ({len(raw)} chars)")
            parsed_items = parsed.get("characters", []) if isinstance(parsed, dict) else []
            if not isinstance(parsed_items, list):
                raise ValueError("Casting characters must be a JSON array")
            parsed_by_name = {
                item.get("name"): item for item in parsed_items
                if isinstance(item, dict) and isinstance(item.get("name"), str)
            }
            for name, _info in batch:
                pick = parsed_by_name.get(name)
                if isinstance(pick, dict):
                    ranked = pick.get("ranked_adapter_ids") or ([pick.get("adapter_id")] if pick.get("adapter_id") else [])
                    if not isinstance(ranked, list):
                        continue
                    valid_ranked = list(dict.fromkeys(
                        i for i in ranked if isinstance(i, str) and i in cand_by_id))
                    if not valid_ranked or any(
                            pick.get(field) is not None and not isinstance(pick[field], str)
                            for field in ("character_style", "reason", "trait_evidence",
                                          "trait_confidence", "character_gender", "age_group")):
                        continue
                    rankings[name] = valid_ranked
                    style_by_name[name] = (pick.get("character_style") or "").strip()[:500]
                    reason_by_name[name] = (pick.get("reason") or "").strip()[:240]
                    info = characters[name]
                    llm_confidence = pick.get("trait_confidence", "unknown")
                    llm_gender = pick.get("character_gender")
                    llm_age = pick.get("age_group")
                    accepted_traits = []
                    rejected_conflict = False
                    if (llm_gender in ("male", "female")
                            and _is_stronger_authoritative_confidence(
                                info["gender_confidence"], llm_confidence)):
                        info["gender"] = llm_gender
                        info["gender_confidence"] = llm_confidence
                        accepted_traits.append(f"gender={llm_gender}")
                    elif llm_gender in ("male", "female") and llm_gender != info["gender"]:
                        rejected_conflict = True
                    if (llm_age in AGE_GROUPS
                            and _is_stronger_authoritative_confidence(
                                info["age_confidence"], llm_confidence)):
                        info["age_group"] = llm_age
                        info["age_confidence"] = llm_confidence
                        accepted_traits.append(f"age={llm_age.replace('_', ' ')}")
                    elif llm_age in AGE_GROUPS and llm_age != info["age_group"]:
                        rejected_conflict = True
                    info["llm_trait_evidence"] = (pick.get("trait_evidence") or "")[:300]
                    if accepted_traits and info["llm_trait_evidence"]:
                        llm_evidence = ("LM accepted " + ", ".join(accepted_traits)
                                        if rejected_conflict else f"LM: {info['llm_trait_evidence']}")
                        info["trait_evidence"] = (
                            f"Local: {info['local_trait_evidence']}; {llm_evidence}")[:300]
    except LLMConfigError as e:
        # Config issue (e.g. base_url rejected by _validate_local_llm_base_url) -
        # surface to the UI instead of silently falling back to heuristic.
        llm_warning = str(e)
    except Exception as e:
        logger.warning(f"LLM voice suggestion failed, falling back to heuristic: {e}")
        llm_warning = str(e)

    llm_ranked_names = {name for name in characters if rankings.get(name)}
    heuristic_characters = [name for name in characters if name not in llm_ranked_names]
    if heuristic_characters:
        fallback_warning = ("LLM omitted or returned unusable rankings; deterministic ranking used for: "
                            + ", ".join(heuristic_characters))
        llm_warning = (llm_warning + ". " + fallback_warning) if llm_warning else fallback_warning
    method = ("llm" if not heuristic_characters else
              "mixed" if llm_ranked_names else "heuristic")

    # Fill missing rankings/styles deterministically, then allocate in priority
    # order while updating reuse counts after every new distinct character.
    for name, info in characters.items():
        profile_text = " ".join([name, info["profile"]] + info["lines"])
        if not rankings.get(name):
            rankings[name] = _rank_heuristic_candidates(
                profile_text, candidates, info["gender"],
                info["age_group"] if _is_authoritative_confidence(info["age_confidence"]) else "unknown",
                filter_gender=_is_authoritative_confidence(info["gender_confidence"]))
        if not style_by_name.get(name):
            style_by_name[name] = info["profile"] or "Natural delivery matching the character's dialogue and role in this book."
        if not reason_by_name.get(name):
            reason_by_name[name] = "Deterministic compatibility and series-diversity ranking"

        existing_member = None
        if cast_name and info["member_key"]:
            existing_member = (lib["casts"][cast_name].get("members", {}).get(info["member_key"])
                               or lib.get("shared", {}).get(info["member_key"]))
        existing_adapter = ((existing_member or {}).get("config") or {}).get("adapter_id")
        if existing_adapter:
            existing_adapter = identities.get(os.path.normcase(existing_adapter), existing_adapter)
        (chosen_id, ranked, is_new_identity, gender_fallback,
         existing_trait_mismatch) = get_voice_allocation(
            profile_text, candidates, rankings[name], info, existing_adapter,
            usage, info["priority"])
        before = usage.get(chosen_id, {}).get("character_count", 0)
        if is_new_identity:
            usage.setdefault(chosen_id, {"character_count": 0, "total_lines": 0, "characters": []})
            usage[chosen_id]["character_count"] += 1
            usage[chosen_id]["total_lines"] += info["line_count"]
            usage[chosen_id]["characters"].append(name)
        chosen = cand_by_id[chosen_id]
        suggestions[name] = {
            "method": "llm" if name in llm_ranked_names else "heuristic",
            "adapter_id": chosen_id, "adapter_name": chosen["name"], "type": chosen["type"],
            "ranked_adapter_ids": ranked,
            "character_style": style_by_name[name], "reason": reason_by_name[name],
            "line_count": info["line_count"], "priority": info["priority"], "book_id": book_id,
            "book_token": book_token, "state_version": request.state_version,
            "cast_member_key": info["member_key"], "reuse_count_before": before,
            "reuse_count_after": before + (1 if is_new_identity else 0),
            "reused": before > 0 and is_new_identity,
            "forced_reuse": info["priority"] == "major" and before > 0 and all(usage.get(i, {}).get("character_count", 0) > 0 for i in ranked),
            "character_gender": info["gender"], "character_age_group": info["age_group"],
            "voice_gender": chosen.get("gender", "unknown"),
            "voice_age_group": chosen.get("age_group", "unknown"),
            "trait_evidence": info["trait_evidence"],
            "local_trait_evidence": info["local_trait_evidence"],
            "llm_trait_evidence": info["llm_trait_evidence"],
            "gender_confidence": info["gender_confidence"],
            "age_confidence": info["age_confidence"],
            "gender_fallback": gender_fallback,
            "existing_trait_mismatch": existing_trait_mismatch,
        }

    return {"method": method, "suggestions": suggestions, "candidate_count": len(candidates),
            "favorites": sorted(favorites),
            "adapter_usage": usage, "book_id": book_id, "cast": cast_name,
            "major_line_threshold": CAST_MAJOR_LINE_THRESHOLD, "llm_warning": llm_warning,
            "heuristic_characters": heuristic_characters}




def _apply_voice_suggestions(suggestions: Dict[str, dict], cast_name: Optional[str]) -> dict:
    if any(value.get("state_version") for value in suggestions.values()):
        raise HTTPException(status_code=409, detail="State suggestions must be saved and selected on their state card")
    catalog = _build_lora_candidates()
    identities = get_lora_candidate_id_map(catalog)
    candidates = {c["adapter_id"]: c for c in catalog}
    with ensure_book_state(os.path.dirname(SCRIPT_PATH)):
        counts = _script_line_counts()
        book_id = get_active_book_id()
        if cast_name and not book_id:
            raise HTTPException(status_code=400, detail="Active book identity is required to save suggestions to a cast.")

        with file_lock(VOICE_LIBRARY_PATH), file_lock(VOICE_CONFIG_PATH):
            current_token = None
            if any(value.get("book_token") is not None for value in suggestions.values()):
                current_token = get_book_snapshot_token(get_book_snapshot(
                    os.path.dirname(SCRIPT_PATH), allow_missing_script=True, include_voices=False))
            voice_config = safe_load_json(VOICE_CONFIG_PATH, default={})
            lib = _load_voice_library()
            if cast_name and cast_name not in lib["casts"]:
                raise HTTPException(status_code=404, detail=f"Cast '{cast_name}' not found.")
            usage = get_cast_adapter_usage(lib, cast_name, identities)
            applied = []
            for character, suggestion in suggestions.items():
                if character not in counts:
                    continue
                if suggestion.get("book_token") is not None and suggestion["book_token"] != current_token:
                    raise HTTPException(status_code=409, detail="Active book changed; generate voice suggestions again")
                suggestion_book_id = secure_filename(suggestion.get("book_id") or "")
                if suggestion_book_id != secure_filename(book_id or ""):
                    raise HTTPException(status_code=409, detail=(
                        f"Suggestion for '{character}' belongs to a different book. Generate suggestions again."))
                adapter_id = suggestion.get("adapter_id")
                if adapter_id:
                    adapter_id = identities.get(os.path.normcase(adapter_id), adapter_id)
                candidate = candidates.get(adapter_id)
                if not candidate:
                    raise HTTPException(status_code=400, detail=f"Unknown or unavailable LoRA adapter: {adapter_id}")
                style = (suggestion.get("character_style") or "").strip()[:500]
                cfg = dict(voice_config.get(character) or {})
                cfg.pop("active_candidate", None)
                cfg.pop("active_version", None)
                cfg.update({
                    "type": candidate["type"], "adapter_id": adapter_id,
                    "adapter_path": (f"builtin_lora/{adapter_id}" if candidate["type"] == "builtin_lora"
                                     else f"lora_models/{adapter_id}"),
                    "character_style": style,
                    # Stable per character, so a voice is one draw for the
                    # whole book rather than a fresh draw per line.
                    "seed": str(character_voice_seed(character)),
                    **get_trait_assignment_metadata(suggestion),
                })
                cfg["persona_voice_audit"] = {
                    "persona_ref": cfg.get("persona_ref"),
                    "persona_description": (cfg.get("description") or "")[:1000],
                    "voice_adapter_id": adapter_id,
                    "suggestion_reason": (suggestion.get("reason") or "")[:240],
                }
                voice_config[character] = cfg

                if cast_name:
                    try:
                        key = get_cast_member_key(character, book_id)
                    except ValueError as e:
                        raise HTTPException(status_code=400, detail=str(e))
                    members = get_cast_storage_pool(lib, cast_name, character)
                    casting = {
                        "priority": suggestion.get("priority"),
                        "suggestion_reason": (suggestion.get("reason") or "")[:240],
                        "reuse_count_when_assigned": usage.get(adapter_id, {}).get("character_count", 0),
                        **get_trait_assignment_metadata(suggestion),
                    }
                    members[key] = _make_library_entry(
                        character, cfg, counts[character], book_id, casting, members.get(key))
                    usage = get_cast_adapter_usage(lib, cast_name, identities)
                applied.append(character)

            if cast_name:
                atomic_json_write_pair(voice_config, VOICE_CONFIG_PATH,
                                       lib, VOICE_LIBRARY_PATH)
            else:
                atomic_json_write(voice_config, VOICE_CONFIG_PATH)
    return {"applied": applied, "count": len(applied), "cast": cast_name,
            "book_id": book_id, "adapter_usage": get_cast_adapter_usage(lib, cast_name, identities)}


@router.post("/api/suggest_voices/apply")
async def apply_voice_suggestion(request: VoiceSuggestionApplyRequest):
    return await asyncio.to_thread(
        _apply_voice_suggestions, {request.character: request.suggestion},
        (request.cast or "").strip() or None)

@router.post("/api/suggest_voices/apply_bulk")
async def apply_voice_suggestions_bulk(request: VoiceSuggestionApplyBulkRequest):
    return await asyncio.to_thread(
        _apply_voice_suggestions, request.suggestions,
        (request.cast or "").strip() or None)
