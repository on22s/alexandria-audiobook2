"""Read artifact-bound evidence that single-pass book generation finished."""
import hashlib
import json
from pathlib import Path
from source_encoding import get_decoded_source_text, get_normalized_source_newlines


def get_file_sha256(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def get_generation_input(path, *, owning_root=None):
    """Read source text and its raw-byte identity from one snapshot.

    The snapshot is the SAME group-aware confined capture the
    completion/publication re-verification uses (_read_source_snapshot):
    a TXT whose basename matches the DOCX group pattern therefore
    binds only as a complete verified DOCX group (receipt + original
    proofs), and an incomplete group refuses here, before generation,
    instead of binding raw bytes that later verification refuses.
    `owning_root` confines the read where the HTTP generation context
    knows the book data directory; explicit CLI sources keep their
    source-parent authority and remain supported in ordinary dirs.
    """
    from script_selection_binding import _read_source_snapshot
    digest, data = _read_source_snapshot(path, include_bytes=True,
                                         owning_root=owning_root,
                                         missing_as_fnf=True)
    if data is None:
        raise ValueError('Selected source is unavailable')
    text, _encoding = get_decoded_source_text(data)
    text = get_normalized_source_newlines(text)
    return text, digest


def get_generation_completion_error(output_path, input_path):
    try:
        output = Path(output_path)
        manifest = json.loads(Path(str(output) + '.generation_quality.json').read_text(encoding='utf-8'))
        output_bytes = output.read_bytes()
        entries = json.loads(output_bytes.decode('utf-8'))
        if not isinstance(entries, list) or not entries or any(not isinstance(row, dict) for row in entries):
            return 'Output is not a nonempty script entry list'
        if not isinstance(manifest, dict) or manifest.get('status') != 'complete':
            return 'Generation is not marked complete'
        total = manifest.get('total_chunks')
        accepted = manifest.get('accepted_chunk_count')
        chunks = manifest.get('chunks')
        fingerprint = manifest.get('fingerprint')
        if (type(total) is not int or total < 1 or type(accepted) is not int
                or accepted != total or not isinstance(chunks, list) or len(chunks) != total
                or not isinstance(fingerprint, dict)):
            return 'Generation chunk counts are incomplete or malformed'
        hashes = fingerprint.get('chunk_sha256')
        if not isinstance(hashes, list) or len(hashes) != total:
            return 'Generation source chunk evidence is incomplete'
        for number, row in enumerate(chunks, 1):
            if (not isinstance(row, dict) or type(row.get('chunk_number')) is not int
                    or row['chunk_number'] != number
                    or not isinstance(hashes[number - 1], str)
                    or len(hashes[number - 1]) != 64
                    or any(char not in '0123456789abcdef' for char in hashes[number - 1])
                    or row.get('source_sha256') != hashes[number - 1]):
                return 'Generation chunks are not a complete ordered source sequence'
        artifact = manifest.get('completion_artifact')
        if (not isinstance(artifact, dict) or type(artifact.get('version')) is not int
                or artifact['version'] != 1):
            return 'Generation has no bound completion artifact'
        # Input identity re-verified through the same confined digest
        # seam that bound it (script_selection_binding._source_digest):
        # an aliased/hardlinked source refuses here exactly as the
        # binding seam refuses it, instead of passing a plain hash of
        # identical bytes through a different admission policy.
        from script_selection_binding import _source_digest
        if artifact.get('input_sha256') != _source_digest(input_path):
            return 'Generation source file changed'
        if artifact.get('output_sha256') != hashlib.sha256(output_bytes).hexdigest():
            return 'Generated output changed after completion'
    except (OSError, ValueError, TypeError) as error:
        return 'Cannot verify generation completion: ' + str(error)
    return None
