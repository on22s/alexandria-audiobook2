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
                            or not stat.S_ISREG(facts.st_mode) or facts.st_nlink != 1):
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
        finally:
            stage.close()
        return str(destination)
    raise ValueError('Too many source upload filename collisions')
