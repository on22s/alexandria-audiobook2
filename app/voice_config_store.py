"""One locked write path for the active voice configuration."""

import json
import os
import shutil
import hashlib

from utils import atomic_json_write, file_lock


def apply_voice_config_update(path, transform, expected=None, backup_path=None, expected_revision=None,
                              *, publish=None):
    """Read, transform, and atomically save a voice config under its file lock.

    ``transform`` receives the current dict and returns a new dict. A CLI
    repair can pass its reported snapshot as ``expected`` so an intervening
    UI save is rejected instead of overwritten. A paired-file publisher may
    replace the default atomic write; the caller must hold its companion lock.
    """
    with file_lock(path):
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as source:
                current = json.load(source)
        else:
            current = {}
        if not isinstance(current, dict):
            raise ValueError("Voice config must be a JSON object")
        if expected_revision is not None and get_voice_config_revision(current) != expected_revision:
            raise VoiceConfigConflict("Voice configuration changed; reload it before saving")
        if expected is not None and current != expected:
            raise RuntimeError("Voice config changed since the repair report; rerun the report")
        updated = transform(current)
        if not isinstance(updated, dict):
            raise ValueError("Voice config update must return a JSON object")
        # Validate the FINAL proposed map BEFORE any commit with the
        # one storable validator: the stored encoding itself (indent=2,
        # finite only), not just the compact revision digest. A
        # nonfinite or unserializable map is a no-write 400-class
        # refusal here, never an after-write 400 with the bad bytes
        # already persisted.
        ensure_voice_config_storable(updated)
        if backup_path:
            if os.path.lexists(backup_path):
                raise FileExistsError(f"Voice config backup already exists: {backup_path}")
            shutil.copy2(path, backup_path)
        if publish is None:
            atomic_json_write(updated, path)
        else:
            publish(updated, path)
        return updated



class VoiceConfigConflict(RuntimeError):
    """A guarded save was based on a stale book or voice snapshot."""


_MAX_VOICE_CONFIG_DEPTH = 100


def _json_nesting_exceeds(value, limit):
    """Iteratively (no recursion) whether dict/list nesting exceeds limit."""
    stack = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        if isinstance(current, dict):
            depth += 1
            if depth > limit:
                return True
            stack.extend((item, depth) for item in current.values())
        elif isinstance(current, (list, tuple)):
            depth += 1
            if depth > limit:
                return True
            stack.extend((item, depth) for item in current)
    return False


def is_voice_config_depth_exceeded(config):
    """Whether a voice map nests beyond the one shared depth bound.

    The single iterative rule used by BOTH the structured read boundary
    (``get_book_snapshot``) and the pre-write validator below: refusal
    must not depend on how deep the runtime's JSON decoder/encoder
    happens to recurse (Python 3.12 parses nesting that 3.10 rejects in
    ``json.loads`` itself, leaving the response encoder to fail outside
    the route's error mapping). Pure iteration, no serialization.
    """
    return _json_nesting_exceeds(config, _MAX_VOICE_CONFIG_DEPTH)


def ensure_voice_config_storable(config):
    """Refuse a final map the actual storage encoding cannot write.

    Preserve BOTH guarantees before any invalidation, backup or write:
    (1) the canonical revision validation - sort_keys (mixed-type keys
    refuse) and UTF-8 encoding (unpaired surrogates refuse, which a
    bare json.dumps str would accept); and (2) actual stored-encoding
    readiness - ``atomic_json_write`` uses ``json.dump(indent=2)``,
    whose encoder on some runtimes raises RecursionError for nesting
    where the compact revision encoder succeeds. An explicit bounded
    depth check runs first and iteratively, so refusal near the
    recursion ceiling does not depend on how many frames the caller
    already holds (preflight and actual dump could otherwise differ).
    Legitimate voice maps nest only a handful of levels; 100 leaves a
    wide reserve below every runtime's ceiling.
    """
    if is_voice_config_depth_exceeded(config):
        raise ValueError("Voice configuration values must be finite JSON values")
    try:
        get_voice_config_revision(config)
        json.dumps(config, indent=2, ensure_ascii=False,
                   allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError) as exc:
        raise ValueError(
            "Voice configuration values must be finite JSON values") from exc


def get_voice_config_revision(config):
    """Return one canonical revision for every stored voice field."""
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
