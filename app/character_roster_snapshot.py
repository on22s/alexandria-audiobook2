"""Bounded book/source/cast snapshots for an explicit character review."""
import hashlib
import json
import re
import copy
from pathlib import Path

from book_state_transaction import get_snapshot_digest
from character_roster import (get_character_roster_review, validate_character_roster,
    validate_character_roster_proposal, MAX_RETIRED_IDS)
from character_roster_import import get_roster_import_reconciliation, validate_roster_approval
from project_scope_admission import is_scope_admitted
from project_scope_files import capture_scope_file
from project_scope_source import get_scope_source_snapshot
from source_evidence import MAX_SOURCE_CHARS, _require_keys

MAX_ROSTER_BYTES = 2 * 1024**2
MAX_CAST_BYTES = 8 * 1024**2
UNDO = '.character_roster_undo.json'
DOMAIN = 'prepared_source_unicode_characters_v1'


def get_roster_json_bytes(value, limit=MAX_ROSTER_BYTES):
    """Encode a bounded JSON artifact without accepting NaN or invalid Unicode."""
    parts, size = [], 0
    for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True,
                                 separators=(',', ':')).iterencode(value):
        raw = part.encode('utf-8'); size += len(raw)
        if size > limit:
            raise ValueError('Character review exceeds its byte limit')
        parts.append(raw)
    return b''.join(parts)


def get_roster_digest(raw):
    return hashlib.sha256(raw).hexdigest() if raw is not None else None


def is_character_roster_approval_current(snapshot, data, saved):
    return (saved is not None and saved['source_binding'] == snapshot['source_binding']
            and saved['cast_sha256'] == get_roster_digest(data['cast']))


def get_character_roster_snapshot(root, *, load_saved=True):
    """Caller excludes tasks and holds book admission; no application files change."""
    if type(load_saved) is not bool:
        raise ValueError('Saved roster loading flag must be boolean')
    if not is_scope_admitted(root):
        raise ValueError('Task and book admission are required for character review')
    root = Path(root)
    state_proof, raw_state = capture_scope_file(root, 'state.json', include_bytes=True)
    state = json.loads(raw_state) if raw_state is not None else None
    if not isinstance(state, dict):
        raise ValueError('Saved book state is required')
    for key in ('active_book_id', 'book_generation'):
        if state.get(key) is not None and (not isinstance(state[key], str) or not 1 <= len(state[key]) <= 1024):
            raise ValueError('Invalid saved book identity')
    source_proof, source = get_scope_source_snapshot(state, byte_limit=4 * MAX_SOURCE_CHARS,
                                                    character_limit=MAX_SOURCE_CHARS)
    binding = {'raw_sha256': source_proof['sha256'],
               'prepared_text_sha256': source_proof['prepared_text_sha256'],
               'encoding': source_proof['encoding'], 'strip_front_matter': source_proof['strip_front_matter'],
               'offset_domain': DOMAIN}
    stem = 'cast_lists/' + source_proof['sha256']
    paths = {'cast': stem + '.json', 'roster': stem + '.roster.json'}
    proofs, data = {}, {}
    for key, name in paths.items():
        proofs[key], data[key] = capture_scope_file(root, name, include_bytes=True,
            limit=MAX_CAST_BYTES if key == 'cast' else MAX_ROSTER_BYTES)
    proofs['undo'], _ = capture_scope_file(root, UNDO, limit=32 * 1024**2)
    saved = json.loads(data['roster']) if load_saved and data['roster'] is not None else None
    if saved is not None:
        keys = ('schema', 'source_binding', 'roster', 'cast_sha256')
        if isinstance(saved, dict) and 'approval' in saved:
            keys += ('approval',)
        _require_keys(saved, keys, 'Saved character review')
        if type(saved['schema']) is not int or saved['schema'] != 1:
            raise ValueError('Unsupported saved character review')
        _require_keys(saved['source_binding'], tuple(binding), 'Saved source binding')
        binding_value = saved['source_binding']
        if (binding_value['offset_domain'] != DOMAIN
                or not isinstance(binding_value['encoding'], str) or not 1 <= len(binding_value['encoding']) <= 100
                or (binding_value['strip_front_matter'] is not None and type(binding_value['strip_front_matter']) is not bool)
                or any(not isinstance(binding_value[key], str) or re.fullmatch(r'[0-9a-f]{64}', binding_value[key]) is None
                       for key in ('raw_sha256', 'prepared_text_sha256'))
                or not isinstance(saved['cast_sha256'], str) or re.fullmatch(r'[0-9a-f]{64}', saved['cast_sha256']) is None):
            raise ValueError('Invalid saved character source/cast binding')
        validate_character_roster(saved['roster'])
        if 'approval' in saved:
            validate_roster_approval(saved['approval'], saved['roster'])
        # Stored bindings are compared only; they never authorize a path read.
        if binding_value['raw_sha256'] != binding['raw_sha256']:
            raise ValueError('Saved character source differs from its source-keyed cache; preserve the invalid record for manual inspection')
        if saved['roster']['source_sha256'] != saved['source_binding']['prepared_text_sha256']:
            raise ValueError('Saved roster and source binding differ')
    snapshot = {'book_token': get_snapshot_digest([str(root.absolute()), state.get('active_book_id'), state.get('book_generation')]),
                'state': state_proof, 'source': source_proof, 'source_binding': binding,
                'files': proofs, 'paths': paths}
    return snapshot, source, data, saved


def get_character_roster_preview(root, proposal, imported_roster=None, import_id_mapping=None):
    """Recompute inputs and explicit identity decisions; this never approves them."""
    get_roster_json_bytes({'proposal': proposal, 'imported_roster': imported_roster, 'import_id_mapping': import_id_mapping})
    validate_character_roster_proposal(proposal)
    if imported_roster is not None:
        validate_character_roster(imported_roster)
    if imported_roster is None and import_id_mapping is not None:
        raise ValueError('An ID mapping requires an imported roster')
    snapshot, source, data, saved = get_character_roster_snapshot(root)
    import_review = None
    if saved is not None and imported_roster is not None:
        import_review = get_roster_import_reconciliation(imported_roster, import_id_mapping, proposal['entries'])
    elif import_id_mapping is not None:
        raise ValueError('Use identity decisions to reconcile an import when no current roster exists')
    previous = saved['roster'] if saved is not None else imported_roster
    review = get_character_roster_review(source, previous, proposal,
        source_changed=imported_roster is not None or (saved is not None and saved['source_binding'] != snapshot['source_binding']))
    if import_review is not None:
        retired = sorted(set(review['roster']['retired_ids']) | set(import_review['retired_ids']))
        if len(retired) > MAX_RETIRED_IDS or set(retired) & {e['id'] for e in review['roster']['entries']}:
            raise ValueError('Imported retirement history conflicts with this draft or exceeds its limit')
        review = copy.deepcopy(review)
        review['roster']['retired_ids'] = retired
    review['import_review'] = import_review
    cast_bytes = get_roster_json_bytes({'cast': review['cast'], 'provenance': {'character_roster': 1}}, MAX_CAST_BYTES)
    if data['cast'] is not None:
        from three_pass_generate import get_cast_from_data
        try:
            current_cast = get_cast_from_data(json.loads(data['cast']))
        except (ValueError, TypeError):
            raise ValueError('Current cast needs inspection in the cast editor') from None
        desired_cast = get_cast_from_data(review['cast'])
        if current_cast == desired_cast:
            cast_bytes = data['cast']
    record = {'schema': 1, 'source_binding': snapshot['source_binding'],
              'roster': review['roster'], 'cast_sha256': get_roster_digest(cast_bytes),
              'approval': {'decisions': review['decisions'], 'import_review': import_review}}
    roster_bytes = get_roster_json_bytes(record)
    revision = get_snapshot_digest({'snapshot': snapshot, 'proposal': proposal, 'imported_roster': imported_roster, 'import_id_mapping': import_id_mapping})
    return {'revision': revision, 'snapshot': snapshot, 'review': review,
            'cast_changed': cast_bytes != data['cast'], 'cast_bytes': cast_bytes, 'roster_bytes': roster_bytes,
            'before': data, 'source_name': Path(snapshot['source']['absolute_path']).name,
            'approval_current': is_character_roster_approval_current(snapshot, data, saved)}
