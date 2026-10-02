"""Durable indexed checkpoint changes, including edits and cursor rewinds."""
import copy
import hashlib
import json
from pathlib import Path
import uuid

from generation_checkpoint_shards import (
    get_generation_shard_directory, _get_shard_versions)
from utils import atomic_json_write, file_lock

STORAGE = "indexed-checkpoint-deltas-v1"


def _get_digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
                                    allow_nan=False, separators=(",", ":"))
                          .encode("utf-8")).hexdigest()


def _get_version(path):
    stat = path.stat()
    return (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)


def _get_changes(previous, current):
    changes = {}
    for key, value in current.items():
        old = previous.get(key)
        if key in previous and old == value:
            continue
        if isinstance(old, list) and isinstance(value, list):
            changes[key] = {"length": len(value), "entries": {
                str(index): entry for index, entry in enumerate(value)
                if index >= len(old) or entry != old[index]}}
        else:
            changes[key] = {"value": value}
    return {"fields": changes, "removed": sorted(previous.keys() - current.keys())}


def _apply_changes(previous, changes):
    if (not isinstance(changes, dict) or set(changes) != {"fields", "removed"}
            or not isinstance(changes["fields"], dict)
            or not isinstance(changes["removed"], list)
            or any(not isinstance(key, str) for key in changes["removed"])):
        raise ValueError("Invalid checkpoint delta fields")
    result = copy.deepcopy(previous)
    for key in changes["removed"]:
        if key not in result or key in changes["fields"]:
            raise ValueError("Invalid checkpoint delta removal")
        del result[key]
    for key, change in changes["fields"].items():
        if not isinstance(change, dict):
            raise ValueError("Invalid checkpoint delta change")
        if set(change) == {"value"}:
            result[key] = copy.deepcopy(change["value"])
            continue
        if (set(change) != {"length", "entries"}
                or type(change["length"]) is not int or change["length"] < 0
                or not isinstance(change["entries"], dict)
                or not isinstance(result.get(key), list)):
            raise ValueError("Invalid indexed checkpoint delta")
        old = result[key]
        length = change["length"]
        entries = change["entries"]
        for index in entries:
            if (not index.isascii() or not index.isdigit()
                    or str(int(index)) != index or int(index) >= length):
                raise ValueError("Invalid checkpoint delta index")
        if any(str(index) not in entries for index in range(len(old), length)):
            raise ValueError("Checkpoint delta omits appended entries")
        result[key] = [copy.deepcopy(entries[str(index)]) if str(index) in entries
                       else old[index] for index in range(length)]
    return result


def _load_checkpoint(path):
    with path.open(encoding="utf-8") as stream:
        header = json.load(stream)
    if not isinstance(header, dict):
        raise ValueError("Checkpoint must be an object")
    if header.get("storage") != STORAGE:
        return header, None, None, None
    base = header.get("base")
    if not isinstance(base, dict) or header.get("sha256") != _get_digest(base):
        raise ValueError("Checkpoint base digest mismatch")
    directory = get_generation_shard_directory(path, header)
    versions = _get_shard_versions(directory)
    current, digest = base, header["sha256"]
    for index in range(len(versions)):
        with (directory / f"{index:08d}.json").open(encoding="utf-8") as stream:
            record = json.load(stream)
        if (not isinstance(record, dict)
                or set(record) != {"changes", "previous", "sha256"}
                or record["previous"] != digest
                or record["sha256"] != _get_digest({
                    "changes": record["changes"], "previous": digest})):
            raise ValueError("Checkpoint delta digest mismatch")
        current = _apply_changes(current, record["changes"])
        digest = record["sha256"]
    if versions != _get_shard_versions(directory):
        raise ValueError("Checkpoint deltas changed while reading")
    return current, directory, versions, digest


def load_generation_delta_checkpoint(path, *, locked=False):
    """Replay committed changes; callers already holding the lock say so."""
    if locked:
        return _load_checkpoint(Path(path))[0]
    data = load_generation_checkpoint_document(path)
    if not isinstance(data, dict):
        raise ValueError("Checkpoint must be an object")
    return data


def load_generation_checkpoint_document(path):
    """Read result JSON unchanged, or replay an indexed checkpoint document."""
    # A legacy atomic snapshot needs no replay lock or new lock-file artifact.
    with Path(path).open(encoding="utf-8") as stream:
        header = json.load(stream)
    if not isinstance(header, dict) or header.get("storage") != STORAGE:
        return header
    with file_lock(path):
        return _load_checkpoint(Path(path))[0]


class GenerationCheckpointDeltas:
    """Serialize only changed indexed entries; retain atomic fsync per unit."""
    def __init__(self, path, *, reset=False):
        self.path = Path(path)
        self.reset = reset
        self.current = None
        self.directory = None
        self.versions = None
        self.header_version = None
        self.digest = None

    def save_checkpoint(self, data, *, compact=False):
        if not isinstance(data, dict):
            raise ValueError("Checkpoint must be an object")
        with file_lock(self.path):
            version = _get_version(self.path) if self.path.exists() else None
            if self.reset:
                self.current, self.directory, self.versions, self.digest = None, None, None, None
            elif (self.current is None or version != self.header_version
                    or (self.directory is not None
                        and _get_shard_versions(self.directory) != self.versions)):
                if version is None:
                    self.current, self.directory, self.versions, self.digest = None, None, None, None
                else:
                    self.current, self.directory, self.versions, self.digest = _load_checkpoint(self.path)
            try:
                if compact:
                    atomic_json_write(data, str(self.path))
                    self.directory, self.versions, self.digest = None, None, None
                elif self.directory is None:
                    directory = self.path.with_name(self.path.name + ".parts-" + uuid.uuid4().hex)
                    directory.mkdir()
                    digest = _get_digest(data)
                    atomic_json_write({"storage": STORAGE, "base": data,
                                       "directory": directory.name, "sha256": digest}, str(self.path))
                    self.directory, self.digest = directory, digest
                else:
                    changes = _get_changes(self.current, data)
                    if changes["fields"] or changes["removed"]:
                        record = {"changes": changes, "previous": self.digest}
                        digest = _get_digest(record)
                        atomic_json_write(dict(record, sha256=digest), str(
                            self.directory / f"{len(self.versions):08d}.json"))
                        self.digest = digest
                self.current = copy.deepcopy(data)
                self.reset = False
                self.header_version = _get_version(self.path)
                self.versions = (_get_shard_versions(self.directory)
                                 if self.directory is not None else None)
            except BaseException:
                # An atomic publication may have succeeded before a durability error.
                self.current = None
                raise
