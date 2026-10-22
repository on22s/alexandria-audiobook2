"""Snapshot-bound word comparison against the current book's source."""
import copy
import hashlib
import json
from functools import lru_cache
from pathlib import Path

from generation_completion import get_generation_input
from generate_script import get_preprocessed_source
from text_diff import word_diff, get_comparison_words
from three_pass_generate import prepare_source_text


def _get_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


@lru_cache(maxsize=1)
def get_prepared_source(source, strip_front_matter):
    prepared, _report = get_preprocessed_source(source, strip_front_matter=strip_front_matter)
    prepared, _audit = prepare_source_text(prepared)
    return prepared


def get_source_preparation(state):
    """Resolve the selected prose and its saved preparation policy once."""
    if not isinstance(state, dict):
        raise ValueError('Invalid book state')
    input_path = state.get('input_file_path')
    if not isinstance(input_path, str) or not input_path:
        raise ValueError('No original source is selected for this book.')
    if Path(input_path).suffix.lower() == '.json':
        raise ValueError('The selected input is script JSON, not original source prose.')
    strip_front_matter = None
    generation_path = state.get('script_generation_input_file')
    if generation_path:
        if generation_path != input_path:
            raise ValueError('Saved generation settings belong to another source; original source is unavailable.')
        options = state.get('script_generation_options')
        if not isinstance(options, dict) or type(options.get('strip_front_matter')) is not bool:
            raise ValueError('Source preparation settings are unavailable.')
        strip_front_matter = options['strip_front_matter']
    return input_path, strip_front_matter


def get_selected_source_words(source, source_sha256, strip_front_matter, proof):
    """Validate an explicit source range before using its word projection."""
    required = {'version', 'word_policy', 'source_text_sha256', 'source_word_count',
                'retained_start_word', 'retained_end_word', 'script_start_entry',
                'input_sha256', 'strip_front_matter'}
    if (not isinstance(proof, dict) or set(proof) != required
            or type(proof['version']) is not int or proof['version'] != 1
            or proof['word_policy'] != 'AA2_text_diff_v1'
            or proof['input_sha256'] != source_sha256
            or proof['source_text_sha256'] != hashlib.sha256(source.encode('utf-8')).hexdigest()
            or type(proof['strip_front_matter']) is not type(strip_front_matter)
            or proof['strip_front_matter'] != strip_front_matter
            or any(type(proof[key]) is not int for key in ('source_word_count',
                'retained_start_word', 'retained_end_word', 'script_start_entry'))
            or proof['script_start_entry'] < 1):
        raise ValueError('Selected source-scope authority is missing or stale.')
    words = get_comparison_words(source)
    start, end = proof['retained_start_word'], proof['retained_end_word']
    if not 0 < start < end == proof['source_word_count'] == len(words):
        raise ValueError('Selected source-scope range is invalid.')
    # This is explicitly a word projection, not altered source prose or a
    # punctuation certificate. Original bytes and prepared text stay bound.
    return ' '.join(words[start:end])


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
        'script_generation_input_file', 'script_generation_options', '_source_scope')}
    source_sha256 = None
    source = None
    reason = None
    scope = 'current source'
    try:
        input_path, strip_front_matter = get_source_preparation(state)
        source, source_sha256 = get_generation_input(input_path)
        if strip_front_matter is not None:
            source = get_prepared_source(source, strip_front_matter)
            scope = 'current source after generation preprocessing'
        if '_source_scope' in state:
            source = get_selected_source_words(source, source_sha256, strip_front_matter,
                                               state['_source_scope'])
            scope = 'verified selected source word range'
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
