"""Bounded short-utterance acoustic evidence, never a lexical or pronunciation verdict."""
import math
import wave

import numpy as np

MAX_SHORT_SECONDS = 15
MAX_ISLANDS = 16
FRAME = 160  # 10 ms, on the shared 16 kHz PCM16 capture.


def get_short_acoustic_evidence(path, cancel_check=lambda: False):
    """Measure silence/levels and candidate repeats between separated voiced islands.

    An island isn't an utterance: breaths, hesitations, stutters and authored
    repetition require review. Candidate similarity is descriptive suspicion,
    not permission to discard a take. No whole-clip DTW or unbounded pair search.
    """
    if cancel_check():
        raise ValueError('Acoustic verification cancelled.')
    with wave.open(str(path), 'rb') as stream:
        frames = stream.getnframes()
        if (stream.getnchannels() != 1 or stream.getsampwidth() != 2 or
                stream.getframerate() != 16000 or not 0 < frames <= MAX_SHORT_SECONDS * 16000):
            raise ValueError('Short acoustic checks require at most 15 seconds of mono 16 kHz PCM16.')
        values = np.frombuffer(stream.readframes(frames), dtype='<i2').astype(np.float32) / 32768
    if len(values) != frames:
        raise ValueError('Short acoustic PCM is incomplete.')
    count = math.ceil(frames / FRAME)
    blocks = np.pad(values, (0, count * FRAME - frames)).reshape(count, FRAME)
    rms = np.sqrt(np.mean(blocks * blocks, axis=1))
    peak = float(np.max(np.abs(values)))
    threshold = max(10 ** (-45 / 20), float(np.max(rms)) * 10 ** (-30 / 20))
    voiced = rms >= threshold
    indices = np.flatnonzero(voiced)
    islands = []
    for index in indices:
        index = int(index)
        if islands and index - islands[-1][1] <= 12:  # Join gaps shorter than 120 ms.
            islands[-1][1] = index + 1
        else:
            islands.append([index, index + 1])
    result = dict(version=1, duration=frames / 16000, peak=peak,
        rms=float(np.sqrt(np.mean(values * values))),
        clipped_fraction=float(np.mean(np.abs(values) >= .999)),
        voiced_seconds=int(np.count_nonzero(voiced)) * .01,
        voiced_islands=[{'start': start * .01, 'end': min(end * .01, frames / 16000)}
                        for start, end in islands[:MAX_ISLANDS]],
        repetition_candidates=[], island_limit_exceeded=len(islands) > MAX_ISLANDS,
        notice='Acoustic islands and similarity do not identify words; listen before judging short speech or authored repetition.')
    if len(islands) > MAX_ISLANDS:
        result['status'] = 'unavailable_island_limit'
        return result
    # 16 broad spectral bands per 10 ms frame, <=1,500 frames. Pair signatures
    # are 64 frames each; at most 120 pairs, with no duration-dependent DP grid.
    spectrum = np.abs(np.fft.rfft(blocks * np.hanning(FRAME), n=256, axis=1))[:, 1:129]
    features = np.log1p(spectrum.reshape(count, 16, 8).mean(axis=2))
    signatures = []
    for start, end in islands:
        if cancel_check():
            raise ValueError('Acoustic verification cancelled.')
        length = end - start
        if not 25 <= length <= 600:
            signatures.append(None)
            continue
        positions = np.linspace(0, length - 1, 64)
        signature = np.array([np.interp(positions, np.arange(length), features[start:end, band])
                              for band in range(16)]).T
        energy = np.interp(positions, np.arange(length), rms[start:end])
        norm = np.linalg.norm(signature, axis=1, keepdims=True)
        normalized = signature / np.maximum(norm, 1e-12)
        # Flat tones have similar frames but no useful speech-like evolution.
        variation = float(np.mean(np.linalg.norm(np.diff(normalized, axis=0), axis=1)))
        signatures.append((normalized, energy, variation))
    for left, first in enumerate(signatures):
        if first is None:
            continue
        for right in range(left + 1, len(signatures)):
            if cancel_check():
                raise ValueError('Acoustic verification cancelled.')
            second = signatures[right]
            if second is None or abs((islands[left][1] - islands[left][0]) -
                                     (islands[right][1] - islands[right][0])) > 8:
                continue
            spectral = float(np.mean(np.sum(first[0] * second[0], axis=1)))
            a, b = first[1] - np.mean(first[1]), second[1] - np.mean(second[1])
            denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
            envelope = float(np.dot(a, b) / denominator) if denominator > 1e-12 else 0.
            if spectral >= .98 and envelope >= .95 and min(first[2], second[2]) >= .04:
                result['repetition_candidates'].append(dict(left_island=left, right_island=right,
                    spectral_similarity=spectral, envelope_correlation=envelope,
                    minimum_spectral_variation=min(first[2], second[2])))
    result['status'] = 'review' if result['repetition_candidates'] or not len(indices) else 'no_acoustic_candidate'
    return result

from loaded_module_code import get_current_loaded_module_code_sha256 as _get_loaded_verification_code
_LOADED_SPEECH_VERIFICATION_CODE_SHA256 = _get_loaded_verification_code()
