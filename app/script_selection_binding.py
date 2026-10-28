"""Validate the selection that owns a published script.

The caller must obtain book-state admission while capturing identity and source
digests, and must publish a binding atomically with its script. This module does
not infer a binding from an existing unbound script.
"""
import hashlib
import json
import os
import re
import stat
from pathlib import Path

_DIGEST = re.compile(r'^[0-9a-f]{64}$')
MAX_BINDING_BYTES = 4096
BINDING_NAME = 'annotated_script.selection_binding.json'


def require_published_selection(data_dir):
    """Keep a retained old script out of the newly selected book's operations."""
    from book_state_transaction import reuse_book_state
    from utils import file_lock

    root = Path(data_dir)
    with reuse_book_state(data_dir), file_lock(str(root / 'state.json')):
        state = _read_selection_state(data_dir, required=False)
        if ('script_selection_pending' in state and state['script_selection_pending'] is not False):
            raise ValueError('The selected source has no published script; previous book files were preserved.')


def _digest(value):
    if not isinstance(value, str) or not _DIGEST.fullmatch(value):
        raise ValueError('Invalid script binding digest')
    return value


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate script identity field')
        result[key] = value
    return result


def _read_source_snapshot(path, *, include_bytes=False):
    source = Path(path)
    if source.is_symlink() or not source.is_file():
        raise ValueError('Selected source is unavailable or is a symlink')
    digest = hashlib.sha256()
    pieces = [] if include_bytes else None
    flags = os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0)
    descriptor = os.open(source, flags)
    with os.fdopen(descriptor, 'rb') as f:
        before = os.fstat(f.fileno())
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('Selected source must be a regular file')
        if before.st_size > 512 * 1024**2:
            raise ValueError('Selected source exceeds size limit')
        count = 0
        for block in iter(lambda: f.read(1024 * 1024), b''):
            count += len(block)
            if count > 512 * 1024**2:
                raise ValueError('Selected source exceeds size limit')
            digest.update(block)
            if pieces is not None:
                pieces.append(block)
        after = os.fstat(f.fileno())
    current = source.lstat()
    signature = lambda stat: (stat.st_dev, stat.st_ino, stat.st_size,
                              stat.st_mtime_ns, stat.st_ctime_ns)
    if signature(before) != signature(after) or signature(after) != signature(current):
        raise ValueError('Selected source changed while it was read')
    return digest.hexdigest(), b''.join(pieces) if pieces is not None else None


def _source_digest(path):
    return _read_source_snapshot(path)[0]


def _read_selection_state(data_dir, *, required):
    """Read bounded state; the caller is responsible for operation admission."""
    from project_scope_files import capture_scope_file
    _, raw = capture_scope_file(data_dir, 'state.json', limit=4 * 1024**2, include_bytes=True)
    if raw is None:
        if required:
            raise ValueError('Selected source state is missing')
        return {}
    try:
        state = json.loads(raw, object_pairs_hook=_unique_pairs,
            parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid state number')))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('Selected source state is invalid') from error
    if not isinstance(state, dict):
        raise ValueError('Selected source state is invalid')
    return state


def capture_generation_selection(data_dir, expected_source_path):
    """Capture a run's selection before work starts, under book-state admission."""
    from book_state_transaction import reuse_book_state, get_book_operation_token
    from utils import file_lock

    if not isinstance(expected_source_path, str) or not expected_source_path:
        raise ValueError('Missing source path')
    with reuse_book_state(data_dir), file_lock(str(Path(data_dir) / 'state.json')):
        state = _read_selection_state(data_dir, required=True)
        if state.get('input_file_path') != expected_source_path:
            raise ValueError('Selected source changed before script generation')
        return {'book_token': get_book_operation_token(data_dir, state=state),
                'source_path': expected_source_path,
                'source_sha256': _source_digest(expected_source_path)}


def _path_digest(source_path):
    if not isinstance(source_path, str) or not source_path or len(source_path) > 32768:
        raise ValueError('Invalid selected source path')
    return hashlib.sha256(source_path.encode('utf-8')).hexdigest()


def _validated_script_entries(script_bytes, *, require_entries=True):
    if not isinstance(script_bytes, bytes) or len(script_bytes) > 512 * 1024**2:
        raise ValueError('Invalid script publication bytes')
    def reject_constant(value):
        raise ValueError('Nonfinite value in script publication')
    entries = json.loads(script_bytes, object_pairs_hook=_unique_pairs,
                         parse_constant=reject_constant)
    if (not isinstance(entries, list) or require_entries and not entries
            or any(not isinstance(row, dict) for row in entries)):
        raise ValueError('Script publication must contain entries')
    return entries


def make_script_selection_binding(book_token, source_sha256, published_script, *, source_path):
    """Build evidence from the captured run and the exact publication bytes."""
    from completed_review_receipt import get_review_entries_fingerprint
    entries = _validated_script_entries(published_script)
    return {'version': 3, 'book_token': _digest(book_token),
            'source_sha256': _digest(source_sha256),
            'source_path_sha256': _path_digest(source_path),
            'published_entries_sha256': get_review_entries_fingerprint(entries),
            'published_script_sha256': hashlib.sha256(published_script).hexdigest()}


def parse_script_binding(raw):
    """Validate a bounded binding without trusting its stated identities."""
    if not isinstance(raw, bytes) or len(raw) > MAX_BINDING_BYTES:
        raise ValueError('Invalid script binding')
    try:
        binding = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('Invalid script binding') from error
    if (not isinstance(binding, dict) or set(binding) != {
            'version', 'book_token', 'source_sha256', 'source_path_sha256',
            'published_entries_sha256', 'published_script_sha256'}
            or type(binding['version']) is not int or binding['version'] != 3):
        raise ValueError('Invalid script binding structure')
    _digest(binding['book_token'])
    _digest(binding['source_sha256'])
    _digest(binding['source_path_sha256'])
    _digest(binding['published_entries_sha256'])
    _digest(binding['published_script_sha256'])
    return binding


def check_binding_source(raw, book_token, source_sha256, *, source_path):
    """Check the selected source identity; this does not certify current script edits."""
    binding = parse_script_binding(raw)
    if (binding['book_token'] != _digest(book_token) or binding['source_sha256'] != _digest(source_sha256)
            or binding['source_path_sha256'] != _path_digest(source_path)):
        raise ValueError('Script belongs to a different book selection or source')
    return binding['published_script_sha256']


def _binding_for_controlled_edit_locked(data_dir, old_script, new_script):
    """Build an edit binding while book admission and the state lock are held."""
    from project_scope_files import capture_scope_file
    from book_state_transaction import get_book_operation_token

    state = _read_selection_state(data_dir, required=False)
    if ('script_selection_pending' in state and state['script_selection_pending'] is not False):
        raise ValueError('The selected source has no published script; previous book files were preserved.')
    _, raw = capture_scope_file(data_dir, BINDING_NAME, limit=MAX_BINDING_BYTES, include_bytes=True)
    _validated_script_entries(new_script)
    if raw is None:
        if state.get('script_binding_required') is True:
            raise ValueError('Required script binding is missing')
        return None
    source_path = state.get('input_file_path')
    if not isinstance(source_path, str) or not source_path:
        raise ValueError('Missing source path')
    selected = {'book_token': get_book_operation_token(data_dir, state=state),
                'source_path': source_path, 'source_sha256': _source_digest(source_path)}
    published = check_binding_source(raw, selected['book_token'], selected['source_sha256'],
                                     source_path=selected['source_path'])
    if hashlib.sha256(old_script).hexdigest() != published:
        raise ValueError('Changed script has no verified edit provenance')
    previous = make_script_selection_binding(selected['book_token'], selected['source_sha256'],
                                             old_script, source_path=selected['source_path'])
    if parse_script_binding(raw)['published_entries_sha256'] != previous['published_entries_sha256']:
        raise ValueError('Script binding entry fingerprint is inconsistent')
    binding = make_script_selection_binding(selected['book_token'], selected['source_sha256'],
                                            new_script, source_path=selected['source_path'])
    return json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()


def binding_for_controlled_edit(data_dir, old_script, new_script):
    """Build a replacement binding under the global state-before-artifact order.

    Caller publishes returned bytes atomically with the edit. A missing legacy
    binding remains missing, but the replacement script is still validated.
    """
    from book_state_transaction import reuse_book_state
    from utils import file_lock
    with reuse_book_state(data_dir), file_lock(str(Path(data_dir) / 'state.json')):
        return _binding_for_controlled_edit_locked(data_dir, old_script, new_script)


def _voice_remap_bytes(root, name, mapping, remap):
    from project_scope_files import capture_scope_file
    _, raw = capture_scope_file(root, name, limit=16 * 1024**2, include_bytes=True)
    if raw is None:
        return None, 0
    def reject_constant(value):
        raise ValueError('Nonfinite value in voice configuration')
    try:
        config = json.loads(raw, object_pairs_hook=_unique_pairs, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('Invalid voice configuration; review was not published') from error
    if not isinstance(config, dict):
        raise ValueError('Invalid voice configuration; review was not published')
    moved = remap(config, mapping)
    return (json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
            if moved else None), moved


def _staged_registry_path(data_dir, staging):
    if not staging:
        return None
    if not isinstance(staging, dict) or set(staging) != {'path', 'before', 'after'}:
        raise ValueError('Invalid staged alias registry')
    path = staging['path']
    if (not isinstance(path, str) or os.path.abspath(os.path.dirname(path)) != os.path.abspath(data_dir)
            or not os.path.basename(path)):
        raise ValueError('Review alias registry must belong to the active data directory')
    reserved = {'state.json', 'annotated_script.json', BINDING_NAME, 'voice_config.json',
                'chunks.json', 'chunks.json.status.jsonl', 'annotated_script.json.threepass_manifest.json',
                '.active_book_transaction.json'}
    from book_recovery_archive import _relative_name, _validate_names
    portable_name = _relative_name(os.path.basename(path))
    _validate_names([portable_name])
    name = portable_name.casefold()
    if (name in {item.casefold() for item in reserved} or name.endswith('.lock')
            or name.startswith('.book-switch-')):
        raise ValueError('Review alias registry collides with a book artifact')
    for key in ('before', 'after'):
        value = staging[key]
        if value is not None and (not isinstance(value, bytes) or len(value) > 4 * 1024**2):
            raise ValueError('Invalid staged alias registry bytes')
    return Path(path)


def _add_staged_registry(data_dir, staging, registry, replacements):
    if registry is None:
        return
    from project_scope_files import capture_scope_file
    _, raw = capture_scope_file(data_dir, registry.name, limit=4 * 1024**2, include_bytes=True)
    if raw != staging['before']:
        raise ValueError('Alias registry changed during review; edits were preserved')
    if staging['after'] is not None:
        from speaker_identity import get_validated_alias_graph
        try:
            value = json.loads(staging['after'], object_pairs_hook=_unique_pairs)
            get_validated_alias_graph(value)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError('Invalid staged alias registry') from error
        replacements[registry.name] = staging['after']


def publish_controlled_script_edit(data_dir, old_script, new_script, *,
                                   voice_config_path=None, voice_mapping=None,
                                   remap_voice_config=None, registry_staging=None):
    """Publish a review edit and its voice remap in one recoverable commit."""
    import contextlib
    from book_state_transaction import apply_book_state_locked, reuse_book_state
    from chunk_status_journal import get_chunk_status_journal_path
    from project_scope_files import capture_scope_file
    from utils import file_lock

    root = Path(data_dir)
    script = root / 'annotated_script.json'
    chunks = root / 'chunks.json'
    journal = Path(get_chunk_status_journal_path(chunks))
    registry = _staged_registry_path(data_dir, registry_staging)
    voice = root / 'voice_config.json'
    if voice_mapping and not voice_config_path:
        raise ValueError('Voice remap requires the active book voice config')
    if voice_mapping and voice_config_path:
        if os.path.abspath(voice_config_path) != os.path.abspath(voice):
            raise ValueError('Active review voice config must belong to the active book')
        if not callable(remap_voice_config):
            raise ValueError('Voice remap is unavailable')
    with (reuse_book_state(data_dir), contextlib.ExitStack() as locks):
        for path in (root / 'state.json', script, chunks, root / BINDING_NAME):
            locks.enter_context(file_lock(str(path)))
        if voice_mapping and voice_config_path:
            locks.enter_context(file_lock(str(voice)))
        if registry is not None:
            locks.enter_context(file_lock(str(registry)))
        _, current = capture_scope_file(data_dir, script.name, limit=512 * 1024**2, include_bytes=True)
        if current != old_script:
            raise ValueError('Active script changed during review; edits were preserved')
        binding = _binding_for_controlled_edit_locked(data_dir, old_script, new_script)
        replacements = {script.name: new_script}
        if binding is not None:
            replacements[BINDING_NAME] = binding
        _add_staged_registry(data_dir, registry_staging, registry, replacements)
        moved = 0
        if voice_mapping and voice_config_path:
            voice_bytes, moved = _voice_remap_bytes(data_dir, voice.name, voice_mapping,
                                                     remap_voice_config)
            if voice_bytes is not None:
                replacements[voice.name] = voice_bytes
        for old in (chunks, journal):
            if os.path.lexists(old) and (old.is_symlink() or not old.is_file()):
                raise ValueError('Existing chunk artifact requires inspection')
        apply_book_state_locked(data_dir, replacements, [chunks.name, journal.name])
        return moved


def updated_saved_binding(raw, old_script, new_script):
    """Advance a saved binding only from its exact, self-consistent input."""
    from completed_review_receipt import get_review_entries_fingerprint
    old_entries = _validated_script_entries(old_script, require_entries=False)
    new_entries = _validated_script_entries(new_script, require_entries=False)
    if raw is None:
        return None
    binding = parse_script_binding(raw)
    if (binding['published_script_sha256'] != hashlib.sha256(old_script).hexdigest()
            or binding['published_entries_sha256'] != get_review_entries_fingerprint(old_entries)):
        raise ValueError('Saved script differs from its binding')
    binding['published_script_sha256'] = hashlib.sha256(new_script).hexdigest()
    binding['published_entries_sha256'] = get_review_entries_fingerprint(new_entries)
    return json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()


def saved_script_is_active_source(data_dir, script_path):
    """Check source aliases while the caller holds active-book admission."""
    state = _read_selection_state(data_dir, required=False)
    source = state.get('input_file_path')
    if source is None:
        return False
    if not isinstance(source, str):
        raise ValueError('Invalid active book source')
    try:
        if os.path.samefile(source, script_path):
            return True
    except (FileNotFoundError, OSError):
        pass
    return os.path.normcase(os.path.realpath(source)) == os.path.normcase(os.path.realpath(script_path))


def publish_saved_script_edit(data_dir, scripts_dir, script_path, old_script, new_script, *,
                              voice_config_path=None, voice_mapping=None, remap_voice_config=None,
                              registry_staging=None):
    """Commit a saved review and its companions after exact-input validation."""
    import contextlib
    from book_state_transaction import apply_book_state_locked, reuse_book_state, ensure_saved_book_state
    from project_scope_files import capture_scope_file
    from publication_source import get_publication_source_path, get_saved_publication_source_bytes
    from utils import file_lock

    root, directory, script = Path(data_dir), Path(scripts_dir), Path(script_path)
    if (os.path.abspath(directory.parent) != os.path.abspath(root)
            or os.path.abspath(script.parent) != os.path.abspath(directory)
            or script.suffix != '.json'):
        raise ValueError('Saved review destination must be a direct saved-book file')
    if not isinstance(old_script, bytes) or not isinstance(new_script, bytes):
        raise ValueError('Invalid saved review bytes')
    binding = directory / (script.stem + '.selection_binding.json')
    voice = directory / (script.stem + '.voice_config.json')
    publication = Path(get_publication_source_path(str(script)))
    registry = _staged_registry_path(data_dir, registry_staging)
    if voice_mapping and not voice_config_path:
        raise ValueError('Voice remap requires this saved book voice config')
    if voice_mapping and voice_config_path:
        if os.path.abspath(voice_config_path) != os.path.abspath(voice) or not callable(remap_voice_config):
            raise ValueError('Saved review voice config must belong to this book')
    with reuse_book_state(data_dir), ensure_saved_book_state(data_dir, scripts_dir), contextlib.ExitStack() as locks:
        for path in (script, binding, publication):
            locks.enter_context(file_lock(str(path)))
        if voice_mapping and voice_config_path:
            locks.enter_context(file_lock(str(voice)))
        if registry is not None:
            locks.enter_context(file_lock(str(registry)))
        if saved_script_is_active_source(data_dir, str(script)):
            raise ValueError('This saved book is currently loaded; switch books before reviewing it')
        _, current = capture_scope_file(scripts_dir, script.name, limit=512 * 1024**2, include_bytes=True)
        if current != old_script:
            raise ValueError('Saved script changed during review; edits were preserved')
        _, raw_binding = capture_scope_file(scripts_dir, binding.name, limit=MAX_BINDING_BYTES, include_bytes=True)
        binding_bytes = updated_saved_binding(raw_binding, old_script, new_script)
        publication_bytes = get_saved_publication_source_bytes(str(script), new_script)
        relative = lambda path: os.path.relpath(path, data_dir)
        replacements = {relative(script): new_script}
        _add_staged_registry(data_dir, registry_staging, registry, replacements)
        if binding_bytes is not None:
            replacements[relative(binding)] = binding_bytes
        if publication_bytes is not None:
            replacements[relative(publication)] = publication_bytes
        moved = 0
        if voice_mapping and voice_config_path:
            voice_bytes, moved = _voice_remap_bytes(scripts_dir, voice.name, voice_mapping,
                                                     remap_voice_config)
            if voice_bytes is not None:
                replacements[relative(voice)] = voice_bytes
        apply_book_state_locked(data_dir, replacements, [])
        return moved


def check_script_selection_binding(raw, book_token, source_sha256, current_script,
                                   *, source_path, verified_edit=False):
    """Check source identity and require separate evidence for changed script bytes."""
    script = check_binding_source(raw, book_token, source_sha256, source_path=source_path)
    if not isinstance(current_script, bytes):
        raise ValueError('Current script must be bytes')
    edited = hashlib.sha256(current_script).hexdigest() != script
    if type(verified_edit) is not bool or (edited and not verified_edit):
        raise ValueError('Edited script requires verified edit provenance')
    return {'edited_after_publication': edited}


def publish_generated_script(data_dir, expected_token, expected_source_path,
                             expected_source_sha256, script_bytes, *, expected_run_id,
                             complete_manifest=False):
    """Publish a run's output and binding together after checking its selection.

    The caller supplies identity and source digest captured before the run, not
    values read after generation. The cancellation route revokes this run under
    the same book lock, so either cancellation or this publication wins.
    """
    from book_state_transaction import (apply_book_state_locked, reuse_book_state,
                                        get_book_operation_token)
    from chunk_status_journal import get_chunk_status_journal_path
    from project_scope_files import capture_scope_file
    from utils import file_lock

    _digest(expected_token)
    _digest(expected_source_sha256)
    if not isinstance(expected_run_id, str) or not re.fullmatch(r'[0-9a-f]{32}', expected_run_id):
        raise ValueError('Invalid generation attempt')
    if not isinstance(expected_source_path, str) or not expected_source_path:
        raise ValueError('Missing captured source path')
    if not isinstance(script_bytes, bytes) or len(script_bytes) > 512 * 1024**2:
        raise ValueError('Invalid generated script bytes')
    try:
        def reject_constant(value):
            raise ValueError('Nonfinite value in generated script')
        entries = json.loads(script_bytes, parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('Invalid generated script') from error
    if not isinstance(entries, list) or not entries or any(not isinstance(row, dict) for row in entries):
        raise ValueError('Generated script must be a nonempty list of entries')
    root = Path(data_dir)
    chunks = root / 'chunks.json'
    manifest_path = root / 'annotated_script.json.threepass_manifest.json'
    with (reuse_book_state(data_dir), file_lock(str(root / 'state.json')),
          file_lock(str(root / 'annotated_script.json')),
          file_lock(str(chunks)), file_lock(str(manifest_path))):
        state = _read_selection_state(data_dir, required=True)
        if (state.get('input_file_path') != expected_source_path
                or get_book_operation_token(data_dir, state=state) != expected_token):
            raise ValueError('Book selection changed during script generation')
        if state.get('script_generation_run_id') != expected_run_id:
            raise ValueError('Generation attempt is no longer current')
        if state.get('script_generation_cancel_requested_run_id') == expected_run_id:
            raise ValueError('Generation attempt was cancelled')
        if 'script_generation_base_sha256' not in state:
            raise ValueError('Generation attempt lacks its starting script identity')
        base = state['script_generation_base_sha256']
        if base is not None:
            _digest(base)
        current_script, _ = capture_scope_file(data_dir, 'annotated_script.json',
                                               limit=512 * 1024**2)
        if current_script['sha256'] != base:
            raise ValueError('Active script changed during generation; edits were preserved')
        if _source_digest(expected_source_path) != expected_source_sha256:
            raise ValueError('Selected source changed during script generation')
        binding = make_script_selection_binding(expected_token, expected_source_sha256,
                                                script_bytes, source_path=expected_source_path)
        journal = Path(get_chunk_status_journal_path(chunks))
        for old in (chunks, journal):
            if os.path.lexists(old) and (old.is_symlink() or not old.is_file()):
                raise ValueError('Existing chunk artifact requires inspection')
        replacements = {
            'annotated_script.json': script_bytes,
            BINDING_NAME: json.dumps(binding, sort_keys=True, separators=(',', ':')).encode(),
        }
        state.pop('script_selection_pending', None)
        state.pop('script_generation_run_id', None)
        state.pop('script_generation_cancel_requested_run_id', None)
        state.pop('script_generation_base_sha256', None)
        state['script_generation_published_run_id'] = expected_run_id
        state['script_binding_required'] = True
        replacements['state.json'] = json.dumps(state, ensure_ascii=False, indent=2,
                                                 allow_nan=False).encode('utf-8')
        if complete_manifest:
            _, raw_manifest = capture_scope_file(data_dir, manifest_path.name,
                                                  limit=16 * 1024**2, include_bytes=True)
            if raw_manifest is None:
                raise ValueError('Generation publication manifest unavailable')
            manifest = json.loads(raw_manifest, object_pairs_hook=_unique_pairs)
            if not isinstance(manifest, dict) or manifest.get('status') != 'publishing':
                raise ValueError('Generation is not awaiting publication')
            manifest['status'] = 'complete'
            replacements[manifest_path.name] = json.dumps(
                manifest, ensure_ascii=False, indent=2, allow_nan=False).encode()
        apply_book_state_locked(data_dir, replacements, [chunks.name, journal.name])
