"""POSIX retirement transactions for export creation, cleanup, and recovery.

Normal callers may wait for the registry while holding a stage owner lock.
Recovery acquires both locks without waiting, avoiding reverse-order deadlock.
Private slot names are cooperative metadata, not public export names.
The permanent root is never removed by ordinary cleanup. This protocol does
not provide Windows crash-recovery qualification.
"""
from contextlib import ExitStack, contextmanager
import json
import os
from pathlib import Path
import secrets
import re
import stat
import time

from export_staging_directory import StageDirectoryChanged, identity, stage_directory, stage_owner_lock
from utils import file_lock, open_or_create_file
from audio_take_history import _version
from export_staging_records import (decode_creation_receipt, decode_receipt,
                                    decode_record, receipt_owner_identity)

DIRECTORY = '.aa2-export-retirement'
LOCK = 'owner.lock'
MAX_SLOTS = 128
# Normal work keeps its original bound. One fixed extra slot lets cleanup
# progress even when every ordinary slot is occupied by an outer stage.
RETIREMENT_RESERVE = '0' * 32
RETAINED_OWNER = 'owner.retained'


class _MissingRetirementLock(FileNotFoundError):
    """The initial open found no lock; later lock failures are distinct."""


class PosixRetirement:
    def __init__(self, authority=None, *, parent=None, wait=False, create=True,
                 expected_parent_identity=None, expected_registry_identity=None):
        if (authority is None) == (parent is None):
            raise ValueError('Provide either admitted stage or recovery parent')
        if type(wait) is not bool:
            raise ValueError('Registry wait flag must be Boolean')
        if type(create) is not bool:
            raise ValueError('Registry creation flag must be Boolean')
        if (expected_parent_identity is not None
                and (type(expected_parent_identity) is not tuple or len(expected_parent_identity) != 2
                     or any(type(value) is not int or value < 0 for value in expected_parent_identity))):
            raise ValueError('Invalid retirement parent identity')
        if (expected_registry_identity is not None
                and (type(expected_registry_identity) is not tuple or len(expected_registry_identity) != 2
                     or any(type(value) is not int or value < 0 for value in expected_registry_identity))):
            raise ValueError('Invalid retirement registry identity')
        if expected_registry_identity is not None and create:
            raise ValueError('Bound retirement recovery cannot create a registry')
        self.create = create
        self.expected_parent_identity = expected_parent_identity
        self.expected_registry_identity = expected_registry_identity
        self.lock_timeout = 10 if wait else 0
        self.authority = authority
        self.parent = Path(parent) if authority is None else authority.path.parent
        self.parent_fd = None
        self.stack = ExitStack()
        self.fd = None
        self.slot_fd = None
        self.slot = None
        self.entered = False

    def __enter__(self):
        if self.entered:
            raise StageDirectoryChanged('Retirement transaction cannot be reused')
        self.entered = True
        try:
            required = (os.open, os.stat, os.mkdir, os.rmdir, os.unlink, os.rename)
            if (any(function not in os.supports_dir_fd for function in required)
                    or os.scandir not in os.supports_fd
                    or not all(hasattr(os, name) for name in
                               ('O_DIRECTORY', 'O_NOFOLLOW', 'O_NONBLOCK', 'fchmod', 'fsync'))):
                raise StageDirectoryChanged('Descriptor-relative retirement is unavailable')
            if self.authority is not None:
                if self.authority.path.name == DIRECTORY:
                    raise StageDirectoryChanged('Staging and retirement namespace collision')
                self.authority.check()
            self.parent_fd = os.open(self.parent.resolve(strict=True),
                                     os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            self.stack.callback(os.close, self.parent_fd)
            parent = self.parent_fd
            if (self.expected_parent_identity is not None
                    and identity(os.fstat(parent)) != self.expected_parent_identity):
                raise StageDirectoryChanged('Retirement parent changed before registry admission')
            if (self.authority is not None and identity(os.fstat(parent))
                    != identity(os.fstat(self.authority.parent_fd))):
                raise StageDirectoryChanged('Retirement parent changed')
            if self.create:
                try:
                    os.mkdir(DIRECTORY, 0o700, dir_fd=parent)
                    os.fsync(parent)
                except FileExistsError:
                    pass
            self.fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                              dir_fd=parent)
            self.stack.callback(os.close, self.fd)
            if (self.expected_registry_identity is not None
                    and identity(os.fstat(self.fd)) != self.expected_registry_identity):
                raise StageDirectoryChanged('Retirement registry changed before admission')
            self.check()
            try:
                flags = os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK
                lock_fd = (open_or_create_file(LOCK, flags, 0o600, dir_fd=self.fd) if self.create
                           else os.open(LOCK, flags, dir_fd=self.fd))
            except FileNotFoundError as error:
                if not self.create:
                    raise _MissingRetirementLock('Retirement lock is missing') from error
                raise
            stream = self.stack.enter_context(os.fdopen(lock_fd, 'r+b'))
            self.lock_stream = stream
            info = os.fstat(stream.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or info.st_size > 4096
                    or info.st_mode & 0o077):
                raise StageDirectoryChanged('Unsafe retirement lock')
            self.stack.enter_context(file_lock(self.parent / DIRECTORY / 'owner',
                                              timeout=self.lock_timeout, lock_handle=stream))
            if identity(os.stat(LOCK, dir_fd=self.fd, follow_symlinks=False)) != identity(info):
                raise StageDirectoryChanged('Retirement lock replaced')
            self.check()
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *error):
        try:
            self.stack.close()
        finally:
            self.fd = self.slot_fd = self.parent_fd = None

    @contextmanager
    def additional_slot(self):
        """Use a separate slot view while retaining this registry's lock.

        A nested-root quarantine must not overwrite the admitted outer slot or
        reacquire the registry lock already held by this transaction.
        """
        self.check()
        child = PosixRetirement(parent=self.parent, create=False)
        try:
            child.parent_fd = os.dup(self.parent_fd)
            child.stack.callback(os.close, child.parent_fd)
            child.fd = os.dup(self.fd)
            child.stack.callback(os.close, child.fd)
            child.entered = True
            child.check()
            yield child
        finally:
            child.__exit__(None, None, None)

    def check(self):
        if self.fd is None:
            raise StageDirectoryChanged('Retirement transaction is not active')
        opened = os.fstat(self.fd)
        if identity(os.stat(self.parent, follow_symlinks=False)) != identity(os.fstat(self.parent_fd)):
            raise StageDirectoryChanged('Retirement parent path changed')
        named = os.stat(DIRECTORY, dir_fd=self.parent_fd, follow_symlinks=False)
        if (not stat.S_ISDIR(opened.st_mode) or opened.st_uid != os.geteuid()
                or opened.st_mode & 0o077 or identity(named) != identity(opened)):
            raise StageDirectoryChanged('Unsafe or replaced retirement root')

    def _check_slot(self):
        self.check()
        if self.slot_fd is None or self.slot is None:
            raise StageDirectoryChanged('No retirement slot admitted')
        named = os.stat(self.slot, dir_fd=self.fd, follow_symlinks=False)
        opened = os.fstat(self.slot_fd)
        if not stat.S_ISDIR(named.st_mode) or identity(named) != identity(opened):
            raise StageDirectoryChanged('Retirement slot replaced')

    def _write_receipt(self, name, value):
        self._check_slot()
        if name not in ('reserved.json', 'verified.json', 'allocation.json', 'creation.json', 'owner.json'):
            raise StageDirectoryChanged('Invalid receipt publication name')
        try:
            os.stat(name, dir_fd=self.slot_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise StageDirectoryChanged('Retirement receipt already exists')
        raw = json.dumps(value, sort_keys=True, separators=(',', ':')).encode('utf-8')
        if len(raw) > 8192:
            raise StageDirectoryChanged('Retirement receipt exceeds limit')
        pending = name + '.pending'
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                             0o600, dir_fd=self.slot_fd)
        with os.fdopen(descriptor, 'wb') as stream:
            if stream.write(raw) != len(raw):
                raise OSError('Short retirement receipt write')
            stream.flush()
            os.fsync(stream.fileno())
        self._check_slot()
        # The registry lock owns these private names. Recovery ignores pending
        # files; only a completely written receipt is published atomically.
        os.replace(pending, name, src_dir_fd=self.slot_fd, dst_dir_fd=self.slot_fd)
        os.fsync(self.slot_fd)
        self._check_slot()

    def read_receipt(self, name):
        """Read bounded immutable metadata; a valid receipt alone permits no deletion."""
        if name not in ('reserved.json', 'verified.json', 'allocation.json', 'creation.json', 'owner.json') or self.slot_fd is None:
            raise StageDirectoryChanged('Invalid retirement receipt request')
        self.check()
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=self.slot_fd)
        with os.fdopen(descriptor, 'rb') as stream:
            before = os.fstat(stream.fileno())
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or before.st_uid != os.geteuid() or before.st_mode & 0o077
                    or not 0 < before.st_size <= 8192):
                raise StageDirectoryChanged('Unsafe retirement receipt')
            raw = stream.read(8193)
            after = os.stat(name, dir_fd=self.slot_fd, follow_symlinks=False)
            if (len(raw) > 8192 or _version(before) != _version(after)
                    or _version(before) != _version(os.fstat(stream.fileno()))):
                raise StageDirectoryChanged('Retirement receipt changed during read')
        value = (decode_creation_receipt(raw, complete=name != 'allocation.json', owner=name == 'owner.json')
                 if name in ('allocation.json', 'creation.json', 'owner.json') else decode_receipt(raw))
        self.check()
        return value

    def slot_names(self):
        """Bound discovery to the permanent private namespace, including junk."""
        self.check()
        names = []
        os.lseek(self.fd, 0, os.SEEK_SET)
        with os.scandir(self.fd) as entries:
            for entry in entries:
                if entry.name == LOCK:
                    continue
                names.append(entry.name)
                if len(names) > MAX_SLOTS + 1:
                    raise StageDirectoryChanged('Retirement namespace exceeds capacity')
        if len(names) > MAX_SLOTS and RETIREMENT_RESERVE not in names:
            raise StageDirectoryChanged('Retirement namespace exceeds capacity')
        if RETIREMENT_RESERVE in names:
            names.remove(RETIREMENT_RESERVE)
            names.insert(0, RETIREMENT_RESERVE)
        return names

    def open_slot(self, name):
        """Admit one private slot without interpreting or deleting its contents."""
        if self.slot is not None or type(name) is not str or re.fullmatch('[0-9a-f]{32}', name) is None:
            raise StageDirectoryChanged('Invalid or already opened retirement slot')
        self.check()
        descriptor = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                             dir_fd=self.fd)
        self.stack.callback(os.close, descriptor)
        info = os.fstat(descriptor)
        named = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
        if (info.st_uid != os.geteuid() or info.st_mode & 0o077
                or identity(info) != identity(named)):
            raise StageDirectoryChanged('Unsafe retirement slot')
        self.slot, self.slot_fd = name, descriptor

    def inspect_slot(self, name):
        """Classify a saved slot without granting deletion authority."""
        self.open_slot(name)
        descriptor = self.slot_fd
        reserved = self.read_receipt('reserved.json')
        if reserved['parent_identity'] != list(identity(os.fstat(self.parent_fd))):
            raise StageDirectoryChanged('Retirement receipt belongs to another parent')
        try:
            verified = self.read_receipt('verified.json')
        except FileNotFoundError:
            verified = None
        if verified is not None and verified != reserved:
            raise StageDirectoryChanged('Retirement receipts disagree')
        try:
            stage = os.stat('stage', dir_fd=descriptor, follow_symlinks=False)
        except FileNotFoundError:
            state = 'removed' if verified is not None else 'reserved_unmoved'
        else:
            if not stat.S_ISDIR(stage.st_mode) or list(identity(stage)) != reserved['stage_identity']:
                state = 'mismatch_preserve'
            else:
                state = 'verified' if verified is not None else 'moved_unverified'
        self.check()
        return {'state': state, 'receipt': reserved}

    def discard_empty_slot(self, name):
        """Retire metadata only when no staged object exists; never touch public data.

        The permanent private namespace and registry lock own these fixed
        metadata names. Unknown nodes or malformed published receipts preserve
        the whole slot. Partial pending files are never deletion authority.
        """
        if self.slot is None:
            self.open_slot(name)
        elif self.slot != name:
            raise StageDirectoryChanged('Different retirement slot already admitted')
        self._check_slot()
        try:
            os.stat('stage', dir_fd=self.slot_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise StageDirectoryChanged('Cannot discard a slot containing a staged object')
        allowed = ('verified.json.pending', 'reserved.json.pending',
                   'owner.json.pending', 'creation.json.pending', 'allocation.json.pending',
                   'verified.json', 'reserved.json', 'owner.json', 'creation.json', 'allocation.json')
        entries = {}
        os.lseek(self.slot_fd, 0, os.SEEK_SET)
        with os.scandir(self.slot_fd) as names:
            for entry in names:
                if entry.name not in allowed or len(entries) >= len(allowed):
                    raise StageDirectoryChanged('Unknown retirement contents preserved')
                info = os.stat(entry.name, dir_fd=self.slot_fd, follow_symlinks=False)
                if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                        or info.st_uid != os.geteuid() or info.st_mode & 0o077
                        or info.st_size > 8192):
                    raise StageDirectoryChanged('Unsafe retirement metadata preserved')
                entries[entry.name] = info
        creation_names = {'allocation.json', 'creation.json', 'owner.json'}
        retirement_names = {'reserved.json', 'verified.json'}
        families = {name.removesuffix('.pending') for name in entries}
        if families & creation_names and families & retirement_names:
            raise StageDirectoryChanged('Mixed slot lifecycles preserved')
        _, created = self.read_creation_records()
        self.read_creation_owner(created)
        reserved = self.read_receipt('reserved.json') if 'reserved.json' in entries else None
        verified = self.read_receipt('verified.json') if 'verified.json' in entries else None
        if reserved is not None and reserved['parent_identity'] != list(identity(os.fstat(self.parent_fd))):
            raise StageDirectoryChanged('Retirement parent mismatch')
        if verified is not None and verified != reserved:
            raise StageDirectoryChanged('Retirement receipts disagree')
        self._check_slot()
        # Validate everything before the first mutation. Canonical verified is
        # removed before reserved so interruption leaves a readable reservation.
        for member in allowed:
            if member not in entries:
                continue
            current = os.stat(member, dir_fd=self.slot_fd, follow_symlinks=False)
            if _version(current) != _version(entries[member]):
                raise StageDirectoryChanged('Retirement metadata changed')
            os.unlink(member, dir_fd=self.slot_fd)
            # Persist each dependent record's removal before deleting its
            # prerequisite; one final sync permits invalid surviving subsets.
            os.fsync(self.slot_fd)
        os.fsync(self.slot_fd)
        self._check_slot()
        os.rmdir(self.slot, dir_fd=self.fd)
        os.fsync(self.fd)

    def validate_stage_marker(self, authority, receipt):
        from export_staging import decode_staging_marker, _matches_parent
        raw = authority.read_marker_bytes(4096)
        admitted = decode_staging_marker(raw)
        if (admitted is None or raw != receipt['marker'].encode('utf-8')
                or not _matches_parent(admitted, self.parent)
                or not receipt['original_name'].startswith(admitted['prefix'])):
            raise StageDirectoryChanged('Retirement marker authority changed')

    def recover_slot(self, name):
        """Recover one slot using a nonblocking stage-owner admission.

        Registry then stage locking is safe here because both acquisitions are
        nonblocking. A live owner is preserved; no waiting on it under registry.
        """
        try:
            inspected = self.inspect_slot(name)
        except FileNotFoundError:
            # A missing reservation is only disposable if no stage or unknown
            # content exists. discard_empty_slot rechecks those boundaries.
            return self.recover_creation_slot(name)
        state, receipt = inspected['state'], inspected['receipt']
        self.check_slot_members()
        if state in ('removed', 'reserved_unmoved'):
            if self.has_retained_owner():
                if state != 'removed':
                    raise StageDirectoryChanged('Unverified retained owner preserved')
                with self.lock_retained_owner(receipt) as stream:
                    self.unlink_retained_owner(receipt, stream)
            self.discard_empty_slot(name)
            return 'metadata_removed'
        if state == 'mismatch_preserve':
            raise StageDirectoryChanged('Mismatched retirement stage preserved')
        path = self.parent / DIRECTORY / self.slot / 'stage'
        if self.has_retained_owner():
            if state != 'verified':
                raise StageDirectoryChanged('Unverified retained owner preserved')
            with self.lock_retained_owner(receipt) as stream:
                self.finish_retained_stage(receipt, stream)
            self.discard_empty_slot(name)
            return 'stage_removed'
        with stage_directory(path, tuple(receipt['stage_identity'])) as authority:
            owner = stage_owner_lock(authority)
            owner_stream = owner.__enter__()
            released = False
            def release_owner():
                nonlocal released
                if not released:
                    released = True
                    owner.__exit__(None, None, None)
            try:
                expected_owner = receipt_owner_identity(receipt, self.parent)
                if expected_owner != list(identity(os.fstat(owner_stream.fileno()))):
                    raise StageDirectoryChanged('Retirement owner identity changed')
                if authority.has_recovery_guard() or authority.has_rollback_backup():
                    raise StageDirectoryChanged('Retirement rollback or recovery payload preserved')
                if state == 'moved_unverified':
                    self.validate_stage_marker(authority, receipt)
                    # Interrupted publication may leave private scratch bytes.
                    # Their contents never establish verification authority.
                    try:
                        pending = os.stat('verified.json.pending', dir_fd=self.slot_fd,
                                          follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        if (not stat.S_ISREG(pending.st_mode) or pending.st_nlink != 1
                                or pending.st_uid != os.geteuid() or pending.st_mode & 0o077
                                or pending.st_size > 8192):
                            raise StageDirectoryChanged('Unsafe pending verification preserved')
                        os.unlink('verified.json.pending', dir_fd=self.slot_fd)
                        os.fsync(self.slot_fd)
                    self._write_receipt('verified.json', receipt)
                self.cleanup_verified_stage(release_owner, owner_stream)
            finally:
                release_owner()
        return 'stage_removed'

    def cleanup_verified_stage(self, release_owner, owner_stream):
        """Remove a verified private stage while caller retains its owner lock.

        The release callback is invoked only by the existing metadata-finalize
        sequence. Recovery callers must acquire that same stage owner first.
        """
        from export_staging import _remove_contents, OWNER_LOCK
        self.check_slot_members()
        reserved = self.read_receipt('reserved.json')
        if self.read_receipt('verified.json') != reserved:
            raise StageDirectoryChanged('Retirement receipts disagree')
        if reserved['parent_identity'] != list(identity(os.fstat(self.parent_fd))):
            raise StageDirectoryChanged('Retirement parent mismatch')
        path = self.parent / DIRECTORY / self.slot / 'stage'
        expected = tuple(reserved['stage_identity'])
        with stage_directory(path, expected) as authority:
            try:
                marker = authority.read_marker_bytes(4096)
            except FileNotFoundError:
                # A prior verified cleanup may have retired its marker before
                # failing final removal. Only the owner file may remain then.
                os.lseek(authority.fd, 0, os.SEEK_SET)
                with os.scandir(authority.fd) as entries:
                    for entry in entries:
                        if entry.name != OWNER_LOCK + '.lock':
                            raise StageDirectoryChanged('Unmarked retirement payload preserved')
            else:
                self.validate_stage_marker(authority, reserved)
            self._check_slot()
            authority.check_owner_stream(owner_stream)
            expected_owner = receipt_owner_identity(reserved, self.parent)
            if expected_owner != list(identity(os.fstat(owner_stream.fileno()))):
                raise StageDirectoryChanged('Retirement owner identity changed')
            if self.has_retained_owner():
                raise StageDirectoryChanged('Ambiguous retirement owner locations')
            owner_info = os.fstat(owner_stream.fileno())
            if owner_info.st_uid != os.geteuid():
                raise StageDirectoryChanged('Foreign staging owner preserved')
            # Old file_lock creation respected the process umask, commonly
            # yielding 0644. Tighten this admitted internal inode while locked
            # before moving it into the private retention namespace.
            if stat.S_IMODE(owner_info.st_mode) != 0o600:
                os.fchmod(owner_stream.fileno(), 0o600)
                os.fsync(owner_stream.fileno())
            _remove_contents(path, authority=authority, retirement=self)
            authority.check_owner_stream(owner_stream)
            os.fsync(authority.fd)
            os.rename(OWNER_LOCK + '.lock', RETAINED_OWNER,
                      src_dir_fd=authority.fd, dst_dir_fd=self.slot_fd)
            # Persist the destination before retiring the source name.
            os.fsync(self.slot_fd)
            os.fsync(authority.fd)
            self.finish_retained_stage(reserved, owner_stream)
            release_owner()
        self.discard_empty_slot(self.slot)

    def has_retained_owner(self):
        self._check_slot()
        try:
            os.stat(RETAINED_OWNER, dir_fd=self.slot_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return True

    def check_slot_members(self, *, creation=False):
        self._check_slot()
        allowed = {'stage', RETAINED_OWNER}
        records = (('allocation.json', 'creation.json', 'owner.json') if creation else
                   ('reserved.json', 'verified.json'))
        for name in records:
            allowed.update((name, name + '.pending'))
        os.lseek(self.slot_fd, 0, os.SEEK_SET)
        with os.scandir(self.slot_fd) as entries:
            for entry in entries:
                if entry.name not in allowed:
                    raise StageDirectoryChanged('Unknown retirement contents preserved')
                if entry.name != 'stage':
                    info = os.stat(entry.name, dir_fd=self.slot_fd, follow_symlinks=False)
                    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                            or info.st_uid != os.geteuid() or info.st_mode & 0o077
                            or info.st_size > 8192):
                        raise StageDirectoryChanged('Unsafe slot metadata preserved')

    def check_retained_owner(self, receipt, stream, *, creation=False):
        self._check_slot()
        expected = receipt_owner_identity(receipt, self.parent)
        self.check_slot_members(creation=creation)
        if creation:
            _, created = self.read_creation_records()
            owned = self.read_creation_owner(created)
            if owned is None or self.creation_cleanup_receipt(owned) != receipt:
                raise StageDirectoryChanged('Partial initialization receipt mismatch')
        opened = os.fstat(stream.fileno())
        named = os.stat(RETAINED_OWNER, dir_fd=self.slot_fd, follow_symlinks=False)
        if (expected is None or expected != list(identity(opened))
                or not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1
                or opened.st_uid != os.geteuid() or opened.st_mode & 0o077
                or opened.st_size > 4096 or identity(named) != identity(opened)
                or not stat.S_ISREG(named.st_mode) or named.st_nlink != 1):
            raise StageDirectoryChanged('Unsafe or replaced retained owner')

    @contextmanager
    def lock_retained_owner(self, receipt, *, creation=False):
        self._check_slot()
        descriptor = os.open(RETAINED_OWNER, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK,
                             dir_fd=self.slot_fd)
        with os.fdopen(descriptor, 'r+b') as stream:
            self.check_retained_owner(receipt, stream, creation=creation)
            with file_lock(self.parent / DIRECTORY / self.slot / RETAINED_OWNER,
                           timeout=0, lock_handle=stream):
                self.check_retained_owner(receipt, stream, creation=creation)
                yield stream

    def unlink_retained_owner(self, receipt, stream, *, creation=False):
        self.check_retained_owner(receipt, stream, creation=creation)
        # Never dispose ownership while a stage, including an alias, remains.
        try:
            os.stat('stage', dir_fd=self.slot_fd, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise StageDirectoryChanged('Retained stage still exists')
        os.unlink(RETAINED_OWNER, dir_fd=self.slot_fd)
        os.fsync(self.slot_fd)

    def finish_retained_stage(self, receipt, stream, *, creation=False):
        from export_staging import MARKER
        self.check_retained_owner(receipt, stream, creation=creation)
        path = self.parent / DIRECTORY / self.slot / 'stage'
        with stage_directory(path, tuple(receipt['stage_identity'])) as authority:
            # Payload removal must finish before moving the owner. Both owner
            # locations, recovery guards and unexpected payload are ambiguous.
            os.lseek(authority.fd, 0, os.SEEK_SET)
            with os.scandir(authority.fd) as entries:
                for entry in entries:
                    if entry.name != MARKER:
                        raise StageDirectoryChanged('Unexpected retained-stage contents')
            try:
                authority.read_marker_bytes(4096)
            except FileNotFoundError:
                pass
            else:
                self.validate_stage_marker(authority, receipt)
                authority.unlink(MARKER)
            self.check_retained_owner(receipt, stream, creation=creation)
            authority.remove_empty_directory()
            os.fsync(self.slot_fd)
        self.unlink_retained_owner(receipt, stream, creation=creation)

    def allocate_slot(self, *, retirement_reserve=False):
        if self.slot is not None:
            raise StageDirectoryChanged('Slot already allocated')
        # Count every unknown entry too; malformed slots consume capacity.
        count = 0
        reserve_used = False
        os.lseek(self.fd, 0, os.SEEK_SET)
        with os.scandir(self.fd) as entries:
            for entry in entries:
                if entry.name == LOCK:
                    continue
                if entry.name == RETIREMENT_RESERVE:
                    reserve_used = True
                    continue
                count += 1
                if count > MAX_SLOTS:
                    raise StageDirectoryChanged('Export retirement capacity exhausted')
        self.check()
        if count >= MAX_SLOTS:
            if not retirement_reserve or reserve_used:
                raise StageDirectoryChanged('Export retirement capacity exhausted')
            slot = RETIREMENT_RESERVE
        else:
            slot = secrets.token_hex(16)
            while slot == RETIREMENT_RESERVE:
                slot = secrets.token_hex(16)
        os.mkdir(slot, 0o700, dir_fd=self.fd)
        self.slot = slot
        os.fsync(self.fd)
        self.slot_fd = os.open(self.slot, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                               dir_fd=self.fd)
        self.stack.callback(os.close, self.slot_fd)

    def allocate_creation(self, kind, token):
        """Reserve privately before creating the empty initialization directory.

        The reservation precedes mkdir so a crash before the stage-identity
        receipt has a bounded recovery path.
        """
        from export_staging import PREFIXES
        if (self.authority is not None or type(kind) is not str or kind not in PREFIXES
                or type(token) is not str or re.fullmatch('[0-9a-f]{32}', token) is None):
            raise StageDirectoryChanged('Invalid private staging allocation')
        self.check()
        self.allocate_slot(retirement_reserve=kind == 'nested_registry')
        record = {'schema': 1, 'kind': kind, 'token': token,
                  'parent_identity': list(identity(os.fstat(self.parent_fd))),
                  'slot_identity': list(identity(os.fstat(self.slot_fd)))}
        self._write_receipt('allocation.json', record)
        self._check_slot()
        os.mkdir('stage', 0o700, dir_fd=self.slot_fd)
        os.fsync(self.slot_fd)
        info = os.stat('stage', dir_fd=self.slot_fd, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise StageDirectoryChanged('Unsafe newly allocated stage')
        created = {**record, 'stage_identity': list(identity(info))}
        self._write_receipt('creation.json', created)
        self._check_slot()
        return self.parent / DIRECTORY / self.slot / 'stage', tuple(created['stage_identity'])

    def initialize_creation(self, kind):
        """Create owner and marker while the registry excludes recovery.

        The returned owner context stays entered until its caller finalizes the
        stage.
        """
        from export_staging import PREFIXES, MARKER, _write_marker
        owner = None
        entered = False
        try:
            path, expected = self.allocate_creation(kind, secrets.token_hex(16))
            with stage_directory(path, expected) as authority:
                owner = stage_owner_lock(authority, create=True)
                stream = owner.__enter__()
                entered = True
                _, created = self.read_creation_records()
                owned = {**created, 'owner_identity': list(identity(os.fstat(stream.fileno())))}
                # Make the inode and its name durable before publishing a
                # receipt that recovery will require them to satisfy.
                os.fsync(stream.fileno())
                os.fsync(authority.fd)
                self._write_receipt('owner.json', owned)
                marker = {'schema': 1, 'kind': kind, 'prefix': PREFIXES[kind],
                          'parent': str(self.parent.resolve()), 'token': created['token'],
                          'owner_identity': owned['owner_identity'],
                          'pid': os.getpid(), 'created_ns': time.time_ns()}
                _write_marker(path / MARKER, marker, authority=authority)
                authority.check_owner_stream(stream)
            return path, expected, marker, owner, stream
        except BaseException as original_error:
            try:
                if entered:
                    owner.__exit__(None, None, None)
                if self.slot_fd is not None:
                    self.recover_creation_slot(self.slot)
            except BaseException as cleanup_error:
                raise original_error from cleanup_error
            raise

    def validate_initialized_creation(self, authority, stream):
        """Read the bound creation authority without mutating or taking ownership."""
        from export_staging import decode_staging_marker
        self.check_slot_members(creation=True)
        _, created = self.read_creation_records()
        owned = self.read_creation_owner(created)
        if owned is None or authority.expected != tuple(owned['stage_identity']):
            raise StageDirectoryChanged('Initialized stage identity mismatch')
        authority.check_owner_stream(stream)
        owner_info = os.fstat(stream.fileno())
        if (list(identity(owner_info)) != owned['owner_identity']
                or owner_info.st_uid != os.geteuid() or owner_info.st_mode & 0o077):
            raise StageDirectoryChanged('Initialized owner identity mismatch')
        raw = authority.read_marker_bytes(4096)
        marker = decode_staging_marker(raw)
        receipt = self.creation_cleanup_receipt(owned)
        expected_marker = json.loads(receipt['marker'])
        if marker is None or any(marker.get(key) != value for key, value in expected_marker.items()):
            raise StageDirectoryChanged('Initialized marker authority mismatch')
        if self.has_retained_owner():
            raise StageDirectoryChanged('Ambiguous initialized owner locations')
        return raw, receipt

    def cleanup_initialized_creation(self, authority, stream, *, recovery=False,
                                     nested_owner_stream=None):
        """Retire a valid created stage using its existing owner admission."""
        from export_staging import MARKER, OWNER_LOCK, _remove_contents
        raw, receipt = self.validate_initialized_creation(authority, stream)
        if authority.has_recovery_guard() or (recovery and authority.has_rollback_backup()):
            raise StageDirectoryChanged('Initialized recovery payload preserved')
        _remove_contents(authority.path, authority=authority, retirement=self,
                         nested_owner_stream=nested_owner_stream)
        if authority.read_marker_bytes(4096) != raw:
            raise StageDirectoryChanged('Initialized marker changed')
        authority.unlink(MARKER)
        authority.check_owner_stream(stream)
        os.rename(OWNER_LOCK + '.lock', RETAINED_OWNER,
                  src_dir_fd=authority.fd, dst_dir_fd=self.slot_fd)
        os.fsync(self.slot_fd)
        os.fsync(authority.fd)
        self.finish_retained_stage(receipt, stream, creation=True)

    def read_creation_records(self):
        self._check_slot()
        try:
            allocated = self.read_receipt('allocation.json')
        except FileNotFoundError:
            allocated = None
        try:
            created = self.read_receipt('creation.json')
        except FileNotFoundError:
            created = None
        if allocated is not None:
            if (allocated['parent_identity'] != list(identity(os.fstat(self.parent_fd)))
                    or allocated['slot_identity'] != list(identity(os.fstat(self.slot_fd)))):
                raise StageDirectoryChanged('Creation reservation identity mismatch')
        if created is not None:
            if ({key: value for key, value in created.items() if key != 'stage_identity'}
                    != allocated):
                raise StageDirectoryChanged('Creation receipts disagree')
        return allocated, created

    def read_creation_owner(self, created):
        try:
            owned = self.read_receipt('owner.json')
        except FileNotFoundError:
            return None
        if ({key: value for key, value in owned.items() if key != 'owner_identity'}
                != created):
            raise StageDirectoryChanged('Initialization owner receipt disagrees')
        return owned

    def creation_cleanup_receipt(self, owned):
        from export_staging import PREFIXES
        marker = {'schema': 1, 'kind': owned['kind'], 'prefix': PREFIXES[owned['kind']],
                  'parent': str(self.parent.resolve()), 'token': owned['token'],
                  'owner_identity': owned['owner_identity']}
        return {'schema': 1, 'original_name': marker['prefix'] + marker['token'],
                'parent_identity': owned['parent_identity'],
                'stage_identity': owned['stage_identity'],
                'owner_identity': owned['owner_identity'],
                'marker': json.dumps(marker, sort_keys=True, separators=(',', ':'))}

    def cleanup_partial_creation(self, authority, created, owned):
        """Only initialization metadata may be retired without a valid marker."""
        from export_staging import MARKER, OWNER_LOCK, decode_staging_marker
        names = set()
        os.lseek(authority.fd, 0, os.SEEK_SET)
        with os.scandir(authority.fd) as entries:
            for entry in entries:
                if entry.name not in (MARKER, OWNER_LOCK + '.lock'):
                    raise StageDirectoryChanged('Populated initialization stage preserved')
                names.add(entry.name)
        if not names:
            if owned is not None:
                raise StageDirectoryChanged('Initialization owner is missing')
            authority.remove_empty_directory()
            os.fsync(self.slot_fd)
            return
        if created is None or OWNER_LOCK + '.lock' not in names:
            raise StageDirectoryChanged('Initialization owner is missing')
        raw = authority.read_marker_bytes(4096) if MARKER in names else None
        if raw is not None and decode_staging_marker(raw) is not None:
            # Fully initialized stages require the adoption path, not the
            # narrowly scoped failed-initialization cleanup below.
            raise StageDirectoryChanged('Initialized stage awaits owned adoption')
        with stage_owner_lock(authority) as stream:
            current_names = set()
            os.lseek(authority.fd, 0, os.SEEK_SET)
            with os.scandir(authority.fd) as entries:
                for entry in entries:
                    if entry.name not in (MARKER, OWNER_LOCK + '.lock'):
                        raise StageDirectoryChanged('Initialization contents changed')
                    current_names.add(entry.name)
            current_raw = authority.read_marker_bytes(4096) if MARKER in current_names else None
            if current_names != names or current_raw != raw:
                raise StageDirectoryChanged('Initialization state changed before owner admission')
            info = os.fstat(stream.fileno())
            if info.st_uid != os.geteuid() or info.st_mode & 0o077:
                raise StageDirectoryChanged('Unsafe initialization owner')
            if owned is None:
                # POSIX initialization creates an empty lock inode; kernel
                # flock never writes content. Unexpected bytes are not ours.
                if info.st_size != 0:
                    raise StageDirectoryChanged('Unbound owner content preserved')
                owned = {**created, 'owner_identity': list(identity(info))}
                try:
                    os.unlink('owner.json.pending', dir_fd=self.slot_fd)
                except FileNotFoundError:
                    pass
                else:
                    os.fsync(self.slot_fd)
                os.fsync(stream.fileno())
                os.fsync(authority.fd)
                self._write_receipt('owner.json', owned)
            if owned['owner_identity'] != list(identity(info)):
                raise StageDirectoryChanged('Initialization owner replaced')
            if raw is not None:
                if authority.read_marker_bytes(4096) != raw:
                    raise StageDirectoryChanged('Partial initialization marker changed')
                authority.unlink(MARKER)
            authority.check_owner_stream(stream)
            os.rename(OWNER_LOCK + '.lock', RETAINED_OWNER,
                      src_dir_fd=authority.fd, dst_dir_fd=self.slot_fd)
            os.fsync(self.slot_fd)
            os.fsync(authority.fd)
            self.finish_retained_stage(self.creation_cleanup_receipt(owned), stream, creation=True)

    def recover_creation_slot(self, name):
        """Recover only empty pre-owner initialization; populated states preserve."""
        if self.slot is None:
            self.open_slot(name)
        elif self.slot != name:
            raise StageDirectoryChanged('Different creation slot already admitted')
        allocated, created = self.read_creation_records()
        owned = self.read_creation_owner(created)
        if allocated is None:
            self.discard_empty_slot(name)
            return 'metadata_removed'
        allowed = {'allocation.json', 'creation.json', 'allocation.json.pending',
                   'creation.json.pending', 'owner.json', 'owner.json.pending',
                   RETAINED_OWNER, 'stage'}
        os.lseek(self.slot_fd, 0, os.SEEK_SET)
        with os.scandir(self.slot_fd) as entries:
            for entry in entries:
                if entry.name not in allowed:
                    raise StageDirectoryChanged('Unexpected initialization slot contents')
                if entry.name != 'stage':
                    metadata = os.stat(entry.name, dir_fd=self.slot_fd, follow_symlinks=False)
                    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1
                            or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077
                            or metadata.st_size > 8192):
                        raise StageDirectoryChanged('Unsafe initialization metadata')
        if self.has_retained_owner():
            if owned is None:
                raise StageDirectoryChanged('Retained initialization owner has no binding')
            receipt = self.creation_cleanup_receipt(owned)
            with self.lock_retained_owner(receipt, creation=True) as stream:
                try:
                    os.stat('stage', dir_fd=self.slot_fd, follow_symlinks=False)
                except FileNotFoundError:
                    self.unlink_retained_owner(receipt, stream, creation=True)
                else:
                    self.finish_retained_stage(receipt, stream, creation=True)
            self.discard_empty_slot(name)
            return 'stage_removed'
        try:
            info = os.stat('stage', dir_fd=self.slot_fd, follow_symlinks=False)
        except FileNotFoundError:
            self.discard_empty_slot(name)
            return 'metadata_removed'
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or info.st_mode & 0o077
                or (created is not None and list(identity(info)) != created['stage_identity'])):
            raise StageDirectoryChanged('Initialization stage identity mismatch')
        path = self.parent / DIRECTORY / name / 'stage'
        with stage_directory(path, identity(info)) as authority:
            from export_staging import decode_staging_marker
            try:
                raw = authority.read_marker_bytes(4096)
            except FileNotFoundError:
                raw = None
            if raw is not None and decode_staging_marker(raw) is not None:
                with stage_owner_lock(authority) as stream:
                    self.cleanup_initialized_creation(authority, stream, recovery=True)
            else:
                self.cleanup_partial_creation(authority, created, owned)
        self.discard_empty_slot(name)
        return 'stage_removed'

    def move(self, owner_stream):
        """Reserve durably, then move; mismatches remain in a charged slot."""
        if self.authority is None or self.slot is not None:
            raise StageDirectoryChanged('Retirement move requires an unused admitted stage')
        self.check()
        self.authority.check()
        marker = self.authority.read_marker_bytes(4096)
        from export_staging import decode_staging_marker, _matches_parent, owner_identity_matches
        admitted = decode_staging_marker(marker)
        try:
            decoded = json.loads(marker)
            marker_text = marker.decode('utf-8')
        except (ValueError, RecursionError) as error:
            raise StageDirectoryChanged('Invalid retirement staging marker') from error
        if (admitted is None or decoded != admitted
                or not _matches_parent(admitted, self.parent)
                or not self.authority.path.name.startswith(admitted['prefix'])):
            raise StageDirectoryChanged('Retirement marker does not match admitted stage')
        self.authority.check_owner_stream(owner_stream)
        if not owner_identity_matches(admitted, owner_stream):
            raise StageDirectoryChanged('Retirement owner identity changed')
        receipt = {'schema': 1, 'original_name': self.authority.path.name,
                   'owner_identity': list(identity(os.fstat(owner_stream.fileno()))),
                   'parent_identity': list(identity(os.fstat(self.authority.parent_fd))),
                   'stage_identity': list(self.authority.expected), 'marker': marker_text}
        decode_receipt(json.dumps(receipt, sort_keys=True, separators=(',', ':')).encode('utf-8'))
        self.allocate_slot()
        self._write_receipt('reserved.json', receipt)
        self._check_slot()
        self.authority.check()
        os.rename(self.authority.path.name, 'stage', src_dir_fd=self.authority.parent_fd,
                  dst_dir_fd=self.slot_fd)
        # Make the recovery destination durable before retiring the public name.
        os.fsync(self.slot_fd)
        os.fsync(self.authority.parent_fd)
        moved = os.stat('stage', dir_fd=self.slot_fd, follow_symlinks=False)
        if not stat.S_ISDIR(moved.st_mode) or identity(moved) != self.authority.expected:
            location = self.authority.path.parent / DIRECTORY / self.slot / 'stage'
            raise StageDirectoryChanged(f'Retirement mismatch preserved at {location}')
        self._check_slot()
        moved_path = self.parent / DIRECTORY / self.slot / 'stage'
        with stage_directory(moved_path, self.authority.expected) as moved_authority:
            if moved_authority.read_marker_bytes(4096) != marker:
                raise StageDirectoryChanged('Moved staging marker changed; slot preserved')
        # Only this second durable record means the move identity was verified.
        self._write_receipt('verified.json', receipt)
        return self.slot_fd, 'stage'


def reclaim_retired_slots(parent, *, max_remove, expected_parent_identity=None):
    """Bounded private-slot discovery; failures preserve the affected slot."""
    result = {'removed': [], 'preserved': [], 'errors': []}
    if type(max_remove) is not int or max_remove <= 0:
        raise ValueError('Retirement removal budget must be a positive integer')
    if (expected_parent_identity is not None
            and (type(expected_parent_identity) is not tuple or len(expected_parent_identity) != 2
                 or any(type(value) is not int or value < 0 for value in expected_parent_identity))):
        raise ValueError('Invalid retirement parent identity')
    parent = Path(parent)
    # Hold the admitted parent open through every registry re-open so its
    # identity cannot be recycled between the initial observation and use.
    from export_staging import _pin_recovery_parent
    try:
        with _pin_recovery_parent(os.path.realpath(parent), with_fd=True) as (pinned_identity, pinned_fd):
            if expected_parent_identity is not None and pinned_identity != expected_parent_identity:
                result['errors'].append(f'{parent}: retirement parent changed before registry admission')
                return result
            with ExitStack() as pinned_registry:
                try:
                    if pinned_fd is None:
                        (parent / DIRECTORY).lstat()
                        registry_identity = None
                    else:
                        if os.open not in os.supports_dir_fd:
                            raise StageDirectoryChanged('Descriptor-relative retirement is unavailable')
                        registry_fd = os.open(DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                              dir_fd=pinned_fd)
                        pinned_registry.callback(os.close, registry_fd)
                        registry_identity = identity(os.fstat(registry_fd))
                except FileNotFoundError:
                    return result
                return _reclaim_retired_slots_bound(parent, max_remove=max_remove,
                                                    expected_parent_identity=pinned_identity,
                                                    expected_registry_identity=registry_identity,
                                                    pinned_parent_fd=pinned_fd,
                                                    pinned_registry_fd=registry_fd if pinned_fd is not None else None)
    except FileNotFoundError:
        return result
    except OSError as error:
        result['errors'].append(f'{parent}: {error}')
        return result


def _reclaim_retired_slots_bound(parent, *, max_remove, expected_parent_identity,
                                 expected_registry_identity, pinned_parent_fd, pinned_registry_fd):
    result = {'removed': [], 'preserved': [], 'errors': []}
    try:
        with PosixRetirement(parent=parent, create=False,
                             expected_parent_identity=expected_parent_identity,
                             expected_registry_identity=expected_registry_identity) as registry:
            names = registry.slot_names()
    except TimeoutError:
        result['preserved'].append(str(parent / DIRECTORY))
        return result
    except _MissingRetirementLock as error:
        # A crash between creating an empty registry and its first lock leaves
        # no stage to recover. Do not create a new lock: a nonempty registry
        # could have a live holder of a removed lock. An empty check consumes
        # only one entry and remains bound to both pinned directory identities.
        try:
            if pinned_parent_fd is None or pinned_registry_fd is None:
                raise error
            with os.scandir(pinned_registry_fd) as entries:
                if next(entries, None) is not None:
                    raise error
            if (identity(os.stat(parent, follow_symlinks=False)) != expected_parent_identity
                    or identity(os.stat(DIRECTORY, dir_fd=pinned_parent_fd, follow_symlinks=False))
                    != expected_registry_identity):
                raise StageDirectoryChanged('Retirement registry changed during empty check')
        except OSError as check_error:
            result['errors'].append(f'{parent / DIRECTORY}: {check_error}')
        return result
    except OSError as error:
        result['errors'].append(f'{parent / DIRECTORY}: {error}')
        return result
    for name in names:
        path = parent / DIRECTORY / name
        try:
            with PosixRetirement(parent=parent, create=False,
                                 expected_parent_identity=expected_parent_identity,
                                 expected_registry_identity=expected_registry_identity) as registry:
                # Discovery releases the registry between slots. Another
                # admitted reaper may already have completed this one; renew
                # existence under the reacquired gate, without treating a
                # missing receipt inside an existing slot as successful cleanup.
                try:
                    os.stat(name, dir_fd=registry.fd, follow_symlinks=False)
                except FileNotFoundError:
                    continue
                outcome = registry.recover_slot(name)
        except TimeoutError:
            result['preserved'].append(str(path / 'stage'))
        except StageDirectoryChanged as error:
            result['preserved'].append(str(path / 'stage'))
            result['errors'].append(f'{path}: {error}')
        except OSError as error:
            result['preserved'].append(str(path / 'stage'))
            result['errors'].append(f'{path}: {error}')
        else:
            result['removed'].append(str(path / 'stage' if outcome == 'stage_removed' else path))
            if len(result['removed']) >= max_remove:
                break
    return result
