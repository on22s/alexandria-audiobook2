"""The placeholder a POSIX book switch leaves at a name it is creating or
removing (#1660, the v4 publication protocol).

While a switch holds the book lock, a target being created or removed can
briefly hold this 64-byte file instead of being absent: the prefix, 32
lowercase hex of nonce, and a newline. Readers that run without the lock must
treat it as "not there yet", never as content. This module is the one
definition: the writer builds the bytes with get_book_placeholder_bytes, and
every reader recognises them with is_book_placeholder (Rule 15).
"""
import os
import re
import stat

PLACEHOLDER_PREFIX = b'alexandria-book-placeholder-v1 '
PLACEHOLDER_SIZE = 64
_PLACEHOLDER = re.compile(re.escape(PLACEHOLDER_PREFIX) + rb'[0-9a-f]{32}\n')


def get_book_placeholder_bytes(nonce):
    """-> the exact placeholder bytes for a 32-lowercase-hex nonce."""
    if not isinstance(nonce, str) or re.fullmatch('[0-9a-f]{32}', nonce) is None:
        raise ValueError('Book placeholder nonce must be 32 lowercase hex digits')
    return PLACEHOLDER_PREFIX + nonce.encode('ascii') + b'\n'


def is_book_placeholder_stream(stream, size):
    """Whether an already-open binary stream of `size` bytes is a placeholder.

    For readers that serve or copy what they opened: checking the open file,
    not the name, leaves no window for a switch to land in between. The
    stream is left at offset 0."""
    if size != PLACEHOLDER_SIZE:
        return False
    stream.seek(0)
    raw = stream.read(PLACEHOLDER_SIZE + 1)
    stream.seek(0)
    return is_book_placeholder_bytes(raw)


def is_book_placeholder_bytes(raw):
    """Whether raw is exactly a book placeholder."""
    return isinstance(raw, (bytes, bytearray)) and _PLACEHOLDER.fullmatch(bytes(raw)) is not None


def is_book_placeholder(path):
    """Whether path is a regular file holding exactly a book placeholder.

    Never follows a final symlink, and reads at most 65 bytes. Any error
    (missing, unreadable, a directory) is False: callers keep their own
    handling for those."""
    try:
        info = os.lstat(path)
        if not stat.S_ISREG(info.st_mode) or info.st_size != PLACEHOLDER_SIZE:
            return False
        flags = (os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
                 | getattr(os, 'O_BINARY', 0))
        descriptor = os.open(path, flags)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                return False   # replaced after the lstat
            raw = os.read(descriptor, PLACEHOLDER_SIZE + 1)
        finally:
            os.close(descriptor)
    except OSError:
        return False
    return is_book_placeholder_bytes(raw)
