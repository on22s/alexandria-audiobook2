"""Keep large older deliveries on disk while fencing unchanged pre-scope files."""
from contextlib import contextmanager, nullcontext
import base64
import contextvars
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid
from audio_take_history import _ensure_plain_path, _version
from editor_poll_snapshot import get_snapshot_digest
from export_file_io import PUBLICATION_JOURNAL, PUBLICATION_SET_JOURNAL
from project_scope_files import capture_scope_file

LEGACY_EXPORTS = ('cloned_audiobook.mp3', 'audiobook.m4b', 'audacity_export.zip', 'chapter_exports')
SAVED_BOOK_FILE_REMOVALS = LEGACY_EXPORTS[:-1]
# R2: read-along artifacts join the same pre-scope fencing. The legacy set
# stays accepted so inventories persisted before this extension remain
# interpretable instead of raising "needs inspection".
READALONG_EXPORTS = ('readalong.sidecar.json', 'readaloud.epub', 'readaloud.receipt.json')
BASE_EXPORTS = LEGACY_EXPORTS + READALONG_EXPORTS
# Mixed deliveries are digest-bound but a digest is not book or scope
# provenance: inventory them in the same bounded fence as standard exports.
MIXED_DELIVERIES = ('mixed_audiobook.mp3', 'mixed_audiobook.wav', 'mixed_audiobook.m4b')
EXPORTS = BASE_EXPORTS + MIXED_DELIVERIES
SUPPORTED_INVENTORIES = (set(LEGACY_EXPORTS), set(BASE_EXPORTS), set(EXPORTS))
MAX_SCOPE_EXPORT_ROWS = 4096
PUBLICATIONS_FILE = '.export_publications.json'
MAX_PUBLICATION_ROWS = 4096
MAX_BOOK_METADATA_BYTES = 4 * 1024**2
MAX_PUBLICATION_JOURNAL_BYTES = MAX_BOOK_METADATA_BYTES + 4 * ((MAX_BOOK_METADATA_BYTES + 2) // 3)
MAX_ARTIFACT_HASH_BYTES = 2 * 1024**3
MAX_ARTIFACT_HASH_BYTES = 2**63 - 1
_RECEIPT_FIELDS = frozenset(('book_token', 'scope_token', 'sha256', 'file_version'))
_HEX_DIGEST = re.compile(r'[0-9a-f]{64}')
_JSON_WHITESPACE = re.compile(r'[ \t\n\r]*')
_JSON_INTEGER = re.compile(r'-?[0-9]+')


def get_scope_export_inventory(root):
    root = Path(os.path.abspath(root))
    result = {}
    for name in EXPORTS:
        path = _ensure_plain_path(root/name, root)
        rows = {}
        if os.path.lexists(path):
            pending = [path]
            while pending:
                item = _ensure_plain_path(pending.pop(), root); info = item.lstat()
                if len(rows) >= MAX_SCOPE_EXPORT_ROWS:
                    raise ValueError('Scope export inventory exceeds its file-count limit.')
                if stat.S_ISREG(info.st_mode) and info.st_nlink == 1:
                    rows[item.relative_to(root).as_posix()] = list(_version(info))
                elif stat.S_ISDIR(info.st_mode):
                    rows[item.relative_to(root).as_posix()] = list(_version(info))
                    pending.extend(sorted(item.iterdir()))
                else:
                    raise ValueError('Scope exports contain an unsupported file or alias.')
        result[name] = rows
    return result


def _is_supported_scope_export_inventory(inventory):
    """Purely check the producer layouts and serialized row shape.

    Supported inventories are exactly the current or legacy producer
    key sets, with row maps whose path keys are strings and whose
    versions are lists of exactly five strict ints, matching
    audio_take_history._version. Bools and floats are not ints here.
    """
    if not isinstance(inventory, dict) or set(inventory) not in SUPPORTED_INVENTORIES:
        return False
    for rows in inventory.values():
        if not isinstance(rows, dict):
            return False
        if len(rows) > MAX_SCOPE_EXPORT_ROWS:
            return False
        for path, version in rows.items():
            if not isinstance(path, str):
                return False
            if not isinstance(version, list) or len(version) != 5 or any(type(item) is not int for item in version):
                return False
    return True


def is_scope_export_inventory_compatible(recorded, current):
    """Return whether a recorded inventory still fences current exports.

    Only an exact current inventory, or an exact earlier producer inventory
    whose recorded rows still match and whose newer exports are empty, is
    compatible. Any other key set is malformed for this purpose.
    """
    if not _is_supported_scope_export_inventory(recorded) or not _is_supported_scope_export_inventory(current):
        return False
    if set(current) != set(EXPORTS):
        return False
    keys = set(recorded)
    if keys == set(EXPORTS):
        return recorded == current
    if keys not in (set(LEGACY_EXPORTS), set(BASE_EXPORTS)):
        return False
    if any(recorded[name] != current[name] for name in keys):
        return False
    return all(current[name] == {} for name in EXPORTS if name not in keys)


def _read_export_fence(root):
    """Return (state, inventory) for a rooted book; inventory may be absent."""
    proof, raw = capture_scope_file(root, 'state.json',
        limit=MAX_BOOK_METADATA_BYTES, include_bytes=True)
    from book_placeholder import is_book_placeholder_bytes
    if not proof['exists'] or is_book_placeholder_bytes(raw):
        return {}, None
    state = json.loads(raw)
    if not isinstance(state, dict):
        raise ValueError('Invalid book state.')
    inventory = state.get('_source_scope_exports')
    if inventory is None:
        return state, None
    if not _is_supported_scope_export_inventory(inventory):
        raise ValueError('Scope export inventory needs inspection.')
    return state, inventory


def _is_hex_digest(value):
    return isinstance(value, str) and _HEX_DIGEST.fullmatch(value) is not None


def _is_valid_receipt(value):
    if not isinstance(value, dict) or set(value) != _RECEIPT_FIELDS:
        return False
    if not _is_hex_digest(value['book_token']) or not _is_hex_digest(value['sha256']):
        return False
    if value['scope_token'] is not None and not _is_hex_digest(value['scope_token']):
        return False
    version = value['file_version']
    if not isinstance(version, list) or len(version) != 5 or any(type(item) is not int for item in version):
        return False
    return True


def _receipt_key(root, path):
    root = Path(os.path.abspath(root)); path = Path(os.path.abspath(path))
    if not path.is_relative_to(root):
        raise ValueError('Export publication is outside the book root.')
    relative = path.relative_to(root).as_posix()
    if not relative or '..' in Path(relative).parts or Path(relative).is_absolute():
        raise ValueError('Invalid export publication path.')
    return root, path, relative


@contextmanager
def export_reader_admission(root):
    from book_state_transaction import ensure_book_state, is_book_state_admitted
    root = os.path.abspath(root)
    if is_book_state_admitted(root):
        yield
        return
    try:
        with ensure_book_state(root, timeout=0, recover=False):
            yield
    except TimeoutError as error:
        from fastapi import HTTPException
        raise HTTPException(status_code=409, detail='Book metadata is busy; retry the download.') from error


def read_book_state_bounded(root):
    proof, raw = capture_scope_file(root, 'state.json',
        limit=MAX_BOOK_METADATA_BYTES, include_bytes=True)
    if not proof['exists']:
        return {}
    state = json.loads(raw)
    if not isinstance(state, dict):
        raise ValueError('Invalid book state.')
    return state


def read_publication_receipts(root, *, include_bytes=False):
    root = Path(os.path.abspath(root))
    proof, raw = capture_scope_file(root, PUBLICATIONS_FILE,
        limit=MAX_BOOK_METADATA_BYTES, include_bytes=True)
    if not proof['exists']:
        return ({}, None) if include_bytes else {}
    records = _parse_publication_receipts(raw)
    return (records, raw) if include_bytes else records


def _parse_publication_receipts(raw):
    records = json.loads(raw) if raw is not None else {}
    if not isinstance(records, dict):
        raise ValueError('Export publication receipt ledger needs inspection.')
    for name, receipt in records.items():
        if not isinstance(name, str) or not _is_valid_receipt(receipt):
            raise ValueError('Export publication receipt ledger needs inspection.')
    return records


def get_compacted_publication_receipts(records, token, scope_token):
    retained = {name: receipt for name, receipt in records.items()
                if receipt.get('book_token') == token
                and receipt.get('scope_token') == scope_token}
    return serialize_publication_receipts(retained)


def sync_parent_directory(root, name=None):
    """Durably order POSIX directory mutations; Windows keeps native rename semantics."""
    if os.name != 'posix':
        return
    directory = Path(os.path.abspath(root)) if name is None else (
        Path(os.path.abspath(root)) / name).parent
    descriptor = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@contextmanager
def _publication_scratch(root, prefix):
    import tempfile
    from export_staging import create_export_staging

    staging = create_export_staging(root, 'publication')
    try:
        descriptor, filename = tempfile.mkstemp(prefix=prefix, dir=staging.path)
        with os.fdopen(descriptor, 'wb') as stream:
            yield stream, Path(filename)
    finally:
        staging.finish(remove=True)


def _write_publication_bytes(root, name, raw, prefix, before_replace=None):
    root = Path(os.path.abspath(root))
    with _publication_scratch(root, prefix) as (stream, staged):
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())
        stream.close()
        if before_replace is not None:
            before_replace(staged, raw)
        os.replace(staged, root / name)
        sync_parent_directory(root)


def write_publication_ledger(root, raw, *, before_replace=None):
    if len(raw) > MAX_BOOK_METADATA_BYTES:
        raise ValueError('Export publication receipt ledger exceeds its bound.')
    _write_publication_bytes(root, PUBLICATIONS_FILE, raw, '.export-ledger-', before_replace)


def write_atomic(root, name, raw, before_replace=None):
    _write_publication_bytes(root, name, raw, '.export-meta-', before_replace)


JOURNAL_PHASES = ('intent', 'committed')


class ExportSetChanged(ValueError):
    """A predecessor or staged member no longer matches the admitted set."""


class ExportCommitUncertain(OSError):
    """A visible commit decision still needs its directory sync."""


class ExportRecoveryPending(RuntimeError):
    """A failed publication left its intent journal and proposed ledger on disk.

    The terminal outcome must say recovery is pending, not report an ordinary
    cancellation: the guarded artifacts still belong to the unfinished
    transaction and the next book admission will replay the rollback.
    """


def _capture_proof(root, name, *, limit=MAX_ARTIFACT_HASH_BYTES, include_bytes=False,
                   allow_hardlinks=False):
    options = {'allow_hardlinks': True} if allow_hardlinks else {}
    proof, raw = capture_scope_file(root, name, limit=limit, include_bytes=include_bytes,
                                    **options)
    if not proof['exists']:
        return None, raw
    return {key: proof[key] for key in ('sha256', 'file_version')}, raw


def _is_valid_proof(proof, version_length):
    return (proof is None or (isinstance(proof, dict)
            and set(proof) == {'sha256', 'file_version'}
            and _is_hex_digest(proof['sha256'])
            and isinstance(proof['file_version'], list)
            and len(proof['file_version']) == version_length
            and all(type(value) is int for value in proof['file_version'])))


def _match_proof(actual, recorded):
    if actual is None or recorded is None:
        return actual is recorded
    version = recorded['file_version']
    return (actual['sha256'] == recorded['sha256']
            and actual['file_version'][:len(version)] == version)


def _get_version_after_rename(proof):
    return {**proof, 'file_version': proof['file_version'][:4]}


def _get_prior_ledger_raw(ledger):
    raw = ledger['original']
    if raw is not None:
        if not isinstance(raw, str) or len(raw) > 4 * ((MAX_BOOK_METADATA_BYTES + 2) // 3):
            raise ValueError('Export journal has invalid original ledger bytes.')
        raw = base64.b64decode(raw, validate=True)
        if len(raw) > MAX_BOOK_METADATA_BYTES:
            raise ValueError('Export journal original ledger exceeds its bound.')
    return raw


def _validate_ledger_journal(ledger):
    if (not isinstance(ledger, dict) or set(ledger) != {'original', 'original_sha256', 'writes'}
            or not isinstance(ledger['writes'], list) or not 1 <= len(ledger['writes']) <= 2
            or not _is_valid_proof(ledger['writes'][0], 5)
            or (len(ledger['writes']) == 2 and not _is_valid_proof(ledger['writes'][1], 4))):
        raise ValueError('Invalid export ledger rollback evidence.')
    raw = _get_prior_ledger_raw(ledger)
    digest = hashlib.sha256(raw).hexdigest() if raw is not None else None
    if (digest != ledger['original_sha256']
            or len(ledger['writes']) == 1 and (ledger['writes'][0] or {}).get('sha256') != digest):
        raise ValueError('Export journal original ledger changed.')
    return _parse_publication_receipts(raw)


def _get_ledger_intent(root):
    proof, raw = _capture_proof(root, PUBLICATIONS_FILE, limit=MAX_BOOK_METADATA_BYTES,
                               include_bytes=True)
    records = _parse_publication_receipts(raw)
    return {'original': base64.b64encode(raw).decode('ascii') if raw is not None else None,
            'original_sha256': proof['sha256'] if proof is not None else None,
            'writes': [proof]}, records


def _get_owned_ledger(root, ledger):
    current, raw = _capture_proof(root, PUBLICATIONS_FILE, limit=MAX_BOOK_METADATA_BYTES,
                                 include_bytes=True)
    if not any(_match_proof(current, known) for known in ledger['writes']):
        raise ValueError('Export receipt ledger changed outside publication.')
    return current, raw


def _require_publication_version(root, name, proof, *, allow_hardlinks=False):
    path = _ensure_plain_path(root / name, root)
    try:
        info = path.lstat()
    except FileNotFoundError:
        if proof is None:
            return
        raise ValueError('Export file disappeared during publication.') from None
    if (proof is None or not stat.S_ISREG(info.st_mode) or info.st_nlink < 1
            or (not allow_hardlinks and info.st_nlink != 1)
            or list(_version(info)) != proof['file_version']):
        raise ValueError('Export file changed during publication.')
    return info


def _write_export_journal(root, name, record, before_replace=None):
    payload = serialize_publication_receipts(record)
    if len(payload) > MAX_PUBLICATION_JOURNAL_BYTES:
        raise ValueError('Export publication journal exceeds its bound.')
    write_atomic(root, name, payload, before_replace=before_replace)


def _write_export_commit(root, name, record, read_journal, confirm, before_replace=None):
    try:
        _write_export_journal(root, name, record, before_replace=before_replace)
    except OSError:
        try:
            observed = read_journal(root)
        except (OSError, ValueError) as error:
            raise ExportCommitUncertain('Export commit is pending recovery.') from error
        if observed != record:
            if observed is not None and observed['phase'] == 'committed':
                raise ExportCommitUncertain('Export commit is pending recovery.')
            raise
        # The name is visible. Retry only the check and sync while the same
        # book lease and the original recovery evidence are still held.
        try:
            confirm(root, record)
        except (OSError, ValueError) as error:
            raise ExportCommitUncertain('Export commit is pending recovery.') from error


def _write_owned_ledger(root, raw, name, record):
    ledger = record['ledger']
    prior, current_raw = _get_owned_ledger(root, ledger)
    if current_raw == raw:
        return

    def authorize(staged):
        next_record = {**record, 'ledger': {**ledger, 'writes': [prior, staged]}}
        _write_export_journal(root, name, next_record)
        _require_publication_version(root, PUBLICATIONS_FILE, prior)

    if raw is None:
        authorize(None)
        (root / PUBLICATIONS_FILE).unlink()
        return

    def before_replace(staged, _raw):
        relative = _receipt_key(root, staged)[2]
        proof, _ = _capture_proof(root, relative, limit=MAX_BOOK_METADATA_BYTES)
        if proof is None or proof['sha256'] != hashlib.sha256(raw).hexdigest():
            raise ValueError('Staged export ledger changed before replacement.')
        authorize(_get_version_after_rename(proof))
        _require_publication_version(root, relative, proof)

    write_publication_ledger(root, raw, before_replace=before_replace)


def _json_member_spans(text, start):
    """Yield value offsets within an already parsed JSON object."""
    decoder = json.JSONDecoder()
    offset = _JSON_WHITESPACE.match(text, start + 1).end()
    while text[offset] != '}':
        key, key_end = decoder.raw_decode(text, offset)
        colon = _JSON_WHITESPACE.match(text, key_end).end()
        value_start = _JSON_WHITESPACE.match(text, colon + 1).end()
        _, value_end = decoder.raw_decode(text, value_start)
        yield key, value_start, value_end
        separator = _JSON_WHITESPACE.match(text, value_end).end()
        if text[separator] == '}':
            return
        offset = _JSON_WHITESPACE.match(text, separator + 1).end()


def _rewrite_ledger_versions(raw, versions):
    """Update only the numeric version tokens of rows whose files moved."""
    encoding = json.detect_encoding(raw)
    bom = b''
    if encoding in ('utf-16', 'utf-32'):
        size = 2 if encoding == 'utf-16' else 4
        bom, raw = raw[:size], raw[size:]
        encoding += '-le' if bom.startswith(b'\xff') else '-be'
    text = raw.decode(encoding)
    spans = {}
    for key, start, _end in _json_member_spans(text, _JSON_WHITESPACE.match(text).end()):
        if key in versions:
            spans[key] = {field: (begin, end)
                          for field, begin, end in _json_member_spans(text, start)}['file_version']
    cursor = 0
    output = []
    for key, (start, end) in sorted(spans.items(), key=lambda item: item[1]):
        numbers = iter(versions[key])
        output.extend((text[cursor:start],
                       _JSON_INTEGER.sub(lambda _match: str(next(numbers)), text[start:end])))
        cursor = end
    output.append(text[cursor:])
    return bom + ''.join(output).encode(encoding)


def _restore_owned_ledger(root, name, record, restored):
    raw = _get_prior_ledger_raw(record['ledger'])
    records = _parse_publication_receipts(raw)
    changed = {}
    for key, proof in restored.items():
        prior = records[key]
        if proof['sha256'] != prior['sha256']:
            raise ValueError('Restored export disagrees with its prior receipt.')
        _require_publication_version(root, key, proof,
            allow_hardlinks=name == PUBLICATION_SET_JOURNAL and _allows_linked_predecessor(key))
        if proof['file_version'] != prior['file_version']:
            changed[key] = proof['file_version']
    if changed:
        raw = _rewrite_ledger_versions(raw, changed)
    _write_owned_ledger(root, raw, name, record)


def read_publication_journal(root):
    root = Path(os.path.abspath(root))
    proof, raw = capture_scope_file(root, PUBLICATION_JOURNAL,
        limit=MAX_PUBLICATION_JOURNAL_BYTES, include_bytes=True)
    if not proof['exists']:
        return None
    record = json.loads(raw)
    expected = {'version', 'phase', 'target', 'backup', 'staged',
                'staged_sha256', 'prior', 'prior_existed', 'prior_sha256',
                'prior_eligible'}
    if isinstance(record, dict) and record.get('version') == 3:
        expected |= {'ledger', 'before', 'new', 'copy'}
    if (not isinstance(record, dict) or set(record) != expected
            or record['version'] not in (2, 3) or record['phase'] not in JOURNAL_PHASES):
        raise ValueError('Export publication journal needs inspection.')
    target_name = record['target']
    if (not isinstance(target_name, str) or not target_name
            or Path(target_name).is_absolute() or '..' in Path(target_name).parts
            or Path(target_name).as_posix() != target_name):
        raise ValueError('Export publication journal needs inspection.')
    if record['backup']:
        name = record['backup']
        if name != Path(name).name or not name.startswith('.export-previous-'):
            raise ValueError('Export publication journal needs inspection.')
        if Path(name).as_posix() != name:
            raise ValueError('Export publication journal needs inspection.')
    if (not _is_hex_digest(record['staged_sha256'])
            or type(record['prior_existed']) is not bool
            or (record['prior_sha256'] is not None
                and not _is_hex_digest(record['prior_sha256']))
            or (record['prior'] is not None
                and not _is_valid_receipt(record['prior']))
            or type(record['prior_eligible']) is not bool):
        raise ValueError('Export publication journal needs inspection.')
    if record['version'] == 3:
        records = _validate_ledger_journal(record['ledger'])
        if (records.get(target_name) != record['prior']
                or not _is_valid_proof(record['before'], 5)
                or not _is_valid_proof(record['new'], 4) or record['new'] is None
                or record['new']['sha256'] != record['staged_sha256']
                or not _is_valid_proof(record['copy'], 4)
                or (record['before'] is not None) != record['prior_existed']
                or (record['before'] or {}).get('sha256') != record['prior_sha256']
                or (record['copy'] is not None
                    and record['copy']['sha256'] != record['prior_sha256'])
                or record['prior_eligible'] and (record['prior'] is None
                    or record['before'] != {key: record['prior'][key]
                                             for key in ('sha256', 'file_version')})):
            raise ValueError('Export publication target evidence needs inspection.')
    return record


def bounded_artifact_proof(root, name):
    root = Path(os.path.abspath(root))
    path = _ensure_plain_path(root / name, root)
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_ARTIFACT_HASH_BYTES:
        raise ValueError('Export artifact is not a bounded regular file.')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
        version = list(_version(os.fstat(stream.fileno())))
    if list(_version(path.lstat())) != version:
        raise ValueError('Export artifact changed while recording its proof.')
    return {'sha256': digest.hexdigest(), 'file_version': version}


def _allows_linked_predecessor(target):
    # Replacing this supported local EPUB name must preserve sibling links.
    # The replacement, receipt ledger and journal remain single-link files.
    return target == 'readaloud.epub'


def set_journal_members(root, targets, backups, *, staged=None, receipts=None, sources=None):
    from book_state_transaction import _capture_target_parent_identities_path

    root = Path(os.path.abspath(root))
    if receipts is None:
        receipts = read_publication_receipts(root)
    members = []
    for index, (target, backup) in enumerate(zip(targets, backups, strict=True)):
        name = Path(os.path.relpath(target, root)).as_posix()
        path = root / name
        existed = path.is_file()
        proof = (_capture_proof(root, name, allow_hardlinks=_allows_linked_predecessor(name))[0]
                 if staged is not None else
                 bounded_artifact_proof(root, name) if existed else None)
        prior = receipts.get(name)
        eligible = (prior is not None and proof is not None and proof['sha256'] == prior['sha256']
                    and not is_scope_export_historical(root, path))
        member = {'target': name,
                        'backup': Path(os.path.relpath(backup, root)).as_posix(),
                        'existed': existed,
                        'prior': prior,
                        'prior_eligible': eligible,
                        'prior_sha256': proof['sha256'] if proof else None}
        if staged is not None:
            member['before'] = proof
            report = staged[index]
            member['new'] = _get_version_after_rename(
                {key: report[key] for key in ('sha256', 'file_version')})
            if ((proof is not None) != existed or not _is_valid_proof(member['new'], 4)
                    or eligible and proof != {key: prior[key] for key in ('sha256', 'file_version')}):
                raise ValueError('Export set member evidence changed before publication.')
        if sources is not None:
            source = _receipt_key(root, sources[index])[2]
            member['source'] = source
            member['parents'] = {name: _capture_target_parent_identities_path(root, name)
                                 for name in (member['target'], member['backup'], source)}
        members.append(member)
    return members


def write_set_journal(root, targets, backups, prior_ledger=None, committed=False, *,
                      staged=None, sources=None, commit_check=None):
    root = Path(os.path.abspath(root))
    if committed:
        record = read_set_journal(root)
        if record is None:
            return
        _sync_export_target_parents(root, (member['target'] for member in record['members']))
        if record['version'] == 3:
            _check_export_set_commit(root, record, verify_backups=True)
        # Last safe boundary before the durable intent->committed decision:
        # after parent sync, ledger/backup verification and the journal's own
        # staged write + fsync, immediately before the commit rename (R1).
        if commit_check is not None:
            commit_check()
        record = {**record, 'phase': 'committed'}
        _write_export_commit(root, PUBLICATION_SET_JOURNAL, record, read_set_journal,
                             _confirm_export_set_commit,
                             before_replace=(None if commit_check is None else
                                             (lambda _staged, _raw: commit_check())))
        # Readers fence guarded journals until retirement. A cleanup problem
        # after the durable decision may keep the stage, but cannot undo it.
        return recover_set_journal(root, cleanup_stage=False)
    else:
        ledger, receipts = _get_ledger_intent(root)
        members = set_journal_members(root, targets, backups, staged=staged, receipts=receipts,
                                       sources=sources)
        if len(serialize_publication_receipts(members)) > MAX_BOOK_METADATA_BYTES:
            raise ValueError('Export set publication journal exceeds its bound.')
        record = {'version': 3, 'phase': 'intent', 'members': members, 'ledger': ledger}
        _write_export_journal(root, PUBLICATION_SET_JOURNAL, record)


def read_set_journal(root):
    root = Path(os.path.abspath(root))
    proof, raw = capture_scope_file(root, PUBLICATION_SET_JOURNAL,
        limit=MAX_PUBLICATION_JOURNAL_BYTES, include_bytes=True)
    if not proof['exists']:
        return None
    record = json.loads(raw)
    if (not isinstance(record, dict) or record.get('version') not in (2, 3)
            or record.get('phase') not in (*JOURNAL_PHASES, 'settled')
            or not isinstance(record.get('members'), list)
            or len(record['members']) > MAX_PUBLICATION_ROWS):
        raise ValueError('Export set publication journal needs inspection.')
    records = _validate_ledger_journal(record.get('ledger')) if record['version'] == 3 else None
    for member in record['members']:
        fields = {'target', 'backup', 'existed', 'prior', 'prior_eligible', 'prior_sha256'}
        if record['version'] == 3 and isinstance(member, dict) and 'before' in member:
            fields |= {'before', 'new'}
            if 'source' in member:
                fields |= {'source', 'parents'}
        if (not isinstance(member, dict)
                or set(member) != fields
                or type(member['existed']) is not bool
                or type(member['prior_eligible']) is not bool):
            raise ValueError('Export set publication journal needs inspection.')
        for name in ('target', 'backup', 'source') if 'source' in member else ('target', 'backup'):
            value = member[name]
            if (not isinstance(value, str) or not value or Path(value).is_absolute()
                    or '\\' in value or '\x00' in value or Path(value).anchor
                    or not Path(value).parts
                    or '..' in Path(value).parts or Path(value).as_posix() != value):
                raise ValueError('Export set publication journal needs inspection.')
        if (member['prior'] is not None and not _is_valid_receipt(member['prior'])
                or (member['prior_sha256'] is not None
                    and not _is_hex_digest(member['prior_sha256']))):
            raise ValueError('Export set publication journal needs inspection.')
        if records is not None and records.get(member['target']) != member['prior']:
            raise ValueError('Export set prior receipt disagrees with its ledger snapshot.')
        if 'before' in member and (not _is_valid_proof(member['before'], 5)
                or not _is_valid_proof(member['new'], 4) or member['new'] is None
                or (member['before'] is not None) != member['existed']
                or (member['before'] or {}).get('sha256') != member['prior_sha256']
                or member['prior_eligible'] and (member['prior'] is None or
                    member['before'] != {key: member['prior'][key]
                                         for key in ('sha256', 'file_version')})):
            raise ValueError('Export set target evidence needs inspection.')
        if 'source' in member:
            names = {member[key] for key in ('target', 'backup', 'source')}
            parents = member['parents']
            if (len(names) != 3 or not isinstance(parents, dict) or set(parents) != names
                    or any(not isinstance(chain, list) or len(chain) != len(Path(name).parts)
                           or any(not isinstance(pair, list) or len(pair) != 2
                                  or any(type(value) is not int or value < 0 for value in pair)
                                  for pair in chain)
                           for name, chain in parents.items())):
                raise ValueError('Export set parent evidence needs inspection.')
    if record['phase'] == 'settled' and (
            record['version'] != 3 or _get_registered_set_stage(record) is None):
        raise ValueError('Settled export set has no registered staging evidence.')
    return record


def _get_registered_set_stage(record):
    from export_staging_retirement import DIRECTORY

    members = record['members']
    if not members or 'source' not in members[0]:
        return None
    stage = Path(members[0]['source']).parent
    if len(stage.parts) < 3 or stage.parent.parent.name != DIRECTORY:
        return None
    if stage.name != 'stage' or re.fullmatch(r'[0-9a-f]{32}', stage.parent.name) is None:
        raise ValueError('Invalid export set staging location.')
    expected = members[0]['parents'][members[0]['source']]
    for member in members:
        if 'source' not in member:
            raise ValueError('Export set staging evidence disagrees.')
        source, backup, target = (Path(member[key]) for key in ('source', 'backup', 'target'))
        if (source.parent != stage or backup.parent != stage / 'old'
                or target.parent != stage.parents[2]
                or member['parents'][member['source']] != expected
                or member['parents'][member['backup']][:-1] != expected
                or member['parents'][member['target']] != expected[:-3]):
            raise ValueError('Export set staging evidence disagrees.')
    return stage, expected


def is_export_set_pending(record):
    return (record is not None and (record['phase'] != 'committed'
            or any('source' in member for member in record['members'])))


def _check_export_set_commit(root, record, *, verify_backups=False):
    _owned, raw = _get_owned_ledger(root, record['ledger'])
    receipts = _parse_publication_receipts(raw)
    for member in record['members']:
        if 'source' in member:
            _require_set_parents(root, member, (member['target'],))
            receipt = receipts.get(member['target'])
            if not receipt or not _match_proof(receipt, member['new']):
                raise ExportSetChanged('Export set receipt changed before commit.')
            _require_set_version(root, member['target'], receipt)
            if verify_backups and member['before'] is not None:
                _require_set_parents(root, member, (member['backup'],))
                saved, _ = _capture_proof(root, member['backup'],
                    allow_hardlinks=_allows_linked_predecessor(member['target']))
                if not _match_proof(saved, _get_version_after_rename(member['before'])):
                    raise ExportSetChanged('Export set backup changed before commit.')


def _confirm_export_set_commit(root, record):
    if record['version'] == 3:
        _check_export_set_commit(root, record)
    sync_parent_directory(root)


def _require_set_version(root, name, proof, *, allow_hardlinks=False):
    try:
        return _require_publication_version(root, name, proof, allow_hardlinks=allow_hardlinks)
    except ValueError as error:
        raise ExportSetChanged('Export set member changed during publication.') from error


def _require_set_parents(root, member, names=None):
    from book_state_transaction import _capture_target_parent_identities_path

    parents = member['parents']
    if any(_capture_target_parent_identities_path(root, name) != parents[name]
           for name in (parents if names is None else names)):
        raise ExportSetChanged('Export set parent changed during publication.')


_FORWARD_MOVE_VALIDATOR = contextvars.ContextVar(
    'export_forward_move_validator', default=None)


@contextmanager
def _forward_export_move():
    """Authorize only forward publication moves for the enclosed call.

    Recovery performs its own independently authorized moves and must not be
    refused by a stop that only fences new publication. Keeping this as a
    context value leaves ``_move_guarded_set_file``'s call signature stable
    for existing callers and fixtures.
    """
    from audio_generation_owner import ensure_audio_generation_commit_active
    token = _FORWARD_MOVE_VALIDATOR.set(ensure_audio_generation_commit_active)
    try:
        yield
    finally:
        _FORWARD_MOVE_VALIDATOR.reset(token)


def _move_guarded_set_file(root, member, source, destination, proof):
    from book_state_transaction import _move
    linked = (_allows_linked_predecessor(member['target'])
              and (source == member['backup'] or destination == member['backup']))
    info = _require_set_version(root, source, proof, allow_hardlinks=linked)
    source_identity = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns,
                       info.st_mode, info.st_nlink)
    parents = member['parents']
    try:
        _move(root / source, root / destination, exclusive=True, anchor=root,
              anchor_identity=tuple(parents[source][0]),
              source_parent_identity=parents[source], destination_parent_identity=parents[destination],
              expected_source_identity=source_identity,
              before_move=_FORWARD_MOVE_VALIDATOR.get())
    except (FileExistsError, ValueError) as error:
        raise ExportSetChanged('Export set member changed during its move.') from error


def publish_export_set_member(root, member, report):
    """Move each admitted object exclusively, then verify what actually moved."""
    from audio_generation_owner import ensure_audio_generation_commit_active
    # Revalidate exact ownership at the publication boundary: the set journal
    # already contains intent, but the captured owner is no longer serialized
    # across its yield, so a cancel or claim replacement here must refuse the
    # move (including a reused/manifest-only set) before any target changes.
    ensure_audio_generation_commit_active()
    root = Path(os.path.abspath(root))
    target, backup, source = (member[key] for key in ('target', 'backup', 'source'))
    before = member['before']
    _require_set_version(root, target, before,
                         allow_hardlinks=_allows_linked_predecessor(target))
    if before is not None:
        # Identity above is prepared; recheck exact ownership immediately
        # before the first forward mutation. The restore move inside the
        # failure branch below is recovery and must remain authorized (R13).
        from audio_generation_owner import ensure_audio_generation_commit_active
        ensure_audio_generation_commit_active()
        # The lowest exclusive move repeats this after its own blocking
        # descriptor/parent/identity preparation (F03).
        with _forward_export_move():
            _move_guarded_set_file(root, member, target, backup, before)
        saved = {'file_version': list(_version((root / backup).lstat()))}
        if saved['file_version'][:4] != before['file_version'][:4]:
            # POSIX no-overwrite protects the destination, but cannot bind
            # the source inode atomically. Keep a substituted object intact.
            if not os.path.lexists(root / target):
                _move_guarded_set_file(root, member, backup, target, saved)
            raise ExportSetChanged('Export set predecessor changed during backup.')
    from audio_generation_owner import ensure_audio_generation_commit_active
    ensure_audio_generation_commit_active()
    with _forward_export_move():
        _move_guarded_set_file(root, member, source, target, report)
    version = list(_version((root / target).lstat()))
    if version[:4] != member['new']['file_version']:
        raise ExportSetChanged('Export set output changed during publication.')
    _require_set_version(root, target, {'file_version': version})
    return {**report, 'file_version': version}


def read_prior_export_set_member(root, member):
    """Read the bounded prior member from its original or journaled backup."""
    if not member['existed']:
        return None
    for name in (member['backup'], member['target']):
        proof, raw = _capture_proof(root, name, limit=MAX_BOOK_METADATA_BYTES,
                                   include_bytes=True)
        if proof is None or proof['sha256'] != member['prior_sha256']:
            continue
        if 'before' not in member or _match_proof(proof, _get_version_after_rename(member['before'])):
            return raw
    raise ValueError('Export set prior member cannot be verified.')


def _get_export_rollback_plan(root, target, backup, before, new, restored, *,
                              allow_foreign=False, allow_hardlinks=False):
    proof, _ = _capture_proof(root, target, allow_hardlinks=allow_hardlinks)
    saved, _ = _capture_proof(root, backup, allow_hardlinks=allow_hardlinks)
    if saved is not None and not _match_proof(saved, restored):
        raise ValueError('Export rollback backup changed outside publication.')
    if _match_proof(proof, before) or restored is not None and _match_proof(proof, restored):
        return proof, saved, 'keep'
    if proof is not None and not _match_proof(proof, new):
        if allow_foreign and saved is None:
            return proof, saved, 'independent'
        raise ValueError('Export target changed outside publication.')
    if before is not None:
        if saved is None:
            raise ValueError('Export rollback backup is missing.')
        return proof, saved, 'restore'
    return proof, saved, 'remove'


def _apply_export_rollback_plan(root, target, backup, plan):
    proof, saved, action = plan
    _require_publication_version(root, target, proof)
    if action == 'restore':
        _require_publication_version(root, backup, saved)
        os.replace(root / backup, root / target)
        proof, _ = _capture_proof(root, target)
        if not _match_proof(proof, _get_version_after_rename(saved)):
            raise ValueError('Restored export changed during rollback.')
    elif action == 'remove':
        (root / target).unlink()
        proof = None
    return proof


def _apply_guarded_set_rollback(root, member, plan):
    current, saved, action = plan
    target, backup, source = (member[key] for key in ('target', 'backup', 'source'))
    if action == 'independent':
        return None
    if action == 'keep':
        _require_set_version(root, target, current,
                             allow_hardlinks=_allows_linked_predecessor(target))
        return current
    if current is not None:
        _move_guarded_set_file(root, member, target, source, current)
        parked, _ = _capture_proof(root, source)
        if not _match_proof(parked, _get_version_after_rename(current)):
            if parked is not None and not os.path.lexists(root / target):
                _move_guarded_set_file(root, member, source, target, parked)
            raise ExportSetChanged('Export set target changed during rollback.')
    if action == 'remove':
        return None
    _move_guarded_set_file(root, member, backup, target, saved)
    proof, _ = _capture_proof(root, target, allow_hardlinks=_allows_linked_predecessor(target))
    if not _match_proof(proof, _get_version_after_rename(saved)):
        raise ExportSetChanged('Export set backup changed during rollback.')
    return proof


def _recover_owned_set(root, record, *, cleanup_stage):
    _get_owned_ledger(root, record['ledger'])
    members = record['members']
    plans = {}
    blocked = set()
    for member in members:
        if 'before' in member:
            before = member['before']
            target = member['target']
            try:
                guarded = 'source' in member
                if guarded:
                    _require_set_parents(root, member)
                plans[target] = _get_export_rollback_plan(
                    root, target, member['backup'], before, member['new'],
                    _get_version_after_rename(before) if before is not None else None,
                    allow_foreign=guarded,
                    allow_hardlinks=guarded and _allows_linked_predecessor(target))
            except ValueError:
                if not guarded:
                    raise
                # Roll back proven members without touching the conflicting
                # name or releasing the set-wide recovery fence.
                blocked.add(target)
    restored = {}
    for member in members:
        target, backup = member['target'], member['backup']
        if target in blocked:
            continue
        if target in plans:
            if 'source' in member:
                try:
                    proof = _apply_guarded_set_rollback(root, member, plans[target])
                except ValueError:
                    blocked.add(target)
                    continue
            else:
                proof = _apply_export_rollback_plan(root, target, backup, plans[target])
        else:
            # Compatibility for intents created without a staged version.
            backup_proof, _ = _capture_proof(root, backup)
            proof, _ = _capture_proof(root, target)
            if backup_proof is not None and backup_proof['sha256'] == member['prior_sha256']:
                os.replace(root / backup, root / target)
                proof, _ = _capture_proof(root, target)
            elif not member['existed'] and proof is not None:
                raise ValueError('Export set cannot prove the new target belongs to this intent.')
            if (proof or {}).get('sha256') != member['prior_sha256']:
                raise ValueError('Export set rollback target changed.')
        if member['prior_eligible'] and member['prior'] is not None and proof is not None:
            restored[target] = proof
    _restore_owned_ledger(root, PUBLICATION_SET_JOURNAL, record, restored)
    if blocked:
        return False
    retire_set_journal(root, (member['target'] for member in members),
                       cleanup_stage=cleanup_stage)
    return True


def recover_set_journal(root, *, raise_rollback_error=False, cleanup_stage=True):
    root = Path(os.path.abspath(root))
    record = read_set_journal(root)
    if record is None:
        return False
    if record['phase'] == 'settled':
        try:
            retire_set_journal(root, cleanup_stage=True)
        except (OSError, ValueError):
            return False
        return True
    if record['phase'] == 'committed':
        try:
            _confirm_export_set_commit(root, record)
            for member in record['members']:
                if 'source' in member and os.path.lexists(root / member['backup']):
                    _require_set_parents(root, member, (member['backup'],))
                _remove_publication_backup(root, member['backup'], member['prior_sha256'],
                                           member.get('before'),
                                           allow_hardlinks=_allows_linked_predecessor(member['target']))
            retire_set_journal(root, cleanup_stage=cleanup_stage)
        except (OSError, ValueError):
            return False
        return True
    if record['version'] == 3:
        try:
            return _recover_owned_set(root, record, cleanup_stage=cleanup_stage)
        except OSError:
            if raise_rollback_error:
                raise
            return False
    records = read_publication_receipts(root)
    current_records = dict(records)
    for member in record['members']:
        if member['prior'] is None:
            records.pop(member['target'], None)
        else:
            records[member['target']] = member['prior']
    verified = True
    if records != current_records:
        try:
            write_publication_ledger(root, serialize_publication_receipts(records))
        except OSError:
            verified = False
    for member in record['members']:
        target, backup = root / member['target'], root / member['backup']
        try:
            _ensure_plain_path(target, root)
            _ensure_plain_path(backup, root)
        except ValueError:
            verified = False
            break
        if backup.is_file():
            os.replace(backup, target)
        proof, _raw = capture_scope_file(root, member['target'],
            limit=MAX_ARTIFACT_HASH_BYTES) if target.is_file() else (None, None)
        if member['existed'] is False:
            if proof is not None:
                target.unlink(missing_ok=True)
                proof = None
        elif (proof is None or proof['sha256'] != member['prior_sha256']):
            verified = False
            continue
        if (member['prior_eligible'] and proof is not None
                and member['prior'] is not None):
            try:
                rebind_publication_version(root, member['target'], proof)
            except OSError:
                verified = False
    if verified:
        try:
            retire_set_journal(root, (member['target'] for member in record['members']))
        except OSError:
            return False
    return verified


def recover_failed_export_set(root, staging_path):
    """Keep a publication failure distinct from a failed recovery attempt."""
    try:
        return recover_set_journal(root, raise_rollback_error=True, cleanup_stage=False)
    except (OSError, ValueError) as rollback_error:
        raise RuntimeError(f'Export rollback failed; recovery files kept at {staging_path}: '
                           f'{rollback_error}') from rollback_error


def get_export_set_cleanup_pending_detail(staging_path):
    return f'Export committed; pending recovery. Recovery files kept at {staging_path}.'


def get_export_set_rollback_pending_detail(staging_path):
    """Phase-specific outcome when an interrupted undo did not finish.

    The intent journal, backups and external bytes are preserved; the next
    book admission attempts recovery. This is deliberately distinct from both
    an ordinary cancellation (which finished its rollback) and a committed
    export whose cleanup is pending.
    """
    return (f'Export rollback is pending recovery; the next book admission '
            f'will attempt it. Recovery files kept at {staging_path}.')


def _sync_export_target_parents(root, targets):
    parents = {}
    for target in targets:
        parents.setdefault(Path(target).parent, target)
    for parent, target in parents.items():
        if parent != Path('.'):
            sync_parent_directory(root, target)


def retire_set_journal(root, restored_targets=(), *, cleanup_stage=False):
    # Retry this even when a prior attempt moved the backup away before its
    # directory sync. A verified target alone does not prove durable rename.
    _sync_export_target_parents(root, restored_targets)
    sync_parent_directory(root)
    if cleanup_stage:
        record = read_set_journal(root)
        registered = _get_registered_set_stage(record) if record is not None else None
        if registered is not None:
            from book_state_transaction import _capture_target_parent_identities_path
            from export_staging import retire_settled_export_staging

            stage, expected = registered
            # The registry remains after success. A missing slot or stage can
            # be a prior retirement step, but a replaced ancestor is foreign.
            chain = _capture_target_parent_identities_path(root, (stage / 'owner').as_posix())
            if chain[:-2] != expected[:-2]:
                raise OSError('Export staging registry changed during settlement.')
            if record['phase'] != 'settled':
                # Media and ledger are settled. Retry now needs only the stage
                # identities, even after partially removing the stage or slot.
                _write_export_journal(root, PUBLICATION_SET_JOURNAL,
                                      {**record, 'phase': 'settled'})
            retire_settled_export_staging(Path(root) / stage, expected[-4:])
    (Path(root) / PUBLICATION_SET_JOURNAL).unlink(missing_ok=True)
    sync_parent_directory(root)


def write_publication_journal(root, record):
    _write_export_journal(root, PUBLICATION_JOURNAL, record)


def _remove_publication_backup(root, name, sha256, expected=None, *, allow_hardlinks=False):
    if name is None:
        return
    proof, _ = _capture_proof(root, name, allow_hardlinks=allow_hardlinks)
    if proof is None:
        return
    if (proof['sha256'] != sha256 or expected is not None
            and not _match_proof(proof, _get_version_after_rename(expected))):
        raise ValueError('Export backup changed outside publication.')
    _require_publication_version(root, name, proof, allow_hardlinks=allow_hardlinks)
    (root / name).unlink()


def _confirm_single_export_commit(root, record):
    if record['version'] == 3:
        _, raw = _get_owned_ledger(root, record['ledger'])
        receipt = _parse_publication_receipts(raw).get(record['target'])
        if receipt is None or not _match_proof(receipt, record['new']):
            raise ValueError('Published export receipt changed after commit.')
        _require_publication_version(root, record['target'], receipt)
    sync_parent_directory(root)


def recover_publication_journal(root):
    root = Path(os.path.abspath(root))
    record = read_publication_journal(root)
    if record is None:
        return False
    target_name = record['target']
    target = root / target_name
    backup = root / record['backup'] if record['backup'] else None
    prior = record['prior']
    phase = record['phase']
    if phase == 'committed':
        try:
            _confirm_single_export_commit(root, record)
            _remove_publication_backup(root, record['backup'], record['prior_sha256'],
                                       record.get('copy'))
            (root / PUBLICATION_JOURNAL).unlink(missing_ok=True)
            sync_parent_directory(root)
        except OSError:
            return False
        return True
    if record['version'] == 3:
        try:
            _get_owned_ledger(root, record['ledger'])
            plan = _get_export_rollback_plan(root, target_name, record['backup'],
                                             record['before'], record['new'], record['copy'])
            proof = _apply_export_rollback_plan(root, target_name, record['backup'], plan)
            restored = {target_name: proof} if record['prior_eligible'] else {}
            _restore_owned_ledger(root, PUBLICATION_JOURNAL, record, restored)
            # A copy still present was proven before any rollback mutation.
            if plan[2] == 'keep' and plan[1] is not None:
                _require_publication_version(root, record['backup'], plan[1])
                backup.unlink()
            sync_parent_directory(root, target_name)
            sync_parent_directory(root)
            (root / PUBLICATION_JOURNAL).unlink()
            sync_parent_directory(root)
        except OSError:
            return False
        return True
    target_proof = None
    if target.is_file():
        target_proof, _raw = capture_scope_file(root, target_name,
            limit=MAX_ARTIFACT_HASH_BYTES)
    current = read_publication_receipts(root).get(target_name)
    ledger_changed = current != prior
    new_ledger = ledger_changed and current is not None
    if backup is not None and backup.is_file():
        if new_ledger or target is None or not target.is_file():
            os.replace(backup, target)
        else:
            backup.unlink(missing_ok=True)
    elif (not record['prior_existed'] and target.is_file()
            and ledger_changed and target_proof is not None
            and target_proof['sha256'] == record['staged_sha256']):
        target.unlink(missing_ok=True)
    verified = True
    try:
        records = read_publication_receipts(root)
        current_records = dict(records)
        if prior is None:
            records.pop(target_name, None)
        else:
            records[target_name] = prior
        if records != current_records:
            write_publication_ledger(root, serialize_publication_receipts(records))
    except OSError:
        verified = False
    if target.is_file():
        proof, _raw = capture_scope_file(root, target_name,
            limit=MAX_ARTIFACT_HASH_BYTES)
        if (record['prior_eligible'] and prior is not None
                and proof['sha256'] == prior['sha256']):
            try:
                if not rebind_publication_version(root, target_name, proof):
                    verified = False
            except OSError:
                verified = False
    if verified:
        try:
            _remove_publication_backup(root, record['backup'], record['prior_sha256'])
            sync_parent_directory(root)
            (root / PUBLICATION_JOURNAL).unlink(missing_ok=True)
            sync_parent_directory(root)
        except OSError:
            return False
        return True
    return False

def serialize_publication_receipts(records):
    return json.dumps(records, ensure_ascii=False, indent=2).encode('utf-8')


def get_saved_book_rollback_receipts(root, rows, target_versions):
    """Capture only current receipts for individual exports this load removes."""
    state, _ = _read_export_fence(root)
    records = read_publication_receipts(root)
    eligible = {}
    for row, version in zip(rows, target_versions):
        name = row['path']
        if name in SAVED_BOOK_FILE_REMOVALS and row['existed'] and row['new_sha256'] is None:
            receipt = _current_publication(root, root / name, state, records, name,
                                           row['old_sha256'], list(version[:5]))
            if receipt is not None and list(_version((root / name).lstat())) == receipt['file_version']:
                eligible[name] = receipt
    return eligible


def get_restored_export_versions(root, receipts):
    versions = {}
    for name, receipt in receipts.items():
        proof, _ = capture_scope_file(root, name, limit=MAX_ARTIFACT_HASH_BYTES)
        version = proof['file_version']
        if (proof['sha256'] != receipt['sha256']
                or version[:4] != receipt['file_version'][:4]):
            raise ValueError('Restored export changed before receipt renewal.')
        versions[name] = version
    return versions


def refresh_publication_versions(root, restored, *, prior_receipts=None, before_replace=None):
    root = Path(os.path.abspath(root))
    records = read_publication_receipts(root)
    if prior_receipts is not None:
        if any(records.get(name) != receipt for name, receipt in prior_receipts.items()):
            raise ValueError('Original export receipt changed before receipt renewal.')
        changed = {name: version for name, version in restored.items()
                   if version != records[name]['file_version']}
        if changed:
            for name, version in changed.items():
                records[name] = {**records[name], 'file_version': version}
            # The original ledger can already be close to its byte limit.
            raw = json.dumps(records, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
            write_publication_ledger(root, raw, before_replace=before_replace)
        return
    changed = False
    for name, original in restored.items():
        if name not in records:
            continue
        path = root / name
        if not path.is_file():
            continue
        proof, _raw = capture_scope_file(root, name, limit=MAX_ARTIFACT_HASH_BYTES)
        if not proof['exists'] or proof['sha256'] != records[name]['sha256']:
            continue
        version = list(proof['file_version'])
        if (original is not None
                and list(records[name]['file_version'][:4]) != list(original[:4])):
            continue
        if list(_version(path.lstat())) != version:
            continue
        if version == records[name]['file_version']:
            continue
        records[name] = {**records[name], 'file_version': version}
        changed = True
    if changed:
        write_publication_ledger(root, serialize_publication_receipts(records))


def rebind_publication_version(root, relative, proof):
    root = Path(os.path.abspath(root))
    records = read_publication_receipts(root)
    if relative not in records:
        return False
    receipt = records[relative]
    version = list(proof['file_version'])
    if (proof['sha256'] != receipt['sha256']
            or list(_version((root / relative).lstat())) != version):
        return False
    records[relative] = {**receipt, 'file_version': version}
    write_publication_ledger(root, serialize_publication_receipts(records))
    return True


def settle_publication_receipt(root, relative, sha256, staged_version):
    root = Path(os.path.abspath(root))
    journal = read_publication_journal(root)
    if journal is not None and journal['version'] == 3:
        _proof, raw = _get_owned_ledger(root, journal['ledger'])
        records = _parse_publication_receipts(raw)
    else:
        records = read_publication_receipts(root)
    if relative not in records or records[relative]['sha256'] != sha256:
        raise ValueError('Export publication receipt changed before settlement.')
    proof, _raw = capture_scope_file(root, relative, limit=MAX_ARTIFACT_HASH_BYTES)
    version = list(proof['file_version'])
    if (not proof['exists'] or proof['sha256'] != sha256
            or list(version[:4]) != list(staged_version[:4])
            or list(_version((root / relative).lstat())) != version):
        raise ValueError('Published export changed before receipt settlement.')
    records[relative] = {**records[relative], 'file_version': version}
    raw = serialize_publication_receipts(records)
    if journal is not None and journal['version'] == 3:
        _write_owned_ledger(root, raw, PUBLICATION_JOURNAL, journal)
    else:
        write_publication_ledger(root, raw)
    return version


def preflight_export_publication(root, path):
    preflight_export_publication_batch(root, (path,))


def preflight_export_publication_batch(root, paths):
    from book_state_transaction import get_book_operation_token
    root = Path(os.path.abspath(root))
    records = read_publication_receipts(root)
    token = get_book_operation_token(root)
    additions = set()
    for path in paths:
        _root, _path, relative = _receipt_key(root, path)
        if relative not in records:
            additions.add(relative)
    if len(records) + len(additions) > MAX_PUBLICATION_ROWS:
        raise ValueError('Export publication receipt ledger needs inspection.')
    proposed = dict(records)
    for relative in additions:
        proposed[relative] = {'book_token': token, 'scope_token': None,
                              'sha256': '0' * 64, 'file_version': [0, 0, 0, 0, 0]}
    if len(serialize_publication_receipts(proposed)) > MAX_BOOK_METADATA_BYTES:
        raise ValueError('Export publication receipt ledger exceeds its bound.')


def publish_export_with_receipt(root, staged_path, destination, sha256, replace,
                                expected_version=None, *, admit_replace=nullcontext,
                                cancel_check=None):
    import shutil
    from book_state_transaction import is_book_state_admitted

    root, _, staged_relative = _receipt_key(root, staged_path)
    if not is_book_state_admitted(root):
        raise ValueError('Book admission is required for export publication.')
    _, destination, relative = _receipt_key(root, destination)
    identity = get_export_reader_identity(root)
    staged_proof, _ = _capture_proof(root, staged_relative)
    if (staged_proof is None
            or (expected_version is not None
                and list(staged_proof['file_version']) != list(expected_version))):
        raise ValueError('Published export changed before receipt recording.')
    if sha256 is not None and staged_proof['sha256'] != sha256:
        raise ValueError('Published export changed before receipt recording.')
    sha256 = staged_proof['sha256']
    if (read_publication_journal(root) is not None
            or read_set_journal(root) is not None):
        raise OSError('An unresolved export publication is pending; retry after recovery.')
    preflight_export_publication(root, destination)
    ledger, records = _get_ledger_intent(root)
    prior = records.get(relative)
    prior_proof, _ = _capture_proof(root, relative)
    prior_eligible = prior_proof is not None and prior is not None and (
        prior_proof == {key: prior[key] for key in prior_proof}
        and (prior['book_token'], prior['scope_token']) == identity)
    backup_name = '.export-previous-' + uuid.uuid4().hex
    journal = {'version': 3, 'phase': 'intent', 'target': relative,
               'backup': backup_name, 'staged': staged_relative,
               'staged_sha256': sha256,
               'prior': prior,
               'prior_existed': prior_proof is not None,
               'prior_sha256': prior_proof['sha256'] if prior_proof else None,
               'prior_eligible': prior_eligible,
               'ledger': ledger, 'before': prior_proof,
               'new': _get_version_after_rename(staged_proof), 'copy': None}
    write_publication_journal(root, journal)
    backup = None
    try:
        if prior_proof is not None:
            with _publication_scratch(root, '.export-copy-') as (target, copy):
                with destination.open('rb') as source:
                    if list(_version(os.fstat(source.fileno()))) != prior_proof['file_version']:
                        raise ValueError('Previous export changed before publication.')
                    shutil.copyfileobj(source, target, 1024 * 1024)
                    target.flush()
                    os.fsync(target.fileno())
                    if list(_version(os.fstat(source.fileno()))) != prior_proof['file_version']:
                        raise ValueError('Previous export changed before publication.')
                target.close()
                copy_relative = _receipt_key(root, copy)[2]
                backup_proof, _ = _capture_proof(root, copy_relative)
                if backup_proof is None or backup_proof['sha256'] != prior_proof['sha256']:
                    raise ValueError('Previous export changed before publication.')
                _require_publication_version(root, relative, prior_proof)
                journal = {**journal, 'copy': _get_version_after_rename(backup_proof)}
                write_publication_journal(root, journal)
                backup = root / backup_name
                _require_publication_version(root, backup_name, None)
                _require_publication_version(root, copy_relative, backup_proof)
                os.replace(copy, backup)
                sync_parent_directory(root)
        else:
            _require_publication_version(root, relative, None)
        next_records = dict(records)
        next_records[relative] = {
            'book_token': identity[0],
            'scope_token': identity[1],
            'sha256': sha256,
            'file_version': list(staged_proof['file_version'])}
        proposed_bytes = serialize_publication_receipts(next_records)
        if len(proposed_bytes) > MAX_BOOK_METADATA_BYTES:
            raise ValueError('Export publication receipt ledger exceeds its bound.')
        _write_owned_ledger(root, proposed_bytes, PUBLICATION_JOURNAL, journal)
        if cancel_check is None:
            from audio_generation_owner import ensure_audio_generation_commit_active
            cancel_check = ensure_audio_generation_commit_active
        with admit_replace():
            if get_export_reader_identity(root) != identity:
                raise ValueError('Export book changed before publication.')
            _require_publication_version(root, relative, prior_proof)
            _require_publication_version(root, staged_relative, staged_proof)
            if cancel_check is not None and cancel_check() is True:
                from audio_generation_owner import AudioGenerationCancelled
                raise AudioGenerationCancelled()
            replace()
        installed = settle_publication_receipt(root, relative, sha256, staged_proof['file_version'])
        journal = read_publication_journal(root)
        _get_owned_ledger(root, journal['ledger'])
        with admit_replace():
            _require_publication_version(root, relative, {'file_version': installed})
            if get_export_reader_identity(root) != identity:
                raise ValueError('Export book changed before publication.')
            _write_export_commit(root, PUBLICATION_JOURNAL, {**journal, 'phase': 'committed'},
                                 read_publication_journal, _confirm_single_export_commit)
    except ExportCommitUncertain:
        raise
    except BaseException as error:
        from audio_generation_owner import AudioGenerationCancelled
        from tts import ExportCancelled
        if not isinstance(error, (AudioGenerationCancelled, ExportCancelled)):
            # Preserve historical behavior for ordinary faults: a rollback
            # failure surfaces its own error to the caller.
            recover_publication_journal(root)
            raise
        try:
            recovered = recover_publication_journal(root)
        except BaseException:
            recovered = False
        if not recovered and os.path.lexists(os.path.join(root, PUBLICATION_JOURNAL)):
            # A cancellation whose rollback/cleanup did not finish would
            # otherwise be reported as an ordinary cancelled task. Describe
            # only the artifacts actually still on disk: rollback may already
            # have restored the original ledger even when cleanup failed (R14).
            notes = []
            try:
                pending = read_publication_journal(root)
            except (OSError, ValueError):
                pending = None
            if pending is not None:
                notes.append('the intent journal remains')
                try:
                    _records, current_raw = read_publication_receipts(root, include_bytes=True)
                    ledger = pending.get('ledger') or {}
                    prior_raw = _get_prior_ledger_raw(ledger)
                    writes = ledger.get('writes') or []
                    proposed_sha = ((writes[1] or {}).get('sha256')
                                    if len(writes) == 2 else None)
                    current_sha = (hashlib.sha256(current_raw).hexdigest()
                                   if current_raw is not None else None)
                except (OSError, ValueError, KeyError, TypeError):
                    pass
                else:
                    # Name original/proposed only when the bytes match the
                    # journal's own authority. A valid but unrelated ledger is
                    # neither, and saying otherwise hides a real conflict (F10).
                    if current_raw == prior_raw:
                        notes.append('the original receipt ledger is restored')
                    elif proposed_sha is not None and current_sha == proposed_sha:
                        notes.append('a proposed receipt ledger remains')
                    else:
                        notes.append('an unrelated or unverified receipt ledger remains')
                backup_name = pending.get('backup')
                if (isinstance(backup_name, str) and backup_name
                        and os.path.lexists(root / backup_name)):
                    notes.append('a rollback backup remains')
            else:
                notes.append('recovery state remains')
            raise ExportRecoveryPending(
                'Export publication is pending recovery; the next book admission '
                'will attempt it' + (f" ({', '.join(notes)})." if notes else '.')) from error
        raise
    cleanup_ok = True
    if backup is not None:
        try:
            _remove_publication_backup(root, backup_name, journal['prior_sha256'], journal['copy'])
        except OSError:
            cleanup_ok = False
    if cleanup_ok:
        try:
            (root / PUBLICATION_JOURNAL).unlink(missing_ok=True)
        except OSError:
            pass

def record_export_publication(root, path, sha256=None, expected_version=None, *, export_set=False):
    from book_state_transaction import ensure_book_state, get_book_operation_token, is_book_state_admitted
    root, path, relative = _receipt_key(root, path)
    admission = nullcontext() if is_book_state_admitted(root) else ensure_book_state(root, recover=False)
    with admission:
        proof, _raw = capture_scope_file(root, relative,
            limit=MAX_ARTIFACT_HASH_BYTES)
        if not proof['exists'] or not _is_hex_digest(proof['sha256']):
            raise ValueError('Export publication must be a regular file.')
        if sha256 is not None and proof['sha256'] != sha256:
            raise ValueError('Published export changed before receipt recording.')
        if (expected_version is not None
                and list(proof['file_version']) != list(expected_version)):
            raise ValueError('Published export changed before receipt recording.')
        sha256 = proof['sha256']
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or list(_version(info)) != list(proof['file_version'])):
            raise ValueError('Published export changed before receipt recording.')
        state = read_book_state_bounded(root)
        if export_set:
            journal = read_set_journal(root)
            if (journal is None or journal['version'] != 3 or journal['phase'] != 'intent'
                    or not any(member['target'] == relative for member in journal['members'])):
                raise ValueError('Export set no longer owns this publication.')
            _proof, raw = _get_owned_ledger(root, journal['ledger'])
            records = _parse_publication_receipts(raw)
        else:
            records = read_publication_receipts(root)
        if relative not in records and len(records) >= MAX_PUBLICATION_ROWS:
            raise ValueError('Export publication receipt ledger needs inspection.')
        records = {**records, relative: {
            'book_token': get_book_operation_token(root),
            'scope_token': _scope_token(state),
            'sha256': sha256,
            'file_version': list(proof['file_version'])}}
        proposed = serialize_publication_receipts(records)
        if len(proposed) > MAX_BOOK_METADATA_BYTES:
            raise ValueError('Export publication receipt ledger exceeds its bound.')
        if export_set:
            _write_owned_ledger(root, proposed, PUBLICATION_SET_JOURNAL, journal)
        else:
            write_publication_ledger(root, proposed)


def _scope_token(state):
    scope = state.get('_source_scope')
    return get_snapshot_digest(scope) if scope is not None else None


def _current_publication(root, path, state, records, relative, expected_sha256, expected_version):
    receipt = records.get(relative)
    if not _is_valid_receipt(receipt):
        return None
    from book_state_transaction import get_book_operation_identity
    token = get_snapshot_digest(get_book_operation_identity(root, state=state))
    if receipt['book_token'] != token or receipt['scope_token'] != _scope_token(state):
        return None
    if expected_sha256 is not None and receipt['sha256'] != expected_sha256:
        return None
    version = expected_version if expected_version is not None else (
        list(_version(path.stat())) if path.is_file() else None)
    if version != receipt['file_version']:
        return None
    return receipt


def get_historical_export_reason(root, path, *, expected_sha256=None, expected_version=None):
    root = Path(os.path.abspath(root)); path = Path(os.path.abspath(path))
    state, inventory = _read_export_fence(root)
    relative = path.relative_to(root).as_posix()
    reason = 'scope' if state.get('_source_scope') is not None else 'book'
    journal = read_publication_journal(root)
    if (journal is not None and journal['target'] == relative
            and (journal['phase'] != 'committed' or journal['version'] == 3)):
        return reason
    set_journal = read_set_journal(root)
    if is_export_set_pending(set_journal):
        if any(member['target'] == relative for member in set_journal['members']):
            # A fenced set (intent or committed-cleanup) is not merely a
            # previous-book artifact: name its pending recovery so the reader
            # reason matches the worker's outcome (F04).
            return 'pending'
    if not path.is_file():
        return reason if expected_version is not None else None
    version = expected_version if expected_version is not None else list(_version(path.stat()))
    records = read_publication_receipts(root)
    if relative in records:
        if _current_publication(root, path, state, records, relative,
                                expected_sha256, version) is not None:
            return None
        return reason
    if inventory is None:
        return None
    recorded = None
    for rows in inventory.values():
        if relative in rows:
            recorded = rows[relative]
            break
    legacy_mix = (relative in MIXED_DELIVERIES
                  and set(inventory) in (set(LEGACY_EXPORTS), set(BASE_EXPORTS)))
    canonical = any(relative == name or relative.startswith(name + '/')
                    for name in EXPORTS)
    if canonical:
        return reason
    if recorded is None and not legacy_mix:
        return None
    return reason


def is_scope_export_historical(root, path):
    return get_historical_export_reason(root, path) is not None


def get_export_reader_identity(root):
    state, _inventory = _read_export_fence(root)
    from book_state_transaction import get_book_operation_identity
    return (get_snapshot_digest(get_book_operation_identity(root, state=state)),
            _scope_token(state))


def open_current_export_reader(root, path, *, expected=None):
    from export_file_io import open_export_reader
    with export_reader_admission(root):
        if expected is not None and get_export_reader_identity(root) != tuple(expected):
            require_scope_export_current(root, path, force_stale=True)
        try:
            stream = open_export_reader(path)
        except OSError:
            require_scope_export_current(root, path, force_stale=True)
            raise
        try:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode):
                raise ValueError('Export download requires a regular file')
            require_scope_export_current(root, path,
                expected_version=list(_version(info)))
        except BaseException:
            stream.close()
            raise
    return stream


def require_scope_export_current(root, path, *, expected_sha256=None, expected_version=None,
                                 force_stale=False):
    with export_reader_admission(root):
        reason = get_historical_export_reason(root, path,
            expected_sha256=expected_sha256, expected_version=expected_version) if not force_stale else 'book'
    if reason is not None:
        from core import AppError
        if reason == 'book':
            raise AppError(409, 'This export belongs to a previous book; rebuild it before downloading.',
                           key='safety.export.previous_book')
        if reason == 'pending':
            raise AppError(409, 'This export is pending recovery; retry after the next book admission finishes or attempts it.',
                           key='safety.export.pending_recovery')
        raise AppError(409, 'This export predates the selected source scope. Rebuild it before downloading.',
                       key='safety.export.previous_scope')
