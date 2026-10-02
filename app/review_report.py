"""Native report eligibility and atomic replacement of its optional summary."""
import hashlib
import json
import os
from pathlib import Path
import tempfile
from utils import file_lock

_METADATA_MARKER = "\n<!-- Alexandria review metadata "


def _save_report_text(path, content):
    fd, staged = tempfile.mkstemp(prefix='.review_report_', suffix='.md', dir=Path(path).parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(staged, path)
    finally:
        if os.path.exists(staged):
            os.unlink(staged)


def _get_report_digest(content, summary, incomplete):
    record = {'version':1, 'content':content, 'summary':summary, 'incomplete':incomplete}
    return hashlib.sha256(json.dumps(record, sort_keys=True).encode('utf-8')).hexdigest()


def save_review_report(path, content, summary, incomplete):
    """Publish deterministic Markdown and bind explanation eligibility to its bytes."""
    metadata = {'version':1, 'sha256':_get_report_digest(content, summary, bool(incomplete)),
                'summary':summary, 'incomplete':bool(incomplete)}
    encoded = json.dumps(metadata, ensure_ascii=True).replace('<', '\\u003c').replace('>', '\\u003e')
    _save_report_text(path, content+_METADATA_MARKER+encoded+" -->\n")


def get_review_report_info(path):
    """Read-only eligibility; edited, legacy or malformed reports are not admitted."""
    raw = Path(path).read_bytes()
    document = raw.decode('utf-8')
    content, marker, encoded = document.rpartition(_METADATA_MARKER)
    if not marker or not encoded.endswith(' -->\n'):
        raise ValueError('Report has no native explanation eligibility record')
    metadata = json.loads(encoded[:-5])
    if (not isinstance(metadata,dict) or type(metadata.get('version')) is not int
            or metadata['version'] != 1 or type(metadata.get('incomplete')) is not bool
            or not isinstance(metadata.get('summary'),str) or not metadata['summary']
            or metadata.get('sha256') != _get_report_digest(content, metadata['summary'], metadata['incomplete'])):
        raise ValueError('Report changed or has no matching explanation eligibility record')
    token = '\n## In Plain English\n\n'+metadata['summary']
    if content.count(token) != 1:
        raise ValueError('Report summary does not match its eligibility record')
    return {**metadata, 'content':content, 'summary_start':content.index(token)+len('\n## In Plain English\n\n')}


def apply_review_report_explanation(path, expected_digest, summary):
    """Replace only the admitted summary; changed reports retain their current bytes."""
    with file_lock(path):
        info = get_review_report_info(path)
        if info['incomplete'] or info['sha256'] != expected_digest:
            raise ValueError('Report changed or is incomplete; explanation was not saved')
        start = info['summary_start']
        content = info['content']
        updated = content[:start]+summary+content[start+len(info['summary']):]
        save_review_report(path, updated, summary, False)
