"""Durable history records for long-running Alexandria tasks."""

import datetime
import hashlib
import os
import threading
from functools import lru_cache

from utils import (atomic_json_write, file_lock, get_unique_id, is_path_inside,
                   safe_load_json, secure_filename)


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def start_run(history_dir, task_name):
    """Create and persist a running task record, returning its id."""
    os.makedirs(history_dir, exist_ok=True)
    run_id = get_unique_id("run")
    record = {
        "id": run_id,
        "task": task_name,
        "status": "running",
        "started_at": _utc_now(),
        "finished_at": None,
        "error": None,
        "artifacts": [],
    }
    atomic_json_write(record, os.path.join(history_dir, f"{run_id}.json"))
    return run_id


def _get_run_path(history_dir, run_id):
    safe_id = secure_filename(run_id)
    if not safe_id or safe_id != run_id:
        raise FileNotFoundError(f"Run history record not found: {run_id}")
    return os.path.join(history_dir, f"{run_id}.json")


def _apply_run_update(history_dir, run_id, get_updates):
    """Merge a callback's returned fields into the fresh record under its lock.

    The callback reads the record without mutating it; None skips publication.
    """
    path = _get_run_path(history_dir, run_id)
    with file_lock(path):
        record = get_run(history_dir, run_id)
        if record is None:
            raise FileNotFoundError(f"Run history record not found: {run_id}")
        updates = get_updates(record)
        if updates is None:
            return None
        updated = {**record, **updates}
        atomic_json_write(updated, path)
        return updated


def update_run(history_dir, run_id, updates):
    """Atomically merge bounded task-specific summary fields into one run."""
    if not isinstance(updates, dict):
        raise TypeError("Run updates must be a mapping")
    identity_fields = {"id", "task", "started_at"}
    forbidden = identity_fields.intersection(updates)
    if forbidden:
        raise ValueError("Run identity fields cannot be updated: " +
                         ", ".join(sorted(forbidden)))
    return _apply_run_update(history_dir, run_id, lambda record: updates)


def finish_run(history_dir, run_id, status, error=None):
    """Finish an existing task record without changing its identity/start time."""
    return update_run(history_dir, run_id, {
        "status": status, "finished_at": _utc_now(), "error": error})


def _get_file_snapshot(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        before = os.fstat(source.fileno())
        size = 0
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
            size += len(block)
        after = os.fstat(source.fileno())
    version_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns", "st_ctime_ns")
    if (size != before.st_size or
            any(getattr(before, field) != getattr(after, field)
                for field in version_fields)):
        raise ValueError(f"Declared file changed while hashing: {path}")
    return {"sha256": digest.hexdigest(), "size_bytes": size}


def record_artifact(history_dir, run_id, artifact_path, kind, data_dir,
                    source_paths=(), config_path=None):
    """Hash an output and its declared inputs, then append it to a run record."""
    _get_run_path(history_dir, run_id)
    data_dir = os.path.abspath(data_dir)
    artifact_path = os.path.abspath(artifact_path)
    if not is_path_inside(artifact_path, data_dir):
        raise ValueError("Artifact path must be inside data directory")
    if not os.path.isfile(artifact_path):
        raise FileNotFoundError(f"Artifact not found: {artifact_path}")

    def describe(path):
        absolute = os.path.abspath(path)
        if not is_path_inside(absolute, data_dir):
            raise ValueError("Declared path must be inside data directory")
        if not os.path.isfile(absolute):
            raise FileNotFoundError(f"Declared file not found: {absolute}")
        return {
            "path": os.path.relpath(absolute, data_dir),
            **_get_file_snapshot(absolute),
        }

    artifact = {
        **describe(artifact_path),
        "kind": kind,
        "recorded_at": _utc_now(),
        "sources": [describe(path) for path in source_paths],
        "config": describe(config_path) if config_path else None,
    }
    _apply_run_update(history_dir, run_id, lambda record: {
        "artifacts": [*record.get("artifacts", []), artifact]})
    return artifact


def get_run(history_dir, run_id):
    """Return one run record, or None when its safe id does not exist."""
    safe_id = secure_filename(run_id)
    if not safe_id or safe_id != run_id:
        return None
    record = safe_load_json(os.path.join(history_dir, f"{safe_id}.json"), default={})
    return record or None


@lru_cache(maxsize=16)
def _get_run_sort_cache(history_dir):
    # Intentionally shared, lock-protected metadata only; never full records.
    return threading.Lock(), {}


def _get_run_file_version(path):
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (stat.st_dev, stat.st_ino, stat.st_size,
            stat.st_mtime_ns, stat.st_ctime_ns)


def _get_run_with_version(path):
    before = _get_run_file_version(path)
    record = safe_load_json(path, default={})
    after = _get_run_file_version(path)
    return record, after if before == after else None


def _iter_run_records(history_dir):
    """Read complete history for recovery, priming immutable sorting metadata."""
    history_dir = os.path.abspath(history_dir)
    if not os.path.isdir(history_dir):
        return
    lock, cache = _get_run_sort_cache(history_dir)
    for name in os.listdir(history_dir):
        if name.startswith("run_") and name.endswith(".json"):
            record, version = _get_run_with_version(os.path.join(history_dir, name))
            with lock:
                if version is not None:
                    cache[name] = (version, record.get("started_at", ""), bool(record))
                else:
                    cache.pop(name, None)
            if record:
                yield record


def list_runs(history_dir, limit=100):
    """Return newest complete records, reading only requested rows once indexed.

    The first scan (or changed files) loads sorting metadata. Every listing
    checks directory membership and file versions; JSON payloads are not cached.
    """
    try:
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ValueError("limit must be an integer") from exc
    if limit < 1:
        raise ValueError("limit must be positive")
    history_dir = os.path.abspath(history_dir)
    if not os.path.isdir(history_dir):
        return []
    lock, cache = _get_run_sort_cache(history_dir)
    with lock:
        names = [name for name in os.listdir(history_dir)
                 if name.startswith("run_") and name.endswith(".json")]
        live_names = set(names)
        for removed in set(cache) - live_names:
            cache.pop(removed)
        ordered = []
        for name in names:
            path = os.path.join(history_dir, name)
            version = _get_run_file_version(path)
            if version is None:
                cache.pop(name, None)
                continue
            metadata = cache.get(name)
            if metadata is None or metadata[0] != version:
                record, stable_version = _get_run_with_version(path)
                metadata = (stable_version, record.get("started_at", ""), bool(record))
                if stable_version is not None:
                    cache[name] = metadata
                else:
                    cache.pop(name, None)
            if metadata[2]:
                ordered.append((name, metadata[1]))
        ordered.sort(key=lambda item: item[1], reverse=True)
        records = []
        for name, _ in ordered:
            record = safe_load_json(os.path.join(history_dir, name), default={})
            if record:
                records.append(record)
            if len(records) >= min(limit, 500):
                break
        return records


def mark_interrupted_runs(history_dir):
    """Mark records left running by a prior server process as interrupted."""
    changed = []
    for record in _iter_run_records(history_dir):
        if record.get("status") != "running":
            continue
        updated = _apply_run_update(history_dir, record["id"], lambda current: {
            "status": "interrupted", "finished_at": _utc_now(),
            "error": "Server stopped before the run completed.",
            "next_action": "Review the last completed stage and start a new run.",
        } if current.get("status") == "running" else None)
        if updated is not None:
            changed.append(updated)
    return changed


def prune_runs(history_dir, max_count=200, max_age_days=90):
    """Delete expired/excess records while preserving active and newest failed runs."""
    records = list_runs(history_dir, limit=500)
    newest_failed = next((item["id"] for item in records
                          if item.get("status") in ("failed", "interrupted")), None)
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=max_age_days)
    removed = []
    kept_finished = 0
    for record in records:
        protected = record.get("status") == "running" or record.get("id") == newest_failed
        try:
            started = datetime.datetime.fromisoformat(record.get("started_at", ""))
            if started.tzinfo is None:
                raise ValueError("Run timestamp has no timezone")
        except (TypeError, ValueError):
            started = datetime.datetime.min.replace(tzinfo=datetime.timezone.utc)
        expired = started < cutoff
        excessive = kept_finished >= max_count
        if not protected and (expired or excessive):
            safe_id = secure_filename(record.get("id", ""))
            if not safe_id or safe_id != record.get("id"):
                continue
            path = os.path.join(history_dir, f"{safe_id}.json")
            try:
                os.unlink(path)
                removed.append(record["id"])
            except OSError:
                pass
        elif not protected:
            kept_finished += 1
    return removed
