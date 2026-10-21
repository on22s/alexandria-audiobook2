"""Bounded identity of actual known local serving inputs.

HF snapshot file symlinks belong to the trusted model-loader domain. They are
not recovery-artifact aliases: project cache IO rejects those separately.
This module records bytes before a load and verifies the admitted file set and
versions after it. An already loaded, unrecorded object cannot be relabeled.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat

MAX_MODEL_FILES = 512
MAX_MODEL_DIRECTORIES = 64
MAX_MODEL_BYTES = 16 * 1024 ** 3
MAX_MODEL_FILE_BYTES = 8 * 1024 ** 3
HASH_BLOCK_BYTES = 1024 * 1024
_REQUIRED_ASSETS = frozenset({
    'config.json', 'generation_config.json', 'preprocessor_config.json',
    'tokenizer_config.json', 'vocab.json', 'merges.txt',
    'speech_tokenizer/config.json', 'speech_tokenizer/preprocessor_config.json',
})


class RecoveryIdentityUnavailable(RuntimeError):
    """An unproven serving dependency; never a successful cache identity."""


def get_recovery_digest(value):
    data = json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')
    return hashlib.sha256(data).hexdigest()


def _get_stat_identity(info):
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


@dataclass(frozen=True)
class _ModelFile:
    name: str
    target: str
    identity: tuple
    name_identity: tuple
    sha256: str = ''


@dataclass(frozen=True)
class RecoveryModelSnapshot:
    root: str
    files: tuple
    sha256: str
    directories: tuple


def _get_model_inventory(root):
    files = []
    total = 0
    directories = [root]
    visited = 0
    directory_versions = {}

    def capture_directory_names(path):
        # HF blobs live outside snapshots. Bind their canonical name chain too,
        # so replacing/restoring a blobs parent cannot hide a different load.
        for parent in (path, *path.parents):
            key = str(parent)
            if key not in directory_versions:
                info = parent.stat()
                if not stat.S_ISDIR(info.st_mode):
                    raise RecoveryIdentityUnavailable('unsupported_model_directory')
                directory_versions[key] = _get_stat_identity(info)
                if len(directory_versions) > MAX_MODEL_DIRECTORIES:
                    raise RecoveryIdentityUnavailable('model_directory_limit')

    while directories:
        directory = directories.pop()
        capture_directory_names(directory)
        visited += 1
        if visited > MAX_MODEL_DIRECTORIES:
            raise RecoveryIdentityUnavailable('model_directory_limit')
        with os.scandir(directory) as entries:
            for entry in entries:
                path = Path(entry.path)
                name = path.relative_to(root).as_posix()
                if len(name) > 1024:
                    raise RecoveryIdentityUnavailable('model_path_limit')
                if entry.is_dir(follow_symlinks=False):
                    directories.append(path)
                    if len(directories) + visited > MAX_MODEL_DIRECTORIES:
                        raise RecoveryIdentityUnavailable('model_directory_limit')
                    continue
                named = path.lstat()
                if stat.S_ISLNK(named.st_mode):
                    # A direct HF snapshot link to a regular blob is supported.
                    # Chained/directory aliases require a larger loader contract.
                    link = Path(os.readlink(path))
                    target = Path(os.path.abspath(link if link.is_absolute() else path.parent / link))
                    if target.is_symlink() or target.resolve(strict=True) != target:
                        raise RecoveryIdentityUnavailable('indirect_model_asset_alias')
                else:
                    target = path
                info = target.stat()
                capture_directory_names(target.parent)
                if (not stat.S_ISREG(info.st_mode) or info.st_size <= 0
                        or info.st_size > MAX_MODEL_FILE_BYTES):
                    raise RecoveryIdentityUnavailable('unsupported_model_asset')
                total += info.st_size
                files.append(_ModelFile(name, str(target), _get_stat_identity(info), _get_stat_identity(named)))
                if len(files) > MAX_MODEL_FILES or total > MAX_MODEL_BYTES:
                    raise RecoveryIdentityUnavailable('model_inventory_limit')
    return (tuple(sorted(files, key=lambda item: item.name)), tuple(sorted(directory_versions.items())))


def _validate_model_inventory(root, files):
    names = {item.name for item in files}
    if not _REQUIRED_ASSETS.issubset(names):
        raise RecoveryIdentityUnavailable('incomplete_local_model')
    # The first supported loader uses conventional files in these two local
    # directories. Config-provided file overrides can make Transformers consume
    # outside assets despite a local from_pretrained directory (notably
    # tokenizer_file and fast_tokenizer_files). Treat that as unproven instead
    # of hashing an incomplete dependency set. Ordinary loading remains valid.
    for item in files:
        if (item.name.rsplit('/', 1)[-1].endswith('config.json')
                and item.name.count('/') <= 1):
            if item.identity[2] > 1024 * 1024:
                raise RecoveryIdentityUnavailable('model_config_limit')
            document = json.loads((root / item.name).read_bytes())
            if not isinstance(document, dict):
                raise RecoveryIdentityUnavailable('invalid_model_config')
            _ensure_conventional_asset_references(document)
        if item.name.rsplit('/', 1)[-1] == 'audio_tokenizer_config.json':
            raise RecoveryIdentityUnavailable('unsupported_audio_tokenizer_reference')
    for prefix in ('', 'speech_tokenizer/'):
        if prefix + 'model.safetensors' in names:
            continue
        index_name = prefix + 'model.safetensors.index.json'
        if index_name not in names:
            raise RecoveryIdentityUnavailable('missing_local_weights')
        path = root / index_name
        if path.stat().st_size > 1024 * 1024:
            raise RecoveryIdentityUnavailable('weight_index_limit')
        document = json.loads(path.read_bytes())
        weight_map = document.get('weight_map') if isinstance(document, dict) else None
        if not isinstance(weight_map, dict) or not weight_map or len(weight_map) > 100000:
            raise RecoveryIdentityUnavailable('invalid_weight_index')
        for filename in weight_map.values():
            if (not isinstance(filename, str) or not filename.endswith('.safetensors')
                    or '/' in filename or '\\' in filename or filename in ('.', '..')
                    or prefix + filename not in names):
                raise RecoveryIdentityUnavailable('escaping_weight_reference')


def _ensure_conventional_asset_references(value, depth=0):
    if depth > 32:
        raise RecoveryIdentityUnavailable('model_config_depth_limit')
    if isinstance(value, dict):
        for key, item in value.items():
            if ((key.endswith(('_file', '_files', '_path', '_paths'))
                 or key in ('auto_map', 'audio_tokenizer', 'custom_pipelines', 'gguf_file'))
                    and item not in (None, '', False, [], {})):
                raise RecoveryIdentityUnavailable('unsupported_model_asset_reference')
            _ensure_conventional_asset_references(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _ensure_conventional_asset_references(item, depth + 1)


def capture_recovery_model_snapshot(local_path, *, check_active=None):
    """Hash a bounded complete local snapshot before a known native model load."""
    root = Path(local_path).resolve(strict=True)
    files, directories = _get_model_inventory(root)
    _validate_model_inventory(root, files)
    captured = []
    for item in files:
        if check_active is not None:
            check_active()
        digest = hashlib.sha256()
        # Resolve the loader name again: retaining an old target alone would
        # miss a symlink change between inventory and actual loader admission.
        if str((root / item.name).resolve(strict=True)) != item.target:
            raise RecoveryIdentityUnavailable('model_asset_changed')
        fd = os.open(item.target, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(fd, 'rb') as handle:
            if _get_stat_identity(os.fstat(handle.fileno())) != item.identity:
                raise RecoveryIdentityUnavailable('model_asset_changed')
            remaining = item.identity[2]
            while remaining:
                if check_active is not None:
                    check_active()
                block = handle.read(min(HASH_BLOCK_BYTES, remaining))
                if not block:
                    raise RecoveryIdentityUnavailable('short_model_asset')
                digest.update(block)
                remaining -= len(block)
            if handle.read(1) or _get_stat_identity(os.fstat(handle.fileno())) != item.identity:
                raise RecoveryIdentityUnavailable('model_asset_changed')
        captured.append(_ModelFile(item.name, item.target, item.identity, item.name_identity, digest.hexdigest()))
    snapshot = RecoveryModelSnapshot(str(root), tuple(captured), get_recovery_digest(
        [{'name': item.name, 'bytes': item.identity[2], 'sha256': item.sha256} for item in captured]), directories)
    ensure_recovery_model_snapshot_current(snapshot)
    return snapshot


def ensure_recovery_model_snapshot_current(snapshot):
    """Prove the known loader's admitted files stayed stable through loading."""
    if type(snapshot) is not RecoveryModelSnapshot:
        raise RecoveryIdentityUnavailable('missing_load_snapshot')
    current, directories = _get_model_inventory(Path(snapshot.root))
    captured = tuple(_ModelFile(item.name, item.target, item.identity, item.name_identity) for item in snapshot.files)
    if current != captured or directories != snapshot.directories:
        raise RecoveryIdentityUnavailable('model_snapshot_changed')


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()
