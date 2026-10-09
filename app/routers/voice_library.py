import asyncio
import contextlib
import difflib
import json
import logging
import os
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core import (
    LORA_MODELS_DIR,
    CAST_MAJOR_LINE_THRESHOLD,
    CHARACTER_ALIASES_PATH,
    SCRIPTS_DIR,
    SCRIPT_PATH,
    SHARED_DEFAULT_NAMES,
    VOICE_CONFIG_PATH,
    VOICE_LIBRARY_PATH,
    _get_saved_book_id,
    _load_voice_library,
    _make_library_entry,
    get_portable_voice_config,
    _norm_name,
    _script_line_counts,
    _warn_corrupted_json,
    add_known_label,
    get_active_book_id,
    get_cast_adapter_usage,
    get_cast_member_key,
    get_cast_storage_pool,
    get_member_labels,
    get_trait_assignment_metadata,
)
from voice_manifest import get_resolved_adapter_id_mapping
from book_state_transaction import ensure_book_state
from utils import atomic_json_write, file_lock, is_generic_speaker, safe_load_json, secure_filename


logger = logging.getLogger("AlexandriaUI")
router = APIRouter()


class CastCreateRequest(BaseModel):
    name: str

class LibrarySaveRequest(BaseModel):
    cast: str
    characters: List[str]                     # current-book character names to save into the cast
    shared: Optional[List[str]] = None        # subset to force into the shared (cross-series) pool
    cast_specific: Optional[List[str]] = None # subset to force into the cast even if normally shared (e.g. a different narrator)

class LibraryApplyRequest(BaseModel):
    cast: str
    mapping: Dict[str, str]                   # current character name -> library member key to apply

class CastMatchBulkRequest(BaseModel):
    name: str                                 # cast name
    script_names: List[str]                   # saved scripts to union-match against the cast

class LibraryApplyBulkRequest(BaseModel):
    cast: str
    mapping: Dict[str, str]                   # character name -> library member key
    script_names: List[str]                   # saved scripts to apply the mapping to


## ── Series Voice Library (cross-book cast) ──────────────────────

def _name_similarity(a: str, b: str) -> float:
    """Similarity in [0,1] combining sequence ratio and token overlap on normalized names."""
    na, nb = _norm_name(a), _norm_name(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    ta, tb = set(na.split()), set(nb.split())
    jaccard = len(ta & tb) / len(ta | tb) if (ta and tb) else 0.0
    # Containment bonus: "kenji" vs "kenji sato"
    contain = 1.0 if (ta and tb and (ta <= tb or tb <= ta)) else 0.0
    return max(ratio, jaccard, contain * 0.9)


def _mutate_voice_library(mutator, companion_paths=()):
    """Mutate and publish under the library lock and ordered companion locks."""
    try:
        with file_lock(VOICE_LIBRARY_PATH), contextlib.ExitStack() as locks:
            for path in companion_paths:
                locks.enter_context(file_lock(path))
            lib = _load_voice_library()
            result = mutator(lib)
            atomic_json_write(lib, VOICE_LIBRARY_PATH)
            return result
    except TimeoutError as e:
        logger.warning(f"Could not acquire lock to update voice library: {e}")
        raise


async def _mutate_voice_library_async(mutator):
    """Offload _mutate_voice_library to a worker thread so file_lock's wait loop
    can't block the event loop; turns lock-contention timeouts into a 503."""
    try:
        return await asyncio.to_thread(_mutate_voice_library, mutator)
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Voice library is busy (locked by another operation); please try again.")


def _load_character_aliases() -> dict:
    """The global alias registry ({"ALIAS": "CANONICAL"}), empty if absent."""
    aliases = safe_load_json(CHARACTER_ALIASES_PATH, default={})
    return aliases if isinstance(aliases, dict) else {}


def _cast_match_pool(lib: dict, cast_name: str, book_id: Optional[str] = None,
                     include_all_generic: bool = False,
                     aliases: Optional[dict] = None) -> dict:
    """Build the candidate pool for matching against a cast: shared first, cast
    members override on key collision (a cast-specific narrator beats the
    shared narrator = "different narrator"). Each candidate carries every
    label it is known as (name, remembered labels, registered aliases)."""
    aliases = _load_character_aliases() if aliases is None else aliases
    def candidate(k, m, source):
        entry = {"name": m.get("name", k), "known_as": m.get("known_as")}
        return {"key": k, "name": entry["name"], "source": source,
                "type": (m.get("config") or {}).get("type"),
                "known_as": add_known_label(entry["known_as"], entry["name"]),
                "labels": get_member_labels(entry, aliases)}
    pool = {}
    for k, m in lib["shared"].items():
        pool[k] = candidate(k, m, "shared")
    for k, m in lib["casts"][cast_name].get("members", {}).items():
        if m.get("generic") and not include_all_generic and m.get("book_id") != book_id:
            continue
        if is_generic_speaker(m.get("name", k)) and not m.get("book_id"):
            continue  # legacy ambiguous generic entry
        pool[k] = candidate(k, m, "cast")
    return pool


def _build_match_proposals(counts: Dict[str, int], pool: dict) -> List[dict]:
    """Fuzzy-match each character in `counts` against `pool`, returning proposals
    sorted by line count descending. Shared by /match and /match_bulk."""
    proposals = []
    for char in sorted(counts, key=lambda n: counts[n], reverse=True):
        best, best_score, best_label = None, 0.0, None
        for cand in pool.values():
            # A member answers to its display name first, then every label it
            # was saved from / applied to, then registered aliases of those.
            for label in cand.get("labels") or [cand["name"]]:
                score = _name_similarity(char, label)
                if score > best_score:
                    best, best_score, best_label = cand, score, label
        match = None
        if best and best_score >= 0.6:
            match = {
                "key": best["key"], "name": best["name"], "source": best["source"],
                "type": best["type"], "score": round(best_score, 3),
                "exact": best_score >= 0.999,
                "via": _match_via(best, best_label),
            }
        proposals.append({"character": char, "line_count": counts[char], "match": match})
    return proposals


def _match_via(candidate: dict, label: Optional[str]) -> str:
    """Which of a candidate's labels produced the match: its display name, a
    remembered `known_as` label, or a registered alias."""
    if label is None or _norm_name(label) == _norm_name(candidate["name"]):
        return "name"
    known = {_norm_name(x) for x in candidate.get("known_as") or []}
    return "known_as" if _norm_name(label) in known else "alias"


def _remember_applied_labels(cast_name: str, mapping: Dict[str, str],
                             applied: List[str]) -> None:
    """Record each applied character label on its library member (`known_as`)
    so a renamed character matches by identity in the next book. One library
    transaction; skips generic labels and members that no longer exist."""
    wanted = {char: mapping[char] for char in applied if char in mapping}
    if not wanted:
        return
    def remember(lib):
        for char, key in wanted.items():
            entry = (lib["casts"].get(cast_name, {}).get("members", {}).get(key)
                     or lib["shared"].get(key))
            if not entry or entry.get("generic") or is_generic_speaker(char):
                continue
            labels = add_known_label(entry.get("known_as"), entry.get("name", ""))
            entry["known_as"] = add_known_label(labels, char)
    _mutate_voice_library(remember)


def _apply_cast_mapping(lib: dict, cast_name: str, mapping: Dict[str, str],
                         current_config: dict, chars: Optional[dict] = None,
                         book_id: Optional[str] = None) -> Tuple[dict, List[str]]:
    """Apply a confirmed character -> library member mapping onto a voice_config
    dict, returning a new dict (current_config is not mutated) along with the
    list of characters that were actually applied.

    If `chars` is given (the per-speaker line counts of a specific book), only
    characters present in it are considered — used by the bulk endpoint so a
    book only receives entries for characters that actually appear in it."""
    def resolve_entry(key):
        # cast members win over shared on collision
        cast_entry = lib["casts"][cast_name].get("members", {}).get(key)
        if cast_entry is not None:
            return cast_entry
        return lib["shared"].get(key)

    result_config = dict(current_config)
    applied = []
    for char, key in mapping.items():
        if chars is not None and char not in chars:
            continue
        if book_id and is_generic_speaker(char):
            scoped_key = get_cast_member_key(char, book_id)
            if resolve_entry(scoped_key):
                key = scoped_key
        entry = resolve_entry(key)
        if not entry:
            continue
        cfg = dict(entry.get("config") or {})
        assignment = (entry.get("assignments") or {}).get(book_id or "", {})
        if assignment.get("character_style"):
            cfg["character_style"] = assignment["character_style"]
        for field in get_trait_assignment_metadata({}):
            if field in assignment:
                cfg[field] = assignment[field]
        cfg = get_portable_voice_config(cfg)   # entries saved before this rule may still carry them
        cfg.pop("ready", None)
        # Preserve the current character's book-specific parts: its alias,
        # ready flag, character-state versions and voice timeline.
        current = result_config.get(char) if isinstance(result_config.get(char), dict) else {}
        if current.get("alias_of"):
            cfg["alias_of"] = current["alias_of"]
        if current.get("ready"):
            cfg["ready"] = True
        own_states = {key: value for key, value in (current.get("versions") or {}).items()
                      if isinstance(value, dict) and "persona_state" in value}
        if own_states:
            cfg["versions"] = {**(cfg.get("versions") or {}), **own_states}
        if current.get("version_timeline"):
            kept = [point for point in current["version_timeline"]
                    if point.get("version_id") is None or point.get("version_id") in (cfg.get("versions") or {})]
            if kept:
                cfg["version_timeline"] = kept
        result_config[char] = cfg
        applied.append(char)
    return result_config, applied


def _apply_cast_to_config_file(config_path: str, lib: dict, cast_name: str,
                                mapping: Dict[str, str], chars: Optional[dict] = None,
                                book_id: Optional[str] = None,
                                require_members: bool = False) -> List[str]:
    """Load a voice_config.json (if present), apply the cast mapping under a file
    lock, write it back atomically if anything changed, and return the list of
    characters that were applied.

    Raises TimeoutError if the lock can't be acquired - callers should map that
    to a 503 (single-book) or a per-book error entry (bulk).
    """
    with file_lock(config_path):
        current_config = safe_load_json(config_path, default={})

        current_config, applied = _apply_cast_mapping(
            lib, cast_name, mapping, current_config, chars=chars, book_id=book_id)

        if require_members:
            missing = sorted(char for char in mapping
                             if (chars is None or char in chars) and char not in applied)
            if missing:
                raise HTTPException(status_code=409, detail=(
                    "Mapped cast members are unavailable for: " + ", ".join(missing)))

        if applied:
            atomic_json_write(current_config, config_path)
    return applied


def _get_cast_library(cast_name):
    """Read a cast; mutation callers hold the library lock until publication."""
    lib = _load_voice_library()
    if cast_name not in lib["casts"]:
        raise HTTPException(status_code=404, detail=f"Cast '{cast_name}' not found.")
    return lib


def _is_safe_saved_script_name(name):
    return bool(name) and secure_filename(name) == name


def _apply_cast_to_saved_book(name, cast_name, mapping):
    script_path = os.path.join(SCRIPTS_DIR, f"{name}.json")
    with file_lock(VOICE_LIBRARY_PATH), ensure_book_state(SCRIPTS_DIR), file_lock(script_path):
        lib = _get_cast_library(cast_name)
        if not os.path.isfile(script_path):
            raise HTTPException(status_code=404, detail="Saved script not found")
        book_id = _get_saved_book_id(name)
        chars = _script_line_counts(script_path)
        return _apply_cast_to_config_file(
            os.path.join(SCRIPTS_DIR, f"{name}.voice_config.json"),
            lib, cast_name, mapping, chars=chars, book_id=book_id, require_members=True)


def _apply_cast_to_current_book(cast_name, mapping):
    with ensure_book_state(os.path.dirname(VOICE_CONFIG_PATH)), file_lock(VOICE_LIBRARY_PATH):
        lib = _get_cast_library(cast_name)
        return _apply_cast_to_config_file(
            VOICE_CONFIG_PATH, lib, cast_name, mapping, book_id=get_active_book_id())


@router.get("/api/voice_library")
async def voice_library_get():
    """Return the full library plus the current book's characters with line counts."""
    return await asyncio.to_thread(_build_voice_library_response)


def _build_voice_library_response():
    """Return the full library plus the current book's characters with line counts."""
    lib = _load_voice_library()
    counts = _script_line_counts()

    casts = []
    for cast_name, cast in sorted(lib["casts"].items()):
        members = cast.get("members", {})
        adapter_usage = get_cast_adapter_usage(lib, cast_name)
        casts.append({
            "name": cast_name,
            "member_count": len(members),
            "members": [
                {"key": k, "name": m.get("name", k), "type": (m.get("config") or {}).get("type"),
                 "adapter_id": (m.get("config") or {}).get("adapter_id"),
                 "character_style": (m.get("config") or {}).get("character_style", ""),
                 "line_count": m.get("line_count", 0), "generic": bool(m.get("generic")),
                 "book_id": m.get("book_id"), "assignments": m.get("assignments", {}),
                 "known_as": add_known_label(m.get("known_as"), m.get("name", k))}
                for k, m in sorted(members.items())
            ],
            "adapter_usage": adapter_usage,
        })

    shared = [
        {"key": k, "name": m.get("name", k), "type": (m.get("config") or {}).get("type"),
         "line_count": m.get("line_count", 0)}
        for k, m in sorted(lib["shared"].items())
    ]

    current_characters = [
        {"name": name, "line_count": counts[name]}
        for name in sorted(counts, key=lambda n: counts[n], reverse=True)
    ]

    return {"casts": casts, "shared": shared, "current_characters": current_characters,
            "active_book_id": get_active_book_id(), "major_line_threshold": CAST_MAJOR_LINE_THRESHOLD}


@router.post("/api/voice_library/casts")
async def voice_library_create_cast(request: CastCreateRequest):
    name = request.name.strip()
    if not name:
        raise HTTPException(status_code=400, detail="Cast name is required.")
    if name == "__shared__":
        # Reserved sentinel: other endpoints treat this name as the global
        # shared pool, so a real cast by this name would be unaddressable.
        raise HTTPException(status_code=400, detail="'__shared__' is a reserved name.")
    def create(lib):
        if name in lib["casts"]:
            raise HTTPException(status_code=409, detail=f"Cast '{name}' already exists.")
        lib["casts"][name] = {"members": {}}

    await _mutate_voice_library_async(create)
    return {"status": "created", "name": name}


@router.post("/api/voice_library/favorites/{adapter_id}")
async def voice_library_toggle_favorite(adapter_id: str):
    """Star or unstar one adapter; suggestions prefer compatible favorites."""
    adapter_id = adapter_id.strip()
    if not adapter_id:
        raise HTTPException(status_code=400, detail="Adapter id is required.")

    def toggle(lib):
        try:
            stored = lib.get("favorites") or []
            identities = get_resolved_adapter_id_mapping(LORA_MODELS_DIR, [*stored, adapter_id])
            favorites = sorted({identities[name] for name in stored})
            current_id = identities[adapter_id]
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        if current_id in favorites:
            favorites.remove(current_id)
            starred = False
        else:
            favorites.append(current_id)
            starred = True
        lib["favorites"] = favorites
        return {"favorite": starred, "favorites": favorites}

    return await _mutate_voice_library_async(toggle)


@router.delete("/api/voice_library/casts/{cast}")
async def voice_library_delete_cast(cast: str):
    def delete(lib):
        if cast not in lib["casts"]:
            raise HTTPException(status_code=404, detail=f"Cast '{cast}' not found.")
        del lib["casts"][cast]

    await _mutate_voice_library_async(delete)
    return {"status": "deleted", "name": cast}


@router.delete("/api/voice_library/casts/{cast}/members/{key}")
async def voice_library_delete_member(cast: str, key: str):
    def delete(lib):
        if cast == "__shared__":
            pool = lib["shared"]
        else:
            if cast not in lib["casts"]:
                raise HTTPException(status_code=404, detail=f"Cast '{cast}' not found.")
            pool = lib["casts"][cast].setdefault("members", {})
        if key not in pool:
            raise HTTPException(status_code=404, detail=f"Member '{key}' not found.")
        del pool[key]

    await _mutate_voice_library_async(delete)
    return {"status": "deleted", "cast": cast, "key": key}


def _save_voice_library_sync(request):
    cast_name = request.cast.strip()
    shared_override = {_norm_name(n) for n in (request.shared or [])}
    cast_specific = {_norm_name(n) for n in (request.cast_specific or [])}

    def save(lib):
        voice_config = {}
        if os.path.exists(VOICE_CONFIG_PATH):
            try:
                voice_config = safe_load_json(VOICE_CONFIG_PATH, default={})
            except (json.JSONDecodeError, ValueError) as e:
                _warn_corrupted_json("voice config", VOICE_CONFIG_PATH, "ignoring", e)
                voice_config = {}

        counts = _script_line_counts()
        book_id = get_active_book_id()
        if cast_name not in lib["casts"]:
            raise HTTPException(status_code=404, detail=f"Cast '{cast_name}' not found. Create it first.")
        saved = {"cast": [], "shared": []}
        for char in request.characters:
            config = voice_config.get(char)
            if not config:
                continue
            try:
                key = get_cast_member_key(char, book_id)
            except ValueError as e:
                raise HTTPException(status_code=400, detail=str(e))
            is_shared = (key in SHARED_DEFAULT_NAMES or key in shared_override) and key not in cast_specific
            if is_shared:
                pool = get_cast_storage_pool(lib, cast_name, char)
                entry = _make_library_entry(char, config, counts.get(char, 0), book_id,
                                            existing=pool.get(key))
                pool[key] = entry
                saved["shared"].append(char)
            else:
                members = lib["casts"][cast_name].setdefault("members", {})
                entry = _make_library_entry(char, config, counts.get(char, 0), book_id,
                                            existing=members.get(key))
                members[key] = entry
                saved["cast"].append(char)
        return saved

    with ensure_book_state(os.path.dirname(VOICE_CONFIG_PATH)):
        saved = _mutate_voice_library(save, companion_paths=(SCRIPT_PATH, VOICE_CONFIG_PATH))
    return {"status": "saved", "cast": cast_name, "saved": saved}


@router.post("/api/voice_library/save")
async def voice_library_save(request: LibrarySaveRequest):
    """Save selected current-book characters into a cast (NARRATOR -> shared by default)."""
    try:
        return await asyncio.to_thread(_save_voice_library_sync, request)
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Voice library or current book is busy; please try again.")


@router.post("/api/voice_library/match")
async def voice_library_match(request: CastCreateRequest):
    """Fuzzy-match the current book's characters against a cast (+shared pool).
    Returns proposals for the user to confirm before applying. `name` = cast name."""
    return await asyncio.to_thread(_build_cast_match_response, request)


def _build_cast_match_response(request: CastCreateRequest):
    """Fuzzy-match the current book's characters against a cast (+shared pool).
    Returns proposals for the user to confirm before applying. `name` = cast name."""
    cast_name = request.name.strip()
    lib = _get_cast_library(cast_name)

    pool = _cast_match_pool(lib, cast_name, get_active_book_id())

    counts = _script_line_counts()
    if not counts:
        raise HTTPException(status_code=400, detail="No characters in the current book. Generate a script first.")

    proposals = _build_match_proposals(counts, pool)

    return {"cast": cast_name, "proposals": proposals}


@router.post("/api/voice_library/match_bulk")
async def voice_library_match_bulk(request: CastMatchBulkRequest):
    """Fuzzy-match the union of characters across several saved books against a
    cast (+shared pool). Same proposal shape as /api/voice_library/match, but
    `line_count` is the sum across all selected books."""
    return await asyncio.to_thread(_build_bulk_cast_match_response, request)


def _build_bulk_cast_match_response(request: CastMatchBulkRequest):
    """Fuzzy-match the union of characters across several saved books against a
    cast (+shared pool). Same proposal shape as /api/voice_library/match, but
    `line_count` is the sum across all selected books."""
    cast_name = request.name.strip()
    lib = _get_cast_library(cast_name)

    pool = _cast_match_pool(lib, cast_name, include_all_generic=True)

    def _collect_counts():
        counts = {}
        for name in dict.fromkeys(request.script_names):
            if not _is_safe_saved_script_name(name):
                raise HTTPException(status_code=400, detail="Invalid script name")
            safe_name = name
            script_path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
            for char, n in _script_line_counts(script_path).items():
                counts[char] = counts.get(char, 0) + n
        return counts

    # The complete response, including matching, runs in the route's worker.
    with ensure_book_state(SCRIPTS_DIR):
        counts = _collect_counts()

    if not counts:
        raise HTTPException(status_code=400, detail="No characters found in the selected books.")

    proposals = _build_match_proposals(counts, pool)

    return {"cast": cast_name, "proposals": proposals, "book_count": len(set(request.script_names))}


def get_cast_label_persistence_warning(cast_name: str) -> str:
    """Explain the secondary failure without undoing a committed assignment."""
    return (f"Voices from cast '{cast_name}' were applied, but learned character labels "
            "could not be saved because the voice library is busy. Confirm the character "
            "mappings when applying this cast to another book.")


@router.post("/api/voice_library/apply")
async def voice_library_apply(request: LibraryApplyRequest):
    """Apply confirmed cast members onto the current voice_config by the given mapping."""
    cast_name = request.cast.strip()

    # Offload to a worker thread so file_lock's wait loop can't block the event loop.
    # Hold the lock across the read-modify-write so this can't race a batch
    # review's concurrent speaker-rename remap of the same file.
    try:
        applied = await asyncio.to_thread(
            _apply_cast_to_current_book, cast_name, request.mapping)
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Voice config is busy (locked by another operation); please try again.")
    warnings = []
    try:
        await asyncio.to_thread(_remember_applied_labels, cast_name, request.mapping, applied)
    except TimeoutError:
        logger.warning("Applied cast '%s' but could not record the labels (library locked).", cast_name)
        if applied:
            warnings.append(get_cast_label_persistence_warning(cast_name))

    return {"status": "applied", "cast": cast_name, "applied": applied, "count": len(applied),
            "warnings": warnings}


@router.post("/api/voice_library/apply_bulk")
async def voice_library_apply_bulk(request: LibraryApplyBulkRequest):
    """Apply confirmed cast members onto several saved books' voice_config.json
    files at once. Each book only receives entries for characters that actually
    appear in that book."""
    cast_name = request.cast.strip()

    def _apply_all():
        # Preserve the existing request-level error for an initially absent cast.
        with file_lock(VOICE_LIBRARY_PATH):
            _get_cast_library(cast_name)
        results = []
        seen = set()
        for name in request.script_names:
            if not _is_safe_saved_script_name(name) or name in seen:
                error = "Duplicate script name" if name in seen else "Invalid script name"
                results.append({"name": name, "applied": [], "count": 0, "error": error})
                continue
            seen.add(name)
            try:
                applied = _apply_cast_to_saved_book(name, cast_name, request.mapping)
            except (TimeoutError, HTTPException) as error:
                detail = error.detail if isinstance(error, HTTPException) else str(error)
                results.append({"name": name, "applied": [], "count": 0, "error": detail})
                continue

            results.append({"name": name, "applied": applied, "count": len(applied)})
        applied_union = sorted({c for r in results for c in r["applied"]})
        try:
            _remember_applied_labels(cast_name, request.mapping, applied_union)
        except TimeoutError:
            logger.warning("Applied cast '%s' in bulk but could not record the labels (library locked).", cast_name)
            warning = get_cast_label_persistence_warning(cast_name)
            results = [{**result, "warnings": [warning]} if result["applied"] else result
                       for result in results]
        return results

    # Offload the per-book locking/read/write loop to a worker thread so
    # applying a cast to a long series doesn't block the event loop.
    results = await asyncio.to_thread(_apply_all)

    return {"cast": cast_name, "results": results,
            "warnings": list(dict.fromkeys(
                warning for result in results for warning in result.get("warnings", [])))}
