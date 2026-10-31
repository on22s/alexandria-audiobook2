"""Privately staged, quota-bounded asset uploads owned through import cleanup."""
import errno
import os
import stat

from audio_take_history import _ensure_plain_path, _check_cancelled
from sound_design_assets import (SoundDesignAssetStore, MAX_SOURCE_BYTES,
                                 validate_sound_asset_display_name, ensure_sound_asset_lease)
from source_upload_stage import OwnedSourceUploadStage, get_upload_stage_inventory

MAX_STAGING_BYTES = MAX_SOURCE_BYTES
MAX_STAGES = 16
STAGING_DIRECTORY = '.sound_design_uploads'


def _open_sound_upload_stage(directory, available):
    try:
        return OwnedSourceUploadStage(directory, byte_limit=available)
    except OSError as error:
        if error.errno in (errno.ENOTSUP, getattr(errno, 'EOPNOTSUPP', errno.ENOTSUP)):
            raise ValueError('Sound upload staging needs an unnamed-file directory') from error
        if os.name == 'nt' and getattr(error, 'winerror', None) == 32:
            raise ValueError('Sound upload staging has an active owner; retry after it stops') from error
        raise


def _close_sound_upload_stage(stage):
    try:
        stage.close()
    except BaseException as error:
        return error
    return None


def import_sound_asset_upload(root, source, *, display_name='Sound asset', cancel_check=None):
    validate_sound_asset_display_name(display_name)
    store = SoundDesignAssetStore(root)
    directory = _ensure_plain_path(store.root / STAGING_DIRECTORY, store.root)
    directory.mkdir(mode=0o700, exist_ok=True)
    lock = _ensure_plain_path(directory / 'quota.lock', store.root)
    if os.path.lexists(lock):
        info = lock.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1:
            raise ValueError('Unsafe sound upload lease')
    with ensure_sound_asset_lease(directory / 'quota', cancel_check):
        _check_cancelled(cancel_check)
        charged = count = 0
        live = get_upload_stage_inventory(directory)
        count = live['count']
        for path in directory.iterdir():
            _ensure_plain_path(path, store.root)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Unsafe sound upload files require inspection')
            if path.name == 'quota.lock' or path.name in live['names']:
                continue
            if not path.name.startswith('.source-upload-') or not path.name.endswith('.part'):
                raise ValueError('Unknown sound upload files require inspection')
            charged += info.st_size
            count += 1
        available = min(MAX_SOURCE_BYTES, MAX_STAGING_BYTES - charged - live['bytes'])
        if available <= 0 or count >= MAX_STAGES:
            raise ValueError('Sound upload staging is full; retained uploads require inspection')
        stage = _open_sound_upload_stage(directory, available)
        try:
            written = 0
            while block := source.read(65536):
                _check_cancelled(cancel_check)
                written += len(block)
                if written > available:
                    raise ValueError('Sound upload exceeds its storage allowance')
                stage.write(block)
            stage.seal()
            _check_cancelled(cancel_check)
            result = store.import_owned_source(stage, display_name=display_name,
                                              cancel_check=cancel_check)
        except BaseException as error:
            cleanup = _close_sound_upload_stage(stage)
            if cleanup is not None:
                raise error from cleanup
            raise
        cleanup = _close_sound_upload_stage(stage)
        if cleanup is not None:
            raise cleanup
        return result
