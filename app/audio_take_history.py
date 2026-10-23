"""Bounded, explicit immutable snapshots; chunk metadata alone selects audio."""
from contextlib import contextmanager, ExitStack
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import time
import uuid

from adapter_publication import sync_adapter_directory
from audio_generation_owner import AudioGenerationCancelled
from audio_validation import GeneratedAudioError, validate_wav_container
from utils import file_lock
from take_metadata_pool import compact_metadata, expand_metadata

MAX_RECORDS = 4096
MAX_LINE_RECORDS = 16
MAX_AUDIO_BYTES = 64 * 1024**2
MAX_PAYLOAD_BYTES = 1024**3
MAX_MANIFEST_BYTES = 16 * 1024**2
MAX_RECORD_BYTES = 128 * 1024
MAX_METADATA_DEPTH = 64
_VERSION = re.compile(r'[0-9a-f]{32}')
_DIGEST = re.compile(r'[0-9a-f]{64}')
_SUFFIXES = {'.wav', '.mp3', '.flac', '.ogg'}


class TakeAudioOutsideSnapshotBounds(ValueError):
    """Valid output may be ineligible for bounded optional take evidence."""


def _encode(value):
    return json.dumps(value, ensure_ascii=True, sort_keys=True,
                      separators=(',', ':'), allow_nan=False).encode('ascii')


def _ensure_metadata_depth(value):
    # JSON's parser can accept nesting which later deepcopy/response encoding
    # cannot. Admit bounded structure before any recursive record operation.
    # Iterator frames use memory proportional to depth, not record breadth.
    frames = [iter((value,))]
    deepest = 0
    while frames:
        try:
            item = next(frames[-1])
        except StopIteration:
            frames.pop()
            continue
        if type(item) is dict:
            children = iter(item.values())
        elif type(item) is list:
            children = iter(item)
        else:
            continue
        if len(frames) > MAX_METADATA_DEPTH:
            raise ValueError('Take history metadata is too deeply nested; existing audio was preserved.')
        deepest = max(deepest, len(frames))
        frames.append(children)
    return deepest


def _version(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _ensure_plain_path(path, root):
    path = Path(os.path.abspath(path))
    try:
        parts = path.relative_to(root).parts
    except ValueError as error:
        raise ValueError('Take audio must stay inside the project.') from error
    current = root
    for part in parts:
        current /= part
        if (current.is_symlink() or os.path.normcase(os.path.realpath(current)) != os.path.normcase(str(current))):
            raise ValueError('Take history does not accept file or directory aliases.')
    return path


def _open_regular(path, root, limit, *, allow_empty=False):
    path = _ensure_plain_path(path, root)
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or not (0 if allow_empty else 1) <= before.st_size <= limit:
        raise ValueError('Take history needs a nonempty, bounded regular file without hard links.')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    try:
        stream = os.fdopen(fd, 'rb')
    except BaseException:
        os.close(fd)
        raise
    try:
        if _version(os.fstat(fd)) != _version(before):
            raise ValueError('Take file changed while opening.')
        return stream, before
    except BaseException:
        stream.close()
        raise


def _check_cancelled(cancel_check):
    if cancel_check is not None and cancel_check():
        raise AudioGenerationCancelled()


def _hash(stream, limit, *, cancel_check=None):
    from audio_source_io import read_bounded_audio_hash
    return read_bounded_audio_hash(stream, limit, lambda: _check_cancelled(cancel_check))


def _decode(stream, *, cancel_check=None, source_path=None):
    import numpy as np
    import soundfile as sf
    try:
        _check_cancelled(cancel_check)
        validate_wav_container(stream, 'Take snapshot',
                               cancel_check=lambda: _check_cancelled(cancel_check))
    except GeneratedAudioError as error:
        raise ValueError(str(error)) from error
    stream.seek(0)
    with sf.SoundFile(stream, closefd=False) as audio:
        rate, channels, expected = audio.samplerate, audio.channels, audio.frames
        if not 0 < rate <= 192000 or channels not in (1, 2) or not 0 < expected <= rate * 600:
            raise TakeAudioOutsideSnapshotBounds('Take snapshots support mono/stereo audio of at most ten minutes.')
        if audio.format == 'MP3':
            from audio_pcm_decode import decode_audio_pcm
            if source_path is None:
                raise ValueError('MPEG validation needs its admitted seekable source path.')
            return decode_audio_pcm(stream, source_path, frames=expected,
                sample_rate=rate, channels=channels, source_limit=MAX_AUDIO_BYTES,
                check_active=lambda: _check_cancelled(cancel_check),
                cancellation_errors=(AudioGenerationCancelled,))
        frames = 0
        while True:
            _check_cancelled(cancel_check)
            block = audio.read(65536, dtype='float32', always_2d=True)
            if not len(block):
                break
            frames += len(block)
            if frames > expected or not np.isfinite(block).all():
                raise ValueError('Take audio has invalid or non-finite decoded samples.')
        if frames != expected:
            raise ValueError('Take audio is truncated.')
    return dict(frames=frames, sample_rate=rate, channels=channels)


class AudioTakeHistory:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        self.directory = self.root / 'take_history'
        self.manifest = self.directory / 'manifest.json'
        self.pending = self.directory / 'manifest.pending'
        self._compact_records = {}

    @contextmanager
    def _locked(self, *, cancel_check=None):
        _check_cancelled(cancel_check)
        _ensure_plain_path(self.directory, self.root)
        self.directory.mkdir(exist_ok=True)
        lock = Path(str(self.manifest) + '.lock')
        _ensure_plain_path(lock, self.root)
        if lock.exists() and (not lock.is_file() or lock.stat().st_nlink != 1):
            raise ValueError('Take history lock must be a regular owned file.')
        with ExitStack() as stack:
            if cancel_check is None:
                stack.enter_context(file_lock(str(self.manifest)))
            else:
                # Never hold an audio task mutex while waiting or copying. The
                # callback may briefly reacquire it to admit the owning task.
                deadline = time.monotonic() + 10
                while True:
                    _check_cancelled(cancel_check)
                    try:
                        stack.enter_context(file_lock(str(self.manifest), timeout=0))
                    except TimeoutError:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise
                        time.sleep(min(.05, remaining))
                    else:
                        break
            _check_cancelled(cancel_check)
            for path in (self.manifest, self.pending):
                _ensure_plain_path(path, self.root)
                if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
                    raise ValueError('Take history metadata must be a regular owned file.')
                if path == self.pending and path.exists() and path.stat().st_size > MAX_MANIFEST_BYTES:
                    raise ValueError('Interrupted take metadata exceeds its size limit; it was preserved.')
            yield self._load()

    def _load(self):
        if not self.manifest.exists():
            return {'version': 1, 'records': []}
        stream, info = _open_regular(self.manifest, self.root, MAX_MANIFEST_BYTES)
        with stream:
            # Allocate for the admitted extent, rather than16MiB on every small
            # list. One extra byte and the version checks still refuse growth.
            data = stream.read(info.st_size + 1)
            if len(data) != info.st_size or _version(os.fstat(stream.fileno())) != _version(info):
                raise ValueError('Take metadata changed while reading; it was preserved.')
        if _version(_ensure_plain_path(self.manifest, self.root).stat()) != _version(info):
            raise ValueError('Take metadata changed while reading; it was preserved.')
        try:
            document = json.loads(data)
        except RecursionError as error:
            raise ValueError('Take history manifest is too deeply nested; existing audio was preserved.') from error
        if type(document) is dict and document.get('version') == 3:
            document = expand_metadata(document, encode=_encode, max_records=MAX_RECORDS,
                                       max_record_bytes=MAX_RECORD_BYTES,
                                       max_manifest_bytes=MAX_MANIFEST_BYTES,
                                       check_depth=_ensure_metadata_depth)
        return self._validate(document)

    def _validate(self, document):
        for _ in self._iter_validated_record_bytes(document):
            pass
        return document

    def _iter_validated_record_bytes(self, document):
        depth = _ensure_metadata_depth(document)
        if (type(document) is not dict or set(document) != {'version', 'records'}
                or type(document['version']) is not int or document['version'] not in (1, 2) or type(document['records']) is not list
                or len(document['records']) > MAX_RECORDS):
            raise ValueError('Take history manifest is invalid; existing audio was preserved.')
        seen, counts, total = set(), {}, 0
        for row in document['records']:
            if type(row) is not dict:
                raise ValueError('Take history record is invalid; existing audio was preserved.')
            raw = _encode(row)
            if (len(raw) > MAX_RECORD_BYTES
                    or type(row.get('version')) is not str or not _VERSION.fullmatch(row['version'])
                    or type(row.get('sha256')) is not str or not _DIGEST.fullmatch(row['sha256'])
                    or type(row.get('book_token')) is not str or not _DIGEST.fullmatch(row['book_token'])
                    or type(row.get('uid')) is not str or not 0 < len(row['uid']) <= 128
                    or row.get('state') not in ('pending', 'ready', 'deleted', 'purging')
                    or type(row.get('protected')) is not bool
                    or type(row.get('captured')) is not dict
                    or type(row.get('created')) not in (int, float) or not 0 <= row['created'] <= 253402300799
                    or type(row.get('bytes')) is not int or not 0 < row['bytes'] <= MAX_AUDIO_BYTES
                    or row.get('suffix') not in _SUFFIXES or row['version'] in seen):
                raise ValueError('Take history record is invalid; existing audio was preserved.')
            seen.add(row['version']); total += row['bytes']
            key = (row['book_token'], row['uid']); counts[key] = counts.get(key, 0) + 1
            yield raw, depth
        if total > MAX_PAYLOAD_BYTES or any(n > MAX_LINE_RECORDS for n in counts.values()):
            raise ValueError('Take history exceeds its storage limits; existing audio was preserved.')

    def _save(self, document, *, pooled=True):
        fingerprints, depth = [], 0
        for raw, depth in self._iter_validated_record_bytes(document):
            fingerprints.append(hashlib.sha256(raw).digest())
        data = _encode(document)
        if len(data) > MAX_MANIFEST_BYTES:
            raise ValueError('Take history metadata is full. Remove an unneeded snapshot before saving another.')
        current_records = {}
        # Compact record wrappers add one level. Preserve deeply nested valid
        # legacy records in their original representation rather than widening
        # the parser/deepcopy bound or retaining an unreadable candidate.
        if pooled and depth < MAX_METADATA_DEPTH:
            minimum_saving = max(4096, (len(data) + 19) // 20)
            candidate = compact_metadata(document, encode=_encode, minimum_saving=minimum_saving,
                                         fingerprints=tuple(fingerprints), previous_records=self._compact_records)
            if candidate is not None and len(data) - len(candidate.encoded) >= minimum_saving:
                data, current_records = candidate.encoded, candidate.records
        # One declared, bounded pending name can be overwritten after interruption.
        # Its bytes never authorize payload creation until the manifest replaces.
        _ensure_plain_path(self.pending, self.root)
        fd = os.open(self.pending, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            stream = os.fdopen(fd, 'w+b')
        except BaseException:
            os.close(fd)
            raise
        with stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_MANIFEST_BYTES:
                raise ValueError('Interrupted take metadata is unsupported; it was preserved.')
            stream.truncate(0)
            stream.write(data); stream.flush(); os.fsync(stream.fileno())
        os.replace(self.pending, self.manifest)
        sync_adapter_directory(str(self.directory))
        # Only one successfully published snapshot is retained. Cached content
        # is immutable bytes/tuples and is never an admission or validation gate.
        self._compact_records = current_records

    def apply_metadata_format(self, *, pooled):
        """Explicit reversible metadata conversion under the archive lock."""
        if type(pooled) is not bool:
            raise ValueError('Choose compact or expanded take metadata.')
        with self._locked() as document:
            if not self.manifest.exists():
                raise ValueError('There is no saved take metadata to convert.')
            self._ensure_inventory(document)
            self._save(document, pooled=pooled)

    def enable_automatic_format(self, *, cancel_check=None):
        """Fence older manual-only writers before any automatic hold is saved."""
        with self._locked(cancel_check=cancel_check) as document:
            self._ensure_inventory(document)
            if document['version'] == 1:
                document['version'] = 2
                self._save(document)

    def verify_retained(self, version, sha256, book_token, uid, captured, *, cancel_check=None):
        """Verify a committed predecessor without reopening a reused source slot."""
        with self._locked(cancel_check=cancel_check) as document:
            if document['version'] != 2:
                raise ValueError('Automatic take history requires its newer archive format.')
            row = self._find(document, version, sha256, book_token, uid)
            if row['state'] != 'ready' or row['captured'] != captured:
                raise ValueError('Retained predecessor changed. Inspect take history before retrying.')
            with self._open_verified(row, cancel_check=cancel_check):
                _check_cancelled(cancel_check)
            return {**self._summary(row), 'path': self._path(row).relative_to(self.root).as_posix()}

    def _path(self, row):
        return self.directory / (row['version'] + row['suffix'])

    def _ensure_inventory(self, document):
        expected = {'manifest.json', 'manifest.pending', 'manifest.json.lock'} | {self._path(row).name for row in document['records']}
        for path in self.directory.iterdir():
            if path.name not in expected:
                raise ValueError('Unknown take history files were preserved. Inspect the archive before changing it.')
            _ensure_plain_path(path, self.root)
            if not path.is_file() or path.stat().st_nlink != 1:
                raise ValueError('Take history contains an unsupported file.')

    @staticmethod
    def _summary(row):
        from take_publication import KEY, validate_take_publication
        result = {key: copy.deepcopy(row.get(key)) for key in (
            'version', 'book_token', 'book_label', 'uid', 'sha256', 'bytes', 'state',
            'protected', 'created', 'take', 'audio_revision', 'shape')}
        captured = row['captured']
        result['text'] = captured.get('text', '')[:160] if isinstance(captured.get('text'), str) else ''
        result['speaker'] = captured.get('speaker', '')[:128] if isinstance(captured.get('speaker'), str) else ''
        # A metadata hint only. Current dependencies and actual bytes are
        # checked exclusively by the explicit restoration worker.
        try:
            result['has_restore_evidence'] = validate_take_publication({
                'uid':row['uid'], 'audio_revision':row.get('audio_revision'), KEY:captured.get(KEY)}) is not None
        except ValueError:
            result['has_restore_evidence'] = False
        return result

    def list(self, book_token=None, uid=None, offset=0):
        with self._locked() as document:
            records = document['records']
            selected = [r for r in reversed(records) if (book_token is None or r['book_token'] == book_token)
                        and (uid is None or r['uid'] == uid)]
            return dict(records=[self._summary(r) for r in selected[offset:offset + 50]],
                        total=len(selected), next_offset=offset + 50 if len(selected) > offset + 50 else None,
                        usage=dict(records=len(records), bytes=sum(r['bytes'] for r in records),
                                   max_records=MAX_RECORDS, max_bytes=MAX_PAYLOAD_BYTES,
                                   max_line_records=MAX_LINE_RECORDS),
                        notice='Snapshots retain originating metadata. Browsing alone does not establish current restoration eligibility or change selection.')

    def capture(self, path, book_token, uid, captured, *, book_label='',
                expected_source_version=None, cancel_check=None):
        """Snapshot admitted bytes, optionally fenced to an earlier file version.

        Caller keeps book/source identity stable. A cancellation callback may
        return true or raise its owner's cancellation exception; it must not be
        called under a nonreentrant owner mutex. Interrupted copies stay charged.
        """
        _check_cancelled(cancel_check)
        if expected_source_version is not None:
            if (type(expected_source_version) not in (tuple, list)
                    or len(expected_source_version) != 5
                    or any(type(value) is not int for value in expected_source_version)):
                raise ValueError('Invalid predecessor file version.')
            expected_source_version = tuple(expected_source_version)
        if type(book_token) is not str or not _DIGEST.fullmatch(book_token) or type(uid) is not str or not 0 < len(uid) <= 128:
            raise ValueError('Invalid take snapshot target.')
        # JSON round-trip makes an owned, finite, bounded record before reservation.
        captured = json.loads(_encode(captured))
        if len(_encode(captured)) > MAX_RECORD_BYTES - 2048:
            raise ValueError('This take has too much recorded metadata to snapshot.')
        path = Path(path)
        suffix = path.suffix.lower()
        if suffix not in _SUFFIXES:
            raise ValueError('This audio format cannot be saved in take history.')
        with self._locked(cancel_check=cancel_check) as document, _open_regular(path, self.root, MAX_AUDIO_BYTES)[0] as source:
            self._ensure_inventory(document)
            original = _version(os.fstat(source.fileno()))
            if expected_source_version is not None and original != expected_source_version:
                raise ValueError('Prior take changed since its metadata was captured; existing files were preserved.')
            digest, size = _hash(source, MAX_AUDIO_BYTES, cancel_check=cancel_check)
            if _version(os.fstat(source.fileno())) != original or _version(path.stat()) != original:
                raise ValueError('Selected take changed while hashing.')
            for row in document['records']:
                if (row['book_token'], row['uid'], row['sha256'], row.get('captured')) == (book_token, uid, digest, captured):
                    if row['state'] != 'ready':
                        raise ValueError('This snapshot is deleted or incomplete. Undo or remove it before saving it again.')
                    # A damaged existing copy must never masquerade as a successful save.
                    with self._open_verified(row, cancel_check=cancel_check):
                        _check_cancelled(cancel_check)
                        return self._summary(row)
            rows = document['records']
            if (len(rows) >= MAX_RECORDS or sum(r['bytes'] for r in rows) + size > MAX_PAYLOAD_BYTES
                    or sum((r['book_token'], r['uid']) == (book_token, uid) for r in rows) >= MAX_LINE_RECORDS):
                raise ValueError('Take history is full. Deleted and incomplete copies still count; permanently remove an unneeded snapshot.')
            row = dict(version=uuid.uuid4().hex, book_token=book_token, uid=uid,
                       book_label=str(book_label)[:256], sha256=digest, bytes=size, suffix=suffix,
                       state='pending', protected=False, created=time.time(), captured=captured,
                       take=captured.get('take'), audio_revision=captured.get('audio_revision'))
            _check_cancelled(cancel_check)
            rows.append(row)
            self._save(document)  # Charged before creating any artifact bytes.
            target = self._path(row)
            source.seek(0)
            _check_cancelled(cancel_check)
            with target.open('xb') as output:
                while True:
                    _check_cancelled(cancel_check)
                    block = source.read(1024**2)
                    if not block:
                        break
                    if output.tell() + len(block) > size:
                        raise ValueError('Selected take changed during copying.')
                    output.write(block)
                output.flush(); os.fsync(output.fileno())
            sync_adapter_directory(str(self.directory))
            if (_version(os.fstat(source.fileno())) != original or
                    _version(_ensure_plain_path(path, self.root).stat()) != original):
                raise ValueError('Selected take changed during copying; incomplete snapshot was preserved.')
            with _open_regular(target, self.root, MAX_AUDIO_BYTES)[0] as copied:
                copied_version = _version(os.fstat(copied.fileno()))
                if _hash(copied, MAX_AUDIO_BYTES, cancel_check=cancel_check) != (digest, size):
                    raise ValueError('Snapshot bytes do not match the selected take.')
                try:
                    row['shape'] = _decode(copied, cancel_check=cancel_check, source_path=target)
                except AudioGenerationCancelled:
                    raise
                except RuntimeError as error:
                    raise ValueError('Saved take cannot be decoded; the incomplete snapshot was preserved.') from error
                if (_version(os.fstat(copied.fileno())) != copied_version or
                        _version(_ensure_plain_path(target, self.root).stat()) != copied_version):
                    raise ValueError('Snapshot changed during decoding; incomplete copy was preserved.')
            _check_cancelled(cancel_check)
            row['state'] = 'ready'
            self._save(document)
            return self._summary(row)

    def _open_verified(self, row, *, cancel_check=None):
        stream, info = _open_regular(self._path(row), self.root, MAX_AUDIO_BYTES)
        try:
            if _hash(stream, MAX_AUDIO_BYTES, cancel_check=cancel_check) != (row['sha256'], row['bytes']):
                raise ValueError('Saved take bytes changed; snapshot was preserved for inspection.')
            if _version(os.fstat(stream.fileno())) != _version(info):
                raise ValueError('Saved take changed while verifying.')
            stream.seek(0)
            return stream
        except BaseException:
            stream.close()
            raise

    @staticmethod
    def _find(document, version, sha256, book_token=None, uid=None):
        for row in document['records']:
            if (row['version'] == version and row['sha256'] == sha256
                    and (book_token is None or row['book_token'] == book_token)
                    and (uid is None or row['uid'] == uid)):
                return row
        raise ValueError('Saved snapshot changed or no longer exists. Refresh history.')

    def open(self, version, sha256, *, book_token=None, uid=None):
        with self._locked() as document:
            row = self._find(document, version, sha256, book_token, uid)
            if row['state'] != 'ready':
                raise ValueError('Undo deletion or finish the snapshot before auditioning it.')
            return self._open_verified(row), row['bytes'], row['suffix']

    def open_for_restore(self, version, sha256, book_token, uid, *, cancel_check=None):
        """Pin verified bytes and originating metadata under the archive lock."""
        with self._locked(cancel_check=cancel_check) as document:
            if document['version'] != 2:
                raise ValueError('This archive has no supported restoration evidence.')
            row = self._find(document, version, sha256, book_token, uid)
            if row['state'] != 'ready':
                raise ValueError('Only a complete, undeleted snapshot can be restored.')
            return copy.deepcopy(row), self._open_verified(row, cancel_check=cancel_check)

    def change(self, version, sha256, action, is_referenced, *, book_token=None, uid=None):
        """Caller keeps current chunk references locked throughout mutation."""
        with self._locked() as document:
            self._ensure_inventory(document)
            row = self._find(document, version, sha256, book_token, uid)
            path = self._path(row)
            if action in ('delete', 'purge') and (row['protected'] or is_referenced(path)):
                raise ValueError('Current, referenced or protected take snapshots cannot be removed.')
            if action in ('protect', 'unprotect') and row['state'] == 'ready':
                row['protected'] = action == 'protect'
            elif action == 'delete' and row['state'] == 'ready':
                row['state'] = 'deleted'
            elif action == 'undo' and row['state'] == 'deleted':
                with self._open_verified(row):
                    pass
                row['state'] = 'ready'
            elif action == 'purge' and row['state'] in ('pending', 'deleted', 'purging'):
                # Persist cleanup intent before unlink. Death after unlink leaves a
                # charged, retryable receipt, never unaccounted artifact bytes.
                row['state'] = 'purging'; self._save(document)
                if path.exists():
                    stream, info = _open_regular(path, self.root, MAX_AUDIO_BYTES, allow_empty=True)
                    stream.close()
                    if _version(path.stat()) != _version(info):
                        raise ValueError('Snapshot changed before permanent removal.')
                    path.unlink(); sync_adapter_directory(str(self.directory))
                document['records'].remove(row)
            else:
                raise ValueError('This action is unavailable for the current snapshot state.')
            self._save(document)
            return dict(version=version, action=action)


if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='Convert existing take metadata without changing audio selection.')
    parser.add_argument('project_directory', help='Project data directory containing take_history/')
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--compact-metadata', action='store_true')
    mode.add_argument('--expand-metadata', action='store_true')
    args = parser.parse_args()
    AudioTakeHistory(args.project_directory).apply_metadata_format(pooled=args.compact_metadata)
    print('Metadata saved. Compact storage is used only when it produces a substantial saving.')
