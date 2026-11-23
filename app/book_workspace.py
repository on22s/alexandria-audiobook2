"""Read-only, book-bound facts for the workflow workspace.

The caller holds the book operation lock. Opening this view must not create
chunks, repair corrupt data, generate audio or certify unbound output files.
"""
import json
import logging
from collections import Counter
from pathlib import Path

from workspace_guidance import get_workspace_message, with_workspace_guidance
from audio_validation import is_audio_file_present
from book_state_transaction import get_book_operation_token, get_book_snapshot
from chunk_status_journal import ChunkStatusJournal
from editor_review_facts import get_editor_review_facts
from export_quality_session import get_export_input_revision, get_session_evidence
from project_scope_exports import get_historical_export_reason
from script_recovery_facts import get_saved_script_recovery_manifest, three_pass_manifest_path

logger = logging.getLogger('AlexandriaUI')

WORKSPACE_ENUMS = {
    'recoveryStatus': ['candidate', 'not_reported'],
    'recoveryEligibility': ['not_rechecked'],
    'reviewStatus': ['not_prepared', 'unavailable', 'observed'],
    'exportStatus': ['unavailable', 'observed'],
    'exportInputs': ['not_rechecked'],
    'audioStatus': ['not_started', 'files_present', 'needs_attention', 'unavailable'],
    'outputStatus': ['present_unverified', 'historical_source_scope', 'not_found', 'unavailable'],
    'nextTab': ['script', 'voices', 'editor', 'audio'],
}


def _get_audio_review(chunks, get_audio_path, book_token, voices):
    """Summarize existing editor evidence; never run or certify a check."""
    uids = [row.get('uid') for row in chunks]
    if (any(type(row.get('id')) is not int for row in chunks)
            or any(not isinstance(uid, str) or not uid for uid in uids)
            or len(set(uids)) != len(uids)):
        return {'status': 'unavailable'}
    facts = get_editor_review_facts(chunks, get_audio_path, book_token, include_readiness=True, voice_config=voices)
    speech = Counter(row['speech_evidence'] for row in facts)
    return {'status': 'observed', 'identity_check': 'metadata_only',
            'speech_current_metadata': speech['current_metadata'],
            'speech_not_current': speech['not_current'], 'speech_not_checked': speech['not_checked'],
            'speech_attention': sum(any(issue.startswith('speech_') for issue in row['issues']) for row in facts),
            'drift_flagged': sum('drift' in row['issues'] for row in facts),
            'outlier_flagged': sum('outliers' in row['issues'] for row in facts),
            'outlier_unavailable': sum(row['outlier']['status'] == 'unavailable' for row in facts),
            'outlier_unmeasured': sum(row['outlier']['status'] == 'unmeasured' for row in facts),
            'unknown_speakers': sum('unknown' in row['issues'] for row in facts)}


@with_workspace_guidance
def get_book_workspace(data_dir, output_paths, get_voice_rows, get_audio_path, *, export_states=None, include_input_revision=False):
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
             'missing': 0, 'generating': 0, 'message': get_workspace_message('workspace.guidance.open-editor-to-prepare-audio-chunks'),
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
                     'generating': generating, 'review': _get_audio_review(chunks, get_audio_path, book_token, snapshot['voices']),
                     'message': get_workspace_message('workspace.guidance.file-presence-is-checked-current-script-and-voice-dependencies')}
        except (OSError, ValueError, TypeError):
            audio['status'] = 'unavailable'
            audio['review'] = {'status': 'unavailable'}
            audio['message'] = get_workspace_message('workspace.guidance.saved-audio-progress-could-not-be-read-open-editor')
    outputs = []
    for label, path in output_paths.items():
        try:
            file = Path(path)
            present = file.is_file() and file.stat().st_size > 0
            status = 'present_unverified' if present else 'not_found'
            if present:
                reason = get_historical_export_reason(root, path)
                if reason is not None:
                    status = 'historical_previous_book' if reason == 'book' else 'historical_source_scope'
        except OSError:
            status = 'unavailable'
        outputs.append({'format': label, 'status': status})
    export_review = {'status': 'unavailable', 'attempts': [], 'reports': [],
                     'current_input_revision': None, 'source_scope': 'unknown'}
    if export_states is not None:
        evidence = get_session_evidence(str(root), export_states, book_token)
        # Current input revision under the caller's book lock. This capture
        # hashes the full input set, so it is opt-in (efficiency-5): only
        # the collection overview needs it; the regular workspace view keeps
        # None/unknown so Home refreshes never pay the capture. Unknown
        # capture stays None/unknown so old receipts remain historical.
        current_revision, current_scope = None, 'unknown'
        input_hashes = {'script': None, 'voice': None}
        if include_input_revision:
            try:
                info = get_export_input_revision(str(root), book_token)
                if isinstance(info, dict):
                    current_revision = info.get('revision')
                    current_scope = info.get('scope', 'unknown') if info.get('revision') is not None else 'unknown'
                    script_sha = info.get('script_sha256')
                    voice_sha = info.get('voice_sha256')
                    input_hashes = {
                        'script': script_sha if isinstance(script_sha, str) else None,
                        'voice': voice_sha if isinstance(voice_sha, str) else None,
                    }
            except Exception:
                logger.warning('Book workspace input revision capture failed', exc_info=True)
                current_revision, current_scope = None, 'unknown'
        export_review = {
            'status': 'observed', 'identity_check': evidence['identity_check'],
            'history_scope': evidence['history_scope'],
            'verified_at_export': sum(row.get('status') == 'verified' for row in evidence['reports']),
            'review_at_export': sum(row.get('status') == 'review' for row in evidence['reports']),
            'unavailable': evidence['unavailable'],
            'failed_attempts': len(evidence['rejected_attempts']),
            'running': sum(row.get('status') == 'running' for row in evidence['attempts']),
            # Per-task timing is retained so the overview can order attempts
            # chronologically instead of by fixed task order.
            'attempts': [{'task': row.get('task'), 'status': row.get('status'),
                          'started_at': row.get('started_at'),
                          'input_revision': row.get('input_revision'),
                          'input_binding': row.get('input_binding'),
                          'source_scope': row.get('source_scope')}
                         for row in evidence['attempts']],
            'reports': [{'task': row.get('task'), 'file': row.get('file'),
                         'status': row.get('status'),
                         'input_revision': row.get('input_revision'),
                         'input_binding': row.get('input_binding'),
                         'attempt_started_at': row.get('attempt_started_at'),
                         'skipped_chunks': row.get('skipped_chunks'),
                         'source_scope': row.get('source_scope')}
                        for row in evidence['reports']],
            'current_input_revision': current_revision,
            'input_hashes': input_hashes,
            'source_scope': current_scope,
            # A selected-book token survives edits inside that book. These
            # receipts describe completed checks, not current script/voice proof.
            'current_inputs': 'not_rechecked'}
    if recovery:
        next_step = {'tab': 'script', 'label': get_workspace_message('workspace.guidance.inspect-script-recovery'),
                     'reason': get_workspace_message('workspace.guidance.an-interrupted-script-run-has-saved-recovery-evidence-inspect')}
    elif not entries:
        next_step = {'tab': 'script', 'label': get_workspace_message('workspace.guidance.continue-with-script'),
                     'reason': get_workspace_message('workspace.guidance.choose-a-book-or-generate-its-annotated-script')}
    elif pending:
        next_step = {'tab': 'voices', 'label': get_workspace_message('workspace.guidance.continue-with-voices'),
                     'reason': get_workspace_message('workspace.next.voices-pending', count=pending)}
    elif audio['status'] == 'files_present' and any(row['status'] == 'present_unverified' for row in outputs):
        next_step = {'tab': 'audio', 'label': get_workspace_message('workspace.guidance.review-output-files'),
                     'reason': get_workspace_message('workspace.guidance.output-files-exist-verify-their-book-and-content-before')}
    else:
        next_step = {'tab': 'editor', 'label': get_workspace_message('workspace.guidance.continue-in-editor'),
                     'reason': get_workspace_message('workspace.guidance.review-render-or-merge-the-book-audio-in-editor')}
    name = snapshot['identity'].get('active_book_id')
    if name is not None and not isinstance(name, str):
        raise ValueError('Invalid saved book name')
    if not name:
        name = Path(snapshot['identity'].get('input_file_path') or '').stem
    name = Path(name.replace('\\', '/')).name if name else ''
    if get_book_operation_token(data_dir) != book_token:
        raise ValueError('Selected book changed while reading workspace facts')
    return {'book_token': book_token, 'name': name or (get_workspace_message('workspace.name.unnamed') if entries else get_workspace_message('workspace.name.no-book-selected')),
            'enums': WORKSPACE_ENUMS,
            'script': {'status': 'available' if entries else 'not_started', 'entries': len(entries),
                       'recovery': script_recovery,
                       'sha256': snapshot['script_sha256']},
            'cast': {'total': len(voice_rows), 'pending': pending},
            'audio': audio,
            'outputs': outputs, 'export_review': export_review, 'next': next_step}
