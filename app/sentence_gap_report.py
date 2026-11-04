"""Report-only analysis of sentence gaps INSIDE one rendered take.

This module never mutates audio, never synthesizes, never runs ASR, and never
authorizes an insertion/removal/crop by itself. ASR timestamps alone never
authorize an insertion, speech removal or phoneme crop: a proposal below only
exists when (a) text segmentation gives a certain sentence boundary, (b) both
neighbouring words are timed and match the authored text, (c) provenance binds
the exact text/audio bytes, and (d) a channel-aware acoustic measurement finds
a quiet interval strictly between words. Anything else is reported as skipped
with an explicit reason.

Word-timing evidence (no bare-span trust)
-----------------------------------------
Bare ``{from,to,start,end}`` offset lists do NOT prove transcript or timing
provenance, so this module does not accept them. Callers supply a word-timing
evidence envelope::

    {
      "display_text": <prepared text>,
      "segments": [{"word": str, "start": float, "end": float}, ...],
      "spans": [{"from": int, "to": int, "start": float, "end": float}, ...],
      "duration": float, "provider": str, "language": "en",
      "binding": {"audio_sha256": <pcm digest>, "text_sha256": <text digest>,
                  "provider": str, "language": "en", "payload_sha256": <payload digest>},
      "fingerprint": <word_timing.get_timing_fingerprint(binding)>
    }

``segments`` are the originally observed ASR words (as heard, before any
validated-span filtering); ``spans`` must be exactly what
``word_timing.get_validated_word_spans(display_text, segments, duration)``
returns. The envelope is accepted only when ``display_text`` equals the
analysed text, the binding audio/text hashes match the actual PCM/text being
analysed, provider/language agree with provenance, the fingerprint matches
the binding including its payload digest, and the duration is consistent with the audio. The CLI fails
closed on a bare list or an incomplete/unsupported cache (for example an
Editor cache without original ``segments``) instead of relabelling stripped
spans with the current take's provenance. No ASR is executed here and no
``word`` is ever synthesized from authored offsets to claim an observed
transcript.

Existing between-take gap suggestions live in ``dialogue_continuity`` and are
not this issue. Pause/timeline accounting for any *future, separately
approved* application must reuse ``tts.get_pause_duration_ms`` /
``tts.iter_timeline`` with ``export_limits.MAX_PAUSE_MS``: the derived artifact
is a new playback/export file, samples outside affected regions are preserved,
and the original take bytes are never rewritten. No such application exists
here; the coordinate maps below only define the mapping such an application
would have to honour.

Coordinate maps
---------------
``GapCoordinateMap`` maps original -> derived times for insertions (pause
extensions) at fixed original times. Identity (no insertions) is a no-op.
``compose_coordinate_maps`` applies one map then another. All maps validate
bounds: times outside ``[0, duration]`` raise ``ValueError``.
"""
import argparse
import hashlib
import json
import math
import os
import re
import sys
import tempfile
import time
from bisect import bisect_left, bisect_right
from numbers import Integral, Real
from pathlib import Path

from word_timing import MAX_SECONDS, MAX_AUDIO_BYTES

MAX_TEXT_CHARS = 20000
MAX_WORD_SPANS = 2048

# Independent decoded allocation budget, checked before the CLI reads samples.
MAX_DECODED_BYTES = 128 * 1024**2
MAX_REPORT_BYTES = 256 * 1024
MAX_JSON_BYTES = 1024**2
MIN_QUIET_SECONDS = 0.12
EDGE_MARGIN_SECONDS = 0.03
QUIET_DBFS = -45.0
FRAME_SECONDS = 0.01
_HEX64 = re.compile(r'[0-9a-f]{64}')
_TERMINAL = '.!?。！？…'
_CLOSING = '"\'”’»」』)）]】'
# 20k Unicode characters encode to at most 4 UTF-8 bytes each.
MAX_TEXT_BYTES = 4 * MAX_TEXT_CHARS
# PCM RMS chunking: frames per chunk keeps the squared temp small even at
# the 384 kHz sample-rate cap (256 * 3840 * 2 * 8 bytes ~= 15.7 MiB max).
_RMS_CHUNK_FRAMES = 256


def _finite(value):
    return isinstance(value, Real) and not isinstance(value, bool) and math.isfinite(value)


def _check_cancel(cancel_check):
    if cancel_check is not None and cancel_check():
        raise ValueError('Sentence gap report cancelled.')


def _validate_text(text):
    """Shared fail-fast text validation (V42: single copy for both callers)."""
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT_CHARS:
        raise ValueError('Sentence gap analysis requires nonempty text up to 20,000 characters.')
    if any(0xD800 <= ord(c) <= 0xDFFF for c in text):
        raise ValueError('Text contains invalid Unicode.')


def get_sentence_spans(text):
    """Split *text* into sentence spans with certainty flags.

    Returns list of dicts {char_start, char_end, text, certain, reason}.
    Uncertain boundaries (abbreviation-like, decimal, missing terminator on a
    non-final span) are marked, never silently treated as certain.
    """
    _validate_text(text)
    spans, start, i, n = [], 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch in _TERMINAL or ch == '\n':
            end = i + 1
            # Closing quotes/brackets belong to the sentence.
            while end < n and text[end] in _CLOSING:
                end += 1
            certain, reason = True, None
            if ch == '\n':
                certain, reason = False, 'newline_boundary_uncertain'
            elif ch in '.。' and i > 0 and text[i - 1].isdigit() and i + 1 < n and text[i + 1].isdigit():
                certain, reason = False, 'decimal_not_sentence_boundary'
            elif ch == '…':
                certain, reason = False, 'ellipsis_boundary_uncertain'
            elif ch == '.' and re.search(
                    r'\b(?:[A-Za-z]\.|Mr\.|Mrs\.|Ms\.|Dr\.|St\.|Prof\.|Sr\.|Jr\.|Inc\.|Fig\.|Ave\.|Vol\.|'
                    r'vs\.|etc\.|No\.|Approx\.|Lt\.|Col\.|Gen\.|Sen\.|Rep\.|Gov\.|Hon\.|Rev\.|Capt\.|Sgt\.|Eq\.|Ch\.|Dept\.|Messrs\.|Mmes\.|al\.)$',
                    text[start:i + 1]):
                certain, reason = False, 'abbreviation_boundary_uncertain'
            elif ch == '.' and text[end:].lstrip()[:1].islower():
                certain, reason = False, 'abbreviation_boundary_uncertain'
            elif end < n and not text[end].isspace():
                certain, reason = False, 'no_space_after_terminal'
            spans.append({'char_start': start, 'char_end': end, 'text': text[start:end],
                          'certain': certain, 'reason': reason})
            nxt = end
            while nxt < n and text[nxt].isspace():
                nxt += 1
            start = nxt
            i = nxt
            continue
        i += 1
    if start < n:
        tail = text[start:]
        if tail.strip():
            spans.append({'char_start': start, 'char_end': n, 'text': tail,
                          'certain': False, 'reason': 'missing_terminal_punctuation'})
    if not spans:
        raise ValueError('No sentence spans found.')
    return spans


class GapCoordinateMap:
    """Original<->derived time map for insertions at original times."""

    def __init__(self, duration, insertions=()):
        if not _finite(duration) or not 0 < duration <= MAX_SECONDS:
            raise ValueError('Coordinate map requires 0 < duration <= 120 seconds.')
        self.duration = float(duration)
        rows = []
        for item in insertions:
            at, length = item
            if not _finite(at) or not _finite(length) or not 0 <= at <= duration or not 0 < length:
                raise ValueError('Invalid insertion in coordinate map.')
            rows.append((float(at), float(length)))
        rows.sort()
        for (a1, _), (a2, _) in zip(rows, rows[1:]):
            if a2 < a1:
                raise ValueError('Insertions must be ordered.')
        self.insertions = tuple(rows)
        # V16: finite inputs must not overflow into a nonfinite derived
        # duration or saturate map arithmetic. No arbitrary cap is imposed on
        # large-but-finite maps; only mathematically unrepresentable (inf)
        # results are rejected at construction.
        try:
            derived = float(duration) + math.fsum(l for _, l in rows)
        except OverflowError as exc:
            raise ValueError('Coordinate map derived duration is not representable.') from exc
        cumulative = 0.0
        for _, length in rows:
            cumulative += length
            if not math.isfinite(cumulative):
                raise ValueError('Coordinate map derived duration is not representable.')
        if not math.isfinite(derived):
            raise ValueError('Coordinate map derived duration is not representable.')
        if rows and derived == math.fsum(l for _, l in rows):
            # Adding the original duration had no floating-point effect: map
            # arithmetic has saturated and can no longer tell original
            # times apart.
            raise ValueError('Coordinate map derived duration is not representable.')
        self.derived_duration = derived

    def _check(self, value, upper):
        if not _finite(value) or not 0 <= value <= upper:
            raise ValueError('Time outside coordinate map bounds.')

    def map_original_to_derived(self, t):
        self._check(t, self.duration)
        result = float(t) + math.fsum(l for at, l in self.insertions if at <= t)
        if not math.isfinite(result):
            raise ValueError('Mapped time is not representable.')
        return result

    def map_derived_to_original(self, t):
        self._check(t, self.derived_duration)
        cursor, orig = 0.0, 0.0
        # Walk segments: [orig, at] maps to [cursor, cursor + (at-orig)].
        remaining = float(t)
        pos_orig, pos_der = 0.0, 0.0
        for at, length in self.insertions:
            seg = at - pos_orig
            if remaining <= pos_der + seg:
                return pos_orig + (remaining - pos_der)
            pos_der += seg + length
            pos_orig = at
            if remaining <= pos_der:
                # Inside an inserted region: maps back to the insertion point.
                return pos_orig
        return pos_orig + (remaining - pos_der)

    def is_identity(self):
        return not self.insertions


def compose_coordinate_maps(first, second):
    """Apply *first* then *second*; second's domain is first's derived range."""
    if not isinstance(first, GapCoordinateMap) or not isinstance(second, GapCoordinateMap):
        raise ValueError('Composition requires coordinate maps.')
    if abs(second.duration - first.derived_duration) > 1e-9:
        raise ValueError('Second map duration must equal first map derived duration.')
    insertions = list(first.insertions)
    for at, length in second.insertions:
        insertions.append((first.map_derived_to_original(at), length))
    return GapCoordinateMap(first.duration, insertions)


def _validate_provenance(provenance, text, audio_sha256, verified_file_sha256=None):
    if not isinstance(provenance, dict):
        raise ValueError('Provenance is required.')
    required = {'audio_sha256', 'text_sha256', 'provider', 'language'}
    if not required <= set(provenance):
        raise ValueError('Provenance is missing required fields.')
    if provenance['audio_sha256'] != audio_sha256 or not _HEX64.fullmatch(str(provenance['audio_sha256'] or '')):
        raise ValueError('Provenance audio identity does not match the analysed audio.')
    if provenance['text_sha256'] != hashlib.sha256(text.encode('utf-8')).hexdigest():
        raise ValueError('Provenance text identity does not match the analysed text.')
    if not isinstance(provenance['provider'], str) or not provenance['provider'].strip():
        raise ValueError('Provenance provider is required.')
    if provenance.get('language') != 'en':
        raise ValueError("Only provenance language 'en' is supported.")
    for key in ('asr_seconds',):
        if key in provenance and (not _finite(provenance[key]) or provenance[key] < 0):
            raise ValueError('Provenance timing must be finite and nonnegative.')
    result = {'audio_sha256': provenance['audio_sha256'],
              'text_sha256': provenance['text_sha256'],
              'provider': provenance['provider'], 'language': 'en'}
    # V07: a file identity can only be carried when the caller actually
    # verified file bytes (CLI bridge). A PCM-only caller cannot verify any
    # file digest, so an unverified claim is rejected, never echoed as if
    # it were verified.
    supplied_file = provenance.get('file_sha256')
    if supplied_file is not None and verified_file_sha256 is None:
        raise ValueError('Provenance file identity is unverified for decoded PCM input.')
    if verified_file_sha256 is not None:
        if not _HEX64.fullmatch(str(verified_file_sha256 or '')):
            raise ValueError('Provenance file identity is malformed.')
        if supplied_file is not None and supplied_file != verified_file_sha256:
            raise ValueError('Provenance file identity does not match the verified audio file bytes.')
        result['file_sha256'] = verified_file_sha256
    return result


def _validate_spans(word_spans, text, duration):
    if not isinstance(word_spans, (list, tuple)) or not 1 <= len(word_spans) <= MAX_WORD_SPANS:
        raise ValueError('Word spans must be a list of 1 to 2048 entries.')
    rows = []
    prev_end = 0.0
    prev_char_to = 0
    for span in word_spans:
        if not isinstance(span, dict):
            raise ValueError('Malformed word span.')
        try:
            frm, to, start, end = span['from'], span['to'], span['start'], span['end']
        except KeyError as exc:
            raise ValueError('Word span is missing fields.') from exc
        if type(frm) is not int or type(to) is not int or not 0 <= frm < to <= len(text):
            raise ValueError('Word span character offsets are out of bounds.')
        if not _finite(start) or not _finite(end) or not 0 <= start < end <= duration:
            raise ValueError('Word span timestamps are out of bounds.')
        if start < prev_end:
            raise ValueError('Word spans overlap or are unordered.')
        # V05: character ordering must agree with time ordering; a
        # char-reversed list must not silently redefine neighbouring words.
        if frm < prev_char_to:
            raise ValueError('Word spans overlap or are unordered.')
        prev_end = end
        prev_char_to = to
        rows.append({'from': frm, 'to': to, 'start': float(start), 'end': float(end),
                     'text': text[frm:to], 'word': span.get('word')})
    return rows


def _validate_timing_evidence(evidence, text, duration, pcm_digest, provenance):
    """Validate an evidence envelope and return normalized span rows.

    Fail-closed: anything short of a complete envelope whose spans are
    reproduced by the existing ``word_timing`` validator raises ValueError.
    """
    from word_timing import get_timing_fingerprint, get_validated_word_spans
    if not isinstance(evidence, dict):
        raise ValueError(
            'Word timing evidence envelope required; bare span lists are not trusted timing input.')
    required = {'display_text', 'segments', 'spans', 'duration', 'provider', 'language',
                'binding', 'fingerprint'}
    if not required <= set(evidence):
        raise ValueError('Word timing evidence envelope is incomplete.')
    if evidence['display_text'] != text:
        raise ValueError('Word timing evidence text does not match the analysed text.')
    binding = evidence['binding']
    if not isinstance(binding, dict):
        raise ValueError('Word timing evidence binding is malformed.')
    if not {'audio_sha256', 'text_sha256', 'provider', 'language', 'payload_sha256'} <= set(binding):
        raise ValueError('Word timing evidence binding is incomplete.')
    if binding['audio_sha256'] != pcm_digest:
        raise ValueError('Word timing evidence audio binding does not match the analysed audio.')
    if binding['text_sha256'] != provenance['text_sha256']:
        raise ValueError('Word timing evidence text binding does not match the analysed text.')
    if binding['provider'] != evidence['provider'] or binding['provider'] != provenance['provider']:
        raise ValueError('Word timing evidence provider does not match.')
    if binding['language'] != 'en' or evidence['language'] != 'en':
        raise ValueError("Only word timing evidence language 'en' is supported.")
    if binding['payload_sha256'] != _get_timing_payload_digest(evidence):
        raise ValueError('Word timing evidence payload does not match its binding.')
    if evidence['fingerprint'] != get_timing_fingerprint(binding):
        raise ValueError('Word timing evidence fingerprint does not match its binding.')
    ev_duration = evidence['duration']
    if not _finite(ev_duration) or not 0 < ev_duration <= MAX_SECONDS:
        raise ValueError('Word timing evidence duration is invalid.')
    if abs(float(ev_duration) - duration) > 0.25:
        raise ValueError('Word timing evidence duration does not match the analysed audio.')
    if not isinstance(evidence['segments'], list) or not evidence['segments']:
        raise ValueError('Word timing evidence segments are required.')
    if not isinstance(evidence['spans'], list) or not evidence['spans']:
        raise ValueError('Word timing evidence spans are required.')
    if len(evidence['segments']) > 4096:
        raise ValueError('Word timing evidence segments exceed their size limit.')
    verified = get_validated_word_spans(text, evidence['segments'], float(ev_duration))
    # Exact-equality, same convention as editor_word_timing.get_cached_word_timing.
    if verified != evidence['spans']:
        raise ValueError('Word timing evidence spans do not match the validated transcript.')
    return _validate_spans(verified, text, duration)


_PCM_DIGEST_FORMAT = {'sample_dtype': '<f8', 'order': 'C', 'shape': 'frames,channels',
                      'samples': 'decoded at stored sample rate and channel count'}


def get_pcm_digest(pcm, *, cancel_check=None):
    """SHA-256 of decoded float samples, little-endian float64 in C order.

    Mono (frames,) and (frames, 1) have the same bytes. No resampling or
    amplitude scaling occurs; integer arrays are rejected. Strided/Fortran
    views are hashed by bounded row blocks in the same C order.
    """
    import numpy as np
    arr = np.asarray(pcm)
    if arr.dtype.kind != 'f' or arr.ndim not in (1, 2):
        raise ValueError('PCM must be a real float array.')
    digest = hashlib.sha256()
    for first in range(0, len(arr), 65536):
        _check_cancel(cancel_check)
        block = np.ascontiguousarray(arr[first:first + 65536], dtype='<f8')
        digest.update(memoryview(block).cast('B'))
    _check_cancel(cancel_check)
    return digest.hexdigest()


def _get_timing_payload_digest(evidence):
    payload = {key: evidence[key] for key in
               ('display_text', 'segments', 'spans', 'duration', 'provider', 'language')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                     allow_nan=False, separators=(',', ':')).encode('utf-8')).hexdigest()


def get_word_timing_evidence(*, text, pcm, sample_rate, segments, provider, language='en'):
    """Prepare a report envelope from existing observed words; never run ASR.

    Integrity binds the payload to the declared identity; it does not attest
    who produced the observations. Stripped Editor caches cannot reconstruct it.
    """
    from word_timing import get_timing_fingerprint, get_validated_word_spans
    arr, duration = _prepare_analysis_inputs(text, pcm, sample_rate, MIN_QUIET_SECONDS)
    evidence = {'display_text': text, 'segments': segments,
                'spans': get_validated_word_spans(text, segments, duration),
                'duration': duration, 'provider': provider, 'language': language}
    binding = {'audio_sha256': get_pcm_digest(arr),
               'text_sha256': hashlib.sha256(text.encode('utf-8')).hexdigest(),
               'provider': provider, 'language': language,
               'payload_sha256': _get_timing_payload_digest(evidence)}
    evidence.update(binding=binding, fingerprint=get_timing_fingerprint(binding))
    return evidence


def _frame_rms_db(pcm, sample_rate, first=0, last=None, cancel_check=None):
    """Channel-max frame RMS, with absolute frame phase and bounded temporaries."""
    import numpy as np
    hop = max(1, int(round(sample_rate * FRAME_SECONDS)))
    total = len(pcm)
    frames = (total + hop - 1) // hop if total else 0
    last = frames if last is None else min(last, frames)
    out = np.empty(max(0, last - first), dtype=np.float64)
    for base in range(first, last, _RMS_CHUNK_FRAMES):
        _check_cancel(cancel_check)
        stop = min(base + _RMS_CHUNK_FRAMES, last)
        n = stop - base
        start_row = base * hop
        available = min(total, stop * hop)
        full = (available - start_row) // hop if available > start_row else 0
        if full > 0:
            block = np.asarray(pcm[start_row:start_row + full * hop], dtype=np.float64)
            if block.ndim == 1:
                block = block[:, None]
            shaped = block.reshape(full, hop, block.shape[1])
            rms = np.sqrt(np.mean(shaped ** 2, axis=1))
            peak = np.max(rms, axis=1)
            out[base - first:base - first + full] = np.where(
                peak > 0, 20.0 * np.log10(np.where(peak > 0, peak, 1.0)), -np.inf)
        if full < n:
            idx = base + full
            block = pcm[idx * hop:(idx + 1) * hop]
            rms = np.sqrt(np.mean(np.asarray(block, dtype=np.float64) ** 2, axis=0))
            peak = float(np.max(rms)) if rms.size else 0.0
            out[idx - first] = 20 * math.log10(peak) if peak > 0 else -np.inf
    return out, hop


def _get_frame_bounds(hop, sample_rate, lo, hi, n):
    """Absolute indices whose frame starts satisfy lo <= t < hi."""
    start = int(math.ceil(lo * sample_rate / hop)) if lo > 0 else 0
    start = max(0, min(start, n))
    while start > 0 and (start - 1) * hop / sample_rate >= lo:
        start -= 1
    while start < n and start * hop / sample_rate < lo:
        start += 1
    end = int(math.ceil(hi * sample_rate / hop)) if hi > 0 else 0
    end = max(start, min(end, n))
    while end > start and end * hop / sample_rate >= hi:
        end -= 1
    while end < n and end * hop / sample_rate < hi:
        end += 1
    return start, end


def _get_quiet_runs(mask, hop, sample_rate, lo, hi, duration, *, first=0, frames=None):
    frames = len(mask) if frames is None else frames
    start, end = _get_frame_bounds(hop, sample_rate, lo, hi, frames)
    run = None
    for idx in range(start, end + 1):
        quiet = idx < end and bool(mask[idx - first])
        if quiet and run is None:
            run = idx
        if not quiet and run is not None:
            s, e = run * hop / sample_rate, min(idx * hop / sample_rate, hi, duration)
            if e > s:
                yield s, e
            run = None


def _longest_quiet(quiet_mask, hop, sample_rate, lo, hi, duration):
    return max(_get_quiet_runs(quiet_mask, hop, sample_rate, lo, hi, duration),
               key=lambda interval: interval[1] - interval[0], default=None)


def _get_quiet_measurement(interval):
    if interval is None:
        return None
    start, end = interval
    return {'start': start, 'end': end, 'seconds': end - start, 'threshold_dbfs': QUIET_DBFS}


def _has_samples_above_quiet_floor(arr, cancel_check):
    # A sufficient global low-level rejection, without an extra whole-take RMS
    # pass: typical takes return on the first bounded block with audible samples.
    import numpy as np
    threshold = 10 ** (QUIET_DBFS / 20)
    for start in range(0, len(arr), 65536):
        _check_cancel(cancel_check)
        if np.any(np.abs(arr[start:start + 65536]) >= threshold):
            return True
    return False


def _serialize_report_bytes(report):
    """Exact emitted UTF-8 bytes (V30): indent=2, LF, allow_nan=False."""
    return json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False).encode('utf-8')


def _validate_sample_rate(sample_rate):
    if (not isinstance(sample_rate, Integral) or isinstance(sample_rate, bool)
            or not 0 < sample_rate <= 384000):
        raise ValueError('Invalid sample rate.')


def _prepare_analysis_inputs(text, pcm, sample_rate, min_quiet_seconds):
    """Validate both entry points before conversion, hashing or acoustics."""
    import numpy as np
    _validate_text(text)
    _validate_sample_rate(sample_rate)
    if not _finite(min_quiet_seconds) or not 0 < min_quiet_seconds <= 10:
        raise ValueError('Invalid minimum quiet duration.')
    # V23/V25: inspect dtype/shape/frame/duration bounds BEFORE any float64
    # conversion, and reject complex (lossy) input instead of discarding the
    # imaginary part.
    native = np.asarray(pcm)
    if native.dtype.kind != 'f':
        raise ValueError('PCM must be a real float array.')
    if native.ndim == 1:
        native = native[:, None]
    if native.ndim != 2 or native.shape[0] == 0 or native.shape[1] not in (1, 2):
        raise ValueError('PCM must be (frames,) or (frames, 1|2 channels).')
    duration = len(native) / sample_rate
    if not 0 < duration <= MAX_SECONDS:
        raise ValueError(f'Audio duration must be within {MAX_SECONDS} seconds.')
    if native.shape[0] > sample_rate * MAX_SECONDS:
        raise ValueError('Audio exceeds the frame allowance.')
    arr = native.astype(np.float64, copy=False)
    for start in range(0, len(arr), 65536):
        if not np.isfinite(arr[start:start + 65536]).all():
            raise ValueError('PCM contains non-finite samples.')
    return arr, duration


def get_sentence_gap_report(*, text, word_spans, pcm, sample_rate, provenance,
                            explicit_pause_after_sentence=(), cancel_check=None,
                            min_quiet_seconds=MIN_QUIET_SECONDS):
    """Build a bounded report-only sentence-gap analysis for one take.

    *word_spans* must be a word-timing evidence envelope (see module
    docstring); bare span lists are rejected. *pcm* is a real floating-point
    array shaped (frames,) or (frames, channels), channels in
    {1, 2}. Returns a JSON-serializable dict. Raises ValueError on invalid,
    non-finite, oversized or stale inputs, and on cancellation (checked
    before and between validation passes, per digest/RMS chunk and boundary,
    and immediately before returning. The shared transcript matcher itself
    is not interruptible; cancellation during it is observed on return).
    Integer PCM must be normalized by its decoder before this API is called.
    """
    _check_cancel(cancel_check)
    arr, duration = _prepare_analysis_inputs(text, pcm, sample_rate, min_quiet_seconds)
    sample_rate = int(sample_rate)
    pcm_digest = get_pcm_digest(arr, cancel_check=cancel_check)
    return _build_sentence_gap_report(
        text=text, word_spans=word_spans, arr=arr, duration=duration,
        sample_rate=sample_rate, pcm_digest=pcm_digest, provenance=provenance,
        explicit_pause_after_sentence=explicit_pause_after_sentence,
        cancel_check=cancel_check, min_quiet_seconds=min_quiet_seconds)


def _get_boundary_entry(si, left, right):
    return {
        'sentence_index': si,
        'source_boundary': {'left_char_end': left['char_end'],
                            'reason': left['reason'] or 'terminal_punctuation'},
        'original_coordinates': {
            'left_sentence': {'char_start': left['char_start'], 'char_end': left['char_end']},
            'right_sentence': {'char_start': right['char_start'], 'char_end': right['char_end']}},
        'asr_search_window': None, 'measured_quiet_interval': None,
        'safe_quiet_interval': None, 'proposed_extension_ms': 0,
        'proposed_pause_ms': None, 'skip_reason': None, 'status': 'skipped'}


def _build_sentence_gap_report(*, text, word_spans, arr, duration, sample_rate,
                               pcm_digest, provenance,
                               explicit_pause_after_sentence=(), cancel_check=None,
                               min_quiet_seconds=MIN_QUIET_SECONDS,
                               verified_file_sha256=None):
    """Shared build boundary (V48): the CLI passes its precomputed PCM digest
    instead of paying a second full hash. Public callers recompute it in
    ``get_sentence_gap_report``; the digest is never trusted from a caller
    that could supply a different PCM buffer."""
    _check_cancel(cancel_check)
    prov = _validate_provenance(provenance, text, pcm_digest, verified_file_sha256)
    _check_cancel(cancel_check)
    spans = _validate_timing_evidence(word_spans, text, duration, pcm_digest, prov)
    _check_cancel(cancel_check)
    sentences = get_sentence_spans(text)
    _check_cancel(cancel_check)
    # V13: falsy scalars (0/False/'') must not silently erase choices.
    if explicit_pause_after_sentence is None:
        explicit_raw = ()
    elif isinstance(explicit_pause_after_sentence, (str, bytes, Real)):
        raise ValueError('Explicit pauses must be sentence indices.')
    else:
        try:
            explicit_raw = explicit_pause_after_sentence
        except TypeError as exc:  # pragma: no cover - guarded by isinstance above
            raise ValueError('Explicit pauses must be sentence indices.') from exc
    try:
        explicit = set(explicit_raw)
    except TypeError as exc:
        raise ValueError('Explicit pauses must be sentence indices.') from exc
    for item in explicit:
        if (not isinstance(item, Integral) or isinstance(item, bool)
                or not 0 <= item < len(sentences)):
            raise ValueError('Explicit pause sentence index is out of bounds.')
    explicit = {int(item) for item in explicit}
    # Word tokens for immediate-neighbour enforcement (V04/V05).
    from word_timing import get_word_tokens
    tokens = get_word_tokens(text)
    token_starts = [t[1] for t in tokens]
    span_starts = [s['from'] for s in spans]
    by_to = {s['to']: s for s in spans}
    by_from = {s['from']: s for s in spans}
    validated_times = {(s['start'], s['end']) for s in spans}
    unmatched = [(s['start'], s['end']) for s in word_spans['segments']
                 if get_word_tokens(s['word']) and (s['start'], s['end']) not in validated_times]
    unmatched_ends = [end for _, end in unmatched]
    # A compact minimum entry is a strict lower byte bound for every emitted
    # entry: zero has the shortest numeric representation; empty reason and
    # null intervals are minima, and a numeric pause is shorter than null.
    minimum = _get_boundary_entry(0, {'char_start': 0, 'char_end': 0, 'reason': ''},
                                  {'char_start': 0, 'char_end': 0})
    minimum['source_boundary']['reason'] = ''
    minimum['proposed_pause_ms'] = 0
    floor = len(json.dumps(minimum, separators=(',', ':')).encode('utf-8'))
    if (len(sentences) - 1) * floor > MAX_REPORT_BYTES:
        raise ValueError('Sentence gap report exceeds its size limit.')
    proposals = []
    hop = max(1, int(round(sample_rate * FRAME_SECONDS)))
    frames = (len(arr) + hop - 1) // hop
    # Dense, tiny windows otherwise cost more Python calls than one reduction.
    use_full_rms = frames <= (len(sentences) - 1) * 64
    full_db = None
    audible = None
    for si in range(len(sentences) - 1):
        _check_cancel(cancel_check)
        left, right = sentences[si], sentences[si + 1]
        entry = _get_boundary_entry(si, left, right)

        def _skip(reason):
            entry['skip_reason'] = reason
            proposals.append(entry)

        if si in explicit:
            _skip('explicit_user_pause')
            continue
        if not left['certain']:
            _skip('uncertain_sentence_boundary')
            continue
        crossing_index = bisect_left(span_starts, right['char_start']) - 1
        if crossing_index >= 0 and spans[crossing_index]['to'] > left['char_end']:
            _skip('ambiguous_grouped_word_timing_at_boundary')
            continue
        left_tokens = [t for t in tokens[bisect_left(token_starts, left['char_start']):
                                       bisect_left(token_starts, left['char_end'])]
                       if t[2] <= left['char_end']]
        right_tokens = [t for t in tokens[bisect_left(token_starts, right['char_start']):
                                        bisect_left(token_starts, right['char_end'])]
                        if t[2] <= right['char_end']]
        if not left_tokens or not right_tokens:
            _skip('missing_asr_timing_for_neighbouring_words')
            continue
        prev, nxt = by_to.get(left_tokens[-1][2]), by_from.get(right_tokens[0][1])
        if prev is None or nxt is None:
            _skip('missing_asr_timing_for_neighbouring_words')
            continue
        if prev['from'] < left['char_start'] or prev['to'] > left['char_end'] \
                or nxt['from'] < right['char_start'] or nxt['to'] > right['char_end']:
            _skip('neighbouring_span_not_confined_to_sentence')
            continue
        if prev['end'] > nxt['start']:
            _skip('overlapping_asr_boundaries')
            continue
        window = (prev['end'], nxt['start'])
        entry['asr_search_window'] = {'start': window[0], 'end': window[1],
                                      'seconds': window[1] - window[0]}
        if window[1] == window[0]:
            _skip('no_gap_in_asr_search_window')
            continue
        unmatched_index = bisect_right(unmatched_ends, window[0])
        if unmatched_index < len(unmatched) and unmatched[unmatched_index][0] < window[1]:
            _skip('unmatched_observed_speech_in_search_window')
            continue
        if audible is None:
            audible = _has_samples_above_quiet_floor(arr, cancel_check)
        if not audible:
            _skip('take_below_quiet_threshold')
            continue
        first, last = _get_frame_bounds(hop, sample_rate, *window, frames)
        if use_full_rms:
            if full_db is None:
                full_db, _ = _frame_rms_db(arr, sample_rate, cancel_check=cancel_check)
            db, offset = full_db[first:last], first
        else:
            db, _ = _frame_rms_db(arr, sample_rate, first, last, cancel_check)
            offset = first
        runs = list(_get_quiet_runs(db < QUIET_DBFS, hop, sample_rate, *window,
                                    duration, first=offset, frames=frames))
        if not runs:
            _skip('no_quiet_interval_in_search_window')
            continue
        raw = max(runs, key=lambda q: q[1] - q[0])
        eligible = []
        for q in runs:
            safe = (max(q[0], window[0] + EDGE_MARGIN_SECONDS),
                    min(q[1], window[1] - EDGE_MARGIN_SECONDS))
            if safe[1] - safe[0] >= min_quiet_seconds:
                eligible.append((q, safe))
        if not eligible:
            entry['measured_quiet_interval'] = _get_quiet_measurement(raw)
            _skip('quiet_interval_below_minimum' if raw[1] - raw[0] < min_quiet_seconds
                  else 'quiet_interval_too_close_to_word_edge')
            continue
        raw, safe = max(eligible, key=lambda pair: pair[1][1] - pair[1][0])
        entry['measured_quiet_interval'] = _get_quiet_measurement(raw)
        entry['safe_quiet_interval'] = _get_quiet_measurement(safe)
        if not (prev['end'] <= safe[0] and safe[1] <= nxt['start']):
            _skip('within_word_dip')
            continue
        extension = int(round((safe[1] - safe[0]) * 1000))
        if extension <= 0:
            _skip('quiet_interval_rounds_to_zero_ms')
            continue
        entry['proposed_pause_ms'] = extension
        entry['proposed_extension_ms'] = extension
        entry['status'] = 'proposal'
        proposals.append(entry)
    _check_cancel(cancel_check)
    report = {
        'schema_version': 1,
        'kind': 'sentence_gap_report_only',
        'mutates_audio': False,
        'authorizes_edit': False,
        'duration_seconds': duration,
        'sample_rate': sample_rate,
        'channels': int(arr.shape[1]),
        'provenance': prov,
        'pcm_digest_format': dict(_PCM_DIGEST_FORMAT),
        'timing_evidence_payload_sha256': word_spans['binding']['payload_sha256'],
        'proposals': proposals,
        'coordinate_map': {
            'duration': duration,
            'insertions': [{'at_original': (p['safe_quiet_interval']['start']
                                             if p['safe_quiet_interval'] else None),
                            'extension_ms': p['proposed_extension_ms']}
                           for p in proposals if p['status'] == 'proposal']},
        'notice': ('Report only. ASR timestamps alone never authorize insertion, '
                   'speech removal or phoneme crop. Original take bytes are unchanged.'),
    }
    # V30: enforce the limit on the exact emitted UTF-8 bytes.
    if len(_serialize_report_bytes(report)) > MAX_REPORT_BYTES:
        raise ValueError('Sentence gap report exceeds its size limit.')
    _check_cancel(cancel_check)
    return report


_NO_EXPECTED_DIGEST = object()


def _read_wav_bounded(path, cancel_check=None, expected_digest=_NO_EXPECTED_DIGEST):
    """Bounded offline WAV read for the CLI; returns (pcm, rate, sha256).

    V49: streaming bounded hashing via audio_source_io (1 MiB blocks with
    in-stream limit + raising cancel wrapper), pre- and post-decode; both
    hashes are required (mutation fence). The same opened handle is decoded
    and re-hashed, with path replacement detection by (dev, inode).
    """
    import soundfile as sf
    from audio_source_io import read_bounded_audio_hash
    from audio_validation import GeneratedAudioError, validate_wav_container

    def _check():
        _check_cancel(cancel_check)

    path = Path(path)
    if not path.is_file():
        raise ValueError('Audio file is missing.')
    # Stat via the opened handle (fresh open); size limit also enforced
    # in-stream by the hash helper for a file growing during the read.
    with path.open('rb') as stream:
        st = os.fstat(stream.fileno())
        if st.st_size == 0 or st.st_size > MAX_AUDIO_BYTES:
            raise ValueError(f'Audio file is missing, empty or exceeds {MAX_AUDIO_BYTES // 1024**2} MiB.')
        digest, _size = read_bounded_audio_hash(stream, MAX_AUDIO_BYTES, _check)
        if expected_digest is not _NO_EXPECTED_DIGEST and digest != expected_digest:
            raise ValueError('Provenance audio identity does not match the audio file bytes.')
        try:
            validate_wav_container(stream, 'Sentence gap input', cancel_check=_check)
        except GeneratedAudioError as error:
            raise ValueError(str(error)) from error
        stream.seek(0)
        # Decode from the same handle; sf accepts a file object.
        with sf.SoundFile(stream) as snd:
            samplerate, channels, frames = snd.samplerate, snd.channels, snd.frames
            _validate_sample_rate(samplerate)
            if frames <= 0 or frames > samplerate * MAX_SECONDS:
                raise ValueError(f'Audio duration must be within {MAX_SECONDS} seconds.')
            if channels not in (1, 2):
                raise ValueError('Audio must have 1 or 2 channels.')
            if frames * channels * 8 > MAX_DECODED_BYTES:
                raise ValueError('Decoded audio exceeds the analysis byte allowance.')
            rate = samplerate
            data = snd.read(dtype='float64', always_2d=True)
        _check_cancel(cancel_check)
        stream.seek(0)
        digest_after, _size_after = read_bounded_audio_hash(stream, MAX_AUDIO_BYTES, _check)
        if digest_after != digest:
            raise ValueError('Audio source changed while reading.')
        # Path replacement detection: the path must still name this inode.
        try:
            path_st = os.stat(str(path))
        except OSError as exc:
            raise ValueError('Audio source changed while reading.') from exc
        if (path_st.st_dev, path_st.st_ino) != (st.st_dev, st.st_ino):
            raise ValueError('Audio source changed while reading.')
        if path_st.st_size != st.st_size:
            raise ValueError('Audio source changed while reading.')
        _check_cancel(cancel_check)
    return data, rate, digest


def _load_json_bounded(path, limit=MAX_JSON_BYTES):
    _check_json_limit(limit)
    with Path(path).open('rb') as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise ValueError('Input JSON exceeds its size limit.')
    return json.loads(data.decode('utf-8'), object_pairs_hook=_get_unique_json_object,
                      parse_constant=_reject_json_constant)


def _get_unique_json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Input JSON contains duplicate object keys.')
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError('Input JSON contains a non-finite constant.')


def _check_json_limit(limit):
    if type(limit) is not int or not 0 < limit <= MAX_AUDIO_BYTES:
        raise ValueError('Invalid JSON size limit.')


def _read_text_bounded(path, cancel_check=None):
    """Bounded text read: read limit+1 bytes before decoding (V14)."""
    _check_cancel(cancel_check)
    with Path(path).open('rb') as stream:
        data = stream.read(MAX_TEXT_BYTES + 1)
    if len(data) > MAX_TEXT_BYTES:
        raise ValueError('Sentence gap analysis requires nonempty text up to 20,000 characters.')
    _check_cancel(cancel_check)
    return data.decode('utf-8')


def _resolved(path):
    return Path(path).resolve()


def _same_path(a, b):
    try:
        return os.path.samefile(str(a), str(b))
    except (OSError, ValueError):
        return False


def _guard_output_alias(out_path, input_paths):
    """V01: output must not alias any input (resolved/samefile)."""
    out_res = _resolved(out_path)
    for raw in input_paths:
        if raw is None:
            continue
        inp = Path(raw)
        if _resolved(inp) == out_res or _same_path(inp, Path(out_path)):
            raise ValueError('Output path must not alias an input path.')


def _publish_report(report, out_path, cancel_check=None):
    """Unique-temp publish with cancel immediately before replace (V15).

    The temp file is unique (mkstemp in the output directory), never the
    predictable ``<out>.tmp``; cleanup is attempted on every failure and a
    locked residue is diagnosed. The prior good report is preserved because
    publication is a final
    os.replace.
    """
    data = _serialize_report_bytes(report)
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError('Sentence gap report exceeds its size limit.')
    out = Path(out_path)
    out_dir = str(out.parent) if str(out.parent) else '.'
    fd, tmp_name = tempfile.mkstemp(prefix='.sentence-gap-', suffix='.json', dir=out_dir)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(5):
            _check_cancel(cancel_check)
            try:
                os.replace(str(tmp), str(out))
                break
            except OSError as error:
                transient = (getattr(error, 'winerror', None) in (5, 32)
                             or 'Access is denied' in str(error)
                             or 'being used by another process' in str(error)
                             or 'The process cannot access the file' in str(error))
                if attempt == 4 or not transient:
                    raise
                time.sleep(0.05 * (2 ** attempt))
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except OSError as error:
            # Cleanup must never mask cancellation or the publication error.
            try:
                print(f'warning: could not remove owned report temp {tmp}: {error}',
                      file=sys.stderr)
            except Exception:
                pass
        try:
            dir_fd = os.open(out_dir, os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except (OSError, AttributeError):
            pass


def main(argv=None, cancel_check=None):
    parser = argparse.ArgumentParser(description='Report-only internal sentence-gap analysis.')
    parser.add_argument('--audio', required=True, help='Rendered take WAV file (read-only).')
    parser.add_argument('--text', help='Authored/prepared text of the take.')
    parser.add_argument('--text-file', help='UTF-8 file containing the text.')
    parser.add_argument('--word-timings', required=True,
                        help='JSON word-timing evidence envelope '
                             '(display_text, segments, spans, duration, provider, '
                             'language, binding, fingerprint).')
    parser.add_argument('--provenance', required=True, help='JSON provenance object.')
    parser.add_argument('--out', required=True, help='Report JSON output path (written atomically).')
    parser.add_argument('--explicit-pause-after', default='',
                        help='Comma-separated sentence indices with explicit user pauses.')
    parser.add_argument('--min-quiet-seconds', type=float, default=MIN_QUIET_SECONDS)
    args = parser.parse_args(argv)
    if (args.text is None) == (args.text_file is None):
        parser.error('Provide exactly one of --text or --text-file.')
    if args.text_file == '':
        parser.error('--text-file must name a nonempty path.')
    # V01: guard resolved output aliases to ALL inputs before any write.
    input_paths = [args.audio, args.word_timings, args.provenance]
    if args.text_file:
        input_paths.append(args.text_file)
    _guard_output_alias(args.out, input_paths)
    text = args.text if args.text is not None else _read_text_bounded(args.text_file, cancel_check)
    _validate_text(text)
    timings = _load_json_bounded(args.word_timings)
    provenance = _load_json_bounded(args.provenance)
    try:
        explicit = [int(x) for x in args.explicit_pause_after.split(',') if x.strip()]
    except ValueError as exc:
        raise ValueError('Explicit pause indices must be integers.') from exc
    expected_digest = (provenance.get('audio_sha256') if isinstance(provenance, dict)
                       else _NO_EXPECTED_DIGEST)
    pcm, rate, file_digest = _read_wav_bounded(args.audio, cancel_check, expected_digest)
    # Provenance must bind the actual file bytes, not only decoded PCM.
    if not isinstance(provenance, dict) or provenance.get('audio_sha256') != file_digest:
        raise ValueError('Provenance audio identity does not match the audio file bytes.')
    # V48: single PCM hash computed here once, passed through the private
    # build boundary instead of being recomputed by the library (CLI file
    # digest vs PCM digest kept distinct: file_digest stays in provenance
    # via verified_file_sha256).
    pcm_float, duration = _prepare_analysis_inputs(
        text, pcm, rate, args.min_quiet_seconds)
    pcm_digest = get_pcm_digest(pcm_float, cancel_check=cancel_check)
    provenance_for_build = {**provenance, 'audio_sha256': pcm_digest}
    report = _build_sentence_gap_report(
        text=text, word_spans=timings, arr=pcm_float, duration=duration,
        sample_rate=rate, pcm_digest=pcm_digest, provenance=provenance_for_build,
        explicit_pause_after_sentence=explicit, cancel_check=cancel_check,
        min_quiet_seconds=args.min_quiet_seconds,
        verified_file_sha256=file_digest)
    _check_cancel(cancel_check)
    # Alias again at the publication boundary.
    _guard_output_alias(args.out, input_paths)
    _publish_report(report, args.out, cancel_check)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except ValueError as error:
        print(f'error: {error}', file=sys.stderr)
        raise SystemExit(2)
