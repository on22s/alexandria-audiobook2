"""One shared, full-book resolver for voice/style timeline anchors.

A timeline point used to carry only ``from_index`` - the chunk's list slot.
Editor insert/delete renumbers ``chunk['id']``, so the point slid onto
different text (#2160). New points also carry ``from_uid``, the stable chunk
identity. This module maps uids to ABSOLUTE book positions and materializes a
copy of the voice config for reads; the persisted config is never rewritten.

Policy for the cases the old index-only timeline could not express:

* a uid that resolves in the supplied rows wins over the recorded index;
* an unknown uid (removed anchor, regenerated chunk, anchor outside a sparse
  batch) keeps its recorded index - legacy semantics, never a dropped point;
* duplicate uids at different positions are ambiguous and are not resolved;
* a malformed legacy point is left untouched, so the existing per-chunk
  validator still reports it for the affected speaker only.
"""
import copy
import json
from contextlib import contextmanager
from contextvars import ContextVar

TIMELINE_KEYS = ("style_timeline", "version_timeline")
TIMELINE_VIEW_FIELDS = ("display_index", "delete_key", "saved_point", "book_token")

_full_uid_index = ContextVar("timeline_full_uid_index", default=None)


@contextmanager
def full_uid_index_context(mapping):
    """Bind the authoritative whole-book uid map around a sparse batch dispatch.

    A batch may request rows [2, 4] while the anchor moved to row 5. Without the
    full map a resolver would see an absent uid and fall back to the stale index;
    the caller supplies the map instead of changing the engine call signature.
    """
    token = _full_uid_index.set(mapping)
    try:
        yield
    finally:
        _full_uid_index.reset(token)


def get_style_timeline_index(point):
    """Read a nonnegative integer timeline position, rejecting malformed data."""
    value = point.get("from_index", 0)
    try:
        index = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError("style_timeline from_index must be a nonnegative integer") from exc
    if isinstance(value, bool) or index < 0 or (not isinstance(value, str) and value != index):
        raise ValueError("style_timeline from_index must be a nonnegative integer")
    return index


def _index_uids(chunks, *, sparse=False):
    mapping, repeated = {}, set()
    for position, row in enumerate(chunks or []):
        if not isinstance(row, dict):
            continue
        uid = row.get("uid")
        if not isinstance(uid, str) or not uid:
            continue
        index = row.get("index") if sparse else position
        index = index if type(index) is int and index >= 0 else position
        if uid in mapping and mapping[uid] != index:
            repeated.add(uid)
        mapping[uid] = index
    for uid in repeated:
        mapping.pop(uid, None)
    return mapping, repeated


def build_uid_index(chunks):
    """Map unique UIDs to absolute positions in the whole book."""
    return _index_uids(chunks)[0]


def ambiguous_uids(chunks):
    """Uids appearing at more than one absolute position; never writable."""
    return _index_uids(chunks)[1]


def get_anchor_uid(chunks, index):
    """The stable uid of the row at an ABSOLUTE book index, or None."""
    rows = chunks if isinstance(chunks, (list, tuple)) else []
    for position, row in enumerate(rows):
        if isinstance(row, dict) and position == index:
            uid = row.get("uid")
            if isinstance(uid, str) and uid:
                return uid
    return None


def resolve_timeline_index(point, uid_index):
    """Effective absolute index of one point. Unknown uid -> recorded index."""
    index = get_style_timeline_index(point)
    uid = point.get("from_uid")
    if isinstance(uid, str) and uid and isinstance(uid_index, dict):
        resolved = uid_index.get(uid)
        if isinstance(resolved, int) and not isinstance(resolved, bool) and resolved >= 0:
            return resolved
    return index


def validate_write_anchor(chunks, from_index, uid=None, *, exact=False):
    """Authorize a NEW change point; return its canonical (index, uid).

    Reading preserves tolerated orphans, but a write may only introduce an
    anchor that is verifiable now: a uid that resolves unambiguously in the
    current book, or a bare legacy index before chunks exist. A pre-existing
    orphan uid may be saved unchanged (``exact``) so a snapshot
    round-trip cannot be blocked by history.
    """
    rows = chunks or []
    mapping = build_uid_index(rows)
    ambiguous = ambiguous_uids(rows)
    if isinstance(uid, str) and uid:
        if uid in ambiguous:
            raise ValueError("This line identity is ambiguous; reload the book before saving.")
        if uid in mapping:
            if not 0 <= from_index < len(rows) and not exact:
                raise ValueError("The change point must name a line in the current book.")
            return mapping[uid], uid
        if exact:
            return from_index, uid
        raise ValueError("The anchored line no longer exists; reload before saving.")
    if chunks is not None and not 0 <= from_index < len(rows) and not exact:
        raise ValueError("The change point must name a line in the current book.")
    found = get_anchor_uid(rows, from_index) if rows else None
    if found in ambiguous:
        raise ValueError("This line identity is ambiguous; reload the book before saving.")
    return from_index, found


def _stored_index(point):
    """Numeric-string legacy indices must compare equal to their integer form."""
    try:
        return get_style_timeline_index(point)
    except (TypeError, ValueError):
        return None


def get_timeline_point_at_anchor(points, from_index, uid):
    """Find an existing UID record, or the legacy point at the selected line."""
    legacy = None
    for point in reversed(points or []):
        if not isinstance(point, dict):
            continue
        if uid and point.get("from_uid") == uid:
            return point
        if legacy is None and not point.get("from_uid") and _stored_index(point) == from_index:
            legacy = point
    return legacy


def replace_style_point(points, from_index, uid, character_style, *, uid_index=None):
    """Replace the point identified by uid (or legacy index), keeping others."""
    previous = get_timeline_point_at_anchor(points, from_index, uid)
    result = []
    for point in points or []:
        if not isinstance(point, dict):
            result.append(point)
            continue
        point_uid = point.get("from_uid")
        if (point_uid and point_uid != uid) or (not point_uid and _stored_index(point) != from_index):
            result.append(point)
    if character_style:
        point = {**(previous or {}), "from_index": from_index, "character_style": character_style}
        if isinstance(uid, str) and uid:
            point["from_uid"] = uid
        result.append(point)
    return sorted(result, key=lambda item: resolve_timeline_index(item, uid_index)
                  if isinstance(item, dict) else -1)


def delete_style_points(points, from_index, uid=None, *, current_uid=None):
    """Delete a badge by its recorded identity, or remove points at a live index."""
    rows = points or []
    if isinstance(uid, str) and uid:
        target = [point for point in rows if isinstance(point, dict)
                  and point.get("from_uid") == uid and _stored_index(point) == from_index]
        return [point for point in rows if point not in target]
    return [point for point in rows if not isinstance(point, dict)
            or (point.get("from_uid") != current_uid if point.get("from_uid")
                else _stored_index(point) != from_index)]


def canonical_point_payload(point, kind, *, exact=False):
    """Compare saved JSON exactly, or normalize legacy equivalents for compatibility."""
    data = {key: value for key, value in point.items() if key not in TIMELINE_VIEW_FIELDS}
    if not exact:
        data.pop("from_uid", None)
        field = "character_style" if kind == "style_timeline" else "version_id"
        if data.get(field) is None or (kind == "style_timeline" and data[field] == ""):
            data.pop(field, None)
        try:
            data["from_index"] = get_style_timeline_index(point)
        except (TypeError, ValueError):
            pass
    return json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)


def get_timeline_index_spelling(point):
    """Exact JSON spelling and presence of a point's fallback index."""
    return json.dumps(point["from_index"], ensure_ascii=False) if "from_index" in point else None


def _resolve_points(points, uid_index):
    if not isinstance(points, list):
        return points
    ordered = []
    seen = {}
    for point in points:
        if not isinstance(point, dict):
            ordered.append(point)
            continue
        try:
            index = resolve_timeline_index(point, uid_index)
        except (TypeError, ValueError):
            ordered.append(point)
            continue
        resolved = dict(point, from_index=index)
        if index in seen:
            ordered[seen[index]] = resolved
        else:
            seen[index] = len(ordered)
            ordered.append(resolved)
    return ordered


def resolve_timeline_anchors(voice_config, chunks=None, uid_index=None, *, sparse=False):
    """-> a config whose timeline indices follow their stable anchor chunks.

    Explicit whole-book rows or a UID index take precedence. Batch callers
    pass sparse=True to prefer the bound whole-book index and otherwise use
    the supplied rows' absolute indices. A private caller can omit rows when
    the whole-book index is already bound.
    """
    if not isinstance(voice_config, dict):
        return voice_config
    bound = _full_uid_index.get()
    if uid_index is not None:
        mapping = uid_index
    elif chunks is not None and not sparse:
        mapping = build_uid_index(chunks)
    elif bound is not None:
        mapping = bound
    elif chunks is not None:
        mapping = _index_uids(chunks, sparse=True)[0]
    else:
        mapping = None
    resolved = None
    for name, data in voice_config.items():
        if not isinstance(data, dict):
            continue
        changes = {}
        for key in TIMELINE_KEYS:
            if key not in data or data[key] is None:
                continue
            points = _resolve_points(data[key], mapping)
            if points != data[key]:
                changes[key] = points
        if changes:
            if resolved is None:
                resolved = copy.deepcopy(voice_config)
            resolved[name].update(changes)
    return voice_config if resolved is None else resolved
