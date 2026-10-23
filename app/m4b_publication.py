"""Explicit source cover materialization, with immutable export dependencies."""
import base64
from contextlib import contextmanager
import hashlib
from pathlib import Path
import tempfile

from book_state_transaction import ensure_book_state, get_book_operation_token
from m4b_cover import get_validated_cover_bytes
from project_scope_files import capture_scope_file
from publication_preview import get_publication_review, get_publication_selection
from tts import ensure_audio_export_active


def _get_identity(facts):
    return {'version': facts['file_version'], 'sha256': facts['sha256']} if facts['exists'] else None


@contextmanager
def ensure_m4b_publication(root_dir, cover_path, source_cover=None, expected_book_token=None, cancel_check=None):
    """Never replace an uploaded asset; temporary files belong only to this export."""
    expected_sources = {}
    ensure_audio_export_active(cancel_check)
    if expected_book_token is not None:
        with ensure_book_state(root_dir, recover=False):
            if get_book_operation_token(root_dir) != expected_book_token:
                raise ValueError('Selected book changed before M4B export')
            facts, _ = capture_scope_file(root_dir, 'state.json')
            expected_sources[str(Path(root_dir) / 'state.json')] = _get_identity(facts)
    if source_cover is None or cover_path:
        yield {'cover_path': cover_path, 'expected_sources': expected_sources}
        return
    with ensure_book_state(root_dir, recover=False):
        review = get_publication_review(root_dir)
        if expected_book_token is not None and review['book_token'] != expected_book_token:
            raise ValueError('Selected book changed before source-cover admission')
        selected = get_publication_selection(review, {**source_cover, 'select_cover': True})
        captured = review['captured']
        row = next(row for row in captured['document']['candidates'] if row['id'] == selected['origin_id'])
        cover = row['cover']
        for root, facts in ((root_dir, review['state']), (root_dir, review['uploaded']),
                            (captured['root'], captured['source']), (captured['root'], captured['companion'])):
            expected_sources[str(Path(root) / facts['path'])] = _get_identity(facts)
    ensure_audio_export_active(cancel_check)
    raw = base64.b64decode(cover['data_base64'], validate=True)
    if hashlib.sha256(raw).hexdigest() != cover['sha256']:
        raise ValueError('Stored source cover changed')
    raw = get_validated_cover_bytes(raw, cover['media_type'])
    ensure_audio_export_active(cancel_check)
    with tempfile.TemporaryDirectory(prefix='.m4b-source-cover-', dir=root_dir) as directory:
        path = Path(directory) / ('cover.png' if cover['media_type'] == 'image/png' else 'cover.jpg')
        path.write_bytes(raw)
        ensure_audio_export_active(cancel_check)
        yield {'cover_path': str(path), 'expected_sources': expected_sources,
               'cover_sha256': cover['sha256']}
