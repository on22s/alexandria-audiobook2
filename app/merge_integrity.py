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
        'Re-import the original source file and generate the script again from it to record one.'
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
    book's .meta.json across resolved hops. The resolver already read and
    parsed every hop while binding it, so only each hop's small metadata
    companion is read here. Never borrows unrelated run settings from
    state['script_generation_options']. None means no hop records a choice:
    the comparison then tries both preparations, as Review does for a legacy
    parent, rather than assuming the checkbox default.
    """
    import os
    from project_scope_files import MAX_SCOPE_METADATA_BYTES, capture_scope_file

    data_root = Path(os.path.abspath(data_dir))
    for hop in origin['hops']:
        if hop.get('strip_front_matter_invalid'):
            raise ValueError('Source preparation settings are unavailable.')
        recorded = hop.get('strip_front_matter')
        if type(recorded) is bool:
            return recorded
        script_abs = Path(os.path.abspath(hop['script_path']))
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
    return None


def get_saved_book_strip_front_matter(data_dir, saved_script_path, entries):
    """Derive the preparation an unrecorded saved book's own script matches.

    Save-time only: re-saving a book saved before the choice was recorded must
    record the preparation its script was generated with. Returns the one
    preparation the script matches, or None when the origin cannot be resolved,
    the script matches neither, or both match (then the choice is not
    observable and stays unrecorded; the read path tries both).
    """
    import os
    from generation_completion import get_generation_input
    from literal_policy import get_strip_front_matter_choices
    from script_selection_binding import resolve_saved_book_origin

    if not isinstance(entries, list) or not entries:
        return None
    script_words = []
    for row in entries:
        if not isinstance(row, dict) or not isinstance(row.get('text', ''), str):
            return None
        script_words.extend(get_comparison_words(row['text']))
    data_root = os.path.abspath(data_dir)
    try:
        origin = resolve_saved_book_origin(
            data_root, saved_script_path, require_terminal_file=True)
        source, _digest_value = get_generation_input(
            origin['source_path'], owning_root=data_root)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None
    try:
        matches = [choice for choice in get_strip_front_matter_choices(None)
                   if get_comparison_words(get_prepared_source(source, choice)) == script_words]
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        return None
    return matches[0] if len(matches) == 1 else None


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
        from literal_policy import get_strip_front_matter_choices
        return {'input_path': origin['source_path'],
                'strip_front_matter': strip_front_matter,
                'strip_choices': get_strip_front_matter_choices(strip_front_matter),
                'source_sha256': origin['source_sha256'],
                'saved_script': True,
                'origin': origin}
    strip_front_matter = None
    generation_path = state.get('script_generation_input_file')
    if generation_path:
        if generation_path != input_path:
            raise ValueError('Saved generation settings belong to another source; original source is unavailable.')
        options = state.get('script_generation_options')
        if not isinstance(options, dict) or type(options.get('strip_front_matter')) is not bool:
            raise ValueError('Source preparation settings are unavailable.')
        strip_front_matter = options['strip_front_matter']
    return {'input_path': input_path,
            'strip_front_matter': strip_front_matter,
            'strip_choices': (strip_front_matter,),
            'source_sha256': None,
            'saved_script': False,
            'origin': None}


def get_source_preparation(state, *, data_dir=None):
    """Resolve the selected prose and its saved preparation policy once."""
    details = _get_source_preparation_details(state, data_dir=data_dir)
    return details['input_path'], details['strip_front_matter']


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


def _script_comparison_words(script_bytes):
    """The published script's comparison words, or None when unreadable."""
    try:
        rows = json.loads(script_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError):
        return None
    if not isinstance(rows, list) or not rows:
        return None
    words = []
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('text', ''), str):
            return None
        words.extend(get_comparison_words(row['text']))
    return words


def _choose_unrecorded_preparation(candidates, script_bytes, entries):
    """Pick the preparation an unrecorded saved book was generated with.

    The script itself selects the preparation, exactly as Review's
    legacy-parent path proves a child against the variant it derives; an
    edited comparison row must not choose the variant. When the script
    matches neither (it was edited beyond preprocessing), the rows being
    compared choose the preparation with fewer differences. Returns
    (choice, prepared_text, diff_or_None).
    """
    script_words = _script_comparison_words(script_bytes)
    if script_words is not None:
        matches = [candidate for candidate in candidates
                   if get_comparison_words(candidate[1]) == script_words]
        if len(matches) == 1:
            return matches[0][0], matches[0][1], None
    ranked = [(len(word_diff(text, entries)['hunks']), index, choice, text, diff)
              for index, (choice, text) in enumerate(candidates)
              for diff in (word_diff(text, entries),)]
    best = min(ranked, key=lambda row: row[:2])
    return best[2], best[3], best[4]


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
    saved_script_shape = (data_dir is not None
                          and isinstance(input_path, str)
                          and Path(input_path).suffix.lower() == '.json')
    identity = {key: state.get(key) for key in (
        'active_book_id', 'book_generation', 'input_file_path', '_source_scope')}
    if not saved_script_shape:
        # A saved book's comparison never reads these run fields, so they must
        # not invalidate an issued confirmation: a cancelled re-generation
        # rewrites them without changing anything the comparison used.
        for key in ('script_generation_input_file', 'script_generation_options'):
            identity[key] = state.get(key)
    source_sha256 = None
    source = None
    reason = None
    scope = 'current source'
    saved_script = False
    strip_front_matter = None
    preparation_unrecorded = False
    preparation_diff = None
    try:
        details = _get_source_preparation_details(state, data_dir=data_dir)
        input_path = details['input_path']
        saved_script = details['saved_script']
        if saved_script:
            origin = details['origin']
            identity['resolved_origin'] = {
                'source_path': origin['source_path'],
                'source_path_sha256': origin['source_path_sha256']}
        try:
            source, source_sha256 = get_generation_input(
                input_path, owning_root=data_dir if saved_script else None)
        except FileNotFoundError as exc:
            if saved_script:
                raise ValueError(_SAVED_SOURCE_REASONS['missing_source']) from exc
            raise
        resolved_source_sha256 = details['source_sha256']
        if resolved_source_sha256 is not None and source_sha256 != resolved_source_sha256:
            raise ValueError(_SAVED_SOURCE_REASONS['changed_source'])
        if expected_source_sha256 is not None and source_sha256 != expected_source_sha256:
            raise ValueError('Source changed during the comparison; refresh before relying on it.')
        choices = details['strip_choices']
        if len(choices) == 1:
            strip_front_matter = choices[0]
            if strip_front_matter is not None:
                source = get_prepared_source(source, strip_front_matter)
                scope = 'current source after generation preprocessing'
            if '_source_scope' in state:
                source = get_selected_source_words(source, source_sha256, strip_front_matter,
                                                   state['_source_scope'])
                scope = 'verified selected source word range'
        else:
            # A book saved before the preparation choice was recorded admits
            # both preparations, exactly as Review's legacy-parent path does.
            candidates, errors = [], []
            for choice in choices:
                try:
                    text = get_prepared_source(source, choice)
                    if '_source_scope' in state:
                        text = get_selected_source_words(
                            text, source_sha256, choice, state['_source_scope'])
                    candidates.append((choice, text))
                except (OSError, ValueError, UnicodeError) as error:
                    errors.append(error)
            if not candidates:
                raise errors[0]
            preparation_unrecorded = True
            strip_front_matter, source, preparation_diff = _choose_unrecorded_preparation(
                candidates, script_bytes, entries)
            scope = ('verified selected source word range' if '_source_scope' in state
                     else 'current source after generation preprocessing')
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
    if saved_script:
        result['resolved_strip_front_matter'] = strip_front_matter
    if preparation_unrecorded:
        result['preparation_note'] = (
            'This saved script records no front-matter preparation choice; the '
            'comparison used the preparation with '
            + ('front matter stripped.' if strip_front_matter else 'front matter kept.'))
    if source is None:
        report = {**result, 'status': 'unavailable', 'reason': reason,
                  'hunks': [], 'totals': {}, 'chunks_total': 0}
    else:
        diff = (preparation_diff if preparation_diff is not None
                else _get_cached_diff(snapshot, source, _get_json(entries)))
        # The paged view reads the cached diff without copying its containers.
        # Legacy callers retain detached mutable reports as before.
        if review_offset is None:
            diff = copy.deepcopy(diff)
        report = {**result, **diff, 'status': 'differences' if diff['hunks'] else 'verified'}
    return report if review_offset is None else get_source_integrity_page(report, review_offset)
