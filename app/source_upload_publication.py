"""Publish complete source uploads without exposing a writable public file.

The caller owns the upload quota lease and descriptor-owned stage. Anonymous
extents participate in the route's accounting until native publication.
"""
import os
from pathlib import Path
import stat
import sys
import errno

from source_upload_stage import OwnedSourceUploadStage, _destination_name


def create_source_upload_stage(directory, *, limit):
    return OwnedSourceUploadStage(directory, byte_limit=limit)


def move_source_upload_exclusively(source, destination, *, source_dir_fd=None, destination_dir_fd=None):
    """Atomic no-overwrite move, with no intermediate second hard link.

    Unlike link/unlink, process death cannot leave an alias that invalidates the
    existing single-link source admission rule. Unsupported OS/filesystem pairs
    fail closed; a normal Unix rename would silently overwrite another owner.
    """
    anchored = source_dir_fd is not None or destination_dir_fd is not None
    if anchored:
        if (type(source_dir_fd) is not int or source_dir_fd < 0
                or type(destination_dir_fd) is not int or destination_dir_fd < 0
                or type(source) is not str or type(destination) is not str
                or os.path.basename(source) != source or os.path.basename(destination) != destination
                or source in ('', '.', '..') or destination in ('', '.', '..')
                or any(char in value for value in (source, destination) for char in ('\\', '\0'))):
            raise ValueError('Exclusive directory-anchored move needs valid descriptors and names')
        if os.name == 'nt':
            raise OSError(errno.ENOTSUP, 'Directory-anchored exclusive moves are unavailable')
    elif os.name == 'nt':
        os.rename(source, destination)
        return
    import ctypes
    library = ctypes.CDLL(None, use_errno=True)
    source, destination = os.fsencode(source), os.fsencode(destination)
    if b'\0' in source or b'\0' in destination:
        raise ValueError('Invalid source upload path')
    if sys.platform.startswith('linux'):
        operation = getattr(library, 'renameat2', None)
        arguments = (source_dir_fd if anchored else -100, source,
                     destination_dir_fd if anchored else -100, destination, 1)  # RENAME_NOREPLACE
        types = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    elif sys.platform == 'darwin':
        if anchored:
            operation = getattr(library, 'renameatx_np', None)
            arguments = (source_dir_fd, source, destination_dir_fd, destination, 4)
            types = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        else:
            operation = getattr(library, 'renamex_np', None)
            arguments = (source, destination, 4)  # RENAME_EXCL, macOS 10.12+
            types = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
    else:
        operation = None
    if operation is None:
        raise OSError(errno.ENOTSUP, 'Exclusive source upload publication is unavailable')
    operation.argtypes = types
    operation.restype = ctypes.c_int
    if operation(*arguments) != 0:
        raise OSError(ctypes.get_errno(), 'Exclusive source upload publication failed')


def get_retained_epub_name(txt_filename):
    """Deterministic retained-EPUB name beside an extracted TXT."""
    if not isinstance(txt_filename, str) or not txt_filename or '/' in txt_filename or '\\' in txt_filename:
        raise ValueError('Invalid source upload name')
    stem = Path(txt_filename).stem
    if not stem:
        raise ValueError('Invalid source upload name')
    return stem + '.source.epub'


def get_retained_epub_path(txt_path):
    return str(Path(txt_path).with_name(get_retained_epub_name(Path(txt_path).name)))


def publish_retained_epub_upload(stage, retained_name, *, limit, on_visible=None, exact=False):
    """Publish retained original EPUB bytes beside the extracted TXT.

    Reuses the descriptor-owned stage publication (quota lease already held
    by the caller); no companion, no second authority. Collisions step to
    ``<stem>_<n>.source.epub`` unless ``exact`` is set. Callers that must
    keep a TXT / companion / retained group coherent (same stem) pass
    ``exact=True`` after jointly selecting a free triple so a race cannot
    step the retained file away from its TXT. On unknown commit outcome the
    caller retains the file (never deletes ``publication_started`` stages).
    """
    if (not isinstance(stage, OwnedSourceUploadStage)
            or not isinstance(retained_name, str) or retained_name in ('', '.', '..')
            or '/' in retained_name or '\\' in retained_name or '\0' in retained_name
            or retained_name.startswith('.')
            or not retained_name.endswith('.source.epub')
            or type(limit) is not int or limit < 0):
        raise ValueError('Invalid retained EPUB publication')
    _destination_name(retained_name)
    if stage.size > limit:
        raise ValueError('Retained EPUB exceeds its limit')
    stage.seal()
    if exact:
        if type(exact) is not bool:
            raise ValueError('Invalid retained EPUB publication')
        stage._check_root()
        destination = stage.directory / retained_name
        if os.path.lexists(destination):
            raise ValueError('Retained EPUB destination already exists')
        try:
            stage.publish(retained_name)
        except FileExistsError:
            raise ValueError('Retained EPUB destination already exists') from None
        try:
            if on_visible is not None:
                on_visible(str(destination))
            stage.sync_directory()
        except BaseException:
            try:
                stage.close()
            except BaseException:
                pass
            raise
        else:
            stage.close()
        return str(destination)
    # Full stem before '.source.epub' is the collision base.
    stem = retained_name[: -len('.source.epub')]
    for number in range(1, 1002):
        name = retained_name if number == 1 else f'{stem}_{number}.source.epub'
        if not name.endswith('.source.epub'):
            continue
        stage._check_root()
        destination = stage.directory / name
        if os.path.lexists(destination):
            continue
        try:
            stage.publish(name)
        except FileExistsError:
            continue
        try:
            if on_visible is not None:
                on_visible(str(destination))
            stage.sync_directory()
        except BaseException:
            try:
                stage.close()
            except BaseException:
                pass
            raise
        else:
            stage.close()
        return str(destination)
    raise ValueError('Too many retained EPUB filename collisions')


def get_retained_epub_inventory(directory, quota_limit, *, scan_limit=4096, group_limit=100):
    """Bounded metadata-only inventory for retained EPUB uploads.

    Lists each extracted-TXT stem and its ``<stem>.source.epub`` companion
    (plus the ``<TXT>.publication.json`` provenance companion) using
    filesystem metadata only. Contents, hashes and provenance are never
    read or verified here; nothing is admitted, modified or deleted.
    Returned ``groups`` and each group's ``files`` are sorted; the scanned
    subset is the first ``scan_limit`` filesystem entries (a bounded
    unsorted scan), so beyond-limit omission depends on filesystem order
    and is not deterministic. ``observed_named_bytes`` is a partial quota
    count of scanned named regular single-link files (non-matching entries
    included in the scan count and byte count; active anonymous stage
    names, symlinks/hardlinks and unreadable entries excluded), mirroring
    the DOCX inventory contract. Callers needing exact quota must use
    ``get_upload_storage_bytes``.
    """
    from pathlib import Path as _Path
    if (type(scan_limit) is not int or type(group_limit) is not int
            or type(quota_limit) is not int or scan_limit < 0
            or group_limit < 0 or quota_limit < 0):
        raise ValueError('Invalid retained EPUB inventory limits')
    groups = {}
    scanned = counted_bytes = omitted = 0
    complete = True
    unavailable = False
    active_names = set()
    try:
        from source_upload_stage import get_upload_stage_inventory
        active_names = set(get_upload_stage_inventory(directory).get('names', set()))
    except Exception:
        active_names = set()
    # Open separately so a missing/unavailable directory returns the
    # unavailable-location shape, while a mid-scan OSError yields the
    # collected partial groups with unavailable markers.
    try:
        scan_ctx = os.scandir(directory)
    except OSError:
        return {'location': 'unavailable', 'groups': [], 'scanned_entries': 0,
                'scan_limit': scan_limit, 'group_limit': group_limit,
                'scan_complete': False, 'omitted_marker_entries': 0,
                'observed_named_bytes': 0, 'quota_limit_bytes': quota_limit,
                'unavailable_entries': True,
                'notice': ('Metadata only; contents, hashes and provenance are not verified. '
                           'Files may change during inspection. No automatic deletion. '
                           'Stop the app before manual cleanup. Back up each TXT together '
                           'with its retained EPUB and provenance companion.')}
    try:
        with scan_ctx as entries:
            iterator = iter(entries)
            while True:
                try:
                    entry = next(iterator)
                except StopIteration:
                    break
                except OSError:
                    complete = False
                    unavailable = True
                    break
                if scanned >= scan_limit:
                    complete = False
                    break
                scanned += 1
                name = entry.name
                if name in active_names:
                    continue
                size = None
                try:
                    # lstat (not DirEntry.stat) so Windows link counts match
                    # the single-link rule used by the upload route.
                    info = os.lstat(os.path.join(directory, name))
                    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                        raise ValueError('Not a regular file')
                    size = info.st_size
                    counted_bytes += size
                except (OSError, ValueError):
                    # Filesystem semantics: a disappeared / unreadable entry
                    # is an unavailable scan member, never trusted bytes.
                    unavailable = True
                if name.endswith('.source.epub'):
                    stem = name[:-len('.source.epub')]
                    txt_name = stem + '.txt'
                elif name.endswith('.publication.json'):
                    candidate_txt = name[:-len('.publication.json')]
                    if not candidate_txt.lower().endswith(('.txt', '.md')):
                        continue
                    if '/' in candidate_txt or '\\' in candidate_txt:
                        continue
                    stem = _Path(candidate_txt).stem
                    txt_name = candidate_txt
                elif name.lower().endswith(('.txt', '.md')):
                    stem = _Path(name).stem
                    txt_name = name
                else:
                    continue
                if not stem or stem.startswith('.'):
                    continue
                key = txt_name
                if key not in groups:
                    if len(groups) >= group_limit:
                        omitted += 1
                        continue
                    groups[key] = {'stem': stem, 'txt_filename': txt_name, 'files': []}
                groups[key]['files'].append(
                    {'filename': name, 'bytes': size,
                     'state': 'unverified' if size is not None else 'unavailable'})
    except OSError:
        complete = False
        unavailable = True
    group_list = [groups[key] for key in sorted(groups)]
    for group in group_list:
        present = {item['filename'] for item in group['files']}
        wants = {group['txt_filename'], group['stem'] + '.source.epub',
                 group['txt_filename'] + '.publication.json'}
        group['state'] = ('unavailable'
                          if any(item['state'] == 'unavailable' for item in group['files'])
                          else 'unverified' if present == wants else 'incomplete/unverified')
    # Deterministic ordering of files within each group.
    for group in group_list:
        group['files'] = sorted(group['files'], key=lambda item: item['filename'])
    return {'location': str(_Path(directory).absolute()),
            'groups': group_list,
            'scanned_entries': scanned, 'scan_limit': scan_limit,
            'group_limit': group_limit, 'scan_complete': complete,
            'omitted_marker_entries': omitted,
            'observed_named_bytes': counted_bytes,
            'quota_limit_bytes': quota_limit,
            'unavailable_entries': unavailable,
            'notice': ('Metadata only; contents, hashes and provenance are not verified. '
                       'Files may change during inspection. Observed bytes are a partial '
                       'quota count of scanned named regular files, excluding active '
                       'anonymous uploads, symlinks/hardlinks and unreadable entries. '
                       'Scan/result limits may omit files. Stop the app before manual cleanup. '
                       'Keep full backups. Do not remove currently selected/referenced groups. '
                       'Back up each TXT together with its retained EPUB and provenance '
                       'companion, including partial/orphan groups and collision-numbered '
                       'names. No automatic deletion.')}
def publish_completed_upload(stage, filename, on_visible, *, limit, companion_bytes=None):
    """Publish only the descriptor-owned payload, never a reopened stage name.

    The stage records visibility before closing its writer. on_visible follows
    writer close and precedes directory sync. On error callers retain sources
    whenever stage.publication_started indicates an unknown commit outcome, and
    close the owner to release unpublished resources. Unsupported primitives
    fail without a pathname-selected copy/rename fallback.
    """
    if (not isinstance(stage, OwnedSourceUploadStage)
            or not isinstance(filename, str) or filename in ('', '.', '..')
            or '/' in filename or '\\' in filename or '\0' in filename
            or filename.startswith('.') or Path(filename).suffix.lower() not in ('.txt', '.md')
            or type(limit) is not int or limit < 0 or not callable(on_visible)):
        raise ValueError('Invalid source upload publication')
    _destination_name(filename)
    if stage.size > limit:
        raise ValueError('Source upload stage is unsafe or exceeds its limit')
    stage.seal()
    if companion_bytes is not None:
        from publication_source import get_publication_source_document
        document = get_publication_source_document(companion_bytes)
        if document['binding'] != {'kind': 'text', 'sha256': stage.sha256()}:
            raise ValueError('Source upload provenance does not match its text')
    base, extension = os.path.splitext(filename)
    for number in range(1, 1002):
        name = filename if number == 1 else f'{base}_{number}{extension}'
        stage._check_root()
        destination = stage.directory / name
        # Never accidentally pair a new source with orphaned old provenance.
        if (os.path.lexists(destination)
                or os.path.lexists(str(destination) + '.publication.json')):
            continue
        companion = Path(str(destination) + '.publication.json')
        companion_identity = None
        companion_version = None
        published = False
        try:
            if companion_bytes is not None:
                # The source name is still absent. Reserve its companion without
                # overwriting anyone else's evidence, then finish it first.
                descriptor = os.open(companion.name if stage._directory_fd is not None else companion,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600,
                    **({'dir_fd': stage._directory_fd} if stage._directory_fd is not None else {}))
                try:
                    facts = os.fstat(descriptor)
                    companion_identity = (facts.st_dev, facts.st_ino)
                    # Keep raw ownership if buffered-stream allocation fails.
                    with os.fdopen(descriptor, 'wb', closefd=False) as output:
                        output.write(companion_bytes)
                        output.flush()
                        os.fsync(output.fileno())
                finally:
                    os.close(descriptor)
                # Windows can finalize write timestamps only when the write
                # handle closes. Capture the cleanup version afterward while
                # still verifying that the reserved identity owns this name.
                facts = (os.stat(companion.name, dir_fd=stage._directory_fd,
                                 follow_symlinks=False)
                         if stage._directory_fd is not None else companion.lstat())
                if ((facts.st_dev, facts.st_ino) != companion_identity
                        or not stat.S_ISREG(facts.st_mode) or facts.st_nlink != 1):
                    raise ValueError('Source provenance changed during publication')
                companion_version = (facts.st_size, facts.st_mtime_ns)
            stage.publish(name)
            published = True
        except FileExistsError:
            continue
        finally:
            if companion_identity is not None and not published and not stage.publication_started:
                active_error = sys.exc_info()[1]
                cleanup_error = None
                try:
                    facts = (os.stat(companion.name, dir_fd=stage._directory_fd,
                                     follow_symlinks=False)
                             if stage._directory_fd is not None else companion.lstat())
                except OSError as error:
                    cleanup_error = error
                else:
                    if ((facts.st_dev, facts.st_ino) != companion_identity
                            or not stat.S_ISREG(facts.st_mode) or facts.st_nlink != 1
                            or companion_version is not None
                            and (facts.st_size, facts.st_mtime_ns) != companion_version):
                        cleanup_error = ValueError(
                            'Source provenance changed during publication')
                    else:
                        try:
                            if stage._directory_fd is not None:
                                os.unlink(companion.name, dir_fd=stage._directory_fd)
                            else:
                                companion.unlink()
                        except OSError as error:
                            cleanup_error = error
                # A retryable collision becomes a governed conflict when its
                # companion changed. Never replace a more informative native
                # publication failure such as ENOSPC with the cleanup symptom.
                if (cleanup_error is not None and
                        (active_error is None or isinstance(active_error, FileExistsError))):
                    raise ValueError(
                        'Source provenance changed during publication') from cleanup_error
        try:
            on_visible(str(destination))
            stage.sync_directory()
        except BaseException:
            try:
                stage.close()
            except BaseException:
                pass
            raise
        else:
            stage.close()
        return str(destination)
    raise ValueError('Too many source upload filename collisions')
