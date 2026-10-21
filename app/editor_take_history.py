"""Editor snapshot admission; never select or relabel a historical recording."""
import os

from audio_take_history import AudioTakeHistory
from book_state_transaction import ensure_book_state, get_book_operation_token, get_book_operation_identity
from chunk_status_journal import get_chunk_audio_key
from utils import file_lock
from take_retention import get_captured_take, get_retained_audio_paths


def capture_editor_take(manager, index, request):
    with ensure_book_state(manager.root_dir):
        if get_book_operation_token(manager.root_dir) != request.expected_book_token:
            raise ValueError('Active book changed. Refresh Editor.')
        with manager._chunks_lock, file_lock(manager.chunks_path):
            rows = manager._get_saved_chunks_locked()
            if not 0 <= index < len(rows):
                raise ValueError('The selected Editor line no longer exists.')
            row = rows[index]
            if (row.get('uid') != request.expected_uid or row.get('audio_revision') != request.expected_audio_revision
                    or row.get('status') != 'done' or not row.get('audio_path')):
                raise ValueError('The selected saved take changed. Refresh Editor.')
            if not manager._is_project_audio_path(row['audio_path']):
                raise ValueError('Take snapshots require saved audio inside the project.')
            captured = get_captured_take(row)
            history = AudioTakeHistory(manager.root_dir)
            return history.capture(os.path.join(manager.root_dir, row['audio_path']),
                request.expected_book_token, request.expected_uid, captured,
                book_label=get_book_operation_identity(manager.root_dir)[1] or '')

def get_editor_takes(manager, book_token, uid=None, *, all_books=False, offset=0):
    with ensure_book_state(manager.root_dir):
        if get_book_operation_token(manager.root_dir) != book_token:
            raise ValueError('Active book changed. Refresh Editor.')
        return AudioTakeHistory(manager.root_dir).list(None if all_books else book_token, uid, offset)


def open_editor_take(manager, book_token, version, sha256, source_book_token, uid):
    with ensure_book_state(manager.root_dir):
        if get_book_operation_token(manager.root_dir) != book_token:
            raise ValueError('Active book changed. Refresh Editor.')
        return AudioTakeHistory(manager.root_dir).open(version, sha256, book_token=source_book_token, uid=uid)


def change_editor_take(manager, request, extra_rows=None):
    with ensure_book_state(manager.root_dir), manager._chunks_lock, file_lock(manager.chunks_path):
        if get_book_operation_token(manager.root_dir) != request.expected_book_token:
            raise ValueError('Active book changed. Refresh Editor.')
        rows = manager._get_saved_chunks_locked()
        if extra_rows is not None:
            rows = [*rows, *extra_rows()]
        references = {get_chunk_audio_key(manager.chunks_path, path) for row in rows
                      for path in (row.get('audio_path'), *get_retained_audio_paths(row))}
        def referenced(path):
            return get_chunk_audio_key(manager.chunks_path, str(path)) in references
        return AudioTakeHistory(manager.root_dir).change(
            request.version, request.sha256, request.action, referenced,
            book_token=request.source_book_token, uid=request.expected_uid)


def get_editor_take_policy(manager):
    from take_retention import get_take_history_policy
    with ensure_book_state(manager.root_dir):
        return {**get_take_history_policy(manager.root_dir, tolerate_invalid=True),
                'book_token': get_book_operation_token(manager.root_dir)}


def change_editor_take_policy(manager, request):
    from take_retention import set_take_history_policy
    with ensure_book_state(manager.root_dir):
        if get_book_operation_token(manager.root_dir) != request.expected_book_token:
            raise ValueError('Active book changed. Refresh automatic history controls.')
        return {**set_take_history_policy(manager, request.enabled, request.expected_revision),
                'book_token': request.expected_book_token}
