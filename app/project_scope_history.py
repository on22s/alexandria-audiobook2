"""Read-only history authority; scope changes never rewrite originating records."""
from pathlib import Path
from audio_take_history import AudioTakeHistory, MAX_MANIFEST_BYTES, _check_cancelled
from project_scope_files import capture_scope_file
from take_publication import _read_audio
from take_retention import validate_retained_take


def get_scope_history_plan(root, book_token, retained_uids, *, retained_rows=None, cancel_check=None):
    """Verify retained ready/deleted versions and preserve all other history.

    Caller owns book/task admission and must resnapshot before applying. No
    history directory, archive lock, format conversion or purge is created.
    """
    if (not isinstance(book_token, str) or len(book_token) != 64
            or any(c not in '0123456789abcdef' for c in book_token)
            or not isinstance(retained_uids, list)
            or any(not isinstance(uid, str) or not uid for uid in retained_uids)
            or len(set(retained_uids)) != len(retained_uids)):
        raise ValueError('Unambiguous book and retained UID bindings are required.')
    root = Path(root)
    before, _ = capture_scope_file(root, 'take_history/manifest.json', limit=MAX_MANIFEST_BYTES,
                                   cancel_check=cancel_check)
    pending, _ = capture_scope_file(root, 'take_history/manifest.pending', limit=MAX_MANIFEST_BYTES,
                                    cancel_check=cancel_check)
    if pending['exists']:
        raise ValueError('Interrupted take history metadata requires inspection before changing scope.')
    history = AudioTakeHistory(root)
    document = history._load()
    retained, preserved_other, preserved_payloads, predecessors = [], 0, [], []
    wanted = set(retained_uids)
    for record in document['records']:
        _check_cancelled(cancel_check)
        if record['book_token'] != book_token or record['uid'] not in wanted:
            preserved_other += 1
            proof, _ = capture_scope_file(root, history._path(record).relative_to(history.root).as_posix(),
                                          cancel_check=cancel_check)
            preserved_payloads.append(proof)
            continue
        if record['state'] not in ('ready', 'deleted'):
            raise ValueError('An incomplete retained history version requires inspection.')
        path = history._path(record)
        audio, version = _read_audio(history.root, path, cancel_check)
        if (audio['sha256'] != record['sha256'] or audio['size'] != record['bytes']
                or {key: audio[key] for key in ('frames', 'sample_rate', 'channels')} != record['shape']):
            raise ValueError('Retained take history bytes differ from their saved manifest.')
        retained.append({'version': record['version'], 'uid': record['uid'],
                         'state': record['state'], 'audio': audio,
                         'path': path.relative_to(history.root).as_posix(),
                         'file_version': list(version)})
    for row in retained_rows or []:
        receipt = validate_retained_take(row)
        if receipt is None:
            continue
        if row.get('uid') not in wanted or receipt['book_token'] != book_token:
            raise ValueError('Retained predecessor belongs to a different book or UID.')
        archive = receipt['archive']
        if archive is None:
            proof, _ = capture_scope_file(root, receipt['path'], cancel_check=cancel_check)
            if not proof['exists'] or proof['file_version'] != receipt['source_version']:
                raise ValueError('Retained predecessor source slot changed.')
            predecessors.append(proof)
        else:
            record = history._find(document, archive['version'], archive['sha256'], book_token, row['uid'])
            if (record['state'] != 'ready' or record['captured'] != receipt['captured']
                    or history._path(record).relative_to(history.root).as_posix() != archive['path']):
                raise ValueError('Retained predecessor archive metadata differs.')
    after, _ = capture_scope_file(root, 'take_history/manifest.json', limit=MAX_MANIFEST_BYTES,
                                  cancel_check=cancel_check)
    pending_after, _ = capture_scope_file(root, 'take_history/manifest.pending', limit=MAX_MANIFEST_BYTES,
                                          cancel_check=cancel_check)
    if after != before or pending_after != pending:
        raise ValueError('Take history changed during scope preview.')
    return {'manifest': before, 'pending': pending, 'retained_versions': retained,
            'preserved_other_records': preserved_other, 'preserved_payloads':preserved_payloads,
            'predecessors': predecessors, 'purged_records': 0}
