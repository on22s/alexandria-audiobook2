"""Snapshot-bound word comparison against the current book's source."""
import copy
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from generation_completion import get_generation_input
from generate_script import get_preprocessed_source
from text_diff import word_diff
from three_pass_generate import prepare_source_text


def _get_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


@lru_cache(maxsize=1)
def _get_prepared_source(source, strip_front_matter):
    prepared, _report = get_preprocessed_source(source, strip_front_matter=strip_front_matter)
    prepared, _audit = prepare_source_text(prepared)
    return prepared


@lru_cache(maxsize=1)
def _get_cached_diff(snapshot, source, entries_json):
    return word_diff(source, json.loads(entries_json))


def get_source_integrity(state, script_bytes, entries, *, comparison='editor chunks'):
    """Compare copied rows; callers serialize native book/chunk reads themselves.

    A successful comparison is against the *current* selected source, not a
    proof of the original generation input or the words in existing WAVs.
    """
    if not isinstance(state, dict):
        raise ValueError('Invalid book state')
    if (not isinstance(entries, list) or any(not isinstance(row, dict)
            or not isinstance(row.get('text', ''), str) for row in entries)):
        raise ValueError('Invalid comparison rows')
    input_path = state.get('input_file_path')
    identity = {key: state.get(key) for key in (
        'active_book_id', 'book_generation', 'input_file_path',
        'script_generation_input_file', 'script_generation_options')}
    source_sha256 = None
    source = None
    reason = None
    scope = 'current source'
    try:
        if not isinstance(input_path, str) or not input_path:
            raise ValueError('No original source is selected for this book.')
        source, source_sha256 = get_generation_input(input_path)
        if Path(input_path).suffix.lower() == '.json':
            raise ValueError('The selected input is script JSON, not original source prose.')
        generation_path = state.get('script_generation_input_file')
        if generation_path:
            if generation_path != input_path:
                raise ValueError('Saved generation settings belong to another source; original source is unavailable.')
            options = state.get('script_generation_options')
            if not isinstance(options, dict) or type(options.get('strip_front_matter')) is not bool:
                raise ValueError('Source preparation settings are unavailable.')
            source = _get_prepared_source(source, options['strip_front_matter'])
            scope = 'current source after generation preprocessing'
    except (OSError, ValueError, UnicodeError) as exc:
        reason = str(exc)
        source = None
    payload = {'identity': identity, 'source_sha256': source_sha256,
               'script_sha256': hashlib.sha256(script_bytes).hexdigest(),
               'entries': entries, 'comparison': comparison}
    snapshot = hashlib.sha256(_get_json(payload).encode('utf-8')).hexdigest()
    result = {'snapshot': snapshot, 'comparison': comparison, 'source_scope': scope,
              'source_name': Path(input_path).name if isinstance(input_path, str) else None}
    if source is None:
        return {**result, 'status': 'unavailable', 'reason': reason,
                'hunks': [], 'totals': {}, 'chunks_total': 0}
    diff = copy.deepcopy(_get_cached_diff(snapshot, source, _get_json(entries)))
    return {**result, **diff, 'status': 'differences' if diff['hunks'] else 'verified'}
