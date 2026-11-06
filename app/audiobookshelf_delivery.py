"""Explicit, manual delivery of a completed shareable export to Audiobookshelf.

This module is deliberately small and side-effect-explicit (verb-first names):
every network action is initiated by an explicit user request (Load libraries,
Preview, Send, Reconcile). Nothing here runs at startup, polls, or retries on
its own. Credentials live in the OS credential store (``keyring``) with an
explicit environment-variable fallback (``ALEXANDRIA_ABS_API_KEY``) that is
bound to one server via ``ALEXANDRIA_ABS_SERVER_URL``; there is no plaintext
file storage and no reuse of the LLM provider configuration, whose
``resolve_api_key`` has an OpenAI-specific ``OPENAI_API_KEY`` fallback that
must never supply an Audiobookshelf token.

Identity model: a preview binds book token, export file, file version, SHA-256,
input revision, destination library/folder, server, and metadata. Send
revalidates every binding field; a stale, replaced, partial, or incomplete
export is rejected with the local file untouched. Transfer bytes stream from
an owned immutable snapshot copy whose SHA-256 is verified before the network,
so a source replacement before capture refuses without sending, and a
replacement during the transfer can never cause unconfirmed bytes to leave.
Delivery status lives in a small durable ledger (``audiobookshelf_ledger.json``)
persisted *before* the network call, separate from synthesis/export status.
Ambiguous outcomes (timeout, dropped connection, 5xx, cancel) stay
``unknown`` and block re-send until the user reconciles. So does HTTP 200
from ``POST /api/upload``: the pinned server's ``MiscController.handleUpload``
awaits ``file.mv(path)`` but ignores a failed move and still replies 200, so
a 200 only means the upload was *accepted*, never that the file bytes were
stored. An accepted upload is recorded as ``unknown`` with
``upload_accepted_at``/``verification_pending`` and can be promoted to
``sent`` only by an explicit byte-verified reconcile. A pinned
Audiobookshelf server does expose raw file bytes
(``GET /api/items/:id/file/:fileid/download``), so an explicit reconcile
fetches the exact item/file from the ledger-bound server and promotes
``unknown`` to ``sent`` only after the streamed bytes match the ledger's
exact byte length AND SHA-256. Title/folder/size only locates candidates
and never proves delivery or absence; a wrong hash, truncated/oversize
body, 204 X-Accel, 403/404, redirect, ambiguous, malformed, or unavailable
result stays ``unknown`` with an honest note. There is no listing-absence
override or automatic retry.

Book-switch semantics: once the snapshot is bound and the ledger
``in_flight`` entry persisted, the transfer continues and records its outcome
for the previewed book/export/destination even if the active book changes;
the cancel handle stays keyed to the original preview id.
"""
import hashlib
import ipaddress
import json
import os
import threading
import time
import uuid
from urllib.parse import quote, urlparse

CONFIG_NAME = 'audiobookshelf_config.json'
LEDGER_NAME = 'audiobookshelf_ledger.json'
CREDENTIAL_SERVICE = 'alexandria-audiobookshelf'
ENV_KEY_NAME = 'ALEXANDRIA_ABS_API_KEY'
ENV_SERVER_NAME = 'ALEXANDRIA_ABS_SERVER_URL'
EXPORT_FILES = {'mp3': 'cloned_audiobook.mp3', 'm4b': 'audiobook.m4b'}
MIME_TYPES = {'mp3': 'audio/mpeg', 'm4b': 'audio/mp4'}
_UPLOAD_CHUNK = 64 * 1024
_SNAPSHOT_CHUNK = 256 * 1024
_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_TITLE = 500
_MAX_METADATA = 1000
_PREVIEW_TTL = 600
_MAX_PREVIEWS = 50
_MAX_LEDGER_ENTRIES = 1000
_ITEMS_PAGE_SIZE = 100
_MAX_ITEM_PAGES = 10
_CONNECT_TIMEOUT = 10
_READ_TIMEOUT = 30

_ledger_lock = threading.RLock()
_reserve_lock = threading.RLock()
_preview_lock = threading.Lock()
_previews = {}
_cancel_events = {}
_active_deliveries = set()
_upload_progress = {}
_PROCESS_STARTED_AT = time.time()
_recovered = False


class DeliveryError(ValueError):
    """A user-facing delivery refusal with an HTTP status for the caller."""

    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


# --- Credential store -------------------------------------------------------

def _is_secure_backend(backend):
    """True when a keyring backend is an OS-secure store (never plaintext/fail)."""
    if backend is None:
        return False
    name = type(backend).__name__.lower()
    module = type(backend).__module__.lower()
    if 'plaintext' in name or 'plaintext' in module:
        return False
    if name in ('failkeyring', 'nullkeyring'):
        return False
    if 'fail' in module and 'keyring' in module:
        return False
    return True


def _get_keyring():
    try:
        import keyring
    except ImportError:
        return None
    return keyring


def _check_secure_backend(keyring):
    """Refuse plaintext/file/untrusted backends; never leak the key."""
    if keyring is None:
        return
    get_keyring = getattr(keyring, 'get_keyring', None)
    if get_keyring is None:
        # Test fakes expose no backend; structural qualification is exercised
        # separately with a real backend shape.
        return
    try:
        backend = get_keyring()
    except Exception as error:
        raise DeliveryError(
            'The OS credential store is unavailable. The API key was not stored anywhere.', 503) from error
    if not _is_secure_backend(backend):
        raise DeliveryError(
            'The OS credential store backend is not secure, so the API key was refused. '
            'Configure a secure OS keyring backend, then retry.', 503)


def _env_token_for(server_url):
    """Environment fallback bound to one server; never a global token."""
    token = os.environ.get(ENV_KEY_NAME, '').strip()
    if not token:
        return ''
    allowed = os.environ.get(ENV_SERVER_NAME, '').strip().rstrip('/')
    if not allowed:
        return ''
    if allowed.rstrip('/') == str(server_url).rstrip('/'):
        return token
    return ''


def save_api_key(server_url, token):
    """Persist an API key in the OS credential store (or the explicit env var).

    Returns the storage label so the UI can say where it went. Never writes
    plaintext. Raises DeliveryError when no secure store is available.
    """
    server_url = _validate_base_url(server_url)
    token = str(token or '').strip()
    if not token:
        raise DeliveryError('Enter the Audiobookshelf API key first.', 400)
    keyring = _get_keyring()
    if keyring is not None:
        _check_secure_backend(keyring)
        try:
            keyring.set_password(CREDENTIAL_SERVICE, server_url, token)
        except Exception as error:
            raise DeliveryError(
                'Could not store the API key in the OS credential store. Is it locked or unavailable? '
                'The key was not stored anywhere else.', 503) from error
        return 'OS credential store'
    # No silent plaintext fallback: only the explicit environment variable,
    # which the operator sets knowingly, counts as the fallback.
    raise DeliveryError(
        'No OS credential store is available (keyring is not installed) and '
        f'{ENV_KEY_NAME} is not set. Refusing to store the key anywhere else. '
        'Install keyring or set the environment variable, then retry.', 503)


def get_api_key(server_url):
    """Read the API key for one server; empty string when absent."""
    server_url = _validate_base_url(server_url)
    keyring = _get_keyring()
    if keyring is not None:
        _check_secure_backend(keyring)
        try:
            value = keyring.get_password(CREDENTIAL_SERVICE, server_url)
        except Exception as error:
            raise DeliveryError(
                'The OS credential store is unavailable, so the API key could not be read. '
                'No token was sent anywhere.', 503) from error
        if value:
            return value
    return _env_token_for(server_url)


def clear_api_key(server_url):
    """Remove the stored key for one server (no-op if absent)."""
    server_url = _validate_base_url(server_url)
    keyring = _get_keyring()
    if keyring is not None:
        _check_secure_backend(keyring)
        try:
            keyring.delete_password(CREDENTIAL_SERVICE, server_url)
        except Exception as error:
            raise DeliveryError(
                'The OS credential store is unavailable, so the API key could not be removed.', 503) from error


def is_secret_text(text, token):
    """True when a string contains the credential (for redaction checks)."""
    if not text or not token:
        return False
    return token in str(text)


def redact_text(text, token):
    """Remove the credential from any string that might surface it."""
    if not text or not token:
        return text
    return str(text).replace(token, '[redacted]')


# --- Server config (non-secret) ---------------------------------------------

def _is_loopback_host(hostname):
    host = (hostname or '').strip().lower()
    if host in ('localhost', '127.0.0.1', '::1'):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _validate_base_url(value):
    """Validate the server base URL; credentials in URLs are refused."""
    raw = str(value or '').strip().rstrip('/')
    if not raw or '\0' in raw or ' ' in raw:
        raise DeliveryError('Enter the Audiobookshelf server URL.', 400)
    parsed = urlparse(raw)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname:
        raise DeliveryError('The server URL must start with http:// or https://.', 400)
    if parsed.username or parsed.password:
        raise DeliveryError('Do not embed credentials in the server URL; use the API key field.', 400)
    if parsed.query or parsed.fragment:
        raise DeliveryError('The server URL must not contain a query string or fragment.', 400)
    if parsed.scheme == 'http' and not _is_loopback_host(parsed.hostname):
        raise DeliveryError(
            'Plain HTTP is only allowed for loopback servers (localhost/127.0.0.1). '
            'Use https:// for any other host.', 400)
    return raw


def _config_path(data_dir):
    return os.path.join(data_dir, CONFIG_NAME)


def _ledger_path(data_dir):
    return os.path.join(data_dir, LEDGER_NAME)


def save_server_config(data_dir, base_url, library_id=None, folder_id=None):
    """Persist the non-secret server selection; never the API key.

    Any config change invalidates every live preview, so a stale
    server-bound preview can never be confirmed against a new server.
    """
    base_url = _validate_base_url(base_url)
    from utils import atomic_json_write
    config = {'base_url': base_url}
    if library_id:
        config['library_id'] = str(library_id)
    if folder_id:
        config['folder_id'] = str(folder_id)
    atomic_json_write(config, _config_path(data_dir))
    with _preview_lock:
        _previews.clear()
        _cancel_events.clear()
    return config


def get_server_config(data_dir):
    """Read the non-secret server selection (``{}`` when unconfigured)."""
    from utils import safe_load_json
    config = safe_load_json(_config_path(data_dir), {})
    return config if isinstance(config, dict) else {}


# --- Ledger ------------------------------------------------------------------

def _validate_ledger_entry(entry):
    """Shape-check one entry; corrupted entries are dropped without losing
    safety-critical unknown/sent evidence that is well-formed."""
    if not isinstance(entry, dict):
        return None
    if entry.get('status') not in ('sent', 'failed', 'unknown', 'in_flight'):
        return None
    for key in ('id', 'book_token', 'sha256', 'library_id', 'folder_id'):
        value = entry.get(key)
        if not isinstance(value, str) or not value:
            return None
    if len(entry['id']) > 64 or len(entry['book_token']) > 128 or len(entry['sha256']) > 128:
        return None
    if len(entry['library_id']) > 200 or len(entry['folder_id']) > 200:
        return None
    output_bytes = entry.get('output_bytes')
    if not isinstance(output_bytes, int) or output_bytes < 0 or output_bytes > 2 ** 31:
        return None
    return entry


def _read_ledger_raw(data_dir):
    """Read and shape-validate the ledger without any side effect."""
    from utils import safe_load_json
    ledger = safe_load_json(_ledger_path(data_dir), [])
    if not isinstance(ledger, list):
        return []
    valid = []
    for entry in ledger:
        checked = _validate_ledger_entry(entry)
        if checked is not None:
            valid.append(checked)
    return valid


def _trim_ledger(ledger):
    """Bound the ledger by dropping oldest failed entries first; never drop
    unknown/sent/in_flight dedup evidence."""
    if len(ledger) <= _MAX_LEDGER_ENTRIES:
        return ledger
    excess = len(ledger) - _MAX_LEDGER_ENTRIES
    drop = set()
    for index, entry in enumerate(ledger):
        if excess <= 0:
            break
        if entry.get('status') == 'failed':
            drop.add(index)
            excess -= 1
    if not drop:
        return ledger
    return [entry for index, entry in enumerate(ledger) if index not in drop]


def _write_ledger(data_dir, ledger):
    from utils import atomic_json_write
    atomic_json_write(list(ledger), _ledger_path(data_dir))


def save_ledger(data_dir, ledger):
    """Persist the ledger atomically (called before any network send)."""
    with _ledger_lock:
        _write_ledger(data_dir, _trim_ledger([entry for entry in ledger if _validate_ledger_entry(entry) is not None] if isinstance(ledger, list) else []))


def demote_interrupted_deliveries(data_dir):
    """Recover interrupted sends and legacy success records without byte proof.

    Runs under lock+file lock once per process (first ledger access).
    Never demotes a live owned delivery or a byte-verified success.
    """
    global _recovered
    from utils import file_lock
    with _ledger_lock:
        if _recovered:
            return []
    with _ledger_lock, file_lock(_ledger_path(data_dir), timeout=30):
        ledger = _read_ledger_raw(data_dir)
        changed = False
        for entry in ledger:
            if entry.get('status') == 'sent' and not (
                    entry.get('reconciled_sha256') == entry.get('sha256')
                    and entry.get('reconciled_bytes') == entry.get('output_bytes')
                    and entry.get('reconciled_item_id')
                    and entry.get('reconciled_file_id')
                    and entry.get('reconciled_verified_at')):
                entry['status'] = 'unknown'
                entry['verification_pending'] = True
                entry['verification_note'] = (
                    'An earlier upload was marked sent without file-byte proof. '
                    'Verify delivery before treating it as delivered. Resend stays blocked.')
                changed = True
            if (entry.get('status') == 'in_flight'
                    and entry.get('id') not in _active_deliveries
                    and entry.get('created_at', 0) < _PROCESS_STARTED_AT):
                entry['status'] = 'unknown'
                entry['error'] = 'Interrupted by restart; outcome unknown. Reconcile before retrying.'
                changed = True
        if changed:
            _write_ledger(data_dir, _trim_ledger(ledger))
        _recovered = True
        return ledger


def get_ledger(data_dir):
    """Read the delivery ledger (pure read after one-time recovery).

    Recovery demotes interrupted pre-process uploads and legacy success
    records without file-byte proof. Concurrent polling preserves live sends
    and verified deliveries.
    """
    if not _recovered:
        demote_interrupted_deliveries(data_dir)
    return _read_ledger_raw(data_dir)


def get_latest_delivery(data_dir, book_token, sha256, library_id, folder_id):
    """Newest ledger entry for one exact book/export/destination identity."""
    matches = [entry for entry in get_ledger(data_dir)
               if entry.get('book_token') == book_token
               and entry.get('sha256') == sha256
               and entry.get('library_id') == library_id
               and entry.get('folder_id') == folder_id]
    return matches[-1] if matches else None


def save_ledger_entry(data_dir, entry):
    """Append one entry and persist before the caller touches the network."""
    from utils import file_lock
    with _ledger_lock, file_lock(_ledger_path(data_dir), timeout=30):
        ledger = _read_ledger_raw(data_dir)
        ledger.append(entry)
        _write_ledger(data_dir, _trim_ledger(ledger))
        return entry


def update_ledger_entry(data_dir, entry):
    """Persist an existing entry's new status/facts."""
    from utils import file_lock
    with _ledger_lock, file_lock(_ledger_path(data_dir), timeout=30):
        ledger = _read_ledger_raw(data_dir)
        for index, existing in enumerate(ledger):
            if existing.get('id') == entry.get('id'):
                ledger[index] = entry
                break
        _write_ledger(data_dir, _trim_ledger(ledger))


def get_upload_progress(preview_id):
    """Live upload progress for one preview (``None`` when idle/unknown)."""
    with _preview_lock:
        state = _upload_progress.get(preview_id)
        return dict(state) if state is not None else None


def _set_upload_progress(preview_id, sent, total):
    with _preview_lock:
        _upload_progress[preview_id] = {'sent': int(sent), 'total': int(total)}


def _clear_upload_progress(preview_id):
    with _preview_lock:
        _upload_progress.pop(preview_id, None)


# --- Export identity ----------------------------------------------------------

def get_report_for_export(data_dir, states, book_token, export_file):
    """Find the session's completed-current report for one export file.

    ``states`` is the ``process_state`` mapping the caller passes in (this
    module never reaches into global task state itself). Reuses the existing
    ``get_session_evidence`` fence: the report must still be present, the file
    version unchanged, and the receipt unreplaced.
    """
    from export_quality_session import get_session_evidence
    evidence = get_session_evidence(data_dir, states, book_token)
    for report in evidence.get('reports', []):
        if report.get('file') == export_file:
            return report
    return None


def get_current_export_identity(data_dir, states, book_token, export_file):
    """Revalidate a completed-current export and return its exact identity.

    Raises DeliveryError (409) for a missing, unbound, stale, replaced,
    partial, out-of-scope, or incomplete export. Never raises for a file that
    merely needs re-reading: the SHA-256 is recomputed from the live bytes so
    a replaced file cannot reuse a previewed digest.
    """
    if export_file not in ('cloned_audiobook.mp3', 'audiobook.m4b'):
        raise DeliveryError('Only completed MP3 and M4B exports can be sent.', 400)
    report = get_report_for_export(data_dir, states, book_token, export_file)
    if report is None:
        raise DeliveryError('No completed export evidence for this book. Export first.', 409)
    if report.get('status') not in ('verified', 'review'):
        raise DeliveryError('The export evidence is not a completed check.', 409)
    if report.get('input_binding') != 'current' or not report.get('input_revision'):
        raise DeliveryError('The export is not bound to the current input revision.', 409)
    from export_quality_session import get_export_input_revision
    current = get_export_input_revision(data_dir, book_token)
    if current.get('revision') != report.get('input_revision'):
        raise DeliveryError('The book changed since the export finished. Re-export before sending.', 409)
    path = os.path.join(data_dir, export_file)
    from export_quality import get_export_file_identity, get_export_file_version
    try:
        if get_export_file_version(path) != report.get('file_version'):
            raise DeliveryError('The export file was replaced after its check. Re-export before sending.', 409)
        identity = get_export_file_identity(path)
    except DeliveryError:
        raise
    except (OSError, ValueError) as error:
        raise DeliveryError(f'Cannot read the export file: {error}', 409) from error
    if identity['sha256'] != report.get('sha256') or identity['output_bytes'] != report.get('output_bytes'):
        raise DeliveryError('The export bytes changed after the check. Re-export before sending.', 409)
    from project_scope_exports import require_scope_export_current
    try:
        require_scope_export_current(data_dir, path)
    except DeliveryError:
        raise
    except Exception as error:  # HTTPException from the existing fence
        detail = getattr(error, 'detail', str(error))
        raise DeliveryError(str(detail), 409) from error
    return {'path': path, 'export_file': export_file,
            'sha256': identity['sha256'], 'output_bytes': identity['output_bytes'],
            'file_version': identity['file_version'],
            'input_revision': report['input_revision'],
            'book_token': book_token}


def _clean_metadata(value, label, required, limit):
    text = str(value or '').strip()
    if required and not text:
        raise DeliveryError(f'Enter the {label} first.', 400)
    if len(text) > limit:
        raise DeliveryError(f'The {label} is too long.', 400)
    return text


def save_preview(data_dir, states, book_token, export_file, library_id, folder_id,
                 title, author='', series='', *, base_url=None, token=None):
    """Validate and bind one preview; returns the preview the UI must confirm.

    When ``base_url``/``token`` are supplied (the route always supplies
    them), the library/folder are validated against the server's explicit
    listing before the preview is bound. The server is stored in the preview
    and re-checked at send time.
    """
    if not library_id or not folder_id:
        raise DeliveryError('Choose a library and folder first (Load libraries).', 400)
    identity = get_current_export_identity(data_dir, states, book_token, export_file)
    if base_url is not None:
        validated_base = _validate_base_url(base_url)
    else:
        validated_base = get_server_config(data_dir).get('base_url')
        if not validated_base:
            raise DeliveryError('Configure the server first (Save server).', 400)
        validated_base = _validate_base_url(validated_base)
    if token:
        _validate_destination(validated_base, token, str(library_id), str(folder_id))
    preview = {
        'preview_id': uuid.uuid4().hex,
        'book_token': book_token,
        'export_file': export_file,
        'filename': export_file,
        'format': 'mp3' if export_file.endswith('.mp3') else 'm4b',
        'sha256': identity['sha256'],
        'output_bytes': identity['output_bytes'],
        'file_version': identity['file_version'],
        'input_revision': identity['input_revision'],
        'library_id': str(library_id),
        'folder_id': str(folder_id),
        'title': _clean_metadata(title, 'title', True, _MAX_TITLE),
        'author': _clean_metadata(author, 'author', False, _MAX_METADATA),
        'series': _clean_metadata(series, 'series', False, _MAX_METADATA),
        'base_url': validated_base,
        'server': validated_base,
        'created_at': time.time(),
    }
    with _preview_lock:
        now = time.time()
        expired = [key for key, item in _previews.items() if now - item.get('created_at', 0) > _PREVIEW_TTL]
        for key in expired:
            _previews.pop(key, None)
        while len(_previews) >= _MAX_PREVIEWS:
            oldest = min(_previews, key=lambda key: _previews[key].get('created_at', 0))
            _previews.pop(oldest, None)
        _previews[preview['preview_id']] = preview
    return preview


def get_preview(preview_id):
    """Fetch one live preview by id (``None`` once it was consumed/expired)."""
    with _preview_lock:
        preview = _previews.get(preview_id)
        if preview is None:
            return None
        if time.time() - preview.get('created_at', 0) > _PREVIEW_TTL:
            _previews.pop(preview_id, None)
            return None
        return preview


def _consume_preview(preview_id):
    with _preview_lock:
        _previews.pop(preview_id, None)


def _preview_matches(stored, confirm):
    # Only the client-supplied binding fields are compared here; the SHA-256
    # and input revision are re-read from the live export by the caller.
    for key in ('book_token', 'export_file', 'library_id', 'folder_id',
                'title', 'author', 'series', 'base_url'):
        if str(stored.get(key, '')) != str((confirm or {}).get(key, '')):
            return False
    return True


# --- Audiobookshelf HTTP client ------------------------------------------------

def _session(token):
    import requests
    session = requests.Session()
    session.headers.update({'Authorization': f'Bearer {token}',
                              'Accept': 'application/json'})
    session.trust_env = False
    return session


def _read_json_limited(response):
    """Stream the response body bounded at _MAX_RESPONSE_BYTES (never eagerly
    buffers the whole body before the cap)."""
    chunks = []
    total = 0
    for chunk in response.iter_content(chunk_size=_UPLOAD_CHUNK):
        if not chunk:
            continue
        total += len(chunk)
        if total > _MAX_RESPONSE_BYTES:
            try:
                response.close()
            except Exception:
                pass
            raise DeliveryError('The server response is too large.', 502)
        chunks.append(chunk)
    raw = b''.join(chunks)
    if not raw:
        return {}
    try:
        return json.loads(raw.decode('utf-8', 'replace'))
    except ValueError:
        return {}


def get_libraries(base_url, token, *, timeout=30):
    """Explicitly initiated: list libraries accessible to the stored token."""
    base_url = _validate_base_url(base_url)
    if not token:
        raise DeliveryError('No API key stored for this server.', 401)
    import requests
    session = _session(token)
    try:
        try:
            response = session.get(f'{base_url}/api/libraries', timeout=timeout,
                                   allow_redirects=False, stream=True)
        except requests.Timeout as error:
            raise DeliveryError('The library request timed out.', 504) from error
        except requests.ConnectionError as error:
            raise DeliveryError('Could not reach the server for the library list.', 502) from error
        try:
            if 300 <= response.status_code < 400:
                raise DeliveryError('The server redirected the library request; refusing to follow it with the API key.', 502)
            if response.status_code == 401:
                raise DeliveryError('The server rejected the API key (401).', 401)
            if response.status_code == 403:
                raise DeliveryError('The API key has no permission to list libraries (403).', 403)
            if not response.ok:
                raise DeliveryError(f'Listing libraries failed (HTTP {response.status_code}).', 502)
            payload = _read_json_limited(response)
        finally:
            try:
                response.close()
            except Exception:
                pass
    finally:
        session.close()
    libraries = payload.get('libraries') if isinstance(payload, dict) else None
    if not isinstance(libraries, list):
        raise DeliveryError('The server returned an unexpected library list.', 502)
    return [{'id': str(item.get('id', '')), 'name': str(item.get('name', '')),
             'folders': [{'id': str(folder.get('id', '')),
                          'fullPath': str(folder.get('fullPath', ''))}
                         for folder in item.get('folders') or []
                         if isinstance(folder, dict) and folder.get('id')]}
            for item in libraries if isinstance(item, dict) and item.get('id')]


def _validate_destination(base_url, token, library_id, folder_id):
    """Reject unknown libraries/folders against the server's live listing."""
    libraries = get_libraries(base_url, token)
    for library in libraries:
        if library.get('id') == str(library_id):
            for folder in library.get('folders') or []:
                if folder.get('id') == str(folder_id):
                    return
            raise DeliveryError(
                f'Folder {folder_id} does not belong to library {library_id} on this server. '
                'Reload libraries, then preview again.', 400)
    raise DeliveryError(
        f'Library {library_id} was not found on this server. '
        'Reload libraries, then preview again.', 400)


def get_library_items(base_url, token, library_id, *, timeout=30):
    """Authoritative paginated listing used only to reconcile an ambiguous
    outcome (bounded to _MAX_ITEM_PAGES pages of _ITEMS_PAGE_SIZE)."""
    base_url = _validate_base_url(base_url)
    import requests
    session = _session(token)
    collected = []
    pages_checked = 0
    try:
        for page in range(_MAX_ITEM_PAGES):
            try:
                response = session.get(f'{base_url}/api/libraries/{library_id}/items',
                                       params={'limit': _ITEMS_PAGE_SIZE, 'page': page},
                                       timeout=timeout, allow_redirects=False, stream=True)
            except requests.Timeout as error:
                raise DeliveryError('The reconciliation request timed out; the outcome stays unknown.', 504) from error
            except requests.ConnectionError as error:
                raise DeliveryError('Could not reach the server to reconcile; the outcome stays unknown.', 502) from error
            try:
                if not response.ok:
                    raise DeliveryError(
                        f'Reconciliation listing failed (HTTP {response.status_code}); the outcome stays unknown.', 502)
                payload = _read_json_limited(response)
            finally:
                try:
                    response.close()
                except Exception:
                    pass
            pages_checked += 1
            results = payload.get('results') if isinstance(payload, dict) else None
            if not isinstance(results, list):
                break
            collected.extend(results)
            if len(results) < _ITEMS_PAGE_SIZE:
                break
    finally:
        session.close()
    return collected, pages_checked


def _unlink_quietly(path):
    try:
        if path and os.path.exists(path):
            os.unlink(path)
    except OSError:
        pass


def _make_snapshot(data_dir, export_path, expected_sha256, expected_file_version):
    """Copy the export to an owned immutable snapshot file; verify its SHA
    before any network. A source change before/during capture refuses."""
    import tempfile
    from export_quality import ensure_export_file
    fd, snapshot_path = tempfile.mkstemp(prefix='abs-export-', suffix='.bin')
    os.close(fd)
    try:
        digest = hashlib.sha256()
        with ensure_export_file(export_path) as (source, version):
            if list(version) != list(expected_file_version):
                raise DeliveryError(
                    'The export file was replaced before upload. Nothing was sent.', 409)
            with open(snapshot_path, 'wb') as out:
                while True:
                    block = source.read(_SNAPSHOT_CHUNK)
                    if not block:
                        break
                    out.write(block)
                    digest.update(block)
        if digest.hexdigest() != expected_sha256:
            raise DeliveryError(
                'The export bytes changed after the preview. Nothing was sent. Preview again.', 409)
    except DeliveryError:
        _unlink_quietly(snapshot_path)
        raise
    except (OSError, ValueError) as error:
        _unlink_quietly(snapshot_path)
        raise DeliveryError(f'Cannot read the export file: {error}', 409) from error
    return snapshot_path


class _StreamingMultipart:
    """Bounded streaming multipart body with exact Content-Length and live
    progress/cancel between writes. Files stream from the snapshot handle in
    _UPLOAD_CHUNK blocks; the whole audiobook is never held in RAM."""

    def __init__(self, preview, snapshot_path, cancel_event=None, progress=None):
        self.preview = preview
        self.snapshot_path = snapshot_path
        self.cancel_event = cancel_event
        self.progress = progress
        self.boundary = '----aa2-' + uuid.uuid4().hex
        self._file_size = os.path.getsize(snapshot_path)
        self._prefix = self._build_prefix()
        self._suffix = f'\r\n--{self.boundary}--\r\n'.encode('utf-8')
        self._total = len(self._prefix) + self._file_size + len(self._suffix)

    def _build_prefix(self):
        boundary = self.boundary
        parts = []

        def text_field(name, value):
            return (f'--{boundary}\r\n'
                    f'Content-Disposition: form-data; name="{name}"\r\n'
                    f'\r\n{value}\r\n').encode('utf-8')

        parts.append(text_field('title', self.preview['title']))
        parts.append(text_field('library', self.preview['library_id']))
        parts.append(text_field('folder', self.preview['folder_id']))
        if self.preview.get('author'):
            parts.append(text_field('author', self.preview['author']))
        if self.preview.get('series'):
            parts.append(text_field('series', self.preview['series']))
        parts.append((f'--{boundary}\r\n'
                      f'Content-Disposition: form-data; name="file"; filename="{self.preview["filename"]}"\r\n'
                      f'Content-Type: {MIME_TYPES[self.preview["format"]]}\r\n'
                      f'\r\n').encode('utf-8'))
        return b''.join(parts)

    def __len__(self):
        return self._total

    def __iter__(self):
        yield self._prefix
        sent = 0
        with open(self.snapshot_path, 'rb') as handle:
            while True:
                if self.cancel_event is not None and self.cancel_event.is_set():
                    raise DeliveryError('Upload cancelled.', 499)
                block = handle.read(_UPLOAD_CHUNK)
                if not block:
                    break
                sent += len(block)
                if self.progress is not None:
                    self.progress(sent)
                yield block
        yield self._suffix


def _post_snapshot(base_url, token, preview, snapshot_path, cancel_event=None,
                   progress=None, *, timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT)):
    """POST one snapshot file to ``POST /api/upload``; returns outcome class.

    ``'accepted'`` only on HTTP 200: the server accepted the upload, but its
    handler ignores a failed ``file.mv()`` and still replies 200, so this is
    NEVER ``'sent'`` -- ``send_preview`` records it as ``unknown`` pending
    byte verification. ``'failed'`` on a definitive pre-delivery rejection
    (400/401/403/404). ``'unknown'`` on anything that might have delivered
    after bytes started (5xx, timeout, dropped connection, cancel, redirect,
    and any other status such as 204). Refusing to follow a redirect does
    not prove the initial server stored nothing. Never
    follows redirects, so the credential cannot leak to another origin.
    """
    import requests
    base_url = _validate_base_url(base_url)
    session = _session(token)
    body = _StreamingMultipart(preview, snapshot_path, cancel_event, progress)
    headers = {'Content-Type': f'multipart/form-data; boundary={body.boundary}',
               'Content-Length': str(len(body))}
    response = None
    try:
        try:
            response = session.post(f'{base_url}/api/upload', data=body, headers=headers,
                                    timeout=timeout, allow_redirects=False, stream=True)
        except (requests.Timeout, requests.ConnectionError) as error:
            raise DeliveryError(f'Upload outcome unknown: {type(error).__name__}.', 504) from error
        try:
            if 300 <= response.status_code < 400:
                raise DeliveryError('The server redirected the upload; refusing to follow it with the API key.', 502)
            if response.status_code == 200:
                return 'accepted'
            if response.status_code in (400, 401, 403, 404):
                detail = redact_text(str(response.status_code), token)
                raise DeliveryError(
                    f'The server rejected the upload (HTTP {detail}). Nothing was delivered; you may retry.',
                    response.status_code)
            raise DeliveryError(f'Upload outcome unknown (HTTP {response.status_code}).', 502)
        finally:
            try:
                response.close()
            except Exception:
                pass
    finally:
        session.close()


def upload_export(base_url, token, preview, path, file_version, cancel_event=None,
                  progress=None, *, timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT)):
    """Snapshot ``path`` (verifying its SHA) and stream it to the server."""
    expected_sha = preview.get('sha256')
    if not expected_sha:
        from export_quality import get_export_file_identity
        try:
            expected_sha = get_export_file_identity(path)['sha256']
        except (OSError, ValueError) as error:
            raise DeliveryError(f'Cannot read the export file: {error}', 409) from error
    snapshot_path = _make_snapshot('', path, expected_sha, file_version)
    try:
        return _post_snapshot(base_url, token, preview, snapshot_path,
                              cancel_event=cancel_event, progress=progress, timeout=timeout)
    finally:
        _unlink_quietly(snapshot_path)


# --- Send / reconcile orchestration --------------------------------------------

def _is_safe_id_segment(value):
    """True when a library/item/file id is safe to embed (URL-encoded) later."""
    if not isinstance(value, str) or not value:
        return False
    if len(value) > 200:
        return False
    if '/' in value or '\\' in value:
        return False
    if value in ('.', '..'):
        return False
    return all(ord(ch) >= 32 and ord(ch) != 127 for ch in value)


def _update_reconciled_entry(data_dir, updated_entry):
    """Compare-and-swap a reconcile write; never demote a live or sent entry.

    Runs the read-modify-write under one lock+file lock with no network held,
    so a concurrent send/reconcile cannot lose ledger state.
    """
    from utils import file_lock
    with _ledger_lock, file_lock(_ledger_path(data_dir), timeout=30):
        ledger = _read_ledger_raw(data_dir)
        for index, existing in enumerate(ledger):
            if existing.get('id') == updated_entry.get('id'):
                if existing.get('status') != 'unknown':
                    return existing
                ledger[index] = updated_entry
                break
        else:
            return updated_entry
        _write_ledger(data_dir, _trim_ledger(ledger))
        return updated_entry


def _fetch_item_detail(base_url, token, item_id, *, timeout=30):
    """Fetch one exact library item from the ledger-bound server."""
    import requests
    base_url = _validate_base_url(base_url)
    session = _session(token)
    try:
        try:
            response = session.get(f"{base_url}/api/items/{quote(item_id, safe='')}",
                                   timeout=timeout, allow_redirects=False, stream=True)
        except requests.Timeout as error:
            raise DeliveryError('The item lookup timed out; the outcome stays unknown.', 504) from error
        except requests.ConnectionError as error:
            raise DeliveryError('Could not reach the server for the item lookup; the outcome stays unknown.', 502) from error
        try:
            if 300 <= response.status_code < 400:
                raise DeliveryError('The item lookup was redirected; refusing to follow it. The outcome stays unknown.', 502)
            if response.status_code == 401:
                raise DeliveryError('The server rejected the API key for the item lookup (401); the outcome stays unknown.', 401)
            if response.status_code == 403:
                raise DeliveryError('The API key cannot access this item (403); the outcome stays unknown.', 403)
            if response.status_code == 404:
                raise DeliveryError('The candidate item was not found on this server (404); the outcome stays unknown.', 404)
            if not response.ok:
                raise DeliveryError(f'The item lookup failed (HTTP {response.status_code}); the outcome stays unknown.', 502)
            payload = _read_json_limited(response)
        finally:
            try:
                response.close()
            except Exception:
                pass
    finally:
        session.close()
    if not isinstance(payload, dict):
        raise DeliveryError('The item lookup returned malformed data; the outcome stays unknown.', 502)
    return payload


def _download_candidate_digest(base_url, token, item_id, file_id, expected_bytes,
                                *, timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT)):
    """Stream one candidate file and return (sha256, total_bytes).

    The stream aborts past ``expected_bytes``, so an oversize body fails
    without buffering it whole. Raises DeliveryError for every non-verifiable
    outcome (204 X-Accel, 403/404/401, redirect, oversize, timeout).
    """
    import requests
    base_url = _validate_base_url(base_url)
    session = _session(token)
    try:
        try:
            response = session.get(
                f"{base_url}/api/items/{quote(item_id, safe='')}/file/{quote(file_id, safe='')}/download",
                timeout=timeout, allow_redirects=False, stream=True)
        except requests.Timeout as error:
            raise DeliveryError('The file download timed out; the outcome stays unknown.', 504) from error
        except requests.ConnectionError as error:
            raise DeliveryError('Could not reach the server for the file download; the outcome stays unknown.', 502) from error
        try:
            if 300 <= response.status_code < 400:
                raise DeliveryError('The file download was redirected; refusing to follow it. The outcome stays unknown.', 502)
            if response.status_code == 204:
                raise DeliveryError('The server uses an X-Accel proxy for downloads, so the bytes cannot be verified here; the outcome stays unknown.', 502)
            if response.status_code == 401:
                raise DeliveryError('The server rejected the API key for the file download (401); the outcome stays unknown.', 401)
            if response.status_code == 403:
                raise DeliveryError('The API key has no download permission (403); the outcome stays unknown.', 403)
            if response.status_code == 404:
                raise DeliveryError('The candidate file was not found (404); the outcome stays unknown.', 404)
            if not response.ok:
                raise DeliveryError(f'The file download failed (HTTP {response.status_code}); the outcome stays unknown.', 502)
            digest = hashlib.sha256()
            total = 0
            for chunk in response.iter_content(chunk_size=_UPLOAD_CHUNK):
                if not chunk:
                    continue
                total += len(chunk)
                if total > expected_bytes:
                    raise DeliveryError('The downloaded file is larger than expected (oversize); the outcome stays unknown.', 502)
                digest.update(chunk)
            return digest.hexdigest(), total
        finally:
            try:
                response.close()
            except Exception:
                pass
    finally:
        session.close()


def reconcile_delivery(data_dir, delivery_id, base_url, token, *, timeout=30):
    """Byte-verify one unknown delivery against the ledger-bound server.

    This is the ONLY path from ``unknown`` to ``sent``, and it applies equally
    to an upload-accepted record (HTTP 200, ``verification_pending``) and to a
    generically ambiguous one. No send ever records ``sent`` on its own.
    Title/folder/size only locates candidates in the bounded paginated listing
    of the ledger's own library. A single exact item/file candidate is then
    fetched and its raw bytes streamed; promotion to ``sent`` requires the
    full file's exact byte length AND SHA-256 to match the ledger. Every other
    outcome -- no candidate, ambiguous matches, malformed metadata, wrong or
    truncated/oversize bytes, 204/403/404, redirect, or an unavailable endpoint
    -- is recorded and stays ``unknown`` with resend still blocked. No listing
    absence ever proves non-delivery and no retry is started here. Only the
    entry-not-found and wrong-server paths raise (no network, no write); every
    verification failure is persisted as a still-unknown record.
    """
    ledger = get_ledger(data_dir)
    entry = next((item for item in ledger if item.get('id') == delivery_id), None)
    if entry is None:
        raise DeliveryError('No such delivery record.', 404)
    if entry.get('status') != 'unknown':
        return entry
    entry_server = entry.get('server') or entry.get('base_url')
    if not entry_server:
        raise DeliveryError('The delivery record names no server; re-export and send again.', 409)
    bound_base = _validate_base_url(entry_server)
    if _validate_base_url(base_url) != bound_base:
        raise DeliveryError('The delivery record names a different server; refusing to contact another server. The outcome stays unknown.', 409)

    def _record_unknown(note, candidates, pages):
        entry['reconciled_at'] = time.time()
        entry['reconciliation'] = note
        entry['reconciliation_candidates'] = candidates
        entry['reconciliation_pages'] = pages
        return _update_reconciled_entry(data_dir, entry)

    expected_sha = entry.get('sha256')
    expected_bytes = entry.get('output_bytes')
    if (not isinstance(expected_sha, str) or len(expected_sha) != 64
            or any(ch not in '0123456789abcdefABCDEF' for ch in expected_sha)
            or not isinstance(expected_bytes, int) or expected_bytes <= 0
            or not entry.get('title') or not entry.get('library_id') or not entry.get('folder_id')):
        return _record_unknown(
            'Reconcile refused: the ledger record is malformed (digest, byte '
            'length, title, or destination are missing), so the outcome REMAINS '
            'unknown and resend stays blocked.', [], 0)

    try:
        items, pages_checked = get_library_items(bound_base, token, entry['library_id'], timeout=timeout)
    except DeliveryError as error:
        return _record_unknown(f'{error} The status REMAINS unknown and resend stays blocked.', [], 0)
    candidates = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        item_library = item.get('libraryId')
        if item_library is not None and str(item_library) != str(entry['library_id']):
            continue
        media = item.get('media') if isinstance(item.get('media'), dict) else {}
        metadata = media.get('metadata') if isinstance(media.get('metadata'), dict) else {}
        if (metadata.get('title') == entry.get('title')
                and item.get('folderId') == entry.get('folder_id')
                and media.get('size') == entry.get('output_bytes')):
            candidate_id = item.get('id')
            if _is_safe_id_segment(candidate_id) and candidate_id not in seen:
                seen.add(candidate_id)
                candidates.append(candidate_id)
    prefix = f'Reconcile checked {pages_checked} bounded page(s) of {_ITEMS_PAGE_SIZE} items.'
    if not candidates:
        return _record_unknown(
            f'{prefix} No title+folder+size candidate was found. An empty or '
            'limited listing alone cannot prove a late upload will not finish, '
            'so the status REMAINS unknown and resend stays blocked.', candidates, pages_checked)
    if len(candidates) > 1:
        return _record_unknown(
            f'{prefix} Found {len(candidates)} ambiguous title+folder+size '
            f'candidate(s) ({", ".join(candidates[:3])}{", ..." if len(candidates) > 3 else ""}), '
            'so no single file can be verified. The status REMAINS unknown and '
            'resend stays blocked.', candidates, pages_checked)
    item_id = candidates[0]
    try:
        detail = _fetch_item_detail(bound_base, token, item_id, timeout=timeout)
    except DeliveryError as error:
        return _record_unknown(
            f'{prefix} Candidate {item_id} could not be confirmed: {error} '
            'The status REMAINS unknown and resend stays blocked.', candidates, pages_checked)
    if (detail.get('id') != item_id
            or str(detail.get('libraryId', '')) != str(entry['library_id'])
            or detail.get('folderId') != entry.get('folder_id')
            or detail.get('mediaType') != 'book'):
        return _record_unknown(
            f'{prefix} Candidate {item_id} failed full item identity '
            '(id, library, folder, or book type mismatch). The status REMAINS '
            'unknown and resend stays blocked.', candidates, pages_checked)
    media = detail.get('media') if isinstance(detail.get('media'), dict) else {}
    metadata = media.get('metadata') if isinstance(media.get('metadata'), dict) else {}
    if metadata.get('title') != entry.get('title'):
        return _record_unknown(
            f'{prefix} Candidate {item_id} failed title identity. The status '
            'REMAINS unknown and resend stays blocked.', candidates, pages_checked)
    library_files = detail.get('libraryFiles')
    audio_files = media.get('audioFiles')
    if not isinstance(library_files, list) or not isinstance(audio_files, list):
        return _record_unknown(
            f'{prefix} Candidate {item_id} has malformed file metadata '
            '(libraryFiles/audioFiles missing). The status REMAINS unknown and '
            'resend stays blocked.', candidates, pages_checked)
    file_candidates = []
    for library_file in library_files:
        if not isinstance(library_file, dict):
            continue
        file_metadata = library_file.get('metadata')
        file_metadata = file_metadata if isinstance(file_metadata, dict) else {}
        ino = library_file.get('ino')
        if (file_metadata.get('size') == entry['output_bytes']
                and _is_safe_id_segment(ino)):
            file_candidates.append(ino)
    if not file_candidates:
        return _record_unknown(
            f'{prefix} Candidate {item_id} has no single file with the exact '
            'expected byte length. The status REMAINS unknown and resend stays '
            'blocked.', candidates, pages_checked)
    if len(file_candidates) > 1:
        return _record_unknown(
            f'{prefix} Candidate {item_id} has {len(file_candidates)} files with '
            'the exact byte length, so no single file can be verified. The '
            'status REMAINS unknown and resend stays blocked.', candidates, pages_checked)
    file_id = file_candidates[0]
    audio_sizes = []
    for audio_file in audio_files:
        if isinstance(audio_file, dict) and audio_file.get('ino') == file_id:
            audio_meta = audio_file.get('metadata')
            audio_sizes.append(audio_meta.get('size') if isinstance(audio_meta, dict) else None)
    if audio_sizes != [entry['output_bytes']]:
        return _record_unknown(
            f'{prefix} Candidate {item_id} file {file_id} failed audio-file '
            'identity (missing or size mismatch in audioFiles). The status '
            'REMAINS unknown and resend stays blocked.', candidates, pages_checked)
    try:
        found_sha, found_bytes = _download_candidate_digest(
            bound_base, token, item_id, file_id, entry['output_bytes'],
            timeout=(_CONNECT_TIMEOUT, _READ_TIMEOUT))
    except DeliveryError as error:
        return _record_unknown(
            f'{prefix} Candidate {item_id} file {file_id} could not be '
            f'verified: {error} The status REMAINS unknown and resend stays '
            'blocked.', candidates, pages_checked)
    if found_bytes != entry['output_bytes']:
        return _record_unknown(
            f'{prefix} Downloaded candidate file bytes are truncated '
            f'({found_bytes} of {entry["output_bytes"]} bytes). The status '
            'REMAINS unknown and resend stays blocked.', candidates, pages_checked)
    if found_sha.lower() != str(entry['sha256']).lower():
        return _record_unknown(
            f'{prefix} Downloaded candidate file bytes do not match the '
            'expected SHA-256 (wrong bytes). The status REMAINS unknown and '
            'resend stays blocked.', candidates, pages_checked)
    verified_at = time.time()
    entry['status'] = 'sent'
    entry['verification_pending'] = False
    entry.pop('error', None)
    entry['reconciled_at'] = verified_at
    entry['reconciled_item_id'] = item_id
    entry['reconciled_file_id'] = file_id
    entry['reconciled_bytes'] = found_bytes
    entry['reconciled_sha256'] = found_sha
    entry['reconciled_verified_at'] = verified_at
    entry['reconciliation'] = (
        f'{prefix} One exact item/file candidate was verified by raw bytes: '
        f'item {item_id}, file {file_id}, {found_bytes} bytes, SHA-256 '
        f'{found_sha}. The full export file matches the ledger digest and '
        'length, so the status is now sent.')
    entry['reconciliation_candidates'] = candidates
    entry['reconciliation_pages'] = pages_checked
    return _update_reconciled_entry(data_dir, entry)


def send_preview(data_dir, states, base_url, token, preview_id, confirm, *,
                 acknowledge_unknown=False, progress=None):
    """Revalidate one preview and deliver it; returns the final ledger entry.

    Durable order: identity re-checks + destination revalidation -> owned
    snapshot copy (SHA verified) -> ledger ``in_flight`` persisted atomically
    (check+reserve under one lock+file lock) -> network streaming from the
    snapshot. The local export is only read, never moved or deleted, and its
    bytes are verified unchanged after the transfer. HTTP 200 records
    ``unknown`` with ``upload_accepted_at``/``verification_pending`` (never
    ``sent``); only an explicit byte-verified reconcile promotes it.
    ``acknowledge_unknown`` is accepted for wire compatibility but never
    overrides an ``unknown`` outcome: resend stays blocked.
    """
    stored = get_preview(preview_id)
    if stored is None:
        raise DeliveryError('The preview expired or was already used. Preview again.', 409)
    if not _preview_matches(stored, confirm):
        raise DeliveryError(
            'The preview no longer matches the book, export, server, destination, or metadata. Preview again.', 409)
    validated_base = _validate_base_url(base_url)
    if validated_base != stored.get('base_url'):
        raise DeliveryError(
            'The server changed after the preview. Nothing was sent. Preview again.', 409)
    identity = get_current_export_identity(data_dir, states, confirm['book_token'], confirm['export_file'])
    if (identity['sha256'] != stored['sha256'] or identity['input_revision'] != stored['input_revision']
            or identity['file_version'] != stored['file_version']):
        raise DeliveryError('The export or book changed after the preview. Nothing was sent. Preview again.', 409)
    _validate_destination(validated_base, token, stored['library_id'], stored['folder_id'])
    snapshot_path = _make_snapshot(data_dir, identity['path'], stored['sha256'], stored['file_version'])
    entry = None
    try:
        # Atomic duplicate check + reservation + preview consumption.
        from utils import file_lock
        with _reserve_lock, _ledger_lock, file_lock(_ledger_path(data_dir), timeout=30):
            ledger = _read_ledger_raw(data_dir)
            matches = [item for item in ledger
                       if item.get('book_token') == confirm['book_token']
                       and item.get('sha256') == stored['sha256']
                       and item.get('library_id') == stored['library_id']
                       and item.get('folder_id') == stored['folder_id']]
            prior = matches[-1] if matches else None
            if prior is not None and prior.get('status') == 'sent':
                raise DeliveryError(
                    'This exact export was already delivered to this destination. '
                    'Preview a different export or title.', 409)
            if prior is not None and prior.get('status') in ('in_flight', 'unknown'):
                if prior.get('verification_pending') or prior.get('upload_accepted_at'):
                    raise DeliveryError(
                        'A previous send of this export to this destination was '
                        'accepted by the server but is not yet verified. '
                        'Verify delivery before retrying.', 409)
                raise DeliveryError(
                    'A previous send of this export to this destination has an unknown outcome. '
                    'Reconcile it before retrying.', 409)
            entry = {'id': uuid.uuid4().hex, 'status': 'in_flight',
                     'created_at': time.time(), 'book_token': stored['book_token'],
                     'export_file': stored['export_file'], 'sha256': stored['sha256'],
                     'output_bytes': stored['output_bytes'], 'input_revision': stored['input_revision'],
                     'server': validated_base, 'base_url': validated_base,
                     'library_id': stored['library_id'],
                     'folder_id': stored['folder_id'], 'title': stored['title'],
                     'author': stored['author'], 'series': stored['series'],
                     'preview_id': preview_id}
            ledger.append(entry)
            _write_ledger(data_dir, _trim_ledger(ledger))
            _active_deliveries.add(entry['id'])
        _consume_preview(preview_id)
        cancel_event = threading.Event()
        with _preview_lock:
            _cancel_events[preview_id] = cancel_event
        _set_upload_progress(preview_id, 0, stored['output_bytes'])

        def _track(sent):
            _set_upload_progress(preview_id, sent, stored['output_bytes'])
            if progress is not None:
                progress(sent)

        try:
            outcome = _post_snapshot(validated_base, token, stored, snapshot_path,
                                     cancel_event=cancel_event, progress=_track)
            if outcome == 'accepted':
                # HTTP 200 is not proof the bytes were stored (the server
                # ignores a failed file.mv()). Record the acceptance, keep
                # the status unknown, and block resend until Verify delivery
                # confirms the exact bytes -- no automatic retry here.
                entry['status'] = 'unknown'
                entry['upload_accepted_at'] = time.time()
                entry['verification_pending'] = True
                entry['verification_note'] = (
                    'Upload accepted by the server (HTTP 200); delivery is '
                    'not yet verified. Use Verify delivery to confirm the '
                    'exact file bytes on the server. Resend stays blocked '
                    'until byte verification succeeds.')
            else:
                entry['status'] = outcome
        except DeliveryError as error:
            message = str(error).lower()
            if 'redirect' in message or 'unknown' in message or error.status in (499, 504):
                entry['status'] = 'unknown'
            elif error.status in (400, 401, 403, 404):
                entry['status'] = 'failed'
            elif error.status == 409 and 'nothing was sent' in message:
                entry['status'] = 'failed'
            else:
                entry['status'] = 'unknown'
            entry['error'] = str(error)
        except Exception as error:  # network shape the client did not classify
            entry['status'] = 'unknown'
            entry['error'] = f'Upload outcome unknown: {type(error).__name__}.'
        finally:
            with _preview_lock:
                _cancel_events.pop(preview_id, None)
            _clear_upload_progress(preview_id)
            _active_deliveries.discard(entry['id'])
        # The local file must be byte-identical after any outcome.
        try:
            from export_quality import get_export_file_version
            if get_export_file_version(identity['path']) != stored['file_version']:
                entry['error'] = ('WARNING: the local export changed during delivery; '
                                  'the delivery status above does not cover it.')
        except OSError:
            pass
        update_ledger_entry(data_dir, entry)
        return entry
    finally:
        _unlink_quietly(snapshot_path)


def cancel_preview_upload(preview_id):
    """Request cancellation of the in-flight upload for one preview.

    Cancellation is best-effort at the client: the server may already have
    received the bytes, so a cancelled send is recorded ``unknown`` by the
    sender, never assumed undelivered.
    """
    with _preview_lock:
        event = _cancel_events.get(preview_id)
    if event is None:
        return False
    event.set()
    return True
