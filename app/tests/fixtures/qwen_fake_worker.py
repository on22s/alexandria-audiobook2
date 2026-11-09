"""Fake qwen worker fixture (#1844, TEST ONLY — never user-selectable).

Speaks the qwen_align_worker.py argv protocol: argv[1]=job.json,
argv[2]=result.json. Implements the same sequential measured-cursor
protocol as the real worker in pure stdlib: for each planned window it
slices the row WAV at the measured previous-end cursor, grows the
slice in 3-second steps while speech reaches the slice end, and
reports actual windows (frame offsets, call counts, slice tokens).

Deterministic token model: speech for a window occupies the planned
window duration minus the 3s tail (or an explicit per-row
``fake_speech_offsets`` list of absolute speech-end seconds — a
test-only control for variable-rate / repeated / tiny-remainder
cases). Tokens are proportional across the speech span and cover the
window text exactly (words by whitespace, else per character), which
is what the parent validates.
"""
import json
import math
import sys
import unicodedata
import wave

MAX_CALLS_PER_WINDOW = 64
GROWTH_SECONDS = 3.0
TAIL_SECONDS = 3.0
MAX_SLICE_SECONDS = 180.0


def _clean(word):
    folded = unicodedata.normalize("NFKC", word or "")
    folded = folded.replace("’", "'").replace("‘", "'")
    kept = []
    for ch in folded:
        if ch == "'":
            kept.append(ch)
        elif unicodedata.category(ch).startswith(("L", "N")):
            kept.append(ch)
    return "".join(kept)


def _units(text):
    words = [_clean(w) for w in (text or "").split()]
    words = [w for w in words if w]
    if words:
        return words
    return [_clean(c) for c in (text or "") if not c.isspace() and _clean(c)]


def _slice_tokens(piece, slice_seconds, speech_seconds):
    words = _units(piece)
    if not words:
        raise ValueError("fake worker: window has no words")
    if speech_seconds <= 0:
        # No-progress fixture: tokens ending at zero must be refused
        # by the worker's own forward-progress guard below.
        return [{"text": w, "start_time": 0.0, "end_time": 0.0} for w in words]
    speech = min(speech_seconds, slice_seconds)
    step = speech / float(len(words))
    tokens = []
    for i, word in enumerate(words):
        start = round(i * step, 3)
        end = round((i + 1) * step, 3)
        if end <= start:
            end = round(start + 0.001, 3)
        tokens.append({"text": word, "start_time": start, "end_time": end})
    return tokens


def _align_row(row):
    with wave.open(row["wav_path"], "rb") as stream:
        rate = stream.getframerate()
        total_frames = stream.getnframes()
    text = row["text"]
    windows = row.get("windows") or [{
        "char_start": 0, "char_end": len(text),
        "est_start_sec": 0.0, "est_end_sec": float(row.get("duration_sec") or 0)}]
    speech_offsets = row.get("fake_speech_offsets")
    cursor_frame = 0
    accepted = []
    merged = []
    for index, plan in enumerate(windows):
        piece = text[plan["char_start"]:plan["char_end"]]
        planned_seconds = float(plan["est_end_sec"]) - float(plan["est_start_sec"])
        if speech_offsets is not None:
            speech_end_absolute = float(speech_offsets[index])
        else:
            speech_end_absolute = None
        end_frame = min(total_frames, cursor_frame + int(round(planned_seconds * rate)))
        if end_frame <= cursor_frame:
            raise ValueError("fake worker: planned window has no frames")
        calls = 0
        while True:
            if calls >= MAX_CALLS_PER_WINDOW:
                raise ValueError("fake worker: sequential call bound exhausted")
            calls += 1
            slice_seconds = (end_frame - cursor_frame) / float(rate)
            if speech_end_absolute is not None:
                speech_seconds = speech_end_absolute - cursor_frame / float(rate)
            elif planned_seconds > TAIL_SECONDS:
                speech_seconds = planned_seconds - TAIL_SECONDS
            else:
                speech_seconds = slice_seconds * 0.8
            tokens = _slice_tokens(piece, slice_seconds, speech_seconds)
            last_end = tokens[-1]["end_time"]
            if end_frame >= total_frames \
                    or last_end <= slice_seconds - TAIL_SECONDS + 1e-9:
                break
            cap_frame = cursor_frame + int(round(MAX_SLICE_SECONDS * rate))
            wider = min(total_frames, end_frame + int(round(GROWTH_SECONDS * rate)), cap_frame)
            if wider <= end_frame:
                raise ValueError("fake worker: bounded growth exhausted")
            end_frame = wider
        slice_seconds = (end_frame - cursor_frame) / float(rate)
        if tokens[-1]["end_time"] <= 0:
            raise ValueError("fake worker: no forward progress")
        advance = int(math.ceil(tokens[-1]["end_time"] * rate - 1e-9))
        new_cursor = cursor_frame + advance
        if new_cursor <= cursor_frame:
            raise ValueError("fake worker: no forward progress")
        offset = cursor_frame / float(rate)
        accepted.append({
            "char_start": plan["char_start"], "char_end": plan["char_end"],
            "start_frame": cursor_frame, "end_frame": end_frame,
            "offset_sec": offset, "slice_duration_sec": slice_seconds,
            "calls": calls, "tokens": tokens})
        for token in tokens:
            merged.append({"text": token["text"],
                           "start_time": offset + token["start_time"],
                           "end_time": offset + token["end_time"]})
        cursor_frame = new_cursor
    return merged, accepted


def main(argv):
    with open(argv[1], encoding="utf-8") as handle:
        job = json.load(handle)
    tokens = {}
    windows = {}
    for row in job["rows"]:
        merged, accepted = _align_row(row)
        tokens[row["id"]] = merged
        windows[row["id"]] = accepted
    with open(argv[2], "w", encoding="utf-8") as handle:
        json.dump({"ok": True, "tokens": tokens, "windows": windows,
                   "provider": {"qwen_asr_version": "test-fixture",
                                "device": job.get("device"),
                                "encoder_adapter": "test-fixture",
                                "decoder": "test-fixture"}},
                  handle, ensure_ascii=False)


if __name__ == "__main__":
    main(sys.argv)
