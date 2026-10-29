"""Plain-path admission; live operations still need their identity guards."""
import os
from pathlib import Path
import stat


def is_plain_path(path):
    """Accept DOS short spellings, but refuse existing reparse components.

    Missing trailing components are allowed for publication destinations.
    This is an admission observation, not protection against later swaps.
    POSIX retains the existing no-aliased-ancestors policy.
    """
    path = Path(os.path.abspath(path))
    if os.name != 'nt':
        return os.path.normcase(os.path.realpath(path)) == os.path.normcase(str(path))
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        if (stat.S_ISLNK(info.st_mode) or
                getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            return False
    return True
