"""Nonblocking exclusive kernel locks shared by app and preparer callers."""
import os


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
