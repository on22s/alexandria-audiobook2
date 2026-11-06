"""Publish confirmed DOCX originals and receipts before their selectable text."""
import json
import logging
import os
import sys
import uuid
import hashlib
import re
from pathlib import Path

from project_scope_files import capture_scope_file
from docx_preview import POLICY, MAX_ARCHIVE, MAX_TEXT

from source_upload_publication import create_source_upload_stage
from utils import secure_filename


class DocxStorageLimit(ValueError):
    """The complete original/text/receipt group does not fit the quota."""


def get_docx_group_names(path):
    name = Path(path).name
    if not re.fullmatch(r'.+-aa2docx-[0-9a-f]{32}\.txt', name):
        return None
    stem = name[:-4]
    return {'original': stem + '.docx', 'text': name,
            'receipt': stem + '.docx-receipt.json'}


def capture_docx_group(path, *, byte_limit=MAX_ARCHIVE + MAX_TEXT + 65536, cancel_check=None,
                      owning_root=None):
    """Validate receipt against bounded descriptor captures; return those same bytes.

    `owning_root` fixes the authority boundary: every group member is
    captured relative to that root. Without it the root is the source
    parent (existing callers' contract). Callers that must prove the
    group lives under a trusted directory pass the owning root, so a
    source parent substituted outside it cannot feed outside bytes.
    """
    names = get_docx_group_names(path)
    if names is None:
        return None
    capture_root = Path(path).parent if owning_root is None else Path(os.path.abspath(owning_root))
    parent_abs = Path(os.path.abspath(Path(path).parent))
    if owning_root is not None:
        try:
            parent_relative = parent_abs.relative_to(capture_root)
        except ValueError:
            raise ValueError('DOCX group is outside the owning root.')
        # No dot-part check here: both operands are abspath-normalized, so a
        # successful relative_to() can never yield '.'/'..'/'' parts (#1724 C081).
    proofs, payloads = {}, {}
    remaining = byte_limit
    for kind, limit in (('receipt', 65536), ('original', MAX_ARCHIVE), ('text', MAX_TEXT)):
        member_relative = Path(parent_relative, names[kind]).as_posix() if owning_root is not None else names[kind]
        proof, raw = capture_scope_file(capture_root, member_relative,
            limit=min(limit, max(1, remaining)), include_bytes=kind != 'original',
            cancel_check=cancel_check)
        if not proof['exists']:
            raise ValueError('DOCX group member is missing: ' + kind)
        remaining -= proof['size']
        if remaining < 0:
            raise ValueError('DOCX group exceeds byte limit')
        proofs[kind], payloads[kind] = proof, raw
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('Duplicate DOCX receipt field')
            result[key] = value
        return result
    receipt = json.loads(payloads['receipt'], object_pairs_hook=unique)
    fields = {'policy', 'policy_text', 'original_sha256', 'text_sha256', 'warnings',
              'preview_digest', 'version', 'filenames', 'uploaded_filename', 'confirmed'}
    if (not isinstance(receipt, dict) or set(receipt) != fields
            or type(receipt['version']) is not int or receipt['version'] != 1
            or receipt['confirmed'] is not True or receipt['policy'] != POLICY
            or receipt['filenames'] != names
            or not isinstance(receipt['uploaded_filename'], str)
            or not receipt['uploaded_filename'].lower().endswith('.docx')
            or not isinstance(receipt['policy_text'], str)
            or not isinstance(receipt['warnings'], list)
            or any(not isinstance(item, str) for item in receipt['warnings'])
            or receipt['original_sha256'] != proofs['original']['sha256']
            or receipt['text_sha256'] != proofs['text']['sha256']):
        raise ValueError('Invalid DOCX receipt or source digest')
    preview = {key: receipt[key] for key in (
        'policy', 'policy_text', 'original_sha256', 'text_sha256', 'warnings')}
    digest = hashlib.sha256(json.dumps(preview, sort_keys=True, ensure_ascii=False,
        separators=(',', ':')).encode('utf-8')).hexdigest()
    if receipt['preview_digest'] != digest:
        raise ValueError('DOCX preview digest mismatch')
    # Surface the receipt's verified imported identity (DS3007): the
    # receipt was validated above against the captured proofs, so its
    # uploaded_filename is trustworthy provenance, not a guess.
    return {'kind': 'docx', 'proofs': proofs, 'text_bytes': payloads['text'],
            'uploaded_filename': receipt['uploaded_filename']}


def publish_docx_upload(original, preview, filename, *, remaining):
    """Retain all published material on errors, including unknown native outcomes."""
    title = secure_filename(os.path.splitext(filename)[0])
    title = secure_filename(title.encode('utf-8')[:80].decode('utf-8', errors='ignore')) or 'docx'
    stem = title + '-aa2docx-' + uuid.uuid4().hex
    names = {'original': stem + '.docx', 'text': stem + '.txt',
             'receipt': stem + '.docx-receipt.json'}
    receipt = {key: value for key, value in preview.items() if key != 'text'}
    receipt.update(version=1, filenames=names, uploaded_filename=filename, confirmed=True)
    receipt_bytes = json.dumps(receipt, ensure_ascii=False, sort_keys=True).encode('utf-8')
    text_bytes = preview['text'].encode('utf-8')
    if len(receipt_bytes) + len(text_bytes) > remaining:
        raise DocxStorageLimit('DOCX original, text and receipt exceed upload storage limits')
    directory = original.directory
    root_identity = original._root_identity
    stages = []
    try:
        original.seal()
        original.publish(names['original'])
        original.sync_directory()
        original.close()
        path = None
        for payload, name in ((receipt_bytes, names['receipt']), (text_bytes, names['text'])):
            stage = create_source_upload_stage(directory, limit=len(payload))
            stages.append(stage)
            if stage._root_identity != root_identity:
                raise ValueError('DOCX upload storage changed between publications')
            stage.write(payload)
            stage.seal()
            path = stage.publish(name)
            stage.sync_directory()
            stage.close()
        return path, names
    finally:
        active_error = sys.exc_info()[1]
        errors = []
        for stage in reversed(stages):
            try:
                stage.close()
            except BaseException as error:
                errors.append(error)
        if errors and active_error is None:
            raise errors[0]
        if errors:
            logging.getLogger('AlexandriaUI').warning('DOCX stage cleanup also failed: %s', type(errors[0]).__name__)
