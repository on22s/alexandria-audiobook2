"""Kernel task leases coordinate admission and stale-state recovery across servers."""
from contextlib import contextmanager
import errno
import os
from pathlib import Path
import re

from utils import file_lock


class TaskOwnershipBusy(RuntimeError):
    """Another process still owns a conflicting task or startup recovery."""


def ensure_task_ownership_directory(data_dir):
    directory = Path(data_dir).resolve() / '.task_ownership'
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def acquire_task_slot(directory, task_name):
    if not isinstance(task_name, str) or re.fullmatch(r'[A-Za-z0-9_]+', task_name) is None:
        raise ValueError('Invalid task ownership name')
    handle = (directory / ('task-' + task_name + '.lock')).open('a+b')
    try:
        if os.name == 'nt':
            import msvcrt
            handle.seek(0, os.SEEK_END)
            if not handle.tell():
                handle.write(b'\0')
                handle.flush()
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return handle
    except OSError as error:
        handle.close()
        if error.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
            raise TaskOwnershipBusy(f'Task {task_name} is owned by another worker') from error
        raise
    except BaseException:
        handle.close()
        raise


def get_task_ownership_slot_name(task_name, *, cpu_only=False):
    """Keep CPU resource slots distinct from caller-visible task identities."""
    if (not isinstance(task_name, str) or task_name.startswith('cpu__')
            or re.fullmatch(r'[A-Za-z0-9_]+', task_name) is None):
        raise ValueError('Invalid task ownership name')
    return 'cpu__' + task_name if cpu_only else task_name


def acquire_task_lease(data_dir, task_name, conflicts, *, cpu_only=False):
    """Reserve one slot while the same barrier excludes startup cleanup/admission."""
    slot = get_task_ownership_slot_name(task_name, cpu_only=cpu_only)
    opposite = get_task_ownership_slot_name(task_name, cpu_only=not cpu_only)
    directory = ensure_task_ownership_directory(data_dir)
    lease = None
    try:
        with file_lock(directory / 'admission', timeout=0):
            if (directory / ('task-' + opposite + '.lock')).exists():
                probe = acquire_task_slot(directory, opposite)
                probe.close()
            for conflict in sorted(set() if cpu_only else set(conflicts) - {task_name}):
                probe = acquire_task_slot(directory, conflict)
                probe.close()
            lease = acquire_task_slot(directory, slot)
        return lease
    except TimeoutError as error:
        if lease is not None:
            lease.close()
        raise TaskOwnershipBusy('Task admission or startup recovery is in progress') from error
    except BaseException:
        if lease is not None:
            lease.close()
        raise


@contextmanager
def ensure_startup_recovery(data_dir, timeout=10):
    """Permit cleanup only without any live lease; hold admission through cleanup."""
    directory = ensure_task_ownership_directory(data_dir)
    with file_lock(directory / 'admission', timeout=timeout):
        busy = []
        for path in sorted(directory.glob('task-*.lock')):
            name = path.name[len('task-'):-len('.lock')]
            try:
                probe = acquire_task_slot(directory, name)
            except TaskOwnershipBusy:
                busy.append(name)
            else:
                probe.close()
        yield not busy
