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


SOURCE_REVIEW_PAGE_CHARS = 8192


def _require_review_offset(offset):
    if type(offset) is not int or not 0 <= offset <= 2**53 - 1:
        raise ValueError('Invalid source comparison offset')


def _get_review_fragments(report):
    if report['status'] == 'unavailable':
        reason = report.get('reason')
        if reason is not None and not isinstance(reason, str):
            raise ValueError('Invalid source comparison reason')
        yield reason or 'Original source comparison is unavailable.'
    elif report['status'] == 'differences':
        for index, hunk in enumerate(report['hunks']):
            if index:
                yield '\n\n'
            if any(not isinstance(hunk.get(key), str) for key in
                   ('source_before', 'source_words', 'source_after', 'script_words')):
                raise ValueError('Invalid source comparison text')
            yield 'Source: '
            yield hunk['source_before']
            yield ' ['
            yield hunk['source_words']
            yield '] '
            yield hunk['source_after']
            yield '\nMixed dialogue: '
            yield hunk['script_words']
    elif report['status'] != 'verified':
        raise ValueError('Invalid source comparison status')


def get_source_integrity_page(report, offset=0):
    """Present the complete comparison by codepoint ranges, without flattening it.

    The existing diff still lives in the server cache. Only this page's text is
    copied or returned; neither pagination nor presentation changes consent's
    existing snapshot identity. An unavailable reason is paged in the same way.
    """
    _require_review_offset(offset)
    total, parts = 0, []
    for fragment in _get_review_fragments(report):
        start = total
        total += len(fragment)
        first, last = max(0, offset - start), min(len(fragment), offset + SOURCE_REVIEW_PAGE_CHARS - start)
        if first < last:
            parts.append(fragment[first:last])
    if offset > total:
        raise ValueError('Source comparison offset exceeds its text')
    page = ''.join(parts)
    end = offset + len(page)
    return dict(schema=1, status=report['status'], snapshot=report['snapshot'],
                page=page, offset=offset, total=total, page_size=SOURCE_REVIEW_PAGE_CHARS,
                next_offset=end if end < total else None)


def is_source_integrity_admitted(report, confirmation):
    """Require current explicit confirmation for differences or unavailable prose."""
    return ((confirmation is None and report['status'] == 'verified')
            or (isinstance(confirmation, str) and confirmation == report['snapshot']))


def _get_json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':'), allow_nan=False)


@lru_cache(maxsize=1)
def get_prepared_source(source, strip_front_matter):
    prepared, _report = get_preprocessed_source(source, strip_front_matter=strip_front_matter)
    prepared, _audit = prepare_source_text(prepared)
    return prepared


def get_saved_source_prose_path(data_dir, input_path, state):
    """Follow a loaded saved script's bindings to the prose it was built from.

    Loading a saved book selects the saved script JSON itself, so the original
    prose is named only by that saved script's binding chain. Each hop is
    verified against its own script bytes before its source path is trusted.
    """
    import os
    from project_scope_files import capture_scope_file
    from script_selection_binding import (MAX_SOURCE_BINDING_HOPS, MAX_BINDING_BYTES,
        _read_source_snapshot, _validated_script_entries, get_bound_source_path,
        parse_script_binding, saved_binding_matches)
    path = os.path.abspath(input_path)
    seen = set()
    for _hop in range(MAX_SOURCE_BINDING_HOPS + 1):
        if Path(path).suffix.lower() != '.json':
            return path
        key = os.path.normcase(path)
        if key in seen:
            raise ValueError("This saved script's original-source chain loops. Load a script made "
                             'directly from the original source, or re-import the source text.')
        seen.add(key)
        directory = os.path.dirname(path)
        _, raw_binding = capture_scope_file(directory, Path(path).stem + '.selection_binding.json',
                                            limit=MAX_BINDING_BYTES, include_bytes=True)
        if raw_binding is None:
            raise ValueError('This saved script has no recorded link to its original source. '
                             'Save the script again from the original source text to record one.')
        binding = parse_script_binding(raw_binding)
        _, script_raw = _read_source_snapshot(path, include_bytes=True, owning_root=data_dir)
        if (script_raw is None or not saved_binding_matches(
                binding, script_sha256=hashlib.sha256(script_raw).hexdigest(),
                entries=_validated_script_entries(script_raw))):
            raise ValueError("This saved script's original-source record is damaged. Re-import the "
                             'original source file and save the script again.')
        try:
            source_path = get_bound_source_path(
                data_dir, binding, active=False, selected=state.get('input_file_path'))
        except ValueError as error:
            raise ValueError("The saved script's original source file is missing or was renamed. "
                             'Re-import the original source under its original name, or save this '
                             'script again from the source to record a new link.') from error
        if Path(source_path).suffix.lower() != '.json':
            digest, _ = _read_source_snapshot(source_path, owning_root=data_dir)
            if digest != binding['source_sha256']:
                raise ValueError("The saved script's original source file has changed since the "
                                 'script was saved. Restore the original source bytes to compare.')
        path = os.path.abspath(source_path)
    raise ValueError("This saved script's original-source chain is longer than allowed. Load a "
                     'script made directly from the original source, or re-import the source text.')


def get_source_preparation(state, *, data_dir=None):
    """Resolve the selected prose and its saved preparation policy once."""
    if not isinstance(state, dict):
        raise ValueError('Invalid book state')
    input_path = state.get('input_file_path')
    if not isinstance(input_path, str) or not input_path:
        raise ValueError('No original source is selected for this book.')
    saved_script = Path(input_path).suffix.lower() == '.json'
    if saved_script:
        if data_dir is None:
            raise ValueError('The selected input is script JSON, not original source prose.')
        input_path = get_saved_source_prose_path(data_dir, input_path, state)
    strip_front_matter = None
    generation_path = state.get('script_generation_input_file')
    if generation_path:
        if generation_path != input_path:
            # Only a resolved saved script may carry another book's run
            # settings; every direct selection must still match exactly.
            if not saved_script:
                raise ValueError('Saved generation settings belong to another source; original source is unavailable.')
        else:
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


def get_source_integrity(state, script_bytes, entries, *, comparison='editor chunks', review_offset=None,
                         expected_source_sha256=None, data_dir=None):
    """Compare copied rows; callers serialize native book/chunk reads themselves.

    A successful comparison is against the *current* selected source, not a
    proof of the original generation input or the words in existing WAVs.
    """
    if review_offset is not None:
        _require_review_offset(review_offset)
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
        input_path, strip_front_matter = get_source_preparation(state, data_dir=data_dir)
        source, source_sha256 = get_generation_input(input_path)
        if expected_source_sha256 is not None and source_sha256 != expected_source_sha256:
            raise ValueError('Source changed during the comparison; refresh before relying on it.')
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
        report = {**result, 'status': 'unavailable', 'reason': reason,
                  'hunks': [], 'totals': {}, 'chunks_total': 0}
    else:
        diff = _get_cached_diff(snapshot, source, _get_json(entries))
        # The paged view reads the cached diff without copying its containers.
        # Legacy callers retain detached mutable reports as before.
        if review_offset is None:
            diff = copy.deepcopy(diff)
        report = {**result, **diff, 'status': 'differences' if diff['hunks'] else 'verified'}
    return report if review_offset is None else get_source_integrity_page(report, review_offset)
