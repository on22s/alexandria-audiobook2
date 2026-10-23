"""Pure, bounded identity decisions for source-backed cast review (#1443).

The caller owns book/source binding, explicit approval and atomic persistence.
Quote locations support review; identity is established only by the decisions.
Nothing here alters a script, voice, chunk, audio file or generation checkpoint.
"""
import copy
import hashlib
import re

from source_evidence import (MAX_ENTRIES, MAX_SOURCE_CHARS, _require_keys, _require_text,
                             get_source_evidence_review, validate_source_evidence_packet)

MAX_ALIASES = 32
MAX_RETIRED_IDS = 2000


def _validate_id(value):
    if not isinstance(value, str) or re.fullmatch(r'character_[0-9a-f]{20}', value) is None:
        raise ValueError('Expected an opaque character ID')


def _get_entry_packet(source_characters, fingerprint, entries):
    if not isinstance(entries, list) or not 1 <= len(entries) <= MAX_ENTRIES:
        raise ValueError('Roster requires between 1 and 200 entries')
    cast, claims = [], []
    for entry in entries:
        _require_keys(entry, ('id', 'canonical_name', 'aliases', 'evidence'), 'Roster entry')
        aliases = entry['aliases']
        if not isinstance(aliases, list) or len(aliases) > MAX_ALIASES:
            raise ValueError('Each character supports at most 32 aliases')
        for alias in aliases:
            _require_text(alias, 1024, 'Alias')
        # Validate name types/UTF-8 before normalization or generator validation.
        _require_text(entry['canonical_name'], 1024, 'Canonical name')
        normalized = [a.strip().upper() for a in aliases]
        if len(set(normalized)) != len(normalized) or entry['canonical_name'].strip().upper() in normalized:
            raise ValueError('Aliases must be distinct and differ from their canonical name')
        cast.append({'name': entry['canonical_name'], 'aliases': list(aliases)})
        claims.append({key: entry[key] for key in ('id', 'canonical_name', 'evidence')})
    packet = {'source_sha256': fingerprint, 'entries': claims}
    validate_source_evidence_packet(source_characters, fingerprint, packet)
    # Share generation's protected-label and cross-character alias ownership rules.
    from three_pass_generate import get_cast_from_data
    get_cast_from_data(cast)
    return packet, cast


def validate_character_roster(roster):
    """Validate a saved/imported historical record without scanning a new source."""
    _require_keys(roster, ('schema', 'source_sha256', 'source_characters', 'entries', 'retired_ids'), 'Roster')
    if type(roster['schema']) is not int or roster['schema'] != 1:
        raise ValueError('Unsupported roster schema')
    _get_entry_packet(roster['source_characters'], roster['source_sha256'], roster['entries'])
    retired = roster['retired_ids']
    if not isinstance(retired, list) or len(retired) > MAX_RETIRED_IDS:
        raise ValueError('Retired character history exceeds the supported limit')
    for identity in retired:
        _validate_id(identity)
    if len(set(retired)) != len(retired) or set(retired) & {e['id'] for e in roster['entries']}:
        raise ValueError('Retired IDs must be distinct and cannot be active')


def get_validated_character_decisions(decisions):
    """Validate all decision structures before source reads or transition checks."""
    if not isinstance(decisions, list) or len(decisions) > MAX_ENTRIES:
        raise ValueError('At most 200 explicit decisions are supported')
    result = []
    for decision in decisions:
        _require_keys(decision, ('action', 'from_ids', 'to_ids'), 'Decision')
        action, before, after = (decision[k] for k in ('action', 'from_ids', 'to_ids'))
        if not isinstance(action, str) or action not in ('add', 'update', 'merge', 'split', 'retire'):
            raise ValueError('Unsupported identity decision')
        for identities in (before, after):
            if not isinstance(identities, list) or len(identities) > MAX_ENTRIES:
                raise ValueError('Invalid decision identity list')
            for identity in identities:
                _validate_id(identity)
            if len(set(identities)) != len(identities):
                raise ValueError('Repeated identity in a decision')
        result.append((action, set(before), set(after)))
    return result


def validate_character_roster_proposal(proposal):
    """Bounded structural preflight; actual source ranges are rechecked on review."""
    _require_keys(proposal, ('source_sha256', 'entries', 'decisions'), 'Proposal')
    _get_entry_packet(MAX_SOURCE_CHARS, proposal['source_sha256'], proposal['entries'])
    get_validated_character_decisions(proposal['decisions'])


def _get_transition_retirements(previous, entries, decisions, source_changed):
    old = {e['id']: e for e in previous['entries']} if previous else {}
    new = {e['id']: e for e in entries}
    retired = set(previous['retired_ids']) if previous else set()
    if retired & set(new):
        raise ValueError('A retired identity cannot be reused')
    used_inputs, used_outputs = set(), set()
    for action, inputs, outputs in get_validated_character_decisions(decisions):
        if inputs - set(old) or outputs - set(new):
            raise ValueError('Decision references an absent identity')
        if inputs & used_inputs or outputs & used_outputs:
            raise ValueError('An identity cannot participate in competing decisions')
        valid = (
            action == 'add' and not inputs and len(outputs) == 1 and not outputs & set(old)
            or action == 'update' and len(inputs) == 1 and inputs == outputs
            or action == 'merge' and len(inputs) >= 2 and len(outputs) == 1 and outputs <= inputs
            or action == 'split' and len(inputs) == 1 and len(outputs) >= 2 and not (outputs & set(old) - inputs)
            or action == 'retire' and len(inputs) == 1 and not outputs
        )
        if not valid:
            raise ValueError('Decision shape would steal, replace or ambiguously reuse an identity')
        # A merge/retire cannot leave a consumed identity active elsewhere.
        if (inputs - outputs) & set(new):
            raise ValueError('A consumed identity must be removed from the resulting roster')
        used_inputs.update(inputs); used_outputs.update(outputs)
    required_old = {i for i in old if i not in new or old[i] != new[i] or source_changed}
    required_new = {i for i in new if i not in old or old[i] != new[i] or source_changed}
    if required_old - used_inputs or required_new - used_outputs:
        raise ValueError('Every changed, added or removed identity requires an explicit decision; source revisions require reconciliation')
    retired.update(set(old) - set(new))
    if len(retired) > MAX_RETIRED_IDS:
        raise ValueError('Retired character history exceeds the supported limit')
    return sorted(retired)


def get_character_roster_review(source_text, previous, proposal, *, source_changed=False):
    """Return a detached proposal review; ambiguous/missing claims block approval.

    Callers must validate all external bytes and book/source preparation guards
    before this function. Approval must recompute against the current snapshot.
    """
    if type(source_changed) is not bool:
        raise ValueError('Source reconciliation flag must be boolean')
    _require_text(source_text, MAX_SOURCE_CHARS, 'Source')
    fingerprint = hashlib.sha256(source_text.encode('utf-8')).hexdigest()
    _require_keys(proposal, ('source_sha256', 'entries', 'decisions'), 'Proposal')
    if proposal['source_sha256'] != fingerprint:
        raise ValueError('Source fingerprint differs; prepare a new review for this revision')
    if previous is not None:
        validate_character_roster(previous)
    packet, cast = _get_entry_packet(len(source_text), fingerprint, proposal['entries'])
    changed = previous is not None and (source_changed or previous['source_sha256'] != fingerprint)
    retired = _get_transition_retirements(previous, proposal['entries'], proposal['decisions'], changed)
    report = get_source_evidence_review(source_text, packet)
    roster = {'schema': 1, 'source_sha256': fingerprint, 'source_characters': len(source_text),
              'entries': proposal['entries'], 'retired_ids': retired}
    return copy.deepcopy({'roster': roster, 'cast': cast, 'decisions': proposal['decisions'],
                          'source_changed': changed, 'evidence': report,
                          'can_approve': all(row['status'] == 'exact' for entry in report['entries'] for row in entry['evidence'])})
