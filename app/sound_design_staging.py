"""Serialize private mix rendering and charge retained candidates after a crash."""
from contextlib import contextmanager
import os
from pathlib import Path
import re
import stat
import tempfile

from audio_take_history import _ensure_plain_path, _check_cancelled
from sound_design_assets import ensure_sound_asset_lease, MAX_TOTAL_BYTES
from sound_design_cues import _require_int

DIRECTORY = '.sound_design_renders'
MAX_STAGES = 16
MAX_ENTRIES = 4096
MAX_DEPTH = 4
# Worker creation receipts/marker plus one nested-retirement container are
# bounded by 64 KiB. Worker request/chapter metadata has its own output budget.
EXPORT_METADATA_RESERVE = 64 * 1024


def get_sound_render_stage_files(directory, root, *, cancel_check=None):
    """Bounded traversal without following aliases; preserve suspicious entries."""
    files, directories, charged = [], [], 0
    pending = [(Path(directory), 0)]
    entries = 0
    while pending:
        parent, depth = pending.pop()
        _ensure_plain_path(parent, root)
        if not stat.S_ISDIR(parent.lstat().st_mode):
            raise ValueError('Unsafe sound render directory requires inspection')
        directories.append(parent)
        with os.scandir(parent) as children:
            for child in children:
                _check_cancelled(cancel_check)
                entries += 1
                if entries > MAX_ENTRIES:
                    raise ValueError('Too many sound render entries; inspect retained stages')
                path = _ensure_plain_path(Path(child.path), root)
                info = path.lstat()
                if stat.S_ISDIR(info.st_mode):
                    if depth >= MAX_DEPTH:
                        raise ValueError('Sound render nesting exceeds its limit')
                    pending.append((path, depth + 1))
                elif stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    charged += info.st_size
                    if charged > MAX_TOTAL_BYTES:
                        raise ValueError('Sound render staging exceeds its allowance')
                    files.append(path)
                else:
                    raise ValueError('Unsafe sound render file requires inspection')
    return charged, files, directories


@contextmanager
def ensure_sound_render_quota(root, *, cancel_check=None, max_bytes=MAX_TOTAL_BYTES):
    """Hold the existing quota lease for either rendering or candidate metadata.

    Return (directory, remaining bytes, retained stage count). An exactly full
    store can still be inspected, but callers must reserve every new byte.
    """
    _require_int(max_bytes, 1, MAX_TOTAL_BYTES, 'Sound render staging allowance')
    root = Path(root).absolute()
    if root.resolve() != root or not root.is_dir():
        raise ValueError('Sound rendering requires a canonical project root')
    directory = _ensure_plain_path(root / DIRECTORY, root)
    directory.mkdir(mode=0o700, exist_ok=True)
    lock = _ensure_plain_path(directory / 'quota.lock', root)
    if os.path.lexists(lock):
        info = lock.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1:
            raise ValueError('Unsafe sound render lease')
    with ensure_sound_asset_lease(directory / 'quota', cancel_check):
        charged, count = get_sound_render_usage(directory, root, cancel_check=cancel_check,
                                                max_bytes=max_bytes)
        _check_cancelled(cancel_check)
        yield directory, max_bytes - charged, count


def get_sound_render_usage(directory, root, *, cancel_check=None, max_bytes=MAX_TOTAL_BYTES):
    """Charge public candidates and private export slots without reclaiming."""
    charged, count, _ = get_sound_render_inventory(
        directory, root, cancel_check=cancel_check, max_bytes=max_bytes)
    return charged, count


def get_sound_render_inventory(directory, root, *, cancel_check=None, max_bytes=MAX_TOTAL_BYTES):
    """Validate all quota entries and return only public candidate names.

    Private export slots remain charged and counted, never reclaimed or
    presented as candidates. An empty permanent registry is infrastructure.
    """
    from export_staging_retirement import DIRECTORY as REGISTRY, PosixRetirement
    charged = count = 0
    names = []
    with os.scandir(directory) as children:
        for child in children:
            _check_cancelled(cancel_check)
            if child.name == 'quota.lock':
                continue
            if child.name == REGISTRY:
                try:
                    with PosixRetirement(parent=directory, create=False) as registry:
                        for name in registry.slot_names():
                            count += 1
                            if count > MAX_STAGES:
                                raise ValueError('Sound render stages are full; inspect retained candidates')
                            registry.open_slot(name)
                            charged += get_sound_render_stage_files(
                                Path(directory) / REGISTRY / name, root,
                                cancel_check=cancel_check)[0]
                            registry.slot = registry.slot_fd = None
                            if charged > max_bytes:
                                raise ValueError('Sound render staging is full; inspect retained candidates')
                except TimeoutError:
                    raise
                except OSError as error:
                    raise ValueError('Unsafe sound export registry requires inspection') from error
                continue
            count += 1
            if count > MAX_STAGES:
                raise ValueError('Sound render stages are full; inspect retained candidates')
            if re.fullmatch(r'render-[A-Za-z0-9_-]+', child.name) is None:
                raise ValueError('Unknown sound render entry requires inspection')
            names.append(child.name)
            charged += get_sound_render_stage_files(child.path, root, cancel_check=cancel_check)[0]
            if charged > max_bytes:
                raise ValueError('Sound render staging is full; inspect retained candidates')
    return charged, count, sorted(names)


@contextmanager
def ensure_sound_export_stage(root, *, cancel_check=None, max_bytes=MAX_TOTAL_BYTES):
    """Own a recoverable delivery copy under the existing quota lease."""
    from export_staging import create_export_staging
    with ensure_sound_render_quota(root, cancel_check=cancel_check,
                                   max_bytes=max_bytes) as (directory, _, count):
        if count >= MAX_STAGES:
            raise ValueError('Sound render stages are full; inspect retained candidates')
        with create_export_staging(directory, 'sound_delivery') as stage:
            charged, _ = get_sound_render_usage(directory, Path(root).absolute(),
                cancel_check=cancel_check, max_bytes=max_bytes)
            available = max_bytes - charged - EXPORT_METADATA_RESERVE
            if available <= 0:
                raise ValueError('Insufficient quota for sound delivery metadata')
            _check_cancelled(cancel_check)
            yield stage, available


@contextmanager
def ensure_sound_render_stage(root, *, cancel_check=None, max_bytes=MAX_TOTAL_BYTES):
    """Reserve remaining quota until rendering ends; retain successful candidates.

    Writers must enforce the yielded byte allowance before allocating data,
    including anonymous temporary files. This lease serializes all render
    writers; the final size check alone is not an allocation limit. Failed
    renders clean only this call's private stage after validating its contents.
    Old stages remain charged and recoverable, never automatically deleted.
    """
    with ensure_sound_render_quota(root, cancel_check=cancel_check,
                                   max_bytes=max_bytes) as (directory, available, count):
        root = Path(root).absolute()
        if count >= MAX_STAGES:
            raise ValueError('Sound render stages are full; inspect retained candidates')
        if available <= 0:
            raise ValueError('Sound render staging is full; inspect retained candidates')
        _check_cancelled(cancel_check)
        stage = Path(tempfile.mkdtemp(prefix='render-', dir=directory))
        try:
            yield stage, available
            _check_cancelled(cancel_check)
            if get_sound_render_stage_files(stage, root, cancel_check=cancel_check)[0] > available:
                raise ValueError('Sound render exceeded its reserved allowance')
        except BaseException:
            # Refuse cleanup if ownership/path validation fails. Such files
            # remain charged on the next attempt and require explicit inspection.
            _, files, directories = get_sound_render_stage_files(stage, root)
            for path in files:
                path.unlink()
            for path in reversed(directories):
                path.rmdir()
            raise
