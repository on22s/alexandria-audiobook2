"""Validate the selection that owns a published script.

The caller must obtain book-state admission while capturing identity and source
digests, and must publish a binding atomically with its script. This module does
not infer a binding from an existing unbound script.
"""
import hashlib
import json
import math
import os
import re
import stat
from pathlib import Path

_DIGEST = re.compile(r'^[0-9a-f]{64}$')
MAX_BINDING_BYTES = 4096
MAX_SOURCE_BINDING_HOPS = 8
MAX_SELECTION_STATE_BYTES = 4 * 1024**2
BINDING_NAME = 'annotated_script.selection_binding.json'
STALE_SOURCE_MESSAGE = 'Selected source changed during generation; preserving prior output'


class PublicationProof(int):
    """The moved count plus the exact bytes a publisher committed.

    It is an ``int`` so every existing caller of the publishers keeps seeing a
    plain count. Review's completion evidence reads only these captured bytes;
    it never re-reads a mutable output/companion path after publication.
    """

    def __new__(cls, moved, *, output_raw_sha256, output_entries_sha256, companions):
        proof = int.__new__(cls, moved)
        proof.output_raw_sha256 = output_raw_sha256
        proof.output_entries_sha256 = output_entries_sha256
        proof.companions = dict(companions)
        return proof


def _committed_identity(path, committed):
    """Identity of exact committed bytes, including an unchanged companion."""
    if not path:
        return None
    return {'path': os.path.abspath(path),
            'sha256': hashlib.sha256(committed).hexdigest() if isinstance(committed, bytes) else None}


def _output_proof_values(new_script):
    from completed_review_receipt import get_review_entries_fingerprint
    try:
        entries = json.loads(new_script)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError, TypeError):
        entries = None
    return (hashlib.sha256(new_script).hexdigest(),
            get_review_entries_fingerprint(entries) if isinstance(entries, list) and entries else None)


def is_script_selection_pending(state):
    """Only an absent flag or explicit False permits published-script access."""
    return 'script_selection_pending' in state and state['script_selection_pending'] is not False


def require_published_selection(data_dir):
    """Keep a retained old script out of the newly selected book's operations."""
    from book_state_transaction import reuse_book_state
    from utils import file_lock

    root = Path(data_dir)
    with reuse_book_state(data_dir), file_lock(str(root / 'state.json')):
        state = _read_selection_state(data_dir, required=False)
        if is_script_selection_pending(state):
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


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('Nonfinite value in JSON')
    return number


def _capture_raw_source(path, *, owning_root=None, include_bytes=True, cancel_check=None):
    """One raw-source authority decision shared by both raw readers.

    Resolves the owning root (known book data directory where the HTTP
    generation context provides it, else the source's own parent so
    explicit CLI sources in ordinary directories keep working) and reads
    through project_scope_files.capture_scope_file, so descriptor,
    plain-path and hardlink guards apply consistently: an aliased
    (symlinked / junction) source parent is refused instead of followed
    into outside bytes (DS3076). `include_bytes` is passed through
    unchanged: digest-only callers stream the hash without retaining
    up to 512 MiB of source bytes.
    """
    from project_scope_files import capture_scope_file
    source_abs = Path(os.path.abspath(path))
    if owning_root is not None:
        root_abs = Path(os.path.abspath(owning_root))
        try:
            relative = source_abs.relative_to(root_abs)
        except ValueError:
            raise ValueError('Selected source is outside the book data directory') from None
    else:
        root_abs = source_abs.parent
        relative = Path(source_abs.name)
    return capture_scope_file(root_abs, relative.as_posix(),
                              limit=512 * 1024**2, include_bytes=include_bytes,
                              cancel_check=cancel_check)


def _read_source_snapshot(path, *, include_bytes=False, owning_root=None,
                          missing_as_fnf=False, cancel_check=None):
    """Read a raw selected source through the shared confined capture seam.

    A plain missing source raises ValueError by default (existing
    contract); callers whose contract is FileNotFoundError opt in
    explicitly via missing_as_fnf=True.
    """
    from docx_publication import capture_docx_group
    group = capture_docx_group(path, owning_root=owning_root,
                               cancel_check=cancel_check)
    if group is not None:
        return group['proofs']['text']['sha256'], group['text_bytes'] if include_bytes else None
    proof, raw = _capture_raw_source(path, owning_root=owning_root,
                                     include_bytes=include_bytes,
                                     cancel_check=cancel_check)
    if not proof.get('exists'):
        if missing_as_fnf:
            raise FileNotFoundError(str(path))
        raise ValueError('Selected source is unavailable')
    return proof['sha256'], raw


def _source_digest(path, *, owning_root=None, cancel_check=None):
    return _read_source_snapshot(path, owning_root=owning_root,
                                 cancel_check=cancel_check)[0]


def require_unchanged_source(source_path, expected_sha256, *, owning_root=None):
    """Refuse publication when the selected source changed after capture.

    Re-verifies the confined source digest at the publication boundary. Shared
    by the legacy completion publisher and the three-pass publisher so a source
    edited or swapped mid-run can never publish a script for stale text.
    """
    if _source_digest(source_path, owning_root=owning_root) != _digest(expected_sha256):
        raise ValueError(STALE_SOURCE_MESSAGE)


def _read_selection_state(data_dir, *, required):
    """Read bounded state; the caller is responsible for operation admission."""
    from project_scope_files import capture_scope_file
    _, raw = capture_scope_file(data_dir, 'state.json', limit=MAX_SELECTION_STATE_BYTES, include_bytes=True)
    if raw is None:
        if required:
            raise ValueError('Selected source state is missing')
        return {}
    try:
        state = json.loads(raw, object_pairs_hook=_unique_pairs, parse_float=_finite_float,
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
                'source_sha256': _source_digest(expected_source_path, owning_root=data_dir)}


def _path_digest(source_path):
    if not isinstance(source_path, str) or not source_path or len(source_path) > 32768:
        raise ValueError('Invalid selected source path')
    return hashlib.sha256(source_path.encode('utf-8')).hexdigest()


def get_bound_source_path(data_dir, binding, *, active, selected=None,
                          saved_books_root=None):
    """Find the one on-disk file whose path digest the binding recorded.

    A binding stores only the digest of its source path, so every surface that
    needs the source itself resolves it the same way: the active selection, the
    legacy uploads directory, then direct saved-book names in the library.
    Library names are matched before any candidate's bytes are opened.
    """
    from core import BASE_DIR, ROOT_DIR
    from utils import get_app_storage_path

    if active:
        candidates = [selected]
    else:
        candidates = [selected] if isinstance(selected, str) and selected else []
        uploads = get_app_storage_path(data_dir, ROOT_DIR, BASE_DIR, 'uploads')
        if os.path.isdir(uploads):
            for path in Path(uploads).iterdir():
                if path.suffix.lower() == '.json':
                    continue
                candidates.extend((str(path), path.as_posix(),
                                   os.path.join(uploads, path.name)))
    paths = {path for path in candidates if _path_digest(path) == binding['source_path_sha256']}
    if not paths and not active:
        from direct_path_admission import is_plain_path
        from routers.scripts_library import _is_saved_book_filename

        # Save As copies the active binding, whose source can be another saved
        # book. Existing bindings contain only a path hash. Match direct
        # library names before opening any candidate's bytes.
        library = (Path(os.path.abspath(saved_books_root))
                   if saved_books_root is not None
                   else Path(os.path.abspath(data_dir), 'scripts'))
        if is_plain_path(library) and library.is_dir():
            paths = {str(path) for path in library.iterdir()
                     if _is_saved_book_filename(path.name)
                     and any(_path_digest(spelling) == binding['source_path_sha256']
                             for spelling in (str(path), path.as_posix()))}
    if len(paths) != 1:
        raise ValueError('Bound source path cannot be verified')
    return paths.pop()


class SavedSourceResolutionError(ValueError):
    """Structured failure when resolving a saved book's verified origin chain."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code
        self.message = message


def _validate_saved_book_member(data_dir, candidate_path, *, saved_books_root=None):
    """Confine a saved script path to a direct plain child of the book library."""
    from direct_path_admission import is_plain_path
    from routers.scripts_library import _is_saved_book_filename

    if not isinstance(data_dir, (str, os.PathLike)) or not os.fspath(data_dir):
        raise SavedSourceResolutionError('outside_library', 'Invalid book data directory.')
    data_root = Path(os.path.abspath(data_dir))
    library = (Path(os.path.abspath(saved_books_root))
               if saved_books_root is not None
               else data_root / 'scripts')
    try:
        library_rel = library.relative_to(data_root)
    except ValueError:
        raise SavedSourceResolutionError(
            'outside_library', 'Saved script library is outside the book data directory.') from None
    if not library_rel.parts:
        raise SavedSourceResolutionError(
            'outside_library', 'Saved script library must be a subdirectory of the book data directory.')
    try:
        if not is_plain_path(data_root) or not is_plain_path(library):
            raise SavedSourceResolutionError(
                'outside_library', 'Saved script library must be a plain directory.')
    except OSError as error:
        raise SavedSourceResolutionError(
            'outside_library', 'Saved script library cannot be verified.') from error
    if not isinstance(candidate_path, (str, os.PathLike)):
        raise SavedSourceResolutionError(
            'outside_library', 'Saved script must be a direct saved book inside the library.')
    text_path = os.fspath(candidate_path)
    if not text_path or '\0' in text_path:
        raise SavedSourceResolutionError(
            'outside_library', 'Saved script must be a direct saved book inside the library.')
    try:
        plain = is_plain_path(text_path)
    except OSError as error:
        raise SavedSourceResolutionError(
            'outside_library', 'Saved script path cannot be verified.') from error
    if not plain:
        raise SavedSourceResolutionError(
            'outside_library', 'Saved script must be a direct plain file inside the library.')
    cand = Path(os.path.abspath(text_path))
    same_parent = os.path.normcase(str(cand.parent)) == os.path.normcase(str(library))
    if not same_parent and os.name != 'nt':
        same_parent = (os.path.normcase(os.path.realpath(cand.parent))
                       == os.path.normcase(os.path.realpath(library)))
    if not same_parent or not _is_saved_book_filename(cand.name):
        raise SavedSourceResolutionError(
            'outside_library', 'Saved script must be a direct saved book inside the library.')
    return data_root, library, library_rel, cand.name


def _has_matching_json_source(data_dir, source_path_sha256):
    """True when a binding's path digest names an existing .json under data_dir.

    A binding records only the digest of its source path, so an unresolvable
    .json source is told apart from a missing one by locating its path. Only
    path strings are hashed (no file bytes are read), symlinked directories are
    not followed, and the scan is bounded.
    """
    root = os.path.abspath(data_dir)
    scanned = 0
    for directory, dirnames, filenames in os.walk(root, onerror=lambda _error: None):
        dirnames[:] = [name for name in dirnames
                       if not os.path.islink(os.path.join(directory, name))]
        for name in filenames:
            scanned += 1
            if scanned > 100_000:
                return False
            if not name.lower().endswith('.json'):
                continue
            path = os.path.join(directory, name)
            if any(_path_digest(spelling) == source_path_sha256
                   for spelling in (path, Path(path).as_posix())):
                return True
    return False


def resolve_saved_book_origin(
    data_dir: str,
    saved_script_path: str,
    *,
    saved_books_root: str | None = None,
    expected_script_sha256: str | None = None,
    require_terminal_file: bool = True,
    cancel_check=None,
) -> dict:
    """Walk a saved library script's verified binding chain to its origin source.

    Lock-free: callers hold any required book/state/library locks.
    Confines every .json hop (including saved_script_path) to direct plain
    files in saved_books_root (default: <data_dir>/scripts) matching
    _is_saved_book_filename, checks expected_script_sha256 at hop 0 when
    given, checks saved_binding_matches AND binding['source_sha256'] at
    EVERY hop (intermediate .json AND terminal prose), enforces cycle and
    MAX_SOURCE_BINDING_HOPS guards, and raises a structured
    SavedSourceResolutionError(code, message) on failure.

    ``cancel_check`` is polled by every bounded read the walk performs (each
    hop's script and binding, and the terminal source), keeping the 512 MiB
    per-hop bound while making a cancelled build observed during the walk
    instead of after it. Cancellation propagates by its own type.
    """
    from project_scope_files import capture_scope_file, MAX_SCRIPT_BYTES

    # The resolver is a shared API (#2354/#2355); a relative data_dir would
    # otherwise hash relative candidate spellings that never match the
    # absolute path digests bindings record.
    if isinstance(data_dir, (str, os.PathLike)) and os.fspath(data_dir):
        data_dir = os.path.abspath(os.fspath(data_dir))
    if isinstance(saved_books_root, (str, os.PathLike)) and os.fspath(saved_books_root):
        saved_books_root = os.path.abspath(os.fspath(saved_books_root))
    if expected_script_sha256 is not None:
        try:
            _digest(expected_script_sha256)
        except ValueError as error:
            raise SavedSourceResolutionError('damaged_binding', 'Invalid expected script digest.') from error
    data_root, library, library_rel, current_name = _validate_saved_book_member(
        data_dir, saved_script_path, saved_books_root=saved_books_root)
    seen = set()
    hops = []
    expected_sha = expected_script_sha256
    for _hop in range(MAX_SOURCE_BINDING_HOPS):
        member_path = library / current_name
        member_rel = (library_rel / current_name).as_posix()
        binding_name = Path(current_name).stem + '.selection_binding.json'
        binding_rel = (library_rel / binding_name).as_posix()
        binding_path = str(library / binding_name)
        seen.add(os.path.normcase(str(member_path)))
        try:
            script_proof, script_raw = capture_scope_file(
                data_root, member_rel, limit=MAX_SCRIPT_BYTES, include_bytes=True,
                cancel_check=cancel_check)
        except (OSError, ValueError) as error:
            raise SavedSourceResolutionError('damaged_binding', 'Saved script cannot be read safely.') from error
        if not script_proof.get('exists') or script_raw is None:
            raise SavedSourceResolutionError('missing_source', 'Saved script file is missing.')
        script_sha256 = script_proof['sha256']
        if expected_sha is not None and script_sha256 != expected_sha:
            raise SavedSourceResolutionError('changed_source', 'Saved script bytes changed since it was linked.')
        try:
            binding_proof, raw_binding = capture_scope_file(
                data_root, binding_rel, limit=MAX_BINDING_BYTES, include_bytes=True,
                cancel_check=cancel_check)
        except (OSError, ValueError) as error:
            raise SavedSourceResolutionError('damaged_binding', 'Saved script binding cannot be read safely.') from error
        if not binding_proof.get('exists') or raw_binding is None:
            raise SavedSourceResolutionError('missing_binding', 'Saved script has no original-source binding.')
        try:
            binding = parse_script_binding(raw_binding)
            entries = _validated_script_entries(script_raw)
        except (ValueError, RecursionError) as error:
            raise SavedSourceResolutionError('damaged_binding', 'Saved script or binding is malformed.') from error
        if not saved_binding_matches(binding, script_sha256=script_sha256, entries=entries):
            raise SavedSourceResolutionError('damaged_binding', 'Saved script differs from its binding.')
        # Carry the literal preparation choice read from the bytes already in
        # hand so the comparison does not read and parse every hop twice.
        try:
            from literal_policy import get_literal_policy
            hop_policy = get_literal_policy(entries)
        except ValueError:
            hop_policy = {'strip_front_matter_invalid': True}
        else:
            hop_policy = {'strip_front_matter': hop_policy.get('strip_front_matter')
                          if isinstance(hop_policy, dict) else None}
        try:
            source_path = get_bound_source_path(
                data_dir, binding, active=False, saved_books_root=library)
        except ValueError as error:
            if _has_matching_json_source(data_dir, binding['source_path_sha256']):
                raise SavedSourceResolutionError(
                    'outside_library', 'Saved script binding points to script JSON outside the library.') from error
            if not require_terminal_file:
                hops.append({'script_path': str(member_path), 'binding_path': binding_path,
                             'source_path': None, 'source_sha256': binding['source_sha256'],
                             **hop_policy})
                return {'source_path': None, 'source_sha256': binding['source_sha256'],
                        'source_path_sha256': binding['source_path_sha256'], 'hops': hops}
            raise SavedSourceResolutionError('missing_source', 'Bound source file is missing.') from error
        if Path(source_path).suffix.lower() != '.json':
            try:
                digest, _ = _read_source_snapshot(
                    source_path, owning_root=data_dir, missing_as_fnf=True,
                    cancel_check=cancel_check)
            except FileNotFoundError as error:
                if not require_terminal_file:
                    hops.append({'script_path': str(member_path), 'binding_path': binding_path,
                                 'source_path': None, 'source_sha256': binding['source_sha256'],
                                 **hop_policy})
                    return {'source_path': None, 'source_sha256': binding['source_sha256'],
                            'source_path_sha256': binding['source_path_sha256'], 'hops': hops}
                raise SavedSourceResolutionError('missing_source', 'Bound source file is missing.') from error
            except (OSError, ValueError) as error:
                message = str(error).lower()
                code = 'outside_library' if ('outside' in message or 'alias' in message) else 'damaged_binding'
                raise SavedSourceResolutionError(code, 'Bound source file cannot be verified safely.') from error
            if digest != binding['source_sha256']:
                raise SavedSourceResolutionError('changed_source', 'Bound source bytes changed.')
            resolved_source = os.path.abspath(source_path)
            hops.append({'script_path': str(member_path), 'binding_path': binding_path,
                         'source_path': resolved_source, 'source_sha256': digest,
                         **hop_policy})
            return {'source_path': resolved_source, 'source_sha256': digest,
                    'source_path_sha256': binding['source_path_sha256'], 'hops': hops}
        _, _, _, next_name = _validate_saved_book_member(
            data_dir, source_path, saved_books_root=library)
        next_path = library / next_name
        if os.path.normcase(str(next_path)) in seen:
            raise SavedSourceResolutionError('cycle', 'Saved script binding chain loops.')
        hops.append({'script_path': str(member_path), 'binding_path': binding_path,
                     'source_path': str(next_path), 'source_sha256': binding['source_sha256'],
                     **hop_policy})
        expected_sha = binding['source_sha256']
        current_name = next_name
    raise SavedSourceResolutionError('hop_limit', 'Saved script binding chain exceeds hop limit.')


def resolve_saved_book_reattachment(
    data_dir: str,
    saved_script_path: str,
    *,
    saved_books_root: str | None = None,
    expected_script_sha256: str | None = None,
    cancel_check=None,
) -> dict:
    """The origin an identical re-upload would reattach this saved book under.

    This is the ONE check the upload route and the retained-EPUB recovery text
    share (#2355). It is ``resolve_saved_book_origin`` with
    ``require_terminal_file=False``: the upload route reattaches an identical
    re-upload when this walk verifies and the incoming source digest equals the
    returned ``source_sha256``, because the saved binding's recorded digest
    stays authoritative after the terminal source file is gone. A terminal file
    that is still present with different bytes is still refused as
    ``changed_source``.

    The recovery text asks this same function, so its advice ("re-upload the
    byte-identical source") and the upload route can never disagree. Callers
    that only need to locate the retained file use ``resolve_saved_book_origin``
    with the terminal file required instead.
    """
    return resolve_saved_book_origin(
        data_dir, saved_script_path, saved_books_root=saved_books_root,
        expected_script_sha256=expected_script_sha256,
        require_terminal_file=False, cancel_check=cancel_check)


def _validated_script_entries(script_bytes, *, require_entries=True):
    if not isinstance(script_bytes, bytes) or len(script_bytes) > 512 * 1024**2:
        raise ValueError('Invalid script publication bytes')
    def reject_constant(value):
        raise ValueError('Nonfinite value in script publication')
    try:
        entries = json.loads(script_bytes, object_pairs_hook=_unique_pairs, parse_float=_finite_float,
                             parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('Invalid script publication') from error
    if (not isinstance(entries, list) or require_entries and not entries
            or any(not isinstance(row, dict) for row in entries)):
        raise ValueError('Script publication must contain entries')
    return entries


def make_script_selection_binding(book_token, source_sha256, published_script, *, source_path):
    """Build evidence from the captured run and the exact publication bytes.

    This is the generic publisher used by generation, edits, save/load and
    rebinding. It deliberately never derives cut coverage: scope evidence must
    come from the controlled scope-apply transaction over the parent rows.
    """
    from completed_review_receipt import get_review_entries_fingerprint
    entries = _validated_script_entries(published_script)
    return {'version': 3, 'book_token': _digest(book_token),
            'source_sha256': _digest(source_sha256),
            'source_path_sha256': _path_digest(source_path),
            'published_entries_sha256': get_review_entries_fingerprint(entries),
            'published_script_sha256': hashlib.sha256(published_script).hexdigest()}


_SCOPE_CUT_FIELDS = frozenset(('version', 'source_sha256', 'parent_policy_sha256',
    'scope_policy_sha256', 'coverage_sha256', 'outer_start', 'outer_end', 'start_word'))


def is_scope_cut_evidence(value):
    """Strict, versioned retained-cut evidence carried by a binding.

    The digest is content-free: it is the ordered coverage of the parent's
    verified retained raw intervals. The endpoints are the exact raw outer
    endpoints of that coverage, so any right/left overrun changes them.
    """
    if not isinstance(value, dict) or set(value) != _SCOPE_CUT_FIELDS:
        return False
    if type(value['version']) is not int or value['version'] != 1:
        return False
    if any(not isinstance(value[key], str) or not _DIGEST.fullmatch(value[key])
           for key in ('source_sha256', 'parent_policy_sha256',
                       'scope_policy_sha256', 'coverage_sha256')):
        return False
    if any(type(value[key]) is not int or value[key] < 0
           for key in ('outer_start', 'outer_end', 'start_word')):
        return False
    return 0 <= value['outer_start'] < value['outer_end']


def get_scope_cut_from_binding(binding):
    """Return verified scope evidence from a parsed binding, else None."""
    if not isinstance(binding, dict):
        return None
    value = binding.get('scope_cut')
    return value if is_scope_cut_evidence(value) else None


def _serialize_binding(binding):
    return json.dumps(binding, sort_keys=True, separators=(',', ':')).encode()


def attach_scope_cut_evidence(binding_bytes, evidence):
    """Attach producer-derived scope evidence inside the book transaction.

    Only the controlled scope-apply path may call this. It never accepts
    evidence read from a script (child) - the caller must derive it from the
    parent rows and prepared source.
    """
    if not is_scope_cut_evidence(evidence):
        raise ValueError('Invalid scope cut evidence')
    if not isinstance(binding_bytes, bytes):
        raise ValueError('Invalid binding bytes')
    binding = parse_script_binding(binding_bytes)
    binding['scope_cut'] = evidence
    serialized = _serialize_binding(binding)
    if len(serialized) > MAX_BINDING_BYTES:
        raise ValueError('Script binding with scope evidence exceeds its byte limit')
    return serialized


def carry_scope_cut_evidence(old_binding_raw, binding):
    """Carry valid existing scope evidence onto a generic rebuilt binding."""
    if not isinstance(old_binding_raw, bytes) or not isinstance(binding, dict):
        return binding
    try:
        evidence = get_scope_cut_from_binding(parse_script_binding(old_binding_raw))
    except (ValueError, RecursionError):
        return binding
    if evidence is not None:
        binding['scope_cut'] = evidence
    return binding


def parse_script_binding(raw):
    """Validate a bounded binding without trusting its stated identities."""
    if not isinstance(raw, bytes) or len(raw) > MAX_BINDING_BYTES:
        raise ValueError('Invalid script binding')
    try:
        binding = json.loads(raw, object_pairs_hook=_unique_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('Invalid script binding') from error
    base = {'version', 'book_token', 'source_sha256', 'source_path_sha256',
            'published_entries_sha256', 'published_script_sha256'}
    if (not isinstance(binding, dict) or not base <= set(binding)
            or set(binding) - (base | {'scope_cut'})
            or type(binding['version']) is not int or binding['version'] != 3):
        raise ValueError('Invalid script binding structure')
    _digest(binding['book_token'])
    _digest(binding['source_sha256'])
    _digest(binding['source_path_sha256'])
    _digest(binding['published_entries_sha256'])
    _digest(binding['published_script_sha256'])
    if 'scope_cut' in binding and not is_scope_cut_evidence(binding['scope_cut']):
        raise ValueError('Invalid scope cut evidence in script binding')
    return binding


def check_binding_source(raw, book_token, source_sha256, *, source_path):
    """Check the selected source identity; this does not certify current script edits."""
    binding = parse_script_binding(raw)
    if (binding['book_token'] != _digest(book_token) or binding['source_sha256'] != _digest(source_sha256)
            or binding['source_path_sha256'] != _path_digest(source_path)):
        raise ValueError('Script belongs to a different book selection or source')
    return binding['published_script_sha256']


def saved_binding_matches(binding, *, script_sha256, entries=None, entries_sha256=None):
    """Self-consistency predicate for a parsed saved binding (single source).

    Shared by the collection overview, the load path and
    ``updated_saved_binding`` so the saved-binding rule cannot drift
    between the view and the gate. ``entries`` is fingerprinted in-place;
    ``entries_sha256`` reuses an already computed fingerprint (the
    overview's lazy single computation).
    """
    from completed_review_receipt import get_review_entries_fingerprint
    fingerprint = (entries_sha256 if entries_sha256 is not None
                   else get_review_entries_fingerprint(entries))
    return (binding.get('published_script_sha256') == script_sha256
            and binding.get('published_entries_sha256') == fingerprint)


def _binding_for_controlled_edit_locked(data_dir, old_script, new_script):
    """Build an edit binding while book admission and the state lock are held."""
    from project_scope_files import capture_scope_file
    from book_state_transaction import get_book_operation_token

    state = _read_selection_state(data_dir, required=False)
    if is_script_selection_pending(state):
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
                'source_path': source_path,
                'source_sha256': _source_digest(source_path, owning_root=data_dir)}
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
    binding = carry_scope_cut_evidence(raw, binding)
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


def _voice_remap_bytes(root, name, mapping, remap, expected_sha256=None):
    """Return (exact committed bytes, moved) for the voice companion.

    An unchanged voice config is committed as the bytes read here under the
    publication lock, so an external edit cannot lend its identity to a fact.
    Absence is stable only when it was already absent at run start and no remap
    needs the file; an unexpected removal still refuses.
    """
    from project_scope_files import capture_scope_file
    _, raw = capture_scope_file(root, name, limit=16 * 1024**2, include_bytes=True)
    if raw is None:
        if not mapping and expected_sha256 is None:
            return None, 0
        raise ValueError('Voice configuration disappeared during review; edits were preserved')
    def reject_constant(value):
        raise ValueError('Nonfinite value in voice configuration')
    try:
        config = json.loads(raw, object_pairs_hook=_unique_pairs, parse_float=_finite_float,
                            parse_constant=reject_constant)
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError('Invalid voice configuration; review was not published') from error
    if not isinstance(config, dict):
        raise ValueError('Invalid voice configuration; review was not published')
    if not mapping or not callable(remap):
        return raw, 0
    config, moved = remap(config, mapping)
    return (json.dumps(config, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')
            if moved else raw), moved


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
                'annotated_script.json.review_checkpoint.json',
                'annotated_script.json.generation_checkpoint.json',
                'annotated_script.json.threepass_checkpoint.json',
                'annotated_script.json.review_completed.json',
                'annotated_script.json.generation_quality.json',
                'annotated_script.json.partial.json', 'script_preflight.json',
                '.sound_design_cues.json', '.sound_design_renditions.json',
                '.project_scope_undo.json', '.character_roster_undo.json',
                'config.json', 'voice_library.json', 'cloned_audiobook.mp3', 'audiobook.m4b',
                '.active_book_transaction.json'}
    from book_recovery_archive import _relative_name, _validate_names
    portable_name = _relative_name(os.path.basename(path))
    _validate_names([portable_name])
    name = portable_name.casefold()
    if (name in {item.casefold() for item in reserved} or name.endswith('.lock')
            or name.startswith('.book-switch-')
            or name.startswith(('annotated_script.json.review_checkpoint.json.parts-',
                                'annotated_script.json.generation_checkpoint.json.parts-',
                                'annotated_script.json.threepass_checkpoint.json.parts-'))):
        raise ValueError('Review alias registry collides with a book artifact')
    for key in ('before', 'after'):
        value = staging[key]
        if value is not None and (not isinstance(value, bytes) or len(value) > MAX_SELECTION_STATE_BYTES):
            raise ValueError('Invalid staged alias registry bytes')
    return Path(path)


def _add_staged_registry(data_dir, staging, registry, replacements):
    """Stage an alias registry and return the exact bytes committed for it."""
    if registry is None:
        return None
    from project_scope_files import capture_scope_file
    _, raw = capture_scope_file(data_dir, registry.name, limit=MAX_SELECTION_STATE_BYTES, include_bytes=True)
    if raw != staging['before']:
        raise ValueError('Alias registry changed during review; edits were preserved')
    from speaker_identity import get_validated_alias_graph
    for registry_bytes in (raw, staging['after']):
        if registry_bytes is None:
            continue
        try:
            value = json.loads(registry_bytes, object_pairs_hook=_unique_pairs)
            get_validated_alias_graph(value)
        except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
            raise ValueError('Invalid staged alias registry') from error
    if staging['after'] is not None:
        replacements[registry.name] = staging['after']
        return staging['after']
    return raw


def verify_review_source_identity(data_dir, expected):
    """Fence captured coordinates while the caller holds publication admission."""
    if expected is None:
        return
    from book_state_transaction import get_book_operation_token
    from project_scope_files import capture_scope_file
    from review_startup import record_failure

    try:
        if expected['book_token'] is not None:
            state = _read_selection_state(data_dir, required=True)
            if (is_script_selection_pending(state)
                    or get_book_operation_token(data_dir, state=state) != expected['book_token']
                    or state.get('input_file_path') != expected['source_path']):
                raise ValueError('Review selection differs from its capture')
        for source in expected['sources']:
            relative = os.path.relpath(source['binding_path'], data_dir).replace(os.sep, '/')
            proof, _ = capture_scope_file(data_dir, relative, limit=MAX_BINDING_BYTES)
            if (proof['sha256'] != source['binding_sha256']
                    or _source_digest(source['source_path'], owning_root=data_dir) != source['source_sha256']):
                raise ValueError('Review source differs from its capture')
    except (OSError, ValueError, TypeError, KeyError):
        record_failure('review_source_identity_changed', task='review')
        raise ValueError('Review source identity changed during review; edits were preserved') from None


def publish_controlled_script_edit(data_dir, old_script, new_script, *,
                                   voice_config_path=None, voice_mapping=None,
                                   remap_voice_config=None, registry_staging=None,
                                   alias_registry_path=None, expected_voice_sha256=None,
                                   expected_source_identity=None):
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
    alias_capture = Path(alias_registry_path) if alias_registry_path else None
    if (alias_capture is not None and registry is not None
            and os.path.abspath(alias_capture) != os.path.abspath(registry)):
        raise ValueError('Review alias registry differs from its staged path')
    voice = root / 'voice_config.json'
    capture_voice = bool(voice_config_path) and (
        os.path.abspath(voice_config_path) == os.path.abspath(voice))
    if voice_mapping and not voice_config_path:
        raise ValueError('Voice remap requires the active book voice config')
    if voice_mapping and voice_config_path:
        if not capture_voice:
            raise ValueError('Active review voice config must belong to the active book')
        if not callable(remap_voice_config):
            raise ValueError('Voice remap is unavailable')
    with (reuse_book_state(data_dir), contextlib.ExitStack() as locks):
        for path in (root / 'state.json', script, chunks, root / BINDING_NAME):
            locks.enter_context(file_lock(str(path)))
        if capture_voice:
            locks.enter_context(file_lock(str(voice)))
        if registry is not None:
            locks.enter_context(file_lock(str(registry)))
        elif alias_capture is not None:
            locks.enter_context(file_lock(str(alias_capture)))
        _, current = capture_scope_file(data_dir, script.name, limit=512 * 1024**2, include_bytes=True)
        if current != old_script:
            raise ValueError('Active script changed during review; edits were preserved')
        verify_review_source_identity(data_dir, expected_source_identity)
        binding = _binding_for_controlled_edit_locked(data_dir, old_script, new_script)
        replacements = {script.name: new_script}
        if binding is not None:
            replacements[BINDING_NAME] = binding
        alias_committed = _add_staged_registry(data_dir, registry_staging, registry, replacements)
        if alias_committed is None and registry is None and alias_capture is not None:
            _, alias_committed = capture_scope_file(
                data_dir, alias_capture.name, limit=MAX_SELECTION_STATE_BYTES, include_bytes=True)
        moved = 0
        voice_committed = None
        if capture_voice:
            voice_committed, moved = _voice_remap_bytes(data_dir, voice.name, voice_mapping,
                                                         remap_voice_config, expected_voice_sha256)
            if moved:
                replacements[voice.name] = voice_committed
        for old in (chunks, journal):
            if os.path.lexists(old) and (old.is_symlink() or not old.is_file()):
                raise ValueError('Existing chunk artifact requires inspection')
        apply_book_state_locked(data_dir, replacements, [chunks.name, journal.name])
        companions = {'aliases': _committed_identity(alias_capture or registry, alias_committed),
                      'voice': _committed_identity(voice if capture_voice else None, voice_committed)}
        raw_sha, entries_sha = _output_proof_values(new_script)
        return PublicationProof(moved, output_raw_sha256=raw_sha,
                                output_entries_sha256=entries_sha, companions=companions)


def updated_saved_binding(raw, old_script, new_script):
    """Advance a saved binding only from its exact, self-consistent input."""
    from completed_review_receipt import get_review_entries_fingerprint
    old_entries = _validated_script_entries(old_script, require_entries=False)
    new_entries = _validated_script_entries(new_script, require_entries=False)
    if raw is None:
        return None
    binding = parse_script_binding(raw)
    if not saved_binding_matches(binding, script_sha256=hashlib.sha256(old_script).hexdigest(),
                                 entries=old_entries):
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
                              registry_staging=None, alias_registry_path=None,
                              expected_voice_sha256=None, expected_source_identity=None):
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
    alias_capture = Path(alias_registry_path) if alias_registry_path else None
    if (alias_capture is not None and registry is not None
            and os.path.abspath(alias_capture) != os.path.abspath(registry)):
        raise ValueError('Review alias registry differs from its staged path')
    capture_voice = bool(voice_config_path) and (
        os.path.abspath(voice_config_path) == os.path.abspath(voice))
    if voice_mapping and not voice_config_path:
        raise ValueError('Voice remap requires this saved book voice config')
    if voice_mapping and voice_config_path:
        if not capture_voice or not callable(remap_voice_config):
            raise ValueError('Saved review voice config must belong to this book')
    with reuse_book_state(data_dir), ensure_saved_book_state(data_dir, scripts_dir), contextlib.ExitStack() as locks:
        locks.enter_context(file_lock(str(root / 'state.json')))
        for path in (script, binding, publication):
            locks.enter_context(file_lock(str(path)))
        if capture_voice:
            locks.enter_context(file_lock(str(voice)))
        if registry is not None:
            locks.enter_context(file_lock(str(registry)))
        elif alias_capture is not None:
            locks.enter_context(file_lock(str(alias_capture)))
        if saved_script_is_active_source(data_dir, str(script)):
            raise ValueError('This saved book is currently loaded; switch books before reviewing it')
        _, current = capture_scope_file(scripts_dir, script.name, limit=512 * 1024**2, include_bytes=True)
        if current != old_script:
            raise ValueError('Saved script changed during review; edits were preserved')
        verify_review_source_identity(data_dir, expected_source_identity)
        _, raw_binding = capture_scope_file(scripts_dir, binding.name, limit=MAX_BINDING_BYTES, include_bytes=True)
        binding_bytes = updated_saved_binding(raw_binding, old_script, new_script)
        publication_bytes = get_saved_publication_source_bytes(str(script), new_script)
        relative = lambda path: os.path.relpath(path, data_dir).replace(os.sep, '/')
        replacements = {relative(script): new_script}
        alias_committed = _add_staged_registry(data_dir, registry_staging, registry, replacements)
        if alias_committed is None and registry is None and alias_capture is not None:
            _, alias_committed = capture_scope_file(
                data_dir, alias_capture.name, limit=MAX_SELECTION_STATE_BYTES, include_bytes=True)
        # A saved review rewrites the script but keeps its .meta.json; rebind
        # the recorded source normalizations to the new bytes through the one
        # producer, or the review silently drops them.
        from readalong_export import get_refreshed_saved_script_metadata
        meta = directory / (script.stem + '.meta.json')
        meta_bytes = get_refreshed_saved_script_metadata(str(script), new_script)
        if meta_bytes is not None:
            replacements[relative(meta)] = meta_bytes
        if binding_bytes is not None:
            replacements[relative(binding)] = binding_bytes
        if publication_bytes is not None:
            replacements[relative(publication)] = publication_bytes
        moved = 0
        voice_committed = None
        if capture_voice:
            voice_committed, moved = _voice_remap_bytes(scripts_dir, voice.name, voice_mapping,
                                                         remap_voice_config, expected_voice_sha256)
            if moved:
                replacements[relative(voice)] = voice_committed
        apply_book_state_locked(data_dir, replacements, [])
        companions = {'aliases': _committed_identity(alias_capture or registry, alias_committed),
                      'voice': _committed_identity(voice if capture_voice else None, voice_committed)}
        raw_sha, entries_sha = _output_proof_values(new_script)
        return PublicationProof(moved, output_raw_sha256=raw_sha,
                                output_entries_sha256=entries_sha, companions=companions)


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
                             complete_manifest=False, before_replace=None):
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
        entries = json.loads(script_bytes, parse_float=_finite_float, parse_constant=reject_constant)
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
        expected_selection = {'book_token': expected_token,
                              'source_path': expected_source_path,
                              'source_sha256': expected_source_sha256}
        if ('script_generation_selection' in state
                and state['script_generation_selection'] != expected_selection):
            raise ValueError('Generation selection identity is inconsistent')
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
        if _source_digest(expected_source_path, owning_root=data_dir) != expected_source_sha256:
            raise ValueError(STALE_SOURCE_MESSAGE)
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
            from three_pass_generate import get_three_pass_publication_blocker
            _, raw_manifest = capture_scope_file(data_dir, manifest_path.name,
                                                   limit=16 * 1024**2, include_bytes=True)
            if raw_manifest is None:
                raise ValueError('Generation publication manifest unavailable')
            try:
                manifest = json.loads(
                    raw_manifest, object_pairs_hook=_unique_pairs,
                    parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid numeric constant')))
                blocker = (get_three_pass_publication_blocker(
                    manifest, expected_run_id=expected_run_id,
                    staged_script_bytes=script_bytes, require_publishing=True)
                    if isinstance(manifest, dict) else 'generation manifest is invalid')
            except (UnicodeDecodeError, ValueError, RecursionError) as error:
                raise ValueError('Generation completion evidence is invalid; edits were preserved') from error
            if blocker:
                raise ValueError(f'Generation completion evidence is invalid: {blocker}; edits were preserved')
            manifest['status'] = 'complete'
            manifest['completion']['status'] = 'complete'
            manifest['publication'] = {
                'run_id': expected_run_id,
                'script_sha256': hashlib.sha256(script_bytes).hexdigest(),
            }
            replacements[manifest_path.name] = json.dumps(
                manifest, ensure_ascii=False, indent=2, allow_nan=False).encode()
        apply_book_state_locked(data_dir, replacements, [chunks.name, journal.name],
                                before_replace=before_replace)
