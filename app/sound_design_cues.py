"""Strict immutable cue recipes and read-only, sample-exact placement.

This is metadata, not media admission or approval. Callers must capture current
book/script, selected audio and PCM geometry together before projecting cues.
No content search or automatic relocation is permitted when those proofs change.
"""
import copy
import hashlib
import json
import re

from audio_pcm_producer import get_promoted_pcm_shape
from source_evidence import _require_keys, _require_text

MAX_CUES = 128
MAX_DIALOGUE = 100000
MAX_CUE_BYTES = 2 * 1024**2
MAX_RATE = 192000
MAX_SECONDS = 24 * 60 * 60


def _require_int(value, minimum, maximum, label):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f'{label} is out of bounds')


def _require_digest(value, label, *, nullable=False):
    if nullable and value is None:
        return
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{64}', value) is None:
        raise ValueError(f'{label} must be a lowercase SHA-256 digest')


def _require_choice(value, choices, label):
    if not isinstance(value, str) or value not in choices:
        raise ValueError(f'Invalid {label}')


def _validate_book(book):
    _require_keys(book, ('book_id', 'source_sha256', 'script_sha256'), 'Cue book')
    _require_text(book['book_id'], 1024, 'Book identity')
    for key in ('source_sha256', 'script_sha256'):
        _require_digest(book[key], key)


def _validate_selection(row):
    _require_keys(row, ('chunk_uid', 'generation_sha256', 'audio_sha256', 'audio_revision'),
                  'Dialogue selection')
    _require_text(row['chunk_uid'], 128, 'Chunk identity')
    for key in ('generation_sha256', 'audio_sha256'):
        _require_digest(row[key], key)
    revision = row['audio_revision']
    if revision is not None:
        _require_text(revision, 128, 'Audio revision')


def _validate_cue_schema(schema):
    if type(schema) is not int or schema not in (1, 2):
        raise ValueError('Unsupported cue schema')


def _validate_recipe(recipe, schema=1):
    _validate_cue_schema(schema)
    fields = ('book', 'selection', 'boundary', 'offset_frames', 'frame_rate',
              'kind', 'asset', 'duration', 'mix')
    _require_keys(recipe, fields + (('rendition_id',) if schema == 2 else ()), 'Cue recipe')
    if schema == 2:
        _require_digest(recipe['rendition_id'], 'Consumed rendition identity')
    _validate_book(recipe['book'])
    _validate_selection(recipe['selection'])
    _validate_cue_placement(recipe)
    asset = recipe['asset']
    _require_keys(asset, ('asset_id', 'sha256', 'frames', 'frame_rate', 'channels',
                         'sample_width'), 'Selected cue asset')
    _require_text(asset['asset_id'], 128, 'Asset identity')
    _require_digest(asset['sha256'], 'Asset fingerprint')
    _require_int(asset['frame_rate'], 1, MAX_RATE, 'Asset rate')
    _require_int(asset['frames'], 1, asset['frame_rate'] * MAX_SECONDS, 'Asset frames')
    _require_int(asset['channels'], 1, 2, 'Asset channels')
    if type(asset['sample_width']) is not int or asset['sample_width'] not in (1, 2, 4):
        raise ValueError('Invalid asset sample width')
    _validate_cue_duration_mix(recipe)


def validate_sound_design_cue_parameters(parameters):
    """Validate authored fields before acquiring book/asset/source authority."""
    _require_keys(parameters, ('boundary','offset_frames','frame_rate','kind','duration','mix'),
                  'Authored cue parameters')
    _validate_cue_placement(parameters)
    _validate_cue_duration_mix(parameters)


def _validate_cue_placement(recipe):
    _require_choice(recipe['boundary'], ('start', 'end'), 'cue boundary')
    _require_int(recipe['frame_rate'], 1, MAX_RATE, 'Authored frame rate')
    _require_int(recipe['offset_frames'], -MAX_RATE * MAX_SECONDS,
                 MAX_RATE * MAX_SECONDS, 'Cue offset')
    _require_choice(recipe['kind'], ('effect', 'ambience'), 'cue kind')


def _validate_cue_duration_mix(recipe):
    duration = recipe['duration']
    _require_keys(duration, ('mode', 'frames', 'loop_crossfade_frames'), 'Cue duration')
    _require_choice(duration['mode'], ('native', 'fixed'), 'duration mode')
    if duration['mode'] == 'native':
        if duration['frames'] is not None or duration['loop_crossfade_frames'] is not None:
            raise ValueError('Native duration cannot extend or loop an asset')
    else:
        _require_int(duration['frames'], 1, recipe['frame_rate'] * MAX_SECONDS, 'Cue frames')
        if duration['loop_crossfade_frames'] is not None:
            if recipe['kind'] != 'ambience':
                raise ValueError('Only explicitly selected ambience may loop')
            _require_int(duration['loop_crossfade_frames'], 1,
                         recipe['frame_rate'] * 10, 'Loop crossfade')
    mix = recipe['mix']
    _require_keys(mix, ('gain_db', 'duck_db', 'fade_in_frames', 'fade_out_frames'), 'Cue mix')
    for key, low, high in (('gain_db', -96, 24), ('duck_db', -60, 0)):
        value = mix[key]
        if type(value) not in (int, float) or not low <= value <= high:
            raise ValueError(f'Invalid cue {key}')
    for key in ('fade_in_frames', 'fade_out_frames'):
        _require_int(mix[key], 0, recipe['frame_rate'] * MAX_SECONDS, key)


def _get_version_bytes(document):
    # All leaves and collection sizes have been checked before encoding.
    return json.dumps(document, ensure_ascii=False, allow_nan=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def get_sound_design_duration_error(cue, native_frames):
    """Share asset-dependent timing checks for validated authoring/projection."""
    duration = cue['duration']
    frames = native_frames if duration['mode'] == 'native' else duration['frames']
    crossfade = duration['loop_crossfade_frames']
    if crossfade is not None and (crossfade >= native_frames or frames <= native_frames):
        return 'invalid_loop_extension'
    if frames > native_frames and crossfade is None:
        return 'explicit_loop_required'
    if cue['mix']['fade_in_frames'] + cue['mix']['fade_out_frames'] > frames:
        return 'fades_exceed_duration'
    return None


def _get_projection_revision(versions, book, dialogue, pcm_plan, scope, check_active):
    # Stream the already validated snapshot once, not once per cue. A preceding
    # source or pause can move an unchanged anchor, so cue versions alone are
    # insufficient to fence a preview, cache or later approval.
    seed = dict(schema=1, versions=[row['version'] for row in versions], book=book,
                dialogue=dialogue, pcm_plan=pcm_plan, scope=scope)
    digest, since_check = hashlib.sha256(), 0
    for part in json.JSONEncoder(ensure_ascii=False, allow_nan=False, sort_keys=True,
                                 separators=(',', ':')).iterencode(seed):
        raw = part.encode('utf-8')
        digest.update(raw); since_check += len(raw)
        if since_check >= 65536:
            if check_active is not None:
                check_active()
            since_check = 0
    return digest.hexdigest()


def get_sound_design_cue_version(cue_id, recipe, parent_version=None, *, schema=1):
    """Return a detached immutable revision; this does not approve any media."""
    _require_text(cue_id, 128, 'Cue identity')
    _require_digest(parent_version, 'Parent version', nullable=True)
    _validate_recipe(recipe, schema)
    seed = dict(schema=schema, cue_id=cue_id, parent_version=parent_version, recipe=recipe)
    return dict(copy.deepcopy(seed), version=hashlib.sha256(_get_version_bytes(seed)).hexdigest())


def validate_sound_design_cue_version_structure(document):
    """Bound all leaves before joint registry accounting, copying or hashing."""
    _require_keys(document, ('schema', 'cue_id', 'parent_version', 'recipe', 'version'), 'Cue version')
    _validate_cue_schema(document['schema'])
    _require_text(document['cue_id'], 128, 'Cue identity')
    _require_digest(document['parent_version'], 'Parent version', nullable=True)
    _validate_recipe(document['recipe'], document['schema'])
    _require_digest(document['version'], 'Cue version')


def validate_sound_design_cue_version(document):
    validate_sound_design_cue_version_structure(document)
    expected = get_sound_design_cue_version(document['cue_id'], document['recipe'],
                                          document['parent_version'], schema=document['schema'])
    if document['version'] != expected['version']:
        raise ValueError('Cue version fingerprint differs')


def _validate_plan(plan, count):
    _require_keys(plan, ('frames', 'frame_rate', 'channels', 'sample_width',
                         'source_frame_ranges', 'intentional_pause_frames'), 'Dialogue PCM plan')
    _require_int(plan['frame_rate'], 1, MAX_RATE, 'Dialogue rate')
    _require_int(plan['frames'], 1, plan['frame_rate'] * MAX_SECONDS, 'Dialogue frames')
    _require_int(plan['channels'], 1, 2, 'Dialogue channels')
    if type(plan['sample_width']) is not int or plan['sample_width'] not in (1, 2, 4):
        raise ValueError('Invalid dialogue sample width')
    for key, limit in (('source_frame_ranges', count), ('intentional_pause_frames', MAX_DIALOGUE)):
        rows = plan[key]
        if not isinstance(rows, list) or len(rows) > limit or (key == 'source_frame_ranges' and len(rows) != count):
            raise ValueError('Dialogue ranges do not match captured sources')
        last_start = 0
        for row in rows:
            if not isinstance(row, list) or len(row) != 2:
                raise ValueError('Invalid dialogue frame range')
            start, end = row
            _require_int(start, last_start, plan['frames'], 'Frame range start')
            _require_int(end, start, plan['frames'], 'Frame range end')
            last_start = start


def get_sound_design_cue_plan(versions, book, dialogue, pcm_plan, *, scope=None, check_active=None):
    """Project explicitly supplied revisions onto one captured dry PCM plan.

    `dialogue` is in the writer's source order, after trimming. Fingerprints must
    come from the same captured selected sources used by that writer. Historical
    revisions are not a license to search text or silently rebind a lost UID.
    """
    _validate_book(book)
    if not isinstance(versions, list) or len(versions) > MAX_CUES:
        raise ValueError('Cue count exceeds the supported limit')
    if not isinstance(dialogue, list) or not 1 <= len(dialogue) <= MAX_DIALOGUE:
        raise ValueError('Dialogue count exceeds the supported limit')
    selected = {}
    for row in dialogue:
        if check_active is not None:
            check_active()
        _validate_selection(row)
        if row['chunk_uid'] in selected:
            raise ValueError('Dialogue chunk identities must be unique')
        selected[row['chunk_uid']] = row
    _validate_plan(pcm_plan, len(dialogue))
    if scope is None:
        scope = [0, pcm_plan['frames']]
    if not isinstance(scope, list) or len(scope) != 2:
        raise ValueError('Invalid cue scope')
    _require_int(scope[0], 0, pcm_plan['frames'] - 1, 'Scope start')
    _require_int(scope[1], scope[0] + 1, pcm_plan['frames'], 'Scope end')
    seen, size = set(), 0
    for document in versions:
        if check_active is not None:
            check_active()
        validate_sound_design_cue_version(document)
        if document['cue_id'] in seen:
            raise ValueError('Select exactly one version per cue')
        seen.add(document['cue_id'])
        size += len(_get_version_bytes(document))
        if size > MAX_CUE_BYTES:
            raise ValueError('Cue plan exceeds its byte limit')
    ranges = {row['chunk_uid']: span for row, span in zip(dialogue, pcm_plan['source_frame_ranges'])}
    results = []
    for document in versions:
        if check_active is not None:
            check_active()
        cue = document['recipe']
        row = dict(cue_id=document['cue_id'], version=document['version'], status='unresolved',
                   reason=None, start_frame=None, end_frame=None)
        reason = None
        if cue['book'] != book:
            reason = 'book_or_source_changed'
        elif selected.get(cue['selection']['chunk_uid']) != cue['selection']:
            reason = 'dialogue_selection_changed'
        elif cue['frame_rate'] != pcm_plan['frame_rate']:
            reason = 'output_clock_changed'
        if reason is not None:
            row.update(status='stale', reason=reason)
            results.append(row)
            continue
        span = ranges[cue['selection']['chunk_uid']]
        start = span[0 if cue['boundary'] == 'start' else 1] + cue['offset_frames']
        asset = cue['asset']
        layout = (pcm_plan['channels'], pcm_plan['frame_rate'], pcm_plan['sample_width'])
        if asset['channels'] > layout[0]:
            row['reason'] = 'asset_channels_require_review'
            results.append(row)
            continue
        shape = ((asset['channels'], asset['frame_rate'], asset['sample_width']), asset['frames'])
        native = get_promoted_pcm_shape(shape, layout, reject_impossible_channels=True)[1]
        duration = cue['duration']
        frames = native if duration['mode'] == 'native' else duration['frames']
        duration_error = get_sound_design_duration_error(cue, native)
        if span[0] == span[1]:
            reason = 'empty_dialogue_source'
        elif duration_error is not None:
            reason = duration_error
        elif start < 0 or start + frames > pcm_plan['frames']:
            reason = 'outside_dialogue_timeline'
        elif start + frames <= scope[0] or start >= scope[1]:
            row.update(status='outside_scope')
        elif start < scope[0] or start + frames > scope[1]:
            reason = 'crosses_scope_boundary'
        else:
            row.update(status='current', start_frame=start, end_frame=start + frames)
        if reason is not None:
            row['reason'] = reason
        results.append(row)
    revision = _get_projection_revision(versions, book, dialogue, pcm_plan, scope, check_active)
    if check_active is not None:
        check_active()
    return dict(frame_rate=pcm_plan['frame_rate'], frames=pcm_plan['frames'], scope=list(scope),
                projection_revision=revision,
                can_project=all(row['status'] in ('current', 'outside_scope') for row in results), cues=results)
