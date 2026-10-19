"""Shared cached whisper.cpp runtime discovery and native timestamp conversion."""
import os
from pathlib import Path
from numbers import Real
import math


def get_whisper_cpp_runtime(root):
    root = Path(root)
    candidates = [root/'whisper.cpp/build/bin/whisper-cli',
                  root/'whisper.cpp/build/bin/Release/whisper-cli.exe',
                  root/'whisper.cpp/build/Release/whisper-cli.exe']
    binary = os.environ.get('ALEXANDRIA_WHISPER_CPP_BIN') or str(next(
        (path for path in candidates if path.is_file()), candidates[0]))
    model = os.environ.get('ALEXANDRIA_WHISPER_CPP_MODEL') or str(root/'models/whisper.cpp/ggml-small.en.bin')
    return {'binary': binary, 'model': model,
            'available': os.path.isfile(binary) and os.access(binary, os.X_OK) and os.path.isfile(model)}


def get_coalesced_whisper_cpp_segments(transcription):
    """Convert whisper.cpp JSON words without inventing word boundaries.

    whisper.cpp can emit a real word with identical start/end offsets. Keep
    its text by joining it to the next positive-duration word (or the prior
    word at EOF). All other malformed values are left for the shared strict
    validator to reject.
    """
    if not isinstance(transcription, list):
        raise ValueError("whisper.cpp transcription must be a list")

    converted = []
    pending = []
    for index, segment in enumerate(transcription):
        if not isinstance(segment, dict):
            raise ValueError(
                f"whisper.cpp transcription segment {index} is not an object")
        text = segment.get("text", "")
        if not isinstance(text, str):
            raise ValueError(
                f"whisper.cpp transcription segment {index} has non-text data")
        text = text.strip()
        if not text:
            continue

        offsets = segment.get("offsets")
        if not isinstance(offsets, dict):
            converted.append({"word": text, "start": None, "end": None})
            continue
        start_ms = offsets.get("from")
        end_ms = offsets.get("to")
        numeric = (
            isinstance(start_ms, Real) and not isinstance(start_ms, bool)
            and isinstance(end_ms, Real) and not isinstance(end_ms, bool)
        )
        finite = (
            numeric and math.isfinite(float(start_ms))
            and math.isfinite(float(end_ms))
        )
        if finite and float(end_ms) == float(start_ms):
            pending.append(text)
            continue

        converted.append({
            "word": " ".join(pending + [text]),
            "start": float(start_ms) / 1000.0 if numeric else start_ms,
            "end": float(end_ms) / 1000.0 if numeric else end_ms,
        })
        pending = []

    if pending:
        if not converted:
            raise ValueError(
                "whisper.cpp returned only zero-duration word segments")
        converted[-1] = dict(converted[-1])
        converted[-1]["word"] += " " + " ".join(pending)
    return converted
