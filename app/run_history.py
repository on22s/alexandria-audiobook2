"""Durable history records for long-running Alexandria tasks."""

import datetime
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
