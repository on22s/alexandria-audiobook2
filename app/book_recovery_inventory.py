"""Read-only candidate inventory for a future whole-book recovery bundle.

This is intentionally not an authorization to switch or restore a book:
external assets, shared-history merging, missing-file semantics and exact
scope/undo migration still require a qualified implementation.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

from audio_take_history import (AudioTakeHistory, MAX_MANIFEST_BYTES as MAX_HISTORY_MANIFEST_BYTES,
                                _check_cancelled, _ensure_plain_path, _version)
from book_recovery_archive import (MAX_FILES, _direct_root, _relative_name,
                                   _validate_portable_inventory_names)
from book_state_transaction import get_snapshot_digest
from chunk_status_journal import ChunkStatusJournal
from direct_path_admission import is_plain_path
from project_scope_admission import is_scope_admitted
from project_scope_exports import EXPORTS
from project_scope_files import capture_scope_file
from publication_source import MAX_PUBLICATION_SOURCE_BYTES, get_publication_source_document
from sound_design_assets import DIRECTORY as SOUND_ASSETS, get_sound_asset_manifest
from sound_design_registry import ACTIVE_CUES, get_sound_design_registry
from sound_design_renditions import (ACTIVE_RENDITIONS, capture_sound_design_companions,
                                      get_sound_rendition_ledger)
from take_retention import validate_retained_take
from tts import _resolve_asset_path
from utils import secure_filename

METADATA_LIMIT = 64 * 1024**2
STATIC_ARTIFACTS = (
    'state.json', 'annotated_script.json', 'annotated_script.selection_binding.json',
    'voice_config.json', 'chunks.json', 'chunks.json.status.jsonl',
    '.sound_design_cues.json', '.sound_design_renditions.json',
    '.project_scope_undo.json', '.character_roster_undo.json', 'character_aliases.json',
    'annotated_script.json.review_completed.json', 'annotated_script.json.threepass_manifest.json',
    'annotated_script.json.generation_quality.json', 'annotated_script.json.partial.json',
    'script_preflight.json', 'take_history/manifest.json', 'take_history/manifest.pending',
)
CHECKPOINTS = (
    'annotated_script.json.review_checkpoint.json',
    'annotated_script.json.generation_checkpoint.json',
    'annotated_script.json.threepass_checkpoint.json',
)
MAX_ROOT_ENTRIES = MAX_FILES * 16


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate book recovery metadata field')
        result[key] = value
    return result


def _checkpoint_artifacts(root, cancel_check, root_identity):
    """Bound discovery of checkpoint epochs, including interrupted old epochs."""
    prefixes = tuple(name + '.parts-' for name in CHECKPOINTS)
    before_info = root.lstat()
    if (not stat.S_ISDIR(before_info.st_mode) or stat.S_ISLNK(before_info.st_mode)
            or (before_info.st_dev, before_info.st_ino) != root_identity
            or not is_plain_path(root)):
        raise ValueError('Recovery root changed during checkpoint discovery')
    before = list(_version(before_info))
    result, unrecognized = [], False
    with os.scandir(root) as entries:
        for index, entry in enumerate(entries):
            _check_cancelled(cancel_check)
            if index >= MAX_ROOT_ENTRIES:
                raise ValueError('Recovery root entry count exceeds its limit')
            prefix = next((p for p in prefixes if entry.name.startswith(p)), None)
            if prefix is None:
                continue
            if not re.fullmatch(r'[0-9a-f]{32}', entry.name[len(prefix):]):
                unrecognized = True
                continue
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                raise ValueError('Invalid generation checkpoint shard ownership')
            result.append(root / entry.name)
    after = root.lstat()
    if (not stat.S_ISDIR(after.st_mode) or stat.S_ISLNK(after.st_mode)
            or (after.st_dev, after.st_ino) != root_identity
            or list(_version(after)) != before
            or not is_plain_path(root)):
        raise ValueError('Recovery root changed during checkpoint discovery')
    return sorted(result), before, unrecognized


def inspect_active_book_recovery_candidates(root, *, byte_limit, cancel_check=None):
    """Inspect known active-book artifacts without writing or authorizing capture."""
    root = _direct_root(root)
    root_info = root.lstat()
    root_identity = (root_info.st_dev, root_info.st_ino)
    if not is_scope_admitted(root):
        raise ValueError('Task and book admission are required for recovery inventory')
    if type(byte_limit) is not int or byte_limit < 1:
        raise ValueError('Recovery inventory byte limit must be positive')
    _check_cancelled(cancel_check)
    proofs, missing, directories, external, unresolved = {}, set(), {}, [], {
        'whole_book_inventory_not_yet_qualified', 'restore_not_implemented'}
    total = 0
    external_proofs = []
    source_provenance = 'unavailable'

    def provenance_status(proof, raw, kind):
        if raw is None:
            return 'absent'
        try:
            document = get_publication_source_document(raw)
        except (ValueError, TypeError, RecursionError):
            unresolved.add('source_publication_provenance_invalid')
            return 'invalid'
        if not proof['exists'] or document['binding'] != {'kind': kind, 'sha256': proof['sha256']}:
            unresolved.add('source_publication_provenance_stale')
            return 'stale'
        return 'current'

    def add(name, *, required=False, read=False):
        nonlocal total
        _check_cancelled(cancel_check)
        name = _relative_name(name)
        if name in proofs:
            if read:
                current, raw = capture_scope_file(root, name, limit=min(METADATA_LIMIT, max(1, proofs[name]['size'])),
                                                   include_bytes=True, cancel_check=cancel_check)
                if current != proofs[name]:
                    raise ValueError('Recovery candidate changed during inventory')
                return raw
            return None
        if name in missing:
            if required:
                unresolved.add('required_artifact_missing')
            return None
        if len(proofs) + len(missing) + len(directories) + len(external_proofs) >= MAX_FILES:
            raise ValueError('Recovery candidate count exceeds its limit')
        remaining = max(1, byte_limit - total)
        limit = min(METADATA_LIMIT, remaining) if read else remaining
        if name == 'take_history/manifest.json':
            limit = min(limit, MAX_HISTORY_MANIFEST_BYTES)
        proof, raw = capture_scope_file(root, name, limit=limit,
                                        include_bytes=read, cancel_check=cancel_check)
        if not proof['exists']:
            missing.add(name)
            if required:
                unresolved.add('required_artifact_missing')
            return None
        total += proof['size']
        if total > byte_limit:
            raise ValueError('Recovery candidate bytes exceed the limit')
        proofs[name] = proof
        return raw

    def add_tree(path):
        nonlocal total
        pending = [path]
        while pending:
            _check_cancelled(cancel_check)
            node = _ensure_plain_path(pending.pop(), root)
            if not os.path.lexists(node):
                continue
            info = node.lstat()
            rel = node.relative_to(root).as_posix()
            if stat.S_ISDIR(info.st_mode):
                if len(directories) + len(proofs) + len(missing) + len(external_proofs) >= MAX_FILES:
                    raise ValueError('Recovery candidate count exceeds its limit')
                directories[rel] = {'file_version':list(_version(info)),
                                    'mode':stat.S_IMODE(info.st_mode)}
                with os.scandir(node) as entries:
                    for entry in entries:
                        _check_cancelled(cancel_check)
                        if len(pending) + len(directories) + len(proofs) + len(missing) + len(external_proofs) >= MAX_FILES:
                            raise ValueError('Recovery candidate count exceeds its limit')
                        pending.append(Path(entry.path))
            elif stat.S_ISREG(info.st_mode):
                add(rel)
            else:
                raise ValueError('Recovery candidates contain an unsupported artifact')

    state_raw = add('state.json', required=True, read=True)
    if state_raw is None:
        raise ValueError('Active book state is missing')
    try:
        state = json.loads(state_raw, object_pairs_hook=_unique_pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid numeric constant')))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('Invalid active book state') from error
    if not isinstance(state, dict):
        raise ValueError('Invalid active book state')
    book_id = state.get('active_book_id')
    generation = state.get('book_generation')
    if (not isinstance(book_id, str) or not book_id or len(book_id) > 1024
            or not isinstance(generation, str) or not generation or len(generation) > 1024):
        raise ValueError('Invalid active book identity')
    token = get_snapshot_digest([str(root), book_id, generation])
    for name in STATIC_ARTIFACTS:
        if name != 'state.json':
            add(name)
    for name in CHECKPOINTS:
        add(name)
    checkpoints, root_version, unrecognized = _checkpoint_artifacts(
        root, cancel_check, root_identity)
    if unrecognized:
        unresolved.add('unrecognized_checkpoint_artifacts_require_inspection')
    for artifact in checkpoints:
        add_tree(artifact)
    for name in EXPORTS:
        add_tree(root / name)

    source = state.get('input_file_path')
    if not isinstance(source, str) or not os.path.isabs(source):
        unresolved.add('source_path_unavailable_or_not_absolute')
    else:
        import core
        source_path = Path(os.path.abspath(source))
        uploads = Path(os.path.abspath(core.UPLOADS_DIR))
        scripts = Path(os.path.abspath(core.SCRIPTS_DIR))
        name = source_path.name
        saved_stem = name[:-5] if name.endswith('.json') else ''
        saved_reserved = ('.voice_config', '.meta', '.selection_binding', '.json.review_completed',
                          '.json.partial',
                          '.generation_checkpoint', '.generation_quality', '.threepass_checkpoint',
                          '.threepass_manifest', '.review_checkpoint', '.json.publication')
        is_upload = (source_path.parent == uploads and name == secure_filename(name)
                     and source_path.suffix.lower() in ('.txt', '.md'))
        is_saved = (source_path.parent == scripts and bool(saved_stem)
                    and saved_stem == secure_filename(saved_stem)
                    and not saved_stem.casefold().endswith(saved_reserved))
        try:
            source_relative = source_path.relative_to(root).as_posix()
        except ValueError:
            source_relative = None
        if not (is_upload or is_saved):
            # State alone never grants authority over arbitrary project files.
            external.append({'kind': 'selected_source', 'qualified': False})
            unresolved.add('source_path_outside_selection_authority')
        elif source_relative is None:
            if is_upload:
                publication_raw = None
                for relative in (name, name + '.publication.json'):
                    _check_cancelled(cancel_check)
                    if len(proofs) + len(missing) + len(directories) + len(external_proofs) >= MAX_FILES:
                        raise ValueError('Recovery candidate count exceeds its limit')
                    is_publication = relative != name
                    remaining = max(1, byte_limit - total)
                    proof, raw = capture_scope_file(uploads, relative,
                        limit=min(remaining, MAX_PUBLICATION_SOURCE_BYTES) if is_publication else remaining,
                        include_bytes=is_publication, cancel_check=cancel_check)
                    if is_publication:
                        publication_raw = raw
                    if proof['exists']:
                        total += proof['size']
                        if total > byte_limit:
                            raise ValueError('Recovery candidate bytes exceed the limit')
                    elif relative == name:
                        unresolved.add('required_artifact_missing')
                    external_proofs.append((uploads, relative, proof))
                source_provenance = provenance_status(external_proofs[0][2], publication_raw, 'text')
                external.append({'kind': 'configured_upload', 'name': name,
                                 'files': [proof for _, _, proof in external_proofs]})
                unresolved.add('external_source_capture_and_restore_not_implemented')
            else:
                external.append({'kind': 'selected_source', 'qualified': False})
                unresolved.add('external_source_copy_not_supported')
        else:
            add(source_relative, required=True)
            publication_raw = add(source_relative + '.publication.json', read=True)
            source_proof = proofs.get(source_relative, {'exists': False})
            source_provenance = provenance_status(source_proof, publication_raw, 'text' if is_upload else 'script')
            if is_saved:
                unresolved.add('saved_source_classification_not_yet_qualified')

    if 'chunks.json' in proofs:
        chunk_raw = add('chunks.json', read=True)
        journal_raw = add('chunks.json.status.jsonl', read=True) if 'chunks.json.status.jsonl' in proofs else None
        identity = {key:state.get(key) for key in ('active_book_id', 'book_generation')}
        chunks = ChunkStatusJournal(root/'chunks.json', identity).get_rows_from_bytes(chunk_raw, journal_raw)
        for row in chunks:
            _check_cancelled(cancel_check)
            if row.get('status') == 'generating':
                unresolved.add('generating_chunk_requires_recovery')
            paths = []
            if row.get('audio_path'):
                paths.append(row['audio_path'])
            elif row.get('status') == 'done':
                unresolved.add('required_artifact_missing')
            retained = validate_retained_take(row)
            if retained is not None:
                paths.append(retained['archive']['path'] if retained['archive'] else retained['path'])
            for path in paths:
                if not isinstance(path, str) or not path:
                    raise ValueError('Invalid recovery audio path')
                absolute = Path(os.path.abspath(os.path.join(root, path)))
                try:
                    relative = absolute.relative_to(root).as_posix()
                except ValueError as error:
                    raise ValueError('Recovery audio path escapes the project') from error
                import core
                allowed = False
                for base in (Path(os.path.abspath(core.VOICELINES_DIR)), root / 'take_history'):
                    try:
                        if absolute.relative_to(base).parts:
                            allowed = True
                            break
                    except ValueError:
                        pass
                if not allowed:
                    unresolved.add('chunk_audio_outside_known_storage')
                    continue
                add(relative, required=True)
    elif 'chunks.json.status.jsonl' in proofs:
        unresolved.add('chunk_journal_without_snapshot')

    if ACTIVE_CUES in proofs or ACTIVE_RENDITIONS in proofs:
        book_id = state.get('active_book_id')
        if not isinstance(book_id, str) or not book_id:
            unresolved.add('sound_book_identity_unavailable')
            book_id = None
        cue_raw, ledger_raw = capture_sound_design_companions(
            root, ACTIVE_CUES, ACTIVE_RENDITIONS, expected_book_id=book_id,
            cancel_check=cancel_check)
        for name, raw in ((ACTIVE_CUES, cue_raw), (ACTIVE_RENDITIONS, ledger_raw)):
            if ((raw is None) != (name not in proofs) or raw is not None and
                    hashlib.sha256(raw).hexdigest() != proofs[name]['sha256']):
                raise ValueError('Sound companions changed during inventory')
        registry = get_sound_design_registry(cue_raw) if cue_raw is not None else None
        ledger = get_sound_rendition_ledger(ledger_raw) if ledger_raw is not None else None
        references = []
        if registry is not None:
            references.extend((row['recipe']['asset'], None) for row in registry['versions'])
        if ledger is not None:
            for record in ledger['records']:
                references.extend((artifact['canonical'], artifact) for artifact in
                    [record['master'], *[stem['artifact'] for stem in record['stems']]])
        manifest_name = SOUND_ASSETS + '/manifest.json'
        manifest_raw = add(manifest_name, read=True) if references else None
        manifest = get_sound_asset_manifest(manifest_raw) if manifest_raw is not None else {'assets': []}
        registered = {row['asset_id']: row for row in manifest['assets']}
        for facts, artifact in references:
            _check_cancelled(cancel_check)
            asset_id = facts['asset_id'] if artifact is None else artifact['asset_id']
            if not re.fullmatch(r'[0-9a-f]{32}', asset_id):
                unresolved.add('legacy_sound_asset_requires_inspection')
                continue
            row = registered.get(asset_id)
            if row is None or row['state'] != 'ready':
                unresolved.add('sound_asset_missing_or_incomplete')
                continue
            canonical = row['canonical']
            if (any(facts[key] != canonical[key] for key in
                    ('sha256', 'frames', 'frame_rate', 'channels', 'sample_width')) or
                    artifact is not None and (artifact['source'] != row['source'] or
                                              artifact['canonical'] != canonical)):
                unresolved.add('sound_asset_metadata_mismatch')
                continue
            for suffix, expected in (('.source', row['source']), ('.pcm', canonical)):
                name = SOUND_ASSETS + '/' + asset_id + suffix
                add(name, required=True)
                proof = proofs.get(name)
                if proof is not None and (proof['sha256'] != expected['sha256'] or
                                          proof['size'] != expected['bytes']):
                    unresolved.add('sound_asset_bytes_mismatch')
        unresolved.add('sound_shared_asset_restore_not_implemented')

    if 'take_history/manifest.json' in proofs:
        history_raw = add('take_history/manifest.json', read=True)
        history = AudioTakeHistory(root).parse_manifest_bytes(history_raw, cancel_check=cancel_check)
        for row in history['records']:
            if row['book_token'] == token:
                add('take_history/' + row['version'] + row['suffix'], required=True)
                if row['state'] not in ('ready', 'deleted'):
                    unresolved.add('incomplete_take_history_requires_inspection')
        unresolved.add('shared_take_history_restore_not_implemented')
    if 'take_history/manifest.pending' in proofs:
        unresolved.add('pending_take_history_requires_inspection')
    if 'voice_config.json' in proofs:
        import core
        raw = add('voice_config.json', read=True)
        try:
            voices = json.loads(raw, object_pairs_hook=_unique_pairs,
                parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid numeric constant')))
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError('Invalid voice configuration') from error
        if not isinstance(voices, dict):
            raise ValueError('Invalid voice configuration')
        pending, visited, visited_items = [voices], 0, 0
        while pending:
            _check_cancelled(cancel_check)
            current = pending.pop()
            visited += 1
            if visited + len(pending) > MAX_FILES * 16:
                raise ValueError('Voice configuration exceeds recovery traversal limit')
            if isinstance(current, dict):
                visited_items += len(current)
                if visited_items > MAX_FILES * 16:
                    raise ValueError('Voice configuration exceeds recovery traversal limit')
                if visited + len(pending) + len(current) > MAX_FILES * 16:
                    raise ValueError('Voice configuration exceeds recovery traversal limit')
                for key, value in current.items():
                    _check_cancelled(cancel_check)
                    if key in ('ref_audio', 'adapter_path') and value not in (None, ''):
                        if not isinstance(value, str):
                            unresolved.add('voice_asset_path_invalid')
                            continue
                        try:
                            path = Path(os.path.abspath(value if os.path.isabs(value)
                                                        else _resolve_asset_path(value)))
                            bases = ((core.CLONE_VOICES_DIR, core.DESIGNED_VOICES_DIR) if key == 'ref_audio'
                                     else (core.LORA_MODELS_DIR, core.BUILTIN_LORA_DIR))
                            allowed = False
                            for base in bases:
                                candidate_root = Path(os.path.abspath(base))
                                try:
                                    subpath = path.relative_to(candidate_root)
                                except ValueError:
                                    continue
                                if subpath.parts:
                                    allowed = True
                                    break
                            if not allowed:
                                raise ValueError('Voice asset outside configured roots')
                            name = path.relative_to(root).as_posix()
                            _relative_name(name)
                            plain_path = _ensure_plain_path(path, root)
                        except (ValueError, OSError):
                            unresolved.add('external_or_invalid_voice_asset_requires_inspection')
                            continue
                        if not os.path.lexists(plain_path):
                            add(name, required=True)
                        elif path.is_dir():
                            add_tree(path)
                        else:
                            add(name, required=True)
                    if isinstance(value, (dict, list)):
                        pending.append(value)
            elif isinstance(current, list):
                visited_items += len(current)
                if visited_items > MAX_FILES * 16:
                    raise ValueError('Voice configuration exceeds recovery traversal limit')
                if visited + len(pending) + len(current) > MAX_FILES * 16:
                    raise ValueError('Voice configuration exceeds recovery traversal limit')
                for value in current:
                    _check_cancelled(cancel_check)
                    if isinstance(value, (dict, list)):
                        pending.append(value)
        # All configured versions and candidates are considered, but adapter
        # aliases, global manifests and shared voice ownership are not migrated.
        unresolved.add('voice_asset_resolution_and_shared_restore_not_qualified')
    if '.project_scope_undo.json' in proofs or '.character_roster_undo.json' in proofs:
        unresolved.add('path_bound_undo_migration_not_implemented')
    if directories:
        unresolved.add('directory_restore_not_implemented')

    for name, proof in proofs.items():
        _check_cancelled(cancel_check)
        info = _ensure_plain_path(root / name, root).lstat()
        if info.st_nlink != 1 or list(_version(info)) != proof['file_version']:
            raise ValueError('Recovery candidate changed during inventory')
    for name in missing:
        _check_cancelled(cancel_check)
        if os.path.lexists(_ensure_plain_path(root / name, root)):
            raise ValueError('Recovery candidate appeared during inventory')
    for directory, name, proof in external_proofs:
        _check_cancelled(cancel_check)
        if not is_plain_path(directory):
            raise ValueError('Recovery source root changed during inventory')
        path = _ensure_plain_path(directory / name, directory)
        if not proof['exists']:
            if os.path.lexists(path):
                raise ValueError('Recovery external candidate appeared during inventory')
        elif not os.path.lexists(path):
            raise ValueError('Recovery external candidate changed during inventory')
        else:
            info = path.lstat()
            if info.st_nlink != 1 or list(_version(info)) != proof['file_version']:
                raise ValueError('Recovery external candidate changed during inventory')
    for name, observation in directories.items():
        _check_cancelled(cancel_check)
        info = _ensure_plain_path(root / name, root).lstat()
        if (list(_version(info)) != observation['file_version']
                or stat.S_IMODE(info.st_mode) != observation['mode']):
            raise ValueError('Recovery directory changed during inventory')
    _validate_portable_inventory_names(proofs, directories, cancel_check)
    if list(_version(root.lstat())) != root_version:
        raise ValueError('Recovery root changed during inventory')
    if not is_scope_admitted(root):
        raise ValueError('Task and book admission ended during recovery inventory')
    document = {'version':2, 'book_token':token, 'files':[proofs[name] for name in sorted(proofs)],
                'missing':sorted(missing), 'directories':directories, 'external':external,
                'bytes':total, 'source_provenance':source_provenance,
                'unresolved':sorted(unresolved), 'ready_for_capture':False}
    encoded = json.dumps(document, sort_keys=True, separators=(',', ':'), allow_nan=False).encode()
    if len(encoded) > METADATA_LIMIT:
        raise ValueError('Recovery candidate inventory exceeds its metadata limit')
    return {**document, 'revision':hashlib.sha256(encoded).hexdigest()}


def capture_observed_project_candidates(root, *, expected_revision, byte_limit, quota_bytes,
                                        cancel_check=None):
    """Capture observed project files, never a complete or restorable book.

    The caller must already hold task and scope admission. A fresh inventory
    must match the separately retained revision both before copying and just
    before publication. External assets and unresolved items are *not* copied
    or made safe by this operation; the archive explicitly records partial
    status and does not authorize switching sources or restoring a book.
    """
    from book_recovery_archive import create_recovery_archive
    if not isinstance(expected_revision, str) or not re.fullmatch(r'[0-9a-f]{64}', expected_revision):
        raise ValueError('A valid observed inventory revision is required')
    observed = inspect_active_book_recovery_candidates(root, byte_limit=byte_limit,
                                                        cancel_check=cancel_check)
    if observed['revision'] != expected_revision:
        raise ValueError('Recovery inventory changed before capture')
    token = observed['book_token']

    def recheck():
        current = inspect_active_book_recovery_candidates(root, byte_limit=byte_limit,
                                                          cancel_check=cancel_check)
        if current['revision'] != expected_revision or current['book_token'] != token:
            raise ValueError('Recovery inventory changed before publication')

    receipt = create_recovery_archive(root, [row['path'] for row in observed['files']],
        book_token=token, quota_bytes=quota_bytes, cancel_check=cancel_check,
        expected_proofs=observed['files'], observation_revision=expected_revision,
        _prepublication_check=recheck)
    return {**receipt, 'observation_revision':expected_revision, 'observation_complete':False}
