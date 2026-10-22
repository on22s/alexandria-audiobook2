"""Pure script/chunk boundary checks for the reversible scope planner."""
from project import group_into_chunks, NARRATOR_GENERATION_FIELDS, CHUNK_POLICY_KEY
from literal_policy import SCRIPT_POLICY_KEY, get_literal_policy, ensure_literal_chunks


def get_scope_partition(script, chunks, start_entry):
    """Resolve an explicit suffix without changing rows or certifying audio.

    The caller still has to bind current source, voice and artifact authority.
    A boundary that would split a saved chunk requires a different selection.
    """
    if (not isinstance(script, list) or not script or
            any(not isinstance(row, dict) or not isinstance(row.get('text', ''), str)
                for row in script) or
            type(start_entry) is not int or not 0 < start_entry < len(script)):
        raise ValueError('Select an entry boundary inside the saved script.')
    if not isinstance(chunks, list) or not chunks or any(not isinstance(row, dict) for row in chunks):
        raise ValueError('Saved chunks are required for a retaining scope change.')
    ids = [row.get('id') for row in chunks]
    uids = [row.get('uid') for row in chunks]
    if (any(type(value) is not int or value < 0 for value in ids) or len(set(ids)) != len(ids)
            or any(not isinstance(uid, str) or not uid for uid in uids)
            or len(set(uids)) != len(uids)):
        raise ValueError('Saved chunks require unambiguous IDs and UIDs.')
    grouped = group_into_chunks(script, include_source_indices=True)
    fields = ('speaker', 'text', 'instruct', 'pause_after', 'chapter_origin',
              *NARRATOR_GENERATION_FIELDS, CHUNK_POLICY_KEY)
    def authored(row, *, literal=True):
        return {key: row.get(key) for key in fields if literal or key != CHUNK_POLICY_KEY}
    literal_policy = get_literal_policy(script)
    try:
        if literal_policy is not None:
            ensure_literal_chunks(chunks, literal_policy)
    except ValueError as error:
        raise ValueError('Saved chunks no longer match the script grouping.') from error
    if len(grouped) != len(chunks) or any(authored(a, literal=literal_policy is None) !=
            authored(b, literal=literal_policy is None) for a, b in zip(grouped, chunks)):
        raise ValueError('Saved chunks no longer match the script grouping.')
    retained = []
    for index, row in enumerate(grouped):
        indices = row['source_entry_indices']
        if indices[0] < start_entry <= indices[-1]:
            raise ValueError('The selected source boundary splits a saved chunk.')
        if indices[0] >= start_entry:
            retained.append(index)
    if not retained or retained != list(range(retained[0], len(chunks))):
        raise ValueError('The selected scope has no unambiguous retained chunks.')
    # The complete original policy was checked by the full grouping above.
    # A suffix needs a new source-scope binding; this boundary check must not
    # copy the full-book word proof onto a shorter script or invent a new one.
    suffix_script = [{key: value for key, value in row.items() if key != SCRIPT_POLICY_KEY}
                     for row in script[start_entry:]]
    suffix = group_into_chunks(suffix_script, include_source_indices=True)
    old_suffix = grouped[retained[0]:]
    if (len(suffix) != len(old_suffix) or any(authored(a, literal=False) != authored(b, literal=False) for a, b in zip(suffix, old_suffix))
            or any(a['source_entry_indices'] != [i - start_entry for i in b['source_entry_indices']]
                   for a, b in zip(suffix, old_suffix))):
        raise ValueError('Changing scope would change retained chunk grouping or context.')
    return {'script_start': start_entry, 'chunk_start': retained[0],
            'source_entry_indices': [row['source_entry_indices'] for row in suffix],
            'literal_policy_rebind_required': literal_policy is not None}
