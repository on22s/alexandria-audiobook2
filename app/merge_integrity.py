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


_SAVED_SOURCE_REASONS = {
    'missing_binding': (
        'This saved script has no recorded link to its original source. '
        'Save the script again from the original source text to record one.'
    ),
    'damaged_binding': (
        "This saved script's original-source record is damaged. Re-import the "
        'original source file and save the script again.'
    ),
    'outside_library': (
        'The selected input is script JSON outside the saved books library, '
        'not original source prose.'
    ),
    'missing_source': (
        "The saved script's original source file is missing or was renamed. "
        'Re-import the original source under its original name, or save this '
        'script again from the source to record a new link.'
    ),
    'changed_source': (
        "The saved script's original source file has changed since the "
        'script was saved. Restore the original source bytes to compare.'
    ),
    'cycle': (
        "This saved script's original-source chain loops. Load a script made "
        'directly from the original source, or re-import the source text.'
    ),
    'hop_limit': (
        "This saved script's original-source chain is longer than allowed. Load a "
        'script made directly from the original source, or re-import the source text.'
    ),
}


def _active_saved_script_expected_sha256(data_dir, input_path, state):
    """Verify the active binding's target path and return hop 0's expected digest.

    Checks the active binding against the loaded saved script path (not against
    live edited annotated_script.json bytes), so live Editor edits still surface
    as word differences while replacing the underlying saved library script is
    refused.
    """
    import os
    from project_scope_files import capture_scope_file
    from script_selection_binding import (
        BINDING_NAME, MAX_BINDING_BYTES, SavedSourceResolutionError,
        _path_digest, parse_script_binding)

    try:
        proof, raw = capture_scope_file(
            data_dir, BINDING_NAME, limit=MAX_BINDING_BYTES, include_bytes=True)
    except (OSError, ValueError) as error:
        raise SavedSourceResolutionError('damaged_binding', 'Active script binding cannot be read.') from error
    if not proof.get('exists') or raw is None:
        if state.get('script_binding_required') is True:
            raise SavedSourceResolutionError('missing_binding', 'Active script binding is missing.')
        return None
    try:
        binding = parse_script_binding(raw)
    except (ValueError, RecursionError) as error:
        raise SavedSourceResolutionError('damaged_binding', 'Active script binding is invalid.') from error
    abs_path = os.path.abspath(input_path)
    spellings = {input_path, abs_path, Path(input_path).as_posix(), Path(abs_path).as_posix()}
    if not any(_path_digest(spelling) == binding['source_path_sha256'] for spelling in spellings):
        raise SavedSourceResolutionError(
            'damaged_binding', 'Active script binding does not match the loaded saved script.')
    return binding['source_sha256']


def _get_saved_script_strip_front_matter(data_dir, origin):
    """Read a loaded saved book's own recorded strip_front_matter choice.

    Checks the bound script entries' literal policy first, then the saved
    book's .meta.json across resolved hops. Never borrows unrelated run
    settings from state['script_generation_options']. When no choice is
    recorded, returns False so universal preprocessing (publisher colophon
    and text normalization) runs without silently stripping web-novel front
    matter.
    """
    import os
    from literal_policy import get_literal_policy
    from project_scope_files import (
        MAX_SCOPE_METADATA_BYTES, MAX_SCRIPT_BYTES, capture_scope_file)
    from script_selection_binding import _validated_script_entries

    data_root = Path(os.path.abspath(data_dir))
    for hop in origin['hops']:
        script_abs = Path(os.path.abspath(hop['script_path']))
        script_rel = script_abs.relative_to(data_root).as_posix()
        try:
            _, script_raw = capture_scope_file(
                data_root, script_rel, limit=MAX_SCRIPT_BYTES, include_bytes=True)
            entries = _validated_script_entries(script_raw) if script_raw is not None else []
            policy = get_literal_policy(entries)
        except (OSError, ValueError, RecursionError) as error:
            raise ValueError('Source preparation settings are unavailable.') from error
        if policy is not None and 'strip_front_matter' in policy:
            return policy['strip_front_matter']
        meta_rel = (script_abs.parent / (script_abs.stem + '.meta.json')).relative_to(data_root).as_posix()
        try:
            meta_proof, meta_raw = capture_scope_file(
                data_root, meta_rel, limit=MAX_SCOPE_METADATA_BYTES, include_bytes=True)
        except (OSError, ValueError) as error:
            raise ValueError('Source preparation settings are unavailable.') from error
        if meta_proof.get('exists') and meta_raw is not None:
            try:
                meta = json.loads(meta_raw)
            except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError) as error:
                raise ValueError('Source preparation settings are unavailable.') from error
            if not isinstance(meta, dict):
                raise ValueError('Source preparation settings are unavailable.')
            if 'strip_front_matter' in meta:
                if type(meta['strip_front_matter']) is not bool:
                    raise ValueError('Source preparation settings are unavailable.')
                return meta['strip_front_matter']
    return False


def _get_source_preparation_details(state, *, data_dir=None):
    """Resolve the selected prose, preparation choice, and saved-script proof."""
    if not isinstance(state, dict):
        raise ValueError('Invalid book state')
    input_path = state.get('input_file_path')
    if not isinstance(input_path, str) or not input_path:
        raise ValueError('No original source is selected for this book.')
    saved_script = Path(input_path).suffix.lower() == '.json'
    if saved_script:
        if data_dir is None:
            raise ValueError('The selected input is script JSON, not original source prose.')
        from script_selection_binding import (
            SavedSourceResolutionError, _validate_saved_book_member,
            resolve_saved_book_origin)
        try:
            _validate_saved_book_member(data_dir, input_path)
            expected_sha = _active_saved_script_expected_sha256(data_dir, input_path, state)
            origin = resolve_saved_book_origin(
                data_dir, input_path, expected_script_sha256=expected_sha,
                require_terminal_file=True)
        except SavedSourceResolutionError as error:
            raise ValueError(_SAVED_SOURCE_REASONS[error.code]) from error
        strip_front_matter = _get_saved_script_strip_front_matter(data_dir, origin)
        return origin['source_path'], strip_front_matter, origin['source_sha256'], True
    strip_front_matter = None
    generation_path = state.get('script_generation_input_file')
    if generation_path:
        if generation_path != input_path:
            raise ValueError('Saved generation settings belong to another source; original source is unavailable.')
        options = state.get('script_generation_options')
        if not isinstance(options, dict) or type(options.get('strip_front_matter')) is not bool:
            raise ValueError('Source preparation settings are unavailable.')
        strip_front_matter = options['strip_front_matter']
    return input_path, strip_front_matter, None, False


def get_source_preparation(state, *, data_dir=None):
    """Resolve the selected prose and its saved preparation policy once."""
    input_path, strip_front_matter, _, _ = _get_source_preparation_details(state, data_dir=data_dir)
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
    saved_script = False
    strip_front_matter = None
    try:
        input_path, strip_front_matter, resolved_source_sha256, saved_script = (
            _get_source_preparation_details(state, data_dir=data_dir))
        try:
            source, source_sha256 = get_generation_input(
                input_path, owning_root=data_dir if saved_script else None)
        except FileNotFoundError as exc:
            if saved_script:
                raise ValueError(_SAVED_SOURCE_REASONS['missing_source']) from exc
            raise
        if resolved_source_sha256 is not None and source_sha256 != resolved_source_sha256:
            raise ValueError(_SAVED_SOURCE_REASONS['changed_source'])
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
    if saved_script:
        payload['resolved_strip_front_matter'] = strip_front_matter
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
