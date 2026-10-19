"""Capture and publish word timing only for an unchanged saved Editor take."""
import hashlib
import json
from pathlib import Path
import tempfile

from book_state_transaction import ensure_book_state
from speech_text import get_speech_preparation
from utils import atomic_json_write, is_path_inside
from word_timing import MAX_AUDIO_BYTES, MAX_RESULT_BYTES, get_timing_fingerprint, render_word_timing, get_validated_word_spans


def read_bounded_file_hash(stream, limit):
    digest = hashlib.sha256()
    size = 0
    for block in iter(lambda: stream.read(1024**2), b''):
        size += len(block)
        if size > limit:
            raise ValueError('Timing input grew beyond its size limit.')
        digest.update(block)
    return digest.hexdigest()


def get_regular_file_hash(path, limit):
    path = Path(path)
    if not path.is_file() or path.stat().st_size > limit:
        raise ValueError('Timing input is missing or exceeds its size limit.')
    with path.open('rb') as stream:
        return read_bounded_file_hash(stream, limit)


def get_word_timing_audio_path(manager, row):
    path = Path(manager.get_chunk_audio_path(row.get('audio_path')))
    if not is_path_inside(str(path), manager.voicelines_dir):
        raise ValueError('Word timing requires generated audio inside voicelines.')
    return path


def open_verified_word_audio(manager, index, book_token, uid, revision, sha256, check_identity):
    import os
    import stat
    with ensure_book_state(manager.root_dir):
        check_identity(index, book_token, uid)
        rows = manager.load_chunks()
        if not 0 <= index < len(rows) or rows[index].get('audio_revision') != revision:
            raise ValueError('The timed audio take changed. Refresh Editor.')
        path = get_word_timing_audio_path(manager, rows[index])
        stream = path.open('rb')
        try:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_AUDIO_BYTES:
                raise ValueError('The timed audio is missing or exceeds its size limit.')
            if read_bounded_file_hash(stream, MAX_AUDIO_BYTES) != sha256:
                raise ValueError('The timed audio bytes changed. Request timing again.')
            check_identity(index, book_token, uid)
            if manager.load_chunks()[index].get('audio_revision') != revision:
                raise ValueError('The timed audio take changed. Refresh Editor.')
            stream.seek(0)
            return stream, info.st_size, path.suffix.lower()
        except BaseException:
            stream.close()
            raise


def get_provider_identity(runtime):
    files = []
    for key in ('binary', 'model'):
        path = Path(runtime[key]).resolve()
        info = path.stat()
        if not path.is_file() or not 0 < info.st_size <= (1024**3 if key == 'model' else 64 * 1024**2):
            raise ValueError('Word timing requires a cached model no larger than 1 GiB and a regular runtime executable.')
        files.append({'kind': key, 'path': str(path), 'size': info.st_size,
                      'mtime_ns': info.st_mtime_ns, 'device': info.st_dev, 'inode': info.st_ino})
    return files


def get_timing_inputs(manager, index, book_token, uid, revision, runtime, check_identity):
    check_identity(index, book_token, uid)
    rows = manager.load_chunks()
    if not 0 <= index < len(rows):
        raise ValueError('The selected Editor line no longer exists.')
    row = dict(rows[index])
    if row.get('audio_revision') != revision:
        raise ValueError('The selected audio take changed. Refresh Editor.')
    text = row.get('text', '')
    if not isinstance(text, str) or not text.strip() or len(text) > 20000:
        raise ValueError('Choose a nonempty, saved line of at most 20,000 characters.')
    path = get_word_timing_audio_path(manager, row)
    prepared = get_speech_preparation(text, row.get('instruct'))['text']
    binding = {'schema': 1, 'book_token': book_token, 'uid': uid, 'row': {
        key: row.get(key) for key in ('text', 'instruct', 'speaker', 'audio_path', 'audio_revision')},
        'prepared_text': prepared, 'audio_sha256': get_regular_file_hash(path, MAX_AUDIO_BYTES),
        'provider': get_provider_identity(runtime), 'language': 'en'}
    return binding, path


def get_timing_cache_path(manager, book_token, uid):
    key = get_timing_fingerprint({'book_token': book_token, 'uid': uid})
    return Path(manager.root_dir) / 'reports' / 'word_timing' / (key + '.json')


def get_cached_word_timing(path, binding):
    if not path.is_file():
        return None
    if path.stat().st_size > MAX_RESULT_BYTES:
        raise ValueError('Saved timing is oversized. Remove the private timing cache entry before retrying.')
    result = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(result, dict):
        raise ValueError('Saved timing is malformed.')
    if result.get('binding') != binding:
        return None
    # Validate saved spans as strictly as fresh provider output.
    display = result['display_text']
    if display != binding['prepared_text']:
        raise ValueError('Saved timing text does not match the selected take.')
    segments = [{'word': display[s['from']:s['to']], 'start': s['start'], 'end': s['end']}
                for s in result['spans']]
    verified = get_validated_word_spans(display, segments, result['duration'])
    if verified != result['spans']:
        raise ValueError('Saved word spans are invalid or ambiguous.')
    return result


def publish_word_timing(manager, index, request, runtime, check_identity, cancel_check):
    def capture_locked():
        return get_timing_inputs(manager, index, request.expected_book_token, request.expected_uid,
                                 request.expected_audio_revision, runtime, check_identity)
    if cancel_check():
        raise ValueError('Word timing cancelled.')
    with ensure_book_state(manager.root_dir):
        binding, path = capture_locked()
    cache = get_timing_cache_path(manager, request.expected_book_token, request.expected_uid)
    cached = get_cached_word_timing(cache, binding)
    if cached is not None:
        with ensure_book_state(manager.root_dir):
            current, _ = capture_locked()
            if cancel_check() or current != binding:
                raise ValueError('Timing inputs changed. Refresh Editor.')
            return {**cached, 'cached': True}
    with tempfile.TemporaryDirectory(prefix='.word-timing-', dir=manager.root_dir) as directory:
        snapshot = Path(directory) / 'input.audio'
        size = 0
        with path.open('rb') as source, snapshot.open('wb') as target:
            for block in iter(lambda: source.read(1024**2), b''):
                if cancel_check():
                    raise ValueError('Word timing cancelled.')
                size += len(block)
                if size > MAX_AUDIO_BYTES:
                    raise ValueError('Timing input grew beyond its size limit.')
                target.write(block)
        if get_regular_file_hash(snapshot, MAX_AUDIO_BYTES) != binding['audio_sha256']:
            raise ValueError('Audio changed while capturing the selected take.')
        result = render_word_timing(snapshot, runtime, binding['prepared_text'], cancel_check, directory)
        result = {**result, 'binding': binding, 'fingerprint': get_timing_fingerprint(binding)}
        if len(json.dumps(result, ensure_ascii=False).encode()) > MAX_RESULT_BYTES:
            raise ValueError('Word timing result exceeds its size limit.')
        with ensure_book_state(manager.root_dir):
            current, _ = capture_locked()
            if cancel_check() or current != binding:
                raise ValueError('The book, audio, pronunciation or timing provider changed. Refresh Editor.')
            cache.parent.mkdir(parents=True, exist_ok=True)
            atomic_json_write(result, str(cache))
            # At most one record per book/row, and a finite retained history across books.
            others = sorted((p for p in cache.parent.glob('*.json') if p != cache),
                            key=lambda p: p.stat().st_mtime, reverse=True)
            for old in others[127:]:
                old.unlink()
        return {**result, 'cached': False}
