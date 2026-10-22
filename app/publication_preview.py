"""Read-only, book-bound publication review and explicit choice resolution."""
import json
from pathlib import Path

from book_state_transaction import get_book_operation_token
from editor_poll_snapshot import get_snapshot_digest
from m4b_cover import MAX_COVER_BYTES
from project_scope_files import capture_scope_file, MAX_SCOPE_METADATA_BYTES
from publication_source import get_publication_source_capture, MAX_PUBLICATION_SOURCE_BYTES


def get_publication_review(data_dir):
    """Caller holds book admission; no generation, repair, or asset writes."""
    state_facts, raw = capture_scope_file(data_dir, 'state.json', include_bytes=True)
    state = json.loads(raw) if raw is not None else {}
    if not isinstance(state, dict):
        raise ValueError('Selected book state must be an object')
    book_token = get_book_operation_token(data_dir)
    captured = get_publication_source_capture(state.get('input_file_path'))
    uploaded, _ = capture_scope_file(data_dir, 'm4b_cover.jpg', limit=MAX_COVER_BYTES)
    checks = [(data_dir, state_facts, MAX_SCOPE_METADATA_BYTES), (data_dir, uploaded, MAX_COVER_BYTES)]
    if captured:
        checks.extend([(captured['root'], captured['source'], 512 * 1024**2),
                       (captured['root'], captured['companion'], MAX_PUBLICATION_SOURCE_BYTES)])
    for root, expected, limit in checks:
        current, _ = capture_scope_file(root, expected['path'], limit=limit)
        if current != expected:
            raise ValueError('Book or source publication changed while reading suggestions')
    if get_book_operation_token(data_dir) != book_token:
        raise ValueError('Book changed while reading publication suggestions')
    revision = get_snapshot_digest({'version': 1, 'book_token': book_token, 'state': state_facts,
        'source': captured['source'] if captured else None,
        'companion': captured['companion'] if captured else None, 'uploaded_cover': uploaded})
    return {'book_token': book_token, 'revision': revision, 'captured': captured, 'uploaded': uploaded, 'state': state_facts,
            'source_name': Path(state.get('input_file_path') or '').name}


def get_publication_preview(review):
    captured = review['captured']
    candidates = []
    for row in captured['document']['candidates'] if captured else []:
        cover = row['cover']
        candidates.append({**row, 'cover': {'member': cover['member'], 'sha256': cover['sha256'],
            'media_type': cover['media_type'],
            'preview_data_url': f"data:{cover['media_type']};base64,{cover['data_base64']}"} if cover else None})
    return {'book_token': review['book_token'], 'revision': review['revision'],
        'source_name': review['source_name'],
        'binding_status': captured['binding_status'] if captured else 'not_captured',
        'candidates': candidates, 'uploaded_cover': {'present': review['uploaded']['exists'],
            'sha256': review['uploaded']['sha256'], 'validation': 'not_rechecked'},
        'notice': 'Captured source suggestions need your review; they do not certify script wording. '
                  'Entered values and uploaded covers take precedence. No metadata or cover has been applied.'}


def get_publication_selection(review, selection):
    """Resolve only exact options explicitly chosen from an unchanged preview."""
    if (selection.get('expected_book_token') != review['book_token']
            or selection.get('expected_revision') != review['revision']):
        raise ValueError('Book or source changed after preview; review publication suggestions again')
    captured = review['captured']
    rows = captured['document']['candidates'] if captured else []
    row = next((row for row in rows if row['id'] == selection.get('origin_id')), None)
    if row is None:
        raise ValueError('The chosen source origin is no longer available')
    requires_review = (captured['binding_status'] != 'current' or len(rows) > 1
        or any(len(values) > 1 for values in row['fields'].values()) or bool(row['other_creators']))
    if requires_review and selection.get('reviewed') is not True:
        raise ValueError('Explicitly review the stale or ambiguous source metadata before applying choices')
    fields = {}
    for name in ('title', 'author'):
        index = selection.get(name + '_index')
        if index is not None:
            values = row['fields'][name]
            if type(index) is not int or not 0 <= index < len(values):
                raise ValueError('The chosen metadata value is unavailable')
            fields[name] = values[index]
    cover_selection = None
    if selection.get('select_cover') is True:
        if review['uploaded']['exists']:
            raise ValueError('An uploaded cover takes precedence; remove it before choosing a source cover')
        if row['cover_status'] != 'available':
            raise ValueError('The chosen source has no usable cover')
        cover_selection = {key: selection[key] for key in (
            'expected_book_token', 'expected_revision', 'origin_id', 'reviewed')}
    if not fields and cover_selection is None:
        raise ValueError('Choose at least one suggested field or cover')
    return {'book_token': review['book_token'], 'revision': review['revision'], 'origin_id': row['id'],
            'fields': fields, 'cover_selection': cover_selection}
