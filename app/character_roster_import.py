"""Explicit imported-ID mapping into a current roster, with bounded tombstones."""
import re

from character_roster import (validate_character_roster, _validate_id, MAX_RETIRED_IDS,
                              get_validated_character_decisions)
from source_evidence import _require_keys, _require_text, MAX_ENTRIES


def get_roster_import_reconciliation(imported, mapping, entries):
    """Mapping is user-reviewed intent, never an automatic name/quote identity match."""
    validate_character_roster(imported)
    source = {e['id']: e for e in imported['entries']}
    target = {e['id']: e for e in entries}
    if not isinstance(mapping, dict) or len(mapping) != len(source) or set(mapping) != set(source):
        raise ValueError('An explicit identity mapping is required for every imported character')
    rows = []; retired = set(imported['retired_ids'])
    for identity, destination in mapping.items():
        _validate_id(destination)
        if destination not in target:
            raise ValueError('Imported identity mapping must select an active draft character')
        if identity != destination:
            retired.add(identity)
        rows.append({'imported_id': identity, 'imported_name': source[identity]['canonical_name'],
                     'target_id': destination, 'target_name': target[destination]['canonical_name']})
    if retired & set(target):
        raise ValueError('An imported retired or remapped ID cannot remain active; review its mapping')
    if len(retired) > MAX_RETIRED_IDS:
        raise ValueError('Imported retirement history exceeds the supported limit')
    return {'source_sha256': imported['source_sha256'], 'mappings': rows, 'retired_ids': sorted(retired)}


def validate_roster_approval(approval, roster):
    """The latest explicit intent is bounded history, not inferred identity proof."""
    _require_keys(approval, ('decisions', 'import_review'), 'Saved character approval')
    get_validated_character_decisions(approval['decisions'])
    imported = approval['import_review']
    if imported is None:
        return
    _require_keys(imported, ('source_sha256', 'mappings', 'retired_ids'), 'Saved import review')
    if not isinstance(imported['source_sha256'], str) or re.fullmatch(r'[0-9a-f]{64}', imported['source_sha256']) is None:
        raise ValueError('Invalid saved import source fingerprint')
    if not isinstance(imported['mappings'], list) or not 1 <= len(imported['mappings']) <= MAX_ENTRIES:
        raise ValueError('Invalid saved import mappings')
    targets = {row['id']: row['canonical_name'] for row in roster['entries']}
    seen = set()
    for row in imported['mappings']:
        _require_keys(row, ('imported_id', 'imported_name', 'target_id', 'target_name'), 'Saved import mapping')
        _validate_id(row['imported_id']); _validate_id(row['target_id'])
        _require_text(row['imported_name'], 1024, 'Imported character name')
        _require_text(row['target_name'], 1024, 'Mapped character name')
        if row['imported_id'] in seen or targets.get(row['target_id']) != row['target_name']:
            raise ValueError('Saved import mappings differ from the approved roster')
        seen.add(row['imported_id'])
    retired = imported['retired_ids']
    if not isinstance(retired, list) or len(retired) > MAX_RETIRED_IDS:
        raise ValueError('Invalid saved imported retirement history')
    for identity in retired:
        _validate_id(identity)
    if len(set(retired)) != len(retired) or not set(retired) <= set(roster['retired_ids']):
        raise ValueError('Saved imported retirements differ from the approved roster')
