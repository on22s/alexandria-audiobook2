"""Validate saved candidate metadata and keep its exact PCM descriptor pinned."""
from contextlib import contextmanager
import json
import math
import os
from pathlib import Path
import re
import time

from audio_take_history import _open_regular, _version, _ensure_plain_path
from audio_source_io import read_bounded_audio_hash
from book_state_transaction import get_snapshot_digest
from json_metadata import get_unique_json_object
from project_scope_files import capture_scope_file
from sound_design_assets import MAX_TOTAL_BYTES
from sound_design_cues import (_require_digest, _require_int, _validate_book,
                              _validate_selection, _validate_plan, MAX_CUES, MAX_DIALOGUE)
from sound_design_staging import DIRECTORY
from source_evidence import _require_keys
from tts import ensure_audio_export_active

MAX_CANDIDATE_METADATA = 8 * 1024**2


def get_sound_candidate_document(raw, expected_revision):
    """Reject corrupt/forged shapes even if their checksum was recomputed."""
    _require_digest(expected_revision, 'Expected candidate revision')
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_CANDIDATE_METADATA:
        raise ValueError('Sound candidate metadata exceeds its allowance')
    try:
        document = json.loads(raw, object_pairs_hook=get_unique_json_object,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite candidate value')))
    except (UnicodeError, RecursionError) as error:
        raise ValueError('Invalid sound candidate metadata') from error
    _require_keys(document, ('schema','candidate_revision','binding','pcm_file'), 'Sound candidate')
    if type(document['schema']) is not int or document['schema'] != 1 or document['pcm_file'] != 'mixed.pcm':
        raise ValueError('Unsupported sound candidate format')
    _require_digest(document['candidate_revision'], 'Candidate revision')
    binding = document['binding']
    _require_keys(binding, ('context_revision','book','registry_revision','cue_versions',
                            'dialogue','pcm_plan','mix'), 'Candidate binding')
    _require_digest(binding['context_revision'], 'Candidate context')
    _require_digest(binding['registry_revision'], 'Candidate registry', nullable=True)
    _validate_book(binding['book'])
    versions, dialogue = binding['cue_versions'], binding['dialogue']
    if not isinstance(versions, list) or len(versions) > MAX_CUES:
        raise ValueError('Invalid candidate cue count')
    for version in versions: _require_digest(version, 'Candidate cue version')
    if len(set(versions)) != len(versions):
        raise ValueError('Duplicate candidate cue version')
    if not isinstance(dialogue, list) or not 1 <= len(dialogue) <= MAX_DIALOGUE:
        raise ValueError('Invalid candidate dialogue count')
    for row in dialogue: _validate_selection(row)
    if len({row['chunk_uid'] for row in dialogue}) != len(dialogue):
        raise ValueError('Duplicate candidate dialogue identity')
    plan, mix = binding['pcm_plan'], binding['mix']
    _validate_plan(plan, len(dialogue))
    _require_keys(mix, ('schema','projection_revision','scope','frames','frame_rate','channels',
        'sample_width','bytes','sha256','dry_scope_sha256','dry_store_sha256','logical_staged_bytes',
        'prequantization_peak','postquantization_peak'), 'Candidate mix')
    if type(mix['schema']) is not int or mix['schema'] != 1:
        raise ValueError('Unsupported candidate mix schema')
    for key in ('projection_revision','sha256','dry_scope_sha256','dry_store_sha256'):
        _require_digest(mix[key], key)
    for key in ('frames','frame_rate','channels','sample_width'):
        if type(mix[key]) is not int or mix[key] != plan[key]:
            raise ValueError('Candidate mix geometry disagrees with full frame plan')
    if (not isinstance(mix['scope'], list) or len(mix['scope']) != 2
            or any(type(value) is not int for value in mix['scope'])
            or mix['scope'] != [0, plan['frames']]):
        raise ValueError('Candidate must contain the full mix')
    _require_int(mix['bytes'], 1, MAX_TOTAL_BYTES, 'Candidate PCM bytes')
    if mix['bytes'] != plan['frames'] * plan['channels'] * plan['sample_width']:
        raise ValueError('Candidate byte count disagrees with frame plan')
    _require_int(mix['logical_staged_bytes'], mix['bytes'], MAX_TOTAL_BYTES, 'Candidate staging')
    for key in ('prequantization_peak','postquantization_peak'):
        if type(mix[key]) not in (int,float) or not math.isfinite(mix[key]) or not 0 <= mix[key] <= 1:
            raise ValueError('Invalid candidate peak')
    if document['candidate_revision'] != expected_revision or get_snapshot_digest(binding) != expected_revision:
        raise ValueError('Sound candidate revision changed')
    return document


def validate_sound_candidate_stage(stage_name):
    if not isinstance(stage_name, str) or re.fullmatch(r'render-[A-Za-z0-9_-]{1,128}', stage_name) is None:
        raise ValueError('Invalid sound candidate stage identity')


@contextmanager
def ensure_sound_candidate_open(root, stage_name, expected_revision, *, cancel_check=None):
    """Read-only exact-byte admission, not current-book approval or publication.

    Consumers must use this descriptor, not reopen its pathname. Successful exit
    is part of validation; callers publish only afterwards. No model is loaded.
    """
    validate_sound_candidate_stage(stage_name)
    root = Path(root).absolute()
    relative = f'{DIRECTORY}/{stage_name}/candidate.json'
    deadline = time.monotonic() + 300
    def check():
        ensure_audio_export_active(cancel_check)
        if time.monotonic() >= deadline: raise TimeoutError('Candidate verification exceeded its deadline')
    proof, raw = capture_scope_file(root, relative, include_bytes=True,
                                   limit=MAX_CANDIDATE_METADATA, cancel_check=check)
    document = get_sound_candidate_document(raw, expected_revision)
    path = root / DIRECTORY / stage_name / 'mixed.pcm'
    stream, before = _open_regular(path, root, document['binding']['mix']['bytes'])
    with stream:
        def fence():
            check()
            mix = document['binding']['mix']
            if read_bounded_audio_hash(stream, mix['bytes'], check) != (mix['sha256'], mix['bytes']):
                raise ValueError('Candidate PCM changed')
            _ensure_plain_path(path, root)
            if _version(os.fstat(stream.fileno())) != _version(before) or _version(path.lstat()) != _version(before):
                raise ValueError('Candidate PCM was replaced')
            current, _ = capture_scope_file(root, relative, limit=MAX_CANDIDATE_METADATA, cancel_check=check)
            if current != proof: raise ValueError('Candidate metadata changed')
        fence(); stream.seek(0)
        yield document, stream
        fence()
