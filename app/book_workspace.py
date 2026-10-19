"""Read-only, book-bound facts for the workflow workspace.

The caller holds the book operation lock. Opening this view must not create
chunks, repair corrupt data, generate audio or certify unbound output files.
"""
import json
from pathlib import Path

from audio_validation import is_audio_file_present
from book_state_transaction import get_book_operation_token, get_book_snapshot
from chunk_status_journal import ChunkStatusJournal


def get_book_workspace(data_dir, output_paths, get_voice_rows, get_audio_path):
    root = Path(data_dir)
    snapshot = get_book_snapshot(data_dir, allow_missing_script=True)
    raw = snapshot['script_bytes']
    entries = json.loads(raw) if raw is not None else []
    if not isinstance(entries, list) or any(not isinstance(row, dict) for row in entries):
        raise ValueError('The saved script must be an array of entries')
    if any(value is not None and not isinstance(value, dict) for value in snapshot['voices'].values()):
        raise ValueError('Voice settings must contain objects')
    voice_rows = get_voice_rows(entries, snapshot['voices'])
    pending = sum(bool(row.get('persona_pending') or row.get('persona_states_pending'))
                  for row in voice_rows)
    audio = {'status': 'not_started', 'total': 0, 'present': 0, 'failed': 0,
             'missing': 0, 'generating': 0, 'message': 'Open Editor to prepare audio chunks.'}
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
                     'generating': generating,
                     'message': 'File presence is checked; audio quality still needs listening.'}
        except (OSError, ValueError, TypeError):
            audio['status'] = 'unavailable'
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
    if not entries:
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
    return {'book_token': get_book_operation_token(data_dir), 'name': name or ('Current book (unnamed)' if entries else 'No book selected'),
            'script': {'status': 'available' if entries else 'not_started', 'entries': len(entries)},
            'cast': {'total': len(voice_rows), 'pending': pending}, 'audio': audio,
            'outputs': outputs, 'next': next_step}
