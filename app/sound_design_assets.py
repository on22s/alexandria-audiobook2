"""Durable immutable asset records, independent of active and saved books.

Mutations share a kernel lease through import/recovery. Slow work runs on the
caller's worker, without holding an application task/book mutex. Ready assets
never expire; incomplete reservations survive failures and process death.
"""
from contextlib import contextmanager, ExitStack
import json
import hashlib
import os
from pathlib import Path
import re
import stat
import time
import uuid

from adapter_publication import save_adapter_publication_bytes, sync_adapter_directory
from audio_take_history import _ensure_plain_path, _check_cancelled, _open_regular, _version, _hash
from json_metadata import get_metadata_json_bytes, get_unique_json_object
from project_scope_files import capture_scope_file
from sound_design_cues import _require_digest, _require_int, MAX_RATE, MAX_SECONDS
from source_evidence import _require_keys, _require_text
from utils import file_lock

MAX_ASSETS = 256
MAX_SOURCE_BYTES = 256 * 1024**2
MAX_PCM_BYTES = 1024**3
MAX_TOTAL_BYTES = 4 * 1024**3
MAX_MANIFEST_BYTES = 1024**2
DIRECTORY = '.sound_design_assets'


@contextmanager
def ensure_sound_asset_lease(target, cancel_check=None):
    """Acquire an asset lease with bounded, cancellation-aware contention."""
    with ExitStack() as stack:
        deadline = time.monotonic() + 10
        while True:
            _check_cancelled(cancel_check)
            try:
                stack.enter_context(file_lock(str(target), timeout=0))
                break
            except TimeoutError:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(.05)
        _check_cancelled(cancel_check)
        yield


def _validate_asset_id(value):
    if not isinstance(value, str) or re.fullmatch(r'[0-9a-f]{32}', value) is None:
        raise ValueError('Invalid sound asset identity')


def validate_sound_asset_display_name(value):
    _require_text(value, 128, 'Sound asset name')
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError('Sound asset name must not contain control characters')


def get_sound_asset_manifest_bytes(document):
    _require_keys(document, ('schema', 'assets'), 'Sound asset manifest')
    if type(document['schema']) is not int or document['schema'] != 1:
        raise ValueError('Unsupported sound asset manifest')
    assets = document['assets']
    if not isinstance(assets, list) or len(assets) > MAX_ASSETS:
        raise ValueError('Sound asset count exceeds its limit')
    known, charged = set(), 0
    for row in assets:
        fields = ('asset_id', 'state', 'source', 'reserved_pcm_bytes', 'canonical')
        if isinstance(row, dict) and 'display_name' in row:
            fields += ('display_name',)
        _require_keys(row, fields, 'Sound asset')
        if 'display_name' in row:
            validate_sound_asset_display_name(row['display_name'])
        _validate_asset_id(row['asset_id'])
        if row['asset_id'] in known or row['state'] not in ('incomplete', 'ready'):
            raise ValueError('Repeated sound asset identity or invalid state')
        known.add(row['asset_id'])
        source = row['source']
        _require_keys(source, ('sha256', 'bytes'), 'Sound asset source')
        _require_digest(source['sha256'], 'Source fingerprint')
        _require_int(source['bytes'], 1, MAX_SOURCE_BYTES, 'Original source size')
        _require_int(row['reserved_pcm_bytes'], 1, MAX_PCM_BYTES, 'PCM reservation')
        canonical = row['canonical']
        if row['state'] == 'incomplete':
            if canonical is not None:
                raise ValueError('Incomplete asset cannot advertise canonical audio')
            pcm_bytes = row['reserved_pcm_bytes']
        else:
            _require_keys(canonical, ('sha256', 'bytes', 'frames', 'frame_rate', 'channels',
                                     'sample_width', 'conversion', 'codec', 'format'), 'Canonical sound asset')
            _require_digest(canonical['sha256'], 'Canonical fingerprint')
            _require_int(canonical['frame_rate'], 1, MAX_RATE, 'Canonical rate')
            _require_int(canonical['frames'], 1, canonical['frame_rate'] * MAX_SECONDS, 'Canonical frames')
            _require_int(canonical['channels'], 1, 2, 'Canonical channels')
            _require_int(canonical['sample_width'], 4, 4, 'Canonical sample width')
            _require_int(canonical['bytes'], 1, row['reserved_pcm_bytes'], 'Canonical size')
            if canonical['bytes'] != canonical['frames'] * canonical['channels'] * 4:
                raise ValueError('Canonical asset size disagrees with its frame geometry')
            _require_text(canonical['conversion'], 128, 'Conversion identity')
            _require_text(canonical['codec'], 64, 'Source codec')
            if canonical['format'] not in ('wav', 'flac', 'mp3', 'ogg'):
                raise ValueError('Unsupported source container')
            pcm_bytes = canonical['bytes']
        charged += source['bytes'] + pcm_bytes
    if charged > MAX_TOTAL_BYTES:
        raise ValueError('Sound asset storage exceeds its limit')
    return get_metadata_json_bytes(document, MAX_MANIFEST_BYTES, 'Sound assets')


def _refuse_nonfinite(_value):
    raise ValueError('Nonfinite sound asset metadata is invalid')


def get_sound_asset_manifest(raw):
    if not isinstance(raw, bytes) or not 1 <= len(raw) <= MAX_MANIFEST_BYTES:
        raise ValueError('Sound asset metadata is empty or too large')
    try:
        document = json.loads(raw, object_pairs_hook=get_unique_json_object,
                              parse_constant=_refuse_nonfinite)
        get_sound_asset_manifest_bytes(document)
    except (UnicodeError, ValueError, TypeError, RecursionError):
        raise ValueError('Invalid sound asset metadata; existing files were preserved') from None
    return document


def get_sound_asset_storage_bytes(document):
    get_sound_asset_manifest_bytes(document)
    return sum(row['source']['bytes'] + (row['reserved_pcm_bytes'] if row['state'] == 'incomplete'
                                       else row['canonical']['bytes']) for row in document['assets'])


class SoundDesignAssetStore:
    def __init__(self, root, *, source_limit=MAX_SOURCE_BYTES, pcm_limit=MAX_PCM_BYTES,
                 total_limit=MAX_TOTAL_BYTES):
        self.root = Path(os.path.abspath(root))
        if (not self.root.is_dir()
                or os.path.normcase(os.path.realpath(self.root)) != os.path.normcase(str(self.root))):
            raise ValueError('Sound assets require an existing root without aliases')
        for value, maximum in ((source_limit, MAX_SOURCE_BYTES), (pcm_limit, MAX_PCM_BYTES),
                               (total_limit, MAX_TOTAL_BYTES)):
            _require_int(value, 1, maximum, 'Sound asset storage limit')
        self.source_limit, self.pcm_limit, self.total_limit = source_limit, pcm_limit, total_limit
        self.directory = self.root / DIRECTORY
        self.manifest = self.directory / 'manifest.json'

    def _load(self):
        _proof, raw = capture_scope_file(self.root, f'{DIRECTORY}/manifest.json',
                                         limit=MAX_MANIFEST_BYTES, include_bytes=True)
        return get_sound_asset_manifest(raw) if raw is not None else {'schema': 1, 'assets': []}

    def _save(self, document):
        raw = get_sound_asset_manifest_bytes(document)
        # Existing metadata must retain the same regular-file/alias contract.
        self._load()
        save_adapter_publication_bytes(str(self.manifest), raw, mode=0o600)

    @contextmanager
    def _locked(self, cancel_check=None):
        _check_cancelled(cancel_check)
        _ensure_plain_path(self.directory, self.root)
        self.directory.mkdir(mode=0o700, exist_ok=True)
        lock = Path(str(self.manifest) + '.lock')
        _ensure_plain_path(lock, self.root)
        if os.path.lexists(lock):
            info = lock.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_size > 1:
                raise ValueError('Invalid sound asset store lease')
        with ensure_sound_asset_lease(self.manifest, cancel_check):
            document = self._load()
            self._ensure_inventory(document)
            yield document

    def _ensure_inventory(self, document):
        expected = {'manifest.json', 'manifest.json.lock'}
        sizes = {}
        for row in document['assets']:
            expected.update({row['asset_id'] + '.source', row['asset_id'] + '.pcm'})
            sizes[row['asset_id'] + '.source'] = (row['source']['bytes'], row['state'] == 'ready')
            sizes[row['asset_id'] + '.pcm'] = (
                row['canonical']['bytes'] if row['state'] == 'ready' else row['reserved_pcm_bytes'],
                row['state'] == 'ready')
        found = set()
        for path in self.directory.iterdir():
            _ensure_plain_path(path, self.root)
            info = path.lstat()
            if path.name not in expected or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('Unregistered or unsafe sound asset files require recovery; files were preserved')
            found.add(path.name)
            if path.name in sizes:
                size, complete = sizes[path.name]
                if info.st_size > size or complete and info.st_size != size:
                    raise ValueError('Stored asset size differs from its charged record')
        if any(complete and name not in found for name, (_, complete) in sizes.items()):
            raise ValueError('A ready sound asset is missing; remaining files were preserved')

    def _path(self, asset_id, suffix):
        _validate_asset_id(asset_id)
        return _ensure_plain_path(self.directory / (asset_id + suffix), self.root)

    def _find(self, document, asset_id):
        _validate_asset_id(asset_id)
        for row in document['assets']:
            if row['asset_id'] == asset_id:
                return row
        raise ValueError('Sound asset was not found')

    @contextmanager
    def open_canonical(self, asset_id, *, cancel_check=None):
        """Verify exact original/canonical bytes and retain descriptors during use."""
        row = self._find(self._load(), asset_id)
        if row['state'] != 'ready':
            raise ValueError('Sound asset import is incomplete')
        with ExitStack() as stack:
            opened = []
            for suffix, facts in (('.source', row['source']), ('.pcm', row['canonical'])):
                path = self._path(asset_id, suffix)
                stream, before = _open_regular(path, self.root, facts['bytes'])
                stack.enter_context(stream)
                if _hash(stream, facts['bytes'], cancel_check=cancel_check) != (facts['sha256'], facts['bytes']):
                    raise ValueError('Sound asset bytes changed; files were preserved')
                if _version(os.fstat(stream.fileno())) != _version(before) or _version(path.lstat()) != _version(before):
                    raise ValueError('Sound asset changed while verifying')
                stream.seek(0)
                opened.append((path, stream, before))
            descriptor = {'asset_id': asset_id, **{key: row['canonical'][key] for key in
                ('sha256', 'frames', 'frame_rate', 'channels', 'sample_width')}}
            _check_cancelled(cancel_check)
            yield opened[-1][1], descriptor
            _check_cancelled(cancel_check)
            for path, stream, before in opened:
                _ensure_plain_path(path, self.root)
                if _version(os.fstat(stream.fileno())) != _version(before) or _version(path.lstat()) != _version(before):
                    raise ValueError('Sound asset changed during use')

    def list_assets(self):
        """Read a bounded detached manifest; listing is not media admission."""
        document = self._load()
        return dict(assets=document['assets'], charged_bytes=get_sound_asset_storage_bytes(document),
                    limit_bytes=self.total_limit)

    def import_asset(self, source_name, *, display_name='Sound asset', cancel_check=None):
        from sound_design_decode import decode_sound_design_asset, CONVERSION
        validate_sound_asset_display_name(display_name)
        with self._locked(cancel_check) as document:
            before, _ = capture_scope_file(self.root, source_name, limit=self.source_limit,
                                           cancel_check=cancel_check)
            if not before['exists'] or not before['size']:
                raise ValueError('Sound asset source is missing or empty')
            source_facts = dict(sha256=before['sha256'], bytes=before['size'])
            for row in document['assets']:
                if (row['state'] == 'ready' and row['source'] == source_facts
                        and row['canonical']['conversion'] == CONVERSION):
                    with self.open_canonical(row['asset_id'], cancel_check=cancel_check) as (_, descriptor):
                        return descriptor
            if (len(document['assets']) >= MAX_ASSETS or get_sound_asset_storage_bytes(document)
                    + before['size'] + self.pcm_limit > self.total_limit):
                raise ValueError('Sound asset storage cannot reserve this import')
            row = dict(asset_id=uuid.uuid4().hex, state='incomplete', source=source_facts,
                       reserved_pcm_bytes=self.pcm_limit, canonical=None, display_name=display_name)
            source_copy = self._path(row['asset_id'], '.source')
            pcm_path = self._path(row['asset_id'], '.pcm')
            if os.path.lexists(source_copy) or os.path.lexists(pcm_path):
                raise ValueError('Sound asset identity collision')
            document['assets'].append(row)
            _check_cancelled(cancel_check)
            self._save(document)  # Charge before creating any media bytes.
            source_path = self.root / source_name
            source, info = _open_regular(source_path, self.root, self.source_limit)
            with source, source_copy.open('xb') as output:
                if list(_version(info)) != before['file_version']:
                    raise ValueError('Original sound source changed before copying')
                digest, size = hashlib.sha256(), 0
                while block := source.read(65536):
                    _check_cancelled(cancel_check)
                    size += len(block)
                    if size > before['size']:
                        raise ValueError('Original sound source grew during copying')
                    if output.write(block) != len(block):
                        raise OSError('Incomplete sound source copy')
                    digest.update(block)
                output.flush()
                os.fsync(output.fileno())
                if (size != before['size'] or digest.hexdigest() != before['sha256']
                        or _version(os.fstat(source.fileno())) != _version(info)
                        or _version(_ensure_plain_path(source_path, self.root).lstat()) != _version(info)):
                    raise ValueError('Original sound source changed during copying')
            result = decode_sound_design_asset(self.root, source_copy.relative_to(self.root).as_posix(),
                pcm_path.relative_to(self.root).as_posix(), source_limit=self.source_limit,
                pcm_limit=self.pcm_limit, cancel_check=cancel_check)
            if (result['source']['sha256'], result['source']['size']) != (before['sha256'], before['size']):
                raise ValueError('Copied sound source changed before decoding')
            proof, _ = capture_scope_file(self.root, pcm_path.relative_to(self.root).as_posix(),
                                          limit=self.pcm_limit, cancel_check=cancel_check)
            if (proof['sha256'], proof['size']) != (result['sha256'], result['bytes']):
                raise ValueError('Canonical sound bytes changed before publication')
            row['canonical'] = {key: result[key] for key in ('sha256', 'bytes', 'frames',
                'frame_rate', 'channels', 'sample_width', 'conversion', 'codec', 'format')}
            row['state'] = 'ready'
            _check_cancelled(cancel_check)
            self._save(document)  # A late failure retains a possibly ready asset.
            return {'asset_id': row['asset_id'], **{key: row['canonical'][key] for key in
                ('sha256', 'frames', 'frame_rate', 'channels', 'sample_width')}}

    def purge_incomplete(self, asset_id, *, cancel_check=None):
        """Explicit recovery; the import lease fences live producers and readers."""
        with self._locked(cancel_check) as document:
            row = self._find(document, asset_id)
            if row['state'] != 'incomplete':
                raise ValueError('Ready assets are retained for saved cue history')
            for suffix in ('.source', '.pcm'):
                _check_cancelled(cancel_check)
                self._path(asset_id, suffix).unlink(missing_ok=True)
            sync_adapter_directory(str(self.directory))
            document['assets'].remove(row)
            self._save(document)
