"""Explicit source cover materialization, with immutable export dependencies."""
import base64
from export_staging import create_export_staging
from contextlib import contextmanager, nullcontext
import hashlib
from pathlib import Path

from book_state_transaction import ensure_book_state, get_book_operation_token, is_book_state_admitted
from m4b_cover import get_validated_cover_bytes
from project_scope_files import capture_scope_file
from publication_preview import get_publication_review, get_publication_selection
from tts import ensure_audio_export_active


def _get_identity(facts):
    return {'version': facts['file_version'], 'sha256': facts['sha256']} if facts['exists'] else None


def _ensure_cover_book(root):
    # Mixed candidate admission already holds this non-reentrant file lock.
    # Reuse only the same current thread/context's native book admission.
    return nullcontext() if is_book_state_admitted(root) else ensure_book_state(root,recover=False)


def capture_m4b_publication_cover(root_dir, cover_path, source_cover=None,
                                  expected_book_token=None, cancel_check=None):
    """Resolve native cover precedence and provenance without writing image files.

    Source bytes are bounded and validated by the existing publication review
    and cover decoder. Callers own any later materialization and its quota.
    Uploaded covers retain the native path behavior; they are never replaced.
    """
    expected_sources = {}
    ensure_audio_export_active(cancel_check)
    if expected_book_token is not None:
        with _ensure_cover_book(root_dir):
            if get_book_operation_token(root_dir) != expected_book_token:
                raise ValueError('Selected book changed before M4B export')
            facts, _ = capture_scope_file(root_dir, 'state.json')
            expected_sources[str(Path(root_dir) / 'state.json')] = _get_identity(facts)
    if source_cover is None or cover_path:
        return {'cover_path':cover_path,'expected_sources':expected_sources,
                'cover_bytes':None,'media_type':None,'cover_sha256':None}
    with _ensure_cover_book(root_dir):
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
    return {'cover_path':None,'expected_sources':expected_sources,
            'cover_bytes':raw,'media_type':cover['media_type'],'cover_sha256':cover['sha256']}


@contextmanager
def ensure_m4b_publication(root_dir, cover_path, source_cover=None, expected_book_token=None, cancel_check=None):
    """Never replace an uploaded asset; temporary files belong only to this export."""
    captured = capture_m4b_publication_cover(root_dir,cover_path,source_cover,
                                           expected_book_token,cancel_check)
    if captured['cover_bytes'] is None:
        yield {'cover_path':captured['cover_path'],'expected_sources':captured['expected_sources']}
        return
    with create_export_staging(root_dir, 'cover') as staging:
        directory = staging.path
        path = directory / ('cover.png' if captured['media_type'] == 'image/png' else 'cover.jpg')
        path.write_bytes(captured['cover_bytes'])
        ensure_audio_export_active(cancel_check)
        yield {'cover_path': str(path), 'expected_sources': captured['expected_sources'],
               'cover_sha256': captured['cover_sha256']}
