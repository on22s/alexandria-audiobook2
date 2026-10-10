"""Durable registration evidence for completed batch-trained adapters."""
import json
import math
import os
import re
import time
from pathlib import Path

from task_ownership import acquire_task_slot

from adapter_artifacts import validate_adapter_artifacts
from adapter_publication import is_adapter_checkpoint_recovery_pending
from lora_evidence import get_file_sha256
from utils import atomic_json_write


def get_batch_registration_path(models_dir, dataset_id):
    if not isinstance(dataset_id, str) or not re.fullmatch(r'[a-z0-9_]+', dataset_id):
        raise ValueError('Invalid pending registration dataset identity')
    directory = os.path.join(models_dir, '.batch-registration')
    path = os.path.join(directory, dataset_id + '.json')
    if os.path.islink(directory) or os.path.islink(path):
        raise ValueError('Pending registration evidence must not be a symlink')
    return path


def get_batch_registration_intent(models_dir, dataset_id):
    path = get_batch_registration_path(models_dir, dataset_id)
    if not os.path.exists(path):
        return None
    with open(path, encoding='utf-8') as handle:
        record = json.load(handle)
    if (not isinstance(record, dict) or type(record.get('version')) is not int or record['version'] != 1
            or record.get('dataset_id') != dataset_id
            or not isinstance(record.get('adapter_id'), str)
            or not re.fullmatch(re.escape(dataset_id) + r'_[0-9]+', record['adapter_id'])
            or not isinstance(record.get('zip_source'), str)
            or not os.path.isabs(record['zip_source'])
            or not isinstance(record.get('source_sha256'), str)
            or not re.fullmatch(r'[0-9a-f]{64}', record['source_sha256'])
            or not isinstance(record.get('settings'), dict)
            or not isinstance(record.get('created'), (int, float))
            or isinstance(record.get('created'), bool) or not math.isfinite(record['created'])):
        raise ValueError('Malformed pending batch registration evidence')
    return record


def apply_batch_registration_intent(models_dir, dataset_id, adapter_id, source, settings, *, ownership=None):
    if get_batch_registration_intent(models_dir, dataset_id) is not None:
        raise ValueError('Batch registration already pending for ' + dataset_id)
    record = {'version': 1, 'dataset_id': dataset_id, 'adapter_id': adapter_id,
              'zip_source': os.path.abspath(source), 'source_sha256': get_file_sha256(source),
              'settings': dict(settings), 'created': time.time(), 'entry': None}
    if ownership is not None:
        record['ownership'] = ownership
    path = get_batch_registration_path(models_dir, dataset_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    atomic_json_write(record, path)
    return record


def get_batch_registration_entry(models_dir, dataset_id, source):
    record = get_batch_registration_intent(models_dir, dataset_id)
    if record is None:
        return None
    if (os.path.abspath(source) != record['zip_source']
            or get_file_sha256(source) != record['source_sha256']):
        raise ValueError('Pending registration source archive changed; refusing recovery')
    adapter = os.path.join(models_dir, record['adapter_id'])
    if is_adapter_checkpoint_recovery_pending(adapter):
        raise ValueError('Adapter checkpoint publication recovery is required before registration')
    validate_adapter_artifacts(adapter, require_training_meta=True)
    weights_sha = get_file_sha256(os.path.join(adapter, 'adapter_model.safetensors'))
    with open(os.path.join(adapter, 'training_meta.json'), encoding='utf-8') as handle:
        metadata = json.load(handle)
    if not isinstance(metadata, dict):
        raise ValueError('Malformed pending registration training metadata')
    expected = record.get('checkpoint_sha256') or metadata.get('checkpoint_sha256')
    if weights_sha != expected:
        raise ValueError('Pending registration checkpoint changed or lacks hash evidence')
    if metadata.get('checkpoint_sha256') not in (None, weights_sha):
        raise ValueError('Pending registration metadata does not describe current weights')
    if record.get('entry') is not None:
        entry = record['entry']
        if (not isinstance(entry, dict) or entry.get('id') != record['adapter_id']
                or entry.get('dataset_id') != dataset_id or entry.get('zip_source') != record['zip_source']
                or entry.get('checkpoint_sha256') != weights_sha):
            raise ValueError('Pending registration entry conflicts with its frozen identity')
        return dict(entry)
    count, loss = metadata.get('num_samples'), metadata.get('best_loss', metadata.get('final_loss'))
    if (type(count) is not int or count < 1 or not isinstance(loss, (int, float))
            or isinstance(loss, bool) or not math.isfinite(loss)):
        raise ValueError('Completed pending adapter lacks recoverable training evidence')
    return {'id': record['adapter_id'], 'name': dataset_id, 'dataset_id': dataset_id,
            'zip_source': record['zip_source'], 'checkpoint_sha256': weights_sha,
            'sample_count': count, 'best_loss': loss, 'final_loss': metadata.get('final_loss'),
            'epochs_run': metadata.get('epochs'), 'epoch_losses': {},
            'lora_r': record['settings'].get('lora_r'), 'lr': record['settings'].get('lr'),
            'target_loss': record['settings'].get('target_loss'), 'created': record['created'],
            'evaluation_candidates': metadata.get('evaluation_candidates', []),
            'evaluation_candidate_skips': metadata.get('evaluation_candidate_skips', [])}


def save_batch_registration_entry(models_dir, dataset_id, entry):
    record = get_batch_registration_intent(models_dir, dataset_id)
    if record is None or not isinstance(entry, dict) or entry.get('id') != record['adapter_id'] or entry.get('dataset_id') != dataset_id:
        raise ValueError('Training result does not match pending registration intent')
    adapter = os.path.join(models_dir, record['adapter_id'])
    if is_adapter_checkpoint_recovery_pending(adapter):
        raise ValueError('Adapter checkpoint publication recovery is required before registration')
    validate_adapter_artifacts(adapter, require_training_meta=True)
    digest = get_file_sha256(os.path.join(adapter, 'adapter_model.safetensors'))
    if entry.get('checkpoint_sha256') not in (None, digest):
        raise ValueError('Training result checkpoint hash changed before registration')
    updated = {**record, 'checkpoint_sha256': digest,
               'entry': {**entry, 'zip_source': record['zip_source'], 'checkpoint_sha256': digest}}
    atomic_json_write(updated, get_batch_registration_path(models_dir, dataset_id))
    return dict(updated['entry'])


def clear_batch_registration_intent(models_dir, dataset_id, adapter_id):
    record = get_batch_registration_intent(models_dir, dataset_id)
    if record is not None:
        if record['adapter_id'] != adapter_id:
            raise ValueError('Pending registration owner changed before cleanup')
        os.remove(get_batch_registration_path(models_dir, dataset_id))


BATCH_OWNERSHIP_PROTOCOL = 'retained-posix-lease-v1'


def acquire_batch_registration_lease(models_dir, dataset_id):
    """Caller closes (never explicitly unlocks) after its owned tree and commit."""
    path = get_batch_registration_path(models_dir, dataset_id)
    directory = Path(path).parent
    directory.mkdir(parents=True, exist_ok=True)
    lock = directory / ('task-' + dataset_id + '.lock')
    if lock.is_symlink():
        raise ValueError('Batch ownership lease must not be a symlink')
    return acquire_task_slot(directory, dataset_id)


def restart_incomplete_batch_registration(models_dir, dataset_id, source, adapter_id, settings):
    """Replace a pre-checkpoint intent only under its retained lifetime lease.

    The caller also holds the naming lock. Keep partial output and the old
    intent as evidence; never turn a damaged completed checkpoint into training.
    Legacy intents cannot prove that an orphan trainer has stopped.
    """
    record = get_batch_registration_intent(models_dir, dataset_id)
    if record is None or record.get('ownership') != BATCH_OWNERSHIP_PROTOCOL:
        raise ValueError('Incomplete legacy/unsupported batch intent: stop its original trainer and review its artifacts before manual recovery')
    if os.path.abspath(source) != record['zip_source'] or get_file_sha256(source) != record['source_sha256']:
        raise ValueError('Pending registration source archive changed; refusing recovery')
    old = os.path.join(models_dir, record['adapter_id'])
    if os.path.islink(old) or (os.path.lexists(old) and not os.path.isdir(old)):
        raise ValueError('Unsafe incomplete adapter output')
    if is_adapter_checkpoint_recovery_pending(old):
        raise ValueError('Adapter checkpoint publication recovery is required before retry')
    if (record.get('entry') is not None or record.get('checkpoint_sha256') is not None
            or any(os.path.lexists(os.path.join(old, name)) for name in
                   ('adapter_model.safetensors', 'training_meta.json'))):
        raise ValueError('Pending adapter has checkpoint evidence; repair or recover it without retraining')
    if adapter_id == record['adapter_id'] or os.path.lexists(os.path.join(models_dir, adapter_id)):
        raise ValueError('Retry must use a fresh adapter identity')
    path = get_batch_registration_path(models_dir, dataset_id)
    history = os.path.join(os.path.dirname(path), 'history')
    if os.path.islink(history):
        raise ValueError('Batch intent history must not be a symlink')
    os.makedirs(history, exist_ok=True)
    prior = os.path.join(history, record['adapter_id'] + '.json')
    if os.path.lexists(prior):
        if os.path.islink(prior):
            raise ValueError('Batch history record must not be a symlink')
        with open(prior, encoding='utf-8') as handle:
            if json.load(handle) != record:
                raise ValueError('Batch intent history conflicts with recovery evidence')
    else:
        atomic_json_write(record, prior)
    replacement = {**record, 'adapter_id': adapter_id, 'settings': dict(settings),
                   'created': time.time(), 'entry': None}
    atomic_json_write(replacement, path)
    return replacement


def get_fresh_batch_adapter_id(models_dir, dataset_id):
    """Allocate under naming lock, excluding both output and archived intents."""
    path = get_batch_registration_path(models_dir, dataset_id)
    record = get_batch_registration_intent(models_dir, dataset_id)
    stamp = int(time.time())
    while True:
        candidate = f'{dataset_id}_{stamp}'
        history = os.path.join(os.path.dirname(path), 'history', candidate + '.json')
        if (not os.path.lexists(os.path.join(models_dir, candidate))
                and not os.path.lexists(history)
                and (record is None or record['adapter_id'] != candidate)):
            return candidate
        stamp += 1
