"""Selected literal policy with explicit immutable retained-marker lineage."""
import copy
import hashlib
from dialogue_spans import get_source_span_fragments
from literal_fidelity import _words
from literal_policy import (SCRIPT_POLICY_KEY, CHUNK_POLICY_KEY, ensure_literal_narration,
    ensure_literal_chunks, bind_literal_policy, get_literal_policy, get_literal_policy_identity,
    get_word_identity, get_scope_coverage_digest, get_scope_coverage_intervals,
    get_scope_identity, retain_literal_policy)


def _resolved_row_spans(source, rows):
    """Resolve ordered raw spans for any group of parent rows."""
    spans = []
    position = 0
    while position < len(rows):
        entry = rows[position]
        span = entry.get('source_span')
        if (not isinstance(span, (list, tuple)) or len(span) != 2
                or any(type(value) is not int for value in span)
                or not 0 <= span[0] < span[1] <= len(source)):
            raise ValueError('Parent rows require verified source spans')
        span = list(span)
        stop = position + 1
        while stop < len(rows) and rows[stop].get('source_span') == span:
            stop += 1
        group = rows[position:stop]
        if len(group) > 1:
            texts = [row['text'] for row in group]
            spans.extend(get_source_span_fragments(source, ' '.join(texts), span, texts))
        else:
            spans.append(span)
        position = stop
    return spans


def _partition_spans(source, rows, start_entry):
    """Verify the complete ordered parent partition across the cut boundary."""
    prefix = _resolved_row_spans(source, rows[:start_entry])
    suffix = _resolved_row_spans(source, rows[start_entry:])
    if any(left[1] > right[0] for left, right in zip(prefix, prefix[1:])):
        raise ValueError('Pre-cut parent spans overlap')
    if any(left[1] > right[0] for left, right in zip(suffix, suffix[1:])):
        raise ValueError('Retained parent spans overlap')
    if prefix and suffix and prefix[-1][1] > suffix[0][0]:
        raise ValueError('Retained cut overlaps the discarded parent prefix')
    return prefix, suffix


def _retained_scope_intervals(source, rows, *, start_entry=None):
    """Raw coverage of parent rows, verified across the full cut boundary."""
    if start_entry is None:
        spans = _resolved_row_spans(source, rows)
    else:
        _prefix, spans = _partition_spans(source, rows, start_entry)
    return get_scope_coverage_intervals(source, spans)


def get_scope_cut_evidence(source, script, start_entry, *, parent_policy, policy):
    """Derive compact retained-cut evidence from the parent, never the child."""
    if (parent_policy is None or policy is None
            or policy.get('boundary') != 'selected_source_words_v1'):
        return None
    scope = policy['scope_source']
    try:
        intervals = _retained_scope_intervals(source, script, start_entry=start_entry)
    except ValueError:
        return None
    return {'version': 1, 'source_sha256': hashlib.sha256(source.encode('utf-8')).hexdigest(),
            'parent_policy_sha256': scope['parent_policy_sha256'],
            'scope_policy_sha256': get_scope_identity(policy),
            'coverage_sha256': get_scope_coverage_digest(intervals),
            'outer_start': intervals[0][0], 'outer_end': intervals[-1][1],
            'start_word': scope['start_word']}


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
    try:
        intervals = _retained_scope_intervals(source, script, start_entry=start_entry)
        proof = {'version': 1, 'source_sha256': prepared_sha256,
                 'parent_policy_sha256': scope_source['parent_policy_sha256'],
                 'start_word': start, 'intervals': intervals,
                 'coverage_sha256': get_scope_coverage_digest(intervals)}
        if input_sha256 is not None:
            proof['input_sha256'] = input_sha256
        scope_source['cut_proof'] = proof
    except ValueError:
        scope_source.pop('cut_proof', None)
    policy.update(boundary='selected_source_words_v1', scope_chunks=lineage,
                  scope_source=scope_source)
    get_literal_policy([{SCRIPT_POLICY_KEY: policy}])
    return retain_literal_policy(suffix, policy)
