"""Durable history records for long-running Alexandria tasks."""

import datetime
import hashlib
import os

from utils import atomic_json_write, get_unique_id, safe_load_json, secure_filename


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


def finish_run(history_dir, run_id, status, error=None):
    """Finish an existing task record without changing its identity/start time."""
    safe_id = secure_filename(run_id)
    path = os.path.join(history_dir, f"{safe_id}.json")
    record = safe_load_json(path, default={})
    if not record or record.get("id") != run_id:
        raise FileNotFoundError(f"Run history record not found: {run_id}")
    record = {**record, "status": status, "finished_at": _utc_now(), "error": error}
    atomic_json_write(record, path)
    return record


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as source:
        for block in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def record_artifact(history_dir, run_id, artifact_path, kind, data_dir,
                    source_paths=(), config_path=None):
    """Hash an output and its declared inputs, then append it to a run record."""
    record = get_run(history_dir, run_id)
    if record is None:
        raise FileNotFoundError(f"Run history record not found: {run_id}")
    artifact_path = os.path.abspath(artifact_path)
    if not os.path.isfile(artifact_path):
        raise FileNotFoundError(f"Artifact not found: {artifact_path}")

    def describe(path):
        absolute = os.path.abspath(path)
        return {
            "path": os.path.relpath(absolute, data_dir),
            "sha256": _sha256_file(absolute),
            "size_bytes": os.path.getsize(absolute),
        }

    artifact = {
        **describe(artifact_path),
        "kind": kind,
        "recorded_at": _utc_now(),
        "sources": [describe(path) for path in source_paths if os.path.isfile(path)],
        "config": describe(config_path) if config_path and os.path.isfile(config_path) else None,
    }
    record["artifacts"] = [*record.get("artifacts", []), artifact]
    atomic_json_write(record, os.path.join(history_dir, f"{run_id}.json"))
    return artifact


def get_run(history_dir, run_id):
    """Return one run record, or None when its safe id does not exist."""
    safe_id = secure_filename(run_id)
    if not safe_id or safe_id != run_id:
        return None
    record = safe_load_json(os.path.join(history_dir, f"{safe_id}.json"), default={})
    return record or None


def list_runs(history_dir, limit=100):
    """Return newest run records first, bounded for API/UI callers."""
    if not os.path.isdir(history_dir):
        return []
    records = []
    for name in os.listdir(history_dir):
        if not name.startswith("run_") or not name.endswith(".json"):
            continue
        record = safe_load_json(os.path.join(history_dir, name), default={})
        if record:
            records.append(record)
    records.sort(key=lambda item: item.get("started_at", ""), reverse=True)
    return records[:max(1, min(int(limit), 500))]
