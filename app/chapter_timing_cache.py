"""Content-bound, disposable clip timing facts; never a waveform replacement."""
from dataclasses import dataclass
import hashlib
import importlib.metadata
import json
import marshal
import os
from pathlib import Path
import shutil
import stat
import types

from pydub import AudioSegment
from audio_pcm_producer import PCMSourceRange, get_pcm_block_bytes, get_pcm_source_shape
import tts
from utils import atomic_json_write, file_lock


MAX_CACHE_BYTES = 32 * 1024**2
CACHE_FILENAME = '.chapter-timing-facts.json'


def _version(stat):
    return [stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns]


def read_source_identity(path, cancel_check):
    """Hash compressed input in finite blocks, including restored-mtime edits."""
    tts.ensure_audio_export_active(cancel_check)
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_NONBLOCK', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            admitted = os.fstat(stream.fileno())
            if not stat.S_ISREG(admitted.st_mode):
                return None
            before = _version(admitted)
            digest = hashlib.sha256()
            for block in iter(lambda: stream.read(65536), b''):
                tts.ensure_audio_export_active(cancel_check)
                digest.update(block)
            after = _version(os.fstat(stream.fileno()))
        current = _version(os.stat(path))
    except OSError:
        return None
    if before != after or after != current:
        raise ValueError('Audio changed while indexing chapter timing. Retry the export.')
    return {'version': after, 'sha256': digest.hexdigest()}


def _policy(decoder_codes):
    functions = (tts.trim_edge_silence, tts._silent_edge_ms, get_pcm_source_shape)
    codes = tuple(getattr(function, '__code__', None) for function in functions)
    if not all(isinstance(code, types.CodeType) for code in codes + decoder_codes):
        return None
    defaults = getattr(tts.trim_edge_silence, '__defaults__', None)
    if not isinstance(defaults, tuple) or len(defaults) < 3:
        return None
    converter = shutil.which(AudioSegment.converter)
    try:
        converter_version = _version(os.stat(converter)) if converter else None
    except OSError:
        converter_version = None
    return {'schema': 1, 'code': hashlib.sha256(marshal.dumps(codes + decoder_codes)).hexdigest(),
            'trim_defaults': list(defaults[:3]),
            'edge_step_ms': tts._EDGE_SILENCE_STEP_MS,
            'pydub': importlib.metadata.version('pydub'),
            'converter': converter, 'converter_version': converter_version}


def _digest(body):
    return hashlib.sha256(json.dumps(body, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def _load_records(path, policy, byte_limit):
    try:
        with open(path, 'rb') as stream:
            size = os.fstat(stream.fileno()).st_size
            if size > byte_limit:
                return {}
            raw = stream.read(size + 1)
        if len(raw) > size:
            return {}
        document = json.loads(raw)
        body = document['body']
        if body['policy'] != policy or document['sha256'] != _digest(body):
            return {}
        return body['records'] if isinstance(body['records'], dict) else {}
    except (OSError, ValueError, TypeError, KeyError):
        return {}


def _valid_record(record, identity):
    if not isinstance(record, dict) or record.get('identity') != identity:
        return False
    if record.get('available') is False:
        # A failed decoder can also mean a transient worker/resource error.
        # Only absent/unreadable encoded input is a reusable negative fact;
        # existing bytes need another admission attempt before being omitted.
        return identity is None and record.get('shape') is None
    if record.get('available') is not True or identity is None:
        return False
    shape = record.get('shape')
    if not isinstance(shape, dict):
        return False
    layout, frames = shape.get('layout'), shape.get('frames')
    return (isinstance(layout, list) and len(layout) == 3
            and all(type(value) is int and value > 0 for value in layout)
            and layout[2] in (1, 2, 4) and type(frames) is int and frames >= 0)


def _source_fingerprint(chunks, resolve, identities, policy_sha256):
    inputs = [(identities.get(resolve(chunk['audio_path'])), chunk.get('speaker'),
               chunk.get('pause_after')) for chunk in chunks]
    return _digest([policy_sha256, inputs])


def get_chapter_source_fingerprinter(chunks, resolve, cancel_check, decoder_codes):
    """Read encoded identities for changed-only previews, without PCM decoding."""
    policy = _policy(decoder_codes)
    if policy is None:
        return None
    identities = {}
    for chunk in chunks:
        tts.ensure_audio_export_active(cancel_check)
        path = resolve(chunk['audio_path'])
        identities[path] = read_source_identity(path, cancel_check)
    digest = _digest(policy)
    return lambda selected: _source_fingerprint(selected, resolve, identities, digest)


@dataclass
class ChapterTimingFacts:
    pairs: list
    fresh_sources: dict
    identities: dict
    skipped: int
    policy_sha256: str
    cancel_check: object

    def get_source_fingerprint(self, chunks, resolve):
        return _source_fingerprint(chunks, resolve, self.identities, self.policy_sha256)

    def verify_sources(self):
        for path, identity in self.identities.items():
            tts.ensure_audio_export_active(self.cancel_check)
            if read_source_identity(path, self.cancel_check) != identity:
                raise ValueError('Audio changed after chapter timing was indexed. Retry the export.')


def ensure_chapter_timing_facts(root, chunks, resolve, decode, save_segment,
                                cancel_check, decoder_codes, progress_callback=None):
    """Index legacy clips once, retaining only metadata and owned disk ranges.

    Input hashes are checked on every use. Nonstandard segment providers retain
    their existing export path instead of being given fabricated PCM facts.
    """
    policy = _policy(decoder_codes)
    if policy is None or not chunks:
        return None
    cache_path = str(Path(root) / CACHE_FILENAME)
    # Keep reads bounded by this book's metadata, without imposing a new limit
    # on the number of clips an existing book may export.
    byte_limit = MAX_CACHE_BYTES
    for chunk in chunks:
        path = chunk.get('audio_path')
        try:
            path = resolve(path) if path else ''
        except ValueError:
            path = ''
        byte_limit += len(json.dumps(path).encode()) + 1024
    with file_lock(cache_path):
        previous = _load_records(cache_path, policy, byte_limit)
        records, fresh, identities, pairs = {}, {}, {}, []
        skipped = 0
        for position, chunk in enumerate(chunks):
            tts.ensure_audio_export_active(cancel_check)
            if progress_callback and position and position % 50 == 0:
                progress_callback(position, len(chunks))
            path = chunk.get('audio_path')
            try:
                path = resolve(path) if path else None
            except ValueError:
                path = None
            if path is None:
                skipped += 1
                continue
            identity = read_source_identity(path, cancel_check)
            if identity is None:
                records[path] = {'identity': None, 'available': False, 'shape': None}
                identities[path] = None
                skipped += 1
                continue
            record = records.get(path, previous.get(path))
            if not _valid_record(record, identity):
                segment = decode(chunk)
                if segment is None:
                    record = {'identity': identity, 'available': False, 'shape': None}
                else:
                    trimmed = tts.trim_edge_silence(segment, cancel_check=cancel_check)
                    shape = get_pcm_source_shape(trimmed)
                    if (shape is None or identity is None or type(segment) is not AudioSegment
                            or get_pcm_block_bytes(shape[0]) is None):
                        return None
                    layout, frames = shape
                    record = {'identity': identity, 'available': True,
                              'shape': {'layout': list(layout), 'frames': frames}}
                    fresh[path] = save_segment(segment)
                    del trimmed
                del segment
                if read_source_identity(path, cancel_check) != identity:
                    raise ValueError('Audio changed while indexing chapter timing. Retry the export.')
            records[path] = record
            identities[path] = identity
            if not record['available']:
                skipped += 1
                continue
            layout, frames = record['shape']['layout'], record['shape']['frames']
            # This range is metadata only. No reader is ever given its absent store.
            fact = PCMSourceRange(None, 0, frames * layout[0] * layout[2],
                                  *layout, round(1000 * frames / layout[1]))
            pairs.append((chunk, fact))
        result = ChapterTimingFacts(pairs, fresh, identities, skipped, _digest(policy), cancel_check)
        result.verify_sources()
        body = {'policy': policy, 'records': records}
        document = {'body': body, 'sha256': _digest(body)}
        if records != previous:
            if len(json.dumps(document).encode()) > byte_limit:
                raise ValueError('Chapter timing index exceeds its size limit.')
            atomic_json_write(document, cache_path)
        return result
