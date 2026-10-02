"""One locked write path for the active voice configuration."""

import json
import os
import shutil
import hashlib

from utils import atomic_json_write, file_lock


def apply_voice_config_update(path, transform, expected=None, backup_path=None, expected_revision=None):
    """Read, transform, and atomically save a voice config under its file lock.

    ``transform`` receives the current dict and returns a new dict. A CLI
    repair can pass its reported snapshot as ``expected`` so an intervening
    UI save is rejected instead of overwritten.
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
        if backup_path:
            if os.path.lexists(backup_path):
                raise FileExistsError(f"Voice config backup already exists: {backup_path}")
            shutil.copy2(path, backup_path)
        atomic_json_write(updated, path)
        return updated



class VoiceConfigConflict(RuntimeError):
    """A guarded save was based on a stale book or voice snapshot."""


def get_voice_config_revision(config):
    """Return one canonical revision for every stored voice field."""
    return hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False,
                                    separators=(",", ":"), allow_nan=False).encode("utf-8")).hexdigest()
