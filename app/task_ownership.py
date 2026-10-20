"""Kernel task leases coordinate admission and stale-state recovery across servers."""
from contextlib import contextmanager
import errno
import hashlib
import json
import os
from pathlib import Path
import re
import time

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


def get_task_resource_path(path):
    path = os.path.abspath(path)
    return os.path.normcase(os.path.join(os.path.realpath(os.path.dirname(path)),
                                        os.path.basename(path)))


def get_task_resource_slot(path):
    return 'resource_' + hashlib.sha256(os.fsencode(get_task_resource_path(path))).hexdigest()


def _save_task_resources(lease, resources):
    # Keep byte zero for Windows' byte-range lock, and preserve the locked inode.
    lease.seek(0, os.SEEK_END)
    if not lease.tell():
        lease.write(b'\0')
    lease.truncate(1)
    lease.write(json.dumps({'resources': resources}).encode('utf-8'))
    lease.flush()


def _load_task_resources(path):
    try:
        with path.open('rb') as source:
            source.seek(1)
            value = json.load(source)['resources']
        if isinstance(value, list) and all(isinstance(item, str) and os.path.isabs(item) for item in value):
            return {get_task_resource_path(item) for item in value}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return None  # Legacy or unreadable scope must never authorize a mutation.


@contextmanager
def ensure_task_resources_available(data_dir, task_names, resources, *, mutation_timeout=0):
    """Fence tasks immediately; optionally serialize already-running mutations."""
    directory = ensure_task_ownership_directory(data_dir)
    paths = {get_task_resource_path(path) for path in resources}
    held = []
    deadline = time.monotonic() + mutation_timeout
    try:
        while True:
            retry = False
            try:
                with file_lock(directory / 'admission', timeout=0):
                    for task_name in task_names:
                        for cpu_only in (False, True):
                            slot = get_task_ownership_slot_name(task_name, cpu_only=cpu_only)
                            path = directory / ('task-' + slot + '.lock')
                            if not path.exists():
                                continue
                            try:
                                probe = acquire_task_slot(directory, slot)
                            except TaskOwnershipBusy:
                                owned = _load_task_resources(path)
                                if owned is None or paths & owned:
                                    raise
                            else:
                                probe.close()
                    try:
                        for path in sorted(paths):
                            held.append(acquire_task_slot(directory, get_task_resource_slot(path)))
                    except TaskOwnershipBusy:
                        for lease in reversed(held):
                            lease.close()
                        held.clear()
                        if time.monotonic() >= deadline:
                            raise
                        retry = True
            except TimeoutError as error:
                raise TaskOwnershipBusy('Task admission or startup recovery is in progress') from error
            if not retry:
                break
            time.sleep(min(.01, max(0, deadline - time.monotonic())))
        yield
    finally:
        for lease in reversed(held):
            lease.close()


def acquire_task_lease(data_dir, task_name, conflicts, *, cpu_only=False, resources=None):
    """Reserve one slot while the same barrier excludes startup cleanup/admission."""
    slot = get_task_ownership_slot_name(task_name, cpu_only=cpu_only)
    opposite = get_task_ownership_slot_name(task_name, cpu_only=not cpu_only)
    directory = ensure_task_ownership_directory(data_dir)
    lease = None
    paths = None if resources is None else sorted({get_task_resource_path(path) for path in resources})
    try:
        with file_lock(directory / 'admission', timeout=0):
            if (directory / ('task-' + opposite + '.lock')).exists():
                probe = acquire_task_slot(directory, opposite)
                probe.close()
            for conflict in sorted(set() if cpu_only else set(conflicts) - {task_name}):
                probe = acquire_task_slot(directory, conflict)
                probe.close()
            for path in paths or ():
                probe = acquire_task_slot(directory, get_task_resource_slot(path))
                probe.close()
            lease = acquire_task_slot(directory, slot)
            _save_task_resources(lease, paths)
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
