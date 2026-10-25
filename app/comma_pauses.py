"""Is there a real pause at each comma in a render?

whisper.cpp word boundaries place a comma in time, but its word spans are
contiguous - silence is absorbed into neighbouring words - so they cannot
measure the pause itself. The loudness envelope can: the pause at a mark is the
longest run at least QUIET_DB below the clip's loud level within WINDOW_S of
the boundary.

Validated on known answers before use: cutting a comma pause out of the same
renders moved it 0.20 s -> 0.05 s. It did NOT reliably separate a single cut
line from clean lines, because clean renders also skip commas, so report rates
over many commas (a voice, a book), never a verdict on one line. English only
(whisper.cpp Small.en). A clip whose timing is refused is recorded as refused,
never as a pause of 0 or as a pass.
"""
import re
import tempfile

import numpy as np
import soundfile as sf

QUIET_DB = -30.0
WINDOW_S = 0.3
FRAME_S = 0.010
SKIPPED_PAUSE_S = 0.10
# A mark between two timed words. Punctuation must be followed by whitespace,
# so "3,000" and "3.5" are not pauses; a dash counts with or without spaces.
# A line's final full stop is never between two words, so it is never counted.
MID_LINE_MARK = re.compile(r"[,;:.!?](?=\s)|[—–]")


def get_envelope(path):
    """-> loudness per 10 ms frame (25 ms window), in dB relative to the clip's 95th percentile."""
    pcm, rate = sf.read(path, dtype="float32")
    pcm = pcm.mean(axis=1) if pcm.ndim > 1 else pcm
    hop, window = int(rate * FRAME_S), int(rate * .025)
    frames = np.lib.stride_tricks.sliding_window_view(np.pad(pcm, (0, window)), window)[::hop]
    db = 20 * np.log10(np.sqrt((frames ** 2).mean(axis=1)) + 1e-9)
    return db - np.percentile(db, 95)


def get_mark_pauses(db, text, spans):
    """-> [{at_s, pause_s, mark}] for each mid-line mark between two timed words.

    `spans` are word_timing spans ({from, to, start, end}); their boundaries only
    locate the mark, the envelope measures the silence there.
    """
    reach = int(round(WINDOW_S / FRAME_S))
    pauses = []
    for before, after in zip(spans, spans[1:]):
        between = text[before["to"]:after["from"]]
        mark = MID_LINE_MARK.search(between)
        if not mark:
            continue
        at = int(before["end"] / FRAME_S)
        longest = run = 0
        for level in db[max(0, at - reach):at + reach]:
            run = run + 1 if level <= QUIET_DB else 0
            longest = max(longest, run)
        pauses.append({"at_s": round(before["end"], 3), "pause_s": round(longest * FRAME_S, 3),
                       "mark": mark.group().strip()})
    return pauses


def has_mid_line_mark(text):
    """Whether `text` has a mark get_mark_pauses would measure, between two words."""
    from word_timing import get_word_tokens
    words = get_word_tokens(text)
    return any(MID_LINE_MARK.search(text[before[2]:after[1]]) for before, after in zip(words, words[1:]))


def get_clip_comma_pauses(audio_path, text, runtime):
    """-> {"pauses": [...]} for one clip, or {"refused": reason} when timing refuses it."""
    from word_timing import render_word_timing
    try:
        with tempfile.TemporaryDirectory() as directory:
            timing = render_word_timing(audio_path, runtime, text, lambda: False, directory)
    except ValueError as error:
        return {"refused": str(error)}
    return {"pauses": get_mark_pauses(get_envelope(audio_path), text, timing["spans"])}


def get_summary(results, *, seed=0, resamples=2000):
    """-> rates over every measured mark in `results` (each from get_clip_comma_pauses)."""
    pauses = np.array([mark["pause_s"] for result in results for mark in result.get("pauses", ())])
    refused = sum(1 for result in results if "refused" in result)
    summary = {"clips": len(results), "clips_refused": refused, "marks": int(len(pauses))}
    if not len(pauses):
        return summary
    skipped = pauses < SKIPPED_PAUSE_S
    rng = np.random.default_rng(seed)
    draws = rng.choice(skipped, size=(resamples, len(skipped))).mean(axis=1)
    summary.update(median_pause_s=round(float(np.median(pauses)), 3),
                   share_skipped=round(float(skipped.mean()), 3),
                   share_skipped_ci95=[round(float(v), 3) for v in np.percentile(draws, [2.5, 97.5])])
    return summary
