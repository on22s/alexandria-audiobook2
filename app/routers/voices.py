from book_state_transaction import (JOURNAL, ensure_book_state, get_admitted_book_root,
                                    get_book_snapshot, get_book_snapshot_token, get_book_operation_token)
from chunk_status_journal import capture_chunk_state_file, get_chunk_status_journal_path
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
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Annotated, Callable, Dict, List, Optional, Literal

from fastapi import APIRouter, BackgroundTasks, HTTPException, Path, Query
from pydantic import BaseModel, Field, ValidationError, model_validator, field_validator
from config_settings import load_app_config
from editor_poll_snapshot import get_snapshot_digest
from voice_config_store import (apply_voice_config_update, get_voice_config_revision,
                                ensure_voice_config_storable, InvalidVoiceConfigError,
                                read_voice_config, VoiceConfigConflict, VoiceConfigReadError)
from project import ProjectManager, _new_chunk_uid
from tts import VoiceAliasResolutionError

from core import (
    AppError,
    CHUNKS_PATH,
    DESIGNED_VOICES_DIR,
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
    _script_line_counts, get_script_line_counts, get_cast_importance_order,
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
from voice_manifest import get_adapter_id_alias_map, get_adapter_manifest_rows, get_resolved_adapter_id_mapping, validate_adapter_name
from tts import (VERSION_OVERLAY_EXCLUDED, get_style_timeline_index, get_version_fields,
                 get_voice_config_for_version, resolve_narrator_voice_config, voice_category, voice_is_set)
from timeline_anchors import (TIMELINE_KEYS, TIMELINE_VIEW_FIELDS, ambiguous_uids, build_uid_index, delete_style_points,
                              canonical_point_payload,
                              get_anchor_uid, get_timeline_index_spelling, replace_style_point,
                              resolve_timeline_index, validate_write_anchor)
from speaker_traits import (get_age_distance, get_library_age_group,
                            get_entry_speaker, get_normalized_age_group, get_normalized_gender,
                            get_speaker_trait_summary, get_state_timeline, get_persona_state_targets, get_persona_state_chunk_indices, get_persona_state_entries,
                            find_state_target, get_state_spoken_lines, get_state_version_identity, is_state_version_current, is_possible_gender_reveal)
from utils import (
    atomic_json_write,
    atomic_json_write_pair,
    character_voice_seed,
    extract_json_object,
    file_lock,
    safe_load_json,
    secure_filename,
)
from persona_validation import DESIGN_COUNT_MAX, get_design_text_range, get_selectable_design_texts, validate_persona_payload
from repair_voice_config import get_unseeded_voice_seed_changes, apply_unseeded_voice_seeds
from voice_candidates import CANDIDATE_CONTROL, get_entry_using_candidate, get_entry_with_current_candidate


logger = logging.getLogger("AlexandriaUI")
router = APIRouter()


NarratorStrategy = Literal["global", "focus", "chapter", "character", "gender", "age",
                           "gender_age", "character_gender", "character_age", "character_gender_age"]


def _normalize_anchor_uid(value):
    """Validate an optional stable chunk anchor carried by a timeline point."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("from_uid must be a string")
    if not value or value != value.strip():
        raise ValueError("from_uid must be a non-empty string without surrounding whitespace")
    if len(value) > 128:
        raise ValueError("from_uid is too long")
    value.encode("utf-8")
    return value


class VoiceStylePoint(BaseModel):
    model_config = {"extra": "allow"}
    from_index: int | float | str = 0
    character_style: Optional[str] = ""

    @field_validator("from_index", mode="before")
    @classmethod
    def validate_from_index(cls, value):
        index = get_style_timeline_index({"from_index": value})
        return value if isinstance(value, (float, str)) else index

class VoiceVersionPoint(BaseModel):
    """From chunk `from_index` on, the character speaks with voice version
    `version_id` (a settled age/gender state, #653); None = the main voice
    again, for a state that changes back."""
    model_config = {"extra": "allow"}
    from_index: int | float | str = 0
    version_id: Optional[str] = Field(default=None, min_length=1, max_length=80)

    @field_validator("from_index", mode="before")
    @classmethod
    def validate_from_index(cls, value):
        index = get_style_timeline_index({"from_index": value})
        return value if isinstance(value, (float, str)) else index

class VoiceConfigItem(BaseModel):
    voice_presentation: Optional[str] = Field(default="", max_length=500)
    type: str = "custom"
    voice: Optional[str] = "Ryan"
    character_style: Optional[str] = ""
    default_style: Optional[str] = ""  # backward compat, prefer character_style
    seed: Optional[str] = "-1"
    ref_audio: Optional[str] = None
    ref_text: Optional[str] = None
    adapter_id: Optional[str] = None
    adapter_path: Optional[str] = None
    adapter_generation_sha256: Optional[str] = None
    description: Optional[str] = ""  # voice description (for design type)
    members: Optional[List[str]] = None  # speaker names to voice at once (ensemble type)
    # "Alias of" in the Voices tab: speak with this other speaker's voice
    # (ProjectManager._resolve_alias). Undeclared, the field was dropped by
    # model_dump() on every save, so no alias set in the UI ever took effect.
    alias_of: Optional[str] = None
    # NOTE: the legacy "alias" key is deliberately NOT a writable field here.
    # tts.get_resolved_speaker honors it as a fallback, but the guarded save
    # merge clears it whenever alias_of is explicitly provided (see
    # _apply_voice_save), so no public alias bypass is introduced (#1724 C020).
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
    style_timeline: Optional[List[Optional[VoiceStylePoint]]] = Field(default_factory=list)
    version_timeline: Optional[List[Optional[VoiceVersionPoint]]] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_ensemble_members(self):
        if self.type == "ensemble" and not self.members:
            raise ValueError("ensemble voices must include at least one member")
        if self.type == "ensemble" and any(not member.strip() for member in self.members):
            raise ValueError("ensemble member names cannot be blank")
        return self

    @field_validator("adapter_generation_sha256")
    @classmethod
    def validate_adapter_generation(cls, value):
        return get_validated_adapter_generation(value)


def get_validated_adapter_generation(value):
    """-> the adapter generation pin, or ValueError. The one pin rule for a
    top-level entry and for a version overlaid on it (#1804)."""
    if value is None:
        return value
    if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError("adapter_generation_sha256 must be 64 lowercase hex characters or null")
    return value

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
    # Confirmed after a 409 "voice_overrides": clear the alias, version
    # timeline and narrator strategy that would keep another voice effective
    # (#1800, #2015).
    clear_overrides: bool = False

class VoiceSuggestionApplyBulkRequest(BaseModel):
    cast: Optional[str] = None
    suggestions: Dict[str, Dict]
    clear_overrides: bool = False


class VoiceTargetScope(BaseModel):
    version_id: Optional[str] = Field(default=None, min_length=1, max_length=80)
    book_token: Optional[str] = Field(default=None, pattern=BOOK_TOKEN_PATTERN)


class VoiceBookScope(BaseModel):
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
    # Design this speaker's voice from one of their own lines, keeping the
    # current persona description and skipping the LLM (#1232).
    ref_text: Optional[str] = Field(default=None, min_length=1, max_length=500)
    # How many voices to design from ref_text in one task, each a saved candidate (#1313).
    design_count: Optional[int] = Field(default=None, ge=1, le=DESIGN_COUNT_MAX)


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


def _get_book_voice_manager(config_path, *, read_only=False):
    root = os.path.dirname(config_path)
    chunks_path = CHUNKS_PATH if os.path.dirname(CHUNKS_PATH) == root else os.path.join(root, "chunks.json")
    manager = (project_manager if project_manager.chunks_path == chunks_path else
               ProjectManager(root, create_audio_dir=not read_only))
    if manager is not project_manager:
        manager.chunks_path = chunks_path
        manager.voice_config_path = config_path
    return manager


def _apply_book_voice_update(transform, expected_revision=None, config_path=None, *,
                             publish=None, timeline_edit=False):
    """Publish voice changes while fencing affected renders under chunk locks.

    Timeline transforms receive the saved rows and may assign a missing UID
    in memory. Publish those assignments only after the config passes validation.
    """
    config_path = VOICE_CONFIG_PATH if config_path is None else config_path
    manager = _get_book_voice_manager(config_path)
    from take_retention import admit_take_history_edit
    with admit_take_history_edit(manager.root_dir), manager._chunks_lock, file_lock(manager.chunks_path):
        def update(current):
            if timeline_edit:
                _validate_editable_timeline_document(current)
                chunks = _get_authoritative_chunks_locked(manager)
                old_uids = [row.get("uid") for row in chunks or []]
                updated = transform(current, chunks)
            else:
                updated = transform(current)
            # Pre-invalidation strict-map validation (DS3021 remainder):
            # the one storable validator runs on the FINAL proposed map
            # BEFORE any audio invalidation side effect, so a nonfinite
            # or storage-unserializable payload is an all-file no-write
            # refusal. It matches the actual stored encoding (indent=2),
            # not only the compact revision digest. Valid updates keep
            # the crucial order: invalidation fences old/in-flight audio
            # BEFORE the config publish inside apply_voice_config_update
            # (which re-validates before writing), preserving
            # conservative fencing on publication I/O failure.
            ensure_voice_config_storable(updated)
            if not timeline_edit:
                manager._apply_voice_timeline_audio_invalidation_locked(current, updated)
            elif chunks is not None:
                manager._apply_voice_timeline_audio_invalidation_locked(
                    current, updated, chunks=chunks,
                    uids_added=old_uids != [row.get("uid") for row in chunks])
            return updated
        try:
            return apply_voice_config_update(config_path, update, expected_revision=expected_revision,
                                             publish=publish)
        except VoiceAliasResolutionError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except InvalidVoiceConfigError as exc:
            if not timeline_edit:
                raise
            _refuse_invalid_timeline_voice_document(exc)
        except VoiceConfigReadError as exc:
            if not timeline_edit:
                raise
            _refuse_timeline_voice_read(exc)


@dataclass(frozen=True)
class _TimelineMutation:
    apply: Callable
    expected_book_token: Optional[str] = None
    replay_unconfigured: bool = False

    def __call__(self, entry, chunks=None):
        self.apply(entry, chunks)


def _mutate_book_voice_entry(speaker, mutator, book_token=None):
    timeline_edit = isinstance(mutator, _TimelineMutation)
    root = os.path.dirname(VOICE_CONFIG_PATH)
    with ensure_book_state(root):
        expected_book_token = mutator.expected_book_token if timeline_edit else None
        if expected_book_token is not None and get_book_operation_token(root) != expected_book_token:
            raise AppError(409, 'Active book changed; reload Editor before saving',
                           key='safety.voice.timeline_editor_book_changed')
        if book_token is not None:
            snapshot = get_book_snapshot(root, include_voices=False)
            if get_book_snapshot_token(snapshot) != book_token:
                raise AppError(409, 'Active book changed; reload voices before saving', key='safety.voice.book_changed_before_save')
        if speaker == "NARRATOR":
            speaker = get_script_narrator_speaker()
        _require_script_speaker(speaker)
        def update(current, *args):
            config = copy.deepcopy(current)
            # Canonical-null + malformed-entry seam: explicit null is
            # declared unconfigured, so entry-consuming actions refuse
            # clearly (422) instead of crashing on None or fabricating
            # defaults. Absent keys keep the existing create-{} behavior;
            # explicit ordinary/bulk assignment never reaches this seam.
            _stored_entry = _get_timeline_voice_entry(config, speaker) if timeline_edit else config.get(speaker)
            if speaker in config and _stored_entry is None:
                if timeline_edit and mutator.replay_unconfigured:
                    # An empty State replay checks its fences but cannot initialize a voice.
                    mutator({}, *args)
                    raise _NoOpEcho([])
                raise HTTPException(status_code=422, detail=(
                    f"Speaker '{speaker}' has no voice configured; "
                    "assign a voice before modifying it."))
            if _stored_entry is not None and not isinstance(_stored_entry, dict):
                raise HTTPException(status_code=422, detail=(
                    f"Speaker '{speaker}' has a malformed voice entry (not an object). "
                    "Restore the saved voice configuration before continuing."))
            mutator(config.setdefault(speaker, {}), *args)
            return config
        config = _apply_book_voice_update(update, timeline_edit=timeline_edit)
        return config[speaker], get_voice_config_revision(config)


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
        raise AppError(409, 'Active book changed; reload voices', key='safety.voice.book_changed')
    return get_current_persona_state(speaker, version_id, script=script)


def _require_versions_dict(speaker, entry):
    """The one shared versions-container policy for every mutation seam.

    Canonical null/absent means no saved versions (existing empty
    semantics, returned as {}); a present non-object container is
    malformed data and refuses with an actionable HTTPException 400
    BEFORE any mutator consumes it or any write/invalidation happens.
    Legitimate dict histories pass through untouched (never healed).
    """
    versions = entry.get("versions") if isinstance(entry, dict) else None
    if versions is None:
        return {}
    if not isinstance(versions, dict):
        raise HTTPException(status_code=400, detail=(
            f"Speaker '{speaker}' has malformed voice versions (not an object); "
            "restore the saved voice configuration before continuing."))
    return versions


def _get_lora_adapter_id(config, *, status_code=422):
    """Validate the identifier consumed by an effective voice configuration."""
    if voice_category(config) != "lora":
        return None
    adapter_id = config.get("adapter_id")
    if adapter_id is None or adapter_id == "":
        return None
    try:
        validate_adapter_name(adapter_id)
    except ValueError as exc:
        raise AppError(status_code, 'A LoRA adapter ID must be a valid adapter name.',
                       key='error.voice.invalid_adapter_id') from exc
    return adapter_id


def get_validated_voice_changes(speaker, before, after, *, check_timeline=False, allow_state_removal=False,
                                trusted_states=(), timeline_chunks=None):
    """Validate state invariants for every public voice-config write path.

    `persona_state` is server-owned: only generation and recovery set it
    (they pass `trusted_states`). A client may keep the stored value but never
    introduce or change one, and `state_<24 hex>` ids are reserved for state
    versions (#1040 review C6)."""
    # Central consumed-container validation via the one shared policy
    # above (HTTPException so every facade answers 4xx, never 500).
    old_versions = _require_versions_dict(speaker, before)
    versions = _require_versions_dict(speaker, after)
    _get_lora_adapter_id(after)
    for version_id, version in versions.items():
        if not isinstance(version, dict):
            continue
        _get_lora_adapter_id(get_voice_config_for_version(after, version_id))
        if version_id in trusted_states:
            continue
        old_version = old_versions.get(version_id)
        old_state = old_version.get("persona_state") if isinstance(old_version, dict) else None
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
    applied = {point.get("version_id") for point in points if isinstance(point, dict)} | {after.get("active_version")}
    if removed & applied:
        raise AppError(409, 'Voice version is applied; select another base version or clear its timeline points first', key='error.voice.version_applied')
    changed_states = [(version_id, version) for version_id, version in versions.items()
                      if isinstance(version, dict) and version != old_versions.get(version_id)
                      and ("persona_state" in version or (
                          isinstance(old_versions.get(version_id), dict)
                          and "persona_state" in old_versions[version_id]))]
    changed_states.extend((version_id, old_versions[version_id]) for version_id in removed
                          if isinstance(old_versions[version_id], dict) and "persona_state" in old_versions[version_id])
    timeline_changed = check_timeline or points != (before.get("version_timeline") or [])
    if timeline_changed:
        missing = [point["version_id"] for point in points
                   if isinstance(point, dict) and point.get("version_id") is not None
                   and point["version_id"] not in versions]
        if missing:
            raise HTTPException(status_code=400, detail=f"Unknown voice version: {', '.join(missing)}")
    state_points = [point for point in points if isinstance(point, dict) and point.get("version_id")
                    and isinstance(versions.get(point["version_id"]), dict)
                    and "persona_state" in versions[point["version_id"]]] if timeline_changed else []
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
        chunks = timeline_chunks if timeline_chunks is not None else safe_load_json(CHUNKS_PATH, default=[])
        mapping = get_persona_state_chunk_indices(script, chunks, speaker, state_targets=targets)
        uid_index = build_uid_index(chunks)
        for point in state_points:
            target = by_id.get(point["version_id"])
            version = versions[point["version_id"]]
            if (target is None or not is_state_version_current(version, target)
                    or mapping.get(target["from_entry"]) != resolve_timeline_index(point, uid_index)):
                raise HTTPException(status_code=409, detail="State boundary is stale or lies inside an edited/merged chunk; rebuild chunks and review the timeline")
    return after


def get_validated_voice_target(entry, speaker, version_id, book_token):
    if book_token is None:
        raise HTTPException(status_code=409, detail="State actions require the current book token")
    version = _require_versions_dict(speaker, entry).get(version_id)
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


def get_script_narrator_speaker(*, script=None):
    """Resolve only the supported modern and legacy narrator spellings."""
    if script is None:
        _require_script_speaker(None)
        script = safe_load_json(SCRIPT_PATH, default=[])
    speakers = {get_script_speaker(entry) for entry in script}
    for name in ("NARRATOR", "Narrator"):
        if name in speakers:
            return name
    raise HTTPException(status_code=404, detail="Narrator is not present in the active script")


def _require_script_speaker(speaker, *, script=None):
    if script is None:
        if not os.path.isfile(SCRIPT_PATH):
            raise HTTPException(status_code=422, detail="Generate or open an active script first")
        if speaker is not None:
            script = safe_load_json(SCRIPT_PATH, default=[])
    if speaker is None:
        return
    speakers = {get_script_speaker(entry) for entry in script}
    if speaker not in speakers:
        raise HTTPException(status_code=404, detail="Speaker is not present in the active script")


@router.get("/api/voices")
async def get_voices():
    return await asyncio.to_thread(_ensure_voice_listing)


def _validate_consumed_description(speaker, entry):
    """Refuse an entry whose category now consumes a malformed description.

    The one narrow consumed-field rule shared by active reads, proposal
    merges and guarded saves: a custom/design entry with a present
    non-None non-string description is corrupt data. Absent/null stays
    the legitimate unconfigured contract; other categories leave the
    legacy value byte-preserved and untouched. Domain ValueError so
    HTTP facades map it consistently; callers run it on the RESULTING
    merged entry (preview or save), never on untouched entries.
    """
    if isinstance(entry, dict) and voice_category(entry) in ("custom", "design"):
        description = entry.get("description")
        if description is not None and not isinstance(description, str):
            raise ValueError(
                f"Speaker '{speaker}' has a malformed voice description; "
                "restore the saved voice configuration before continuing.")


def _validate_consumed_overlay(speaker, overlaid):
    """Refuse a card as a version or candidate would leave it when it carries
    a consumed value the top-level save refuses (#1804, #1799): a malformed
    custom/design description, adapter identifier, or generation pin. -> HTTP 422."""
    _get_lora_adapter_id(overlaid)
    try:
        _validate_consumed_description(speaker, overlaid)
        get_validated_adapter_generation(overlaid.get("adapter_generation_sha256"))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=f"Voice configuration for '{speaker}' is invalid: {exc}") from exc


def get_voice_rows(script_data, voice_config):
    """Build one backend eligibility/roster view for legacy and guarded reads."""
    # Most-spoken characters first (owner, 2026-10-09): the same order casting
    # uses, so the list shows who gets first pick of unused voices.
    line_counts = get_script_line_counts(script_data)
    roster = get_cast_importance_order({name for entry in script_data if (name := get_script_speaker(entry))}, line_counts)
    lines = {name: [] for name in roster}
    for entry in script_data:
        name = get_script_speaker(entry)
        if name in lines and isinstance(entry, dict):
            lines[name].append(entry)
    # Narrow domain-boundary validation of malformed external field
    # shapes (CF60009, narrowed): only fields actually consumed are
    # validated. Trait summary consumes roster rows with speaker_gender
    # present; the state timeline consumes dict rows with speaker_gender
    # and a truthy raw speaker (including whitespace-only and legacy
    # type-derived speakers via the roster). Speakerless or age-only
    # metadata is ignored exactly as before. Description is consumed
    # only for custom AND design categories (voice_is_set .strip()s
    # custom descriptions; TTS design generation .strip()s design
    # descriptions) — unused legacy descriptions on other categories
    # stay byte-preserved. Absent/null description stays the legitimate
    # unconfigured contract; inactive entries are not validated. No
    # broad TypeError/AttributeError catch and no silent coercion:
    # genuine programmer errors propagate.
    for entry in script_data:
        if not isinstance(entry, dict) or "speaker_gender" not in entry:
            continue
        consumed = (get_script_speaker(entry) in lines
                    or bool(entry.get("speaker")))
        if not consumed:
            continue
        if ("speaker_age_group" not in entry
                or not isinstance(entry.get("speaker_gender"), str)
                or not isinstance(entry.get("speaker_age_group"), str)):
            raise ValueError(
                "Active script has malformed speaker trait fields; "
                "restore the book state or reload the script, then try again.")
    state_targets = get_persona_state_targets(script_data)
    if not isinstance(voice_config, dict):
        raise ValueError(
            "Saved voice configuration must be an object; "
            "restore the saved voice configuration before continuing.")
    # A present-but-non-object ACTIVE entry is corrupt saved data, not an
    # unconfigured voice: refuse clearly instead of reading it as {} /
    # persona_pending. The canonical null representation is the ONE
    # exception: null means unconfigured (readable, persona_pending,
    # no persistence/default writes on reads) in the canonical
    # consumer contract. Absent active keys stay unconfigured;
    # inactive legacy keys are not in the roster, are not validated
    # here, and are never touched by this read. Domain ValueError (not
    # HTTPException) so compound callers can map it to their
    # unavailable/partial contract; HTTP facades map it to 400.
    for name in roster:
        if name in voice_config and voice_config[name] is not None and not isinstance(voice_config[name], dict):
            raise ValueError(
                f"Speaker '{name}' has a malformed voice entry (not an object); "
                "restore the saved voice configuration before continuing.")
        if name in voice_config and isinstance(voice_config[name], dict):
            _validate_consumed_description(name, voice_config[name])
    rows = []
    for name in roster:
        entry = voice_config.get(name, {})
        row = {"name": name, "config": entry, "line_count": line_counts.get(name, 0),
               "persona_pending": not voice_is_set(entry)}
        # Null (unconfigured) entries keep their canonical None config
        # in the row; read-only logic below uses an empty view and
        # never writes defaults back.
        entry_view = entry if isinstance(entry, dict) else {}
        # Per-line gender/age from pass 2, only when the run asked for them, so
        # a book generated without the switch gets exactly the old rows.
        # Alias eligibility matches the canonical runtime
        # (tts.get_resolved_speaker): only a non-blank string alias counts.
        # A whitespace/non-string legacy value is ignored by the runtime, so
        # it must not suppress persona states here either.
        _effective_alias = entry_view.get("alias_of") or entry_view.get("alias")
        _has_effective_alias = isinstance(_effective_alias, str) and bool(_effective_alias.strip())
        if name in state_targets and not _has_effective_alias:
            # Nullable contract: versions null/absent means no saved states.
            # A present non-object versions container (or a non-object saved
            # state for a current target) is malformed ACTIVE data: refuse
            # via domain ValueError instead of raising AttributeError.
            saved = entry_view.get("versions", {})
            if saved is None:
                saved = {}
            if not isinstance(saved, dict):
                raise ValueError(
                    f"Speaker '{name}' has malformed voice versions (not an object); "
                    "restore the saved voice configuration before continuing.")
            for _target in state_targets[name]:
                # Null saved state is absent/unconfigured (voice_is_set
                # and is_state_version_current already treat it so);
                # only non-null non-objects are malformed.
                if (_target["version_id"] in saved and saved[_target["version_id"]] is not None
                        and not isinstance(saved[_target["version_id"]], dict)):
                    raise ValueError(
                        f"Speaker '{name}' has a malformed saved voice state "
                        f"'{_target['version_id']}'; restore the saved voice configuration "
                        "before continuing.")
                if (_target["version_id"] in saved and isinstance(saved[_target["version_id"]], dict)
                        and voice_category(saved[_target["version_id"]]) in ("custom", "design")):
                    _saved_description = saved[_target["version_id"]].get("description")
                    if _saved_description is not None and not isinstance(_saved_description, str):
                        raise ValueError(
                            f"Speaker '{name}' has a malformed saved voice state "
                            f"description '{_target['version_id']}'; restore the saved voice "
                            "configuration before continuing.")
            # Each state says whether its saved version is current, by the
            # server's own rule, so the cards never re-derive it (C37).
            # Each state carries its own line count (the character's spoken
            # lines inside its segment) for the card's badge.
            row["persona_states"] = [{**target, "current": is_state_version_current(saved.get(target["version_id"]), target),
                                      "line_count": get_script_line_counts(script_data[target["segment_start"]:target["segment_end"]]).get(name, 0)}
                                     for target in state_targets[name]]
            row["persona_states_pending"] = any(
                not voice_is_set(saved.get(target["version_id"])) or not state["current"]
                for target, state in zip(state_targets[name], row["persona_states"]))
        traits = get_speaker_trait_summary(lines[name])
        if traits:
            row["traits"] = traits
        rows.append(row)
    return rows


def _enter_read_admission(stack, *factories):
    """Enter read-admission managers, mapping entry failures honestly.

    Factories are called inside the try so entry-stage construction
    failures are mapped too. Entry-stage TimeoutError is a busy 503;
    entry-stage OSError/ValueError (filesystem, lock, invalid
    journal/root admission) is a 409 integrity refusal. Body-stage
    corrupt JSON/domain errors are mapped separately by each facade
    (400), so this wrapper never masks them, and real programmer
    TypeError/AttributeError errors propagate unchanged.
    """
    for factory in factories:
        try:
            stack.enter_context(factory())
        except TimeoutError as exc:
            raise AppError(503, 'Book or voices are busy; please try again.', key='error.voice.busy') from exc
        except (OSError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=(
                "Active book could not be verified while reading voices. "
                "Restore the book state or try again.")) from exc


def _ensure_voice_listing():
    # Strict listing (DS3005): reuse the same snapshot admission the
    # guarded snapshot route uses. Corrupt/unreadable PRESENT script or
    # voice data refuses clearly instead of folding into plausible
    # defaults; absent files stay ordinary (empty roster / unconfigured).
    from contextlib import ExitStack
    with ExitStack() as stack:
        _enter_read_admission(stack, lambda: ensure_book_state(os.path.dirname(SCRIPT_PATH)))
        try:
            snapshot = get_book_snapshot(os.path.dirname(SCRIPT_PATH),
                                         allow_missing_script=True)
            script_data = (json.loads(snapshot["script_bytes"])
                           if snapshot["script_bytes"] is not None else [])
            if not isinstance(script_data, list):
                raise ValueError("Active script must contain an entry list")
            voice_config = snapshot["voices"]
        except (ValueError, RecursionError) as exc:
            raise HTTPException(status_code=400, detail=(
                "Active book data is invalid and cannot be read. "
                "Restore the book state or reload the script, then try again.")) from exc
        except OSError as exc:
            raise HTTPException(status_code=409, detail=(
                "Active book could not be verified while reading voices. "
                "Restore the book state or try again.")) from exc
        try:
            return get_voice_rows(script_data, voice_config)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/api/voices/{speaker}/versions")
async def save_voice_version(speaker: str, request: VoiceVersionRequest):
    def save_version(current):
        _versions = _require_versions_dict(speaker, current)
        config = dict(request.config or {key: value for key, value in current.items()
                                         if key not in VERSION_OVERLAY_EXCLUDED})
        if config.get("type") not in {None, "custom", "clone", "design", "lora", "builtin_lora", "ensemble"}:
            raise HTTPException(status_code=422, detail="Unsupported voice version type")
        previous = _versions.get(request.version_id) or {}
        if "persona_state" in previous:
            # Saving over a state version keeps its server-owned state (C5).
            config["persona_state"] = previous["persona_state"]
        age = previous.get("age_group", request.age_group) if "persona_state" in previous and "age_group" not in request.model_fields_set else request.age_group
        updated = {**current, "versions": {**_versions,
                    request.version_id: {**config, "age_group": age}}}
        _validate_consumed_overlay(speaker, get_voice_config_for_version(updated, request.version_id))
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
        _versions = _require_versions_dict(speaker, entry)
        if version_id not in _versions:
            raise HTTPException(status_code=404, detail="Voice version not found")
        versions = {key: value for key, value in _versions.items() if key != version_id}
        get_validated_voice_changes(speaker, entry, {**entry, "versions": versions}, allow_state_removal=True)
        entry["versions"] = versions
    entry, revision = await asyncio.to_thread(_mutate_book_voice_entry, speaker, remove, book_token)
    return {"status": "removed", "speaker": speaker, "version_id": version_id,
            "versions": entry.get("versions", {}), "revision": revision}


@router.post("/api/voices/{speaker}/versions/{version_id}/select")
async def select_voice_version(speaker: str, version_id: str, request: VoiceBookScope = VoiceBookScope()):
    await asyncio.to_thread(_require_script_speaker, speaker)
    def select(entry):
        version = _require_versions_dict(speaker, entry).get(version_id)
        if not isinstance(version, dict):
            raise HTTPException(status_code=404, detail="Voice version not found")
        if "persona_state" in version:
            # A state voice belongs to its part of the book: it is applied on
            # the voice timeline, never copied over the whole character (C3).
            raise HTTPException(status_code=409, detail="This is a character-state voice; apply it from Voice changes on its state card")
        after = {**get_voice_config_for_version(entry, version_id), "age_group": version.get("age_group"),
                 "active_version": version_id, "voice_status": "assigned"}
        _get_lora_adapter_id(after)
        # DS3010: if the chosen version does not explicitly define a
        # pin and its adapter differs from the entry's, the old pin
        # must not follow the new asset. Same-adapter versions keep it.
        if "adapter_generation_sha256" not in (get_version_fields(entry, version_id) or {}):
            if not _voice_adapter_identity_same(
                    entry.get("type") or "custom", entry.get("adapter_id"),
                    after.get("type") or "custom", after.get("adapter_id")):
                after["adapter_generation_sha256"] = None
        _validate_consumed_overlay(speaker, after)
        get_validated_voice_changes(speaker, entry, after)
        entry.clear()
        entry.update(after)
    entry, revision = await asyncio.to_thread(_mutate_book_voice_entry, speaker, select, request.book_token)
    return {"status": "selected", "speaker": speaker, "version_id": version_id,
            "config": entry, "revision": revision}


def _get_candidate_kept_fields(version_id):
    """Fields a candidate never replaces: a state version keeps its own identity."""
    return ({"persona_state", "description", "age_group", "gender", "versions", "version_timeline"}
            if version_id else set())


def _validate_candidate_use(speaker, entry, selected):
    """Refuse a card that using a candidate would leave with a malformed
    consumed value (#1799): the same seed, description and pin rules as any
    other voice save."""
    get_validated_voice_changes(speaker, entry, selected)
    _validate_consumed_overlay(speaker, selected)


@router.post("/api/voices/{speaker}/candidates")
async def add_voice_candidate(speaker: str, request: VoiceCandidateRequest):
    await asyncio.to_thread(_require_script_speaker, speaker)
    control = sorted(CANDIDATE_CONTROL.intersection(request.config))
    if control:
        raise HTTPException(status_code=422, detail=(
            "A voice candidate holds a voice only; remove the character fields: " + ", ".join(control)))
    def add(entry):
        candidate = {**request.config, "candidate_id": request.candidate_id}
        _validate_candidate_use(speaker, entry, get_entry_using_candidate(
            entry, candidate, _get_candidate_kept_fields(request.version_id)))
        candidates = [c for c in entry.get("candidates", [])
                      if isinstance(c, dict) and c.get("candidate_id") != request.candidate_id]
        candidates.append(candidate)
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
        # Save the voice being replaced first, so "Use" can never lose it -
        # including voices made before every voice was saved (#1297).
        entry["candidates"] = get_entry_with_current_candidate(entry).get(
            "candidates", entry.get("candidates", []))
        # The card takes exactly the candidate's voice (no fields left over from
        # the previous one); a state version keeps its own identity fields.
        selected = get_entry_using_candidate(entry, candidate, _get_candidate_kept_fields(request.version_id))
        _validate_candidate_use(speaker, entry, selected)
        entry.clear()
        entry.update(selected)
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
    book_token: Optional[str] = Field(default=None, pattern=BOOK_TOKEN_PATTERN)
    expected_book_token: Optional[str] = Field(default=None, pattern=BOOK_TOKEN_PATTERN)

    @field_validator("from_index", mode="before")
    @classmethod
    def validate_from_index(cls, value):
        return get_style_timeline_index({"from_index": value})


@router.post("/api/voices/{speaker}/style_timeline")
async def add_style_point(speaker: str, request: StylePointRequest):
    """Set the character's style from the chosen line onward, or clear it with an empty style."""
    style = request.character_style.strip()
    display = None
    def add(current, chunks):
        nonlocal display
        request_uid = _request_anchor_uid(request)
        points = _get_stored_timeline(current, "style_timeline")
        uid_index = build_uid_index(chunks)
        live_uid = request_uid or get_anchor_uid(chunks, request.from_index)
        submitted = {"from_index": request.from_index, "from_uid": request_uid,
                     "character_style": style}
        effective = next((point for point in reversed(points) if isinstance(point, dict)
                          and resolve_timeline_index(point, uid_index) ==
                              uid_index.get(live_uid, request.from_index)), None)
        echo = bool(style) and any(_get_matching_point([effective], {**submitted, "from_uid": candidate},
            "character_style") is not None for candidate in {request_uid, live_uid})
        index, uid = validate_write_anchor(chunks, request.from_index, request_uid, exact=echo)
        if echo:
            raise _NoOpEcho(_with_display_index(points, build_uid_index(chunks)))
        if style and uid is None and chunks is not None:
            uid = _ensure_timeline_uid(chunks[index])
        updated = replace_style_point(points, index, uid, style, uid_index=build_uid_index(chunks))
        display = _with_display_index(updated, build_uid_index(chunks))
        if updated == points:
            raise _NoOpEcho(display)
        current["style_timeline"] = updated
    try:
        entry, _revision = await asyncio.to_thread(
            _mutate_book_voice_entry, speaker, _TimelineMutation(add, request.expected_book_token),
            request.book_token)
    except _NoOpEcho as echo:
        display = echo.points
    except ValueError as exc:
        _refuse_timeline_write_conflict(exc)
    else:
        if display is None:
            display = entry.get("style_timeline", [])
    return {"status": "saved", "speaker": speaker, "style_timeline": display}


def get_library_voice_config(candidate):
    """The voice config a library candidate is assigned with - the one shape
    _apply_voice_suggestions writes, reused for a state's version."""
    if candidate["type"] == "clone":
        return copy.deepcopy(candidate["config"])
    return {"type": candidate["type"], "adapter_id": candidate["adapter_id"],
            "adapter_path": (f"builtin_lora/{candidate['adapter_id']}"
                             if candidate["type"] == "builtin_lora"
                             else f"lora_models/{candidate['adapter_id']}")}


def get_clone_reference_path(value):
    """Resolve relative, absolute and Windows-style saved reference paths."""
    return os.path.normcase(os.path.realpath(os.path.join(
        os.path.dirname(DESIGNED_VOICES_DIR), value.replace("\\", "/"))))


def get_adapter_users(voice_config, candidates=()):
    """-> {adapter_id: [speakers]} for every LoRA voice the book already uses,
    as a main voice or as a version; optionally include Designer references."""
    clone_ids = {get_clone_reference_path(c["config"]["ref_audio"]): c["adapter_id"]
                 for c in candidates if c.get("type") == "clone"}
    users = {}
    for name, entry in (voice_config or {}).items():
        if not isinstance(entry, dict):
            continue
        versions = _require_versions_dict(name, entry)
        configs = [(None, entry), *((key, v) for key, v in versions.items() if isinstance(v, dict))]
        for version_id, config in configs:
            adapter_id = _get_lora_adapter_id(get_voice_config_for_version(entry, version_id), status_code=400)
            if config.get("type") == "clone" and isinstance(config.get("ref_audio"), str):
                adapter_id = clone_ids.get(get_clone_reference_path(config["ref_audio"]))
            if adapter_id:
                users.setdefault(adapter_id, [])
                if name not in users[adapter_id]:
                    users[adapter_id].append(name)
    return users


def get_state_voice_candidates():
    """Downloaded adapters plus playable saved Designer clone references."""
    from routers.voice_design import get_voice_asset_path
    from audio_validation import validate_generated_audio, GeneratedAudioError
    candidates = _build_lora_candidates(read_only=True)
    for row in _load_manifest(os.path.join(DESIGNED_VOICES_DIR, "manifest.json"), record_diagnostics=False):
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            continue
        text = row.get("sample_text")
        if not isinstance(text, str) or not text.strip():
            continue
        try:
            validate_generated_audio(get_voice_asset_path(DESIGNED_VOICES_DIR, row), "saved designed voice")
        except (HTTPException, GeneratedAudioError, OSError, ValueError) as error:
            logger.warning("Skipping unusable designed voice %s: %s", row["id"], error)
            continue
        description = row.get("description") if isinstance(row.get("description"), str) else ""
        name = row.get("name") if isinstance(row.get("name"), str) and row["name"] else row["id"]
        # Casting labels are explicit metadata; vocal style is not identity.
        age = get_normalized_age_group(row.get("age_group"))
        candidates.append({"adapter_id": "designed:" + row["id"], "type": "clone",
            "name": name, "gender": get_normalized_gender(row.get("gender")),
            "age_group": age, "description": description,
            "config": {"type": "clone", "ref_audio": "designed_voices/" + row["filename"],
                       "ref_text": text, "description": description,
                       "gender": get_normalized_gender(row.get("gender")), "reference_age_group": age}})
    return candidates


def get_state_candidate_age_distance(state_age, candidate):
    """Designer metadata retains child bands; adapters use legacy library bands."""
    age = candidate.get("age_group", "unknown")
    if candidate.get("type") == "clone":
        age = get_normalized_age_group(candidate.get("reference_age_group", age))
    else:
        state_age, age = get_library_age_group(state_age), get_library_age_group(age)
    return get_age_distance(state_age, age)


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
    reference_ages = {get_clone_reference_path(c["config"]["ref_audio"]): c["age_group"]
                      for c in candidates if c.get("type") == "clone"
                      and c.get("age_group", "unknown") != "unknown"}
    for version_id, version in sorted((entry.get("versions") or {}).items()):
        if not isinstance(version, dict):
            continue
        if (version.get("type") == "clone" and "reference_age_group" not in version
                and isinstance(version.get("ref_audio"), str)):
            source_age = reference_ages.get(get_clone_reference_path(version["ref_audio"]))
            if source_age is not None:
                version = {**version, "reference_age_group": source_age}
        version_age = get_normalized_age_group(version.get("age_group"))
        version_gender = version.get("gender") or "unknown"
        if (get_state_candidate_age_distance(state["age_group"], {**version, "age_group": version_age}) <= 1 and version_age != "unknown"
                and version_gender in ("unknown", gender)):
            versions.append({"version_id": version_id, "age_group": version.get("age_group")})
    ranked = _rank_heuristic_candidates("", [{**c, "age_group": get_library_age_group(c.get("age_group", "unknown"))} for c in candidates],
                                        gender if gender in ("male", "female") else None, age, filter_gender=False)
    by_id = {c["adapter_id"]: c for c in candidates}
    ranked.sort(key=lambda key: (
        get_state_candidate_age_distance(state["age_group"], by_id[key]),
        0 if gender not in ("male", "female") or by_id[key].get("gender") == gender else 1))
    fitting = [by_id[i] for i in ranked
               if (by_id[i].get("gender") in ("unknown", gender) or gender not in ("male", "female"))
               and (age == "unknown" or (by_id[i].get("age_group", "unknown") != "unknown"
                                         and get_state_candidate_age_distance(state["age_group"], by_id[i]) <= 1))]
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


def get_speaker_design_texts(speaker, version_id=None, book_token=None):
    """-> the selectable Voice Design texts for one speaker, or for one of its
    states (#1251). Blocking: call from a thread."""
    _require_script_speaker(speaker)
    script = safe_load_json(SCRIPT_PATH, default=[])
    if version_id:
        target = require_current_state_target(speaker, version_id, book_token, script=script)
        return get_selectable_design_texts(get_state_spoken_lines(script, target))
    return get_selectable_design_texts([(entry.get("text") or "") for entry in script
                                        if isinstance(entry, dict) and get_script_speaker(entry) == speaker])


@router.get("/api/voices/{speaker}/design_lines")
async def get_voice_design_lines(speaker: str, version_id: Optional[str] = None, book_token: Optional[str] = None):
    """The speaker's or state's lines for the card design picker, in-range first (#1232)."""
    def read():
        texts = get_speaker_design_texts(speaker, version_id, book_token)
        min_chars, max_chars = get_design_text_range(load_app_config(CONFIG_PATH))
        lines = [{"text": text, "chars": len(text), "in_range": min_chars <= len(text) <= max_chars}
                 for text in texts]
        lines.sort(key=lambda line: not line["in_range"])
        return {"speaker": speaker, "min_chars": min_chars, "max_chars": max_chars, "lines": lines}
    return await asyncio.to_thread(read)


def require_chosen_design_line(request):
    """Refuse a chosen design line that is not this speaker's or state's own (#1232). Blocking."""
    if not request.speaker or request.age_group or request.advanced or request.new_only:
        raise HTTPException(status_code=422, detail="Designing from a chosen line needs one character and no other options")
    if request.ref_text not in get_speaker_design_texts(request.speaker, request.state_version, request.book_token):
        raise HTTPException(status_code=422, detail="The chosen line is not one of this character's lines; reload Voices")
    entry = safe_load_json(VOICE_CONFIG_PATH, default={}).get(request.speaker)
    if request.state_version and isinstance(entry, dict):
        entry = (entry.get("versions") or {}).get(request.state_version)
    if not isinstance(entry, dict) or not str(entry.get("description") or "").strip():
        raise HTTPException(status_code=409, detail="Generate a persona for this character first; a chosen line reuses its description")


@router.get("/api/voices/{speaker}/state_timeline")
async def get_voice_state_timeline(speaker: str):
    """The suggested voice timeline for a character whose settled gender/age
    changes (#653). Suggests only: nothing changes audio until it is applied."""
    def get_input_versions(manager):
        """Fence a lock-free filesystem read against book replacement and publication."""
        def stamp(path):
            try:
                info = os.lstat(path)
            except FileNotFoundError:
                return None
            return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        try:
            root = get_admitted_book_root(manager.root_dir)
            if stamp(root / JOURNAL) is not None:
                raise ValueError("Book recovery is pending")
            return tuple(map(stamp, (root, SCRIPT_PATH, VOICE_CONFIG_PATH, manager.chunks_path,
                get_chunk_status_journal_path(manager.chunks_path), root / "state.json")))
        except (OSError, ValueError) as exc:
            _refuse_unverified_timeline_chunks(exc)

    def build():
        manager = _get_book_voice_manager(VOICE_CONFIG_PATH, read_only=True)
        with manager._chunks_lock:
            before = get_input_versions(manager)
            try:
                raw_script = capture_chunk_state_file(SCRIPT_PATH, what="script")
                script = json.loads(raw_script) if raw_script is not None else None
                if raw_script is not None and (not isinstance(script, list)
                        or any(not isinstance(row, dict) for row in script)):
                    raise ValueError("Invalid active script")
            except (OSError, ValueError, RecursionError) as exc:
                _refuse_unverified_timeline_chunks(exc)
            name = get_script_narrator_speaker(script=script) if speaker == "NARRATOR" else speaker
            _require_script_speaker(name, script=script)
            try:
                voice_config = read_voice_config(VOICE_CONFIG_PATH)
            except InvalidVoiceConfigError as exc:
                _refuse_invalid_timeline_voice_document(exc)
            except VoiceConfigReadError as exc:
                _refuse_timeline_voice_read(exc)
            _validate_editable_timeline_document(voice_config)
            entry = _get_timeline_voice_entry(voice_config, name) or {}
            _get_replayable_version_timeline(entry)
            chunks = _get_authoritative_chunks_locked(manager) or []
            applied = _with_display_index(entry.get("version_timeline", []), build_uid_index(chunks))
            if before != get_input_versions(manager):
                _refuse_unverified_timeline_chunks(None)
        result = build_suggestions(script, chunks, voice_config, name, entry, applied)
        result["chunks_revision"] = _get_timeline_chunks_revision(chunks)
        result["timeline_revision"] = _get_version_timeline_revision(entry)
        result["chunk_count"] = len(chunks)
        _validate_timeline_voice_document(result)
        return result

    def build_suggestions(script, chunks, voice_config, name, entry, applied):
        # One pass, exact speaker identity: the same targets every state route
        # and the validator use (C14: "Lee" and "LEE" are different people).
        state_targets = get_persona_state_targets(script)
        targets = state_targets.get(name.strip(), [])
        states = [{key: target[key] for key in ("from_entry", "gender", "age_group")} for target in targets]
        if not states:
            return {"speaker": speaker, "states": [], "applied": applied}
        strict_mapping = get_persona_state_chunk_indices(script, chunks, name, state_targets=state_targets)
        targets_by_entry = {target["from_entry"]: target for target in targets}
        try:
            candidates = get_state_voice_candidates()
        except (OSError, ValueError) as exc:
            _refuse_timeline_voice_read(exc)
        users = get_adapter_users(voice_config, candidates)
        # Positional mapping applies to every source, including manual/library
        # versions; repeated text cannot identify a state boundary safely.
        out = []
        for state in states:
            line = script[state["from_entry"]]
            target = targets_by_entry.get(state["from_entry"])
            has_generated = target and target["version_id"] in (entry.get("versions") or {})
            chunk_index = strict_mapping.get(state["from_entry"])
            sources = get_state_voice_sources(state, name, entry, candidates, users)
            sources["versions"] = [version for version in sources["versions"]
                if "persona_state" not in entry["versions"][version["version_id"]]
                or (target and version["version_id"] == target["version_id"]
                    and is_state_version_current(entry["versions"][version["version_id"]], target))]
            sources["offer_generate"] = not any(sources[key] for key in ("versions", "library_unused", "library_used"))
            if has_generated:
                sources["versions"].sort(key=lambda version: version["version_id"] != target["version_id"])
            out.append({**state, "possible_gender_reveal": bool(out and is_possible_gender_reveal(out[-1], state)),
                        "from_index": chunk_index,
                        "uid": get_anchor_uid(chunks, chunk_index) if chunk_index is not None else None,
                        "chapter": get_chapter_label(script, state["from_entry"]),
                        "line": (line.get("text") or "")[:120],
                        "sources": sources, "state_version": target["version_id"] if target else None})
        return {"speaker": speaker, "states": out, "applied": applied,
                "chunks_built": bool(chunks)}
    return await asyncio.to_thread(build)


class VersionTimelineRequest(BaseModel):
    book_token: Optional[str] = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    expected_chunks_revision: Optional[str] = Field(default=None, pattern=BOOK_TOKEN_PATTERN)
    expected_timeline_revision: Optional[str] = Field(default=None, pattern=BOOK_TOKEN_PATTERN)
    points: Optional[List[Optional[VoiceVersionPoint]]]


def _get_version_timeline_revision(entry):
    return get_snapshot_digest(entry.get("version_timeline", []))


def _require_version_timeline_capacity(points, stored, *, new=0):
    if len(points) > max(50, len(stored or [])) or new > 50:
        raise AppError(422, 'A voice timeline can contain up to 50 new change points.',
                       key='error.voice.timeline_point_limit')


@router.post("/api/voices/{speaker}/version_timeline")
async def save_version_timeline(speaker: str, request: VersionTimelineRequest):
    """Apply a character's voice timeline. Every version must exist.

    An empty list for an unconfigured speaker is a no-op when
    expected_timeline_revision matches the State snapshot. Without that
    revision the request returns 422. A stale revision returns 409.
    """
    def save(current, chunks):
        if (request.expected_chunks_revision is not None
                and request.expected_chunks_revision != _get_timeline_chunks_revision(chunks or [])):
            _refuse_timeline_write_conflict()
        stored = _get_replayable_version_timeline(current)
        if (request.expected_timeline_revision is not None
                and request.expected_timeline_revision != _get_version_timeline_revision(current)):
            _refuse_timeline_write_conflict()
        if request.points is None:
            if "version_timeline" in current and current["version_timeline"] is None:
                raise _NoOpEcho(None)
            _refuse_timeline_write_conflict()
        _require_version_timeline_capacity(request.points, stored)
        available = _SavedTimelinePoints(stored, "version_timeline")
        submissions = [
            (point.model_dump(exclude_unset=True), _request_anchor_uid(point),
             not (point.model_extra.keys() - {"from_uid", *TIMELINE_VIEW_FIELDS}))
            if point is not None else (None, None, False)
            for point in request.points]
        matches = available.match(submissions)
        _require_version_timeline_capacity(request.points, stored, new=sum(
            point is not None and match is None for point, match in zip(request.points, matches)))
        records = []
        for (submitted, request_uid, _), exact in zip(submissions, matches):
            if submitted is None or exact is not None:
                entry = exact
            else:
                index, uid = validate_write_anchor(chunks, get_style_timeline_index(submitted), request_uid)
                previous = available.take_replacement(submitted, request_uid)
                entry = {**_get_storable_timeline_fields(previous or {}),
                         **_get_storable_timeline_fields(submitted),
                         "version_id": submitted.get("version_id"), "from_index": index}
                if uid is None and chunks is not None and 0 <= index < len(chunks):
                    uid = _ensure_timeline_uid(chunks[index])
                if uid:
                    entry["from_uid"] = uid
            records.append(entry)
        if records == stored and get_snapshot_digest(records) == get_snapshot_digest(stored):
            raise _NoOpEcho(current.get("version_timeline", []))
        if not stored:
            uid_index = build_uid_index(chunks)
            ordered = iter(sorted((point for point in records if point is not None),
                                  key=lambda point: resolve_timeline_index(point, uid_index)))
            records = [next(ordered) if point is not None else None for point in records]
        get_validated_voice_changes(speaker, current, {**current, "version_timeline": records},
                                    check_timeline=True, timeline_chunks=chunks or [])
        current["version_timeline"] = records
    replay_unconfigured = request.points == [] and request.expected_timeline_revision is not None
    try:
        entry, _revision = await asyncio.to_thread(
            _mutate_book_voice_entry, speaker,
            _TimelineMutation(save, replay_unconfigured=replay_unconfigured), request.book_token)
    except _NoOpEcho as echo:
        return {"status": "saved", "speaker": speaker, "version_timeline": echo.points}
    except ValueError as exc:
        _refuse_timeline_write_conflict(exc)
    return {"status": "saved", "speaker": speaker, "version_timeline": entry.get("version_timeline", [])}

@router.delete("/api/voices/{speaker}/version_timeline")
async def clear_version_timeline(
        speaker: str,
        expected_timeline_revision: Annotated[str, Query(
            pattern=BOOK_TOKEN_PATTERN, description="Current timeline_revision from the State snapshot.")],
        book_token: Optional[str] = None):
    """Clear a voice timeline using the required timeline revision from the State snapshot."""
    def clear(current, chunks):
        if expected_timeline_revision != _get_version_timeline_revision(current):
            _refuse_timeline_write_conflict()
        if "version_timeline" not in current:
            raise _NoOpEcho()
        current.pop("version_timeline", None)
    try:
        await asyncio.to_thread(_mutate_book_voice_entry, speaker, _TimelineMutation(clear), book_token)
    except _NoOpEcho:
        pass
    return {"status": "cleared", "speaker": speaker}


@router.delete("/api/voices/{speaker}/style_timeline/{from_index}")
async def remove_style_point(speaker: str, from_index: int = Path(ge=0, le=2**53 - 1), from_uid: Optional[str] = None,
                             book_token: Optional[str] = None, legacy_only: bool = False,
                             delete_key: str | None = None):
    """Remove a style change by live index or by its exact saved badge identity."""
    try:
        uid = _normalize_anchor_uid(from_uid)
    except ValueError as exc:
        _refuse_invalid_timeline_uid(exc)
    display = None
    def remove(current, chunks):
        nonlocal display
        points = _get_stored_timeline(current, "style_timeline")
        uid_index = build_uid_index(chunks)
        current_uid = get_anchor_uid(chunks, from_index) if uid is None and not legacy_only else None
        if current_uid not in uid_index:
            current_uid = None
        if delete_key is not None:
            revision = get_snapshot_digest(points)
            selected = next((i for i, point in enumerate(points) if isinstance(point, dict)
                             and delete_key == f"{revision}:{i}"
                             and point.get("from_uid") == uid
                             and get_style_timeline_index(point) == from_index), None)
            if selected is None:
                _refuse_timeline_write_conflict()
            kept = points[:selected] + points[selected + 1:]
        else:
            targeted = [point for point in points if isinstance(point, dict)
                        and point.get("from_uid") == uid
                        and get_style_timeline_index(point) == from_index]
            if (len(targeted) > 1 and (legacy_only or uid is not None
                    or len({json.dumps(point.get("from_index", 0)) for point in targeted}) > 1)):
                _refuse_timeline_write_conflict()
            kept = delete_style_points(points, from_index, uid, current_uid=current_uid)
        display = _with_display_index(kept, uid_index)
        if kept == points:
            raise _NoOpEcho(display)
        current["style_timeline"] = kept
    try:
        entry, _revision = await asyncio.to_thread(
            _mutate_book_voice_entry, speaker, _TimelineMutation(remove), book_token)
    except _NoOpEcho as echo:
        display = echo.points
    else:
        if display is None:
            display = entry.get("style_timeline", [])
    return {"status": "saved", "speaker": speaker, "style_timeline": display}


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
    if request.design_count is not None and request.ref_text is None:
        raise HTTPException(status_code=422, detail="A design count needs a chosen line")
    if request.ref_text is not None:
        await asyncio.to_thread(require_chosen_design_line, request)
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
    if (request.advanced or request.state_version) and request.ref_text is None:
        batch_size = max(1, min(int(request.batch_size or 40), 200))
        command.extend(["--advanced", "--batch-size", str(batch_size)])
    if request.new_only:
        command.append("--new-only")
    if request.ref_text is not None:
        # One argv element, so a line starting with "-" is never read as an option.
        command.append("--ref-text=" + request.ref_text)
        if (request.design_count or 1) > 1:
            command.extend(["--design-count", str(request.design_count)])
    schedule_claimed_background_task(background_tasks, "persona", run_process, command, "persona")
    # Chosen-line design skips the LLM, including any unrelated manual request.
    process_state["persona"]["accepts_manual_requests"] = request.ref_text is None
    return {"status": "started", "advanced": bool((request.advanced or request.state_version) and request.ref_text is None)}


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
            def update(current_config):
                config = copy.deepcopy(current_config)
                # Same canonical-null/malformed-entry seam as the shared
                # mutator: explicit null is unconfigured (clear 422, no
                # crash/fabrication); absent keys keep create-{} behavior.
                _stored_base = config.get(request.speaker)
                if request.speaker in config and _stored_base is None:
                    raise HTTPException(status_code=422, detail=(
                        f"Speaker '{request.speaker}' has no voice configured; "
                        "assign a voice before recovering a persona for it."))
                if _stored_base is not None and not isinstance(_stored_base, dict):
                    raise HTTPException(status_code=422, detail=(
                        f"Speaker '{request.speaker}' has a malformed voice entry (not an object). "
                        "Restore the saved voice configuration before continuing."))
                base = config.get(request.speaker, {})
                _require_versions_dict(request.speaker, base)
                before = copy.deepcopy(base)
                if request.state_version:
                    target = get_current_persona_state(request.speaker, request.state_version)
                    entry = copy.deepcopy(_require_versions_dict(request.speaker, base).get(request.state_version) or {})
                    entry.update(get_state_version_identity(target))
                    entry["persona_status"] = "generated"
                else:
                    entry = base
                entry.update({"description": description, "character_style": description,
                              "ref_text": ref_text})
                if not entry.get("type"):
                    entry["type"] = "design"
                if request.state_version:
                    _recovered_versions = _require_versions_dict(request.speaker, base)
                    _recovered_versions[request.state_version] = entry
                    base["versions"] = _recovered_versions
                get_validated_voice_changes(request.speaker, before, base,
                                            trusted_states={request.state_version} if request.state_version else ())
                config[request.speaker] = base
                return config
            _apply_book_voice_update(update)

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
            process_state["persona"]["accepts_manual_requests"] = True
    except BaseException:
        if claim_id is not None:
            release_gpu_task_claim("persona", claim_id, pending_only=True)
        raise
    return {"status": "resuming" if request.resume else "saved", "speaker": request.speaker}

def _voice_adapter_identity_same(old_type, old_adapter_id, new_type, new_adapter_id):
    """Whether two LoRA identities name the same physical adapter (DS3031).

    Identity is the type plus the manifest-resolved adapter id, never a
    raw adapter_path string: relative/absolute spellings and catalog
    moves of the same id are the same asset, and a manifest rename
    (previous_ids) resolves both spellings to one current id through the
    existing voice_manifest seam. If identity equivalence cannot be
    established (manifest/admission/data/I/O failure), fail CLOSED by
    refusing the write (409) — never assume identity equality (which
    would persist a stale pin on a new adapter) and never clear/unpin on
    unknown lookup. Only a genuine replacement with a different resolved
    identity, or a type change, is a different asset. Unexpected
    programmer errors propagate unchanged.
    """
    if (old_type or "custom") != (new_type or "custom"):
        return False
    if not old_adapter_id or not new_adapter_id:
        return False
    if os.path.normcase(str(old_adapter_id)) == os.path.normcase(str(new_adapter_id)):
        return True
    try:
        resolved = get_resolved_adapter_id_mapping(
            LORA_MODELS_DIR, [str(old_adapter_id), str(new_adapter_id)])
    except (ValueError, OSError) as exc:
        raise AppError(409, 'Adapter identity could not be verified; restore the adapter manifest or reload voices before saving.', key='safety.voice.adapter_identity_unverified') from exc
    return (os.path.normcase(str(resolved[str(old_adapter_id)]))
            == os.path.normcase(str(resolved[str(new_adapter_id)])))


def _request_anchor_uid(request):
    try:
        return _normalize_anchor_uid(getattr(request, "from_uid", None))
    except ValueError as exc:
        _refuse_invalid_timeline_uid(exc)


def _refuse_invalid_timeline_uid(cause):
    raise AppError(400, 'The timeline line identity is invalid. Reload Editor and select the line again.',
                   key='error.voice.timeline_invalid_uid') from cause


def _refuse_timeline_write_conflict(cause=None):
    raise AppError(409, 'The change point could not be saved. Reload voices and Editor, then try again.',
                   key='error.voice.timeline_write_conflict') from cause


def _refuse_invalid_timeline_voice_document(cause=None):
    raise AppError(400, 'Saved voice data is invalid. Restore the voice configuration and reload voices.',
                   key='error.voice.timeline_invalid_voice_data') from cause


def _refuse_timeline_voice_read(cause):
    raise AppError(409, 'Active voices could not be read.',
                   key='error.voice.timeline_voice_read') from cause


def _refuse_guarded_timeline_anchor():
    raise AppError(400, 'A change point no longer matches its line. Reload Editor and select it again.',
                   key='error.voice.timeline_guarded_anchor')


def _refuse_new_null_timeline_point():
    raise AppError(400, 'New timeline points must be objects.',
                   key='error.voice.timeline_point_object')


def _get_stored_timeline(entry, key):
    """Admit an editable timeline without discarding malformed legacy history."""
    points = entry.get(key)
    if points is None:
        return []
    field = "character_style" if key == "style_timeline" else "version_id"
    try:
        if not isinstance(points, list):
            raise ValueError
        for point in points:
            if point is None:
                continue
            if (not isinstance(point, dict)
                    or (point.get(field) is not None and not isinstance(point[field], str))):
                raise ValueError
            get_style_timeline_index(point)
    except ValueError as exc:
        raise AppError(409, 'The saved timeline is malformed. Restore the voice configuration before editing.',
                       key='error.voice.timeline_malformed') from exc
    return points


def _validate_timeline_voice_document(data):
    try:
        ensure_voice_config_storable(data)
    except ValueError as exc:
        _refuse_invalid_timeline_voice_document(exc)


def _validate_editable_timeline_document(data):
    _validate_timeline_voice_document(data)
    for name, entry in data.items():
        versions = _require_versions_dict(name, entry)
        if isinstance(entry, dict):
            _get_lora_adapter_id(entry, status_code=400)
            for version_id, version in versions.items():
                if isinstance(version, dict):
                    _get_lora_adapter_id(get_voice_config_for_version(entry, version_id), status_code=400)
            for key in TIMELINE_KEYS:
                _get_stored_timeline(entry, key)


def _get_timeline_voice_entry(data, speaker):
    entry = data.get(speaker)
    if entry is not None and not isinstance(entry, dict):
        _refuse_invalid_timeline_voice_document()
    return entry


def _get_replayable_version_timeline(entry):
    stored = _get_stored_timeline(entry, "version_timeline")
    try:
        for point in stored:
            if point is not None:
                VoiceVersionPoint.model_validate(point)
                _normalize_anchor_uid(point.get("from_uid"))
    except (ValidationError, ValueError) as exc:
        _refuse_invalid_timeline_voice_document(exc)
    return stored


def _refuse_unverified_timeline_chunks(cause):
    raise AppError(409, 'Active chunks could not be verified. Restore the book state or reload Editor.',
                   key='error.voice.timeline_chunks_unverified') from cause


def _get_timeline_chunks_revision(chunks):
    try:
        return get_snapshot_digest(chunks)
    except (ValueError, RecursionError) as exc:
        _refuse_unverified_timeline_chunks(exc)


def _get_authoritative_chunks_locked(manager):
    """Read the snapshot and any retained journal. Only an absent pair is legacy."""
    try:
        rows = manager._get_saved_chunk_rows_locked()
        for row in rows:
            uid = row.get("uid")
            if uid is not None and uid != "":
                _normalize_anchor_uid(uid)
        return rows
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        _refuse_unverified_timeline_chunks(exc)


def _ensure_timeline_uid(row):
    uid = row.get("uid")
    if not isinstance(uid, str) or not uid:
        uid = row["uid"] = _new_chunk_uid()
    return uid


class _NoOpEcho(Exception):
    def __init__(self, points=None):
        self.points = points


def _get_matching_point(points, submitted, field):
    """Match a dedicated edit while preserving extension data in the stored point."""
    uid = submitted.get("from_uid")
    index = get_style_timeline_index(submitted)
    for point in reversed(points):
        if not isinstance(point, dict) or point.get("from_uid") != uid:
            continue
        try:
            same_index = get_style_timeline_index(point) == index
        except ValueError:
            continue
        value = point.get(field)
        if field == "character_style" and isinstance(value, str):
            value = value.strip()
        if same_index and value == submitted[field]:
            return point
    return None


def _get_storable_timeline_fields(point):
    return {key: value for key, value in point.items() if key not in TIMELINE_VIEW_FIELDS}


def _get_timeline_browser_value(value):
    """Project numbers whose JSON representation is lost in the browser."""
    if type(value) is int and abs(value) > 2**53 - 1:
        return str(value)
    if type(value) is float and value.is_integer():
        return str(int(value))
    if isinstance(value, list):
        return [_get_timeline_browser_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _get_timeline_browser_value(item) for key, item in value.items()}
    return value


def _with_display_index(points, uid_index, *, style_badges=False):
    """Expose display positions and references to records that need lossless replay."""
    if not isinstance(points, list):
        return points
    result = []
    revision = get_snapshot_digest(points) if style_badges else None
    for ordinal, point in enumerate(points):
        if not isinstance(point, dict):
            result.append(point)
            continue
        try:
            index = resolve_timeline_index(point, uid_index)
        except ValueError:
            index = point.get("from_index")
        raw = _get_storable_timeline_fields(point)
        view = _get_timeline_browser_value(raw)
        if view != raw:
            revision = revision or get_snapshot_digest(points)
            view["saved_point"] = f"{revision}:{ordinal}"
        view["display_index"] = _get_timeline_browser_value(index)
        if style_badges:
            view["delete_key"] = f"{revision}:{ordinal}"
        result.append(view)
    return result


class _SavedTimelinePoints:
    """Match each submitted point to at most one saved record in original order."""

    def __init__(self, points, kind):
        self.points = points if isinstance(points, list) else []
        self.available = dict(enumerate(self.points))
        self.kind = kind
        self.revision = None
        self.exact = defaultdict(deque)
        self.by_anchor = defaultdict(list)
        for ordinal, point in self.available.items():
            if point is None:
                self.exact[None].append(ordinal)
            elif isinstance(point, dict):
                uid = point.get("from_uid")
                if uid is None or isinstance(uid, str):
                    self.exact[canonical_point_payload(point, kind, exact=True)].append(ordinal)
                    self.by_anchor[(uid, get_style_timeline_index(point))].append(ordinal)

    def match(self, submissions):
        """Reserve exact records before compatibility matching or replacement."""
        matches = []
        for point, uid, _ in submissions:
            if point is not None and "saved_point" in point:
                matches.append(self.take_reference(point, uid))
                continue
            key = canonical_point_payload(point, self.kind, exact=True) if point is not None else None
            ordinals = self.exact.get(key)
            while ordinals and ordinals[0] not in self.available:
                ordinals.popleft()
            if point is None and not ordinals:
                _refuse_new_null_timeline_point()
            matches.append(self.available.pop(ordinals.popleft()) if ordinals else None)
        return [self.take_legacy_match(point, uid, minimal) if point is not None and match is None else match
                for (point, uid, minimal), match in zip(submissions, matches)]

    def get_unmatched_at_anchor(self, point, uid):
        return [ordinal for ordinal in self.by_anchor.get((uid, get_style_timeline_index(point)), ())
                if ordinal in self.available]

    def take_legacy_match(self, point, uid, minimal_version):
        spelling = get_timeline_index_spelling(point)
        payload = canonical_point_payload(point, self.kind)
        best = None
        for ordinal in self.get_unmatched_at_anchor(point, uid):
            saved = self.available[ordinal]
            exact_payload = canonical_point_payload(saved, self.kind) == payload
            if not exact_payload and (not minimal_version or saved.get("version_id") != point.get("version_id")):
                continue
            priority = (get_timeline_index_spelling(saved) != spelling, not exact_payload, ordinal)
            if best is None or priority < best:
                best = priority
        return self.available.pop(best[-1]) if best is not None else None

    def take_replacement(self, point, uid):
        """Retained records and other recorded indices never donate metadata."""
        candidates = self.get_unmatched_at_anchor(point, uid)
        spelling = get_timeline_index_spelling(point)
        exact = [i for i in candidates if get_timeline_index_spelling(self.available[i]) == spelling]
        candidates = exact or candidates
        return self.available.pop(candidates[0]) if len(candidates) == 1 else None

    def take_reference(self, point, uid):
        # A browser reference may recover the exact saved JSON, but cannot
        # authorize an edit, a different record, or a second use of a record.
        key = point["saved_point"]
        match = re.fullmatch(r"([0-9a-f]{64}):([0-9]{1,10})", key) if isinstance(key, str) else None
        if match is None:
            _refuse_timeline_write_conflict()
        ordinal = int(match[2])
        saved = self.available.get(ordinal)
        self.revision = self.revision or get_snapshot_digest(self.points)
        if self.revision != match[1] or not isinstance(saved, dict) or saved.get("from_uid") != uid:
            _refuse_timeline_write_conflict()
        projected = _get_timeline_browser_value(_get_storable_timeline_fields(saved))
        if (canonical_point_payload(projected, self.kind, exact=True)
                != canonical_point_payload(point, self.kind, exact=True)):
            _refuse_timeline_write_conflict()
        return self.available.pop(ordinal)


def _normalize_submitted_timeline(points, kind, stored, chunks, uid_index, ambiguous):
    """Preserve legacy records and authorize every new anchor against saved rows."""
    if points is None:
        return None
    if kind == "version_timeline":
        _require_version_timeline_capacity(points, stored)
    saved = _SavedTimelinePoints(stored, kind)
    submissions = []
    for item in points:
        try:
            uid = _normalize_anchor_uid(item.get("from_uid")) if item is not None else None
        except ValueError as exc:
            _refuse_invalid_timeline_uid(exc)
        submissions.append((item, uid, False))
    matches = saved.match(submissions)
    if kind == "version_timeline":
        _require_version_timeline_capacity(points, stored, new=sum(
            point is not None and match is None for point, match in zip(points, matches)))
    result = []
    for (item, uid, _), match in zip(submissions, matches):
        if item is None or match is not None:
            result.append(match)
            continue
        row_index = get_style_timeline_index(item)
        if uid in ambiguous:
            _refuse_guarded_timeline_anchor()
        if chunks is not None and not 0 <= row_index < len(chunks):
            _refuse_guarded_timeline_anchor()
        if uid is not None and uid_index.get(uid) != row_index:
            _refuse_guarded_timeline_anchor()
        if uid is None and chunks is not None:
            uid = get_anchor_uid(chunks, row_index)
            if uid in ambiguous:
                _refuse_guarded_timeline_anchor()
            uid = uid or _ensure_timeline_uid(chunks[row_index])
        point = _get_storable_timeline_fields(item)
        point.pop("from_uid", None)
        if kind == "style_timeline" or "from_index" not in point:
            point["from_index"] = row_index
        if kind == "style_timeline":
            point.setdefault("character_style", "")
        else:
            point.setdefault("version_id", None)
        if uid is not None:
            point["from_uid"] = uid
        result.append(point)
    return result


def _apply_voice_save(config_data, expected_revision=None, book_token=None):
    with ensure_book_state(os.path.dirname(VOICE_CONFIG_PATH)):
        if book_token is not None:
            snapshot = get_book_snapshot(os.path.dirname(VOICE_CONFIG_PATH),
                                         allow_missing_script=True, include_voices=False)
            if get_book_snapshot_token(snapshot) != book_token:
                raise VoiceConfigConflict("Active book changed; reload voices before saving")
        def apply_updates(current_config, chunks):
            uid_index = build_uid_index(chunks)
            ambiguous = ambiguous_uids(chunks)
            updated = dict(current_config)
            for voice_name, config in config_data.items():
                if config is None:
                    if voice_name not in current_config or current_config[voice_name] is not None:
                        raise AppError(400, 'Only an already unconfigured speaker can be saved as null.',
                                       key='error.voice.timeline_null_speaker')
                    continue
                existing = current_config.get(voice_name)
                # A present-but-non-object saved entry is malformed data:
                # refuse instead of silently replacing it with {}. The
                # canonical null (unconfigured) entry is the exception:
                # an explicitly confirmed/ordinary valid update may
                # initialize a null target safely.
                if voice_name in current_config and existing is not None and not isinstance(existing, dict):
                    raise ValueError(
                        f"Speaker '{voice_name}' has a malformed voice entry (not an object).")
                metadata = dict(existing) if isinstance(existing, dict) else {}
                # Canonical null is the unconfigured equivalent of {}:
                # a metadata-only update (e.g. ready) must not
                # materialize Pydantic defaults (custom/Ryan) the way a
                # full dump for an ABSENT key does. Explicit valid
                # proposals still initialize every field they set.
                fields = config.model_dump(exclude_unset=isinstance(existing, dict)
                                           or (existing is None and voice_name in current_config))
                for key in TIMELINE_KEYS:
                    if key in fields:
                        points = getattr(config, key)
                        if points is None and (key not in metadata or metadata[key] is not None):
                            raise AppError(400, 'Use an empty list to clear a voice timeline.',
                                           key='error.voice.timeline_null_clear')
                        fields[key] = _normalize_submitted_timeline(
                            [point.model_dump(exclude_unset=True) if point is not None else None
                             for point in points] if points is not None else None,
                            key, metadata.get(key), chunks, uid_index, ambiguous)
                candidate = {**metadata, **fields}
                # One canonical seam for the legacy alias key (#1724 C020):
                # whenever alias_of is explicitly provided (set or cleared),
                # the legacy fallback key is removed, because
                # tts.get_resolved_speaker would otherwise resurrect it the
                # moment alias_of becomes empty. Saves that do not mention
                # alias_of leave a legacy alias untouched.
                if 'alias_of' in fields:
                    candidate.pop('alias', None)
                _validate_consumed_description(voice_name, candidate)
                get_validated_voice_changes(voice_name, metadata, candidate,
                                            allow_state_removal=book_token is not None,
                                            timeline_chunks=chunks or [])
                # Adapter generation pin seam (#1724 DS030): explicit wins.
                # If the pin key is supplied, preserve that exact validated
                # P/null even when it equals the old pin (value equality is
                # NOT proof of a stale echo: the bulk path explicitly sends
                # source P, including when the target already holds P).
                # Only when the pin is omitted does the conservative rule
                # apply, and it uses genuine adapter identity (type +
                # manifest-resolved adapter id), never inactive voice/ref
                # fields and never a raw adapter_path string compare
                # (DS3031): same adapter keeps its pin across path
                # spellings/catalog moves; any genuine adapter replacement
                # or move to a non-LoRA type clears the old pin.
                if 'adapter_generation_sha256' not in fields:
                    new_type = fields.get("type", metadata.get("type")) or "custom"
                    old_type = metadata.get("type") or "custom"
                    new_adapter = fields.get("adapter_id", metadata.get("adapter_id")) or None
                    old_adapter = metadata.get("adapter_id") or None
                    if not _voice_adapter_identity_same(
                            old_type, old_adapter, new_type, new_adapter):
                        # Only clear a pin the metadata actually held:
                        # materializing None where no pin ever existed is
                        # unwanted metadata noise (explicit/omitted-pin
                        # semantics above are unchanged).
                        if "adapter_generation_sha256" in metadata:
                            candidate["adapter_generation_sha256"] = None
                updated[voice_name] = candidate
            if updated == current_config and get_voice_config_revision(updated) == get_voice_config_revision(current_config):
                raise _NoOpEcho()
            return updated

        try:
            updated = _apply_book_voice_update(apply_updates, expected_revision=expected_revision,
                                               timeline_edit=True)
        except _NoOpEcho:
            return {"status": "saved", "revision": expected_revision, "book_token": book_token}
        return {"status": "saved", "revision": get_voice_config_revision(updated),
                "book_token": book_token}


@router.post("/api/save_voice_config")
async def save_voice_config(config_data: Dict[str, Optional[VoiceConfigItem]],
                            revision: Optional[str] = None, book_token: Optional[str] = None):
    """Accept legacy raw maps only with explicitly supplied snapshot query guards."""
    if not revision or not book_token:
        raise HTTPException(status_code=409, detail=(
            "Legacy voice saves require revision and book_token query parameters from "
            "/api/voice_config/snapshot; migrate to /api/voice_config/save with those guards."))
    try:
        await asyncio.to_thread(_apply_voice_save, config_data, revision, book_token)
    except VoiceConfigConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TimeoutError:
        raise AppError(503, 'Voice config is busy (locked by another operation); please try again.', key='error.voice.config_busy')
    return {"status": "saved"}


class GuardedVoiceSaveRequest(BaseModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    book_token: str = Field(pattern=r"^[0-9a-f]{64}$")
    voices: Dict[str, Optional[VoiceConfigItem]]


def _ensure_voice_snapshot():
    from contextlib import ExitStack
    manager = _get_book_voice_manager(VOICE_CONFIG_PATH)
    with ExitStack() as stack:
        _enter_read_admission(stack, lambda: ensure_book_state(os.path.dirname(VOICE_CONFIG_PATH)),
                              lambda: manager._chunks_lock, lambda: file_lock(manager.chunks_path),
                              lambda: file_lock(VOICE_CONFIG_PATH))
        # Only the structured book-data reads are mapped: get_book_snapshot
        # raises ValueError (incl. JSONDecodeError) for malformed top-level
        # book/voice JSON, and the script bytes are parsed here. A malformed
        # top-level map must be a clear 400, never 500. Other ValueErrors
        # downstream are programmer errors and stay visible (not caught).
        try:
            snapshot = get_book_snapshot(os.path.dirname(VOICE_CONFIG_PATH),
                                         allow_missing_script=True, include_voices=False)
            config = read_voice_config(VOICE_CONFIG_PATH)
            script = json.loads(snapshot["script_bytes"]) if snapshot["script_bytes"] is not None else []
            if not isinstance(script, list):
                raise ValueError("Active script must contain an entry list")
        except (ValueError, RecursionError) as exc:
            raise HTTPException(status_code=400, detail=(
                "Active book data is invalid and cannot be read. "
                "Restore the book state or reload the script, then try again.")) from exc
        except OSError as exc:
            raise HTTPException(status_code=409, detail=(
                "Active book could not be verified while reading voices. "
                "Restore the book state or try again.")) from exc
        # Revision/token/identity/rows are also intended snapshot reads:
        # nonfinite/corrupt book data (NaN/Infinity via plain loads) makes
        # the digest helpers raise ValueError, which must be an honest
        # 400, never 500. The try is narrow to these calls only.
        try:
            ensure_voice_config_storable(config)
            revision = get_voice_config_revision(config)
            book_token = get_book_snapshot_token(snapshot)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=(
                "Active book data is invalid and cannot be read. "
                f"{exc} Restore the book state or reload the script, then try again.")) from exc
        # Operation-token derivation can raise ValueError for a transient
        # root/admission race (DS3085): that is a retryable 409, not a
        # corrupt-data 400. Narrow try only, mirroring the source preview.
        try:
            book_identity_token = get_book_operation_token(os.path.dirname(VOICE_CONFIG_PATH))
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=(
                "Active book could not be verified while reading voices. "
                "Restore the book state or try again.")) from exc
        except OSError as exc:
            raise HTTPException(status_code=409, detail=(
                "Active book could not be verified while reading voices. "
                "Restore the book state or try again.")) from exc
        try:
            voices = get_voice_rows(script, config)
            seed_changes = get_unseeded_voice_seed_changes(config)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=(
                "Active book data is invalid and cannot be read. "
                f"{exc} Restore the book state or reload the script, then try again.")) from exc
        display_uid_index = build_uid_index(_get_authoritative_chunks_locked(manager))
        for row in voices:
            if not isinstance(row, dict):
                continue
            row_config = row.get("config")
            if not isinstance(row_config, dict):
                continue
            row_config = row["config"] = dict(row_config)
            for key in TIMELINE_KEYS:
                if key in row_config:
                    row_config[key] = _with_display_index(
                        row_config[key], display_uid_index, style_badges=key == "style_timeline")
        display_config = copy.deepcopy(config)
        for data in display_config.values():
            if not isinstance(data, dict):
                continue
            for key in TIMELINE_KEYS:
                if key in data:
                    data[key] = _with_display_index(data[key], display_uid_index)
        return {"revision": revision,
                "book_token": book_token, "book_id": snapshot["book_id"], "config": display_config,
                "book_identity_token": book_identity_token,
                "voices": voices,
                "seed_changes": seed_changes}


@router.get("/api/voice_config/snapshot")
async def get_guarded_voice_snapshot():
    try:
        return await asyncio.to_thread(_ensure_voice_snapshot)
    except TimeoutError:
        raise AppError(503, 'Book or voices are busy; please try again.', key='error.voice.busy')


@router.post("/api/voice_config/save")
async def save_guarded_voice_config(request: GuardedVoiceSaveRequest):
    try:
        return await asyncio.to_thread(_apply_voice_save, request.voices,
                                       request.revision, request.book_token)
    except VoiceConfigConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except TimeoutError:
        raise AppError(503, 'Book or voices are busy; please try again.', key='error.voice.busy')


class SingleVoicePreviewRequest(BaseModel):
    # No length cap here: the canonical roster has no speaker-name
    # length limit, so a cap would refuse legitimate long names.
    # Membership in the loaded active roster + CAS stays authoritative.
    source_speaker: str = Field(min_length=1)
    revision: str = Field(pattern=BOOK_TOKEN_PATTERN)
    book_token: str = Field(pattern=BOOK_TOKEN_PATTERN)


def _direct_source_asset(source_speaker, source_config):
    """Return the asset fields of a direct base voice, or refuse clearly.

    An alias, ensemble or dynamically versioned/strategised entry is not
    one direct voice, so there is no hidden source voice to guess: the
    caller is directed to choose a direct base asset instead.
    """
    if not isinstance(source_config, dict):
        raise HTTPException(status_code=422, detail=(
            f"Speaker '{source_speaker}' has a malformed voice entry (not an object). "
            "Choose a speaker with a direct base voice, then use its voice for the book."))
    # Malformed non-string alias metadata (either key) refuses clearly
    # instead of bypassing the direct-asset alias refusal and being
    # silently cleared as a side effect. Canonical runtime alias
    # resolution (tts.get_resolved_speaker) is unchanged.
    for alias_key in ("alias_of", "alias"):
        alias_value = source_config.get(alias_key)
        if alias_value is not None and alias_value != "" and not isinstance(alias_value, str):
            raise HTTPException(status_code=422, detail=(
                f"Speaker '{source_speaker}' has malformed alias metadata (not text). "
                "Restore the saved voice configuration before applying it."))
    alias = source_config.get("alias_of") or source_config.get("alias")
    if isinstance(alias, str) and alias.strip():
        raise HTTPException(status_code=422, detail=(
            f"Speaker '{source_speaker}' is an alias of '{alias.strip()}'. "
            "Choose a speaker with a direct base voice, then use its voice for the book."))
    if source_config.get("active_version"):
        raise HTTPException(status_code=422, detail=(
            f"Speaker '{source_speaker}' speaks with a selected version. "
            "Choose a speaker whose base voice is direct, then use its voice for the book."))
    if source_config.get("version_timeline"):
        raise HTTPException(status_code=422, detail=(
            f"Speaker '{source_speaker}' has a version timeline. "
            "Choose a speaker whose base voice is direct, then use its voice for the book."))
    strategy = source_config.get("narrator_strategy")
    # Empty string is the legacy global spelling (summary normalizes
    # None/"global"/"" to the same effective strategy); support it
    # narrowly, keeping dynamic and malformed-type refusals.
    if strategy not in (None, "", "global"):
        raise HTTPException(status_code=422, detail=(
            f"Speaker '{source_speaker}' uses an active narrator strategy ('{strategy}'). "
            "Choose a speaker whose base voice is direct, then use its voice for the book."))
    voice_type = source_config.get("type") or "custom"
    if voice_type == "design":
        # A raw design description asks for a NEW voice; choosing an existing
        # asset must never create one implicitly. Saved designed voices are
        # stored as clone references (designed_voices/<file> + sample text)
        # via onDesignedVoiceSelect, so they arrive as type 'clone' above.
        raise HTTPException(status_code=422, detail=(
            f"Speaker '{source_speaker}' holds a Voice Design description, which would create a "
            "new voice. Choose a speaker whose saved asset is direct (custom, LoRA, clone or "
            "saved designed reference), then use its voice for the book."))
    if voice_category(source_config) == "ensemble":
        raise HTTPException(status_code=422, detail=(
            f"Speaker '{source_speaker}' is an ensemble. "
            "Choose a speaker with one direct base voice, then use its voice for the book."))
    def _text(key):
        value = source_config.get(key)
        return value if isinstance(value, str) else ""
    # Adapter generation pin: strict 64-lowercase-hex or null. A malformed
    # pin on the chosen source refuses with no write instead of silently
    # unpinning a pinned source or transferring garbage. The pin P is
    # transferred unchanged to targets; it is never recomputed from disk Q.
    source_pin = source_config.get("adapter_generation_sha256")
    if source_pin is not None and (not isinstance(source_pin, str)
            or not re.fullmatch(r"[0-9a-f]{64}", source_pin)):
        raise HTTPException(status_code=422, detail=(
            f"Speaker '{source_speaker}' has a malformed adapter generation pin; "
            "restore the saved voice configuration before applying it."))
    if voice_type == "custom":
        if not _text("voice").strip():
            raise HTTPException(status_code=422, detail=(
                f"Speaker '{source_speaker}' has no custom voice selected. "
                "Pick a voice on its card first, then use its voice for the book."))
        # No "description" in the patch: each target's own persona
        # description is preserved by the exclude_unset merge (#1724 C014).
        # A non-LoRA asset carries no adapter generation: explicit null
        # clears any stale target pin instead of preserving it by omission.
        return {"type": "custom", "voice": _text("voice"), "ref_audio": None, "ref_text": None,
                "adapter_id": None, "adapter_path": None, "members": None,
                "adapter_generation_sha256": None}
    if voice_type == "clone":
        if not _text("ref_audio").strip() or not _text("ref_text").strip():
            raise HTTPException(status_code=422, detail=(
                f"Speaker '{source_speaker}' is a clone voice without reference audio and text. "
                "Choose a complete direct base asset, then use its voice for the book."))
        return {"type": "clone", "voice": None, "ref_audio": _text("ref_audio"),
                "ref_text": _text("ref_text"), "adapter_id": None, "adapter_path": None,
                "members": None, "adapter_generation_sha256": None}
    if voice_type in ("lora", "builtin_lora"):
        if not _text("adapter_id").strip():
            raise HTTPException(status_code=422, detail=(
                f"Speaker '{source_speaker}' is a LoRA voice without an adapter identity. "
                "Choose a complete direct base asset, then use its voice for the book."))
        # Canonical representation: keep a stored path when present;
        # otherwise reuse the one fallback rule already owned by
        # get_library_voice_config (builtin_lora/{id} / lora_models/{id}).
        # TTS requires adapter_path (tts.get_lora_voice_request returns None
        # without it), so an adapter_id-only source must still resolve to a
        # renderable path; reusing the library helper keeps one formula (#1724 C068).
        # Source-local normalization: strip surrounding whitespace BEFORE
        # validation so ' . ' / ' .. ' are rejected as '.'/'..', and the
        # canonical stripped id is used for the fallback path/patch. The
        # shared validate_adapter_name semantics are unchanged (it returns
        # None; spaced folder identities elsewhere stay as they were).
        adapter_id = _text("adapter_id").strip()
        try:
            validate_adapter_name(adapter_id)
        except ValueError:
            raise HTTPException(status_code=422, detail=(
                f"Speaker '{source_speaker}' has an unsafe adapter identity. "
                "Choose a complete direct base asset, then use its voice for the book."))
        # Present adapter_path must be a nonblank string: whitespace-only
        # or non-string present values are corruption (422), never
        # silently replaced by the canonical fallback. Legacy absent /
        # None / empty keeps the canonical fallback. A legitimate
        # nonblank path keeps its EXACT spelling (never stripped).
        _raw_path = source_config.get("adapter_path")
        if _raw_path is not None and _raw_path != "":
            if not isinstance(_raw_path, str) or not _raw_path.strip():
                raise HTTPException(status_code=422, detail=(
                    f"Speaker '{source_speaker}' has a malformed adapter path; "
                    "restore the saved voice configuration before applying it."))
            adapter_path = _raw_path
        else:
            adapter_path = get_library_voice_config(
                {"type": voice_type, "adapter_id": adapter_id})["adapter_path"]
        # Transfer the chosen source's pin P unchanged to targets. Never
        # recompute it from current disk Q: TTS must still refuse Q vs P.
        # The normalized adapter_id is used for the fallback path and the
        # patch; the pin value itself is never altered by normalization.
        return {"type": voice_type, "voice": None, "ref_audio": None, "ref_text": None,
                "adapter_id": adapter_id, "adapter_path": adapter_path,
                "members": None, "adapter_generation_sha256": source_pin}
    raise HTTPException(status_code=422, detail=(
        f"Speaker '{source_speaker}' uses unsupported voice type '{voice_type}'. "
        "Choose a speaker with a direct base voice, then use its voice for the book."))


def _voice_asset_summary(config):
    data = config if isinstance(config, dict) else {}
    timeline = data.get("version_timeline")
    # Effective-alias COMPARISON uses the canonical nonblank-string
    # rule (DS3039): whitespace/non-string aliases are ignored by the
    # runtime, so they must not read as an audible voice change here.
    # The raw override-clearing disclosure in build_single_voice_proposal
    # still inspects the stored keys directly; stored bytes are untouched.
    _raw_alias = data.get("alias_of") or data.get("alias")
    _effective_alias = _raw_alias if isinstance(_raw_alias, str) and _raw_alias.strip() else None
    return {"type": data.get("type") or "custom", "voice": data.get("voice"),
            "ref_audio": data.get("ref_audio"), "ref_text": data.get("ref_text"),
            "adapter_id": data.get("adapter_id"), "adapter_path": data.get("adapter_path"),
            "adapter_generation_sha256": data.get("adapter_generation_sha256"),
            "members": data.get("members"),
            "alias_of": _effective_alias,
            "alias": _effective_alias, "active_version": data.get("active_version"),
            # A malformed (non-list) timeline passes through untouched so the
            # change detection sees it instead of a list of characters (#1724 C045).
            "version_timeline": list(timeline) if isinstance(timeline, list)
            else ([] if timeline is None else timeline),
            # None and "global" are the same effective strategy (#1724 C007).
            "narrator_strategy": data.get("narrator_strategy") or "global"}


def get_masking_voice_overrides(character, entry):
    """-> the settings on `entry` that would keep a voice other than its base
    asset effective, or None when the base asset is what renders (#1800, #2015).

    THE one answer for both apply paths (Rule 15): this project's single-voice
    apply (build_single_voice_proposal) and a voice-suggestion apply
    (_apply_voice_suggestions). It reads the effective values through
    _voice_asset_summary, so an alias the runtime ignores (blank/non-string) is
    not a mask (DS3039). Keys are present only when they are set:

      alias_of          the EFFECTIVE alias (alias_of or the legacy alias)
      version_timeline  a per-chunk version timeline (any stored shape)
      narrator_strategy the strategy when it is not "global"; it masks because
                        tts.resolve_narrator_voice_config routes a narration
                        chunk with focus_speaker to the focus voice (#2015)

    Not masks, by design — each is a marker or history, not another voice:

      active_version   only `version_timeline` is read at render time
                       (tts.active_version_id), and selecting a version bakes
                       its fields into the entry itself
                       (select_voice_version's entry.update), so the key alone
                       changes nothing audible
      active_candidate a UI marker with no audible effect of its own
      ready            approval state
      versions         history the runtime reaches only through the keys above

    build_single_voice_proposal still DISCLOSES those keys being cleared (its
    patch clears them); that is a statement about the stored bytes, not about
    masking, so it stays beside this verdict rather than inside it. Pure: the
    caller's entry is never mutated, a new dict is built (Rule 17).
    """
    summary = _voice_asset_summary(entry)
    overrides = {"character": character, "alias_of": summary["alias_of"],
                 "version_timeline": bool(summary["version_timeline"])}
    if summary["narrator_strategy"] != "global":
        overrides["narrator_strategy"] = summary["narrator_strategy"]
    if not (overrides["alias_of"] or overrides["version_timeline"]
            or overrides.get("narrator_strategy")):
        return None
    return overrides


def get_masking_override_changes(overrides):
    """-> the clearing disclosures for a get_masking_voice_overrides verdict.

    The sentences are the single-voice apply's own, reused by the suggestion
    apply so a reset is reported with one wording everywhere (Rule 15).
    """
    overrides = overrides or {}
    changes = []
    if overrides.get("alias_of"):
        changes.append("alias cleared")
    if overrides.get("version_timeline"):
        changes.append("version timeline/active version cleared")
    if overrides.get("narrator_strategy"):
        changes.append("narrator strategy reset to global")
    return changes


def build_single_voice_proposal(voices, script, source_speaker):
    """Build patch-only proposals for the ACTIVE script speakers, read-only.

    Pure with respect to its inputs: the caller owns the book snapshot
    lock and revision/token guards. Returns one patch per active speaker
    (asset fields, explicit override clears, ready=False) so the client
    can apply exactly the previewed scope through the ordinary guarded
    save queue. Unrelated metadata (seeds, styles, versions, persona,
    inactive/legacy entries) is never sent: the server's exclude_unset
    merge preserves it, and untouched entries are not revalidated.
    Readiness is cleared for ALL affected speakers on a confirmed apply,
    because clearing an active version/timeline/strategy can change the
    effective voice even when the base asset already matches.
    """
    if not isinstance(script, list):
        raise HTTPException(status_code=400, detail="Active script must contain an entry list.")
    if any(not isinstance(entry, dict) for entry in script):
        raise HTTPException(status_code=400, detail="Active script data is corrupt (non-object entry).")
    if not isinstance(voices, dict):
        raise HTTPException(status_code=400, detail="Saved voice configuration must be an object.")
    # A non-dict source entry is a clear 422 before the roster build touches
    # it (get_voice_rows/voice_is_set assume dict entries) (#1724 C002).
    # Canonical null (unconfigured) is NOT malformed: it falls through
    # to the direct-asset unset handling ("has no custom voice
    # selected"). Only non-null non-object shapes refuse as malformed.
    if source_speaker in voices and voices[source_speaker] is not None and not isinstance(voices[source_speaker], dict):
        raise HTTPException(status_code=422, detail=(
            f"Speaker '{source_speaker}' has a malformed voice entry (not an object). "
            "Choose a speaker with a direct base voice, then use its voice for the book."))
    # Malformed ACTIVE entries refuse clearly; missing/unconfigured active
    # speakers stay {}. Inactive legacy entries are never validated here.
    # get_voice_rows is a domain seam (ValueError); this HTTP proposal
    # facade maps it to a clear 400, while compound callers of the same
    # seam keep their unavailable/partial contract.
    try:
        rows = get_voice_rows(script, voices)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    # No second active-entry loop: get_voice_rows already refused every
    # present malformed active entry before returning rows, so a loop
    # here could never trigger. Source-specific 422 above is distinct
    # (source may be inactive/outside roster) and is preserved.
    names = [row["name"] for row in rows]
    if source_speaker not in names:
        raise HTTPException(status_code=404, detail=f"Speaker '{source_speaker}' is not in the active script.")
    source_config = voices.get(source_speaker)
    if source_config is None:
        source_config = {}
    asset = _direct_source_asset(source_speaker, source_config)
    patch_template = dict(asset)
    # alias_of: None is explicit in the patch, so the guarded save merge
    # clears BOTH alias keys at its one seam (#1724 C020) — the legacy
    # "alias" key is never sent as a writable field.
    patch_template.update({"alias_of": None, "version_timeline": [],
                           "active_version": None, "narrator_strategy": "global",
                           "active_candidate": None, "ready": False})
    # Per-speaker voice_status: only speakers whose effective voice or
    # active overrides change lose stale voice approval (reset to
    # unassigned). persona_status and persona/performance metadata
    # (descriptions, styles, seeds, versions/history) are never sent, so
    # the exclude_unset merge preserves them; same asset with no override
    # change keeps its approval. Unchanged asset != same performance, so
    # styles/seeds are NOT wiped to claim identical delivery.
    # The patch is identical for every speaker except this per-speaker
    # approval reset, which depends on that speaker's own before-state.
    VoiceConfigItem(**patch_template)
    # Amplification bound (CR2016/2075): the identical patch is copied once
    # per active speaker, so estimate the expanded serialized size BEFORE
    # building the per-speaker map. Reuses the existing scope voice-mapping
    # budget (MAX_SCOPE_VOICE_MAPPING_BYTES); oversize refuses with no
    # writes and no massive allocation/serialization.
    import project_scope_voices
    # Maximum actual patch includes the optional per-speaker
    # voice_status='unassigned' augmentation, plus key/separator overhead
    # per speaker, so a near-budget map cannot exceed the reused budget
    # after augmentation.
    _max_patch = {**patch_template, "voice_status": "unassigned"}
    _patch_bytes = len(json.dumps(_max_patch, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    _expanded = _patch_bytes * len(rows)
    for _row in rows:
        _expanded += len(json.dumps(_row["name"], ensure_ascii=False, allow_nan=False).encode('utf-8')) + 16
    if _expanded > project_scope_voices.MAX_SCOPE_VOICE_MAPPING_BYTES:
        raise HTTPException(status_code=422, detail=(
            "The selected voice is too large to copy to every speaker in this book. "
            "Choose a speaker with a compact direct voice, then use its voice for the book."))
    proposed = {}
    speakers = []
    after_summary = _voice_asset_summary(patch_template)
    for row in rows:
        name = row["name"]
        before = voices.get(name)
        patch = dict(patch_template)
        changes = []
        before_summary = _voice_asset_summary(before)
        # Persona description is preserved per target, not part of the
        # applied asset, and ready is detected separately below: neither
        # is part of the "voice changed" comparison.
        voice_changed = before_summary != after_summary
        if voice_changed:
            changes.append("voice changed")
        # The patch clears these stored keys, so clearing them is disclosed
        # even where the runtime ignores the value (a whitespace alias,
        # DS3039) or where the key is a marker rather than a mask (an
        # active_version alone). This is the stored-bytes view; the audible
        # verdict is the one shared helper below, and the approval reset and
        # the suggestion apply both read it (Rule 15).
        masking = get_masking_voice_overrides(name, before)
        if isinstance(before, dict) and (before.get("alias_of") or before.get("alias")):
            changes.append("alias cleared")
        if isinstance(before, dict) and (before.get("version_timeline") or before.get("active_version")):
            changes.append("version timeline/active version cleared")
        for change in get_masking_override_changes(masking):
            if change not in changes:
                changes.append(change)
        if isinstance(before, dict) and before.get("active_candidate"):
            # UI-only marker: the saved candidate no longer names the
            # applied voice. Disclosed in the preview; clearing it does
            # not reset voice approval (no audible change by itself).
            changes.append("active candidate cleared (saved candidates kept)")
        if isinstance(before, dict) and bool(before.get("ready")):
            changes.append("ready reset (needs new listening approval)")
        # Reset stale voice approval exactly where the effective voice or a
        # masking override changes; otherwise preserve (omit the key). An
        # alias the runtime ignores (whitespace/non-string) does not make
        # the effective voice change, even though its raw key is cleared
        # and disclosed above (DS3039) — which is why this is the shared
        # verdict, not "was anything disclosed".
        effective_changed = voice_changed or bool(masking)
        if effective_changed:
            patch["voice_status"] = "unassigned"
            if isinstance(before, dict) and before.get("voice_status"):
                changes.append("voice approval reset (needs new listening approval)")
        # Revalidate the per-speaker patch when the approval reset was added.
        if "voice_status" in patch:
            VoiceConfigItem(**patch)
        # Resulting-entry boundary: the patch is applied over the stored
        # entry by the guarded save merge, so refuse here (before any
        # proposal can back a save) when the RESULTING type would consume
        # a malformed legacy description — with no coercion/dropping of
        # persona metadata, and only for this relevant updated speaker.
        _merged_entry = {**(before if isinstance(before, dict) else {}), **patch}
        try:
            _validate_consumed_description(name, _merged_entry)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        # Never offer a proposal that the canonical guarded save would
        # refuse: run the same central change validation on the resulting
        # entry here (read-only; its HTTPException refusals propagate
        # with their own clear status, ValueError maps to 400).
        try:
            get_validated_voice_changes(name, before if isinstance(before, dict) else {}, _merged_entry)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        proposed[name] = patch
        # The client renders only name/line_count/changes; before/after
        # summaries and a top-level asset copy are dead payload (#1724 C096/C097).
        speakers.append({"name": name, "line_count": row.get("line_count", 0), "changes": changes})
    return {"source_speaker": source_speaker,
            "speakers": speakers,
            "affected_speakers": len(speakers),
            "affected_lines": sum(int(item["line_count"]) for item in speakers),
            "voices": proposed}


def _ensure_single_voice_preview(request):
    from contextlib import ExitStack
    with ExitStack() as stack:
        # Entry-stage admission failures (book state / voice lock) are
        # honest 409/503 refusals, not corrupt-data 400s; body-stage
        # corrupt JSON still reaches the route's 400 handler below.
        _enter_read_admission(stack, lambda: ensure_book_state(os.path.dirname(VOICE_CONFIG_PATH)),
                              lambda: file_lock(VOICE_CONFIG_PATH))
        snapshot = get_book_snapshot(os.path.dirname(VOICE_CONFIG_PATH), allow_missing_script=True)
        book_token = get_book_snapshot_token(snapshot)
        if book_token != request.book_token:
            raise VoiceConfigConflict("Active book changed; reload voices before previewing")
        # Narrow JSON seam only: deep stored script JSON is corrupt data
        # (ValueError -> route 400). A RecursionError from the proposal
        # builder below is a programmer failure and must propagate.
        try:
            script = json.loads(snapshot["script_bytes"]) if snapshot["script_bytes"] is not None else []
        except RecursionError as exc:
            raise ValueError("Active script JSON is too deeply nested") from exc
        config = snapshot["voices"]
        revision = get_voice_config_revision(config)
        if revision != request.revision:
            raise VoiceConfigConflict("Voice configuration changed; reload it before previewing")
        proposal = build_single_voice_proposal(config, script, request.source_speaker)
        # Echo the verified guards (recomputed values equal the request's
        # on success, so the echo is exact); no dead book_id payload.
        proposal.update({"book_token": book_token, "revision": revision})
        return proposal


@router.post("/api/voice_config/single-voice-preview")
async def preview_single_voice(request: SingleVoicePreviewRequest):
    try:
        return await asyncio.to_thread(_ensure_single_voice_preview, request)
    except VoiceConfigConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        # Corrupt script/voice JSON or book state (JSONDecodeError and
        # UnicodeDecodeError both subclass ValueError) is a clear refusal,
        # never an unhandled 500 (#1724 C004). Deep stored script JSON
        # is converted to ValueError at the narrow parse seam inside
        # _ensure_single_voice_preview; a RecursionError elsewhere
        # (e.g. in the proposal builder) is a programmer failure and
        # propagates instead of being masked as corrupt data.
        raise HTTPException(status_code=400, detail=(
            "Active book data is corrupt and cannot be previewed. "
            "Restore the book state or reload the script, then preview again.")) from exc
    except TimeoutError:
        raise AppError(503, 'Book or voices are busy; please try again.', key='error.voice.busy')
    except OSError as exc:
        # Filesystem admission/read failures (lock, state, voice files) are
        # honest 409 refusals matching the source-preview sibling, never
        # an unhandled 500. Narrow: only this preview route's read seam.
        raise HTTPException(status_code=409, detail=(
            "Active book could not be verified while previewing. "
            "Restore the book state or try again.")) from exc



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
        raise AppError(503, 'Book or voices are busy; please try again.', key='error.voice.busy')


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


def _build_lora_candidates(*, read_only=False):
    """Downloaded built-in + user-trained adapters with normalized fields for matching."""
    candidates = []
    builtins = _load_builtin_lora_manifest(refresh=False) if read_only else _load_builtin_lora_manifest()
    for m in builtins:
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
    def read_manifest(path):
        return _load_manifest(path, record_diagnostics=False) if read_only else _load_manifest(path)
    for m in get_adapter_manifest_rows(LORA_MODELS_DIR, LORA_MODELS_MANIFEST, read_manifest, read_only=read_only):
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


def get_voice_matching_profile(profile, config):
    """Add only user-saved vocal guidance, without altering character traits."""
    hint = config.get("voice_presentation")
    if not isinstance(hint, str) or not hint.strip():
        return profile
    return f"User-confirmed voice presentation: {hint.strip()[:500]}. {profile}"


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
    ordered_names = get_cast_importance_order(
        [name for name in samples if not requested or name in requested],
        {name: len(lines) for name, lines in samples.items()})
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
            "voice_matching_profile": get_voice_matching_profile(profile, cfg),
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
            "User-confirmed voice presentation guides vocal matching, not gender or age inference; a lower female voice remains female. "
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
        from llm_provider import get_effective_authorization
        full_cfg = load_app_config(CONFIG_PATH)
        llm_cfg = get_active_llm_config(full_cfg)
        status = get_current_status(
            full_cfg.get("llm_mode", "local"), llm_cfg.get("base_url", ""),
            model_name, (full_cfg.get("llm_remote_ssh") or "").strip(),
            use_cache=True, api_key=llm_cfg.get("api_key"),
            authorization=get_effective_authorization(llm_cfg), llm_config=llm_cfg)
        for start in range(0, len(character_items), 2):
            batch = character_items[start:start + 2]
            char_block = "\n\n".join(
                f'CHARACTER: {name}\nLines: {info["line_count"]} ({info["priority"]})\nCurrent trait estimate: gender={info["gender"]}, age={info["age_group"]}\nPersona/style and user-confirmed voice presentation: {(info["voice_matching_profile"] or "(none)")[:800]}\nSample lines:\n'
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


def _apply_voice_suggestions(suggestions: Dict[str, dict], cast_name: Optional[str],
                             clear_overrides: bool = False) -> dict:
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

        with file_lock(VOICE_LIBRARY_PATH):
            current_token = None
            if any(value.get("book_token") is not None for value in suggestions.values()):
                current_token = get_book_snapshot_token(get_book_snapshot(
                    os.path.dirname(SCRIPT_PATH), allow_missing_script=True, include_voices=False))
            lib = _load_voice_library(for_update=bool(cast_name))
            if cast_name and cast_name not in lib["casts"]:
                raise HTTPException(status_code=404, detail=f"Cast '{cast_name}' not found.")
            applied = []
            blocked = []
            cleared = []
            def update(current_config):
                voice_config = copy.deepcopy(current_config)
                usage = get_cast_adapter_usage(lib, cast_name, identities)
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
                    override = get_masking_voice_overrides(character, cfg)
                    if override:
                        if not clear_overrides:
                            blocked.append(override)
                            continue
                        # Confirmed: make the suggestion effective. Saved
                        # versions stay as history; only what selects
                        # another voice is cleared (the one shared verdict
                        # above). A narrator strategy is RESET, not dropped:
                        # "global" is the stored spelling the single-voice
                        # apply writes, and the reset is reported below with
                        # that path's own wording.
                        for key in ("alias_of", "alias", "version_timeline", "active_version"):
                            cfg.pop(key, None)
                        if override.get("narrator_strategy"):
                            cfg["narrator_strategy"] = "global"
                        cleared.append({"character": character,
                                        "changes": get_masking_override_changes(override)})
                    cfg.pop("active_candidate", None)
                    cfg.pop("active_version", None)
                    # DS3010: a genuine adapter replacement clears the
                    # unrelated old pin; the same adapter keeps it.
                    if not _voice_adapter_identity_same(
                            cfg.get("type") or "custom", cfg.get("adapter_id"),
                            candidate["type"], adapter_id):
                        cfg["adapter_generation_sha256"] = None
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
                # Validate the FINAL proposed library INSIDE the
                # transform, after lib-only values (e.g. an untyped
                # suggestion.priority) are constructed but before
                # _apply_book_voice_update invalidates chunks or any
                # publisher writes either file. The voice-map validator
                # cannot see library-only fields, so a nonfinite/deep
                # priority must refuse here as a clear 400 with every
                # file byte-identical.
                if blocked:
                    # Refuse before anything is written: the client confirms
                    # and retries with clear_overrides (#1800).
                    names = ", ".join(item["character"] for item in blocked)
                    raise HTTPException(status_code=409, detail={
                        "code": "voice_overrides", "overrides": blocked,
                        "message": (f"An alias, voice timeline or narrator strategy keeps another voice "
                                    f"in effect for {names}. "
                                    "Confirm to clear it so the suggested voice is used.")})
                if cast_name:
                    try:
                        ensure_voice_config_storable(lib)
                    except ValueError as exc:
                        raise HTTPException(status_code=400, detail=str(exc)) from exc
                return voice_config

            def publish_pair(updated, path):
                atomic_json_write_pair(updated, path, lib, VOICE_LIBRARY_PATH)

            _apply_book_voice_update(update, publish=publish_pair if cast_name else None)
    return {"applied": applied, "count": len(applied), "cleared_overrides": cleared,
            "cast": cast_name, "book_id": book_id,
            "adapter_usage": get_cast_adapter_usage(lib, cast_name, identities)}


@router.post("/api/suggest_voices/apply")
async def apply_voice_suggestion(request: VoiceSuggestionApplyRequest):
    return await asyncio.to_thread(
        _apply_voice_suggestions, {request.character: request.suggestion},
        (request.cast or "").strip() or None, request.clear_overrides)

@router.post("/api/suggest_voices/apply_bulk")
async def apply_voice_suggestions_bulk(request: VoiceSuggestionApplyBulkRequest):
    return await asyncio.to_thread(
        _apply_voice_suggestions, request.suggestions,
        (request.cast or "").strip() or None, request.clear_overrides)
