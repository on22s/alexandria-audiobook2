"""Immutable accepted-chunk shards with an atomically published run header."""
import copy
from contextlib import nullcontext
import hashlib
import json
from pathlib import Path
import re
import shutil
import uuid

from utils import atomic_json_write, file_lock
from adapter_publication import sync_adapter_directory

STORAGE = "accepted-chunk-shards-v1"


def _get_chunk_digest(chunk):
    payload = json.dumps(chunk, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def get_generation_shard_directory(path, header):
    path = Path(path)
    name = header.get("directory")
    prefix = path.name + ".parts-"
    if (not isinstance(name, str) or not name.startswith(prefix)
            or not re.fullmatch(r"[0-9a-f]{32}", name[len(prefix):])):
        raise ValueError("Invalid generation checkpoint shard ownership")
    directory = path.parent / name
    if directory.is_symlink():
        raise ValueError("Generation checkpoint shard directory must not be a symlink")
    return directory


def _get_shard_versions(directory):
    if not directory.is_dir():
        raise ValueError("Generation checkpoint shard directory is missing")
    files = sorted(path for path in directory.iterdir() if not path.name.startswith(".tmp_"))
    versions = []
    for index, path in enumerate(files):
        if path.name != f"{index:08d}.json" or path.is_symlink() or not path.is_file():
            raise ValueError("Generation checkpoint shards are not a contiguous owned prefix")
        stat = path.stat()
        versions.append((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
    return tuple(versions)


def _load_checkpoint(path):
    with Path(path).open(encoding="utf-8") as stream:
        header = json.load(stream)
    if not isinstance(header, dict):
        raise ValueError("Generation checkpoint must be an object")
    if header.get("storage") != STORAGE:
        return header, None, None
    directory = get_generation_shard_directory(path, header)
    versions = _get_shard_versions(directory)
    chunks = []
    for index in range(len(versions)):
        with (directory / f"{index:08d}.json").open(encoding="utf-8") as stream:
            shard = json.load(stream)
        if (not isinstance(shard, dict) or "chunk" not in shard
                or shard.get("sha256") != _get_chunk_digest(shard["chunk"])):
            raise ValueError("Generation checkpoint shard digest mismatch")
        chunks.append(shard["chunk"])
    if _get_shard_versions(directory) != versions:
        raise ValueError("Generation checkpoint shards changed while reading")
    return dict(header, accepted_chunks=chunks), directory, versions


def load_generation_shard_checkpoint(path):
    """Read legacy JSON or all committed immutable chunks without editing either."""
    with file_lock(path):
        return _load_checkpoint(path)[0]


def get_generation_checkpoint_artifacts(path):
    """Enumerate only this checkpoint's UUID-owned shard epochs."""
    path = Path(path)
    artifacts = [str(path)]
    prefix = path.name + ".parts-"
    for candidate in sorted(path.parent.glob(prefix + "*")):
        if not re.fullmatch(r"[0-9a-f]{32}", candidate.name[len(prefix):]):
            continue
        if candidate.is_symlink() or not candidate.is_dir():
            raise ValueError("Invalid generation checkpoint shard ownership")
        artifacts.append(str(candidate))
    return artifacts


def remove_generation_shard_checkpoint(path, *, locked=False):
    """Retire all owned epochs and the header under the canonical lock."""
    with nullcontext() if locked else file_lock(path):
        artifacts = get_generation_checkpoint_artifacts(path)
        Path(path).unlink(missing_ok=True)
        sync_adapter_directory(str(Path(path).parent))
        for artifact in artifacts[1:]:
            shutil.rmtree(artifact)
        sync_adapter_directory(str(Path(path).parent))


class GenerationCheckpointShards:
    """One run's writer; stat-check old shards but serialize only new chunks."""
    def __init__(self, path, fingerprint):
        self.path = Path(path)
        self.fingerprint = copy.deepcopy(fingerprint)
        self.directory = None
        self.chunks = []
        self.versions = None
        self.header_version = None

    def save_chunks(self, chunks):
        with file_lock(self.path):
            stat = self.path.stat() if self.path.exists() else None
            version = ((stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
                       if stat is not None else None)
            if (version != self.header_version or self.directory is None
                    or _get_shard_versions(self.directory) != self.versions):
                self.directory = None
                self.chunks = []
                if version is not None:
                    checkpoint, directory, versions = _load_checkpoint(self.path)
                    if checkpoint.get("fingerprint") == self.fingerprint and directory is not None:
                        self.directory, self.versions = directory, versions
                        self.chunks = checkpoint["accepted_chunks"]
            append = (self.directory is not None and len(chunks) >= len(self.chunks)
                      and chunks[:len(self.chunks)] == self.chunks)
            directory = self.directory if append else self.path.with_name(
                self.path.name + ".parts-" + uuid.uuid4().hex)
            if not append:
                directory.mkdir()
            start = len(self.chunks) if append else 0
            for index in range(start, len(chunks)):
                chunk = copy.deepcopy(chunks[index])
                atomic_json_write({"chunk": chunk, "sha256": _get_chunk_digest(chunk)},
                                  str(directory / f"{index:08d}.json"))
            header = {"storage": STORAGE, "fingerprint": self.fingerprint,
                      "directory": directory.name}
            if not append:
                # A new epoch is invisible until every requested shard is durable.
                atomic_json_write(header, str(self.path))
            self.directory = directory
            if append:
                self.chunks.extend(copy.deepcopy(chunks[start:]))
            else:
                self.chunks = copy.deepcopy(chunks)
            stat = self.path.stat()
            self.header_version = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
            self.versions = _get_shard_versions(directory)
