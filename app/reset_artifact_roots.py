"""The one Reset artifact set for the active book (#2184).

Launcher Reset delegates its book cleanup here so the names it removes come
from the same constants the backend uses to scope one book, not a second
hand-maintained JavaScript list. Recovery and removal share one
``ensure_book_state`` lease: a refused or corrupt journal stops Reset before
any entry is considered. Saved books, the voice library, uploads, adapters,
the shared lexicon and take history are deliberately outside this set.

Known checkpoint damage is refused before any deletion. A genuine per-entry
I/O failure is reported in ``failed`` while independent removals continue;
receipts stay in place while any export they fence is retained, so a retry
after the obstacle clears can finish the remaining work.
"""
import json
import os
from pathlib import Path
import shutil

from adapter_publication import sync_adapter_directory
from book_recovery_inventory import CHECKPOINTS, STATIC_ARTIFACTS
from direct_path_admission import (get_canonical_plain_path,
                                   get_direct_directory_root, is_plain_path)
from export_file_io import (EXPORT_RETIREMENT_DIRECTORY, PUBLICATION_JOURNAL,
                            PUBLICATION_SET_JOURNAL)
from generation_checkpoint_shards import get_generation_checkpoint_artifacts
from project_scope_exports import EXPORTS, PUBLICATIONS_FILE
from utils import file_lock, get_app_config_path


# Shared take history is retained across books; the rest of STATIC_ARTIFACTS
# is the active book's own state. CHECKPOINTS carry their own shard epochs.
STATE_FILES = tuple(name for name in STATIC_ARTIFACTS
                    if not name.startswith('take_history/')) + CHECKPOINTS
# Recent export receipt handling recovers these before Reset may clear them.
RECEIPT_FILES = (PUBLICATIONS_FILE, PUBLICATION_JOURNAL, PUBLICATION_SET_JOURNAL)
FILE_ARTIFACTS = (STATE_FILES
                  + tuple(name for name in EXPORTS if name != 'chapter_exports')
                  + RECEIPT_FILES)
DIRECTORY_ARTIFACTS = ('chapter_exports', EXPORT_RETIREMENT_DIRECTORY, 'voicelines')
OWNED_NAMES = frozenset((*FILE_ARTIFACTS, *DIRECTORY_ARTIFACTS))
EXPORT_NAMES = frozenset(EXPORTS)
RECEIPT_NAMES = frozenset((*RECEIPT_FILES, EXPORT_RETIREMENT_DIRECTORY))
# state.json carries the scope inventory and book identity the export reader
# uses to fence surviving deliveries; the rest of the book state is kept with
# it so a partial cleanup cannot hand an old export back as current.
FENCE_AUTHORITY = frozenset(STATE_FILES)


def get_reset_root_descriptor(launcher_dir=None):
    """-> the one admitted active data root, from the runtime resolver."""
    from book_state_recovery import get_default_data_dir
    try:
        canonical, _identity = get_direct_directory_root(get_default_data_dir())
    except (OSError, ValueError) as error:
        raise ValueError('Active data root is missing or aliased; reset refused.') from error
    return {'root': str(canonical)}


def _inside(root, path):
    try:
        return os.path.commonpath((str(root), str(path))) == str(root)
    except ValueError:
        return False


def _relative(root, path):
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return str(path)


def _validate(root, path, config, *, directory=False):
    """Whether this exact entry is a plain, correctly typed owned artifact."""
    if path == Path(config):
        if not _inside(root, path) or not is_plain_path(path):
            return False
    elif (path.parent != root or path.name not in OWNED_NAMES
          or not _inside(root, path) or not is_plain_path(path)):
        return False
    try:
        if get_canonical_plain_path(path) != path:
            return False
    except (OSError, ValueError):
        return False
    if path.is_symlink() or not path.exists():
        return False
    return path.is_dir() if directory else path.is_file()


def _planned_removals(root, config):
    """-> (path, kind) pairs; names come only from canonical backend sets."""
    kinds = {name: 'checkpoint' for name in CHECKPOINTS}
    entries = [(Path(root) / name, kinds.get(name, 'file')) for name in FILE_ARTIFACTS]
    entries += [(Path(root) / name, 'dir') for name in DIRECTORY_ARTIFACTS]
    entries.append((Path(config), 'config'))
    # Receipts last: they must not be cleared while an export they fence
    # is still on disk because its own removal failed or was refused.
    return sorted(entries, key=lambda entry: _relative(root, entry[0]) in RECEIPT_NAMES)


def _preflight_checkpoints(root):
    """Reject malformed shard ownership before any destructive work."""
    for name in CHECKPOINTS:
        path = root / name
        if path.exists() or path.is_symlink():
            get_generation_checkpoint_artifacts(path)


def _remove_checkpoint(path):
    """Retire owned shard epochs first, keeping the header as retry evidence.

    The canonical helper deletes the header before its shards; a failed shard
    removal then leaves no header for the next Reset to schedule. Enumeration
    still comes from get_generation_checkpoint_artifacts, so only the hex-named
    dirs owned by this checkpoint are touched.
    """
    # Standalone checkpoint writers use this same lock without a book lease.
    with file_lock(path):
        artifacts = get_generation_checkpoint_artifacts(path)
        for shard in artifacts[1:]:
            shutil.rmtree(shard)
        sync_adapter_directory(str(path.parent))
        Path(artifacts[0]).unlink(missing_ok=True)
        sync_adapter_directory(str(path.parent))


def reset_active_book(launcher_dir=None):
    """Recover and clear only owned entries under one admitted book lease."""
    from book_state_transaction import ensure_book_state

    launcher = Path(launcher_dir or Path(__file__).resolve().parent.parent).resolve()
    root = Path(get_reset_root_descriptor(str(launcher))['root'])
    config = get_app_config_path(str(root), str(launcher), str(launcher / 'app'))
    with ensure_book_state(str(root)):
        # Decide on every entry first: a suspicious entry is preserved and
        # the remaining removals still proceed, but nothing is removed on the
        # strength of a name that failed admission.
        removable, skipped, details = [], [], {}
        for path, kind in _planned_removals(root, config):
            name = _relative(root, path)
            if _validate(root, path, config, directory=(kind == 'dir')):
                removable.append((path, kind))
            elif path.exists() or path.is_symlink():
                skipped.append(name)
                details[name] = 'unsupported or aliased artifact'
        _preflight_checkpoints(root)

        removed, failed = [], []

        def perform(path, kind):
            name = _relative(root, path)
            try:
                if kind == 'dir':
                    shutil.rmtree(path)
                elif kind == 'checkpoint':
                    _remove_checkpoint(path)
                else:
                    path.unlink()
            except (OSError, ValueError) as error:
                failed.append(name)
                details[name] = f'{type(error).__name__}: {error}'
            else:
                removed.append(name)

        def hold(name):
            skipped.append(name)
            details[name] = 'fence retained: ' + ', '.join(retained)

        # Exports first: only their actual survivors decide whether the book's
        # fence authority may be cleared.
        for path, kind in removable:
            if _relative(root, path) in EXPORT_NAMES:
                perform(path, kind)
        retained = sorted(name for name in EXPORT_NAMES if (root / name).exists())

        for path, kind in removable:
            name = _relative(root, path)
            if name in EXPORT_NAMES or name in RECEIPT_NAMES:
                continue
            if retained and name in FENCE_AUTHORITY:
                hold(name)
            else:
                perform(path, kind)

        for path, kind in removable:
            name = _relative(root, path)
            if name not in RECEIPT_NAMES:
                continue
            if retained:
                hold(name)
            else:
                perform(path, kind)

    return {'root': str(root), 'removed': sorted(removed), 'skipped': sorted(skipped),
            'failed': sorted(failed), 'details': details, 'retained_exports': retained}


def print_reset_result(launcher_dir=None):
    print(json.dumps(reset_active_book(launcher_dir), sort_keys=True))


if __name__ == '__main__':
    print_reset_result()
