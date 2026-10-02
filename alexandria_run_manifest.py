"""Run ownership and phase-artifact validation for the Alexandria preparer."""

import hashlib
import fcntl
import json
import os
import re
import tempfile
from pathlib import Path


MANIFEST_NAME = '.run_manifest.json'
GENERATED_NAMES = frozenset({
    'asr_segments.json', 'audio_24k_scratch.wav', 'enriched_segments.json',
    'asr_chunks_for_enrich.json', 'diarization.json', 'metadata.jsonl',
    '.source', MANIFEST_NAME,
})
SAMPLE_NAME = re.compile(r'sample_[0-9]+\.wav\Z')
LOCK_ENV = 'ALEXANDRIA_PREPARER_LOCK_FD'


class RunStateError(ValueError):
    """A preparer run cannot safely reuse its working directory."""


def acquire_run_lock(temp_dir):
    """Lock the shared work directory across the parent and its phase children."""
    lock_path = Path(temp_dir).absolute().with_name('.alexandria_preparer.lock')
    inherited = os.environ.get(LOCK_ENV)
    if inherited is not None:
        try:
            fd = int(inherited)
            actual = os.fstat(fd)
            expected = lock_path.stat()
        except (OSError, ValueError) as error:
            raise RunStateError('Invalid inherited preparer lock') from error
        if (actual.st_dev, actual.st_ino) != (expected.st_dev, expected.st_ino):
            raise RunStateError('Inherited preparer lock points at another file')
        return fd
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(fd)
        raise RunStateError('Another preparer run owns dataset_temp') from error
    return fd


def get_file_identity(path):
    resolved = Path(path).resolve(strict=True)
    digest = hashlib.sha256()
    with resolved.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return {'path': str(resolved), 'sha256': digest.hexdigest()}


def get_run_identity(args):
    """Read content and effective options once per process, before phase reuse."""
    options = {name: value for name, value in vars(args).items()
               if name not in {'phase', 'resume', 'hf_token', 'audio', 'source'}
               and (name not in {'alignment_report', 'summary_output'} or value is not None)}
    return {
        'audio': get_file_identity(args.audio),
        'source': get_file_identity(args.source) if args.source else None,
        'options': options,
    }


def write_json_atomic(data, path):
    """Replace one generated JSON artifact only after serialization succeeds."""
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile('w', encoding='utf-8',
                                         dir=destination.parent,
                                         prefix=f'.{destination.name}.',
                                         suffix='.tmp', delete=False) as stream:
            temporary = Path(stream.name)
            json.dump(data, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(destination)
        directory_fd = os.open(destination.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def get_manifest(temp_dir):
    path = Path(temp_dir) / MANIFEST_NAME
    if not path.exists():
        return None
    try:
        manifest = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as error:
        raise RunStateError(f'Unreadable run manifest {path}: {error}') from error
    if not isinstance(manifest, dict) or not isinstance(manifest.get('artifacts'), dict):
        raise RunStateError(f'Invalid run manifest structure: {path}')
    return manifest


def ensure_run_manifest(temp_dir, identity, *, fresh):
    """Start a fresh run or require an exact manifest match on resume/phase use."""
    directory = Path(temp_dir)
    if directory.is_symlink():
        raise RunStateError(f'Work directory must not be a symlink: {directory}')
    directory.mkdir(parents=True, exist_ok=True)
    if fresh:
        for child in directory.iterdir():
            if child.name in GENERATED_NAMES or SAMPLE_NAME.fullmatch(child.name):
                if child.is_dir() and not child.is_symlink():
                    raise RunStateError(f'Generated path is unexpectedly a directory: {child}')
                child.unlink()
        manifest = {'identity': identity, 'artifacts': {}}
        write_json_atomic(manifest, directory / MANIFEST_NAME)
        return manifest
    manifest = get_manifest(directory)
    if manifest is None:
        raise RunStateError(f'No run manifest in {directory}; rerun without --resume')
    if manifest.get('identity') != identity:
        raise RunStateError('Input content or processing options changed; '
                            'start a fresh run without --resume')
    return manifest


def cleanup_run_artifacts(temp_dir, identity):
    """Remove only this run's generated files after successful orchestration."""
    ensure_run_manifest(temp_dir, identity, fresh=False)
    directory = Path(temp_dir)
    for child in directory.iterdir():
        if child.name in GENERATED_NAMES or SAMPLE_NAME.fullmatch(child.name):
            if child.is_dir() and not child.is_symlink():
                raise RunStateError(f'Generated path is unexpectedly a directory: {child}')
            child.unlink()
    if not any(directory.iterdir()):
        directory.rmdir()


def _valid_artifact_shape(name, data):
    if name in {'asr', 'enriched'}:
        return (isinstance(data, dict) and
                isinstance(data.get('detected_lang'), str) and
                isinstance(data.get('word_segments'), list) and
                all(isinstance(word, dict) and 'word' in word
                    for word in data['word_segments']))
    if name == 'diarization':
        return isinstance(data, list) and all(isinstance(turn, dict) for turn in data)
    return False


def is_verified_artifact(temp_dir, identity, name, path):
    """True only for a completed, unchanged artifact with the right shape."""
    manifest = ensure_run_manifest(temp_dir, identity, fresh=False)
    record = manifest['artifacts'].get(name)
    if not isinstance(record, dict):
        return False
    artifact = Path(path)
    try:
        if str(artifact.resolve(strict=True)) != record.get('path'):
            return False
        if get_file_identity(artifact)['sha256'] != record.get('sha256'):
            return False
        if name == 'scratch':
            return True
        data = json.loads(artifact.read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return False
    return _valid_artifact_shape(name, data)


def validate_scratch_path(temp_dir, identity, path):
    """Refuse pre-existing caller files and symlinks before writing scratch."""
    if os.path.islink(path) or (os.path.lexists(path) and not is_verified_artifact(
            temp_dir, identity, 'scratch', path)):
        raise RunStateError(f'Scratch audio is not owned by this run: {path}')


def mark_artifact_complete(temp_dir, identity, name, path):
    """Record a fully written phase artifact after checking its actual bytes."""
    artifact = Path(path)
    if name != 'scratch':
        data = json.loads(artifact.read_text(encoding='utf-8'))
        if not _valid_artifact_shape(name, data):
            raise RunStateError(f'Invalid {name} artifact: {artifact}')
    manifest = ensure_run_manifest(temp_dir, identity, fresh=False)
    updated = dict(manifest)
    updated['artifacts'] = dict(manifest['artifacts'])
    artifact_identity = get_file_identity(artifact)
    if name == 'asr':
        old = updated['artifacts'].get('asr')
        if old is None or old.get('sha256') != artifact_identity['sha256']:
            updated['artifacts'].pop('enriched', None)
    updated['artifacts'][name] = artifact_identity
    write_json_atomic(updated, Path(temp_dir) / MANIFEST_NAME)


def get_sample_path(temp_dir, name):
    """Resolve only generated sample basenames inside the run directory."""
    if not isinstance(name, str) or not SAMPLE_NAME.fullmatch(name):
        raise RunStateError(f'Invalid sample audio filename: {name!r}')
    path = Path(temp_dir) / name
    if path.is_symlink():
        raise RunStateError(f'Sample audio must not be a symlink: {path}')
    return path
