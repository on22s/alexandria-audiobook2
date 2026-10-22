"""Build one read-only, source-backed preview of an explicit suffix."""
import hashlib
import copy
import json
import os
from pathlib import Path
from book_state_transaction import is_book_state_admitted, get_snapshot_digest
from chunk_status_journal import ChunkStatusJournal
from project_scope_files import capture_scope_file
from project_scope_source import get_scope_source_snapshot, get_source_scope
from project_scope_partition import get_scope_partition
from project_scope_voices import get_scope_voice_plan
from project_scope_audio import get_scope_audio_plan
from project_scope_history import get_scope_history_plan
from project_scope_literal import get_literal_scope
from project_scope_assets import get_scope_voice_artifacts
from take_retention import get_retained_audio_paths
from project_scope_exports import get_scope_export_inventory
from merge_integrity import get_selected_source_words


MAX_SCOPE_PLAN_BYTES = 32 * 1024**2


def _get_plan_revision(plan):
    digest = hashlib.sha256()
    size = 0
    encoder = json.JSONEncoder(sort_keys=True, ensure_ascii=False, allow_nan=False,
                               separators=(',', ':'))
    for part in encoder.iterencode(plan):
        raw = part.encode('utf-8')
        size += len(raw)
        if size > MAX_SCOPE_PLAN_BYTES:
            raise ValueError('Project exceeds the bounded scope preview size.')
        digest.update(raw)
    return digest.hexdigest()


def get_scope_preview(root, start_entry, *, cancel_check=None):
    """Caller must exclude conflicting tasks as well as hold book admission.

    This is a preview, not apply authorization. Apply must freshly recompute
    the same complete plan under its own book/task admission.
    """
    root = Path(os.path.abspath(root))
    if not is_book_state_admitted(root):
        raise ValueError('Book admission is required to preview source scope.')
    names = ('state.json', 'annotated_script.json', 'voice_config.json',
             'chunks.json', 'chunks.json.status.jsonl')
    snapshots, data = {}, {}
    for name in names:
        snapshots[name], data[name] = capture_scope_file(root, name, include_bytes=True,
                                                        cancel_check=cancel_check)
    if any(data[name] is None for name in ('state.json','annotated_script.json','chunks.json')):
        raise ValueError('Saved book state, script and chunks are required.')
    state = json.loads(data['state.json'])
    script = json.loads(data['annotated_script.json'])
    voices = json.loads(data['voice_config.json']) if data['voice_config.json'] is not None else {}
    if not isinstance(state, dict):
        raise ValueError('Invalid book state.')
    identity = {key: state.get(key) for key in ('active_book_id', 'book_generation')}
    token = get_snapshot_digest([str(root), identity['active_book_id'], identity['book_generation']])
    chunks = ChunkStatusJournal(root/'chunks.json', identity).get_rows_from_bytes(
        data['chunks.json'], data['chunks.json.status.jsonl'])
    partition = get_scope_partition(script, chunks, start_entry)
    source_file, prepared = get_scope_source_snapshot(state, cancel_check=cancel_check)
    previous_scope = state.get('_source_scope')
    comparison_source = prepared
    if '_source_scope' in state:
        comparison_source = get_selected_source_words(prepared, source_file['sha256'],
            source_file['strip_front_matter'], previous_scope)
    source_scope = get_source_scope(comparison_source, script, start_entry)
    if previous_scope is not None:
        source_scope = {**source_scope,
            'source_text_sha256': previous_scope['source_text_sha256'],
            'source_word_count': previous_scope['source_word_count'],
            'retained_start_word': previous_scope['retained_start_word'] + source_scope['retained_start_word'],
            'retained_end_word': previous_scope['retained_end_word'],
            'script_start_entry': previous_scope['script_start_entry'] + start_entry}
    source_scope = {**source_scope, 'input_sha256': source_file['sha256'],
                    'strip_front_matter': source_file['strip_front_matter']}
    start_chunk = partition['chunk_start']
    selected_script = get_literal_scope(prepared, script, chunks, start_entry, start_chunk,
        input_sha256=source_file['sha256'], strip_front_matter=source_file['strip_front_matter'])
    voice_plan = get_scope_voice_plan(voices, chunks, start_chunk)
    voice_artifacts = get_scope_voice_artifacts(voice_plan, cancel_check=cancel_check)
    audio = get_scope_audio_plan(root, chunks, start_chunk, cancel_check=cancel_check)
    history = get_scope_history_plan(root, token, [row['uid'] for row in chunks[start_chunk:]],
                                     retained_rows=chunks[start_chunk:], cancel_check=cancel_check)
    preserved_paths = {row['audio_path'] for row in chunks[:start_chunk] if row.get('audio_path') is not None}
    preserved_paths.update(path for row in chunks for path in get_retained_audio_paths(row))
    preserved_files = [capture_scope_file(root, path, cancel_check=cancel_check)[0]
                       for path in sorted(preserved_paths)]
    selected_chunks = copy.deepcopy(chunks[start_chunk:])
    for index, row in enumerate(selected_chunks):
        row['id'] = index
        row['source_entry_indices'] = partition['source_entry_indices'][index]
    for name in names:
        current, _ = capture_scope_file(root, name, cancel_check=cancel_check)
        if current != snapshots[name]:
            raise ValueError('Project metadata changed during scope preview.')
    plan = {'version': 1, 'book_token': token, 'files': snapshots,
            'removed_source_start_word': previous_scope['retained_start_word'] if previous_scope else 0,
            'removed_script': script[:start_entry],
            'export_inventory': get_scope_export_inventory(root),
            'source_file': source_file, 'source_scope': source_scope, 'partition': partition,
            'selected_script': selected_script,
            'selected_chunks': selected_chunks,
            'preserved_files': preserved_files,
            'voices': voice_plan, 'voice_artifacts': voice_artifacts, 'audio': audio, 'history': history,
            'removed_uids': [row['uid'] for row in chunks[:start_chunk]],
            'retained_uids': [row['uid'] for row in chunks[start_chunk:]]}
    return {**plan, 'revision': _get_plan_revision(plan)}
