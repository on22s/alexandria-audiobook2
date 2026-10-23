"""Publish complete source uploads without exposing a writable public file.

The caller owns the upload quota lease and finishes all writes before calling
publish_completed_upload. Private files remain in the flat, counted directory.
"""
import os
import hashlib
from pathlib import Path
import re
import stat
import tempfile
import sys
import errno

from adapter_publication import sync_adapter_directory


def create_source_upload_stage(directory):
    descriptor, path = tempfile.mkstemp(prefix='.source-upload-', suffix='.part', dir=directory)
    os.close(descriptor)
    return path


def move_source_upload_exclusively(source, destination):
    """Atomic no-overwrite move, with no intermediate second hard link.

    Unlike link/unlink, process death cannot leave an alias that invalidates the
    existing single-link source admission rule. Unsupported OS/filesystem pairs
    fail closed; a normal Unix rename would silently overwrite another owner.
    """
    if os.name == 'nt':
        os.rename(source, destination)
        return
    import ctypes
    library = ctypes.CDLL(None, use_errno=True)
    source, destination = os.fsencode(source), os.fsencode(destination)
    if b'\0' in source or b'\0' in destination:
        raise ValueError('Invalid source upload path')
    if sys.platform.startswith('linux'):
        operation = getattr(library, 'renameat2', None)
        arguments = (-100, source, -100, destination, 1)  # AT_FDCWD, RENAME_NOREPLACE
        types = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
    elif sys.platform == 'darwin':
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


def publish_completed_upload(stage_path, filename, on_visible, *, limit, companion_bytes=None):
    """Transfer ownership immediately after an exclusive complete-file install.

    on_visible receives the public path before any fallible cleanup or directory
    sync. Even if this call subsequently raises, that public file belongs to the
    library and must not be removed by request cancellation. The caller remains
    responsible for removing its private stage on failure.

    The exclusive move removes the private name atomically; unsupported
    filesystems fail without a copy/overwrite fallback.
    """
    stage = Path(stage_path)
    if (not re.fullmatch(r'\.source-upload-[A-Za-z0-9_-]+\.part', stage.name)
            or not isinstance(filename, str) or filename in ('', '.', '..')
            or '/' in filename or '\\' in filename or '\0' in filename
            or filename.startswith('.') or Path(filename).suffix.lower() not in ('.txt', '.md')
            or type(limit) is not int or limit < 0 or not callable(on_visible)):
        raise ValueError('Invalid source upload publication')
    before = stage.lstat()
    if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
            or before.st_size > limit):
        raise ValueError('Source upload stage is unsafe or exceeds its limit')
    # Match the opened file to the private path before using it as a source.
    # Windows FlushFileBuffers requires write access; retain the actual fsync
    # instead of silently treating an unsupported read-only flush as success.
    with stage.open('r+b') as source:
        opened = os.fstat(source.fileno())
        if (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) != (
                before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns):
            raise ValueError('Source upload stage changed before publication')
        os.fsync(source.fileno())
        if companion_bytes is not None:
            from publication_source import get_publication_source_document
            document = get_publication_source_document(companion_bytes)
            digest = hashlib.sha256()
            for block in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(block)
            if document['binding'] != {'kind': 'text', 'sha256': digest.hexdigest()}:
                raise ValueError('Source upload provenance does not match its text')
    base, extension = os.path.splitext(filename)
    for number in range(1, 1002):
        name = filename if number == 1 else f'{base}_{number}{extension}'
        destination = stage.parent / name
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
                descriptor = os.open(companion, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
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
            move_source_upload_exclusively(stage, destination)
            published = True
        except FileExistsError:
            continue
        finally:
            if companion_identity is not None and not published:
                facts = companion.lstat()
                if (facts.st_dev, facts.st_ino) != companion_identity:
                    raise ValueError('Source provenance changed during publication')
                companion.unlink()
        on_visible(str(destination))
        sync_adapter_directory(str(stage.parent))
        return str(destination)
    raise ValueError('Too many source upload filename collisions')
