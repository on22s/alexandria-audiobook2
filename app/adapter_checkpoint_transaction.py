"""Recoverable whole-directory checkpoint generations under shared root admission."""
import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import tempfile
import uuid

from adapter_artifacts import validate_adapter_artifacts
from audio_validation import validate_generated_audio, validate_finite_audio_values
from adapter_publication import (CHECKPOINT_SWAP_JOURNAL, get_adapter_bundle_sha256,
                                 get_adapter_checkpoint_generation_journal,
                                 get_adapter_publication_recovery_command, get_adapter_publication_owner,
                                 save_adapter_publication_bytes, sync_adapter_directory,
                                 sync_adapter_tree)
from lora_evidence import get_file_sha256
from utils import file_lock
from voice_manifest import get_resolved_adapter_path_locked

GENERATION_FILES = ('adapter_config.json', 'adapter_model.safetensors',
                    'ref_sample.wav', 'training_meta.json')


def get_adapter_checkpoint_journal(adapter_dir):
    return get_adapter_checkpoint_generation_journal(adapter_dir)


def _get_adapter_path(adapter_dir):
    adapter = Path(os.path.abspath(adapter_dir))
    if adapter.is_symlink() or not adapter.parent.is_dir() or adapter.name in ('', '.', '..'):
        raise ValueError('Unsafe adapter checkpoint directory')
    if adapter.exists() and not adapter.is_dir():
        raise ValueError('Adapter checkpoint must be a directory')
    return adapter.parent.resolve() / adapter.name


def _move(source, target):
    os.replace(source, target)
    sync_adapter_directory(source.parent)
    if target.parent != source.parent:
        sync_adapter_directory(target.parent)


def _save_journal(adapter, data):
    save_adapter_publication_bytes(str(get_adapter_checkpoint_journal(adapter)),
                                   json.dumps(data, sort_keys=True).encode())


def _require_bundle(path, digest):
    if path.is_symlink() or not path.is_dir() or get_adapter_bundle_sha256(path) != digest:
        raise ValueError('Adapter generation is missing or changed; recovery evidence retained')


def _finish(adapter, workspace):
    if workspace.exists():
        shutil.rmtree(workspace)
        sync_adapter_directory(adapter.parent)
    get_adapter_checkpoint_journal(adapter).unlink()
    sync_adapter_directory(adapter.parent)



def _require_manifest_file(path, digests):
    if path.is_symlink() or not path.is_file() or get_file_sha256(str(path)) not in digests:
        raise ValueError('Adapter manifest is missing or changed; recovery evidence retained')


def _get_checkpoint_manifest(adapter, workspace, data):
    manifest_data = data.get('manifest')
    if manifest_data is None:
        return None
    if (not isinstance(manifest_data, dict) or manifest_data.get('name') != 'manifest.json'
            or not all(re.fullmatch(r'[0-9a-f]{64}', str(manifest_data.get(key)))
                       for key in ('old_sha256', 'new_sha256'))):
        raise ValueError('Invalid adapter manifest recovery journal')
    manifest = adapter.parent / 'manifest.json'
    if data['phase'] in ('committed', 'recovered'):
        key = 'new_sha256' if data['phase'] == 'committed' else 'old_sha256'
        _require_manifest_file(manifest, {manifest_data[key]})
    else:
        _require_manifest_file(manifest, {manifest_data['old_sha256'], manifest_data['new_sha256']})
        _require_manifest_file(workspace / 'manifest.original', {manifest_data['old_sha256']})
        staged = workspace / 'manifest.next'
        if os.path.lexists(staged):
            _require_manifest_file(staged, {manifest_data['new_sha256']})
    return manifest


def recover_adapter_checkpoint_locked(adapter_dir):
    """Restore an uncommitted generation or finish committed cleanup; root lock held."""
    adapter = _get_adapter_path(adapter_dir)
    journal = get_adapter_checkpoint_journal(adapter)
    if not os.path.lexists(journal):
        return False
    if journal.is_symlink():
        raise ValueError('Unsafe adapter checkpoint journal')
    data = json.loads(journal.read_text())
    if not isinstance(data, dict):
        raise ValueError('Invalid adapter checkpoint journal')
    name = data.get('workspace')
    if (data.get('version') != 1 or data.get('adapter') != adapter.name
            or data.get('phase') not in ('publishing', 'committed', 'recovered')
            or not isinstance(name, str) or not re.fullmatch(r'\.checkpoint-generation-[0-9a-f]{32}', name)
            or not isinstance(data.get('existed'), bool)
            or not re.fullmatch(r'[0-9a-f]{64}', str(data.get('new_sha256')))
            or (data['existed'] and not re.fullmatch(r'[0-9a-f]{64}', str(data.get('old_sha256'))))):
        raise ValueError('Invalid adapter checkpoint journal')
    workspace = adapter.parent / name
    if workspace.is_symlink():
        raise ValueError('Unsafe adapter recovery workspace')
    manifest = _get_checkpoint_manifest(adapter, workspace, data)
    if data['phase'] in ('committed', 'recovered'):
        if data['phase'] == 'committed':
            _require_bundle(adapter, data['new_sha256'])
        elif data['existed']:
            _require_bundle(adapter, data['old_sha256'])
        elif adapter.exists():
            raise ValueError('Unexpected adapter after recovery')
        _finish(adapter, workspace)
        return True
    if not workspace.is_dir():
        raise ValueError('Adapter recovery workspace is missing')
    prepared = workspace / 'prepared'
    if prepared.exists() or prepared.is_symlink():
        _require_bundle(prepared, data['new_sha256'])
    previous, rejected = workspace / 'previous', workspace / 'rejected'
    if rejected.exists() or rejected.is_symlink():
        _require_bundle(rejected, data['new_sha256'])
    if previous.exists() or previous.is_symlink():
        if not data['existed']:
            raise ValueError('Unexpected original adapter generation')
        _require_bundle(previous, data['old_sha256'])
    # Validate every owned generation before moving anything.
    if data['existed']:
        if not previous.exists():
            _require_bundle(adapter, data['old_sha256'])
        elif adapter.exists():
            _require_bundle(adapter, data['new_sha256'])
    elif adapter.exists():
        _require_bundle(adapter, data['new_sha256'])
    if (previous.exists() or not data['existed']) and adapter.exists():
        if rejected.exists():
            raise ValueError('Unexpected duplicate adapter generation')
        _move(adapter, rejected)
    if previous.exists():
        _move(previous, adapter)
    if manifest is not None:
        save_adapter_publication_bytes(str(manifest), (workspace / 'manifest.original').read_bytes(),
                                       mode=(workspace / 'manifest.original').stat().st_mode)
    data['phase'] = 'recovered'
    _save_journal(adapter, data)
    _finish(adapter, workspace)
    return True


@contextlib.contextmanager
def ensure_adapter_checkpoint(adapter_dir, timeout=10):
    """Hold the stable family lock and recover before any generation reads/writes."""
    adapter = _get_adapter_path(adapter_dir)
    with file_lock(str(adapter.parent / 'manifest.json'), timeout=timeout):
        # A naming journal can contain intermediate registry/directory state.
        if get_adapter_publication_owner(str(adapter.parent)):
            command = get_adapter_publication_recovery_command(str(adapter.parent))
            raise ValueError('Adapter publication recovery required: ' + command)
        adapter = _get_adapter_path(get_resolved_adapter_path_locked(str(adapter)))
        command = get_adapter_publication_recovery_command(str(adapter.parent),
                                                          ignore_checkpoint_journal=get_adapter_checkpoint_journal(adapter))
        if command:
            raise ValueError('Adapter publication recovery required: ' + command)
        if os.path.lexists(adapter / CHECKPOINT_SWAP_JOURNAL):
            raise ValueError('Legacy checkpoint-swap recovery is required first')
        recover_adapter_checkpoint_locked(adapter)
        yield adapter


def validate_adapter_checkpoint_generation(adapter_dir):
    """Validate serving artifacts and their declared weight/reference identities."""
    adapter = Path(adapter_dir)
    for filename in GENERATION_FILES:
        path = adapter / filename
        if path.is_symlink() or not path.is_file():
            raise ValueError('Adapter serving artifact must be a regular file: ' + filename)
    validate_adapter_artifacts(str(adapter), require_training_meta=True)
    meta = json.loads((adapter / 'training_meta.json').read_text())
    json.dumps(meta, allow_nan=False)
    if meta.get('checkpoint_sha256') != get_file_sha256(str(adapter / 'adapter_model.safetensors')):
        raise ValueError('Adapter metadata does not identify its weights')
    if not isinstance(meta.get('ref_sample_text'), str) or not meta['ref_sample_text'].strip():
        raise ValueError('Adapter reference text is missing')
    if 'reference_audio_sha256' in meta and meta['reference_audio_sha256'] != get_file_sha256(str(adapter / 'ref_sample.wav')):
        raise ValueError('Adapter metadata does not identify its reference audio')
    import soundfile as sf
    validate_generated_audio(str(adapter / 'ref_sample.wav'), 'adapter reference')
    info = sf.info(str(adapter / 'ref_sample.wav'))
    if info.frames <= 0 or info.samplerate <= 0 or info.channels != 1:
        raise ValueError('Adapter reference must be nonempty mono audio')
    with sf.SoundFile(str(adapter / 'ref_sample.wav')) as audio:
        for block in audio.blocks(blocksize=65536):
            validate_finite_audio_values(block, 'adapter reference')
    return meta


def get_adapter_generation_sha256(adapter_dir):
    """Identify the exact served config, weights, reference audio and metadata."""
    rows = [(name, get_file_sha256(str(Path(adapter_dir) / name))) for name in GENERATION_FILES]
    return hashlib.sha256(json.dumps(rows).encode()).hexdigest()


@contextlib.contextmanager
def ensure_adapter_generation_snapshot(adapter_dir):
    """Capture serving bytes under admission, then release before slow model loads."""
    with tempfile.TemporaryDirectory(prefix='alexandria-lora-generation-') as temporary:
        with ensure_adapter_checkpoint(adapter_dir) as adapter:
            generation = get_adapter_generation_sha256(adapter)
            for name in GENERATION_FILES:
                shutil.copy2(adapter / name, Path(temporary) / name)
            if get_adapter_generation_sha256(temporary) != generation:
                raise ValueError('Adapter generation changed during snapshot capture')
        yield temporary, generation


def apply_adapter_checkpoint_locked(adapter_dir, writer, manifest_entries=None):
    """Writer populates a private staged directory; caller holds ensure_ admission.

    Preserves auxiliary artifacts. Serving files are validated before any live move.
    """
    adapter = _get_adapter_path(adapter_dir)
    if os.path.lexists(get_adapter_checkpoint_journal(adapter)):
        raise ValueError('Adapter checkpoint recovery is required first')
    workspace = adapter.parent / ('.checkpoint-generation-' + uuid.uuid4().hex)
    workspace.mkdir()
    prepared = workspace / 'prepared'
    try:
        manifest = adapter.parent / 'manifest.json'
        manifest_data = None
        if manifest_entries is not None:
            if not isinstance(manifest_entries, list):
                raise ValueError('Adapter manifest entries must be a list')
            _require_manifest_file(manifest, {get_file_sha256(str(manifest))})
            original_manifest = manifest.read_bytes()
            new_manifest = json.dumps(manifest_entries, indent=2, ensure_ascii=False, allow_nan=False).encode()
            save_adapter_publication_bytes(str(workspace / 'manifest.original'), original_manifest,
                                           mode=manifest.stat().st_mode)
            save_adapter_publication_bytes(str(workspace / 'manifest.next'), new_manifest,
                                           mode=manifest.stat().st_mode)
            manifest_data = {'name':'manifest.json',
                             'old_sha256':hashlib.sha256(original_manifest).hexdigest(),
                             'new_sha256':hashlib.sha256(new_manifest).hexdigest()}
        existed = adapter.exists()
        old_digest = get_adapter_bundle_sha256(adapter) if existed else None
        if existed:
            for filename in (*GENERATION_FILES, 'README.md'):
                if (adapter / filename).is_symlink():
                    raise ValueError('Unsafe writable checkpoint artifact: ' + filename)
            shutil.copytree(adapter, prepared, symlinks=True)
        else:
            prepared.mkdir()
        writer(str(prepared))
        validate_adapter_checkpoint_generation(prepared)
        sync_adapter_tree(str(prepared))
        if existed:
            _require_bundle(adapter, old_digest)
        elif adapter.exists() or adapter.is_symlink():
            raise ValueError('Adapter target appeared during staging')
        data = {'version':1, 'adapter':adapter.name, 'workspace':workspace.name,
                'phase':'publishing', 'existed':existed, 'old_sha256':old_digest,
                'new_sha256':get_adapter_bundle_sha256(prepared)}
        if manifest_data is not None:
            _require_manifest_file(manifest, {manifest_data['old_sha256']})
            data['manifest'] = manifest_data
        _save_journal(adapter, data)
        if existed:
            _move(adapter, workspace / 'previous')
        _move(prepared, adapter)
        if manifest_data is not None:
            _move(workspace / 'manifest.next', manifest)
        data['phase'] = 'committed'
        _save_journal(adapter, data)
        _finish(adapter, workspace)
        return get_adapter_generation_sha256(adapter)
    except BaseException:
        if os.path.lexists(get_adapter_checkpoint_journal(adapter)):
            recover_adapter_checkpoint_locked(adapter)
        elif workspace.exists():
            shutil.rmtree(workspace)
        raise


def save_adapter_checkpoint(adapter_dir, writer, manifest_entries=None):
    with ensure_adapter_checkpoint(adapter_dir) as adapter:
        return apply_adapter_checkpoint_locked(adapter, writer, manifest_entries=manifest_entries)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--recover-output', required=True)
    args = parser.parse_args()
    with ensure_adapter_checkpoint(args.recover_output):
        print('Adapter checkpoint recovery finished')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
