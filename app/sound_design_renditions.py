"""Bounded immutable score lineage; metadata is neither media admission nor approval.

Historical source facts stay historical. Callers must separately prove retained
cue closure, actual asset bytes, current context and task/book ownership before
using these records or publishing a ledger mutation.
"""
import hashlib
import json
from pathlib import Path

from json_metadata import get_metadata_json_bytes, get_unique_json_object

ACTIVE_RENDITIONS = '.sound_design_renditions.json'
MAX_RENDITION_BYTES = 2 * 1024**2
MAX_RENDITIONS = 1024
MAX_STEMS = 16


def get_saved_sound_renditions_path(script_path):
    path = Path(script_path)
    return str(path.with_name('.' + path.stem + '.sound_design_renditions.json'))


def _validate_artifact(artifact, master_rate=None):
    from source_evidence import _require_keys
    from sound_design_assets import get_sound_asset_manifest_bytes, MAX_PCM_BYTES
    from sound_design_cues import _require_int, MAX_SECONDS
    _require_keys(artifact, ('asset_id', 'source', 'canonical', 'offset_frames'), 'Rendition artifact')
    # Reuse the admitted asset schema, including source bounds and exact canonical
    # geometry. This synthetic manifest validates facts only; it cannot admit media.
    get_sound_asset_manifest_bytes({'schema': 1, 'assets': [{
        'asset_id': artifact['asset_id'], 'state': 'ready', 'source': artifact['source'],
        'reserved_pcm_bytes': MAX_PCM_BYTES, 'canonical': artifact['canonical']}]})
    limit = 0 if master_rate is None else master_rate * MAX_SECONDS
    _require_int(artifact['offset_frames'], -limit, limit, 'Master-clock artifact offset')


def validate_sound_rendition_definition(composition, origin, derivation):
    """Share strict declared provenance validation across requests and records."""
    from source_evidence import _require_keys, _require_text
    from sound_design_cues import _require_digest, _require_choice
    _require_keys(composition, ('scaffold_sha256', 'arrangement_sha256', 'family_id'),
                  'Rendition composition')
    for name in ('scaffold_sha256', 'arrangement_sha256'):
        _require_digest(composition[name], name, nullable=True)
    if composition['family_id'] is not None:
        _require_text(composition['family_id'], 128, 'Composition family')
    _require_keys(origin, ('kind', 'renderer'), 'Rendition origin')
    _require_choice(origin['kind'], ('imported_audio', 'declared_external_render'), 'rendition origin')
    if origin['kind'] == 'imported_audio':
        if origin['renderer'] is not None:
            raise ValueError('Imported audio cannot declare a renderer')
    else:
        renderer = origin['renderer']
        _require_keys(renderer, ('component_id', 'component_revision', 'request_sha256',
                                 'result_sha256'), 'Declared renderer')
        _require_text(renderer['component_id'], 256, 'Renderer component')
        _require_text(renderer['component_revision'], 512, 'Renderer revision')
        for name in ('request_sha256', 'result_sha256'):
            _require_digest(renderer[name], name)
    _require_keys(derivation, ('kind', 'parent'), 'Rendition derivation')
    _require_choice(derivation['kind'], ('import', 'render', 'revoice', 'mixdown', 'processing'),
                    'rendition derivation')
    if derivation['kind'] in ('import', 'render'):
        expected_origin = 'imported_audio' if derivation['kind'] == 'import' else 'declared_external_render'
        if derivation['parent'] is not None or origin['kind'] != expected_origin:
            raise ValueError('Root rendition derivation must match its origin')
    else:
        _require_digest(derivation['parent'], 'Parent rendition')


def _validate_record_structure(record):
    from source_evidence import _require_keys, _require_text
    from sound_design_cues import _require_digest, _require_choice, _validate_book
    _require_keys(record, ('schema', 'source', 'composition', 'origin', 'derivation',
                           'master', 'stems', 'rendition_id'), 'Sound rendition')
    if type(record['schema']) is not int or record['schema'] != 1:
        raise ValueError('Unsupported sound rendition schema')
    source = record['source']
    _require_keys(source, ('cue_id', 'version', 'book'), 'Rendition source')
    _require_text(source['cue_id'], 128, 'Source cue identity')
    _require_digest(source['version'], 'Source cue version')
    _validate_book(source['book'])
    validate_sound_rendition_definition(record['composition'], record['origin'], record['derivation'])
    _validate_artifact(record['master'])
    master_rate = record['master']['canonical']['frame_rate']
    stems = record['stems']
    if not isinstance(stems, list) or len(stems) > MAX_STEMS:
        raise ValueError('Rendition stem count exceeds its limit')
    seen = set()
    for stem in stems:
        _require_keys(stem, ('stem_id', 'label', 'artifact'), 'Rendition stem')
        _require_text(stem['stem_id'], 128, 'Stem identity')
        _require_text(stem['label'], 256, 'Stem label')
        if stem['stem_id'] in seen:
            raise ValueError('Rendition repeats a stem identity')
        seen.add(stem['stem_id'])
        _validate_artifact(stem['artifact'], master_rate)
    _require_digest(record['rendition_id'], 'Rendition identity')


def _get_record_seed(record):
    return {key: value for key, value in record.items() if key != 'rendition_id'}


def _get_record_digest(record):
    return hashlib.sha256(get_metadata_json_bytes(_get_record_seed(record),
                           MAX_RENDITION_BYTES, 'Sound rendition')).hexdigest()


def get_sound_rendition_record(source, composition, origin, derivation, master, stems):
    """Construct detached identity from bounded facts, without authenticating them."""
    record = dict(schema=1, source=source, composition=composition, origin=origin,
                  derivation=derivation, master=master, stems=stems, rendition_id='0' * 64)
    _validate_record_structure(record)
    # Joint byte accounting precedes copying or fingerprinting any proposed data.
    get_metadata_json_bytes(record, MAX_RENDITION_BYTES, 'Sound rendition')
    record['rendition_id'] = _get_record_digest(record)
    return json.loads(get_metadata_json_bytes(record, MAX_RENDITION_BYTES, 'Sound rendition'))


def get_sound_rendition_ledger_bytes(document):
    """Validate bounded immutable records and their earlier-parent ordering."""
    from source_evidence import _require_keys, _require_text
    from sound_design_cues import _require_digest
    _require_keys(document, ('schema', 'book_id', 'records', 'keep'), 'Rendition ledger')
    if type(document['schema']) is not int or document['schema'] != 1:
        raise ValueError('Unsupported rendition ledger schema')
    _require_text(document['book_id'], 1024, 'Rendition book identity')
    records, keep = document['records'], document['keep']
    if not isinstance(records, list) or len(records) > MAX_RENDITIONS:
        raise ValueError('Rendition history exceeds its record limit')
    if not isinstance(keep, list) or len(keep) > MAX_RENDITIONS:
        raise ValueError('Kept renditions exceed their count limit')
    for record in records:
        _validate_record_structure(record)
    for identity in keep:
        _require_digest(identity, 'Kept rendition identity')
    raw = get_metadata_json_bytes(document, MAX_RENDITION_BYTES, 'Rendition ledger')
    known = {}
    for record in records:
        identity, parent = record['rendition_id'], record['derivation']['parent']
        if identity in known or record['source']['book']['book_id'] != document['book_id']:
            raise ValueError('Rendition history repeats an identity or belongs to another book')
        if parent is not None:
            if parent not in known or record['source'] != known[parent]['source']:
                raise ValueError('Rendition requires an earlier parent with the exact same source')
        if identity != _get_record_digest(record):
            raise ValueError('Rendition fingerprint differs')
        known[identity] = record
    if len(set(keep)) != len(keep) or any(identity not in known for identity in keep):
        raise ValueError('Kept rendition identities must be unique retained records')
    return raw


def get_sound_rendition_ledger(raw):
    """Parse one strict saved companion; invalid metadata never becomes empty history."""
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_RENDITION_BYTES:
        raise ValueError('Rendition ledger exceeds its byte limit or is empty')
    def refuse_nonfinite(_value):
        raise ValueError('Nonfinite rendition metadata is not supported')
    try:
        document = json.loads(raw.decode('utf-8'), object_pairs_hook=get_unique_json_object,
                              parse_constant=refuse_nonfinite)
        get_sound_rendition_ledger_bytes(document)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise ValueError('Rendition metadata is invalid; existing files were preserved') from None
    return document


def validate_sound_rendition_sources(document, registry):
    """Bind historical source facts to retained cue versions, never today's text."""
    from sound_design_registry import get_sound_design_registry_bytes
    get_sound_rendition_ledger_bytes(document)
    get_sound_design_registry_bytes(registry)
    if document['book_id'] != registry['book']['book_id']:
        raise ValueError('Rendition and cue history belong to different books')
    versions = {row['version']: row for row in registry['versions']}
    for record in document['records']:
        source = record['source']
        version = versions.get(source['version'])
        if (version is None or version['cue_id'] != source['cue_id']
                or version['recipe']['book'] != source['book']):
            raise ValueError('Rendition source does not match its retained cue revision')


def validate_sound_rendition_consumers(document, registry):
    """Verify the retained cue -> rendition -> earlier source-cue closure.

    Identical PCM with different provenance remains a different consuming cue.
    These metadata checks do not replace verified asset descriptors on media use.
    """
    validate_sound_rendition_sources(document, registry)
    records = {row['rendition_id']: row for row in document['records']}
    positions = {row['version']: index for index, row in enumerate(registry['versions'])}
    for index, cue in enumerate(registry['versions']):
        if cue['schema'] != 2:
            continue
        record = records.get(cue['recipe']['rendition_id'])
        if (record is None or record['source']['cue_id'] != cue['cue_id']
                or positions[record['source']['version']] >= index):
            raise ValueError('Consuming cue requires a retained rendition with an earlier source cue')
        master = record['master']
        expected = {'asset_id': master['asset_id'], **{name: master['canonical'][name]
                    for name in ('sha256', 'frames', 'frame_rate', 'channels', 'sample_width')}}
        if cue['recipe']['asset'] != expected:
            raise ValueError('Consuming cue asset differs from its rendition master')


def capture_sound_design_companions(root, cue_relative, rendition_relative, *, expected_book_id=None):
    """Capture both optional companions under one existing book admission.

    Unknown/malformed or incomplete provenance is refused before callers replace
    or clear either companion. Exact captured bytes are preserved across copies.
    """
    from book_state_transaction import is_book_state_admitted
    from project_scope_files import capture_scope_file
    from sound_design_registry import get_sound_design_registry, MAX_REGISTRY_BYTES
    if not is_book_state_admitted(root):
        raise ValueError('Book admission is required for sound-design companions')
    _cue_proof, cue_raw = capture_scope_file(root, cue_relative,
        limit=MAX_REGISTRY_BYTES, include_bytes=True)
    _ledger_proof, ledger_raw = capture_scope_file(root, rendition_relative,
        limit=MAX_RENDITION_BYTES, include_bytes=True)
    cues = get_sound_design_registry(cue_raw) if cue_raw is not None else None
    ledger = get_sound_rendition_ledger(ledger_raw) if ledger_raw is not None else None
    if expected_book_id is not None:
        if ((cues is not None and cues['book']['book_id'] != expected_book_id)
                or (ledger is not None and ledger['book_id'] != expected_book_id)):
            raise ValueError('Sound-design companions belong to another book; existing files were preserved')
    if ledger is None:
        if cues is not None and any(row['schema'] == 2 for row in cues['versions']):
            raise ValueError('Consuming cue history requires its retained rendition ledger')
    elif cues is None:
        if ledger['records']:
            raise ValueError('Rendition history requires its retained cue source')
    else:
        validate_sound_rendition_consumers(ledger, cues)
    return cue_raw, ledger_raw


def get_required_sound_rendition_artifacts(document, registry, rendition_ids):
    """Resolve bounded derivation and source-cue ancestry into exact asset facts."""
    from sound_design_cues import _require_digest
    validate_sound_rendition_consumers(document, registry)
    if not isinstance(rendition_ids, list) or len(rendition_ids) > MAX_RENDITIONS:
        raise ValueError('Required rendition count exceeds its limit')
    for identity in rendition_ids:
        _require_digest(identity, 'Required rendition identity')
    records = {row['rendition_id']: row for row in document['records']}
    cues = {row['version']: row for row in registry['versions']}
    seen, artifacts, pending = set(), {}, list(rendition_ids)
    while pending:
        identity = pending.pop()
        if identity in seen:
            continue
        if identity not in records:
            raise ValueError('Required rendition is absent from retained history')
        seen.add(identity); record = records[identity]
        parent = record['derivation']['parent']
        if parent is not None:
            pending.append(parent)
        source_cue = cues[record['source']['version']]
        if source_cue['schema'] == 2:
            pending.append(source_cue['recipe']['rendition_id'])
        for artifact in [record['master'], *[stem['artifact'] for stem in record['stems']]]:
            facts = {key: artifact[key] for key in ('asset_id', 'source', 'canonical')}
            asset_id = facts['asset_id']
            if asset_id in artifacts and artifacts[asset_id] != facts:
                raise ValueError('Rendition ancestry has contradictory immutable asset facts')
            artifacts[asset_id] = facts
    return json.loads(get_metadata_json_bytes(list(artifacts.values()), MAX_RENDITION_BYTES,
                                               'Required rendition artifacts'))


def get_sound_rendition_ledger_edit(document, record):
    """Append one immutable record without silently replacing an older identity."""
    get_sound_rendition_ledger_bytes(document)
    _validate_record_structure(record)
    if any(row['rendition_id'] == record['rendition_id'] for row in document['records']):
        existing = next(row for row in document['records'] if row['rendition_id'] == record['rendition_id'])
        if existing != record:
            raise ValueError('Rendition identity already names different facts')
        return get_sound_rendition_ledger(get_sound_rendition_ledger_bytes(document))
    candidate = {**document, 'records': [*document['records'], record]}
    return get_sound_rendition_ledger(get_sound_rendition_ledger_bytes(candidate))


def get_sound_rendition_keep_edit(document, rendition_id, keep):
    from sound_design_cues import _require_digest
    get_sound_rendition_ledger_bytes(document)
    _require_digest(rendition_id, 'Kept rendition identity')
    if type(keep) is not bool:
        raise ValueError('Keep requires an explicit boolean')
    if not any(row['rendition_id'] == rendition_id for row in document['records']):
        raise ValueError('Rendition is absent from retained history')
    selected = set(document['keep'])
    if keep:
        selected.add(rendition_id)
    else:
        selected.discard(rendition_id)
    candidate = {**document, 'keep': sorted(selected)}
    return get_sound_rendition_ledger(get_sound_rendition_ledger_bytes(candidate))
