"""Privately staged, quota-bounded asset uploads owned through import cleanup."""
import os
from pathlib import Path
import stat

from audio_take_history import _ensure_plain_path, _check_cancelled
from sound_design_assets import (SoundDesignAssetStore, MAX_SOURCE_BYTES,
                                 validate_sound_asset_display_name, ensure_sound_asset_lease)
from source_upload_publication import create_source_upload_stage

MAX_STAGING_BYTES = MAX_SOURCE_BYTES
MAX_STAGES = 16
STAGING_DIRECTORY = '.sound_design_uploads'


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
        for path in directory.iterdir():
            _ensure_plain_path(path, store.root)
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Unsafe sound upload files require inspection')
            if path.name == 'quota.lock':
                continue
            if not path.name.startswith('.source-upload-') or not path.name.endswith('.part'):
                raise ValueError('Unknown sound upload files require inspection')
            charged += info.st_size
            count += 1
        available = min(MAX_SOURCE_BYTES, MAX_STAGING_BYTES - charged)
        if available <= 0 or count >= MAX_STAGES:
            raise ValueError('Sound upload staging is full; retained uploads require inspection')
        stage = Path(create_source_upload_stage(directory))
        try:
            written = 0
            with stage.open('wb') as output:
                while block := source.read(65536):
                    _check_cancelled(cancel_check)
                    written += len(block)
                    if written > available:
                        raise ValueError('Sound upload exceeds its storage allowance')
                    if output.write(block) != len(block):
                        raise OSError('Incomplete sound upload write')
                output.flush()
                os.fsync(output.fileno())
            _check_cancelled(cancel_check)
            return store.import_asset(stage.relative_to(store.root).as_posix(),
                                      display_name=display_name, cancel_check=cancel_check)
        finally:
            # Only this request's private stage. Store-owned source/PCM remain
            # charged and recoverable after an incomplete or late publication.
            stage.unlink(missing_ok=True)
