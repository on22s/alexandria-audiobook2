"""Shared admission for the CLI's durable adapter publication transaction."""
import os
import hashlib
import json
import stat
import tempfile
from pathlib import Path
import shlex

CHECKPOINT_SWAP_JOURNAL = ".checkpoint_swap.json"
NAMING_PUBLICATION_JOURNAL = ".naming_transaction.json"
CLI_PUBLICATION_JOURNAL = ".promotion_transaction.json"
CHECKPOINT_GENERATION_PREFIX = ".checkpoint_generation-"



def get_adapter_checkpoint_generation_journal(adapter_dir):
    adapter = Path(os.path.abspath(adapter_dir))
    adapter = adapter.parent.resolve() / adapter.name
    key = hashlib.sha256(os.path.normcase(str(adapter)).encode()).hexdigest()[:24]
    return adapter.parent / (CHECKPOINT_GENERATION_PREFIX + key + '.json')


def get_adapter_checkpoint_generation_recovery_command(models_dir, ignore_journal=None):
    """Every marker fences the family, including malformed or symlinked evidence."""
    root = Path(models_dir).resolve()
    for journal in sorted(root.glob(CHECKPOINT_GENERATION_PREFIX + '*')):
        if ignore_journal is not None and journal == Path(ignore_journal):
            continue
        try:
            if journal.is_symlink():
                raise ValueError('unsafe journal')
            data = json.loads(journal.read_text())
            name = data.get('adapter') if isinstance(data, dict) else None
            if (not isinstance(name, str) or name in ('', '.', '..')
                    or Path(name).name != name or '/' in name or '\\' in name
                    or get_adapter_checkpoint_generation_journal(root / name) != journal):
                raise ValueError('invalid adapter identity')
        except (OSError, ValueError):
            return 'inspect retained checkpoint generation journal ' + shlex.quote(str(journal))
        return 'app/adapter_checkpoint_transaction.py --recover-output ' + shlex.quote(str(root / name))
    return None


def is_cli_adapter_publication_pending(models_dir):
    """A journal, including an unreadable one, requires owner recovery first."""
    return os.path.lexists(os.path.join(models_dir, CLI_PUBLICATION_JOURNAL))


def is_adapter_checkpoint_recovery_pending(adapter_dir):
    return (os.path.lexists(os.path.join(adapter_dir, CHECKPOINT_SWAP_JOURNAL))
            or os.path.lexists(get_adapter_checkpoint_generation_journal(adapter_dir)))


def is_adapter_naming_recovery_pending(models_dir):
    return os.path.lexists(os.path.join(models_dir, NAMING_PUBLICATION_JOURNAL))


def get_adapter_publication_owner(models_dir):
    if is_cli_adapter_publication_pending(models_dir):
        return "promotion"
    if is_adapter_naming_recovery_pending(models_dir):
        return "naming"
    return None


def get_adapter_publication_recovery_command(models_dir, ignore_checkpoint_journal=None):
    owner = get_adapter_publication_owner(models_dir)
    if owner == "promotion":
        return "promote_adapters.py --recover"
    if owner == "naming":
        return "tools/voice_lab/name_voices.py --recover with the same --manifest and --models-dir"
    return get_adapter_checkpoint_generation_recovery_command(models_dir, ignore_checkpoint_journal)


def sync_adapter_directory(path):
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def sync_adapter_tree(path):
    for directory, _dirs, files in os.walk(path, topdown=False):
        for name in files:
            filename = os.path.join(directory, name)
            if not os.path.islink(filename):
                with open(filename, "r+b" if os.name == "nt" else "rb") as handle:
                    os.fsync(handle.fileno())
        sync_adapter_directory(directory)


def get_adapter_bundle_sha256(root, cancel_check=None):
    """Identify complete original bundles, including preserved assets and links."""
    try:
        root = os.fspath(root)
    except TypeError as error:
        raise ValueError("adapter bundle root is required") from error
    if not isinstance(root, str) or not root:
        raise ValueError("adapter bundle root is required")
    root = os.path.abspath(root)
    rows = []
    artifact_versions = []
    def version(info):
        return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                info.st_ctime_ns, info.st_mode, info.st_nlink)
    def check_cancellation():
        if cancel_check is not None and cancel_check():
            from audio_generation_owner import AudioGenerationCancelled
            raise AudioGenerationCancelled()
    def raise_walk_error(error):
        raise error
    for directory, dirs, files in os.walk(root, onerror=raise_walk_error):
        check_cancellation()
        dirs.sort()
        files.sort()
        directory_before = os.lstat(directory)
        if not stat.S_ISDIR(directory_before.st_mode):
            raise ValueError(f"adapter bundle directory changed: {directory}")
        artifact_versions.append((directory, version(directory_before)))
        rows.append((os.path.relpath(directory, root), "directory",
                     stat.S_IMODE(directory_before.st_mode)))
        for name in dirs + files:
            check_cancellation()
            path = os.path.join(directory, name)
            before = os.lstat(path)
            artifact_versions.append((path, version(before)))
            mode = before.st_mode
            relative = os.path.relpath(path, root)
            if stat.S_ISLNK(mode):
                try:
                    link = os.readlink(path)
                    after = os.lstat(path)
                except OSError as error:
                    raise ValueError(f"adapter bundle artifact changed: {path}") from error
                if version(after) != version(before):
                    raise ValueError(f"adapter bundle artifact changed: {path}")
                rows.append((relative, "link", link))
            elif stat.S_ISREG(mode):
                digest = hashlib.sha256()
                flags = (os.O_RDONLY | getattr(os, "O_BINARY", 0)
                         | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
                fd = os.open(path, flags)
                try:
                    handle = os.fdopen(fd, "rb")
                    fd = None
                    with handle:
                        opened = os.fstat(handle.fileno())
                        if (not stat.S_ISREG(opened.st_mode)
                                or version(opened) != version(before)):
                            raise ValueError(f"adapter bundle artifact changed: {path}")
                        while True:
                            check_cancellation()
                            block = handle.read(1024 * 1024)
                            if not block:
                                break
                            digest.update(block)
                        if version(os.fstat(handle.fileno())) != version(opened):
                            raise ValueError(f"adapter bundle artifact changed: {path}")
                finally:
                    if fd is not None:
                        os.close(fd)
                rows.append((relative, "file", stat.S_IMODE(opened.st_mode), digest.hexdigest()))
            elif not stat.S_ISDIR(mode):
                raise ValueError(f"unsupported adapter bundle artifact: {path}")
        try:
            directory_after = os.lstat(directory)
        except OSError as error:
            raise ValueError(f"adapter bundle directory changed: {directory}") from error
        if version(directory_after) != version(directory_before):
            raise ValueError(f"adapter bundle directory changed: {directory}")
    encoded = json.dumps(rows, ensure_ascii=True).encode("utf-8")
    for path, expected in artifact_versions:
        check_cancellation()
        try:
            current = os.lstat(path)
        except OSError as error:
            raise ValueError(f"adapter bundle artifact changed: {path}") from error
        if version(current) != expected:
            raise ValueError(f"adapter bundle artifact changed: {path}")
    check_cancellation()
    return hashlib.sha256(encoded).hexdigest()


def save_adapter_publication_bytes(path, data, mode=None):
    fd, temporary = tempfile.mkstemp(prefix=".publication-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "wb") as handle:
            if mode is not None:
                os.chmod(temporary, mode)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        sync_adapter_directory(os.path.dirname(path))
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)
