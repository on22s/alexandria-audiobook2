"""Bounded identity of actual known local serving inputs.

HF snapshot file symlinks belong to the trusted model-loader domain. They are
not recovery-artifact aliases: project cache IO rejects those separately.
This module records bytes before a load and verifies the admitted file set and
versions after it. An already loaded, unrecorded object cannot be relabeled.

Directory binding covers the model root, its subdirectories and the direct parent
of an external blob target. Ancestors above those paths are deliberately not
versioned: creating an unrelated sibling changes their mtime/ctime but no model
binding. Direct file, link and directory replace/restore is still caught, and an
ancestor replacement is caught because the model root under the canonical path
has a different identity. Native load certification additionally requires a live
namespace guard from before hashing through load completion. Its self-move events
catch ancestor rename/restore without binding unrelated sibling timestamps.
Standalone snapshots describe inventory observations; they do not prove that
paths stayed unchanged throughout a load without that guard.
"""
from dataclasses import dataclass
import ctypes
import hashlib
import json
import math
import os
from pathlib import Path
import stat
import sys

MAX_MODEL_FILES = 512
MAX_MODEL_DIRECTORIES = 64
MAX_MODEL_BYTES = 16 * 1024 ** 3
MAX_MODEL_FILE_BYTES = 8 * 1024 ** 3
HASH_BLOCK_BYTES = 1024 * 1024
MAX_MODEL_NAMESPACE_WATCHES = 128
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


class _LinuxStatFs(ctypes.Structure):
    # Linux 64-bit libc statfs ABI. Other word sizes remain unqualified.
    _fields_ = [('kind', ctypes.c_long), ('block_size', ctypes.c_long),
                ('blocks', ctypes.c_ulong), ('free', ctypes.c_ulong),
                ('available', ctypes.c_ulong), ('files', ctypes.c_ulong),
                ('free_files', ctypes.c_ulong), ('fsid', ctypes.c_int * 2),
                ('name_length', ctypes.c_long), ('fragment_size', ctypes.c_long),
                ('flags', ctypes.c_long), ('spare', ctypes.c_long * 4)]


def _ensure_private_model_asset(info):
    if (info.st_uid not in (0, os.geteuid()) or info.st_mode & 0o022
            or info.st_ctime_ns % 1_000_000_000 == 0):
        raise RecoveryIdentityUnavailable('unqualified_model_asset_permissions_or_clock')


def _ensure_local_model_filesystem(descriptor, inspect_filesystem):
    info = _LinuxStatFs()
    if inspect_filesystem(descriptor, ctypes.byref(info)) != 0:
        raise OSError(ctypes.get_errno(), 'Model filesystem inspection unavailable')
    # btrfs, XFS, tmpfs and ext-family local filesystems. Network/FUSE and
    # opaque stacked filesystems cannot prove local self-move notifications.
    if info.kind not in (0x9123683e, 0x58465342, 0x1021994, 0xef53):
        raise RecoveryIdentityUnavailable('unsupported_model_namespace_filesystem')


class RecoveryModelNamespaceGuard:
    """Observe directory moves across a native load, without watching siblings.

    Existing file/link ctime and inventory checks still bind individual assets.
    This Linux-only lease covers transient moves of their ancestors. Watches
    attach through directory descriptors, walking actual parents rather than
    reopening an ancestor pathname that may have moved. Any event, including
    watch removal or queue overflow, permanently refuses the load certificate.
    Closing the descriptor also releases all kernel watches after process death.
    """
    def __init__(self, local_path, *, check_active=None):
        self.root = Path(local_path).resolve(strict=True)
        self.check_active = check_active
        self.fd = None
        self.dirty = False
        self.snapshot = None

    def __enter__(self):
        if (self.fd is not None or self.dirty or sys.platform != 'linux'
                or ctypes.sizeof(ctypes.c_long) != 8):
            raise RecoveryIdentityUnavailable('unsupported_model_namespace_guard')
        self.files, self.directories = _get_model_inventory(self.root)
        library = ctypes.CDLL(None, use_errno=True)
        try:
            initialize = library.inotify_init1
            watch = library.inotify_add_watch
            inspect_filesystem = library.fstatfs
        except AttributeError as error:
            raise RecoveryIdentityUnavailable('unsupported_model_namespace_guard') from error
        initialize.argtypes = [ctypes.c_int]
        initialize.restype = ctypes.c_int
        watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
        watch.restype = ctypes.c_int
        inspect_filesystem.argtypes = [ctypes.c_int, ctypes.POINTER(_LinuxStatFs)]
        inspect_filesystem.restype = ctypes.c_int
        descriptor = initialize(os.O_NONBLOCK | os.O_CLOEXEC)
        if descriptor < 0:
            raise OSError(ctypes.get_errno(), 'Model namespace observer unavailable')
        self.fd = descriptor
        try:
            seen = set()
            # IN_MOVE_SELF | IN_DELETE_SELF | IN_UNMOUNT | IN_ONLYDIR.
            # IN_IGNORED and IN_Q_OVERFLOW are delivered automatically.
            mask = 0x800 | 0x400 | 0x2000 | 0x1000000
            for item in self.files:
                if self.check_active is not None:
                    self.check_active()
                asset = os.open(item.target, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    info = os.fstat(asset)
                    if _get_stat_identity(info) != item.identity:
                        raise RecoveryIdentityUnavailable('model_namespace_changed')
                    _ensure_private_model_asset(info)
                    _ensure_local_model_filesystem(asset, inspect_filesystem)
                finally:
                    os.close(asset)
            for path, expected in self.directories:
                directory = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC)
                try:
                    if _get_stat_identity(os.fstat(directory)) != expected:
                        raise RecoveryIdentityUnavailable('model_namespace_changed')
                    _ensure_private_model_asset(os.fstat(directory))
                    while True:
                        if self.check_active is not None:
                            self.check_active()
                        info = os.fstat(directory)
                        key = (info.st_dev, info.st_ino)
                        if key in seen:
                            break
                        if (info.st_uid not in (0, os.geteuid())
                                or (info.st_mode & 0o022
                                    and not (info.st_uid == 0 and info.st_mode & stat.S_ISVTX))):
                            raise RecoveryIdentityUnavailable('unqualified_model_namespace_permissions')
                        if len(seen) >= MAX_MODEL_NAMESPACE_WATCHES:
                            raise RecoveryIdentityUnavailable('model_namespace_limit')
                        _ensure_local_model_filesystem(directory, inspect_filesystem)
                        if watch(self.fd, os.fsencode(f'/proc/self/fd/{directory}'), mask) < 0:
                            raise OSError(ctypes.get_errno(), 'Model namespace watch unavailable')
                        seen.add(key)
                        parent = os.open('..', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC,
                                         dir_fd=directory)
                        previous, directory = directory, parent
                        os.close(previous)
                finally:
                    os.close(directory)
            self.check()
            return self
        except BaseException:
            self.__exit__(*sys.exc_info())
            raise

    def bind(self, snapshot):
        if type(snapshot) is not RecoveryModelSnapshot:
            raise RecoveryIdentityUnavailable('missing_load_snapshot')
        files = tuple(_ModelFile(item.name, item.target, item.identity, item.name_identity)
                      for item in snapshot.files)
        if (snapshot.root != str(self.root) or files != self.files
                or snapshot.directories != self.directories):
            # A changed HF link could introduce an unwatched external target.
            raise RecoveryIdentityUnavailable('model_namespace_inventory_changed')
        self.check()
        self.snapshot = snapshot

    def check(self):
        if self.fd is None or self.dirty:
            raise RecoveryIdentityUnavailable('model_namespace_changed_or_closed')
        if self.check_active is not None:
            self.check_active()
        try:
            os.read(self.fd, 4096)
        except BlockingIOError:
            return
        except BaseException:
            self.dirty = True
            raise
        self.dirty = True
        raise RecoveryIdentityUnavailable('model_namespace_changed_or_closed')

    def __exit__(self, *error):
        self.dirty = True
        if self.fd is not None:
            descriptor, self.fd = self.fd, None
            os.close(descriptor)


def _get_model_inventory(root):
    files = []
    total = 0
    directories = [root]
    visited = 0
    directory_versions = {}

    def record_directory(path):
        # Bind the directories that name admitted assets, not every ancestor.
        # An ancestor's mtime/ctime changes when an unrelated sibling is made;
        # the model root/subdirectories and a private external target parent
        # still bind replacement and rename/restore at the paths that matter.
        key = str(path)
        if key in directory_versions:
            return
        info = path.stat()
        if not stat.S_ISDIR(info.st_mode):
            raise RecoveryIdentityUnavailable('unsupported_model_directory')
        directory_versions[key] = _get_stat_identity(info)
        if len(directory_versions) > MAX_MODEL_DIRECTORIES:
            raise RecoveryIdentityUnavailable('model_directory_limit')

    def record_external_parent(path):
        # A blob's direct parent names the blob. Shared ancestors (including the
        # model root itself) are not recorded for outside targets: they would
        # bind unrelated sibling contents. The target file identity and hash
        # still bind the bytes, and a private parent still catches rename/restore.
        try:
            path.relative_to(root)
        except ValueError:
            if root.is_relative_to(path):
                return
            record_directory(path)

    while directories:
        directory = directories.pop()
        record_directory(directory)
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
                record_external_parent(target.parent)
                if (not stat.S_ISREG(info.st_mode) or info.st_size <= 0
                        or info.st_size > MAX_MODEL_FILE_BYTES):
                    raise RecoveryIdentityUnavailable('unsupported_model_asset')
                total += info.st_size
                files.append(_ModelFile(name, str(target), _get_stat_identity(info), _get_stat_identity(named)))
                if len(files) > MAX_MODEL_FILES or total > MAX_MODEL_BYTES:
                    raise RecoveryIdentityUnavailable('model_inventory_limit')
    return (tuple(sorted(files, key=lambda item: item.name)), tuple(sorted(directory_versions.items())))


def _get_bounded_model_json(path):
    """Read model metadata through a bounded regular-file descriptor."""
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
    try:
        # Keep raw ownership even if constructing the buffered wrapper fails.
        with os.fdopen(fd, 'rb', closefd=False) as handle:
            before = os.fstat(handle.fileno())
            if not stat.S_ISREG(before.st_mode) or before.st_size > 1024 * 1024:
                raise RecoveryIdentityUnavailable('model_metadata_limit')
            data = handle.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024 or _get_stat_identity(os.fstat(handle.fileno())) != _get_stat_identity(before):
                raise RecoveryIdentityUnavailable('model_metadata_changed')
    finally:
        os.close(fd)
    def unique(items):
        value = {}
        for key, item in items:
            if key in value:
                raise ValueError('Duplicate model metadata field')
            value[key] = item
        return value

    def reject_constant(value):
        raise ValueError('Nonfinite model metadata')

    def finite_float(text):
        value = float(text)
        if not math.isfinite(value):
            raise ValueError('Nonfinite model metadata')
        return value

    def bounded_int(text):
        # Also bound work on older Python 3.10 builds or when its global
        # integer-conversion limit is disabled. HF's large tokenizer sentinel
        # (about 10**30) remains well below this metadata-only limit.
        if len(text) - text.startswith('-') > 128:
            raise ValueError('Model metadata integer limit')
        return int(text)

    return json.loads(data, object_pairs_hook=unique, parse_constant=reject_constant,
                      parse_float=finite_float, parse_int=bounded_int)


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
            try:
                document = _get_bounded_model_json(root / item.name)
            except (ValueError, RecursionError) as error:
                raise RecoveryIdentityUnavailable('invalid_model_config') from error
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
        try:
            document = _get_bounded_model_json(path)
        except (ValueError, RecursionError) as error:
            raise RecoveryIdentityUnavailable('invalid_weight_index') from error
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


def get_model_asset_observation(local_path):
    """Observe required local assets, without reading weights or proving bytes.

    This reuses the known loader inventory limits and required-file contract.
    A successful observation is not a native-load or recovery certificate.
    """
    root = Path(local_path).resolve(strict=True)
    files, directories = _get_model_inventory(root)
    _validate_model_inventory(root, files)
    if _get_model_inventory(root) != (files, directories):
        raise RecoveryIdentityUnavailable('model_asset_changed')
    revision = root.name
    if len(revision) not in (40, 64) or any(c not in '0123456789abcdef' for c in revision):
        revision = None
    return {'state': 'required_assets_observed', 'snapshot_revision': revision,
            'files': len(files), 'bytes': sum(item.identity[2] for item in files),
            'content_sha256': None, 'scope': 'local_metadata_only_not_load_or_content_proof'}


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
    """Check the captured inventory now; native loads also require a live fence."""
    if type(snapshot) is not RecoveryModelSnapshot:
        raise RecoveryIdentityUnavailable('missing_load_snapshot')
    current, directories = _get_model_inventory(Path(snapshot.root))
    captured = tuple(_ModelFile(item.name, item.target, item.identity, item.name_identity) for item in snapshot.files)
    if current != captured or directories != snapshot.directories:
        raise RecoveryIdentityUnavailable('model_snapshot_changed')


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()
