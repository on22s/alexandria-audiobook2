"""Durable registration evidence for completed batch-trained adapters."""
import json
import math
import os
import re
import time

from adapter_artifacts import validate_adapter_artifacts
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


def apply_batch_registration_intent(models_dir, dataset_id, adapter_id, source, settings):
    if get_batch_registration_intent(models_dir, dataset_id) is not None:
        raise ValueError('Batch registration already pending for ' + dataset_id)
    record = {'version': 1, 'dataset_id': dataset_id, 'adapter_id': adapter_id,
              'zip_source': os.path.abspath(source), 'source_sha256': get_file_sha256(source),
              'settings': dict(settings), 'created': time.time(), 'entry': None}
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
