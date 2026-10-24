"""Bind explicit cover consent to native selected bytes and source provenance."""
import hashlib
from pathlib import Path
from m4b_cover import MAX_COVER_BYTES, get_validated_cover_bytes
from m4b_publication import capture_m4b_publication_cover
from project_scope_files import capture_scope_file

UPLOADED_COVER = 'm4b_cover.jpg'


def capture_sound_delivery_cover(root, delivery, *, cancel_check=None):
    expected = delivery.get('m4b',{}).get('cover_sha256')
    if delivery['format'] != 'm4b' or expected is None:
        return {'data':None,'expected_sources':{}}
    root = Path(root).absolute()
    selection = delivery['m4b'].get('cover_source')
    # A mixed request explicitly naming a source must validate that source and
    # revision. Passing the uploaded path would take the native legacy shortcut.
    uploaded_path = str(root/UPLOADED_COVER) if selection is None else None
    captured = capture_m4b_publication_cover(root,uploaded_path,selection,
        selection['expected_book_token'] if selection is not None else None,cancel_check)
    sources = dict(captured['expected_sources'])
    raw = captured['cover_bytes']
    if raw is None:
        facts, raw = capture_scope_file(root,UPLOADED_COVER,limit=MAX_COVER_BYTES,
                                        include_bytes=True,cancel_check=cancel_check)
        sources[str(root/UPLOADED_COVER)] = ({'version':facts['file_version'],'sha256':facts['sha256']}
                                            if facts['exists'] else None)
    if raw is None or hashlib.sha256(raw).hexdigest() != expected:
        raise ValueError('Approved M4B cover changed; review and approve again')
    media_type = 'image/png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else 'image/jpeg'
    return {'data':get_validated_cover_bytes(raw,media_type),'expected_sources':sources}


def get_sound_delivery_cover(root, delivery, *, cancel_check=None):
    return capture_sound_delivery_cover(root,delivery,cancel_check=cancel_check)['data']
