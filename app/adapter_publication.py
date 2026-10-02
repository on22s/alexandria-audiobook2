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
                with open(filename, "rb") as handle:
                    os.fsync(handle.fileno())
        sync_adapter_directory(directory)


def get_adapter_bundle_sha256(root):
    """Identify complete original bundles, including preserved assets and links."""
    rows = []
    for directory, dirs, files in os.walk(root):
        dirs.sort()
        files.sort()
        rows.append((os.path.relpath(directory, root), "directory",
                     stat.S_IMODE(os.lstat(directory).st_mode)))
        for name in dirs + files:
            path = os.path.join(directory, name)
            mode = os.lstat(path).st_mode
            relative = os.path.relpath(path, root)
            if stat.S_ISLNK(mode):
                rows.append((relative, "link", os.readlink(path)))
            elif stat.S_ISREG(mode):
                digest = hashlib.sha256()
                with open(path, "rb") as handle:
                    for block in iter(lambda: handle.read(1024 * 1024), b""):
                        digest.update(block)
                rows.append((relative, "file", stat.S_IMODE(mode), digest.hexdigest()))
            elif not stat.S_ISDIR(mode):
                raise ValueError(f"unsupported adapter bundle artifact: {path}")
    return hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode("utf-8")).hexdigest()


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
