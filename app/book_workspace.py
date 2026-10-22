"""Read-only, book-bound facts for the workflow workspace.

The caller holds the book operation lock. Opening this view must not create
chunks, repair corrupt data, generate audio or certify unbound output files.
"""
import json
from collections import Counter
from pathlib import Path

from audio_validation import is_audio_file_present
from book_state_transaction import get_book_operation_token, get_book_snapshot
from chunk_status_journal import ChunkStatusJournal
from editor_review_facts import get_editor_review_facts
from export_quality_session import get_session_evidence
from script_recovery_facts import get_saved_script_recovery_manifest, three_pass_manifest_path


def _get_audio_review(chunks, get_audio_path, book_token):
    """Summarize existing editor evidence; never run or certify a check."""
    uids = [row.get('uid') for row in chunks]
    if (any(type(row.get('id')) is not int for row in chunks)
            or any(not isinstance(uid, str) or not uid for uid in uids)
            or len(set(uids)) != len(uids)):
        return {'status': 'unavailable'}
    facts = get_editor_review_facts(chunks, get_audio_path, book_token, include_readiness=True)
    speech = Counter(row['speech_evidence'] for row in facts)
    return {'status': 'observed', 'identity_check': 'metadata_only',
            'speech_current_metadata': speech['current_metadata'],
            'speech_not_current': speech['not_current'], 'speech_not_checked': speech['not_checked'],
            'speech_attention': sum(any(issue.startswith('speech_') for issue in row['issues']) for row in facts),
            'drift_flagged': sum('drift' in row['issues'] for row in facts),
            'unknown_speakers': sum('unknown' in row['issues'] for row in facts)}


def get_book_workspace(data_dir, output_paths, get_voice_rows, get_audio_path, *, export_states=None):
    root = Path(data_dir)
    book_token = get_book_operation_token(data_dir)
    snapshot = get_book_snapshot(data_dir, allow_missing_script=True)
    raw = snapshot['script_bytes']
    entries = json.loads(raw) if raw is not None else []
    if not isinstance(entries, list) or any(not isinstance(row, dict) for row in entries):
        raise ValueError('The saved script must be an array of entries')
    if any(value is not None and not isinstance(value, dict) for value in snapshot['voices'].values()):
        raise ValueError('Voice settings must contain objects')
    recovery = get_saved_script_recovery_manifest(str(root),
        three_pass_manifest_path(str(root / 'annotated_script.json')))
    script_recovery = {'status': 'candidate' if recovery else 'not_reported',
                       'eligibility': 'not_rechecked'}
    voice_rows = get_voice_rows(entries, snapshot['voices'])
    pending = sum(bool(row.get('persona_pending') or row.get('persona_states_pending'))
                  for row in voice_rows)
    audio = {'status': 'not_started', 'total': 0, 'present': 0, 'failed': 0,
             'missing': 0, 'generating': 0, 'message': 'Open Editor to prepare audio chunks.',
             'review': {'status': 'not_prepared'}}
    chunks_path = root / 'chunks.json'
    if chunks_path.exists():
        try:
            identity = snapshot['identity']
            chunks = ChunkStatusJournal(chunks_path, {key: identity.get(key)
                        for key in ('active_book_id', 'book_generation')}).get_rows()
            present = missing = failed = generating = 0
            for row in chunks:
                status = row.get('status')
                failed += status == 'error'
                generating += status == 'generating'
                if status == 'done':
                    available = is_audio_file_present(row.get('audio_path'), get_audio_path)
                    present += available
                    missing += not available
            total = len(chunks)
            audio = {'status': 'files_present' if total and present == total else 'needs_attention',
                     'total': total, 'present': present, 'failed': failed, 'missing': missing,
                     'generating': generating, 'review': _get_audio_review(chunks, get_audio_path, book_token),
                     'message': 'File presence is checked; current script and voice dependencies have not been rechecked. Review existing chunks in Editor before generating missing work; audio quality still needs listening.'}
        except (OSError, ValueError, TypeError):
            audio['status'] = 'unavailable'
            audio['review'] = {'status': 'unavailable'}
            audio['message'] = 'Saved audio progress could not be read. Open Editor to inspect it; no repair was attempted.'
    outputs = []
    for label, path in output_paths.items():
        try:
            file = Path(path)
            present = file.is_file() and file.stat().st_size > 0
            status = 'present_unverified' if present else 'not_found'
        except OSError:
            status = 'unavailable'
        outputs.append({'format': label, 'status': status})
    export_review = {'status': 'unavailable'}
    if export_states is not None:
        evidence = get_session_evidence(str(root), export_states, book_token)
        export_review = {
            'status': 'observed', 'identity_check': evidence['identity_check'],
            'history_scope': evidence['history_scope'],
            'verified_at_export': sum(row.get('status') == 'verified' for row in evidence['reports']),
            'review_at_export': sum(row.get('status') == 'review' for row in evidence['reports']),
            'unavailable': evidence['unavailable'],
            'failed_attempts': len(evidence['rejected_attempts']),
            'running': sum(row.get('status') == 'running' for row in evidence['attempts']),
            # A selected-book token survives edits inside that book. These
            # receipts describe completed checks, not current script/voice proof.
            'current_inputs': 'not_rechecked'}
    if recovery:
        next_step = {'tab': 'script', 'label': 'Inspect script recovery',
                     'reason': 'An interrupted script run has saved recovery evidence. Inspect it in Script; source and settings eligibility must be rechecked before resuming.'}
    elif not entries:
        next_step = {'tab': 'script', 'label': 'Continue with Script',
                     'reason': 'Choose a book or generate its annotated script.'}
    elif pending:
        next_step = {'tab': 'voices', 'label': 'Continue with Voices',
                     'reason': f'{pending} characters need voice preparation.'}
    elif audio['status'] == 'files_present' and any(row['status'] == 'present_unverified' for row in outputs):
        next_step = {'tab': 'audio', 'label': 'Review output files',
                     'reason': 'Output files exist; verify their book and content before sharing.'}
    else:
        next_step = {'tab': 'editor', 'label': 'Continue in Editor',
                     'reason': 'Review, render or merge the book audio in Editor.'}
    name = snapshot['identity'].get('active_book_id')
    if name is not None and not isinstance(name, str):
        raise ValueError('Invalid saved book name')
    if not name:
        name = Path(snapshot['identity'].get('input_file_path') or '').stem
    name = Path(name.replace('\\', '/')).name if name else ''
    if get_book_operation_token(data_dir) != book_token:
        raise ValueError('Selected book changed while reading workspace facts')
    return {'book_token': book_token, 'name': name or ('Current book (unnamed)' if entries else 'No book selected'),
            'script': {'status': 'available' if entries else 'not_started', 'entries': len(entries),
                       'recovery': script_recovery},
            'cast': {'total': len(voice_rows), 'pending': pending}, 'audio': audio,
            'outputs': outputs, 'export_review': export_review, 'next': next_step}
