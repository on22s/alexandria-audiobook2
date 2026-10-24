"""Revision-bound score lineage operations under existing task/book ownership."""
import json

from book_state_transaction import apply_book_state_locked
from json_metadata import get_unique_json_object
from project_scope_admission import ensure_scope_admission, ensure_audio_scope_admission
from sound_design_assets import SoundDesignAssetStore, _validate_asset_id
from sound_design_context import (capture_sound_design_context, capture_sound_design_anchor,
                                  require_sound_design_context_files_current)
from sound_design_cues import (_require_digest, _require_int, MAX_RATE, MAX_SECONDS,
                               validate_sound_design_cue_parameters)
from sound_design_edit import _require_context, _require_asset_duration
from sound_design_registry import (ACTIVE_CUES, get_sound_design_registry_edit,
                                   get_sound_design_registry_bytes)
from sound_design_renditions import (ACTIVE_RENDITIONS, MAX_RENDITIONS, MAX_STEMS,
    get_sound_rendition_record, get_sound_rendition_ledger_bytes,
    get_sound_rendition_ledger_edit, get_sound_rendition_keep_edit,
    validate_sound_rendition_definition, validate_sound_rendition_consumers)
from sound_design_rendition_media import ensure_sound_rendition_assets
from source_evidence import _require_keys, _require_text

MAX_RENDITION_REQUEST_BYTES = 16384
MAX_CLEANUP_IDS = 128


class RenditionRequestError(ValueError):
    pass


def _refuse_nonfinite(_value):
    raise ValueError('Nonfinite rendition requests are not supported')


def parse_sound_rendition_request(operation, raw):
    """Validate untrusted bounded metadata before any owner or file admission."""
    try:
        return _parse_sound_rendition_request(operation, raw)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise RenditionRequestError('Invalid or oversized rendition request') from None


def _parse_sound_rendition_request(operation, raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_RENDITION_REQUEST_BYTES:
        raise ValueError('Rendition request exceeds its byte limit or is empty')
    value = json.loads(raw.decode('utf-8'), object_pairs_hook=get_unique_json_object,
                       parse_constant=_refuse_nonfinite)
    common = ('context_revision', 'registry_revision', 'ledger_revision')
    fields = {'record': ('source', 'composition', 'origin', 'derivation', 'master', 'stems'),
              'consume': ('cue_id', 'parent_version', 'chunk_uid', 'anchor_revision',
                          'rendition_id', 'parameters'),
              'keep': ('rendition_id', 'keep'),
              'cleanup_preview': ('rendition_ids',),
              'cleanup': ('rendition_ids', 'impact_revision')}
    if operation not in fields:
        raise ValueError('Unknown rendition operation')
    _require_keys(value, common + fields[operation], 'Rendition request')
    _require_digest(value['context_revision'], 'Rendition context revision')
    for key in ('registry_revision', 'ledger_revision'):
        _require_digest(value[key], key, nullable=True)
    if operation == 'record':
        _require_keys(value['source'], ('cue_id', 'version'), 'Rendition source selection')
        _require_text(value['source']['cue_id'], 128, 'Source cue identity')
        _require_digest(value['source']['version'], 'Source cue version')
        validate_sound_rendition_definition(value['composition'], value['origin'], value['derivation'])
        _validate_asset_selection(value['master'])
        stems = value['stems']
        if not isinstance(stems, list) or len(stems) > MAX_STEMS:
            raise ValueError('Rendition stem count exceeds its limit')
        seen = set()
        for stem in stems:
            _require_keys(stem, ('stem_id', 'label', 'asset_id', 'source_sha256',
                                 'canonical_sha256', 'offset_frames'), 'Requested rendition stem')
            _require_text(stem['stem_id'], 128, 'Stem identity')
            _require_text(stem['label'], 256, 'Stem label')
            if stem['stem_id'] in seen:
                raise ValueError('Rendition repeats a stem identity')
            seen.add(stem['stem_id'])
            _validate_asset_selection({key: stem[key] for key in
                                      ('asset_id', 'source_sha256', 'canonical_sha256')})
            _require_int(stem['offset_frames'], -MAX_RATE * MAX_SECONDS,
                         MAX_RATE * MAX_SECONDS, 'Requested master-clock stem offset')
    elif operation == 'consume':
        _require_text(value['cue_id'], 128, 'Consuming cue identity')
        _require_digest(value['parent_version'], 'Parent cue revision', nullable=True)
        _require_text(value['chunk_uid'], 128, 'Stable dialogue identity')
        _require_digest(value['anchor_revision'], 'Cue anchor revision')
        _require_digest(value['rendition_id'], 'Consumed rendition identity')
        validate_sound_design_cue_parameters(value['parameters'])
    elif operation == 'keep':
        _require_digest(value['rendition_id'], 'Kept rendition identity')
        if type(value['keep']) is not bool:
            raise ValueError('Keep requires an explicit boolean')
    else:
        ids = value['rendition_ids']
        if not isinstance(ids, list) or not 1 <= len(ids) <= MAX_CLEANUP_IDS:
            raise ValueError('Select between one and 128 renditions for cleanup')
        for identity in ids:
            _require_digest(identity, 'Cleanup rendition identity')
        if len(set(ids)) != len(ids):
            raise ValueError('Cleanup rendition identities must be unique')
        if operation == 'cleanup':
            _require_digest(value['impact_revision'], 'Cleanup impact revision')
    return value


def _validate_asset_selection(value):
    _require_keys(value, ('asset_id', 'source_sha256', 'canonical_sha256'), 'Rendition asset selection')
    _validate_asset_id(value['asset_id'])
    for key in ('source_sha256', 'canonical_sha256'):
        _require_digest(value[key], key)


def require_sound_rendition_context(snapshot, payload):
    _require_context(snapshot, payload)
    if snapshot['ledger_revision'] != payload['ledger_revision']:
        raise ValueError('Rendition ledger changed; refresh before editing')


def get_sound_rendition_view(snapshot, *, offset=0, limit=100, cue_id=None):
    """Return metadata only; draft selection and keep never imply approval."""
    _require_int(offset, 0, MAX_RENDITIONS, 'Rendition page offset')
    _require_int(limit, 1, 100, 'Rendition page length')
    if cue_id is not None:
        _require_text(cue_id, 128, 'Rendition cue filter')
    records = [row for row in snapshot['renditions']['records']
               if cue_id is None or row['source']['cue_id'] == cue_id]
    selected = set(snapshot['registry']['selected'].values())
    draft = sorted({row['recipe']['rendition_id'] for row in snapshot['registry']['versions']
                    if row['schema'] == 2 and row['version'] in selected})
    end = offset + limit
    return dict(context_revision=snapshot['context_revision'], registry_revision=snapshot['registry_revision'],
                ledger_revision=snapshot['ledger_revision'], book_token=snapshot['book_token'],
                book_id=snapshot['book']['book_id'], records=records[offset:end],
                keep=list(snapshot['renditions']['keep']), draft_selected=draft,
                total_records=len(records), next_offset=end if end < len(records) else None,
                approval_checked=False)


def _get_requested_artifact(rows, request, offset):
    """Select bounded manifest facts; actual media admission happens later."""
    row = rows.get(request['asset_id'])
    if (row is None or row['state'] != 'ready'
            or row['source']['sha256'] != request['source_sha256']
            or row['canonical']['sha256'] != request['canonical_sha256']):
        raise ValueError('Selected rendition asset changed; refresh the library')
    return dict(asset_id=request['asset_id'], source=row['source'],
                canonical=row['canonical'], offset_frames=offset)


def _record(root, snapshot, payload, check_active):
    selected = payload['source']
    source = next((row for row in snapshot['registry']['versions']
                   if row['cue_id'] == selected['cue_id'] and row['version'] == selected['version']), None)
    if source is None:
        raise ValueError('Rendition source cue is absent from retained history')
    source_facts = dict(cue_id=source['cue_id'], version=source['version'],
                        book=source['recipe']['book'])
    # Establish the complete proposed record/ledger from bounded metadata before
    # hashing audio. Parent/source/offset/count/byte limits share the authoritative
    # validators below; actual source/canonical bytes must still verify afterward.
    rows = {row['asset_id']: row for row in SoundDesignAssetStore(root).list_assets()['assets']}
    master = _get_requested_artifact(rows, payload['master'], 0)
    stems = []
    for stem in payload['stems']:
        check_active()
        stems.append(dict(stem_id=stem['stem_id'], label=stem['label'],
            artifact=_get_requested_artifact(rows, stem, stem['offset_frames'])))
    record = get_sound_rendition_record(source_facts, payload['composition'], payload['origin'], payload['derivation'],
        master, stems)
    proposed = get_sound_rendition_ledger_edit(snapshot['renditions'], record)
    validate_sound_rendition_consumers(proposed, snapshot['registry'])
    encoded = get_sound_rendition_ledger_bytes(proposed)
    with ensure_sound_rendition_assets(root, {**snapshot, 'renditions': proposed},
            [record['rendition_id']], check_active=check_active):
        fresh = capture_sound_design_context(root, check_active=check_active)
        require_sound_rendition_context(fresh, payload)
        require_sound_design_context_files_current(root, fresh)
    check_active()
    require_sound_design_context_files_current(root, fresh)
    check_active()
    if proposed != snapshot['renditions']:
        apply_book_state_locked(root, {ACTIVE_RENDITIONS: encoded}, ())
    current = capture_sound_design_context(root, check_active=check_active)
    return dict(status='recorded', rendition_id=record['rendition_id'],
                ledger_revision=current['ledger_revision'], context_revision=current['context_revision'],
                book_token=current['book_token'], refresh_required=True)


def _consume(root, snapshot, payload, check_active):
    record = next((row for row in snapshot['renditions']['records']
                   if row['rendition_id'] == payload['rendition_id']), None)
    if record is None or record['source']['cue_id'] != payload['cue_id']:
        raise ValueError('Consuming cue requires its retained same-cue rendition')
    if payload['parent_version'] != snapshot['registry']['selected'].get(payload['cue_id']):
        raise ValueError('Consuming cue parent changed; review the current revision')
    anchor = capture_sound_design_anchor(root, snapshot, payload['chunk_uid'], check_active=check_active)
    if anchor['anchor_revision'] != payload['anchor_revision']:
        raise ValueError('Selected dialogue audio changed; review the anchor again')
    master = record['master']
    descriptor = {'asset_id': master['asset_id'], **{key: master['canonical'][key] for key in
                 ('sha256', 'frames', 'frame_rate', 'channels', 'sample_width')}}
    recipe = dict(book=snapshot['book'], selection=anchor['selection'], asset=descriptor,
                  rendition_id=record['rendition_id'], **payload['parameters'])
    _require_asset_duration(recipe)
    updated = get_sound_design_registry_edit({**snapshot['registry'], 'book': snapshot['book']},
        payload['cue_id'], recipe, payload['parent_version'], schema=2)
    validate_sound_rendition_consumers(snapshot['renditions'], updated)
    encoded = get_sound_design_registry_bytes(updated)
    with ensure_sound_rendition_assets(root, snapshot, [record['rendition_id']], check_active=check_active):
        fresh = capture_sound_design_context(root, check_active=check_active)
        require_sound_rendition_context(fresh, payload)
        latest = capture_sound_design_anchor(root, fresh, payload['chunk_uid'], check_active=check_active)
        if latest != anchor:
            raise ValueError('Selected dialogue changed before rendition publication')
        require_sound_design_context_files_current(root, fresh, latest)
    check_active()
    require_sound_design_context_files_current(root, fresh, latest)
    check_active()
    apply_book_state_locked(root, {ACTIVE_CUES: encoded}, ())
    current = capture_sound_design_context(root, check_active=check_active)
    return dict(status='saved', cue_id=payload['cue_id'], version=updated['selected'][payload['cue_id']],
                rendition_id=record['rendition_id'], registry_revision=current['registry_revision'],
                ledger_revision=current['ledger_revision'], context_revision=current['context_revision'],
                book_token=current['book_token'], refresh_required=True)


def run_sound_rendition_operation(root, operation, *, payload=None, offset=0, limit=100,
                                   cue_id=None, check_active=lambda: None):
    """Run on the existing worker; media operations retain the CPU audio owner."""
    if operation != 'metadata':
        # Pure callers do not get to bypass the same request schema as HTTP.
        from json_metadata import get_metadata_json_bytes
        payload = parse_sound_rendition_request(operation,
            get_metadata_json_bytes(payload, MAX_RENDITION_REQUEST_BYTES, 'Rendition request'))
    else:
        _require_int(offset, 0, MAX_RENDITIONS, 'Rendition page offset')
        _require_int(limit, 1, 100, 'Rendition page length')
        if cue_id is not None:
            _require_text(cue_id, 128, 'Rendition cue filter')
    admission = ensure_audio_scope_admission if operation in ('record', 'consume') else ensure_scope_admission
    check_active()
    with admission(root):
        snapshot = capture_sound_design_context(root, check_active=check_active)
        if operation == 'metadata':
            return get_sound_rendition_view(snapshot, offset=offset, limit=limit, cue_id=cue_id)
        require_sound_rendition_context(snapshot, payload)
        if operation == 'record':
            return _record(root, snapshot, payload, check_active)
        if operation == 'consume':
            return _consume(root, snapshot, payload, check_active)
        if operation == 'keep':
            proposed = get_sound_rendition_keep_edit(snapshot['renditions'], payload['rendition_id'], payload['keep'])
            encoded = get_sound_rendition_ledger_bytes(proposed)
            require_sound_design_context_files_current(root, snapshot)
            check_active()
            if proposed != snapshot['renditions']:
                apply_book_state_locked(root, {ACTIVE_RENDITIONS: encoded}, ())
            current = capture_sound_design_context(root, check_active=check_active)
            return dict(status='saved', rendition_id=payload['rendition_id'], keep=payload['keep'],
                        ledger_revision=current['ledger_revision'], context_revision=current['context_revision'],
                        book_token=current['book_token'], refresh_required=True)
        from sound_design_rendition_cleanup import run_sound_rendition_cleanup
        return run_sound_rendition_cleanup(root, snapshot, payload,
            execute=operation == 'cleanup', check_active=check_active)
