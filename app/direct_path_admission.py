"""Plain-path admission; live operations still need their identity guards."""
import os
from pathlib import Path
import stat

# POSIX owners whose symlinked ancestors are trusted. Only root can create or
# retarget /var, /tmp or /etc on macOS, or an admin-made symlinked volume.
TRUSTED_ALIAS_OWNERS = frozenset({0})
# Bounds chained aliases, like the kernel's own symlink-loop limit.
MAX_ALIAS_DEPTH = 40


def _stat_directory(path):
    """Read a parent directory's owner and mode (a seam for tests without root)."""
    return path.stat()


def _is_administrator_controlled(link, info):
    """True when no unprivileged user could have created or can swap `link`.

    The link and every directory from / down to the one holding it must be
    owned by a trusted owner and not group- or world-writable (so a sticky
    shared directory does not qualify).
    """
    if info.st_uid not in TRUSTED_ALIAS_OWNERS:
        return False
    for directory in reversed(link.parents):
        try:
            # Earlier components were admitted already, so follow them here.
            parent = _stat_directory(directory)
        except OSError:
            return False
        if (not stat.S_ISDIR(parent.st_mode) or parent.st_uid not in TRUSTED_ALIAS_OWNERS
                or parent.st_mode & (stat.S_IWGRP | stat.S_IWOTH)):
            return False
    return True


def _is_plain_posix_spelling(path, depth):
    """Check `path` as written; the kernel applies '..' after following aliases."""
    if '..' in path.parts:
        # Lexical normalization would skip a link before '..', so a spelling
        # with '..' is admitted only when no component is a symlink at all.
        prefix = Path(path.anchor)
        for part in path.parts[1:]:
            prefix = prefix / part
            if part == '..':
                continue
            try:
                if stat.S_ISLNK(prefix.lstat().st_mode):
                    return False
            except FileNotFoundError:
                continue
            except OSError:
                return False
        return os.path.normcase(os.path.realpath(path)) == os.path.normcase(os.path.abspath(path))
    return _is_plain_posix_path(Path(os.path.abspath(path)), depth)


def _is_plain_posix_path(path, depth):
    """An admitted alias counts as its target spelled directly, so check that too."""
    if depth > MAX_ALIAS_DEPTH:
        return False
    admitted_alias = False
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue  # publication destinations may not exist yet
        except OSError:
            return False  # a file used as a directory, a loop or an unreadable entry
        if stat.S_ISLNK(info.st_mode):
            # Only the requested path itself must not be a link; an alias
            # target may be another administrator alias.
            if (component == path and depth == 0) or not _is_administrator_controlled(component, info):
                return False
            try:
                # Relative targets resolve from the link's physical directory.
                target = Path(os.path.realpath(component.parent)) / os.readlink(component)
            except OSError:
                return False
            if not _is_plain_posix_spelling(target, depth + 1):
                return False
            admitted_alias = True
    if admitted_alias:
        return True
    return os.path.normcase(os.path.realpath(path)) == os.path.normcase(str(path))


def is_plain_path(path):
    """Refuse aliases an unprivileged user could create or swap.

    Windows accepts DOS short spellings but refuses existing reparse
    components. POSIX refuses symlinks, except an ancestor symlink that only an
    administrator controls, such as macOS /var -> /private/var (#1659).
    Missing trailing components are allowed for publication destinations.
    This is an admission observation, not protection against later swaps.
    """
    if os.name != 'nt':
        return _is_plain_posix_spelling(Path(os.getcwd(), path), 0)
    path = Path(os.path.abspath(path))
    for component in (*reversed(path.parents), path):
        try:
            info = component.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            return False
        if (stat.S_ISLNK(info.st_mode) or
                getattr(info, 'st_file_attributes', 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT):
            return False
    return True


def get_direct_directory_root(path):
    """Return an admitted directory's canonical spelling and observed identity.

    Ancestor aliases are admitted only while the shared trust policy accepts
    them, and the final component must never be an alias, so resolving the
    spelling cannot launder a root that is itself a link. The identity is an
    observation: callers must still pin it for live operations.
    """
    named = Path(os.path.abspath(path))
    if not is_plain_path(named):
        raise ValueError('Book root must be a direct directory without aliased ancestors')
    before = named.lstat()
    if not stat.S_ISDIR(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise ValueError('Book root must be a real directory')
    root = named if os.name == 'nt' else Path(os.path.realpath(named))
    identity = (before.st_dev, before.st_ino)
    current = root.lstat()
    after = named.lstat()
    if (not stat.S_ISDIR(current.st_mode) or stat.S_ISLNK(current.st_mode)
            or stat.S_ISLNK(after.st_mode)
            or (current.st_dev, current.st_ino) != identity
            or (after.st_dev, after.st_ino) != identity
            or (os.name != 'nt' and Path(os.path.realpath(named)) != root)):
        raise ValueError('Book root changed during admission')
    return root, identity


def get_direct_spelling(path):
    """Return the admitted spelling of a path whose ancestors may be aliased.

    Ancestor aliases are captured once so stored paths and worker environments
    keep one root identity. A spelling the trust policy refuses is returned as
    written, so admission still reports it instead of following the alias into
    a target no policy admitted.
    """
    named = Path(os.path.abspath(path))
    if os.name == 'nt' or not is_plain_path(named):
        return str(named)
    return os.path.join(os.path.realpath(named.parent), named.name)


def get_canonical_plain_path(path):
    """Return the resolved form of an admitted path, for descriptor walks.

    Walks that open each component with O_NOFOLLOW must start from the
    canonical form; callers still compare identities with the admitted root.
    """
    if not is_plain_path(path):
        raise ValueError('Path is not a direct path')
    return Path(os.path.realpath(os.path.abspath(path)))
