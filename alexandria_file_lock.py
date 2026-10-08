"""Nonblocking exclusive kernel locks shared by app and preparer callers."""
import os


def get_default_gpu_lock_path(repo_dir):
    """Resolve the shared native path without requiring a shell or creating it."""
    root = os.path.realpath(repo_dir)
    git_file = os.path.join(root, '.git')
    if os.path.isfile(git_file):
        with open(git_file, encoding='utf-8') as stream:
            entry = stream.read().strip()
        if not entry.startswith('gitdir: '):
            raise ValueError('Invalid worktree Git directory')
        git_dir = os.path.realpath(os.path.join(root, entry[8:]))
        common_file = os.path.join(git_dir, 'commondir')
        if os.path.isfile(common_file):
            with open(common_file, encoding='utf-8') as stream:
                common = stream.read().strip()
            if not common:
                raise ValueError('Empty worktree common directory')
            root = os.path.dirname(os.path.realpath(os.path.join(git_dir, common)))
    return os.path.join(root, 'ab_test_runtime', 'logs',
                        'alexandria_gpu.lock')


def acquire_exclusive_file_lock(descriptor):
    if os.name == 'nt':
        import msvcrt
        position = os.lseek(descriptor, 0, os.SEEK_CUR)
        try:
            if os.lseek(descriptor, 0, os.SEEK_END) == 0:
                os.write(descriptor, b'\0')
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        finally:
            os.lseek(descriptor, position, os.SEEK_SET)
    else:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)


def release_exclusive_file_lock(descriptor):
    if os.name == 'nt':
        import msvcrt
        position = os.lseek(descriptor, 0, os.SEEK_CUR)
        try:
            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        finally:
            os.lseek(descriptor, position, os.SEEK_SET)
    else:
        import fcntl
        fcntl.flock(descriptor, fcntl.LOCK_UN)


if __name__ == '__main__':
    import sys
    if len(sys.argv) != 2:
        raise SystemExit('Usage: alexandria_file_lock.py REPOSITORY')
    print(get_default_gpu_lock_path(sys.argv[1]))
