"""Opt-in predecessor receipts; selection remains solely in chunk audio_path."""
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from adapter_publication import sync_adapter_directory
from audio_take_history import (AudioTakeHistory, MAX_AUDIO_BYTES, _encode,
    _ensure_plain_path, _open_regular, _version, _ensure_metadata_depth)
from utils import file_lock

KEY = '_retained_take'
MAX_RECEIPT_BYTES = 128 * 1024
MAX_BOOK_RECEIPT_BYTES = 16 * 1024**2
MAX_POLICY_BYTES = 8192
POLICY = '.take_history_policy.json'
_DIGEST = re.compile(r'[a-f0-9]{64}')
_VERSION = re.compile(r'[a-f0-9]{32}')
_CAPTURED_FIELDS = ('text', 'speaker', 'instruct', 'take', 'audio_revision',
                    'pronunciation_receipt', 'synthesis_receipt', 'speech_verification', 'take_publication')


def get_captured_take(row):
    from project import get_chunk_generation_inputs
    captured = {key: copy.deepcopy(row.get(key)) for key in _CAPTURED_FIELDS}
    captured['generation_inputs'] = copy.deepcopy(get_chunk_generation_inputs(row))
    captured['provenance'] = 'selected artifact snapshot; exact generation dependencies not certified'
    return captured


def get_take_history_policy(root, *, tolerate_invalid=False):
    root = Path(root).resolve()
    path = root / POLICY
    if not os.path.lexists(path):
        return dict(enabled=False, revision=hashlib.sha256(b'').hexdigest(), error=None)
    stream, info = _open_regular(path, root, MAX_POLICY_BYTES, allow_empty=True)
    with stream:
        raw = stream.read(info.st_size + 1)
        if len(raw) != info.st_size or _version(os.fstat(stream.fileno())) != _version(info):
            raise ValueError('Automatic history policy changed while reading.')
    if _version(_ensure_plain_path(path, root).stat()) != _version(info):
        raise ValueError('Automatic history policy changed while reading.')
    revision = hashlib.sha256(raw).hexdigest()
    try:
        value = json.loads(raw)
        if (type(value) is not dict or set(value) != {'version', 'enabled'}
                or type(value['version']) is not int or value['version'] != 1
                or type(value['enabled']) is not bool):
            raise ValueError('Unsupported automatic history policy.')
    except (ValueError, UnicodeError, RecursionError) as error:
        if not tolerate_invalid:
            raise ValueError('Automatic history policy is damaged. Refresh its controls and disable it or inspect the file.') from error
        return dict(enabled=None, revision=revision, error='Policy is damaged; disable to replace these admitted policy bytes.')
    return dict(enabled=value['enabled'], revision=revision, error=None)


def set_take_history_policy(manager, enabled, revision):
    from book_state_transaction import is_book_state_admitted
    root = Path(manager.root_dir).resolve()
    if not is_book_state_admitted(root):
        raise ValueError('Book admission is required to change automatic history.')
    if type(enabled) is not bool or type(revision) is not str or not _DIGEST.fullmatch(revision):
        raise ValueError('Invalid automatic history policy update.')
    path, pending = root / POLICY, root / (POLICY + '.pending')
    for candidate in (path, pending, Path(str(path) + '.lock')):
        _ensure_plain_path(candidate, root)
        if os.path.lexists(candidate):
            info = candidate.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_POLICY_BYTES:
                raise ValueError('Automatic history policy contains an unsupported file; inspect it before changing it.')
    with file_lock(str(path)):
        current = get_take_history_policy(root, tolerate_invalid=True)
        if current['revision'] != revision:
            raise ValueError('Automatic history policy changed. Refresh its controls.')
        if enabled:
            # Validate legacy identity without repairing it or loading a model.
            with manager._chunks_lock, file_lock(manager.chunks_path):
                if os.path.exists(manager.chunks_path):
                    manager._get_saved_chunks_locked()
            AudioTakeHistory(root).enable_automatic_format()
        raw = _encode(dict(version=1, enabled=enabled))
        fd = os.open(pending, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        try:
            output = os.fdopen(fd, 'w+b')
        except BaseException:
            os.close(fd); raise
        with output:
            info = os.fstat(output.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > MAX_POLICY_BYTES:
                raise ValueError('Interrupted automatic policy is unsupported; it was preserved.')
            output.truncate(0); output.write(raw); output.flush(); os.fsync(output.fileno())
        os.replace(pending, path); sync_adapter_directory(str(root))
        return get_take_history_policy(root)


@contextmanager
def admit_take_history_edit(root):
    from book_state_transaction import is_book_state_admitted, reuse_book_state
    if is_book_state_admitted(root) or get_take_history_policy(root)['enabled']:
        with reuse_book_state(root):
            yield
    else:
        yield


def validate_retained_take(row):
    receipt = row.get(KEY)
    if receipt is None:
        return None
    _ensure_metadata_depth(receipt)
    if (type(receipt) is not dict or set(receipt) != {
            'version', 'book_token', 'uid', 'path', 'source_version', 'captured', 'archive'}
            or type(receipt['version']) is not int or receipt['version'] != 1
            or type(receipt['book_token']) is not str or not _DIGEST.fullmatch(receipt['book_token'])
            or type(receipt['uid']) is not str or receipt['uid'] != row.get('uid')
            or not 0 < len(receipt['uid']) <= 128
            or type(receipt['path']) is not str or not receipt['path'] or len(receipt['path']) > 4096
            or type(receipt['source_version']) is not list or len(receipt['source_version']) != 5
            or any(type(value) is not int for value in receipt['source_version'])
            or type(receipt['captured']) is not dict
            or receipt['captured'].get('audio_revision') != row.get('audio_revision')
            or len(_encode(receipt)) > MAX_RECEIPT_BYTES):
        raise ValueError('Retained prior-take metadata needs inspection; existing audio was preserved.')
    archive = receipt['archive']
    if archive is not None:
        if (type(archive) is not dict or set(archive) != {'version', 'sha256', 'path'}
                or type(archive['version']) is not str or not _VERSION.fullmatch(archive['version'])
                or type(archive['sha256']) is not str or not _DIGEST.fullmatch(archive['sha256'])
                or archive['path'] != f"take_history/{archive['version']}{Path(receipt['path']).suffix.lower()}"):
            raise ValueError('Retained archive reference needs inspection; existing audio was preserved.')
    return receipt


def get_retained_audio_paths(row):
    receipt = validate_retained_take(row)
    if receipt is None:
        return ()
    if receipt['archive'] is None:
        return (receipt['path'],)
    # Version identity protects its actual archive payload even if a damaged
    # receipt changes a source suffix consistently with its guessed path.
    return tuple(f"take_history/{receipt['archive']['version']}{suffix}"
                 for suffix in ('.wav', '.mp3', '.flac', '.ogg'))


def get_retained_take_bytes(row):
    publication_bytes = 0
    if row.get('take_publication') is not None:
        from take_publication import validate_take_publication
        publication_bytes = len(_encode(validate_take_publication(row)))
    receipt = validate_retained_take(row)
    return publication_bytes + (len(_encode(receipt)) if receipt is not None else 0)


def get_retained_book_bytes(rows):
    """Admit private receipts before callers copy or mutate saved chunks."""
    total = 0
    for row in rows:
        total += get_retained_take_bytes(row)
        if total > MAX_BOOK_RECEIPT_BYTES:
            raise ValueError('Retained prior-take metadata exceeds its book limit; inspect the saved metadata.')
    return total


def make_retained_take(root, row):
    from book_state_transaction import get_book_operation_token
    token = get_book_operation_token(root)
    raw_path = row.get('audio_path')
    if type(raw_path) is not str or not raw_path:
        return None
    path = Path(os.path.join(root, raw_path))
    if path.suffix.lower() not in ('.wav', '.flac', '.mp3', '.ogg'):
        raise ValueError('Prior take format cannot be retained. Disable automatic history or inspect it.')
    stream, info = _open_regular(path, Path(root).resolve(), MAX_AUDIO_BYTES)
    stream.close()
    if _version(_ensure_plain_path(path, Path(root).resolve()).stat()) != _version(info):
        raise ValueError('Prior take changed while recording its metadata.')
    receipt = dict(version=1, book_token=token, uid=row.get('uid'), path=raw_path,
                   source_version=list(_version(info)), captured=get_captured_take(row), archive=None)
    # Reserve room for the later verified archive reference before accepting edits.
    if len(_encode(receipt)) > MAX_RECEIPT_BYTES - 2048:
        raise ValueError('Prior take metadata is too large. Disable automatic history or inspect it.')
    validate_retained_take({**row, KEY: receipt})
    return receipt


def preserve_retained_takes(root, previous, rows, *, updated_uid=None):
    """Caller holds book/chunk locks; compare before any authored rewrite."""
    from project import get_chunk_generation_inputs
    from book_state_transaction import is_book_state_admitted
    enabled = get_take_history_policy(root)['enabled']
    before = {row.get('uid'): row for row in previous if row.get('uid')}
    total = 0
    for row in rows:
        old = before.get(row.get('uid'))
        if old is not None and row.get('uid') != updated_uid:
            existing = validate_retained_take(old)
            if existing is not None:
                row[KEY] = copy.deepcopy(existing)
            elif enabled and old.get('audio_path') and (
                    not row.get('audio_path') or get_chunk_generation_inputs(old) != get_chunk_generation_inputs(row)):
                if not is_book_state_admitted(root):
                    raise ValueError('Automatic history changed during this edit. Refresh and retry.')
                row[KEY] = make_retained_take(root, old)
        total += get_retained_take_bytes(row)
    if total > MAX_BOOK_RECEIPT_BYTES:
        raise ValueError('Prior-take metadata is full. Disable automatic history before editing more lines.')
