"""Authoritative run-spec receipt, published with book-state transactions.

The checkpoint/manifest pair is worker output and can be edited together. This
receipt is written once at run admission through the book-state transaction,
and recovery treats it as the only accepted source of run identity.
"""
import hashlib
import json
import math
import os
from pathlib import Path

RECEIPT_NAME = 'script_run_spec.json'
STATE_DIGEST = 'script_generation_receipt_sha256'
CHECKPOINT_DIGEST = 'run_spec_binding_sha256'
SCHEMA = 1


def receipt_path(data_dir):
    return os.path.join(str(data_dir), RECEIPT_NAME)


def _unique_pairs(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError('duplicate key in run-spec receipt')
        seen[key] = value
    return seen


def _finite_float(value):
    number = float(value)
    if not math.isfinite(number):
        raise ValueError('nonfinite number in run-spec receipt')
    return number


def load_receipt(data_dir):
    path = receipt_path(data_dir)
    try:
        raw = Path(path).read_bytes()
    except FileNotFoundError:
        return None
    return _load_bytes(raw)


def load_receipt_file(path):
    """Load and digest-verify a receipt from its exact admitted path."""
    return _load_bytes(Path(path).read_bytes())


def _load_bytes(raw):
    try:
        receipt = json.loads(raw.decode('utf-8'), object_pairs_hook=_unique_pairs,
                             parse_float=_finite_float,
                             parse_constant=lambda _: (_ for _ in ()).throw(ValueError('invalid constant')))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise ValueError('Run-spec receipt is invalid')
    if not isinstance(receipt, dict) or receipt.get('schema') != SCHEMA:
        raise ValueError('Run-spec receipt has an unsupported shape')
    expected = hashlib.sha256(json.dumps(
        {key: value for key, value in receipt.items() if key != 'sha256'},
        sort_keys=True, ensure_ascii=False, separators=(',', ':'),
        allow_nan=False).encode('utf-8')).hexdigest()
    if receipt.get('sha256') != expected:
        raise ValueError('Run-spec receipt digest does not match its contents')
    return receipt


def build_receipt(*, book_token, source_path, source_sha256, prepared_sha256,
                  options, settings, fingerprint, chunk_count, run_id, spec=None):
    body = {'schema': SCHEMA, 'book_token': book_token,
            'source_path_sha256': hashlib.sha256(source_path.encode('utf-8')).hexdigest(),
            'source_sha256': source_sha256, 'prepared_text_sha256': prepared_sha256,
            'options': options, 'settings': settings, 'fingerprint': fingerprint,
            'chunk_count': chunk_count, 'run_id': run_id, 'spec': spec or {}}
    return _sign_receipt(body)


def _sign_receipt(body):
    digest = hashlib.sha256(json.dumps(
        body, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
        allow_nan=False).encode('utf-8')).hexdigest()
    return dict(body, sha256=digest)


def rebind_attempt(receipt, run_id, *, prior_manifest_sha256=None,
                   legacy_checkpoint_sha256=None, pending_manifest_recovery=False,
                   manifest_at_start=None, start_over=False):
    """Issue a verified spec to a new worker claim without changing its inputs."""
    from three_pass_run_spec import spec_digest

    body = dict(_attempt_inputs(receipt), manifest_at_start=manifest_at_start)
    checkpoint_generation = run_id if start_over else receipt.get('checkpoint_generation')
    if checkpoint_generation is not None:
        body['checkpoint_generation'] = checkpoint_generation
    if prior_manifest_sha256 is not None:
        body['prior_manifest_sha256'] = prior_manifest_sha256
    if legacy_checkpoint_sha256 is not None:
        body['legacy_checkpoint_sha256'] = legacy_checkpoint_sha256
    if pending_manifest_recovery:
        body['pending_manifest_recovery'] = True
    spec = dict(receipt['spec'], run_id=run_id)
    body.update(run_id=run_id, spec=spec,
                settings=dict(receipt['settings'], digest=spec_digest(spec)))
    return _sign_receipt(body)


def _attempt_inputs(receipt):
    return {key: value for key, value in receipt.items()
            if key not in ('sha256', 'prior_manifest_sha256', 'legacy_checkpoint_sha256',
                           'pending_manifest_recovery', 'manifest_at_start', 'checkpoint_generation')}


def has_same_attempt_inputs(saved, recomputed):
    """Compare all admitted inputs independently of predecessor evidence."""
    return _attempt_inputs(saved) == _attempt_inputs(recomputed)


def get_checkpoint_spec_digest(receipt):
    """Bind complete inputs and the active selection across worker claims."""
    from three_pass_run_spec import spec_digest

    binding = {
        'spec': {key: value for key, value in receipt['spec'].items() if key != 'run_id'},
        'prepared_text_sha256': receipt['prepared_text_sha256'],
    }
    if 'checkpoint_generation' in receipt:
        binding['checkpoint_generation'] = receipt['checkpoint_generation']
    return spec_digest(binding)


def receipt_bytes(receipt):
    return json.dumps(receipt, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')


def verify_receipt(receipt, *, book_token, source_path, source_sha256,
                   prepared_sha256, options, fingerprint, chunk_count, spec=None):
    """Every independently recomputable field must match the stored receipt."""
    if not isinstance(receipt, dict) or receipt.get('schema') != SCHEMA:
        return False
    expected = hashlib.sha256(json.dumps(
        {key: value for key, value in receipt.items() if key != 'sha256'},
        sort_keys=True, ensure_ascii=False, separators=(',', ':'),
        allow_nan=False).encode('utf-8')).hexdigest()
    if receipt.get('sha256') != expected:
        return False
    checks = (
        receipt.get('book_token') == book_token,
        receipt.get('source_path_sha256') == hashlib.sha256(
            source_path.encode('utf-8')).hexdigest(),
        receipt.get('source_sha256') == source_sha256,
        receipt.get('prepared_text_sha256') == prepared_sha256,
        receipt.get('options') == options,
        receipt.get('fingerprint') == fingerprint,
        receipt.get('chunk_count') == chunk_count,
        receipt.get('spec') == (spec or {}),
    )
    return all(checks)
