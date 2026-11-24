"""Content identities shared by Voice Lab dedup and analysis caches."""

import hashlib
import json
from pathlib import Path

from lora_evidence import get_file_sha256


def get_voice_analysis_model_files(model, savedir):
    """Follow SpeechBrain's collected parameter paths, not guessed filenames."""
    pretrainer = model.hparams.pretrainer
    files = {"hyperparams": Path(savedir) / "hyperparams.yaml"}
    for name in sorted(pretrainer.loadables):
        if not pretrainer.is_loadable(name):
            continue
        if name in pretrainer.is_local:
            path = pretrainer.paths[name]
        elif pretrainer.collect_in is not None:
            path = Path(pretrainer.collect_in) / (name + ".ckpt")
        else:
            raise ValueError(f"embedding model has no collected artifact for {name}")
        files["parameter:" + name] = Path(path)
    if len(files) == 1:
        raise ValueError("embedding model has no collected parameter artifacts")
    for path in files.values():
        get_file_sha256(path)
    return files


def get_voice_analysis_dependency_versions():
    from importlib.metadata import version
    import platform
    import soundfile
    versions = {name: version(name) for name in (
        "speechbrain", "torch", "torchaudio", "numpy", "scipy", "librosa",
        "soundfile", "hyperpyyaml")}
    versions["python"] = platform.python_version()
    versions["libsndfile"] = soundfile.__libsndfile_version__
    return versions


def get_voice_analysis_stage_identity(model, device, zip_paths, stage_file,
                                      sample_count, seed):
    versions = dict(model._alexandria_dependency_versions)
    versions["device"] = str(device)
    return get_voice_analysis_cache_identity(
        zip_paths=zip_paths, model_id=model._alexandria_model_id,
        model_files=model._alexandria_model_files,
        model_state_sha256=model._alexandria_state_sha256,
        stage_files={"analysis": stage_file, "identity": Path(__file__),
                     "file_hash": Path(__file__).with_name("lora_evidence.py")},
        dependency_versions=versions, sample_count=sample_count, seed=seed)


def get_voice_analysis_model_state_sha256(model):
    """Fingerprint loaded tensors and module configuration, without inference."""
    import torch
    state = model.state_dict()
    if not state:
        raise ValueError("loaded embedding model state is required")
    digest = hashlib.sha256()

    def add_field(value):
        data = value.encode("utf-8")
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)

    for name, module in model.named_modules():
        add_field(name)
        add_field(type(module).__module__ + "." + type(module).__qualname__)
        add_field(module.extra_repr())
    for name in sorted(state):
        value = state[name]
        if not torch.is_tensor(value) or value.layout != torch.strided:
            raise ValueError("embedding model state must contain dense tensors")
        add_field(name)
        add_field(str(value.dtype))
        add_field(str(tuple(value.shape)))
        data = value.detach().cpu().contiguous().reshape(-1).view(torch.uint8).numpy().tobytes()
        digest.update(len(data).to_bytes(8, "big"))
        digest.update(data)
    return digest.hexdigest()


def get_voice_analysis_cache_identity(zip_paths, model_id, model_files,
                                      stage_files, dependency_versions,
                                      sample_count, seed, model_state_sha256=None):
    """Fingerprint concrete source/model/code bytes and sampling parameters.

    The loader must supply the artifacts actually used to load its model;
    repository names alone are insufficient. Missing identity is an error,
    never a plausible cache hit. Returned metadata is independent of inputs.
    """
    if not isinstance(model_id, str) or not model_id.strip():
        raise ValueError("embedding model identity is required")
    if (not isinstance(model_state_sha256, str) or len(model_state_sha256) != 64
            or any(char not in "0123456789abcdef" for char in model_state_sha256)):
        raise ValueError("loaded embedding model state fingerprint is required")
    if type(sample_count) is not int or sample_count < 0:
        raise ValueError("sample count must be a nonnegative integer")
    if type(seed) is not int:
        raise ValueError("sampling seed must be an integer")
    if not isinstance(dependency_versions, dict) or not dependency_versions:
        raise ValueError("stage dependency versions are required")
    if any(not isinstance(key, str) or not key or not isinstance(value, str) or not value
           for key, value in dependency_versions.items()):
        raise ValueError("stage dependency versions must be nonempty strings")

    def get_artifact_records(files, label):
        if not isinstance(files, dict) or not files:
            raise ValueError(f"{label} artifact files are required")
        if any(not isinstance(role, str) or not role for role in files):
            raise ValueError(f"{label} artifact roles must be nonempty strings")
        return [{"role": role, "sha256": get_file_sha256(files[role])}
                for role in sorted(files)]

    paths = sorted({str(Path(path).resolve(strict=True)) for path in zip_paths})
    if not paths:
        raise ValueError("source ZIP files are required")
    document = {
        "version": 1,
        "sources": [{"path": path, "sha256": get_file_sha256(path)} for path in paths],
        "model_id": model_id,
        "model_state_sha256": model_state_sha256,
        "model_files": get_artifact_records(model_files, "model"),
        "stage_files": get_artifact_records(stage_files, "stage"),
        "dependency_versions": dict(sorted(dependency_versions.items())),
        "sample_count": sample_count,
        "seed": seed,
    }
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return {"sha256": hashlib.sha256(encoded.encode("utf-8")).hexdigest(), "document": document}


def load_voice_analysis_pickle(path, default):
    """Read a legacy full cache plus durable, ordered checkpoint shards."""
    from utils import file_lock
    path = Path(path)
    with file_lock(path):
        return _load_voice_analysis_pickle_locked(path, default)


def _load_voice_analysis_pickle_file(path, default):
    import os
    import pickle
    if not path.exists():
        return default
    try:
        with path.open("rb") as handle:
            return pickle.load(handle)
    except (EOFError, pickle.UnpicklingError, OSError, ValueError) as exc:
        print(f"Warning: unreadable cache {path}: {exc}; rebuilding")
        try:
            os.replace(path, path.with_suffix(path.suffix + ".corrupt"))
        except OSError:
            pass
        return default


_UNREADABLE_SHARD = object()


def _get_voice_analysis_shard_sequence(name):
    """Return our shard's sequence prefix, or None for a foreign file name."""
    prefix = name.split("-")[0]
    if not (prefix.isascii() and prefix.isdigit()):
        return None
    return int(prefix)


def _quarantine_voice_analysis_shard(shard, reason):
    """Warn and preserve a shard this reader cannot merge; never delete it."""
    import os
    print(f"Warning: invalid cache shard {shard}: {reason}; preserving as .corrupt")
    try:
        os.replace(shard, shard.with_suffix(shard.suffix + ".corrupt"))
    except OSError:
        pass


def _get_voice_analysis_shard_problem(value, record):
    """Return why a readable shard cannot be merged, or None when it is valid."""
    if not isinstance(record, dict):
        return "record is not a mapping"
    if "nested" not in record or "updates" not in record:
        return "record is missing nested/updates"
    if type(record["nested"]) is not bool:
        return "nested must be a boolean"
    updates = record["updates"]
    if not isinstance(updates, dict):
        return "updates must be a mapping"
    if record["nested"]:
        for component, entries in updates.items():
            if not isinstance(entries, dict):
                return f"nested update {component!r} is not a mapping"
            if isinstance(value, dict) and component in value and not isinstance(value[component], dict):
                return f"nested update {component!r} cannot merge with the existing value"
    return None


def _merge_voice_analysis_shards(path, value):
    """Merge durable shards in order; return the value and the shards consumed.

    A shard this reader cannot merge is quarantined beside its siblings rather
    than dropped, so compaction never deletes work it could not read.
    """
    consumed = []
    for shard in sorted(path.with_suffix(path.suffix + ".parts").glob("*.pkl")):
        record = _load_voice_analysis_pickle_file(shard, _UNREADABLE_SHARD)
        if record is _UNREADABLE_SHARD:
            continue  # Already warned about and quarantined by the file reader.
        problem = _get_voice_analysis_shard_problem(value, record)
        if problem is not None:
            _quarantine_voice_analysis_shard(shard, problem)
            continue
        updates = record["updates"]
        value = dict(value) if value is not None else {}
        if record["nested"]:
            for component, entries in updates.items():
                value[component] = {**value.get(component, {}), **entries}
        else:
            value.update(updates)
        consumed.append(shard)
    return value, consumed


def _load_voice_analysis_pickle_locked(path, default):
    value = _load_voice_analysis_pickle_file(path, default)
    return _merge_voice_analysis_shards(path, value)[0]


def save_voice_analysis_pickle(value, path):
    """Atomically publish one pickle; an interrupted write retains its predecessor."""
    import os
    import pickle
    import tempfile
    path = Path(path)
    fd, temp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    temporary = Path(temp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            pickle.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def save_voice_analysis_checkpoint(updates, path, nested=False):
    """Persist only this group's delta, retaining every earlier checkpoint."""
    import time
    import uuid
    from utils import file_lock
    path = Path(path)
    with file_lock(path):
        directory = path.with_suffix(path.suffix + ".parts")
        directory.mkdir(exist_ok=True)
        sequences = (_get_voice_analysis_shard_sequence(part.name)
                     for part in directory.glob("*.pkl"))
        previous = max((sequence for sequence in sequences if sequence is not None), default=0)
        sequence = max(time.time_ns(), previous + 1)
        shard = directory / f"{sequence:020d}-{uuid.uuid4().hex}.pkl"
        save_voice_analysis_pickle({"nested": nested, "updates": updates}, shard)


def compact_voice_analysis_checkpoints(path):
    """Publish the conventional full cache before removing redundant shards."""
    from utils import file_lock
    path = Path(path)
    with file_lock(path):
        directory = path.with_suffix(path.suffix + ".parts")
        shards = sorted(directory.glob("*.pkl"))
        if not shards:
            return
        value = _load_voice_analysis_pickle_file(path, {})
        value, consumed = _merge_voice_analysis_shards(path, value)
        save_voice_analysis_pickle(value, path)
        for shard in consumed:
            shard.unlink(missing_ok=True)
