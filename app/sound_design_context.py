"""Bounded read-only cue context under the existing task/book admission.

These are metadata and encoded-source identities, not PCM timing or media
approval. The owned audition worker must capture and verify its actual dry PCM.
"""
import json
import os
from pathlib import Path
import stat

from audio_take_history import MAX_AUDIO_BYTES, _ensure_plain_path, _version
from book_state_transaction import get_snapshot_digest
from chunk_status_journal import ChunkStatusJournal
from json_metadata import get_metadata_json_bytes, get_unique_json_object
from project_scope_admission import is_scope_admitted
from project_scope_files import capture_scope_file
from project_scope_source import get_scope_source_snapshot
from sound_design_cues import MAX_DIALOGUE, _validate_book, _validate_selection
from sound_design_registry import ACTIVE_CUES, MAX_REGISTRY_BYTES, get_sound_design_registry
from source_evidence import MAX_SOURCE_CHARS, _require_text
from sound_design_renditions import (ACTIVE_RENDITIONS, MAX_RENDITION_BYTES,
    get_sound_rendition_ledger, validate_sound_rendition_consumers)


MAX_CONTEXT_BYTES = 64 * 1024**2
_FILES = {'state.json':2*1024**2, 'annotated_script.json':32*1024**2,
          'voice_config.json':8*1024**2, 'config.json':2*1024**2,
          'chunks.json':32*1024**2, 'chunks.json.status.jsonl':32*1024**2,
          ACTIVE_CUES:MAX_REGISTRY_BYTES}


def _refuse_nonfinite(_value):
    raise ValueError('Nonfinite cue context metadata is not supported')


def capture_sound_design_context(root, *, check_active=lambda: None):
    """Do not regenerate UIDs, recover transactions or rewrite selected rows."""
    if not is_scope_admitted(root):
        raise ValueError('Task and book admission are required for cue context')
    root = Path(os.path.abspath(root))
    proofs, data, retained = {}, {}, 0
    files = {**_FILES, ACTIVE_RENDITIONS: MAX_RENDITION_BYTES}
    for name, limit in files.items():
        check_active()
        proofs[name], data[name] = capture_scope_file(root, name, limit=min(limit, MAX_CONTEXT_BYTES-retained or 1),
                                                     include_bytes=True, cancel_check=check_active)
        retained += len(data[name] or b'')
        if retained > MAX_CONTEXT_BYTES:
            raise ValueError('Cue context metadata exceeds its joint byte limit')
    if any(data[name] is None for name in ('state.json','annotated_script.json','chunks.json')):
        raise ValueError('Saved book, script and stable Editor rows are required for cues')
    renditions = (get_sound_rendition_ledger(data[ACTIVE_RENDITIONS])
                  if data[ACTIVE_RENDITIONS] is not None else None)
    script = json.loads(data['annotated_script.json'],object_pairs_hook=get_unique_json_object,
                        parse_constant=_refuse_nonfinite)
    if (not isinstance(script,list) or not 1 <= len(script) <= MAX_DIALOGUE
            or any(not isinstance(row,dict) or not isinstance(row.get('text'),str)
                   or ('speaker' in row and not isinstance(row['speaker'],str)) for row in script)):
        raise ValueError('Saved script needs inspection in Editor before cue authoring')
    del script
    state = json.loads(data['state.json'], object_pairs_hook=get_unique_json_object,
                       parse_constant=_refuse_nonfinite)
    if not isinstance(state, dict):
        raise ValueError('Saved book state must be an object')
    _require_text(state.get('active_book_id'), 1024, 'Selected book identity')
    if state.get('book_generation') is not None:
        _require_text(state['book_generation'], 1024, 'Selected book generation')
    identity = {key:state.get(key) for key in ('active_book_id','book_generation')}
    chunks = ChunkStatusJournal(root/'chunks.json', identity).get_rows_from_bytes(
        data['chunks.json'], data['chunks.json.status.jsonl'])
    if not 1 <= len(chunks) <= MAX_DIALOGUE:
        raise ValueError('Saved dialogue count exceeds the cue limit')
    # Replay the native journal, then bound the detached logical snapshot before
    # fingerprinting it. Its private contents never become an API response.
    get_metadata_json_bytes(chunks, MAX_CONTEXT_BYTES-retained, 'Cue context rows')
    for row in chunks:
        _require_text(row.get('uid'), 128, 'Stable dialogue identity')
    source, _prepared = get_scope_source_snapshot(state, byte_limit=4*MAX_SOURCE_CHARS,
                                                 character_limit=MAX_SOURCE_CHARS,
                                                 cancel_check=check_active)
    del _prepared
    book = dict(book_id=identity['active_book_id'], source_sha256=source['sha256'],
                script_sha256=proofs['annotated_script.json']['sha256'])
    _validate_book(book)
    registry = (get_sound_design_registry(data[ACTIVE_CUES]) if data[ACTIVE_CUES] is not None
                else dict(schema=1, book=dict(book), versions=[], selected={}))
    if registry['book']['book_id'] != book['book_id']:
        raise ValueError('Cue registry belongs to another book; existing files were preserved')
    if renditions is None:
        if any(row['schema'] == 2 for row in registry['versions']):
            raise ValueError('Consuming cue history requires its retained rendition ledger')
        renditions = dict(schema=1, book_id=book['book_id'], records=[], keep=[])
    else:
        validate_sound_rendition_consumers(renditions, registry)
    # Absence is still fenced, but its proof must not alter legacy candidate hashes.
    binding_files = {name: proof for name, proof in proofs.items()
                     if name != ACTIVE_RENDITIONS or proof['exists']}
    binding = dict(files=binding_files, source=source, book_identity=identity,
                   rows_sha256=get_snapshot_digest(chunks))
    # Task/book guards exclude application writers. Fence externally changed
    # files too, so a context never combines generations observed mid-capture.
    for name, proof in proofs.items():
        current, _ = capture_scope_file(root, name, limit=files[name], cancel_check=check_active)
        if current != proof:
            raise ValueError('Cue context metadata changed while capturing; refresh the review')
    path = Path(source['absolute_path'])
    current, _ = capture_scope_file(path.parent, path.name, limit=4*MAX_SOURCE_CHARS,
                                     cancel_check=check_active)
    if any(source[key] != value for key, value in current.items()):
        raise ValueError('Selected source changed while capturing cue context; refresh the review')
    check_active()
    return dict(book=book, book_token=get_snapshot_digest([str(root), *identity.values()]),
                context_revision=get_snapshot_digest(binding), registry_revision=proofs[ACTIVE_CUES]['sha256'],
                binding=binding, registry=registry, chunks=chunks, renditions=renditions,
                ledger_revision=proofs[ACTIVE_RENDITIONS]['sha256'],
                rendition_proof=proofs[ACTIVE_RENDITIONS])


def capture_sound_design_anchor(root, context, chunk_uid, *, check_active=lambda: None):
    """Capture one currently selected encoded clip; never accept a request path."""
    if not is_scope_admitted(root):
        raise ValueError('Task and book admission are required for cue anchors')
    _require_text(chunk_uid, 128, 'Stable dialogue identity')
    rows = [row for row in context['chunks'] if row['uid'] == chunk_uid]
    if len(rows) != 1:
        raise ValueError('Selected dialogue identity is absent')
    row = rows[0]
    revision = row.get('audio_revision')
    if row.get('status') != 'done':
        raise ValueError('The selected dialogue needs a completed take')
    path = row.get('audio_path'); _require_text(path, 4096, 'Selected audio path')
    root = Path(os.path.abspath(root))
    # Preserve existing inside-project absolute/relative aliases, while admitting
    # only their current confined, regular target for bounded hashing.
    target = Path(os.path.realpath(root/path))
    try:
        relative = target.relative_to(root).as_posix()
    except ValueError:
        raise ValueError('Selected dialogue audio escapes the project directory') from None
    audio, _ = capture_scope_file(root, relative, limit=MAX_AUDIO_BYTES, cancel_check=check_active)
    if not audio['exists'] or not audio['size']:
        raise ValueError('Selected dialogue audio is missing or empty')
    from project import get_chunk_generation_inputs
    files = context['binding']['files']
    generation = get_snapshot_digest(dict(inputs=get_chunk_generation_inputs(row),
        voices=files['voice_config.json']['sha256'], settings=files['config.json']['sha256']))
    selection = dict(chunk_uid=chunk_uid, generation_sha256=generation,
                     audio_sha256=audio['sha256'], audio_revision=revision)
    _validate_selection(selection)
    return dict(selection=selection, audio=audio, audio_alias=path,
                anchor_revision=get_snapshot_digest([context['context_revision'], selection, audio]))


def require_sound_design_context_files_current(root, context, anchor=None):
    """Short file-version fence after content verification, under book admission."""
    if not is_scope_admitted(root):
        raise ValueError('Task and book admission are required for cue publication')
    root = Path(os.path.abspath(root))
    def require_file(base, proof):
        path = _ensure_plain_path(base/proof['path'],base)
        if not os.path.lexists(path):
            if proof['exists']:
                raise ValueError('Cue source disappeared before publication')
            return
        info = path.lstat()
        if (not proof['exists'] or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1
                or list(_version(info)) != proof['file_version']):
            raise ValueError('Cue source changed before publication')
    for proof in context['binding']['files'].values():
        require_file(root,proof)
    require_file(root, context['rendition_proof'])
    source = context['binding']['source']
    require_file(Path(source['absolute_path']).parent,source)
    if anchor is not None:
        if Path(os.path.realpath(root/anchor['audio_alias'])) != root/anchor['audio']['path']:
            raise ValueError('Selected dialogue alias changed before cue publication')
        require_file(root,anchor['audio'])
