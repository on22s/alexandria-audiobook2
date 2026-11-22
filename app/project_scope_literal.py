"""Selected literal policy with explicit immutable retained-marker lineage."""
import copy
import hashlib
from dialogue_spans import get_source_span_fragments
from literal_fidelity import _words
from literal_policy import (SCRIPT_POLICY_KEY, CHUNK_POLICY_KEY, ensure_literal_narration,
    ensure_literal_chunks, bind_literal_policy, get_literal_policy, get_literal_policy_identity,
    get_word_identity, get_scope_coverage_digest, get_scope_coverage_intervals,
    get_scope_identity, retain_literal_policy)


class ScopeSpanContradiction(ValueError):
    """Parent coordinates contradict the raw source; never downgrade to legacy."""


class ScopeSpanUnavailable(ValueError):
    """An unrelated discarded parent row lacks a span; legacy stays possible."""


def _ordered_spans(spans):
    if any(left[1] > right[0] for left, right in zip(spans, spans[1:])):
        raise ScopeSpanContradiction('Parent source spans are out of order or overlap')
    return spans


def _resolved_row_spans(source, rows, *, allow_missing=False):
    """Resolve shared intervals positionally, before any entry-index cut.

    Returns ordered spans plus a per-row list aligned to ``rows`` (None for a
    row with no span), so a shared span straddling the cut resolves to its
    exact per-row fragments on both sides of the boundary.
    """
    resolved = [None] * len(rows)
    position = 0
    while position < len(rows):
        entry = rows[position]
        span = entry.get('source_span')
        if allow_missing and 'source_span' not in entry:
            position += 1
            continue
        if (not isinstance(span, (list, tuple)) or len(span) != 2
                or any(type(value) is not int for value in span)
                or not 0 <= span[0] < span[1] <= len(source)):
            raise ScopeSpanContradiction('Parent rows require verified source spans')
        span = list(span)
        stop = position + 1
        while stop < len(rows) and rows[stop].get('source_span') == span:
            if any(type(value) is not int for value in rows[stop]['source_span']):
                raise ScopeSpanContradiction('Parent rows require verified source spans')
            stop += 1
        group = rows[position:stop]
        try:
            if len(group) > 1:
                texts = [row['text'] for row in group]
                fragments = get_source_span_fragments(source, ' '.join(texts), span, texts)
            else:
                exact = get_source_span_fragments(
                    source, group[0]['text'], span, [group[0]['text']])[0]
                if list(exact) != span:
                    raise ScopeSpanContradiction('Parent span overruns its raw narration')
                fragments = [exact]
        except ValueError as error:
            raise ScopeSpanContradiction('Parent spans contradict the raw source') from error
        if len(fragments) != len(group):
            raise ScopeSpanContradiction('Shared parent interval has ambiguous fragments')
        for index, fragment in enumerate(fragments):
            resolved[position + index] = list(fragment)
        position = stop
    return _ordered_spans([value for value in resolved if value is not None]), resolved


def _retained_scope_layout(source, rows, start_entry, *, require_complete=True):
    """Verify the partition, split resolved spans by row index, and report gaps.

    Known shared fragments are resolved positionally even when an unrelated
    row has no span; only evidence derivation requires a complete partition.
    """
    _ordered, resolved = _resolved_row_spans(source, rows, allow_missing=True)
    if require_complete and any(value is None for value in resolved):
        raise ScopeSpanUnavailable('A parent row has no verified source span')
    prefix = _ordered_spans([value for value in resolved[:start_entry] if value is not None])
    suffix = _ordered_spans([value for value in resolved[start_entry:] if value is not None])
    intervals = get_scope_coverage_intervals(source, suffix) if suffix else None
    return prefix, suffix, intervals, resolved


def _retained_scope_intervals(source, rows, *, start_entry=None):
    """Raw coverage of parent rows, verified across the full cut boundary."""
    if start_entry is None:
        ordered, resolved = _resolved_row_spans(source, rows)
        if any(value is None for value in resolved):
            raise ScopeSpanUnavailable('A parent row has no verified source span')
        return get_scope_coverage_intervals(source, ordered)
    _prefix, _suffix, intervals, _resolved = _retained_scope_layout(source, rows, start_entry)
    return intervals


def get_scope_cut_evidence(source, script, start_entry, *, parent_policy, policy):
    """Derive compact retained-cut evidence from the parent, never the child."""
    if (parent_policy is None or policy is None
            or policy.get('boundary') != 'selected_source_words_v1'):
        return None
    scope = policy['scope_source']
    try:
        intervals = _retained_scope_intervals(source, script, start_entry=start_entry)
    except ScopeSpanUnavailable:
        return None
    return {'version': 1, 'source_sha256': hashlib.sha256(source.encode('utf-8')).hexdigest(),
            'parent_policy_sha256': scope['parent_policy_sha256'],
            'scope_policy_sha256': get_scope_identity(policy),
            'coverage_sha256': get_scope_coverage_digest(intervals),
            'outer_start': intervals[0][0], 'outer_end': intervals[-1][1],
            'start_word': scope['start_word']}


def verify_parent_scope_evidence(source, parent_rows, evidence, *, prepared_sha256):
    """The shared authority for replacing frozen scope evidence with a recut.

    The current parent must still be the exact scope the binding certified:
    its policy identity, prepared source and resolved coverage endpoints all
    have to match before any new evidence may replace the frozen record.
    """
    from script_selection_binding import is_scope_cut_evidence
    if not is_scope_cut_evidence(evidence):
        raise ScopeSpanContradiction('Frozen scope evidence is malformed')
    policy = get_literal_policy(parent_rows)
    if policy is None or policy['boundary'] != 'selected_source_words_v1':
        raise ScopeSpanContradiction('Frozen scope evidence requires a scoped parent')
    scope = policy['scope_source']
    if (get_scope_identity(policy) != evidence['scope_policy_sha256']
            or scope['parent_policy_sha256'] != evidence['parent_policy_sha256']
            or scope['prepared_source_sha256'] != prepared_sha256
            or evidence['source_sha256'] != prepared_sha256):
        raise ScopeSpanContradiction('Frozen scope evidence differs from the current parent')
    try:
        intervals = _retained_scope_intervals(source, parent_rows)
    except ScopeSpanUnavailable as error:
        raise ScopeSpanContradiction('Frozen scope evidence parent coverage is incomplete') from error
    if (get_scope_coverage_digest(intervals) != evidence['coverage_sha256']
            or intervals[0][0] != evidence['outer_start']
            or intervals[-1][1] != evidence['outer_end']):
        raise ScopeSpanContradiction('Frozen scope evidence coverage differs from the parent')
    return intervals


def get_literal_scope(source, script, chunks, start_entry, start_chunk, *,
                      input_sha256=None, strip_front_matter=None):
    """Caller first proves canonical chunk/source partition; no artifact changes."""
    original = ensure_literal_narration(script)
    suffix = [{key: copy.deepcopy(value) for key, value in row.items() if key != SCRIPT_POLICY_KEY}
              for row in script[start_entry:]]
    if original is None:
        return suffix
    prepared_sha256 = hashlib.sha256(source.encode('utf-8')).hexdigest()
    words = list(_words(source)); base_start = 0
    current = source
    if original['boundary'] == 'selected_source_words_v1':
        prior = original['scope_source']
        if prepared_sha256 != prior['prepared_source_sha256'] or len(words) != prior['total_words']:
            raise ValueError('Current prepared source differs from the literal origin policy.')
        base_start = prior['start_word']
        current = ' '.join(words[base_start:])
    if (get_word_identity([current]) != original['source_words']
            or hashlib.sha256(current.encode('utf-8')).hexdigest() != original['source_sha256']
            or 'input_sha256' in original and original['input_sha256'] != input_sha256
            or 'strip_front_matter' in original and original['strip_front_matter'] != strip_front_matter):
        raise ValueError('Current prepared source differs from the literal origin policy.')
    start = base_start + sum(len(list(_words(row['text']))) for row in script[:start_entry])
    if not 0 < start < len(words):
        raise ValueError('A verifiable literal source boundary is required.')
    selected = ' '.join(words[start:])
    bound = bind_literal_policy(selected, suffix, input_sha256=input_sha256,
                                strip_front_matter=strip_front_matter)
    policy = get_literal_policy(bound)
    lineage = {}
    ensure_literal_chunks(chunks[start_chunk:], original)
    for row in chunks[start_chunk:]:
        uid = row.get('uid')
        if not isinstance(uid, str) or not uid or uid in lineage:
            raise ValueError('Retained literal chunks require unambiguous UIDs.')
        lineage[uid] = copy.deepcopy(row[CHUNK_POLICY_KEY])
    scope_source = {'parent_policy_sha256': get_literal_policy_identity(original),
                    'prepared_source_sha256': prepared_sha256,
                    'start_word': start, 'end_word': len(words), 'total_words': len(words),
                    'script_start_entry': start_entry}
    _prefix, _retained_spans, intervals, resolved = _retained_scope_layout(
        source, script, start_entry, require_complete=False)
    for row, span in zip(suffix, resolved[start_entry:]):
        if span is not None and 'source_span' in row:
            row['source_span'] = list(span)
    if intervals is not None:
        proof = {'version': 1, 'source_sha256': prepared_sha256,
                 'parent_policy_sha256': scope_source['parent_policy_sha256'],
                 'start_word': start, 'intervals': intervals,
                 'coverage_sha256': get_scope_coverage_digest(intervals)}
        if input_sha256 is not None:
            proof['input_sha256'] = input_sha256
        scope_source['cut_proof'] = proof
    else:
        scope_source.pop('cut_proof', None)
    policy.update(boundary='selected_source_words_v1', scope_chunks=lineage,
                  scope_source=scope_source)
    get_literal_policy([{SCRIPT_POLICY_KEY: policy}])
    return retain_literal_policy(suffix, policy)
