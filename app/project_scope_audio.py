"""Read-only retained selected-audio facts for scope planning."""
import os
from pathlib import Path
from audio_take_history import _check_cancelled
from project_scope_files import capture_scope_file
from take_publication import _read_audio, validate_take_publication


def get_scope_audio_plan(root, chunks, chunk_start, *, cancel_check=None):
    """Bind retained selected files without rewriting rows or origin receipts.

    These facts establish current bounded artifact integrity. They do not
    certify historical generation inputs, speech content or history payloads.
    """
    root = Path(os.path.abspath(root))
    if (not isinstance(chunks, list) or not chunks
            or any(not isinstance(row, dict) for row in chunks)
            or type(chunk_start) is not int or not 0 < chunk_start < len(chunks)):
        raise ValueError('An explicit retained chunk boundary is required.')
    facts = []
    for index in range(chunk_start, len(chunks)):
        _check_cancelled(cancel_check)
        row = chunks[index]
        publication = validate_take_publication(row)
        path = row.get('audio_path')
        fact = {'uid': row.get('uid'), 'old_index': index, 'new_index': index - chunk_start,
                'audio_revision': row.get('audio_revision'), 'audio': None,
                'origin_publication_present': publication is not None}
        if row.get('status') == 'generating':
            raise ValueError('A retained chunk is still generating audio.')
        if path is None:
            if row.get('status') == 'done' or publication is not None:
                raise ValueError('A retained completed chunk has no selected audio.')
        else:
            # Apply the same strict path confinement and alias policy as other
            # scope inputs before the shared decoder is allowed to open it.
            proof, _ = capture_scope_file(root, path, cancel_check=cancel_check)
            if not proof['exists']:
                raise ValueError('Retained selected audio is missing.')
            audio, version = _read_audio(root, root/path, cancel_check)
            if (list(version) != proof['file_version'] or audio['sha256'] != proof['sha256']
                    or audio['size'] != proof['size']):
                raise ValueError('Retained selected audio changed during scope preview.')
            if publication is not None and publication['audio'] != audio:
                raise ValueError('Retained selected audio differs from its publication evidence.')
            fact['audio'] = {**proof, **audio}
        facts.append(fact)
    return facts
