"""Bounded snapshots of files referenced by retained effective voices."""
import os
from pathlib import Path
from audio_take_history import _ensure_plain_path, _version, _check_cancelled
from project_scope_files import capture_scope_file
from tts import _resolve_asset_path
from voice_manifest import get_resolved_adapter_path

MAX_ASSET_BYTES = 1024**3
MAX_ASSET_NODES = 4096


def _raise_scan_error(error):
    raise error


def get_scope_asset_snapshot(path, *, cancel_check=None):
    path = Path(os.path.abspath(path))
    if os.path.normcase(os.path.realpath(path)) != os.path.normcase(str(path)):
        raise ValueError('Scope voice artifacts cannot use directory or file aliases.')
    if not path.is_dir():
        proof, _ = capture_scope_file(path.parent, path.name, limit=MAX_ASSET_BYTES,
                                      cancel_check=cancel_check)
        if not proof['exists'] or not proof['size']:
            raise ValueError('A retained voice artifact is missing.')
        return {'absolute_path':str(path), 'kind':'file', 'proof':proof}
    directories, files, total = {}, [], 0
    for directory, names, filenames in os.walk(path, onerror=_raise_scan_error):
        _check_cancelled(cancel_check)
        directory = _ensure_plain_path(directory, path)
        directories[directory.relative_to(path).as_posix()] = list(_version(directory.stat()))
        if len(directories) + len(files) + len(names) + len(filenames) > MAX_ASSET_NODES:
            raise ValueError('Retained voice bundle exceeds the scope file-count limit.')
        for name in names:
            _ensure_plain_path(directory/name, path)
        for name in sorted(filenames):
            proof, _ = capture_scope_file(path, (directory/name).relative_to(path).as_posix(),
                                          limit=MAX_ASSET_BYTES, cancel_check=cancel_check)
            if not proof['exists']:
                raise ValueError('Retained voice bundle changed during reading.')
            total += proof['size']
            if total > MAX_ASSET_BYTES:
                raise ValueError('Retained voice bundle exceeds the 1 GiB scope limit.')
            files.append(proof)
    for relative, version in directories.items():
        if list(_version(_ensure_plain_path(path/relative, path).stat())) != version:
            raise ValueError('Retained voice bundle changed during reading.')
    if not files:
        raise ValueError('Retained voice adapter has no verifiable files.')
    return {'absolute_path':str(path), 'kind':'directory', 'directories':directories,
            'files':sorted(files, key=lambda proof:proof['path'])}


def get_scope_voice_artifacts(voice_plan, *, cancel_check=None):
    paths = set()
    for row in voice_plan['retained']:
        for voice in row['effective_voices'].values():
            for key in ('ref_audio','adapter_path'):
                raw = voice.get(key)
                if raw is None or raw == '':
                    continue
                if not isinstance(raw, str):
                    raise ValueError('Retained voice artifact path needs inspection.')
                path = raw if os.path.isabs(raw) else _resolve_asset_path(raw)
                paths.add(get_resolved_adapter_path(path) if key == 'adapter_path' else path)
    return [get_scope_asset_snapshot(path, cancel_check=cancel_check) for path in sorted(paths)]
