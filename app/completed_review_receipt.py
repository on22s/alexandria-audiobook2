"""Completed review evidence, separate from resumable checkpoints."""
import hashlib
import json
import os
from utils import atomic_json_write

VERSION = 1


def get_review_entries_fingerprint(entries):
    return hashlib.sha256(json.dumps(entries, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()


def get_review_sidecar_identity(path):
    if not path:
        return None
    identity = {'path': os.path.abspath(path)}
    try:
        with open(path, 'rb') as stream:
            identity['sha256'] = hashlib.sha256(stream.read()).hexdigest()
    except FileNotFoundError:
        identity['sha256'] = None
    return identity


def get_review_receipt_path(output_path):
    return output_path + '.review_completed.json'


def get_completed_review_match(input_path, output_path, input_sha256, fingerprint):
    """Return a reason on a cache miss; never change output or checkpoint."""
    path = get_review_receipt_path(output_path)
    try:
        with open(path, encoding='utf-8') as stream:
            receipt = json.load(stream)
        if (not isinstance(receipt, dict) or type(receipt.get('version')) is not int
                or receipt['version'] != VERSION):
            return False, 'unsupported completed-review receipt'
        if (receipt.get('input_path') != os.path.abspath(input_path)
                or receipt.get('output_path') != os.path.abspath(output_path)
                or receipt.get('fingerprint') != fingerprint):
            return False, 'completed-review settings changed'
        with open(output_path, encoding='utf-8') as stream:
            output = json.load(stream)
        if not isinstance(output, list) or not output:
            return False, 'completed-review output is invalid'
        output_sha = get_review_entries_fingerprint(output)
        if receipt.get('output_sha256') != output_sha:
            return False, 'completed-review output changed'
        expected = output_sha if os.path.abspath(input_path) == os.path.abspath(output_path) else receipt.get('input_sha256')
        if input_sha256 != expected:
            return False, 'completed-review input changed'
        if os.path.exists(output_path + '.review_checkpoint.json'):
            return False, 'an incomplete review checkpoint remains'
        return True, 'No changes since the completed review; use --force-review to rerun.'
    except (OSError, ValueError, TypeError) as error:
        return False, f'completed-review receipt unavailable: {type(error).__name__}'


def save_completed_review_receipt(input_path, output_path, input_sha256, output_sha256, fingerprint):
    atomic_json_write({'version': VERSION, 'input_path': os.path.abspath(input_path),
        'output_path': os.path.abspath(output_path), 'input_sha256': input_sha256,
        'output_sha256': output_sha256, 'fingerprint': fingerprint}, get_review_receipt_path(output_path))


def remove_completed_review_receipt(output_path):
    try:
        os.remove(get_review_receipt_path(output_path))
    except FileNotFoundError:
        pass
