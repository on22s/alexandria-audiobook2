"""One-chunk ASR timing. A timing match is never a pronunciation verdict."""
from collections import Counter
import difflib
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import time
import wave

from subprocess_ownership import start_owned_subprocess, stop_owned_subprocess
from whisper_cpp_runtime import get_coalesced_whisper_cpp_segments

MAX_AUDIO_BYTES = 64 * 1024**2
MAX_SECONDS = 120
MAX_RESULT_BYTES = 1024**2
WORDS = re.compile(r"\w+(?:['’]\w+)*", re.UNICODE)


def get_word_tokens(text):
    return [(m.group().casefold().replace('’', "'"), m.start(), m.end()) for m in WORDS.finditer(text)]


def get_timing_fingerprint(binding):
    return hashlib.sha256(json.dumps(binding, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def get_validated_word_spans(text, segments, duration):
    if (not isinstance(segments, list) or len(segments) > 4096
            or not isinstance(duration, (int, float)) or not math.isfinite(duration)
            or not 0 < duration <= MAX_SECONDS):
        raise ValueError('Invalid timing result or audio duration.')
    parsed = []
    previous = 0.0
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get('word'), str):
            raise ValueError('Malformed ASR word.')
        start, end = segment.get('start'), segment.get('end')
        if (any(type(v) not in (int, float) or not math.isfinite(v) for v in (start, end))
                or not 0 <= start < end <= duration or start < previous):
            raise ValueError('Invalid, overlapping or saturated ASR timestamps.')
        previous = end
        words = get_word_tokens(segment['word'])
        # A coalesced native span is kept as one group, never given invented sub-word times.
        if words:
            parsed.append((tuple(w[0] for w in words), float(start), float(end)))
    expected = get_word_tokens(text)
    flat = [word for words, _, _ in parsed for word in words]
    wanted = [word for word, _, _ in expected]
    matches = {}
    for block in difflib.SequenceMatcher(None, wanted, flat, autojunk=False).get_matching_blocks():
        for offset in range(block.size):
            matches[block.b + offset] = block.a + offset
    wc, rc = Counter(wanted), Counter(flat)
    spans = []
    offset = 0
    for words, start, end in parsed:
        positions = [matches.get(offset + i) for i in range(len(words))]
        if (all(p is not None for p in positions)
                and positions == list(range(positions[0], positions[0] + len(words)))
                and all(wc[w] == rc[w] == 1 for w in words)):
            spans.append({'from': expected[positions[0]][1], 'to': expected[positions[-1]][2],
                          'start': start, 'end': end})
        offset += len(words)
    # Repeated endpoints across several spans are a failed instrument, not perfect matches.
    if len(spans) > 1 and len({(s['start'], s['end']) for s in spans}) != len(spans):
        raise ValueError('ASR timestamp saturation.')
    return spans


def run_timing_command(command, cancel_check, timeout=180, *, task_lease_fd=None):
    if cancel_check():
        raise ValueError('Word timing cancelled.')
    process = None
    started = time.monotonic()
    # Provider diagnostics aren't consumed here. Do not spool unlimited native
    # stderr into an anonymous file while a provider is failing noisily.
    try:
        options = {} if task_lease_fd is None else {'task_lease_fd': task_lease_fd, 'umask': 0o077}
        process = start_owned_subprocess(command, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.DEVNULL, start_new_session=True, **options)
        while True:
            if cancel_check():
                raise ValueError('Word timing cancelled.')
            if time.monotonic() - started >= timeout:
                raise ValueError('Word timing timed out.')
            try:
                status = process.wait(timeout=.1)
                break
            except subprocess.TimeoutExpired:
                continue
        if cancel_check():
            raise ValueError('Word timing cancelled.')
        if status:
            raise ValueError(f'The word-timing provider failed with exit code {status}.')
    finally:
        if process is not None:
            try:
                if process.poll() is None:
                    stop_owned_subprocess(process, timeout=1)
            finally:
                control = getattr(process, '_alexandria_control', None)
                if control is not None:
                    control.close()


def decode_asr_audio(audio_path, cancel_check, directory, *, command=None, decoder='ffmpeg'):
    """Decode a bounded capture, rejecting excess duration instead of timing a prefix."""
    command = command or run_timing_command
    decoded = Path(directory) / 'audio.wav'
    _ensure_asr_output_absent(directory, 'audio.wav')
    command([decoder, '-nostdin', '-v', 'error', '-y', '-i', str(audio_path),
                        '-map', '0:a:0', '-vn', '-t', str(MAX_SECONDS + 1),
                        '-ar', '16000', '-ac', '1', '-c:a', 'pcm_s16le', str(decoded)], cancel_check, 60)
    with wave.open(str(decoded)) as stream:
        duration = stream.getnframes() / stream.getframerate()
    if not 0 < duration <= MAX_SECONDS:
        raise ValueError('Word timing supports one nonempty chunk of at most 120 seconds.')
    return decoded, duration


def read_asr_payload(path):
    """Read native JSON with a byte bound even if the output grows during read."""
    with Path(path).open('rb') as stream:
        data = stream.read(MAX_RESULT_BYTES + 1)
    if len(data) > MAX_RESULT_BYTES:
        raise ValueError('ASR timing output exceeds its size limit.')
    payload = json.loads(data.decode('utf-8'))
    if not isinstance(payload, dict):
        raise ValueError('Malformed ASR result.')
    return payload


def _ensure_asr_output_absent(directory, name):
    prepare = getattr(directory, 'ensure_output_absent', None)
    if prepare is not None:
        prepare(name)
    else:
        (Path(directory) / name).unlink(missing_ok=True)


def get_asr_transcript(payload):
    """Keep all ordered native text, including repeats, independent of timestamps."""
    rows = payload.get('transcription')
    if not isinstance(rows, list) or len(rows) > 4096:
        raise ValueError('Malformed or oversized ASR transcription.')
    texts, size = [], 0
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get('text'), str):
            raise ValueError('Malformed ASR transcription text.')
        text = row['text'].strip()
        size += len(text) + bool(texts)
        if size > 20000 or any(0xD800 <= ord(char) <= 0xDFFF for char in text):
            raise ValueError('ASR transcript exceeds its text bound or contains invalid Unicode.')
        texts.append(text)
    return ' '.join(texts)


def transcribe_asr_audio(decoded, runtime, cancel_check, directory, *, command=None):
    """One finite cached English CPU invocation shared by timing and content QA."""
    command = command or run_timing_command
    import psutil
    minimum = max(2 * 1024**3, Path(runtime['model']).stat().st_size * 4 + 512 * 1024**2)
    if psutil.virtual_memory().available < minimum:
        raise ValueError('Insufficient available system RAM for the cached word-timing model.')
    prefix = Path(directory) / 'asr'
    _ensure_asr_output_absent(directory, 'asr.json')
    command([runtime['binary'], '--model', runtime['model'], '--file', str(decoded),
             '--output-file', str(prefix), *get_asr_options()], cancel_check)
    payload = read_asr_payload(prefix.with_suffix('.json'))
    # A malformed text instrument is unavailable even if timestamps look valid.
    get_asr_transcript(payload)
    return payload


def get_asr_options():
    return ['--language', 'en', '--max-len', '1', '--split-on-word',
            '--output-json-full', '--no-prints', '--no-gpu', '--threads', '2']


def render_word_timing(audio_path, runtime, text, cancel_check, directory):
    """Decode a finite prefix, refuse excess duration, then run a cached CPU provider."""
    command = getattr(directory, 'run', None)
    decoded, duration = decode_asr_audio(audio_path, cancel_check, directory, command=command)
    payload = transcribe_asr_audio(decoded, runtime, cancel_check, directory, command=command)
    segments = get_coalesced_whisper_cpp_segments(payload.get('transcription'))
    spans = get_validated_word_spans(text, segments, duration)
    return {'display_text': text, 'spans': spans, 'duration': duration,
            'provider': 'whisper.cpp (English, CPU)', 'language': 'en',
            'notice': 'Estimated ASR timing only. Untimed or repeated words need listening review; this does not certify pronunciation.'}

from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_verification_code
_LOADED_SPEECH_VERIFICATION_CODE_SHA256 = _get_loaded_verification_code()
