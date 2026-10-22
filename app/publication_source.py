"""Bounded source-origin companions, under the caller's upload quota lease."""
import hashlib
import base64
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re

from adapter_publication import save_adapter_publication_bytes, sync_adapter_directory
from project_scope_files import capture_scope_file

MAX_PUBLICATION_SOURCE_BYTES = 48 * 1024**2
MAX_PUBLICATION_ORIGINS = 4


class PublicationSourceLimit(ValueError):
    pass


def get_publication_source_path(input_path):
    return str(input_path) + '.publication.json'


def _is_digest(value):
    return isinstance(value, str) and re.fullmatch('[0-9a-f]{64}', value) is not None


def _is_text(value, limit=20000):
    return isinstance(value, str) and 0 < len(value) <= limit


def _require(condition, message='Invalid source publication provenance'):
    if not condition:
        raise ValueError(message)


def _require_choices(values):
    _require(isinstance(values, list) and len(values) <= 32
             and all(_is_text(value) for value in values))
    _require(len(set(values)) == len(values))


def get_publication_source_document(raw):
    """Validate a bounded companion; cover pixels are rechecked before export."""
    _require(isinstance(raw, bytes) and len(raw) <= MAX_PUBLICATION_SOURCE_BYTES)
    try:
        doc = json.loads(raw)
    except RecursionError as error:
        raise ValueError('Source publication provenance is too deeply nested') from error
    _require(isinstance(doc, dict) and set(doc) == {'version', 'binding', 'candidates'}
             and type(doc['version']) is int and doc['version'] == 1)
    binding = doc['binding']
    _require(isinstance(binding, dict) and set(binding) == {'kind', 'sha256'}
             and binding['kind'] in ('text', 'script') and _is_digest(binding['sha256']))
    rows = doc['candidates']
    _require(isinstance(rows, list) and 0 < len(rows) <= MAX_PUBLICATION_ORIGINS)
    ids, text_ids = [], set()
    for row in rows:
        _require(isinstance(row, dict) and set(row) == {
            'version', 'id', 'origin', 'fields', 'other_creators', 'metadata_status',
            'cover', 'cover_status', 'warnings'} and type(row['version']) is int and row['version'] == 1)
        origin = row['origin']
        _require(isinstance(origin, dict) and set(origin) == {'kind', 'filename', 'sha256', 'text_sha256'}
                 and origin['kind'] == 'epub' and _is_digest(origin['sha256']) and _is_digest(origin['text_sha256']))
        name = origin['filename']
        _require(_is_text(name, 512) and '/' not in name and '\\' not in name
                 and all(ord(char) >= 32 for char in name))
        identity = hashlib.sha256(
            f"AA2_epub_publication_v1:{origin['sha256']}:{origin['text_sha256']}".encode()).hexdigest()
        _require(row['id'] == identity)
        ids.append(identity); text_ids.add(origin['text_sha256'])
        fields = row['fields']
        _require(isinstance(fields, dict) and set(fields) == {'title', 'author'})
        for values in fields.values():
            _require_choices(values)
        others = row['other_creators']
        _require(isinstance(others, list) and len(others) <= 32)
        for creator in others:
            _require(isinstance(creator, dict) and set(creator) == {'name', 'roles'} and _is_text(creator['name']))
            _require_choices(creator['roles'])
        _require(row['metadata_status'] in ('available', 'unavailable'))
        _require(isinstance(row['warnings'], list) and len(row['warnings']) <= 8
                 and all(_is_text(value, 4096) for value in row['warnings']))
        _require(row['cover_status'] in ('available', 'not_declared', 'ambiguous', 'unsupported', 'unavailable'))
        cover = row['cover']
        if row['cover_status'] != 'available':
            _require(cover is None)
        else:
            _require(isinstance(cover, dict) and set(cover) == {'member', 'media_type', 'sha256', 'data_base64'})
            member = cover['member']
            _require(_is_text(member, 65535) and '\\' not in member and '\0' not in member
                     and not PurePosixPath(member).is_absolute() and not PureWindowsPath(member).drive
                     and '..' not in PurePosixPath(member).parts and member == PurePosixPath(member).as_posix())
            _require(cover['media_type'] in ('image/jpeg', 'image/png') and _is_digest(cover['sha256']))
            _require(_is_text(cover['data_base64'], 4 * ((8 * 1024**2 + 2) // 3)))
            data = base64.b64decode(cover['data_base64'], validate=True)
            _require(0 < len(data) <= 8 * 1024**2 and hashlib.sha256(data).hexdigest() == cover['sha256'])
        if row['metadata_status'] == 'unavailable':
            _require(not any(fields.values()) and not others and cover is None)
    _require(len(ids) == len(set(ids)) and len(text_ids) == 1)
    if binding['kind'] == 'text':
        _require(text_ids == {binding['sha256']})
    return doc


def _get_publication_source_bytes(document):
    chunks, size = [], 0
    for part in json.JSONEncoder(ensure_ascii=False, indent=2).iterencode(document):
        chunk = part.encode('utf-8'); size += len(chunk)
        if size > MAX_PUBLICATION_SOURCE_BYTES:
            raise PublicationSourceLimit('Publication provenance exceeds the 48 MiB limit')
        chunks.append(chunk)
    return b''.join(chunks)


def get_publication_source_capture(input_path, *, source_limit=512 * 1024**2):
    """Read exact companions only; never discover origins from filename similarity."""
    if not input_path:
        return None
    _require(isinstance(input_path, (str, Path)), 'Invalid publication source path')
    source = Path(input_path); root = source.parent.resolve()
    if not root.is_dir():
        return None
    companion, raw = capture_scope_file(root, source.name + '.publication.json',
        limit=MAX_PUBLICATION_SOURCE_BYTES, include_bytes=True)
    if raw is None:
        return None
    document = get_publication_source_document(raw)
    facts, _ = capture_scope_file(root, source.name, limit=source_limit)
    expected_kind = 'script' if source.suffix.lower() == '.json' else 'text'
    current = (source.suffix.lower() in ('.txt', '.md', '.json') and facts['exists']
               and document['binding'] == {'kind': expected_kind, 'sha256': facts['sha256']})
    return {'root': str(root), 'source': facts, 'companion': companion,
            'raw': raw, 'document': document, 'binding_status': 'current' if current else 'stale'}


def get_saved_publication_source_bytes(input_path, script_bytes):
    captured = get_publication_source_capture(input_path)
    if captured is None:
        return None
    if captured['binding_status'] == 'stale':
        # Keep the old binding: an ordinary save must not certify stale hints.
        return captured['raw']
    document = {**captured['document'], 'binding': {
        'kind': 'script', 'sha256': hashlib.sha256(script_bytes).hexdigest()}}
    return _get_publication_source_bytes(document)


def get_upload_publication_update(input_path, candidate, remaining_bytes, *, source_limit):
    """Prepare an immutable update; no source/companion is created or changed."""
    source = Path(input_path)
    root = source.parent.resolve()
    facts, _ = capture_scope_file(root, source.name, limit=source_limit)
    expected = candidate['origin']['text_sha256']
    if not facts['exists'] or facts['sha256'] != expected:
        raise ValueError('Canonical source changed before publication provenance capture')
    name = source.name + '.publication.json'
    before, raw = capture_scope_file(root, name, limit=MAX_PUBLICATION_SOURCE_BYTES, include_bytes=True)
    document = get_publication_source_document(raw) if raw is not None else {
        'version': 1, 'binding': {'kind': 'text', 'sha256': expected}, 'candidates': []}
    if (not isinstance(document, dict) or document.get('version') != 1
            or document.get('binding') != {'kind': 'text', 'sha256': expected}
            or not isinstance(document.get('candidates'), list)
            or len(document['candidates']) > MAX_PUBLICATION_ORIGINS
            or any(not isinstance(row, dict) or not isinstance(row.get('id'), str)
                   or not isinstance(row.get('origin'), dict)
                   or row['origin'].get('text_sha256') != expected for row in document['candidates'])):
        raise ValueError('Source publication provenance is corrupt or stale; review it before importing again')
    ids = [row['id'] for row in document['candidates']]
    if len(set(ids)) != len(ids):
        raise ValueError('Source publication origins are ambiguous')
    if candidate['id'] in ids:
        previous = document['candidates'][ids.index(candidate['id'])]
        comparable = {**candidate, 'origin': {**candidate['origin'],
                                            'filename': previous['origin'].get('filename')}}
        if previous != comparable:
            raise ValueError('Existing EPUB origin capture differs; review its provenance before replacing it')
        encoded = raw
    else:
        if len(ids) >= MAX_PUBLICATION_ORIGINS:
            raise PublicationSourceLimit('At most four distinct EPUB origins can share one text upload')
        document['candidates'].append(candidate)
        encoded = _get_publication_source_bytes(document)
    if encoded != raw and len(encoded) > remaining_bytes:
        # Reserve the whole staged replacement, not merely its final size delta.
        raise PublicationSourceLimit('Publication provenance exceeds available upload storage')
    return {'root': str(root), 'name': name, 'before': before,
            'old_bytes': raw, 'new_bytes': encoded}


def apply_publication_source_update(update):
    if update['new_bytes'] == update['old_bytes']:
        return
    current, _ = capture_scope_file(update['root'], update['name'], limit=MAX_PUBLICATION_SOURCE_BYTES)
    if current != update['before']:
        raise ValueError('Source publication provenance changed before publication')
    save_adapter_publication_bytes(str(Path(update['root']) / update['name']), update['new_bytes'])


def restore_publication_source_update(update):
    """Undo only this attempted update, never a changed third-party companion."""
    if update['new_bytes'] == update['old_bytes']:
        return
    current, _ = capture_scope_file(update['root'], update['name'], limit=MAX_PUBLICATION_SOURCE_BYTES)
    if current['sha256'] == update['before']['sha256']:
        return
    if current['sha256'] != hashlib.sha256(update['new_bytes']).hexdigest():
        raise ValueError('Publication provenance changed; refusing destructive upload rollback')
    path = Path(update['root']) / update['name']
    if update['old_bytes'] is None:
        path.unlink()
        sync_adapter_directory(str(path.parent))
    else:
        save_adapter_publication_bytes(str(path), update['old_bytes'])
