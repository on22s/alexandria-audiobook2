"""Crash-recoverable private export staging for Windows.

The registry is a private sibling of the export parent. A bounded slot holds the
stage, its owner lock, and immutable receipts. Recovery never treats a name or a
receipt alone as deletion authority: the stage and owner must still match their
admitted identities, and a live owner is preserved.

The same state machine runs under the POSIX stage authority in tests. Native
Windows execution uses windows_export_staging_directory through stage_directory.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import stat
import time
from contextlib import ExitStack

from audio_take_history import _version
from export_staging_directory import (OWNER_FILE, StageDirectoryChanged, identity,
                                      stage_directory, stage_owner_lock)
from export_staging_records import (decode_creation_receipt, decode_receipt,
                                    receipt_owner_identity)
from utils import file_lock

DIRECTORY = '.aa2-export-retirement'
LOCK = 'owner.lock'
MAX_SLOTS = 128
RETIREMENT_RESERVE = '0' * 32
PENDING_SUFFIX = '.pending'
MAX_RECEIPT_BYTES = 8192


def _is_reparse(info):
    return bool(getattr(info, 'st_file_attributes', 0) & 0x400)


def _check_directory(path, *, label):
    info = os.stat(path, follow_symlinks=False)
    if not stat.S_ISDIR(info.st_mode) or _is_reparse(info):
        raise StageDirectoryChanged(f'Unsafe {label}')
    return info


def _safe_name(name):
    return (type(name) is str and bool(name) and name not in ('.', '..')
            and not any(character in name for character in ('/', '\\', '\x00')))


def _write_json(path, value):
    raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')
    if len(raw) > MAX_RECEIPT_BYTES:
        raise StageDirectoryChanged('Retirement receipt exceeds limit')
    return raw


def _read_bytes(path, limit=MAX_RECEIPT_BYTES):
    named = os.stat(path, follow_symlinks=False)
    if (not stat.S_ISREG(named.st_mode) or named.st_nlink != 1
            or named.st_size > limit or _is_reparse(named)):
        raise StageDirectoryChanged('Unsafe retirement metadata')
    with open(path, 'rb') as stream:
        before = os.fstat(stream.fileno())
        if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                or before.st_size > limit or _version(before) != _version(named)):
            raise StageDirectoryChanged('Unsafe retirement metadata')
        raw = stream.read(limit + 1)
        after = os.fstat(stream.fileno())
        current = os.stat(path, follow_symlinks=False)
        if (len(raw) > limit or _version(before) != _version(after)
                or _version(before) != _version(current) or _is_reparse(current)):
            raise StageDirectoryChanged('Retirement metadata changed while reading')
    return raw


def _atomic_write(path, raw):
    if path.exists():
        raise StageDirectoryChanged('Retirement receipt already exists')
    pending = path.with_name(path.name + PENDING_SUFFIX)
    descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL
                         | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(descriptor, 'wb') as stream:
            if stream.write(raw) != len(raw):
                raise OSError('Short retirement receipt write')
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        try:
            pending.unlink()
        except OSError:
            pass
        raise
    os.replace(pending, path)
    if os.name != 'nt':
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


class LiveOwnerPreserved(StageDirectoryChanged):
    """A live stage owner is a preservation outcome, not a reclaim error."""


class WindowsRetirement:
    """One registry transaction; path operations are bounded to a private root."""

    def __init__(self, parent=None, authority=None, *, wait=False, create=True,
                 expected_parent_identity=None):
        if (parent is None) == (authority is None):
            raise ValueError('Provide either an export parent or an admitted stage')
        if type(wait) is not bool or type(create) is not bool:
            raise ValueError('Invalid retirement transaction flags')
        if (expected_parent_identity is not None
                and (type(expected_parent_identity) is not tuple
                     or len(expected_parent_identity) != 2
                     or any(type(value) is not int or value < 0
                            for value in expected_parent_identity))):
            raise ValueError('Invalid retirement parent identity')
        self.authority = authority
        if authority is not None:
            stage = Path(authority.path).absolute()
            if (stage.name != 'stage' or stage.parent.parent.name != DIRECTORY
                    or re.fullmatch(r'[0-9a-f]{32}', stage.parent.name) is None):
                raise ValueError('Admitted stage is not a private retirement stage')
            parent = stage.parents[2]
        self.parent = Path(parent).absolute()
        self.expected_parent_identity = expected_parent_identity
        self.wait = wait
        self.create = create
        self.stack = ExitStack()
        self.registry = self.parent / DIRECTORY
        self.slot = None
        self.entered = False

    def __enter__(self):
        if self.entered:
            raise StageDirectoryChanged('Retirement transaction cannot be reused')
        self.entered = True
        try:
            parent_info = _check_directory(self.parent, label='export parent')
            if (self.expected_parent_identity is not None
                    and identity(parent_info) != self.expected_parent_identity):
                raise StageDirectoryChanged('Retirement parent changed')
            if self.authority is not None:
                self.authority.check()
                if identity(_check_directory(self.authority.path.parents[2],
                                             label='staging parent')) != identity(parent_info):
                    raise StageDirectoryChanged('Staging and export parents differ')
            if self.create:
                try:
                    self.registry.mkdir(mode=0o700)
                except FileExistsError:
                    pass
            self.registry_info = _check_directory(self.registry, label='retirement registry')
            self.parent_info = parent_info
            lock_path = self.registry / LOCK
            descriptor = os.open(lock_path, os.O_RDWR | (os.O_CREAT if self.create else 0)
                                 | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0),
                                 0o600)
            stream = self.stack.enter_context(os.fdopen(descriptor, 'r+b'))
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 4096:
                raise StageDirectoryChanged('Unsafe retirement lock')
            self.stack.enter_context(file_lock(self.registry / 'owner',
                                               timeout=10 if self.wait else 0,
                                               lock_handle=stream))
            if identity(os.stat(lock_path, follow_symlinks=False)) != identity(info):
                raise StageDirectoryChanged('Retirement lock was replaced')
            if identity(_check_directory(self.registry, label='retirement registry')) != identity(self.registry_info):
                raise StageDirectoryChanged('Retirement registry was replaced')
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *error):
        self.stack.close()
        self.slot = None

    def check(self):
        if identity(_check_directory(self.parent, label='export parent')) != identity(self.parent_info):
            raise StageDirectoryChanged('Export parent changed during retirement')
        if identity(_check_directory(self.registry, label='retirement registry')) != identity(self.registry_info):
            raise StageDirectoryChanged('Retirement registry was replaced')
        if self.slot is not None:
            self._slot_path()

    def _slot_path(self, name=None):
        name = self.slot if name is None else name
        if type(name) is not str or re.fullmatch(r'[0-9a-f]{32}', name) is None:
            raise StageDirectoryChanged('Invalid retirement slot')
        path = self.registry / name
        _check_directory(path, label='retirement slot')
        return path

    def _check_slot(self):
        if self.slot is not None:
            self._slot_path()
        if identity(_check_directory(self.registry, label='retirement registry')) != identity(self.registry_info):
            raise StageDirectoryChanged('Retirement registry was replaced')

    def slot_names(self):
        self._check_slot()
        names = []
        with os.scandir(self.registry) as entries:
            for entry in entries:
                if entry.name == LOCK:
                    continue
                if not _safe_name(entry.name) or len(names) >= MAX_SLOTS + 1:
                    raise StageDirectoryChanged('Retirement namespace exceeds capacity')
                names.append(entry.name)
        if len(names) > MAX_SLOTS and RETIREMENT_RESERVE not in names:
            raise StageDirectoryChanged('Retirement namespace exceeds capacity')
        if RETIREMENT_RESERVE in names:
            names.remove(RETIREMENT_RESERVE)
            names.insert(0, RETIREMENT_RESERVE)
        return names

    def allocate_slot(self):
        if self.slot is not None:
            raise StageDirectoryChanged('Slot already allocated')
        names = self.slot_names()
        if len(names) >= MAX_SLOTS:
            raise StageDirectoryChanged('Retirement capacity exhausted')
        slot = secrets.token_hex(16)
        while slot == RETIREMENT_RESERVE or (self.registry / slot).exists():
            slot = secrets.token_hex(16)
        (self.registry / slot).mkdir(mode=0o700)
        self.slot = slot
        self._check_slot()
        return slot

    def open_slot(self, name):
        if self.slot is not None:
            raise StageDirectoryChanged('A retirement slot is already open')
        self._slot_path(name)
        self.slot = name
        self._check_slot()

    def _write_receipt(self, name, value):
        if name not in ('allocation.json', 'creation.json', 'owner.json',
                        'reserved.json', 'verified.json'):
            raise StageDirectoryChanged('Invalid retirement receipt name')
        self._check_slot()
        path = self._slot_path() / name
        _atomic_write(path, _write_json(path, value))

    def _read_receipt(self, name):
        self._check_slot()
        path = self._slot_path() / name
        raw = _read_bytes(path)
        if name in ('allocation.json', 'creation.json', 'owner.json'):
            return decode_creation_receipt(
                raw, complete=name != 'allocation.json', owner=name == 'owner.json')
        return decode_receipt(raw)

    def _read_creation(self):
        try:
            allocated = self._read_receipt('allocation.json')
        except FileNotFoundError:
            allocated = None
        try:
            created = self._read_receipt('creation.json')
        except FileNotFoundError:
            created = None
        if allocated is not None:
            if (allocated['parent_identity'] != list(identity(_check_directory(self.parent, label='export parent')))
                    or allocated['slot_identity'] != list(identity(_check_directory(self.registry / self.slot,
                                                                                    label='retirement slot')))):
                raise StageDirectoryChanged('Creation reservation identity mismatch')
        if created is not None:
            if allocated is None or ({key: value for key, value in created.items()
                                      if key != 'stage_identity'} != allocated):
                raise StageDirectoryChanged('Creation receipts disagree')
        return allocated, created

    def _read_owner(self, created):
        try:
            owned = self._read_receipt('owner.json')
        except FileNotFoundError:
            return None
        if created is None or ({key: value for key, value in owned.items()
                               if key != 'owner_identity'} != created):
            raise StageDirectoryChanged('Initialization owner receipt disagrees')
        return owned

    def initialize_creation(self, kind):
        from export_staging import PREFIXES, MARKER, _write_marker
        if type(kind) is not str or kind not in PREFIXES:
            raise StageDirectoryChanged('Unknown export staging kind')
        owner = None
        entered = False
        try:
            self.allocate_slot()
            allocation = {'schema': 1, 'kind': kind, 'token': secrets.token_hex(16),
                          'parent_identity': list(identity(_check_directory(self.parent, label='export parent'))),
                          'slot_identity': list(identity(os.stat(self._slot_path(), follow_symlinks=False)))}
            self._write_receipt('allocation.json', allocation)
            stage_path = self._slot_path() / 'stage'
            stage_path.mkdir(mode=0o700)
            stage_info = _check_directory(stage_path, label='initialization stage')
            creation = {**allocation, 'stage_identity': list(identity(stage_info))}
            self._write_receipt('creation.json', creation)
            with stage_directory(stage_path, tuple(creation['stage_identity'])) as authority:
                owner = stage_owner_lock(authority, create=True)
                stream = owner.__enter__()
                entered = True
                owned = {**creation, 'owner_identity': list(identity(os.fstat(stream.fileno())))}
                os.fsync(stream.fileno())
                self._write_receipt('owner.json', owned)
                marker = {'schema': 1, 'kind': kind, 'prefix': PREFIXES[kind],
                          'parent': str(Path(self.parent).resolve()), 'token': allocation['token'],
                          'owner_identity': owned['owner_identity'],
                          'pid': os.getpid(), 'created_ns': time.time_ns()}
                _write_marker(stage_path / MARKER, marker, authority=authority)
                authority.check_owner_stream(stream)
            return stage_path, tuple(creation['stage_identity']), marker, owner, stream, self.slot
        except BaseException as original:
            try:
                if entered:
                    owner.__exit__(None, None, None)
                if self.slot is not None:
                    slot, self.slot = self.slot, None
                    self.recover_slot(slot)
            except BaseException as cleanup_error:
                raise original from cleanup_error
            raise

    def _validate_creation(self, authority, stream):
        from export_staging import PREFIXES, decode_staging_marker
        allocated, created = self._read_creation()
        owned = self._read_owner(created)
        if owned is None or authority.expected != tuple(owned['stage_identity']):
            raise StageDirectoryChanged('Initialized stage identity mismatch')
        authority.check_owner_stream(stream)
        owner_info = os.fstat(stream.fileno())
        if list(identity(owner_info)) != owned['owner_identity']:
            raise StageDirectoryChanged('Initialized owner identity mismatch')
        raw = authority.read_marker_bytes(4096)
        marker = decode_staging_marker(raw)
        expected = {'schema': 1, 'kind': created['kind'],
                    'prefix': PREFIXES[created['kind']], 'parent': str(Path(self.parent).resolve()),
                    'token': created['token'], 'owner_identity': owned['owner_identity']}
        if marker is None or any(marker.get(key) != value for key, value in expected.items()):
            raise StageDirectoryChanged('Initialized marker authority mismatch')
        return raw, created, owned

    def validate_initialized_creation(self, authority, stream):
        return self._validate_creation(authority, stream)[1:]

    def cleanup_initialized_creation(self, authority, stream, *, release_owner=lambda: None,
                                     recovery=False):
        from export_staging import MARKER, _remove_contents
        from export_staging_directory import OWNER_FILE
        raw, created, owned = self._validate_creation(authority, stream)
        if authority.has_recovery_guard() or (recovery and authority.has_rollback_backup()):
            raise StageDirectoryChanged('Initialized recovery payload preserved')
        _remove_contents(authority.path, authority=authority, retirement=self)
        if authority.read_marker_bytes(4096) != raw:
            raise StageDirectoryChanged('Initialized marker changed')
        authority.check_owner_stream(stream)
        authority.unlink(MARKER)
        release_owner()
        authority.unlink(OWNER_FILE)
        authority.remove_empty_directory()

    def discard_empty_slot(self):
        self._check_slot()
        slot_path = self._slot_path()
        if (slot_path / 'stage').exists():
            raise StageDirectoryChanged('Cannot discard a populated retirement slot')
        allowed = {'allocation.json', 'creation.json', 'owner.json',
                   'allocation.json' + PENDING_SUFFIX, 'creation.json' + PENDING_SUFFIX,
                   'owner.json' + PENDING_SUFFIX}
        with os.scandir(slot_path) as entries:
            names = [entry.name for entry in entries]
        if any(name not in allowed for name in names):
            raise StageDirectoryChanged('Unknown retirement contents preserved')
        admitted = {}
        for name in names:
            path = slot_path / name
            info = os.stat(path, follow_symlinks=False)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_size > MAX_RECEIPT_BYTES or _is_reparse(info)):
                raise StageDirectoryChanged('Unsafe retirement metadata preserved')
            admitted[name] = identity(info)
        _, created = self._read_creation()
        self._read_owner(created)
        # Retire dependents before prerequisites so a hard exit always leaves
        # a subset that the next recovery can still validate.
        removal_order = ('owner.json.pending', 'creation.json.pending',
                         'allocation.json.pending', 'owner.json',
                         'creation.json', 'allocation.json')
        for name in removal_order:
            if name not in admitted:
                continue
            path = slot_path / name
            if identity(os.stat(path, follow_symlinks=False)) != admitted[name]:
                raise StageDirectoryChanged('Retirement metadata changed before removal')
            path.unlink()
        slot_path.rmdir()
        self.slot = None

    def recover_slot(self, name):
        from export_staging import MARKER, PREFIXES, decode_staging_marker, _matches_parent
        from export_staging_directory import OWNER_FILE
        self.open_slot(name)
        try:
            stage_path = self._slot_path() / 'stage'
            try:
                stage_info = _check_directory(stage_path, label='retirement stage')
            except FileNotFoundError:
                self.discard_empty_slot()
                return 'metadata_removed'
            allocated, created = self._read_creation()
            owned = self._read_owner(created)
            with stage_directory(stage_path, identity(stage_info)) as authority:
                if authority.has_recovery_guard() or authority.has_rollback_backup():
                    raise StageDirectoryChanged('Recovery payload preserved')
                names = set()
                with os.scandir(stage_path) as entries:
                    for entry in entries:
                        names.add(entry.name)
                if created is None:
                    if allocated is None or names:
                        raise StageDirectoryChanged('Unowned retirement stage preserved')
                    authority.remove_empty_directory()
                    outcome = 'metadata_removed'
                elif list(identity(stage_info)) != created['stage_identity']:
                    raise StageDirectoryChanged('Initialization stage identity mismatch')
                elif owned is None:
                    if names == {OWNER_FILE}:
                        try:
                            owner = stage_owner_lock(authority)
                            stream = owner.__enter__()
                        except TimeoutError:
                            raise LiveOwnerPreserved('Live staging owner preserved')
                        try:
                            release = owner.__exit__
                            release(None, None, None)
                        finally:
                            pass
                        authority.unlink(OWNER_FILE)
                        authority.remove_empty_directory()
                        outcome = 'stage_removed'
                    elif names - {MARKER, OWNER_FILE}:
                        raise StageDirectoryChanged('Unowned retirement stage preserved')
                    elif names:
                        raise StageDirectoryChanged('Initialization owner is missing')
                    else:
                        authority.remove_empty_directory()
                        outcome = 'metadata_removed'
                else:
                    if not names:
                        authority.remove_empty_directory()
                        outcome = 'stage_removed'
                    else:
                        try:
                            owner = stage_owner_lock(authority)
                            stream = owner.__enter__()
                        except TimeoutError:
                            raise LiveOwnerPreserved('Live staging owner preserved')
                        released = False
                        def release_owner():
                            nonlocal released
                            if not released:
                                released = True
                                owner.__exit__(None, None, None)
                        try:
                            if list(identity(os.fstat(stream.fileno()))) != owned['owner_identity']:
                                raise StageDirectoryChanged('Retirement owner identity changed')
                            if names == {OWNER_FILE}:
                                release_owner()
                                authority.unlink(OWNER_FILE)
                                authority.remove_empty_directory()
                                outcome = 'stage_removed'
                            else:
                                raw = authority.read_marker_bytes(4096)
                                marker = decode_staging_marker(raw)
                                if (marker is None or not _matches_parent(marker, self.parent)
                                        or not marker.get('prefix') == PREFIXES.get(marker.get('kind'))
                                        or marker.get('owner_identity') != owned['owner_identity']):
                                    raise StageDirectoryChanged('Retirement marker authority changed')
                                self.cleanup_initialized_creation(
                                    authority, stream, release_owner=release_owner,
                                    recovery=True)
                                outcome = 'stage_removed'
                        finally:
                            release_owner()
            self.discard_empty_slot()
            return outcome
        except BaseException:
            raise

    def reclaim(self, *, max_remove):
        result = {'removed': [], 'preserved': [], 'errors': []}
        for name in self.slot_names():
            try:
                outcome = self.recover_slot(name)
            except LiveOwnerPreserved:
                result['preserved'].append(str(self.registry / name / 'stage'))
            except (OSError, RuntimeError) as error:
                stage_path = self.registry / name / 'stage'
                display = stage_path if os.path.lexists(stage_path) else self.registry / name
                result['preserved'].append(str(display))
                result['errors'].append(f'{display}: {error}')
            else:
                result['removed'].append(str(self.registry / name / 'stage'
                                             if outcome == 'stage_removed'
                                             else self.registry / name))
                if len(result['removed']) >= max_remove:
                    break
            finally:
                self.slot = None
        return result


def reclaim_windows_retired_slots(parent, *, max_remove, expected_parent_identity=None):
    parent = Path(parent)
    if not (parent / DIRECTORY).exists():
        return {'removed': [], 'preserved': [], 'errors': []}
    if type(max_remove) is not int or max_remove <= 0:
        raise ValueError('Retirement removal budget must be a positive integer')
    try:
        with WindowsRetirement(parent, wait=True, create=False,
                               expected_parent_identity=expected_parent_identity) as transaction:
            return transaction.reclaim(max_remove=max_remove)
    except FileNotFoundError:
        return {'removed': [], 'preserved': [], 'errors': []}
