"""Screen labeled WAV endings for possible early TTS stop events.

This is an offline measurement, not a production cutoff detector. A loud
endpoint can be intentional, so the report keeps labels, measurements, and a
confusion matrix visible. It never edits audio and refuses paths outside the
manifest directory.
"""
from __future__ import annotations

import argparse
from array import array
import hashlib
import io
import json
import math
import os
from pathlib import Path
import stat
import struct
import sys
import tempfile
import wave

VERSION = 1
MAX_MANIFEST_BYTES = 4 * 1024**2
MAX_AUDIO_BYTES = 64 * 1024**2
MAX_TOTAL_AUDIO_BYTES = 1024**3
WINDOWS_MS = (5, 10, 20, 30)
DECISION_WINDOW_MS = WINDOWS_MS[0]
LABELS = frozenset(("cut", "clean"))


class EndpointScreenError(ValueError):
    """The bounded endpoint screen refused malformed or unsafe input."""


def _resolve_audio(root, relative):
    if not isinstance(relative, str) or not relative:
        raise EndpointScreenError("audio path is missing")
    try:
        relative.encode(sys.getfilesystemencoding(), "strict")
        candidate = Path(relative)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise EndpointScreenError("audio path escapes the manifest directory")
        current = root
        for part in candidate.parts:
            current = current / part
            if current.is_symlink():
                raise EndpointScreenError("audio path contains a symlink")
        resolved = (root / candidate).resolve()
    except EndpointScreenError:
        raise
    except (OSError, ValueError, UnicodeError) as error:
        raise EndpointScreenError("audio path is invalid") from error
    try:
        resolved.relative_to(root.resolve())
    except ValueError as error:
        raise EndpointScreenError("audio path escapes the manifest directory") from error
    try:
        facts = resolved.stat()
    except OSError as error:
        raise EndpointScreenError(f"audio file is unavailable: {relative}") from error
    if not stat.S_ISREG(facts.st_mode):
        raise EndpointScreenError(f"audio file is not regular: {relative}")
    if facts.st_size > MAX_AUDIO_BYTES:
        raise EndpointScreenError(f"audio file exceeds {MAX_AUDIO_BYTES} bytes: {relative}")
    return resolved, facts


def _wav_data_info(document):
    if len(document) < 12 or document[:4] != b"RIFF" or document[8:12] != b"WAVE":
        raise EndpointScreenError("invalid WAV container")
    if struct.unpack_from("<I", document, 4)[0] != len(document) - 8:
        raise EndpointScreenError("WAV RIFF length does not match the file")
    offset = 12
    while offset + 8 <= len(document):
        name = document[offset:offset + 4]
        size = struct.unpack_from("<I", document, offset + 4)[0]
        start = offset + 8
        if size > len(document) - start:
            raise EndpointScreenError("truncated WAV payload")
        if name == b"data":
            return start, size
        offset = start + size + (size & 1)
    raise EndpointScreenError("WAV data chunk is missing")


def measure_wav_endpoint(path, expected_facts=None):
    path = Path(path)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError) as error:
        raise EndpointScreenError(f"audio file is unavailable: {path.name}") from error
    try:
        with os.fdopen(descriptor, "rb") as handle:
            facts = os.fstat(handle.fileno())
            if not stat.S_ISREG(facts.st_mode):
                raise EndpointScreenError(f"audio file is not regular: {path.name}")
            if facts.st_size > MAX_AUDIO_BYTES:
                raise EndpointScreenError(
                    f"audio file exceeds {MAX_AUDIO_BYTES} bytes: {path.name}")
            if expected_facts is not None and (
                    facts.st_dev != expected_facts.st_dev
                    or facts.st_ino != expected_facts.st_ino
                    or facts.st_size != expected_facts.st_size):
                raise EndpointScreenError(f"audio file changed before read: {path.name}")
            document = handle.read(MAX_AUDIO_BYTES + 1)
            if len(document) != facts.st_size:
                raise EndpointScreenError(f"audio file changed during read: {path.name}")
            document_sha256 = hashlib.sha256(document).hexdigest()
            data_offset, data_size = _wav_data_info(document)
            with wave.open(io.BytesIO(document), "rb") as audio:
                channels, width = audio.getnchannels(), audio.getsampwidth()
                rate, frames = audio.getframerate(), audio.getnframes()
                if channels != 1 or width != 2 or rate <= 0 or frames <= 0:
                    raise EndpointScreenError(
                        "endpoint screen requires non-empty mono PCM16 WAV audio")
                if data_size % (channels * width) or frames * channels * width != data_size:
                    raise EndpointScreenError(
                        f"WAV data is not aligned to complete frames: {path.name}")
                minimum_frames = math.ceil(rate * max(WINDOWS_MS) / 1000)
                if frames < minimum_frames:
                    raise EndpointScreenError(
                        f"audio is shorter than {max(WINDOWS_MS)} ms: {path.name}")
                tail_frames = math.ceil(rate * max(WINDOWS_MS) / 1000)
                tail_bytes = tail_frames * channels * width
                raw = document[data_offset + data_size - tail_bytes:
                               data_offset + data_size]
    except (EOFError, wave.Error) as error:
        raise EndpointScreenError(f"invalid WAV: {path.name}") from error
    except OSError as error:
        raise EndpointScreenError(f"audio read failed: {path.name}") from error
    if len(raw) % 2:
        raise EndpointScreenError(f"truncated WAV payload: {path.name}")
    samples = array("h")
    samples.frombytes(raw)
    if sys.byteorder != "little":
        samples.byteswap()
    if len(samples) != tail_frames:
        raise EndpointScreenError(f"truncated WAV tail: {path.name}")
    metrics = {}
    for window_ms in WINDOWS_MS:
        count = max(1, min(len(samples), math.ceil(rate * window_ms / 1000)))
        tail = samples[-count:]
        rms = math.sqrt(sum(float(value) ** 2 for value in tail) / count)
        metrics[str(window_ms)] = round(
            20.0 * math.log10(max(rms / 32768.0, 1e-12)), 6)
    return {"audio_sha256": document_sha256,
            "sample_rate": rate, "frames": frames,
            "duration_seconds": round(frames / rate, 6),
            "last_sample": samples[-1], "tail_rms_dbfs": metrics}


def build_report(manifest_path, threshold_dbfs=-40.0):
    manifest_path = Path(manifest_path)
    if not math.isfinite(threshold_dbfs) or not -120.0 <= threshold_dbfs <= 0.0:
        raise EndpointScreenError("threshold must be finite and between -120 and 0 dBFS")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    flags |= getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(manifest_path, flags)
        with os.fdopen(descriptor, "rb") as handle:
            facts = os.fstat(handle.fileno())
            if not stat.S_ISREG(facts.st_mode):
                raise EndpointScreenError("manifest is not a regular file")
            if facts.st_size > MAX_MANIFEST_BYTES:
                raise EndpointScreenError("manifest is too large")
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
            if len(raw) != facts.st_size:
                raise EndpointScreenError("manifest changed during read")
    except EndpointScreenError:
        raise
    except (OSError, ValueError) as error:
        raise EndpointScreenError("manifest is unavailable") from error
    if len(raw) > MAX_MANIFEST_BYTES:
        raise EndpointScreenError("manifest is too large")
    try:
        manifest = json.loads(raw)
    except (UnicodeError, ValueError, RecursionError) as error:
        raise EndpointScreenError("manifest is invalid JSON") from error
    rows = manifest.get("rows") if isinstance(manifest, dict) else None
    if (not isinstance(manifest, dict) or type(manifest.get("version")) is not int
            or manifest.get("version") != VERSION
            or not isinstance(rows, list) or not rows or len(rows) > 4096):
        raise EndpointScreenError("manifest must contain 1..4096 version-1 rows")
    root, seen, results = manifest_path.parent.resolve(), set(), []
    total_audio_bytes = 0
    counts = {"true_positive": 0, "false_positive": 0,
              "true_negative": 0, "false_negative": 0}
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
            raise EndpointScreenError("every row needs a non-empty string id")
        try:
            row["id"].encode("utf-8", "strict")
        except UnicodeError as error:
            raise EndpointScreenError("row id is not valid Unicode") from error
        if row["id"] in seen:
            raise EndpointScreenError(f"duplicate row id: {row['id']}")
        seen.add(row["id"])
        label = row.get("label")
        if not isinstance(label, str) or label not in LABELS:
            raise EndpointScreenError(f"row {row['id']} has an invalid label")
        path, facts = _resolve_audio(root, row.get("audio"))
        total_audio_bytes += facts.st_size
        if total_audio_bytes > MAX_TOTAL_AUDIO_BYTES:
            raise EndpointScreenError(
                f"manifest audio work exceeds {MAX_TOTAL_AUDIO_BYTES} bytes")
        measurement = measure_wav_endpoint(path, facts)
        suspicious = (measurement["tail_rms_dbfs"][str(DECISION_WINDOW_MS)]
                      > threshold_dbfs)
        actual_cut = label == "cut"
        bucket = ("true_positive" if suspicious and actual_cut else
                  "false_positive" if suspicious else
                  "false_negative" if actual_cut else "true_negative")
        counts[bucket] += 1
        results.append({"id": row["id"], "label": label,
                        "audio": row["audio"],
                        "suspicious": suspicious, **measurement})
    return {"version": VERSION, "status": "measured_not_production_qualified",
            "caveat": "A loud endpoint can be intentional; labels and listening remain authoritative.",
            "manifest_sha256": hashlib.sha256(raw).hexdigest(),
            "rule": {"window_ms": DECISION_WINDOW_MS, "operator": ">",
                     "threshold_dbfs": threshold_dbfs},
            "confusion_matrix": counts, "rows": results}


def write_new_report(path, report):
    path = Path(path)
    if path.exists() or path.is_symlink():
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".endpoint-report-", dir=path.parent)
    linked = False
    initial = os.fstat(descriptor)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
            source = os.stat(temporary, follow_symlinks=False)
            if (not stat.S_ISREG(source.st_mode)
                    or (initial.st_dev, initial.st_ino)
                    != (source.st_dev, source.st_ino)):
                raise EndpointScreenError("temporary report changed before publication")
            os.link(temporary, path, follow_symlinks=False)
            linked = True
            current = os.stat(path, follow_symlinks=False)
            if (not stat.S_ISREG(current.st_mode)
                    or (initial.st_dev, initial.st_ino)
                    != (current.st_dev, current.st_ino)):
                raise EndpointScreenError("output path changed during publication")
    except BaseException:
        if linked:
            try:
                current = os.stat(path, follow_symlinks=False)
                if (initial.st_dev, initial.st_ino) == (current.st_dev, current.st_ino):
                    path.unlink(missing_ok=True)
            except OSError:
                pass
        raise
    finally:
        try:
            Path(temporary).unlink(missing_ok=True)
        except OSError:
            pass


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--threshold-dbfs", type=float, default=-40.0)
    args = parser.parse_args()
    try:
        report = build_report(args.manifest, args.threshold_dbfs)
    except EndpointScreenError as error:
        message = str(error).encode("unicode_escape", "backslashreplace").decode("ascii")
        print(f"endpoint screen refused input: {message}", file=sys.stderr)
        return 2
    try:
        write_new_report(args.out, report)
    except (EndpointScreenError, FileExistsError, OSError) as error:
        message = str(error).encode("unicode_escape", "backslashreplace").decode("ascii")
        print(f"endpoint screen could not publish report: {message}", file=sys.stderr)
        return 3
    print(json.dumps({"status": report["status"],
                      "confusion_matrix": report["confusion_matrix"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
