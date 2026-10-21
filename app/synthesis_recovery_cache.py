"""Bounded recovery index IO; payloads remain subordinate to Project admission.

Only an admitted caller may initialize/write. Reader admission never calls that
owner while holding the cache lock. Every file operation uses a pinned directory
and a no-follow regular file descriptor; the sibling kernel lock is persistent.
"""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import sys
import threading
import uuid

from utils import file_lock

CACHE_DIRECTORY = '.synthesis-recovery-v1'
INDEX_NAME = 'index.json'
PENDING_NAME = 'index.pending'
MAX_INDEX_BYTES = 4 * 1024 * 1024
MAX_REQUESTS = 16
MAX_WINDOWS = 1024
MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
MAX_AUDIO_BYTES = 4096 * 1024 * 1024
_HEX64 = re.compile(r'[0-9a-f]{64}\Z')
_HEX32 = re.compile(r'[0-9a-f]{32}\Z')
_PAYLOAD_NAME = re.compile(r'[0-9a-f]{64}_(?:0|[1-9][0-9]{0,3})_[0-9a-f]{32}\.(?:wav|stage)\Z')
COPY_BLOCK_BYTES = 65536


class RecoveryCacheUnavailable(RuntimeError):
    """Optional cache failure; ownership/dependency exceptions are distinct."""


def is_recovery_storage_platform_supported():
    # Windows directory sync currently is a no-op in the existing helper.
    # macOS filesystem/native qualification is intentionally a separate step.
    return (sys.platform == 'linux' and hasattr(os, 'O_DIRECTORY')
            and hasattr(os, 'O_NOFOLLOW') and os.open in os.supports_dir_fd
            and os.stat in os.supports_dir_fd)


def _canonical_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=True, allow_nan=False).encode('ascii')


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RecoveryCacheUnavailable('duplicate_index_key')
        result[key] = value
    return result


def _is_hex(value, pattern=_HEX64):
    return isinstance(value, str) and pattern.fullmatch(value) is not None


def _get_file_identity(info):
    # Reading may update atime; it is not evidence of changed payload bytes.
    return (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
            info.st_ctime_ns, info.st_mode, info.st_nlink, info.st_uid)


def _validate_body(body):
    if (not isinstance(body, dict) or set(body) != {'version', 'requests'}
            or type(body['version']) is not int or body['version'] != 1
            or not isinstance(body['requests'], dict) or len(body['requests']) > MAX_REQUESTS):
        raise RecoveryCacheUnavailable('invalid_index_schema')
    charged = 0
    for key, request in body['requests'].items():
        if (not _is_hex(key) or not isinstance(request, dict) or set(request) != {'uid', 'windows'}
                or not _is_hex(request['uid']) or not isinstance(request['windows'], dict)
                or len(request['windows']) > MAX_WINDOWS):
            raise RecoveryCacheUnavailable('invalid_request_schema')
        reserved = 0
        for index, row in request['windows'].items():
            if (not isinstance(index, str) or not index.isascii() or not index.isdecimal()
                    or len(index) > 4 or str(int(index)) != index or not 0 <= int(index) < MAX_WINDOWS
                    or not isinstance(row, dict) or set(row) != {
                        'state', 'attempt', 'claim', 'size', 'sha256', 'frames', 'rate', 'channels'}):
                raise RecoveryCacheUnavailable('invalid_window_schema')
            if (row['state'] not in ('reserved', 'complete') or not _is_hex(row['attempt'], _HEX32)
                    or not _is_hex(row['claim']) or not _is_hex(row['sha256'])
                    or type(row['size']) is not int or not 0 < row['size'] <= MAX_ARTIFACT_BYTES
                    or type(row['frames']) is not int or not 0 < row['frames'] <= MAX_ARTIFACT_BYTES
                    or type(row['rate']) is not int or not 0 < row['rate'] <= 384000
                    or type(row['channels']) is not int or row['channels'] not in (1, 2)):
                raise RecoveryCacheUnavailable('invalid_window_metadata')
            reserved += row['state'] == 'reserved'
            charged += row['size']
        if reserved > 1:
            raise RecoveryCacheUnavailable('too_many_parent_reservations')
    if charged > MAX_AUDIO_BYTES:
        raise RecoveryCacheUnavailable('invalid_accounting')
    return charged


class _IndexTransaction:
    """One private proposal and at most one commit while its admission is live."""
    def __init__(self, storage, data, writable):
        self.data = data
        self._storage = storage
        self._writable = writable
        self._thread = threading.get_ident()
        self._closed = False
        self._written = False

    def commit(self):
        if (self._closed or self._written or not self._writable
                or threading.get_ident() != self._thread):
            raise RecoveryCacheUnavailable('inactive_index_admission')
        # Even an ambiguous failure requires a new transaction/disk reread.
        self._written = True
        self._storage._save_index(self.data)


class RecoveryCacheStorage:
    """Owned persistent cache namespace, with explicit reader/writer admissions.

    admit is the captured Project owner/book/UID context factory. It must not
    be a cache lock or call back into these methods. This class never infers
    authority from persisted claim fields. Use it as a context manager.
    """
    def __init__(self, project_root, admit):
        self.project_root = Path(project_root)
        self.admit = admit
        self._parent_fd = None
        self._directory_fd = None

    def __enter__(self):
        if not is_recovery_storage_platform_supported():
            raise RecoveryCacheUnavailable('unsupported_durability')
        if not callable(self.admit):
            raise RecoveryCacheUnavailable('unowned_request')
        if self._parent_fd is not None:
            raise RecoveryCacheUnavailable('already_open')
        try:
            parent = self.project_root.resolve(strict=True)
            self._parent_fd = os.open(parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            with self.admit():
                created = False
                try:
                    os.mkdir(CACHE_DIRECTORY, mode=0o700, dir_fd=self._parent_fd)
                    created = True
                    os.fsync(self._parent_fd)
                except FileExistsError:
                    pass
                self._directory_fd = os.open(CACHE_DIRECTORY, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                             dir_fd=self._parent_fd)
                self._check_root()
                # Prove that this filesystem accepts actual directory sync.
                os.fsync(self._directory_fd)
                with self._locked_index():
                    if created:
                        self._save_index({'version': 1, 'requests': {}})
                    else:
                        self._load_index()  # Missing/corrupt existing authority is never reset.
            return self
        except BaseException:
            self.close()
            raise

    def __exit__(self, *_):
        self.close()

    def close(self):
        for name in ('_directory_fd', '_parent_fd'):
            descriptor = getattr(self, name)
            if descriptor is not None:
                setattr(self, name, None)
                os.close(descriptor)

    def _check_root(self):
        if self._directory_fd is None or self._parent_fd is None:
            raise RecoveryCacheUnavailable('closed_cache')
        current = os.stat(CACHE_DIRECTORY, dir_fd=self._parent_fd, follow_symlinks=False)
        captured = os.fstat(self._directory_fd)
        if (not stat.S_ISDIR(current.st_mode) or current.st_mode & 0o077
                or current.st_uid != os.geteuid()
                or (current.st_dev, current.st_ino) != (captured.st_dev, captured.st_ino)):
            raise RecoveryCacheUnavailable('unsafe_cache_directory')

    def _open_regular(self, name, flags, *, maximum=None):
        if (name not in (INDEX_NAME, PENDING_NAME, INDEX_NAME + '.lock')
                and not _PAYLOAD_NAME.fullmatch(name)):
            raise RecoveryCacheUnavailable('invalid_cache_path')
        self._check_root()

        # Reject FIFOs/devices without blocking in open before fstat can inspect them.
        fd = os.open(name, flags | os.O_NOFOLLOW | os.O_NONBLOCK,
                     mode=0o600, dir_fd=self._directory_fd)
        try:
            info = os.fstat(fd)
            if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                    or info.st_uid != os.geteuid() or info.st_mode & 0o077
                    or maximum is not None and info.st_size > maximum):
                raise RecoveryCacheUnavailable('unsafe_cache_file')
            return fd
        except BaseException:
            os.close(fd)
            raise

    def _get_payload_names(self, request_key, index, row):
        # Inputs have already passed the authoritative schema; never read paths
        # from metadata or derive them from text, UID, speaker or model names.
        leaf = f'{request_key}_{index}_{row["attempt"]}'
        return leaf + '.stage', leaf + '.wav'

    def _ensure_same_row(self, transaction, request_key, index, expected):
        request = transaction.data['requests'].get(request_key)
        if request is None or request['windows'].get(index) != expected:
            raise RecoveryCacheUnavailable('reservation_changed')

    def save_window(self, request_key, uid, index, claim, source, metadata, *, budget_bytes):
        """Reserve, stream and durably complete one already validated WAV.

        Failure never rolls back accounting: a new admitted transaction must
        reconcile the known paths. This method does not catch owner exceptions.
        Readers/copying hold only the cache lock; owner-held lock attempts are
        always nonblocking. A false return means bounded capacity was exhausted.
        """
        if (type(index) is not int or not 0 <= index < MAX_WINDOWS
                or type(budget_bytes) is not int or not 0 < budget_bytes <= MAX_AUDIO_BYTES
                or not isinstance(metadata, dict) or set(metadata) != {
                    'size', 'sha256', 'frames', 'rate', 'channels'}):
            raise RecoveryCacheUnavailable('invalid_window_proposal')
        index = str(index)
        row = dict(metadata, state='reserved', attempt=uuid.uuid4().hex, claim=claim)
        proposal = {'version': 1, 'requests': {request_key: {'uid': uid, 'windows': {index: row}}}}
        _validate_body(proposal)
        with self.write_transaction() as transaction:
            charged = _validate_body(transaction.data)
            request = transaction.data['requests'].get(request_key)
            if (charged + row['size'] > budget_bytes
                    or request is None and len(transaction.data['requests']) >= MAX_REQUESTS):
                return False
            if request is None:
                request = {'uid': uid, 'windows': {}}
                transaction.data['requests'][request_key] = request
            if request['uid'] != uid:
                raise RecoveryCacheUnavailable('request_uid_changed')
            if index in request['windows'] or any(r['state'] == 'reserved' for r in request['windows'].values()):
                return False
            request['windows'][index] = copy.deepcopy(row)
            transaction.commit()
        stage, final = self._get_payload_names(request_key, index, row)
        # Reread the reservation while holding only the cache lock. A new owner
        # may have retired it between reserve and copy; then create no bytes.
        with self.read_transaction() as transaction:
            self._ensure_same_row(transaction, request_key, index, row)
            source_fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(source_fd, 'rb') as source_handle:
                source_before = os.fstat(source_handle.fileno())
                if not stat.S_ISREG(source_before.st_mode) or source_before.st_size != row['size']:
                    raise RecoveryCacheUnavailable('source_size_changed')
                fd = self._open_regular(stage, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                                        maximum=MAX_ARTIFACT_BYTES)
                with os.fdopen(fd, 'wb') as destination:
                    self._copy_exact(source_handle, destination, row)
                    destination.flush()
                    os.fsync(destination.fileno())
                    stage_identity = _get_file_identity(os.fstat(destination.fileno()))
                if _get_file_identity(source_before) != _get_file_identity(os.fstat(source_handle.fileno())):
                    raise RecoveryCacheUnavailable('source_changed')
        with self.write_transaction() as transaction:
            self._ensure_same_row(transaction, request_key, index, row)
            self._check_root()
            fd = self._open_regular(stage, os.O_RDONLY, maximum=MAX_ARTIFACT_BYTES)
            try:
                if _get_file_identity(os.fstat(fd)) != stage_identity:
                    raise RecoveryCacheUnavailable('staged_payload_changed')
            finally:
                os.close(fd)
            # A reserved immutable version owns both names. Never overwrite an
            # existing final path, even after a previous ambiguous promotion.
            try:
                os.stat(final, dir_fd=self._directory_fd, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise RecoveryCacheUnavailable('version_already_exists')
            os.rename(stage, final, src_dir_fd=self._directory_fd, dst_dir_fd=self._directory_fd)
            os.fsync(self._directory_fd)
            transaction.data['requests'][request_key]['windows'][index]['state'] = 'complete'
            transaction.commit()
        return True

    @staticmethod
    def _copy_exact(source, destination, row):
        digest = hashlib.sha256()
        remaining = row['size']
        while remaining:
            block = source.read(min(COPY_BLOCK_BYTES, remaining))
            if not block:
                raise RecoveryCacheUnavailable('short_payload')
            if destination.write(block) != len(block):
                raise OSError('Short recovery payload write')
            digest.update(block)
            remaining -= len(block)
        if source.read(1) or digest.hexdigest() != row['sha256']:
            raise RecoveryCacheUnavailable('payload_integrity_mismatch')

    def capture_window(self, request_key, uid, index, destination):
        """Capture immutable bytes to a new private file; validate audio outside.

        No owner or cancellation callback runs during this bounded reader copy.
        The caller must re-admit its current request/dependencies before reuse.
        """
        if not _is_hex(request_key) or not _is_hex(uid) or type(index) is not int or not 0 <= index < MAX_WINDOWS:
            raise RecoveryCacheUnavailable('invalid_capture_request')
        with self.read_transaction() as transaction:
            request = transaction.data['requests'].get(request_key)
            if request is None or request['uid'] != uid:
                return None
            row = request['windows'].get(str(index))
            if row is None or row['state'] != 'complete':
                return None
            _, final = self._get_payload_names(request_key, str(index), row)
            fd = self._open_regular(final, os.O_RDONLY, maximum=MAX_ARTIFACT_BYTES)
            with os.fdopen(fd, 'rb') as source:
                before = os.fstat(source.fileno())
                if before.st_size != row['size']:
                    raise RecoveryCacheUnavailable('payload_size_changed')
                destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                                         mode=0o600)
                with os.fdopen(destination_fd, 'wb') as working:
                    self._copy_exact(source, working, row)
                if _get_file_identity(before) != _get_file_identity(os.fstat(source.fileno())):
                    raise RecoveryCacheUnavailable('payload_changed')
            return copy.deepcopy(row)

    def _unlink_rows(self, request_key, rows):
        for index, row in rows.items():
            for name in self._get_payload_names(request_key, index, row):
                try:
                    fd = self._open_regular(name, os.O_RDONLY, maximum=MAX_ARTIFACT_BYTES)
                except FileNotFoundError:
                    continue
                os.close(fd)
                os.unlink(name, dir_fd=self._directory_fd)
        os.fsync(self._directory_fd)

    def prepare_request(self, request_key, uid, claim, *, budget_bytes):
        """Adopt completed matching windows and reconcile an older claim's write.

        Persisted claims describe epochs; live admission authorizes all changes.
        Protect every parent containing a current-claim row from quota eviction.
        At most one stale other parent is retired per admission.
        """
        if (not all(_is_hex(value) for value in (request_key, uid, claim))
                or type(budget_bytes) is not int or not 0 < budget_bytes <= MAX_AUDIO_BYTES):
            raise RecoveryCacheUnavailable('invalid_prepare_request')
        with self.write_transaction() as transaction:
            requests = transaction.data['requests']
            request = requests.get(request_key)
            changed = False
            if request is not None:
                if request['uid'] != uid:
                    raise RecoveryCacheUnavailable('request_uid_changed')
                stale = {index: row for index, row in request['windows'].items()
                         if row['state'] == 'reserved' and row['claim'] != claim}
                if stale:
                    self._unlink_rows(request_key, stale)
                    for index in stale:
                        del request['windows'][index]
                    changed = True
                for row in request['windows'].values():
                    if row['state'] == 'complete' and row['claim'] != claim:
                        row['claim'] = claim
                        changed = True
            charged = _validate_body(transaction.data)
            if charged >= budget_bytes or request is None and len(requests) >= MAX_REQUESTS:
                for key, candidate in list(requests.items()):
                    if key != request_key and not any(row['claim'] == claim for row in candidate['windows'].values()):
                        self._unlink_rows(key, candidate['windows'])
                        del requests[key]
                        changed = True
                        break
            if changed:
                transaction.commit()

    def retire_request(self, request_key):
        """Remove known paths before freeing charge; call only after parent commit.

        Also usable for a stale request under a new exact owner. Retention policy
        must protect other parents from the current claim before calling this.
        """
        if not _is_hex(request_key):
            raise RecoveryCacheUnavailable('invalid_retirement_request')
        with self.write_transaction() as transaction:
            request = transaction.data['requests'].get(request_key)
            if request is None:
                return
            # Even when a prior attempt unlinked everything before failing, its
            # charge cannot be released until directory durability succeeds.
            self._unlink_rows(request_key, request['windows'])
            del transaction.data['requests'][request_key]
            transaction.commit()
    @contextmanager
    def _locked_index(self):
        fd = self._open_regular(INDEX_NAME + '.lock', os.O_RDWR | os.O_CREAT, maximum=1)
        with os.fdopen(fd, 'a+b') as handle:
            with file_lock(str(self.project_root / CACHE_DIRECTORY / INDEX_NAME),
                           timeout=0, lock_handle=handle):
                self._check_root()
                yield

    @contextmanager
    def _transaction(self, writable):
        def enter():
            return self.admit() if writable else _reader_admission()
        with enter():
            with self._locked_index():
                transaction = _IndexTransaction(self, self._load_index(), writable)
                try:
                    yield transaction
                finally:
                    transaction._closed = True

    def write_transaction(self):
        return self._transaction(True)

    def read_transaction(self):
        # No owner/book/chunk callback under this cache-only admission.
        return self._transaction(False)

    def _load_index(self):
        fd = self._open_regular(INDEX_NAME, os.O_RDONLY, maximum=MAX_INDEX_BYTES)
        with os.fdopen(fd, 'rb') as handle:
            before = os.fstat(handle.fileno())
            data = handle.read(MAX_INDEX_BYTES + 1)
            after = os.fstat(handle.fileno())
        if (len(data) > MAX_INDEX_BYTES or len(data) != before.st_size or
                (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns, before.st_ctime_ns) !=
                (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns, after.st_ctime_ns)):
            raise RecoveryCacheUnavailable('unstable_index')
        try:
            document = json.loads(data, object_pairs_hook=_unique_object)
            if not isinstance(document, dict) or set(document) != {'version', 'requests', 'sha256'}:
                raise RecoveryCacheUnavailable('invalid_index_envelope')
            digest = document.pop('sha256')
            _validate_body(document)
            if not _is_hex(digest) or hashlib.sha256(_canonical_bytes(document)).hexdigest() != digest:
                raise RecoveryCacheUnavailable('index_integrity_mismatch')
            return document
        except (ValueError, UnicodeError, RecursionError) as error:
            raise RecoveryCacheUnavailable('invalid_index_json') from error

    def _save_index(self, body):
        _validate_body(body)
        payload = _canonical_bytes(body)
        envelope = {**copy.deepcopy(body), 'sha256': hashlib.sha256(payload).hexdigest()}
        data = _canonical_bytes(envelope)
        if len(data) > MAX_INDEX_BYTES:
            raise RecoveryCacheUnavailable('index_limit')
        fd = self._open_regular(PENDING_NAME, os.O_RDWR | os.O_CREAT, maximum=MAX_INDEX_BYTES)
        with os.fdopen(fd, 'r+b') as handle:
            handle.truncate(0)
            written = handle.write(data)
            if written != len(data):
                raise OSError('Short recovery index write')
            handle.flush()
            os.fsync(handle.fileno())
        self._check_root()
        os.replace(PENDING_NAME, INDEX_NAME, src_dir_fd=self._directory_fd, dst_dir_fd=self._directory_fd)
        os.fsync(self._directory_fd)
        self._check_root()


@contextmanager
def _reader_admission():
    yield


from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_recovery_code
_LOADED_RECOVERY_CODE_SHA256 = _get_loaded_recovery_code()
