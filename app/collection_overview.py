"""Read-only collection overview: project durable per-book state, never start work.

A saved book's durable companions are its script, voice config, meta
(book_id), selection binding, completed-review receipt and generation /
review / three-pass checkpoints. Chunks, chunk audio and merged exports
exist only for the *active* book (DATA_DIR), so for an inactive saved book
render/export evidence is ``unknown`` — it is never inferred from global
output files or session ``process_state``. When the requested saved book is
the active source, the caller attaches the existing active-book workspace
facts for that one row only, and only when the active DATA_DIR script/voice
bytes exactly match this saved snapshot's bytes.

Reading never writes, repairs, resumes, approves or publishes anything.
Caller holds the saved-book (and, for the active row, book) lock.

Scope note: review and export receipts are historical (metadata-only)
evidence only. The overview must not probe LLM/profile/config or network
state to manufacture a current fingerprint. A review receipt whose output
fingerprint/path/version match this saved script establishes that review
was recorded for these entries (``review.status='recorded'``,
``current_settings='not_rechecked'``): it is not current review authority
and must not skip an actually requested review, but it satisfies this
view's "has been reviewed" planning milestone. Readiness is navigation
guidance subject to the existing generation/admission gates, not
qualification or listening approval.

Export completion is stricter: only a session export receipt bound to the
active input revision (script/voice/binding/chunk/journal/take metadata
and full source scope) and to a done full-book attempt, with the output
file metadata still matching, is ``complete``. Old unbound receipts stay
``recorded``. Metadata checks are not full content rehashes, decode or
listening; that limit is disclosed in the evidence. Absent/stale/corrupt
evidence stays ``needs_review``/``unknown`` — render knowledge is never
invented from an inactive book or a partial/scoped/chapter artifact.
"""
import hashlib
import json
import os
import re
from pathlib import Path

from completed_review_receipt import (VERSION as REVIEW_RECEIPT_VERSION,
                                      get_review_entries_fingerprint,
                                      get_review_receipt_path)
from script_recovery_facts import three_pass_manifest_path
from script_selection_binding import (MAX_BINDING_BYTES, parse_script_binding,
                                      saved_binding_matches)
from project_scope_files import (MAX_SCRIPT_BYTES, MAX_SCOPE_METADATA_BYTES,
                                 capture_scope_file)
from editor_poll_snapshot import get_snapshot_digest
from utils import secure_filename

MAX_OVERVIEW_BOOKS = 50


def _checkpoint_groups(name):
    """Checkpoint/quality basenames by family (single source; canonical+legacy)."""
    return {
        'generation': [f'{name}.json.generation_checkpoint.json',
                       f'{name}.generation_checkpoint.json'],
        'review': [f'{name}.json.review_checkpoint.json',
                   f'{name}.review_checkpoint.json'],
        'threepass': [f'{name}.json.threepass_checkpoint.json',
                      f'{name}.threepass_checkpoint.json'],
        'jsonl': [f'{name}.json.checkpoint.jsonl', f'{name}.checkpoint.jsonl'],
        'quality': [f'{name}.json.generation_quality.json',
                    f'{name}.generation_quality.json'],
    }


def _parse_json_bytes(raw):
    """Strictly parse captured JSON bytes; RecursionError -> ValueError."""
    try:
        return json.loads(raw)
    except RecursionError as error:
        raise ValueError('Saved JSON is too deeply nested') from error
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError('Saved JSON is malformed') from error


def _checkpoint_index(scripts_dir):
    """One directory listing for all checkpoint presence checks.

    Returns ``None`` when the listing itself fails so callers can fall
    back to per-file ``is_file`` checks. Shard ownership mirrors
    ``get_generation_checkpoint_artifacts``: only ``<base>.parts-<32hex>``
    directories are valid; the same name as a regular file or symlink is
    invalid ownership (never weakened here).
    """
    index = {'names': set(), 'shards': {}}
    try:
        with os.scandir(scripts_dir) as entries:
            for entry in entries:
                index['names'].add(entry.name)
                marker = entry.name.rfind('.parts-')
                if marker == -1:
                    continue
                suffix = entry.name[marker + len('.parts-'):]
                if not re.fullmatch(r'[0-9a-f]{32}', suffix):
                    continue
                base = entry.name[:marker]
                try:
                    valid = entry.is_dir(follow_symlinks=False) and not entry.is_symlink()
                except OSError:
                    valid = False
                index['shards'].setdefault(base, []).append(valid)
    except OSError:
        return None
    return index


def _checkpoint_facts(scripts_dir, name, index):
    """Presence + validity facts for one book (no sorted globs).

    Generation-quality manifests are completion evidence, not residual
    checkpoints, so they never contribute to ``has_any`` (e-1). A
    three-pass checkpoint left with stage ``done`` beside a complete
    manifest is classified separately in ``_script_stage`` (b-1).
    """
    groups = _checkpoint_groups(name)
    headers = groups['generation'] + groups['review'] + groups['threepass']
    jsonl = groups['jsonl']
    if index is not None:
        names = index['names']
        shards = index['shards']
        has_any = any(base in names for base in headers + jsonl) or any(
            base in shards for base in headers)
        has_review = any(base in names for base in groups['review']) or any(
            base in shards for base in groups['review'])
        invalid = any(valid is False for base in headers
                      for valid in shards.get(base, ()))
        return has_any, has_review, invalid
    # Fallback when listing failed: cheap is_file presence; invalid
    # ownership is detected by namespace matching on existing entries.
    root = Path(scripts_dir)

    def present(basenames):
        return any((root / base).is_file() for base in basenames)

    has_any = present(headers + jsonl)
    has_review = present(groups['review'])
    invalid = False
    try:
        for base in headers:
            prefix = base + '.parts-'
            for candidate in root.glob(prefix + '*'):
                suffix = candidate.name[len(prefix):]
                if not re.fullmatch(r'[0-9a-f]{32}', suffix):
                    continue
                if candidate.is_symlink() or not candidate.is_dir():
                    invalid = True
    except OSError:
        invalid = True
    if invalid:
        has_any = True
    return has_any, has_review, invalid


def _manifest_basenames(name):
    full = os.path.basename(three_pass_manifest_path(f'{name}.json'))
    stem = f'{name}.threepass_manifest.json'
    ordered = [full] if full == stem else [full, stem]
    return ordered


def _checkpoint_basenames(name):
    """Revision inventory: every checkpoint/quality basename (bytes hashed)."""
    groups = _checkpoint_groups(name)
    return list(dict.fromkeys(groups['generation'] + groups['review']
                              + groups['threepass'] + groups['jsonl']
                              + groups['quality']))


def _capture_snapshot(scripts_dir, name, *, script_bytes=None, script_sha256=None):
    """Capture every saved companion exactly once (bounded, read-only).

    Returns a dict ``basename -> {'exists', 'raw', 'sha'}``. ``raw`` bytes
    are retained only where parsing is needed (script/meta/voice/binding/
    receipt/manifests/generation-quality); hashes for checkpoint headers,
    publication and sound-design companions are streaming hashes without
    retaining bytes. ``sha`` is ``'unavailable'`` on bounded-capture failure
    (per-book, never a huge allocation) and ``None`` when truly absent.
    ``script_bytes`` reuses caller-captured bytes (load path) instead of
    rereading the script; ``script_sha256`` additionally reuses the
    caller-captured digest instead of re-hashing the preset bytes.
    """
    snapshot = {}

    def record(basename, limit, need_bytes, preset_raw=None, preset_exists=None, preset_sha=None):
        if basename in snapshot:
            return snapshot[basename]
        if preset_exists is not None:
            raw = preset_raw
            sha = (preset_sha if isinstance(preset_sha, str) and len(preset_sha) == 64
                   else hashlib.sha256(raw).hexdigest()) if raw is not None else None
            entry = {'exists': preset_exists, 'raw': raw, 'sha': sha}
            snapshot[basename] = entry
            return entry
        try:
            info, raw = capture_scope_file(scripts_dir, basename, limit=limit,
                                           include_bytes=need_bytes)
        except (OSError, ValueError, TypeError, RecursionError):
            entry = {'exists': True, 'raw': None, 'sha': 'unavailable'}
            snapshot[basename] = entry
            return entry
        exists = bool(info.get('exists'))
        if not exists:
            entry = {'exists': False, 'raw': None, 'sha': None}
        elif need_bytes:
            sha = info.get('sha256')
            entry = {'exists': True, 'raw': raw,
                     'sha': sha if isinstance(sha, str)
                            else (hashlib.sha256(raw).hexdigest() if raw is not None else 'unavailable')}
        else:
            sha = info.get('sha256')
            entry = {'exists': True, 'raw': None,
                     'sha': sha if isinstance(sha, str) else 'unavailable'}
        snapshot[basename] = entry
        return entry

    # Script: caller bytes or one bounded capture. Presence is signalled
    # separately so missing vs unavailable stays distinct.
    if script_bytes is not None:
        record(f'{name}.json', MAX_SCRIPT_BYTES, True,
               preset_raw=script_bytes, preset_exists=True, preset_sha=script_sha256)
    else:
        record(f'{name}.json', MAX_SCRIPT_BYTES, True)
    record(f'{name}.meta.json', MAX_SCOPE_METADATA_BYTES, True)
    record(f'{name}.voice_config.json', MAX_SCRIPT_BYTES, True)
    record(f'{name}.selection_binding.json', MAX_BINDING_BYTES, True)
    receipt_name = os.path.basename(get_review_receipt_path(f'{name}.json'))
    record(receipt_name, MAX_SCOPE_METADATA_BYTES, True)
    for basename in _manifest_basenames(name):
        record(basename, MAX_SCOPE_METADATA_BYTES, True)
    groups = _checkpoint_groups(name)
    for basename in groups['generation'] + groups['review'] + groups['jsonl']:
        record(basename, MAX_SCOPE_METADATA_BYTES, False)
    # A completed checkpoint must be valid JSON from this same bounded
    # capture; a matching prefix in truncated data is not completion proof.
    for basename in groups['threepass']:
        record(basename, MAX_SCOPE_METADATA_BYTES, True)
    # Generation-quality manifests are parsed for their status (e-1), so
    # their bounded bytes are retained under the metadata cap.
    for basename in groups['quality']:
        record(basename, MAX_SCOPE_METADATA_BYTES, True)
    try:
        from publication_source import (MAX_PUBLICATION_SOURCE_BYTES,
                                        get_publication_source_path)
        from sound_design_registry import MAX_REGISTRY_BYTES, get_saved_sound_design_path
        from sound_design_renditions import (MAX_RENDITION_BYTES,
                                              get_saved_sound_renditions_path)
        record(os.path.basename(get_publication_source_path(f'{name}.json')),
               MAX_PUBLICATION_SOURCE_BYTES, False)
        record(os.path.basename(get_saved_sound_design_path(f'{name}.json')),
               MAX_REGISTRY_BYTES, False)
        record(os.path.basename(get_saved_sound_renditions_path(f'{name}.json')),
               MAX_RENDITION_BYTES, False)
    except (ImportError, OSError, ValueError, TypeError):
        record(f'{name}.json.publication.json', MAX_SCOPE_METADATA_BYTES, False)
        record(f'.{name}.sound_design.json', MAX_SCOPE_METADATA_BYTES, False)
        record(f'.{name}.sound_design_renditions.json', MAX_SCOPE_METADATA_BYTES, False)
    return snapshot


def _revision_from_snapshot(name, book_id, snapshot):
    """Revision over exact companion bytes, not status/presence booleans."""
    def sha(basename):
        entry = snapshot.get(basename)
        return entry.get('sha') if entry else None
    script_sha = sha(f'{name}.json')
    payload = {
        'name': name, 'book_id': book_id,
        'script': script_sha,
        'voice': sha(f'{name}.voice_config.json'),
        'meta': sha(f'{name}.meta.json'),
        'binding': sha(f'{name}.selection_binding.json'),
        'receipt': sha(os.path.basename(get_review_receipt_path(f'{name}.json'))),
        'manifest': {b: sha(b) for b in _manifest_basenames(name)},
        'checkpoints': {b: sha(b) for b in _checkpoint_basenames(name)},
        'publication': sha(f'{name}.json.publication.json'),
        'sound_design': sha(f'.{name}.sound_design.json'),
        'sound_renditions': sha(f'.{name}.sound_design_renditions.json'),
    }
    # Sound/publication basenames derive from helpers; recompute their
    # canonical names so renamed helpers stay exact.
    try:
        from publication_source import get_publication_source_path
        from sound_design_registry import get_saved_sound_design_path
        from sound_design_renditions import get_saved_sound_renditions_path
        payload['publication'] = sha(os.path.basename(get_publication_source_path(f'{name}.json')))
        payload['sound_design'] = sha(os.path.basename(get_saved_sound_design_path(f'{name}.json')))
        payload['sound_renditions'] = sha(os.path.basename(get_saved_sound_renditions_path(f'{name}.json')))
    except (ImportError, OSError, ValueError, TypeError):
        pass
    return get_snapshot_digest(payload)


def _manifest_status_from_snapshot(snapshot, name):
    ordered = _manifest_basenames(name)
    chosen = None
    for basename in ordered:
        entry = snapshot.get(basename)
        if entry is None:
            continue
        if entry.get('sha') == 'unavailable':
            return 'unreadable'
        if entry.get('exists'):
            chosen = entry
            break
    if chosen is None:
        return 'not_reported'
    raw = chosen.get('raw')
    if raw is None:
        return 'unreadable'
    try:
        manifest = _parse_json_bytes(raw)
    except (OSError, ValueError, TypeError, RecursionError):
        return 'unreadable'
    if not isinstance(manifest, dict):
        return 'unreadable'
    status = manifest.get('status')
    if not isinstance(status, str):
        return 'unreadable'
    return status if status in {'failed', 'incomplete', 'publishing', 'complete'} else 'not_reported'


def _generation_quality_status(snapshot, name):
    """Single-pass completion-manifest status (e-1).

    Returns ``'absent'`` when no quality file exists, the manifest's
    string status when a readable JSON object is present, ``'unreadable'``
    when a present file cannot be parsed as an object, and
    ``'changed_after_completion'`` for a ``complete`` manifest whose bound
    output digest disagrees with the saved script bytes already captured in
    the snapshot. Review and script edits publish new bytes without touching
    this manifest, so that is a finished run followed by a later change, not
    an unfinished run; the review receipt decides whether the change is
    reviewed.
    """
    for basename in _checkpoint_groups(name)['quality']:
        entry = snapshot.get(basename)
        if entry is None:
            continue
        if entry.get('sha') == 'unavailable':
            return 'unreadable'
        if not entry.get('exists'):
            continue
        raw = entry.get('raw')
        if raw is None:
            return 'unreadable'
        try:
            manifest = _parse_json_bytes(raw)
        except (OSError, ValueError, TypeError, RecursionError):
            return 'unreadable'
        if not isinstance(manifest, dict):
            return 'unreadable'
        status = manifest.get('status')
        if not isinstance(status, str):
            return 'unreadable'
        if status != 'complete':
            return status
        artifact = manifest.get('completion_artifact')
        script_entry = snapshot.get(f'{name}.json') or {}
        bound = artifact.get('output_sha256') if isinstance(artifact, dict) else None
        if not isinstance(bound, str) or bound != script_entry.get('sha'):
            return 'changed_after_completion'
        return 'complete'
    return 'absent'


def _has_finished_threepass_checkpoint(snapshot, name):
    """Recognize the compact final document written by save('done').

    Successful publication compacts the indexed checkpoint into ordinary
    JSON. An indexed base may have later shard deltas, so its base stage
    cannot establish completion. Every present canonical/legacy checkpoint
    must be a readable compact document with top-level stage 'done'.
    """
    found = False
    for basename in _checkpoint_groups(name)['threepass']:
        entry = snapshot.get(basename) or {}
        if not entry.get('exists'):
            continue
        found = True
        if entry.get('raw') is None or entry.get('sha') == 'unavailable':
            return False
        try:
            checkpoint = _parse_json_bytes(entry['raw'])
        except (OSError, ValueError, TypeError, RecursionError):
            return False
        if (not isinstance(checkpoint, dict) or 'storage' in checkpoint
                or checkpoint.get('stage') != 'done'):
            return False
    return found


def _has_other_residual(scripts_dir, name, index):
    """Generation/review/jsonl checkpoint presence (three-pass excluded).

    Used to decide whether a ``done`` three-pass checkpoint beside a
    complete manifest may be excused: only when no *other* residual
    checkpoint family remains.
    """
    groups = _checkpoint_groups(name)
    others = groups['generation'] + groups['review'] + groups['jsonl']
    if index is not None:
        names, shards = index['names'], index['shards']
        return any(base in names for base in others) or any(
            base in shards for base in others)
    root = Path(scripts_dir)
    return any((root / base).is_file() for base in others)


def _review_stage_from_entry(entry, scripts_dir, name, entries, *, entries_fingerprint=None):
    if entry is None or entry.get('sha') == 'unavailable':
        return {'status': 'unavailable', 'current_settings': 'not_rechecked',
                'evidence': 'The completed-review receipt could not be read.'}
    if not entry.get('exists'):
        return {'status': 'not_started', 'current_settings': 'not_rechecked',
                'evidence': 'No completed-review receipt is saved for this book.'}
    raw = entry.get('raw')
    if raw is None:
        return {'status': 'unavailable', 'current_settings': 'not_rechecked',
                'evidence': 'The completed-review receipt could not be read.'}
    try:
        receipt = _parse_json_bytes(raw)
    except (OSError, ValueError, TypeError, RecursionError):
        return {'status': 'unavailable', 'current_settings': 'not_rechecked',
                'evidence': 'The completed-review receipt could not be read.'}
    if not isinstance(receipt, dict):
        return {'status': 'unavailable', 'current_settings': 'not_rechecked',
                'evidence': 'The completed-review receipt is not a JSON object.'}
    if type(receipt.get('version')) is not int or receipt.get('version') != REVIEW_RECEIPT_VERSION:
        return {'status': 'stale', 'current_settings': 'not_rechecked',
                'evidence': 'The completed-review receipt version is unsupported.'}
    fingerprint = (entries_fingerprint() if entries_fingerprint is not None
                   else get_review_entries_fingerprint(entries))
    if receipt.get('output_sha256') != fingerprint:
        return {'status': 'stale', 'current_settings': 'not_rechecked',
                'evidence': 'The receipt output no longer matches the current script entries; the script was edited after review.'}
    script_path = str(Path(scripts_dir) / f'{name}.json')
    if receipt.get('output_path') != os.path.abspath(script_path):
        return {'status': 'stale', 'current_settings': 'not_rechecked',
                'evidence': 'The receipt names a different output path; the book was moved or renamed after review.'}
    return {'status': 'recorded', 'current_settings': 'not_rechecked',
            'evidence': ('A completed-review receipt is recorded for this script output '
                         '(current_settings=not_rechecked; review was recorded for these '
                         'entries but is not current review authority and must not skip a '
                         'requested review). This satisfies planning readiness only.')}


def _voices_stage_from_entry(entry, entries, get_voice_rows):
    if entry is None or entry.get('sha') == 'unavailable':
        return {'status': 'unavailable', 'total': 0, 'pending': 0,
                'evidence': 'The saved voice settings could not be read.'}
    if not entry.get('exists'):
        return {'status': 'not_started', 'total': 0, 'pending': 0,
                'evidence': 'No saved voice settings file accompanies this book.'}
    raw = entry.get('raw')
    if raw is None:
        return {'status': 'unavailable', 'total': 0, 'pending': 0,
                'evidence': 'The saved voice settings could not be read.'}
    try:
        voices = _parse_json_bytes(raw)
    except (OSError, ValueError, TypeError, RecursionError):
        return {'status': 'unavailable', 'total': 0, 'pending': 0,
                'evidence': 'The saved voice settings could not be read.'}
    if not isinstance(voices, dict):
        return {'status': 'unavailable', 'total': 0, 'pending': 0,
                'evidence': 'The saved voice settings are not a JSON object.'}
    if any(value is not None and not isinstance(value, dict) for value in voices.values()):
        return {'status': 'unavailable', 'total': 0, 'pending': 0,
                'evidence': 'The saved voice settings contain a non-object entry.'}
    try:
        rows = get_voice_rows(entries, voices)
    except (OSError, ValueError, TypeError, AttributeError, KeyError, RecursionError):
        return {'status': 'unavailable', 'total': 0, 'pending': 0,
                'evidence': 'The saved voice settings could not be read.'}
    pending = sum(bool(row.get('persona_pending') or row.get('persona_states_pending')) for row in rows)
    if pending:
        return {'status': 'needs_decisions', 'total': len(rows), 'pending': pending,
                'evidence': f'{pending} of {len(rows)} characters still need voice preparation.'}
    return {'status': 'ready', 'total': len(rows), 'pending': 0,
            'evidence': 'Every character in the saved script has a prepared voice.'}


def _script_stage(script_sha256, manifest, has_checkpoint, invalid_shard,
                  binding_ok, binding_note, entries_count, *,
                  quality_status='absent', finished_checkpoint=False):
    if invalid_shard:
        return {'status': 'unavailable', 'entries': entries_count,
                'script_sha256': script_sha256,
                'evidence': 'A generation/review checkpoint shard ownership entry could not be verified.'}
    if not binding_ok:
        return {'status': 'unavailable', 'entries': entries_count,
                'script_sha256': script_sha256,
                'evidence': binding_note}
    if manifest == 'failed':
        return {'status': 'failed', 'entries': entries_count, 'script_sha256': script_sha256,
                'evidence': 'The three-pass manifest records a failed script run.'}
    if manifest in {'incomplete', 'publishing'}:
        return {'status': 'incomplete', 'entries': entries_count, 'script_sha256': script_sha256,
                'evidence': f'The three-pass manifest records a {manifest} script run.'}
    if manifest == 'unreadable':
        return {'status': 'unavailable', 'entries': entries_count, 'script_sha256': script_sha256,
                'evidence': 'The three-pass manifest could not be read.'}
    if quality_status == 'unreadable':
        return {'status': 'unavailable', 'entries': entries_count, 'script_sha256': script_sha256,
                'evidence': 'The saved generation completion manifest could not be read.'}
    if quality_status not in ('absent', 'complete', 'changed_after_completion'):
        return {'status': 'failed', 'entries': entries_count, 'script_sha256': script_sha256,
                'evidence': 'The last single-pass generation run did not complete.'}
    if has_checkpoint and not finished_checkpoint:
        return {'status': 'incomplete', 'entries': entries_count, 'script_sha256': script_sha256,
                'evidence': 'A generation/review checkpoint remains; the last script or review run did not publish a final script.'}
    if not entries_count:
        return {'status': 'not_started', 'entries': 0, 'script_sha256': script_sha256,
                'evidence': 'The saved script has no entries.'}
    if quality_status == 'changed_after_completion':
        return {'status': 'available', 'entries': entries_count, 'script_sha256': script_sha256,
                'evidence': ('Saved script entries are present; the script changed after its '
                             'single-pass generation completed (a later review or edit).')}
    return {'status': 'available', 'entries': entries_count, 'script_sha256': script_sha256,
            'evidence': 'Saved script entries are present with no failed or incomplete run marker.'}


def _latest_attempt(attempts):
    """Latest export attempt by started_at (ties prefer failed)."""
    if not attempts:
        return None
    def sort_key(item):
        started = item.get('started_at')
        if type(started) not in (int, float):
            started = 0
        failed = 1 if item.get('status') == 'failed' else 0
        return (started, failed)
    latest = max(attempts, key=sort_key)
    return latest


# Full-book deliveries only: task -> exact output filename. Chapter files
# (chapter_exports/…) and Audacity zips are never whole-book proof, and
# no suffix match certifies an override-named output.
_FULL_OUTPUTS = {'audio': 'cloned_audiobook.mp3', 'm4b_export': 'audiobook.m4b'}
_FULL_LABELS = {'audio': 'MP3', 'm4b_export': 'M4B'}


def _active_render_stage(workspace):
    """Translate active-book workspace facts into a render stage.

    A session export receipt is ``recorded`` (historical) unless it is
    bound to the current input revision for an exact full-book output,
    its attempt is done and is the latest full-delivery attempt, all
    chunk takes are present, the output metadata still matches (checked
    by ``get_session_evidence``) and the source scope is full. Metadata
    identity is not a full content rehash or listening check.
    """
    audio = workspace.get('audio', {})
    exported = workspace.get('export_review', {})
    attempts = []
    reports_raw = []
    if isinstance(exported, dict) and exported.get('status') == 'observed':
        attempts = [item for item in (exported.get('attempts') or [])
                    if isinstance(item, dict)]
        reports_raw = [item for item in (exported.get('reports') or [])
                       if isinstance(item, dict)]
    outputs = workspace.get('outputs', [])
    output_statuses = {row.get('status') for row in outputs if isinstance(row, dict)}
    output_labels = {row.get('format'): row.get('status') for row in outputs if isinstance(row, dict)}
    full_attempts = [a for a in attempts if a.get('task') in _FULL_OUTPUTS]
    latest_full = _latest_attempt(full_attempts) if full_attempts else None

    def _blocked(attempt):
        if attempt is None:
            return None
        if attempt.get('status') == 'running':
            return ('in_progress',
                    'A full-book export attempt for this book is currently running; its result is not yet verified.')
        if attempt.get('status') == 'cancelled':
            return ('cancelled',
                    'The latest full-book export attempt for this book was cancelled; earlier receipts, if any, are unchanged.')
        if attempt.get('status') == 'failed':
            return ('failed',
                    'The latest full-book export attempt for this book failed.')
        return None

    blocked = _blocked(latest_full)
    if blocked is not None:
        return {'status': blocked[0], 'evidence': blocked[1]}
    if audio.get('status') == 'unavailable':
        return {'status': 'unavailable',
                'evidence': 'Saved audio progress could not be read; open Editor to inspect it before preparing new audio.'}
    # Current-bound complete check (metadata-only, fail-closed).
    current_revision = exported.get('current_input_revision') if isinstance(exported, dict) else None
    current_scope = exported.get('source_scope') if isinstance(exported, dict) else None
    if (isinstance(current_revision, str) and current_scope == 'full'
            and audio.get('status') == 'files_present'
            and latest_full is not None and latest_full.get('status') == 'done'):
        done_by_task = {}
        for a in full_attempts:
            if a.get('status') == 'done':
                done_by_task[a.get('task')] = a
        for row in reports_raw:
            if row.get('status') != 'verified':
                continue
            task = row.get('task')
            expected_file = _FULL_OUTPUTS.get(task)
            if expected_file is None or row.get('file') != expected_file:
                continue
            # Skipped-chunk exports are partial deliveries, never complete.
            # The count must be an explicit integer zero; a legacy receipt
            # without the field fails closed.
            if type(row.get('skipped_chunks')) is not int or row['skipped_chunks'] != 0:
                continue
            if row.get('input_binding') != 'current' or row.get('input_revision') != current_revision:
                continue
            if row.get('source_scope') != 'full':
                continue
            attempt = done_by_task.get(task)
            if attempt is None or attempt.get('started_at') != row.get('attempt_started_at'):
                continue
            if attempt.get('input_revision') != current_revision or attempt.get('input_binding') != 'current':
                continue
            if attempt.get('source_scope') != 'full':
                continue
            # A later full-output review receipt outranks this older
            # verified receipt (a-5): the latest delivery needs attention.
            matched_started = attempt.get('started_at')
            if type(matched_started) in (int, float) and any(
                    other.get('status') == 'review'
                    and other.get('file') == _FULL_OUTPUTS.get(other.get('task'))
                    and type(other.get('attempt_started_at')) in (int, float)
                    and other.get('attempt_started_at') > matched_started
                    for other in reports_raw):
                continue
            label = _FULL_LABELS.get(task)
            output_status = output_labels.get(label) if output_labels else None
            if output_status in ('historical_source_scope', 'unavailable', 'not_found', 'missing'):
                continue
            return {'status': 'complete',
                    'evidence': (f'A bound full-book export receipt for {expected_file} was verified at '
                                 'export time and its input revision still matches current script, voice, '
                                 'selection binding, chunk/journal and take metadata under the full source '
                                 'scope (metadata-only: output file version/size/mtime, no full rehash, '
                                 'decode or listening check; audio quality still needs listening).')}
    verified = isinstance(exported, dict) and exported.get('status') == 'observed' and exported.get('verified_at_export')
    # Incomplete chunk audio outranks a historical receipt (a-4): partial
    # or missing takes stay render_incomplete, never recorded/complete.
    if audio.get('status') == 'needs_attention' or (
            audio.get('total') and audio.get('present') != audio.get('total')
            and audio.get('status') != 'files_present'):
        return {'status': 'incomplete',
                'evidence': (f"{audio.get('present', 0)} of {audio.get('total', 0)} chunk audio files are present; "
                             f"{audio.get('failed', 0)} failed and {audio.get('missing', 0)} done rows are missing audio.")}
    if verified:
        # Verified receipts for other formats (Audacity/chapter) or unbound
        # full receipts must not certify the attached MP3/M4B outputs.
        full_verified = [row for row in reports_raw
                         if row.get('status') == 'verified'
                         and row.get('file') == _FULL_OUTPUTS.get(row.get('task'))]
        if reports_raw and not full_verified and audio.get('status') == 'files_present':
            return {'status': 'export_needed',
                    'evidence': ('All chunk audio files are present and a verified receipt covers a '
                                 'different export format, not the attached MP3/M4B outputs; no verified '
                                 'export receipt binds those outputs to this book.')}
        return {'status': 'recorded',
                'evidence': ('A session export receipt verified this output at export time but is not '
                             'bound to the current input revision (unbound, mismatched, scoped, '
                             'partial or stale evidence stays historical); current inputs are not '
                             'certified by this receipt.')}
    if audio.get('status') == 'files_present':
        if 'historical_source_scope' in output_statuses:
            return {'status': 'export_needed',
                    'evidence': ('All chunk audio files are present and an output file exists, but it predates '
                                 'the current source scope and must be rebuilt; no verified export receipt binds '
                                 'it to this book.')}
        if 'unavailable' in output_statuses:
            return {'status': 'export_needed',
                    'evidence': ('All chunk audio files are present, but output files could not be inspected; '
                                 'no verified export receipt binds them to this book.')}
        if 'present_unverified' in output_statuses:
            return {'status': 'export_needed',
                    'evidence': 'All chunk audio files are present and an output file exists, but no verified export receipt binds it to this book.'}
        return {'status': 'export_needed',
                'evidence': 'All chunk audio files are present; no output file was found.'}
    if audio.get('total'):
        return {'status': 'incomplete',
                'evidence': (f"{audio.get('present', 0)} of {audio.get('total', 0)} chunk audio files are present; "
                             f"{audio.get('failed', 0)} failed and {audio.get('missing', 0)} done rows are missing audio.")}
    return {'status': 'not_started',
            'evidence': 'No chunk audio has been prepared for the active book.'}


def _validated_saved_entries(raw):
    """Parse + shape-check saved script bytes (single source; reuse-1)."""
    try:
        entries = _parse_json_bytes(raw)
    except (OSError, ValueError, TypeError, RecursionError) as error:
        raise ValueError('The saved script must be an array of entries') from error
    if not isinstance(entries, list) or any(not isinstance(row, dict) for row in entries):
        raise ValueError('The saved script must be an array of entries')
    return entries


def _saved_book_identity_from_snapshot(snapshot, name):
    """Resolve ``(book_id, meta_status)`` from the meta companion (reuse-1)."""
    meta_entry = snapshot.get(f'{name}.meta.json') or {}
    if meta_entry.get('sha') == 'unavailable':
        return name, 'unavailable'
    if not meta_entry.get('exists'):
        return name, 'missing'
    try:
        meta = _parse_json_bytes(meta_entry.get('raw'))
    except (OSError, ValueError, TypeError, RecursionError):
        return name, 'unavailable'
    if not isinstance(meta, dict):
        return name, 'unavailable'
    value = meta.get('book_id')
    if value is not None and not isinstance(value, str):
        return name, 'unavailable'
    if isinstance(value, str) and value:
        return secure_filename(value), 'present'
    return name, 'present'


def compute_saved_book_revision(scripts_dir, name, *, script_bytes=None,
                                script_sha256=None, script_entries=None):
    """Deterministic revision token for one saved book (read-only).

    One bounded snapshot capture: script (or caller-supplied bytes),
    meta, voice, binding, review receipt, both manifest variants, both
    variants of every checkpoint/quality header, publication and
    sound-design companions — all by exact byte hash. Editing any of
    those bytes changes the proof. ``script_bytes`` reuses load-path
    captured bytes instead of rereading the script. ``script_sha256`` and
    ``script_entries`` reuse caller-validated values instead of
    re-hashing/re-parsing the same bytes (efficiency-6).
    """
    snapshot = _capture_snapshot(scripts_dir, name, script_bytes=script_bytes,
                                 script_sha256=script_sha256)
    script_entry = snapshot.get(f'{name}.json')
    if script_entry is None or not script_entry.get('exists'):
        return None
    raw = script_entry.get('raw')
    if raw is None:
        raise ValueError('Saved script identity requires inspection')
    if script_entries is None:
        _validated_saved_entries(raw)
    elif (not isinstance(script_entries, list)
          or any(not isinstance(row, dict) for row in script_entries)):
        raise ValueError('The saved script must be an array of entries')
    book_id, _meta_status = _saved_book_identity_from_snapshot(snapshot, name)
    return _revision_from_snapshot(name, book_id, snapshot)


def get_saved_book_overview(scripts_dir, name, get_voice_rows, *, active_workspace=None,
                            active_saved_name=None, active_script_sha256=None,
                            active_voice_sha256=None, checkpoint_index=None,
                            active_book_id=None):
    """Project one saved book without switching the active book.

    ``active_workspace`` is attached only when this saved book is the
    active source (``active_saved_name`` matches ``name\"`) AND the active
    DATA_DIR script/voice bytes exactly match this saved snapshot
    (``active_script_sha256``/``active_voice_sha256``). Same book_id under
    two saved names is normal and never copies the workspace to the name
    that is not the loaded source. ``active_book_id`` is retained for
    backward compatibility with direct test callers and is ignored: the
    name+byte fence is the only publication authority.
    """
    # One snapshot serves projection + revision (bounded, single pass).
    snapshot = _capture_snapshot(scripts_dir, name)
    # --- Meta (book_id) ---
    book_id, meta_status = _saved_book_identity_from_snapshot(snapshot, name)
    # --- Script ---
    script_entry = snapshot.get(f'{name}.json') or {}
    if script_entry.get('sha') == 'unavailable':
        raise ValueError('Saved script identity requires inspection')
    if not script_entry.get('exists'):
        return {'name': name, 'book_id': book_id, 'is_active': False,
                'loadable': False,
                'meta': {'status': meta_status,
                         'evidence': 'Book metadata is missing.' if meta_status == 'missing' else 'Book metadata could not be read.'},
                'script': {'status': 'missing', 'entries': 0, 'script_sha256': None,
                           'evidence': 'The saved script file is missing.'},
                'review': {'status': 'unknown', 'current_settings': 'not_rechecked', 'evidence': 'No script to review.'},
                'voices': {'status': 'unknown', 'total': 0, 'pending': 0,
                           'evidence': 'No script to cast.'},
                'render': {'status': 'unknown',
                           'evidence': 'No script; render state cannot be assessed.'},
                'revision': None,
                'overall': 'unknown',
                'next': {'tab': 'script', 'label': 'Inspect saved book',
                         'reason': 'The saved script file is missing; reload or restore the book before proceeding.'}}
    raw = script_entry.get('raw')
    if raw is None:
        raise ValueError('Saved script identity requires inspection')
    entries = _validated_saved_entries(raw)
    script_sha256 = script_entry.get('sha')
    if not isinstance(script_sha256, str) or len(script_sha256) != 64:
        raise ValueError('Saved script identity requires inspection')
    # One canonical fingerprint serves both the binding and the receipt
    # check (lazy: rows whose evidence short-circuits pay nothing).
    fingerprint_cache = []

    def entries_fingerprint():
        if not fingerprint_cache:
            fingerprint_cache.append(get_review_entries_fingerprint(entries))
        return fingerprint_cache[0]
    # --- Companions (from the same snapshot) ---
    manifest = _manifest_status_from_snapshot(snapshot, name)
    index = checkpoint_index if checkpoint_index is not None else _checkpoint_index(scripts_dir)
    has_checkpoint, has_review_checkpoint, invalid_shard = _checkpoint_facts(
        scripts_dir, name, index)
    # Binding (optional; absence stays loadable)
    binding_ok = True
    binding_note = None
    binding_entry = snapshot.get(f'{name}.selection_binding.json') or {}
    if binding_entry.get('sha') == 'unavailable':
        binding_ok = False
        binding_note = 'The saved selection binding could not be read; loading this book will be refused until its identity is inspected/restored.'
        binding_exists, binding_raw = True, None
    else:
        binding_exists, binding_raw = binding_entry.get('exists'), binding_entry.get('raw')
    if binding_exists and binding_ok:
        try:
            binding = parse_script_binding(binding_raw)
        except (OSError, ValueError, TypeError, RecursionError):
            binding_ok = False
            binding_note = 'The saved selection binding could not be read; loading this book will be refused until its identity is inspected/restored.'
        else:
            if not saved_binding_matches(binding, script_sha256=script_sha256,
                                         entries_sha256=entries_fingerprint()):
                binding_ok = False
                binding_note = 'The saved selection binding does not match the current script; loading this book will be refused until its identity is inspected/restored.'
    quality_status = _generation_quality_status(snapshot, name)
    finished_checkpoint = (manifest == 'complete'
                           and _has_finished_threepass_checkpoint(snapshot, name)
                           and not _has_other_residual(scripts_dir, name, index))
    script = _script_stage(script_sha256, manifest, has_checkpoint, invalid_shard,
                           binding_ok, binding_note, len(entries),
                           quality_status=quality_status,
                           finished_checkpoint=finished_checkpoint)
    receipt_entry = snapshot.get(os.path.basename(get_review_receipt_path(f'{name}.json')))
    review = _review_stage_from_entry(receipt_entry, scripts_dir, name, entries,
                                      entries_fingerprint=entries_fingerprint)
    if has_review_checkpoint and review['status'] not in ('unavailable',):
        review = {'status': 'incomplete', 'current_settings': 'not_rechecked',
                  'evidence': 'An incomplete review checkpoint remains beside the receipt.'}
    voice_entry = snapshot.get(f'{name}.voice_config.json')
    voices = _voices_stage_from_entry(voice_entry, entries, get_voice_rows)
    voice_sha = (voice_entry or {}).get('sha') if voice_entry else None
    receipt_sha = (receipt_entry or {}).get('sha') if receipt_entry else None
    # --- Active fencing: name match + exact DATA_DIR bytes ---
    # The router supplies active byte hashes (None = active file missing)
    # whenever active_saved_name is set. Same book_id under two names
    # never attaches: only the named row can match.
    matched_name = active_saved_name is not None and name == active_saved_name
    active_bytes_match = True
    if matched_name and active_workspace is not None:
        if script_sha256 != active_script_sha256 or voice_sha != active_voice_sha256:
            active_bytes_match = False
    is_active = bool(active_workspace is not None and matched_name and active_bytes_match)
    # is_active means "workspace attached identity"; bytes mismatch is
    # reported through render evidence while is_active stays about name.
    name_is_active = bool(active_workspace is not None and matched_name)
    if name_is_active and not active_bytes_match:
        render = {'status': 'unknown',
                  'evidence': ('The active copy differs from this saved snapshot (saved script/voice bytes do '
                               'not match the active ones); active render/export evidence is not attributed to '
                               'this saved book. Load it to inspect render state.')}
    elif is_active:
        render = _active_render_stage(active_workspace)
    else:
        render = {'status': 'unknown',
                  'evidence': ('Saved books carry no chunk-audio or export binding; active-book '
                               'audio and output files are not attributed to this book. Load it to inspect render state.')}
    # Revision proof (deterministic, bounded; saved-file identity only)
    # from the single snapshot above — exact bytes, not booleans.
    revision = _revision_from_snapshot(name, book_id, snapshot)
    # --- Overall precedence: severity across all stages ---
    stages = [('script', script), ('review', review), ('voices', voices), ('render', render)]
    failed = [item for item in stages if item[1]['status'] == 'failed']
    unavailable = [item for item in stages if item[1]['status'] == 'unavailable']
    cancelled = [item for item in stages if item[1]['status'] == 'cancelled']
    incomplete = [item for item in stages if item[1]['status'] == 'incomplete']
    in_progress = [item for item in stages if item[1]['status'] == 'in_progress']
    # Stage -> next-step tab for the affected stage (script is the only
    # label mapping that reaches a next step; render/export overrides it).
    stage_tab = {'script': 'script', 'review': 'script', 'voices': 'voices', 'render': 'editor'}
    stage_label = {'script': 'Inspect script recovery'}
    if failed:
        overall = 'failed'
        affected = failed[0][0]
        next_step = {'tab': stage_tab[affected], 'label': stage_label.get(affected, 'Inspect saved book'),
                     'reason': (failed[0][1]['evidence'] if affected == 'script'
                                else 'A script or export run failed; inspect its saved evidence before retrying.')}
        if affected == 'render':
            next_step = {'tab': 'editor', 'label': 'Inspect export evidence',
                         'reason': 'The latest export attempt failed; inspect its saved evidence before retrying.'}
    elif unavailable:
        overall = 'unknown'
        affected = unavailable[0][0]
        next_step = {'tab': stage_tab[affected], 'label': 'Inspect saved book',
                     'reason': unavailable[0][1]['evidence']}
    elif cancelled:
        overall = 'cancelled'
        next_step = {'tab': 'editor', 'label': 'Continue in Editor',
                     'reason': 'The last export attempt was cancelled; restart it from the existing controls when ready.'}
    elif incomplete:
        # Render-only incomplete is its own overall (Editor).
        script_or_review_incomplete = [item for item in incomplete if item[0] in ('script', 'review')]
        if not script_or_review_incomplete and render['status'] == 'incomplete':
            overall = 'render_incomplete'
            next_step = {'tab': 'editor', 'label': 'Continue in Editor',
                         'reason': 'Chunk audio is only partially present.'}
        else:
            overall = 'incomplete'
            next_step = {'tab': 'script', 'label': 'Inspect script recovery',
                         'reason': 'A script or review run left incomplete checkpoint evidence.'}
    elif in_progress:
        overall = 'export_in_progress'
        next_step = {'tab': 'audio', 'label': 'Export in progress',
                     'reason': 'An export attempt is running; its result is not yet verified.'}
    elif script['status'] == 'not_started':
        overall = 'needs_script'
        next_step = {'tab': 'script', 'label': 'Continue with Script',
                     'reason': 'The saved script has no entries yet.'}
    elif review['status'] != 'recorded':
        # Only a same-revision recorded receipt satisfies the planning
        # milestone (current_settings stays not_rechecked; an actually
        # requested review is never skipped by this).
        overall = 'needs_review'
        next_step = {'tab': 'script', 'label': 'Continue with Script review',
                     'reason': 'The script is saved but no completed review matches the current entries.'}
    elif voices['status'] != 'ready':
        overall = 'needs_voices'
        next_step = {'tab': 'voices', 'label': 'Continue with Voices',
                     'reason': 'Review was recorded for the current script; voice decisions are still pending.'}
    elif render['status'] == 'unknown':
        # Missing render knowledge is not an unmet prerequisite: a known
        # saved script + recorded same-revision review + prepared voices
        # is ready for (navigator-guided) audio even when render state
        # is unknown, e.g. an inactive book. Render stays unknown;
        # no take/audio completion is invented.
        if name_is_active:
            # Active bytes mismatch: navigation still points at loading,
            # but do not present unbounded render as ready when the
            # active copy differs from this snapshot.
            if name_is_active and not active_bytes_match:
                overall = 'unknown'
                next_step = {'tab': 'script', 'label': 'Inspect saved book',
                             'reason': 'Render/export evidence is not bound to this saved book; load it to inspect.'}
            else:
                overall = 'ready_for_audio'
                next_step = {'tab': 'editor', 'label': 'Continue in Editor',
                             'reason': ('Script, recorded review and voices are ready; render state is '
                                        'unknown for this view. Load the book to inspect render state.')}
        else:
            overall = 'ready_for_audio'
            next_step = {'tab': 'editor', 'label': 'Continue in Editor',
                         'reason': ('Script, recorded review and voices are ready; render state is unknown '
                                    'for a saved book. Load the book to prepare audio.')}
    elif render['status'] == 'not_started':
        overall = 'ready_for_audio'
        next_step = {'tab': 'editor', 'label': 'Continue in Editor',
                     'reason': 'Script, recorded review and voices are ready; chunk audio has not been prepared.'}
    elif render['status'] == 'export_needed':
        overall = 'export_needed'
        next_step = {'tab': 'audio', 'label': 'Review output files',
                     'reason': 'Chunk audio is present but no verified export binds it to this book.'}
    elif render['status'] == 'recorded':
        overall = 'export_recorded'
        next_step = {'tab': 'audio', 'label': 'Review historical export evidence',
                     'reason': ('A verified export receipt exists for this book at export time but is not '
                                'bound to the current inputs, so this is historical evidence only.')}
    elif render['status'] == 'complete':
        overall = 'completed'
        next_step = {'tab': 'audio', 'label': 'Review bound export evidence',
                     'reason': ('A full-book export receipt is bound to the current input revision '
                                '(metadata-only; no listening check).')}
    else:
        overall = 'unknown'
        next_step = {'tab': 'script', 'label': 'Inspect saved book',
                     'reason': 'Render/export evidence is not bound to this saved book; load it to inspect.'}
    meta_evidence = {'missing': 'No saved book metadata file; book_id falls back to the saved name.',
                     'present': 'Saved book metadata names this book identity.',
                     'unavailable': 'Saved book metadata could not be read; book_id falls back to the saved name.'}
    return {'name': name, 'book_id': book_id, 'is_active': name_is_active,
            'loadable': bool(binding_ok),
            'meta': {'status': meta_status, 'evidence': meta_evidence.get(meta_status, '')},
            'script': script, 'review': review, 'voices': voices, 'render': render,
            'revision': revision,
            'overall': overall, 'next': next_step}


def _unreadable_book_row(scripts_dir, name, error):
    """Per-row unavailable projection for one unreadable saved book.

    Healthy rows are unaffected; the failing row reports its own
    diagnostic without refusing the whole collection (altitude-3).
    """
    evidence = f'The saved book could not be read ({type(error).__name__}); inspect it before loading.'
    return {'name': name, 'book_id': name, 'is_active': False,
            'loadable': False,
            'meta': {'status': 'unavailable',
                     'evidence': 'Saved book metadata could not be read; book_id falls back to the saved name.'},
            'script': {'status': 'unavailable', 'entries': 0, 'script_sha256': None,
                       'evidence': evidence},
            'review': {'status': 'unavailable', 'current_settings': 'not_rechecked',
                       'evidence': 'No script to review.'},
            'voices': {'status': 'unavailable', 'total': 0, 'pending': 0,
                       'evidence': 'No script to cast.'},
            'render': {'status': 'unknown',
                       'evidence': 'The saved book could not be read; render state cannot be assessed.'},
            'revision': None,
            'overall': 'unknown',
            'next': {'tab': 'script', 'label': 'Inspect saved book',
                     'reason': evidence}}


def get_collection_overview(scripts_dir, names, get_voice_rows, *, active_workspace=None,
                            active_saved_name=None, active_script_sha256=None,
                            active_voice_sha256=None, active_book_id=None):
    """Overview rows for an explicitly selected set of saved books, in order.

    One unreadable book never aborts the others: its row reports
    ``unavailable`` with its own diagnostic. Only failures outside
    per-book projection (bad selection, oversize selection, listing
    failure) propagate to the route.
    """
    seen = []
    for name in names:
        if name not in seen:
            seen.append(name)
    if len(seen) > MAX_OVERVIEW_BOOKS:
        raise ValueError(f'Select at most {MAX_OVERVIEW_BOOKS} books for the collection overview')
    index = _checkpoint_index(scripts_dir)
    rows = []
    for name in seen:
        try:
            rows.append(get_saved_book_overview(scripts_dir, name, get_voice_rows,
                                                active_workspace=active_workspace,
                                                active_saved_name=active_saved_name,
                                                active_script_sha256=active_script_sha256,
                                                active_voice_sha256=active_voice_sha256,
                                                checkpoint_index=index,
                                                active_book_id=active_book_id))
        except (OSError, ValueError, TypeError, RecursionError, AttributeError, KeyError) as error:
            rows.append(_unreadable_book_row(scripts_dir, name, error))
    return rows
