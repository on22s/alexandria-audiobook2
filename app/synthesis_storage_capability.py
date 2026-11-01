"""Shared, side-effect-free qualification of the synthesis storage primitive set."""
import os
import sys


_PLATFORMS = frozenset(('linux', 'darwin'))
_DIR_FD_PRIMITIVES = ('open', 'stat', 'unlink', 'rename', 'mkdir', 'rmdir')


def is_synthesis_storage_capable():
    if sys.platform not in _PLATFORMS:
        return False
    if not hasattr(os, 'O_DIRECTORY') or not hasattr(os, 'O_NOFOLLOW'):
        return False
    supported = os.supports_dir_fd
    return all(getattr(os, name, None) in supported for name in _DIR_FD_PRIMITIVES)
