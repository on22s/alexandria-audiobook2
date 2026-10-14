"""Recoverable publication and native reader admission for Preparer ZIP families."""
from contextlib import contextmanager, ExitStack
import json
import os
from pathlib import Path
import re
import shutil
import stat
import tempfile

from alexandria_file_lock import acquire_exclusive_file_lock, release_exclusive_file_lock
from alexandria_run_manifest import (RunStateError, sync_run_directory, write_json_atomic,
                                    get_file_identity)


class ZipPublicationError(RunStateError):
    """A ZIP family is busy or requires preserved publication evidence."""


JOURNAL_SUFFIX = '.volumes.pending.json'
MANIFEST_SUFFIX = '.volumes.json'


def _get_output(path):
    output = Path(os.path.abspath(path))
    if '\\' in output.name:
        raise ZipPublicationError('Invalid ZIP output name')
    if output.parent.is_symlink() or output.parent.resolve() != output.parent:
        raise ZipPublicationError('ZIP output directory must not contain a symlink')
    return output


def _get_names(output, names):
    if (not isinstance(names, list) or not names or len(set(names)) != len(names)
            or any(not isinstance(name, str) or not name or '/' in name or '\\' in name
                   or not name.endswith(output.suffix)
                   or not (name == output.name or name.startswith(output.stem + '_'))
                   for name in names)):
        raise ZipPublicationError(f'Invalid ZIP volume manifest: {output}')
    return names


def _get_hash(path):
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode):
        raise ZipPublicationError(f'ZIP publication evidence must be a regular file: {path}')
    return get_file_identity(path)['sha256']


def _get_existing_hash(path):
    return _get_hash(path) if os.path.lexists(path) else None


def _get_manifest_names(output):
    manifest = Path(str(output) + MANIFEST_SUFFIX)
    if not os.path.lexists(manifest):
        return [output.name] if os.path.lexists(output) else []
    _get_hash(manifest)
    try:
        document = json.loads(manifest.read_text(encoding='utf-8'))
        return _get_names(output, document['volumes'])
    except (ValueError, KeyError, TypeError) as error:
        raise ZipPublicationError(f'Invalid ZIP volume manifest: {manifest}') from error


@contextmanager
def _ensure_locks(output):
    handles = {}
    def acquire(names):
        for name in sorted(set(names)):
            if name in handles:
                continue
            lock = output.parent / ('.' + name + '.volumes.lock')
            if lock.is_symlink():
                raise ZipPublicationError(f'ZIP publication lock is a symlink: {lock}')
            descriptor = os.open(lock, os.O_CREAT | os.O_RDWR, 0o600)
            try:
                acquire_exclusive_file_lock(descriptor)
            except OSError as error:
                os.close(descriptor)
                raise ZipPublicationError(f'ZIP family is busy: {output.name}') from error
            handles[name] = descriptor
    try:
        acquire([output.name])
        yield acquire
    finally:
        failure = None
        for descriptor in reversed(list(handles.values())):
            try:
                release_exclusive_file_lock(descriptor)
            except OSError as error:
                failure = failure or error
            finally:
                try:
                    os.close(descriptor)
                except OSError as error:
                    failure = failure or error
        if failure is not None:
            raise failure


def get_zip_archive_family(path):
    """Resolve a managed/pending family; legacy unmanifested ZIPs own themselves."""
    archive = _get_output(path)
    families = set()
    for marker in archive.parent.iterdir():
        suffix = (JOURNAL_SUFFIX if marker.name.endswith(JOURNAL_SUFFIX)
                  else MANIFEST_SUFFIX if marker.name.endswith(MANIFEST_SUFFIX) else None)
        if suffix is None:
            continue
        output = _get_output(marker.with_name(marker.name[:-len(suffix)]))
        if not (archive.name == output.name or archive.stem.startswith(output.stem + '_')):
            continue
        if suffix == JOURNAL_SUFFIX or archive.name in _get_manifest_names(output):
            families.add(output)
    if len(families) > 1:
        raise ZipPublicationError(f'Ambiguous ZIP family: {archive.name}')
    return next(iter(families), archive)


def _get_snapshot(output, names):
    files = {}
    for name in names:
        path = output.parent / name
        if path.is_symlink():
            raise ZipPublicationError(f'ZIP archive is a symlink: {path}')
        info = path.stat()
        if not stat.S_ISREG(info.st_mode):
            raise ZipPublicationError(f'ZIP archive is not a regular file: {path}')
        files[name] = [info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns]
    return {'family': str(output), 'files': files,
            'manifest': _get_existing_hash(Path(str(output) + MANIFEST_SUFFIX))}


@contextmanager
def ensure_zip_family_readable(output_path, *, include_legacy_prefixes=False):
    """Hold native family admission through the caller's actual reads."""
    output = _get_output(output_path)
    journal = Path(str(output) + JOURNAL_SUFFIX)
    with _ensure_locks(output) as acquire:
        if os.path.lexists(journal):
            raise ZipPublicationError(f'ZIP publication recovery required: {output.name}')
        names = _get_manifest_names(output)
        legacy = not os.path.lexists(Path(str(output) + MANIFEST_SUFFIX))
        if include_legacy_prefixes and legacy:
            names = sorted(path.name for path in output.parent.iterdir()
                           if path.is_file() and path.suffix == '.zip'
                           and (path.name == output.name or path.stem.startswith(output.stem + '_')))
        acquire(names)
        if include_legacy_prefixes and legacy:
            for name in names:
                family = get_zip_archive_family(output.parent / name)
                if os.path.lexists(Path(str(family) + JOURNAL_SUFFIX)):
                    raise ZipPublicationError(f'ZIP publication recovery required: {family.name}')
        yield _get_snapshot(output, names)


@contextmanager
def ensure_zip_archive_readable(path, expected=None):
    """Read one archive only within its captured, currently readable generation."""
    expectations = {str(path): expected} if expected is not None else None
    with ensure_zip_sources_readable([path], expectations) as snapshots:
        yield snapshots[str(path)]


@contextmanager
def ensure_zip_sources_readable(paths, expected=None, destination=None):
    """Admit every source family once, in deterministic order, through actual reads."""
    archives = {str(path): _get_output(path) for path in paths}
    families = {key: get_zip_archive_family(path) for key, path in archives.items()}
    if expected is not None and set(expected) != set(archives):
        raise ZipPublicationError('ZIP input snapshot does not match selected sources')
    with ExitStack() as stack:
        snapshots = {output: stack.enter_context(ensure_zip_family_readable(output))
                     for output in sorted(set(families.values()))}
        result = {}
        for key, path in archives.items():
            output = families[key]
            snapshot = snapshots[output]
            if path.name not in snapshot['files']:
                path.stat()  # Preserve native missing-source errors for legacy callers.
                raise ZipPublicationError(f'ZIP archive is not in the published family: {path.name}')
            if expected is not None and snapshot != expected[key]:
                raise ZipPublicationError(f'ZIP family changed since collection: {path.name}')
            if destination is not None:
                target = Path(os.path.abspath(destination))
                if target == output or (target.parent == output.parent and target.name in snapshot['files']):
                    raise ZipPublicationError('Merge destination belongs to a source ZIP family')
            result[key] = snapshot
        yield result


def get_zip_directory_snapshot(directory, *, skip_underscore=False, archive_filter=None):
    """Capture a whole input list under family admission, refusing pending families."""
    root = Path(directory)
    for marker in root.iterdir():
        if marker.name.endswith(JOURNAL_SUFFIX):
            raise ZipPublicationError(f'ZIP publication recovery required: {marker.name}')
    def get_paths():
        return sorted(path for path in root.iterdir()
                      if (not skip_underscore or not path.name.startswith('_'))
                      and (archive_filter(path) if archive_filter is not None
                           else path.suffix.lower() == '.zip'))
    paths = get_paths()
    with ensure_zip_sources_readable(paths) as snapshots:
        if paths != get_paths():
            raise ZipPublicationError('ZIP input list changed during collection')
        return snapshots


def _get_journal(output):
    journal = Path(str(output) + JOURNAL_SUFFIX)
    _get_hash(journal)
    try:
        document = json.loads(journal.read_text(encoding='utf-8'))
        originals, replacements = document['originals'], document['replacements']
        workspace = document['workspace']
        manifest_name = output.name + MANIFEST_SUFFIX
        if (type(document['version']) is not int or document['version'] != 1 or document['output'] != output.name
                or document['phase'] not in ('prepared', 'cleanup', 'rolled_back')
                or not isinstance(originals, dict) or not isinstance(replacements, dict)
                or not isinstance(workspace, str)
                or not re.fullmatch(re.escape('.' + output.name + '.volumes-stage-') + r'[a-zA-Z0-9_]+', workspace)
                or manifest_name not in originals or manifest_name not in replacements):
            raise ValueError('Invalid journal structure')
        _get_names(output, [name for name in originals if name != manifest_name])
        _get_names(output, [name for name in replacements if name != manifest_name])
        if not set(replacements).issubset(originals):
            raise ValueError('Unclaimed replacement')
        for values, nullable in ((originals, True), (replacements, False)):
            if any(not (nullable and value is None) and
                   (not isinstance(value, str) or not re.fullmatch(r'[0-9a-f]{64}', value))
                   for value in values.values()):
                raise ValueError('Invalid evidence hash')
    except (ValueError, KeyError, TypeError) as error:
        raise ZipPublicationError(f'Invalid ZIP publication journal: {journal}') from error
    return document


def _validate_evidence(output, document):
    """Inspect every location before recovery is allowed to change any of them."""
    workspace = output.parent / document['workspace']
    phase = document['phase']
    if workspace.is_symlink() or (workspace.exists() and not workspace.is_dir()):
        raise ZipPublicationError(f'Invalid ZIP publication workspace: {workspace}')
    if not workspace.exists() and phase == 'prepared':
        raise ZipPublicationError(f'Missing ZIP publication workspace: {workspace}')
    allowed = {'previous', 'next'}
    if workspace.exists() and any(path.name not in allowed for path in workspace.iterdir()):
        raise ZipPublicationError('Unowned ZIP publication workspace entry')
    locations = {}
    for folder, hashes in (('previous', document['originals']), ('next', document['replacements'])):
        directory = workspace / folder
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ZipPublicationError(f'Invalid ZIP publication evidence directory: {directory}')
        if directory.exists() and any(path.name not in hashes for path in directory.iterdir()):
            raise ZipPublicationError('Unowned ZIP publication evidence entry')
    for name, original in document['originals'].items():
        replacement = document['replacements'].get(name)
        final = _get_existing_hash(output.parent / name)
        previous = _get_existing_hash(workspace / 'previous' / name)
        staged = _get_existing_hash(workspace / 'next' / name)
        if (previous is not None and previous != original
                or staged is not None and staged != replacement
                or final is not None and final not in (original, replacement)):
            raise ZipPublicationError(f'Edited or conflicting ZIP publication evidence: {name}')
        if phase == 'prepared':
            if original is not None and original not in (previous, final):
                raise ZipPublicationError(f'Missing original ZIP publication evidence: {name}')
            if replacement is not None and replacement not in (staged, final):
                raise ZipPublicationError(f'Missing replacement ZIP publication evidence: {name}')
            if staged is not None and final == replacement and (previous is not None or final != original):
                raise ZipPublicationError(f'Duplicate replacement ZIP evidence: {name}')
        elif phase == 'cleanup':
            if final != replacement:
                raise ZipPublicationError(f'Published ZIP family changed: {name}')
        elif final != original:
            raise ZipPublicationError(f'Restored ZIP family changed: {name}')
        locations[name] = (final, previous, staged)
    if phase == 'prepared':
        manifest_name = output.name + MANIFEST_SUFFIX
        def get_names_for_hash(digest, paths):
            for path in paths:
                if _get_existing_hash(path) == digest:
                    try:
                        return _get_names(output, json.loads(path.read_text(encoding='utf-8'))['volumes'])
                    except (ValueError, KeyError, TypeError) as error:
                        raise ZipPublicationError('Invalid recorded ZIP manifest') from error
            raise ZipPublicationError('Missing recorded ZIP manifest')
        new_names = get_names_for_hash(document['replacements'][manifest_name],
                                      [workspace / 'next' / manifest_name, output.parent / manifest_name])
        old_hash = document['originals'][manifest_name]
        old_names = (get_names_for_hash(old_hash, [workspace / 'previous' / manifest_name,
                                                 output.parent / manifest_name])
                     if old_hash is not None else [output.name])
        if (set(document['replacements']) != set(new_names + [manifest_name])
                or not set(document['originals']).issubset(old_names + new_names + [manifest_name])
                or not set(old_names if old_hash is not None else []).issubset(document['originals'])):
            raise ZipPublicationError('ZIP journal claims differ from recorded manifests')
    return workspace, locations


def _retire_publication(output, document):
    workspace, _ = _validate_evidence(output, document)
    if workspace.exists():
        shutil.rmtree(workspace)
        sync_run_directory(output.parent)
    Path(str(output) + JOURNAL_SUFFIX).unlink()
    sync_run_directory(output.parent)


def _recover_publication(output, document):
    workspace, locations = _validate_evidence(output, document)
    if document['phase'] == 'prepared':
        # Retain replacement evidence while restoring originals, so recovery can
        # itself be interrupted and repeat safely without borrowing other files.
        for name, replacement in document['replacements'].items():
            final, previous, staged = locations[name]
            original = document['originals'][name]
            if final == replacement and (previous is not None or final != original):
                if staged is not None:
                    raise ZipPublicationError(f'Duplicate replacement ZIP evidence: {name}')
                os.replace(output.parent / name, workspace / 'next' / name)
                sync_run_directory(output.parent)
                sync_run_directory(workspace / 'next')
        for name, original in document['originals'].items():
            backup = workspace / 'previous' / name
            if original is not None and backup.exists():
                os.replace(backup, output.parent / name)
                sync_run_directory(output.parent)
                sync_run_directory(workspace / 'previous')
        document = {**document, 'phase': 'rolled_back'}
        write_json_atomic(document, Path(str(output) + JOURNAL_SUFFIX))
    _retire_publication(output, document)


def ensure_recovered_zip_family(output_path):
    """Recover only a validated, stopped publisher's exact owned ZIP evidence."""
    output = _get_output(output_path)
    with _ensure_locks(output) as acquire:
        if os.path.lexists(Path(str(output) + JOURNAL_SUFFIX)):
            document = _get_journal(output)
            acquire([name for name in document['originals'] if name.endswith(output.suffix)])
            _recover_publication(output, document)


@contextmanager
def apply_zip_volume_set(output_path, new_names):
    """Stage the complete family, publish it recoverably, and retire owned evidence."""
    output = _get_output(output_path)
    _get_names(output, new_names)
    journal = Path(str(output) + JOURNAL_SUFFIX)
    with _ensure_locks(output) as acquire:
        if os.path.lexists(journal):
            document = _get_journal(output)
            acquire([name for name in document['originals'] if name.endswith(output.suffix)])
            _recover_publication(output, document)
        old_names = _get_manifest_names(output)
        acquire(old_names + new_names)
        manifest_name = output.name + MANIFEST_SUFFIX
        for marker in output.parent.glob('*' + MANIFEST_SUFFIX):
            other = _get_output(marker.with_name(marker.name[:-len(MANIFEST_SUFFIX)]))
            could_claim = any(name == other.name or name.startswith(other.stem + '_')
                              for name in old_names + new_names)
            if other != output and could_claim and set(_get_manifest_names(other)).intersection(old_names + new_names):
                raise ZipPublicationError('ZIP output is owned by another volume manifest')
        for marker in output.parent.glob('*' + JOURNAL_SUFFIX):
            other = _get_output(marker.with_name(marker.name[:-len(JOURNAL_SUFFIX)]))
            if other != output and any(name == other.name or name.startswith(other.stem + '_')
                                       for name in old_names + new_names):
                raise ZipPublicationError('ZIP output has another pending volume owner')
        if not Path(str(output) + MANIFEST_SUFFIX).exists():
            for path in output.parent.iterdir():
                if (path.stem.startswith(output.stem + '_') and path.name.endswith(output.suffix)
                        and path.name not in new_names
                        and re.search(r'_vol\d+' + re.escape(output.suffix) + r'$', path.name)):
                    raise ZipPublicationError(f'Unmanaged old ZIP volume: {path}')
        else:
            for name in new_names:
                if name not in old_names and os.path.lexists(output.parent / name):
                    raise ZipPublicationError(f'Unmanaged replacement ZIP volume: {name}')
        originals = {name: _get_existing_hash(output.parent / name)
                     for name in dict.fromkeys(old_names + new_names + [manifest_name])}
        workspace = Path(tempfile.mkdtemp(prefix='.' + output.name + '.volumes-stage-', dir=output.parent))
        stage = workspace / 'next'
        stage.mkdir()
        (workspace / 'previous').mkdir()
        recorded = False
        try:
            yield stage
            manifest = stage / manifest_name
            with manifest.open('w', encoding='utf-8') as stream:
                json.dump({'volumes': new_names}, stream)
                stream.flush()
                os.fsync(stream.fileno())
            replacements = {}
            for name in new_names + [manifest_name]:
                with (stage / name).open('r+b') as stream:
                    os.fsync(stream.fileno())
                replacements[name] = _get_hash(stage / name)
            sync_run_directory(stage)
            sync_run_directory(workspace)
            document = {'version': 1, 'output': output.name, 'workspace': workspace.name,
                        'phase': 'prepared', 'originals': originals, 'replacements': replacements}
            write_json_atomic(document, journal)
            recorded = True
            _validate_evidence(output, document)
            for name, digest in originals.items():
                if digest is not None:
                    os.replace(output.parent / name, workspace / 'previous' / name)
                    sync_run_directory(output.parent)
                    sync_run_directory(workspace / 'previous')
            for name in replacements:
                os.replace(stage / name, output.parent / name)
                sync_run_directory(output.parent)
                sync_run_directory(stage)
            document = {**document, 'phase': 'cleanup'}
            write_json_atomic(document, journal)
        except BaseException:
            if recorded or os.path.lexists(journal):
                try:
                    _recover_publication(output, _get_journal(output))
                except BaseException as error:
                    raise ZipPublicationError(f'ZIP publication recovery required; evidence retained at {journal}: {error}') from error
            else:
                shutil.rmtree(workspace)
            raise
        else:
            try:
                _retire_publication(output, document)
            except OSError as error:
                raise ZipPublicationError(f'ZIP set committed; cleanup recovery required at {journal}: {error}') from error
