import asyncio
import difflib
import json
import logging
import os
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from core import (
    CAST_MAJOR_LINE_THRESHOLD,
    SCRIPTS_DIR,
    SHARED_DEFAULT_NAMES,
    VOICE_CONFIG_PATH,
    VOICE_LIBRARY_PATH,
    _get_saved_book_id,
    _load_voice_library,
    _make_library_entry,
    _norm_name,
    _script_line_counts,
    _warn_corrupted_json,
    get_active_book_id,
    get_cast_adapter_usage,
    get_cast_member_key,
    get_cast_storage_pool,
    get_trait_assignment_metadata,
)
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


def _mutate_voice_library(mutator):
    """Apply one read-modify-write transaction under the library lock."""
    try:
        with file_lock(VOICE_LIBRARY_PATH):
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


def _cast_match_pool(lib: dict, cast_name: str, book_id: Optional[str] = None,
                     include_all_generic: bool = False) -> dict:
    """Build the candidate pool for matching against a cast: shared first, cast
    members override on key collision (a cast-specific narrator beats the
    shared narrator = "different narrator")."""
    pool = {}
    for k, m in lib["shared"].items():
        pool[k] = {"key": k, "name": m.get("name", k), "source": "shared",
                   "type": (m.get("config") or {}).get("type")}
    for k, m in lib["casts"][cast_name].get("members", {}).items():
        if m.get("generic") and not include_all_generic and m.get("book_id") != book_id:
            continue
        if is_generic_speaker(m.get("name", k)) and not m.get("book_id"):
            continue  # legacy ambiguous generic entry
        pool[k] = {"key": k, "name": m.get("name", k), "source": "cast",
                   "type": (m.get("config") or {}).get("type")}
    return pool


def _build_match_proposals(counts: Dict[str, int], pool: dict) -> List[dict]:
    """Fuzzy-match each character in `counts` against `pool`, returning proposals
    sorted by line count descending. Shared by /match and /match_bulk."""
    proposals = []
    for char in sorted(counts, key=lambda n: counts[n], reverse=True):
        best, best_score = None, 0.0
        for cand in pool.values():
            score = _name_similarity(char, cand["name"])
            if score > best_score:
                best, best_score = cand, score
        match = None
        if best and best_score >= 0.6:
            match = {
                "key": best["key"], "name": best["name"], "source": best["source"],
                "type": best["type"], "score": round(best_score, 3),
                "exact": best_score >= 0.999,
            }
        proposals.append({"character": char, "line_count": counts[char], "match": match})
    return proposals


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
        return lib["casts"][cast_name].get("members", {}).get(key) or lib["shared"].get(key)

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
        cfg.pop("alias_of", None)
        # Preserve an existing alias_of on the current character (book-specific)
        if isinstance(result_config.get(char), dict) and result_config[char].get("alias_of"):
            cfg["alias_of"] = result_config[char]["alias_of"]
        result_config[char] = cfg
        applied.append(char)
    return result_config, applied


def _apply_cast_to_config_file(config_path: str, lib: dict, cast_name: str,
                                mapping: Dict[str, str], chars: Optional[dict] = None,
                                book_id: Optional[str] = None) -> List[str]:
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

        if applied:
            atomic_json_write(current_config, config_path)
    return applied


@router.get("/api/voice_library")
async def voice_library_get():
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
                 "book_id": m.get("book_id"), "assignments": m.get("assignments", {})}
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


@router.post("/api/voice_library/save")
async def voice_library_save(request: LibrarySaveRequest):
    """Save selected current-book characters into a cast (NARRATOR -> shared by default)."""
    cast_name = request.cast.strip()
    voice_config = {}
    if os.path.exists(VOICE_CONFIG_PATH):
        try:
            with open(VOICE_CONFIG_PATH, "r", encoding="utf-8") as f:
                voice_config = json.load(f)
        except (json.JSONDecodeError, ValueError) as e:
            _warn_corrupted_json("voice config", VOICE_CONFIG_PATH, "ignoring", e)
            voice_config = {}

    counts = _script_line_counts()
    book_id = get_active_book_id()
    shared_override = {_norm_name(n) for n in (request.shared or [])}
    cast_specific = {_norm_name(n) for n in (request.cast_specific or [])}

    def save(lib):
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

    saved = await _mutate_voice_library_async(save)
    return {"status": "saved", "cast": cast_name, "saved": saved}


@router.post("/api/voice_library/match")
async def voice_library_match(request: CastCreateRequest):
    """Fuzzy-match the current book's characters against a cast (+shared pool).
    Returns proposals for the user to confirm before applying. `name` = cast name."""
    cast_name = request.name.strip()
    lib = _load_voice_library()
    if cast_name not in lib["casts"]:
        raise HTTPException(status_code=404, detail=f"Cast '{cast_name}' not found.")

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
    cast_name = request.name.strip()
    lib = _load_voice_library()
    if cast_name not in lib["casts"]:
        raise HTTPException(status_code=404, detail=f"Cast '{cast_name}' not found.")

    pool = _cast_match_pool(lib, cast_name, include_all_generic=True)

    def _collect_counts():
        counts = {}
        for name in request.script_names:
            safe_name = secure_filename(name)
            if not safe_name:
                continue
            script_path = os.path.join(SCRIPTS_DIR, f"{safe_name}.json")
            for char, n in _script_line_counts(script_path).items():
                counts[char] = counts.get(char, 0) + n
        return counts

    # Offload the per-book file reads to a worker thread so reading a large
    # series doesn't block the event loop (and other in-flight requests).
    counts = await asyncio.to_thread(_collect_counts)

    if not counts:
        raise HTTPException(status_code=400, detail="No characters found in the selected books.")

    proposals = _build_match_proposals(counts, pool)

    return {"cast": cast_name, "proposals": proposals, "book_count": len(request.script_names)}


@router.post("/api/voice_library/apply")
async def voice_library_apply(request: LibraryApplyRequest):
    """Apply confirmed cast members onto the current voice_config by the given mapping."""
    cast_name = request.cast.strip()
    lib = _load_voice_library()
    if cast_name not in lib["casts"]:
        raise HTTPException(status_code=404, detail=f"Cast '{cast_name}' not found.")

    # Offload to a worker thread so file_lock's wait loop can't block the event loop.
    # Hold the lock across the read-modify-write so this can't race a batch
    # review's concurrent speaker-rename remap of the same file.
    try:
        applied = await asyncio.to_thread(
            _apply_cast_to_config_file, VOICE_CONFIG_PATH, lib, cast_name, request.mapping,
            None, get_active_book_id())
    except TimeoutError:
        raise HTTPException(status_code=503, detail="Voice config is busy (locked by another operation); please try again.")

    return {"status": "applied", "cast": cast_name, "applied": applied, "count": len(applied)}


@router.post("/api/voice_library/apply_bulk")
async def voice_library_apply_bulk(request: LibraryApplyBulkRequest):
    """Apply confirmed cast members onto several saved books' voice_config.json
    files at once. Each book only receives entries for characters that actually
    appear in that book."""
    cast_name = request.cast.strip()
    lib = _load_voice_library()
    if cast_name not in lib["casts"]:
        raise HTTPException(status_code=404, detail=f"Cast '{cast_name}' not found.")

    def _apply_all():
        results = []
        for name in request.script_names:
            safe_name = secure_filename(name)
            if not safe_name:
                results.append({"name": name, "applied": [], "count": 0, "error": "Invalid script name"})
                continue
            book_id = _get_saved_book_id(safe_name)
            chars = _script_line_counts(os.path.join(SCRIPTS_DIR, f"{safe_name}.json"))

            config_path = os.path.join(SCRIPTS_DIR, f"{safe_name}.voice_config.json")
            # Hold the lock across the read-modify-write so this can't race a batch
            # review's concurrent speaker-rename remap of the same companion file.
            try:
                applied = _apply_cast_to_config_file(
                    config_path, lib, cast_name, request.mapping, chars=chars, book_id=book_id)
            except TimeoutError as e:
                results.append({"name": name, "applied": [], "count": 0, "error": str(e)})
                continue

            results.append({"name": name, "applied": applied, "count": len(applied)})
        return results

    # Offload the per-book locking/read/write loop to a worker thread so
    # applying a cast to a long series doesn't block the event loop.
    results = await asyncio.to_thread(_apply_all)

    return {"cast": cast_name, "results": results}
