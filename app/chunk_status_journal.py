"""Durable per-UID render updates; callers hold the chunks-file lock."""
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import uuid

from adapter_publication import save_adapter_publication_bytes, sync_adapter_directory
from utils import file_lock

FIELDS = frozenset(("status", "audio_path", "error", "drift"))
logger = logging.getLogger(__name__)


def get_chunk_status_journal_path(chunks_path):
    return os.fspath(chunks_path) + ".status.jsonl"


def remove_chunk_snapshot(chunks_path):
    """Retire generation state when its script has been replaced."""
    removed = False
    with file_lock(chunks_path):
        for path in (Path(chunks_path), Path(get_chunk_status_journal_path(chunks_path))):
            if path.exists():
                path.unlink()
                removed = True
        if removed:
            sync_adapter_directory(str(Path(chunks_path).parent))
    return removed


def _get_version(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns


def _get_json_bytes(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")


class ChunkStatusJournal:
    """Cache only a verified snapshot plus fsynced transitions, bound to a book."""
    def __init__(self, chunks_path, book_identity):
        self.snapshot = Path(chunks_path)
        self.path = Path(get_chunk_status_journal_path(chunks_path))
        self.book_identity = copy.deepcopy(book_identity)
        self._versions = None
        self._rows = None
        self._by_uid = None
        self._sequence = 0
        self._prefix = None
        self._compacted = False

    def _get_versions(self):
        return (_get_version(self.snapshot), _get_version(self.path) if self.path.exists() else None)

    def _load(self):
        versions = self._get_versions()
        if versions == self._versions:
            return
        self._versions = None
        raw = self.snapshot.read_bytes()
        rows = json.loads(raw)
        if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
                or any(not isinstance(row.get("uid"), str) or not row["uid"] for row in rows)
                or len({row["uid"] for row in rows}) != len(rows)):
            raise ValueError("Chunk status snapshot requires unique nonempty UIDs")
        by_uid = {row["uid"]: row for row in rows}
        sequence, prefix, compacted = 0, None, False
        if self.path.exists():
            journal = self.path.read_bytes()
            complete = journal[:journal.rfind(b"\n") + 1]
            lines = complete.splitlines()
            if not lines:
                raise ValueError("Chunk status journal has no committed header")
            records = [json.loads(line) for line in lines]
            header = records.pop(0)
            if (not isinstance(header, dict) or header.get("version") != 1
                    or header.get("book_identity") != self.book_identity):
                raise ValueError("Chunk status journal book identity mismatch")
            digest = hashlib.sha256(raw).hexdigest()
            targets = [record.get("snapshot_sha256") for record in records
                       if isinstance(record, dict) and record.get("type") == "compact"]
            compacted = bool(targets and digest == targets[-1])
            if digest != header.get("snapshot_sha256") and not compacted:
                raise ValueError("Chunk status snapshot changed outside its journal")
            for record in records:
                if not isinstance(record, dict) or record.get("sequence") != sequence + 1:
                    raise ValueError("Invalid chunk status journal sequence")
                sequence += 1
                if record.get("type") == "compact":
                    if record is not records[-1] or not isinstance(record.get("snapshot_sha256"), str):
                        raise ValueError("Invalid chunk status compaction record")
                    if not compacted:
                        logger.warning("Chunk status compaction was not published: %s", self.path)
                        prefix = b"\n".join(lines[:-1]) + b"\n"
                        sequence -= 1
                    continue
                changes, uid = record.get("changes"), record.get("uid")
                if (record.get("type") != "update" or not isinstance(uid, str)
                        or not isinstance(changes, dict) or not changes
                        or not set(changes).issubset(FIELDS)):
                    raise ValueError("Invalid chunk status update record")
                if not compacted:
                    if uid not in by_uid:
                        raise ValueError("Chunk status journal UID is missing")
                    by_uid[uid].update(changes)
            if complete != journal:
                logger.warning("Ignoring uncommitted trailing chunk status bytes: %s", self.path)
                if prefix is None:
                    prefix = complete
        if versions != self._get_versions():
            raise ValueError("Chunk status files changed while reading")
        self._rows, self._by_uid = rows, by_uid
        self._sequence, self._prefix, self._compacted = sequence, prefix, compacted
        self._versions = versions

    def get_rows(self):
        self._load()
        return copy.deepcopy(self._rows)

    def get_row(self, uid):
        self._load()
        return copy.deepcopy(self._by_uid.get(uid))

    def ensure_writable(self):
        """Preserve a torn journal before repairing; finish published compaction."""
        self._load()
        if self._prefix is not None:
            backup = self.path.with_name(self.path.name + ".torn-" + uuid.uuid4().hex)
            save_adapter_publication_bytes(str(backup), self.path.read_bytes())
            save_adapter_publication_bytes(str(self.path), self._prefix)
            self._prefix = None
            self._versions = self._get_versions()
        if self._compacted:
            self.path.unlink()
            sync_adapter_directory(str(self.path.parent))
            self._compacted = False
            self._sequence = 0
            self._versions = self._get_versions()
        if not self.path.exists():
            header = {"version": 1, "book_identity": self.book_identity,
                      "snapshot_sha256": hashlib.sha256(self.snapshot.read_bytes()).hexdigest()}
            save_adapter_publication_bytes(str(self.path), _get_json_bytes(header) + b"\n")
            self._versions = self._get_versions()

    def _append(self, record):
        record = dict(record, sequence=self._sequence + 1)
        payload = _get_json_bytes(record) + b"\n"
        self._versions = None  # a failed append must force recovery on the next read
        with self.path.open("ab") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        self._sequence += 1
        self._versions = self._get_versions()

    def apply_update(self, uid, changes):
        self.ensure_writable()
        if (uid not in self._by_uid or not isinstance(changes, dict) or not changes
                or not set(changes).issubset(FIELDS)):
            raise ValueError("Invalid chunk status update")
        self._append({"type": "update", "uid": uid, "changes": changes})
        self._by_uid[uid].update(copy.deepcopy(changes))
        return copy.deepcopy(self._by_uid[uid])

    def save_compacted(self, rows, save_snapshot=None):
        """Journal the target hash before atomically replacing the snapshot."""
        self.ensure_writable()
        payload = json.dumps(rows, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
        self._append({"type": "compact", "snapshot_sha256": hashlib.sha256(payload).hexdigest()})
        self._versions = None
        if save_snapshot is None:
            save_adapter_publication_bytes(str(self.snapshot), payload)
        else:
            save_snapshot(rows)
        self.path.unlink()
        sync_adapter_directory(str(self.path.parent))
