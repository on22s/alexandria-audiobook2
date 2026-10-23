"""Bounded cue history and private saved-book companions; no media approval.

Imports for optional cue inspection stay inside the read/validate functions so
ordinary book selection without a manifest needs no audio decoder imports.
"""
import json
from pathlib import Path

from json_metadata import get_metadata_json_bytes, get_unique_json_object

ACTIVE_CUES = '.sound_design_cues.json'
MAX_REGISTRY_BYTES = 2 * 1024**2
MAX_VERSIONS = 1024


def get_saved_sound_design_path(script_path):
    path = Path(script_path)
    return str(path.with_name('.' + path.stem + '.sound_design.json'))


def get_sound_design_registry_bytes(document):
    """Validate the complete bounded history before selecting or copying it."""
    from source_evidence import _require_keys, _require_text
    from sound_design_cues import (_validate_book, MAX_CUES,
        validate_sound_design_cue_version_structure, validate_sound_design_cue_version)
    _require_keys(document, ('schema', 'book', 'versions', 'selected'), 'Cue registry')
    if type(document['schema']) is not int or document['schema'] != 1:
        raise ValueError('Unsupported cue registry schema')
    _validate_book(document['book'])
    versions, selected = document['versions'], document['selected']
    if not isinstance(versions, list) or len(versions) > MAX_VERSIONS:
        raise ValueError('Cue history exceeds its version limit')
    if not isinstance(selected, dict) or len(selected) > MAX_CUES:
        raise ValueError('Selected cues exceed their count limit')
    for row in versions:
        validate_sound_design_cue_version_structure(row)
    for cue_id, version in selected.items():
        _require_text(cue_id, 128, 'Selected cue identity')
        if not isinstance(version, str) or len(version) != 64:
            raise ValueError('Selected cue requires a version fingerprint')
    # Joint accounting precedes digest checks and their detached copies.
    raw = get_metadata_json_bytes(document, MAX_REGISTRY_BYTES, 'Cue registry')
    known, roots = {}, set()
    for row in versions:
        version, cue_id, parent = row['version'], row['cue_id'], row['parent_version']
        if version in known or row['recipe']['book']['book_id'] != document['book']['book_id']:
            raise ValueError('Cue history repeats a version or belongs to another book')
        if parent is None:
            if cue_id in roots:
                raise ValueError('Cue identity already has an origin version')
            roots.add(cue_id)
        elif parent not in known or known[parent]['cue_id'] != cue_id:
            raise ValueError('Cue history requires its earlier parent for the same cue')
        known[version] = row
    for cue_id, version in selected.items():
        if version not in known or known[version]['cue_id'] != cue_id:
            raise ValueError('Selected version is absent or belongs to another cue')
    for row in versions:
        validate_sound_design_cue_version(row)
    return raw


def _refuse_nonfinite(_value):
    raise ValueError('Nonfinite cue metadata is not supported')


def get_sound_design_registry(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_REGISTRY_BYTES:
        raise ValueError('Cue registry exceeds its byte limit or is empty')
    try:
        document = json.loads(raw.decode('utf-8'), object_pairs_hook=get_unique_json_object,
                              parse_constant=_refuse_nonfinite)
        get_sound_design_registry_bytes(document)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise ValueError('Cue metadata is invalid; existing files were preserved') from None
    return document


def capture_sound_design_registry(root, relative, *, expected_book_id=None):
    """Read exact companion bytes under the caller's existing book admission."""
    from book_state_transaction import is_book_state_admitted
    from project_scope_files import capture_scope_file
    if not is_book_state_admitted(root):
        raise ValueError('Book admission is required for cue metadata')
    _proof, raw = capture_scope_file(root, relative, limit=MAX_REGISTRY_BYTES, include_bytes=True)
    if raw is not None:
        document = get_sound_design_registry(raw)
        if expected_book_id is not None and document['book']['book_id'] != expected_book_id:
            raise ValueError('Cue metadata belongs to another book; existing files were preserved')
    return raw


def get_selected_sound_design_versions(document):
    """Detach selected revisions in canonical cue-ID order, without reanchoring.

    Historical recipes may be stale. Only the captured PCM projection can
    determine that; selecting a revision is never approval of media.
    """
    value = json.loads(get_sound_design_registry_bytes(document))
    known = {row['version']: row for row in value['versions']}
    return [known[value['selected'][cue_id]] for cue_id in sorted(value['selected'])]


def get_sound_design_registry_edit(document, cue_id, recipe, parent_version=None):
    """Append and select one immutable revision; leave the supplied history alone.

    The caller constructs current book/source/selected-audio facts and owns
    context/registry compare-and-swap plus transactional publication. This pure
    helper validates history and bounds; it does not authenticate those facts.
    """
    from sound_design_cues import (MAX_CUES, get_sound_design_cue_version,
                                   validate_sound_design_cue_version_structure)
    get_sound_design_registry_bytes(document)
    draft = dict(schema=1, cue_id=cue_id, parent_version=parent_version,
                 recipe=recipe, version='0' * 64)
    validate_sound_design_cue_version_structure(draft)
    if recipe['book']['book_id'] != document['book']['book_id']:
        raise ValueError('Cue revision belongs to another book')
    history = document['versions']
    if len(history) >= MAX_VERSIONS:
        raise ValueError('Cue history exceeds its version limit')
    previous = [row for row in history if row['cue_id'] == cue_id]
    if ((previous and not any(row['version'] == parent_version for row in previous))
            or (not previous and parent_version is not None)):
        raise ValueError('Cue revision requires an earlier parent for the same cue')
    if cue_id not in document['selected'] and len(document['selected']) >= MAX_CUES:
        raise ValueError('Selected cues exceed their count limit')
    candidate = {**document, 'versions': [*history, draft],
                 'selected': {**document['selected'], cue_id: draft['version']}}
    # Bound the complete proposed registry before copying or hashing the new
    # recipe. Its real fingerprint has the same encoded length as this draft.
    get_metadata_json_bytes(candidate, MAX_REGISTRY_BYTES, 'Cue registry')
    version = get_sound_design_cue_version(cue_id, recipe, parent_version)
    candidate['versions'][-1] = version
    candidate['selected'][cue_id] = version['version']
    return json.loads(get_sound_design_registry_bytes(candidate))


def get_sound_design_registry_selection(document, cue_id, version):
    """Select a saved revision or explicitly deselect, retaining every version."""
    from source_evidence import _require_text
    from sound_design_cues import _require_digest
    get_sound_design_registry_bytes(document)
    _require_text(cue_id, 128, 'Cue identity')
    _require_digest(version, 'Selected cue version', nullable=True)
    previous = [row for row in document['versions'] if row['cue_id'] == cue_id]
    if not previous or (version is not None and not any(row['version'] == version for row in previous)):
        raise ValueError('Selected version is absent or belongs to another cue')
    selected = dict(document['selected'])
    if version is None:
        selected.pop(cue_id, None)
    else:
        selected[cue_id] = version
    return json.loads(get_sound_design_registry_bytes({**document, 'selected': selected}))
