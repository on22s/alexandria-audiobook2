"""AA2 read-along sidecar (#1720) — bounded, versioned, frame-bound.

Reads the published MP3s exported audio and recomputes the timeline with the
existing ExportSourceSnapshot / PCMSourceStore / iter_timeline / write_combined_pcm
frame clock (#1379). Word timings are reuse-only (no ASR run).
"""
import hashlib
import json
import math
import re
import unicodedata

from audio_pcm_producer import get_audio_duration_ms, get_pcm_source_shape, get_promoted_pcm_shape
from speech_text import get_speech_preparation
from tts import ensure_audio_export_active, get_trim_bounds_ms

SIDECAR_SCHEMA = "aa2-readalong-sidecar"
SIDECAR_VERSION = 1
MAX_SIDECAR_BYTES = 64 * 1024 * 1024
MAX_ENTRIES = 200000
MAX_WORDS_PER_ENTRY = 4096
MAX_TEXT_CHARS = 20000
_FORBIDDEN_KEYS = {"audio_path", "api_key", "password", "token", "voice", "voice_config",
                   "root_dir", "data_dir", "script_path", "chunks_path", "project_id"}

_STATUSES = {"aligned-estimated", "chunk-estimated", "missing", "ambiguous"}


def get_audio_file_sha256(path, limit=2 * 1024**3):
    """Bounded SHA-256 of the exported file; never loads it whole if huge."""
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as stream:
        while True:
            block = stream.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            if size > limit:
                raise ValueError("Exported audio exceeds the sidecar binding limit.")
            digest.update(block)
    if size == 0:
        raise ValueError("Exported audio is empty.")
    return digest.hexdigest(), size


def get_trim_head_frames(segment, take_layout):
    """Frames of leading silence removed by trim (in take layout)."""
    try:
        from tts import get_trim_bounds_ms as _bounds
        start_ms, _end_ms = _bounds(segment)
    except Exception:
        return None
    if start_ms <= 0:
        return 0
    rate = take_layout[1]
    return int(rate * start_ms / 1000.0)


def map_word_spans_to_frames(cached, chunk_shape, layout, chunk_start_frame, chunk_end_frame,
                              *, trim_head_seconds=None, trim_tail_seconds=None):
    """Map take-relative ASR second spans to exported frames.

    Uses true trim coordinates + promotion rounding; still labelled
    estimated. When trim is unknown the transform is unproven and the
    result is downgraded to ambiguous instead of proportional stretching.
    Returns (words, status). Rules: saturation/overlap -> ambiguous.
    """
    spans = cached.get("spans") or []
    duration = cached.get("duration")
    if not spans or not isinstance(duration, (int, float)) or not duration or duration <= 0:
        # No usable words: chunk-level only.
        return None, "missing"
    # Saturation check (mirrors word_timing: repeated endpoints are a failure).
    seen = [(s.get("start"), s.get("end")) for s in spans]
    if len(seen) > 1 and len(set(seen)) != len(seen):
        return None, "ambiguous"
    chunk_frames = chunk_end_frame - chunk_start_frame
    if chunk_frames <= 0:
        return None, "ambiguous"
    # True trim coordinates are required: proportional stretching of the take
    # duration into the trimmed clip shifts words into removed silence.
    # Without them the transform is unproven.
    if trim_head_seconds is None:
        return None, "ambiguous"
    if (not isinstance(trim_head_seconds, (int, float)) or not math.isfinite(trim_head_seconds)
            or trim_head_seconds < 0 or trim_head_seconds > duration):
        return None, "ambiguous"
    if trim_tail_seconds is not None and (
            not isinstance(trim_tail_seconds, (int, float)) or not math.isfinite(trim_tail_seconds)
            or trim_tail_seconds < 0 or trim_head_seconds + trim_tail_seconds > duration):
        return None, "ambiguous"
    head = float(trim_head_seconds)
    tail = float(trim_tail_seconds or 0.0)
    trimmed_seconds = duration - head - tail
    if trimmed_seconds <= 0:
        return None, "ambiguous"
    # chunk_shape is ((channels, rate, width), frames) in take layout.
    take_rate = None
    if isinstance(chunk_shape, tuple) and len(chunk_shape) == 2:
        take_rate = chunk_shape[0][1]
    if not isinstance(take_rate, int) or take_rate <= 0:
        take_rate = layout[1]
    words = []
    for span in spans:
        start_s, end_s = span.get("start"), span.get("end")
        if (not isinstance(start_s, (int, float)) or not isinstance(end_s, (int, float))
                or not math.isfinite(start_s) or not math.isfinite(end_s)
                or start_s < 0 or end_s <= start_s or end_s > duration + 0.05):
            return None, "ambiguous"
        # Subtract leading trim, then locate within the trimmed span.
        t_start = float(start_s) - head
        t_end = float(end_s) - head
        if t_end <= 0:
            return None, "ambiguous"
        t_start = max(0.0, t_start)
        t_end = min(trimmed_seconds, t_end)
        if t_end <= t_start:
            # Word lies (mostly) in removed tail: cannot place honestly.
            return None, "ambiguous"
        # Promotion: take-rate seconds -> export frames via chunk span.
        # Linear within the trimmed span (ASR decode resamples), but anchored
        # at the trim edge instead of stretching the whole take.
        frac_start = t_start / trimmed_seconds
        frac_end = t_end / trimmed_seconds
        start_frame = chunk_start_frame + int(chunk_frames * frac_start)
        end_frame = chunk_start_frame + int(chunk_frames * frac_end)
        start_frame = max(chunk_start_frame, min(end_frame, start_frame))
        end_frame = max(start_frame + 1, min(chunk_end_frame, end_frame))
        words.append({"from": int(span.get("from", 0)), "to": int(span.get("to", 0)),
                      "start_frame": int(start_frame), "end_frame": int(end_frame)})
        if len(words) > MAX_WORDS_PER_ENTRY:
            return None, "ambiguous"
    if not words:
        return None, "missing"
    for prev, cur in zip(words, words[1:]):
        if cur["start_frame"] < prev["end_frame"]:
            return None, "ambiguous"
    return words, "aligned-estimated"


def get_section_id(index, starts):
    """Section id for a row index from sorted chapter-start offsets."""
    current = "sec-0001"
    for pos in starts:
        if index >= pos:
            current = f"sec-{len([p for p in starts if p <= index]):04d}"
    return current


def build_readalong_sidecar(*, entries, binding, includes_text, cancel_check=None, source_receipt=None):
    """Assemble, validate and serialize a sidecar document.

    entries: list of dicts with keys index, section, speaker, text (or None),
      start_frame, end_frame, pause_frames_after, status, words.
    binding: dict with audio_sha256, audio_bytes, sample_rate, channels,
      sample_width, frames.
    source_receipt: optional book/source identity bound at publication time;
      included verbatim so download can verify the current book/source.
    """
    ensure_audio_export_active(cancel_check)
    if not isinstance(includes_text, bool):
        raise ValueError("Include-text choice must be explicit.")
    if not 0 < len(entries) <= MAX_ENTRIES:
        raise ValueError("Sidecar has no entries or too many.")
    if source_receipt is not None and (
            not isinstance(source_receipt, dict)
            or not re.fullmatch("[0-9a-f]{64}", str(source_receipt.get("book_token", "")))
            or not re.fullmatch("[0-9a-f]{64}", str(source_receipt.get("rows_digest", "")))
            or not re.fullmatch("[0-9a-f]{64}", str(source_receipt.get("source_sha256", "")))
            or ("settings_digest" in source_receipt
                and not re.fullmatch("[0-9a-f]{64}", str(source_receipt.get("settings_digest", ""))))):
        raise ValueError("Sidecar source receipt is invalid.")
    if not isinstance(binding, dict) or not re.fullmatch("[0-9a-f]{64}", binding.get("audio_sha256", "")):
        raise ValueError("Sidecar audio binding is invalid.")
    total_frames = binding.get("frames")
    if not isinstance(total_frames, int) or total_frames <= 0:
        raise ValueError("Sidecar binding frame count is invalid.")
    for key in ("sample_rate", "channels", "sample_width"):
        if not isinstance(binding.get(key), int) or binding[key] <= 0:
            raise ValueError("Sidecar binding layout is invalid.")
    prev_end = 0
    clean_entries = []
    for row in entries:
        ensure_audio_export_active(cancel_check)
        start, end = row.get("start_frame"), row.get("end_frame")
        if (not isinstance(start, int) or not isinstance(end, int)
                or start < 0 or end <= start or end > total_frames or start < prev_end):
            raise ValueError("Sidecar entry frames are invalid or overlapping.")
        status = row.get("status")
        if status not in _STATUSES:
            raise ValueError("Sidecar entry status is invalid.")
        text = row.get("text") if includes_text else None
        if includes_text:
            if not isinstance(text, str) or not text or len(text) > MAX_TEXT_CHARS:
                raise ValueError("Sidecar entry text is missing or oversized.")
            # Reject control chars / surrogates that would corrupt JSON players.
            if any(0xD800 <= ord(c) <= 0xDFFF for c in text) or "\x00" in text:
                raise ValueError("Sidecar entry text is malformed.")
        else:
            text = None
        words = row.get("words")
        if status == "aligned-estimated":
            if not isinstance(words, list) or not words:
                raise ValueError("Aligned sidecar entry is missing its words.")
            for word in words:
                wf = (word.get("start_frame"), word.get("end_frame"))
                if (not isinstance(wf[0], int) or not isinstance(wf[1], int)
                        or wf[0] < start or wf[1] > end or wf[1] <= wf[0]):
                    raise ValueError("Sidecar word frames escape their entry.")
        else:
            words = None
        speaker = row.get("speaker")
        if speaker is not None and (not isinstance(speaker, str) or len(speaker) > 500
                                    or "\x00" in speaker):
            raise ValueError("Sidecar speaker is malformed.")
        pause = row.get("pause_frames_after")
        if not isinstance(pause, int) or pause < 0:
            raise ValueError("Sidecar pause is invalid.")
        section = row.get("section")
        if not isinstance(section, str) or not section or len(section) > 64:
            raise ValueError("Sidecar section is invalid.")
        prev_end = end
        clean_entries.append({"index": int(row.get("index", len(clean_entries))),
                              "section": section, "speaker": speaker, "text": text,
                              "start_frame": start, "end_frame": end,
                              "pause_frames_after": pause, "status": status,
                              "words": words})
    sections = []
    ordered = sorted({row["section"] for row in clean_entries})
    for sid in ordered:
        rows = [r for r in clean_entries if r["section"] == sid]
        sections.append({"id": sid, "title": None,
                         "start_frame": rows[0]["start_frame"],
                         "end_frame": rows[-1]["end_frame"],
                         "entry_count": len(rows)})
    bookmarks = [{"id": f"bm-{i:04d}", "label": f"Section {sid[-4:]}",
                  "start_frame": section["start_frame"],
                  "end_frame": section["end_frame"], "section": sid}
                 for i, section in enumerate(sections)]
    doc = {"schema": SIDECAR_SCHEMA, "version": SIDECAR_VERSION,
           "binding": {"audio_sha256": binding["audio_sha256"],
                       "audio_format": "mp3", "audio_bytes": binding.get("audio_bytes"),
                       "sample_rate": binding["sample_rate"],
                       "channels": binding["channels"],
                       "sample_width": binding["sample_width"],
                       "frames": total_frames,
                       "duration_ms": round(1000 * total_frames / binding["sample_rate"], 3)},
           "includes_text": includes_text,
           "sections": sections, "entries": clean_entries, "bookmarks": bookmarks}
    if source_receipt is not None:
        doc["source"] = dict(source_receipt)
    raw = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(raw) > MAX_SIDECAR_BYTES:
        raise ValueError("Sidecar exceeds its size limit.")
    get_validated_sidecar(raw, expected_audio_sha256=binding["audio_sha256"])
    return doc, raw


def _reject_forbidden(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).casefold() in _FORBIDDEN_KEYS:
                raise ValueError(f"Sidecar discloses forbidden field: {key}")
            _reject_forbidden(item)
    elif isinstance(value, list):
        for item in value:
            _reject_forbidden(item)


def get_validated_sidecar(raw, *, expected_audio_sha256=None):
    """Strictly validate raw sidecar bytes; unknown fields are tolerated,
    but forbidden disclosures, oversized/malformed payloads and frame
    corruption fail closed. Panel/CLI callers map ValueError to 409/400.

    Shared contract with the standalone player: VERSION, size/shape/frame
    bounds, arrays, strings, numeric finiteness, section refs and text
    offsets are all checked here.
    """
    if not isinstance(raw, bytes) or not raw:
        raise ValueError("Sidecar is empty or malformed.")
    if len(raw) > MAX_SIDECAR_BYTES:
        raise ValueError("Sidecar exceeds its size limit.")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("Sidecar is malformed.") from error
    if not isinstance(doc, dict) or doc.get("schema") != SIDECAR_SCHEMA:
        raise ValueError("Unsupported sidecar schema.")
    if doc.get("version") != SIDECAR_VERSION:
        raise ValueError("Unsupported sidecar version.")
    binding = doc.get("binding")
    if not isinstance(binding, dict) or not re.fullmatch("[0-9a-f]{64}", binding.get("audio_sha256", "")):
        raise ValueError("Sidecar binding is missing or invalid.")
    if expected_audio_sha256 is not None:
        if not isinstance(expected_audio_sha256, str) or binding["audio_sha256"] != expected_audio_sha256:
            raise ValueError("Sidecar audio does not match the exported audio bytes.")
    _reject_forbidden(doc)
    entries = doc.get("entries")
    sections = doc.get("sections", [])
    bookmarks = doc.get("bookmarks", [])
    if not isinstance(entries, list) or not 0 < len(entries) <= MAX_ENTRIES:
        raise ValueError("Sidecar entries are missing or oversized.")
    if not isinstance(sections, list) or not isinstance(bookmarks, list):
        raise ValueError("Sidecar sections or bookmarks are malformed.")
    if len(sections) > 10000 or len(bookmarks) > 10000:
        raise ValueError("Sidecar sections or bookmarks are oversized.")
    total = binding.get("frames")
    if type(total) is not int or total <= 0:
        raise ValueError("Sidecar frame count is invalid.")
    for key in ("sample_rate", "channels", "sample_width"):
        if type(binding.get(key)) is not int or binding[key] <= 0:
            raise ValueError("Sidecar binding layout is invalid.")
    if not isinstance(doc.get("includes_text"), bool):
        raise ValueError("Sidecar text choice is missing or malformed.")
    section_ids = set()
    for section in sections:
        if not isinstance(section, dict):
            raise ValueError("Sidecar section is malformed.")
        sid = section.get("id")
        if not isinstance(sid, str) or not sid or len(sid) > 64:
            raise ValueError("Sidecar section id is invalid.")
        section_ids.add(sid)
        s_start, s_end = section.get("start_frame"), section.get("end_frame")
        if (type(s_start) is not int or type(s_end) is not int
                or s_start < 0 or s_end <= s_start or s_end > total):
            raise ValueError("Sidecar section frames are corrupted.")
        if type(section.get("entry_count")) is not int or section["entry_count"] < 0:
            raise ValueError("Sidecar section count is invalid.")
        if section.get("title") is not None and not isinstance(section["title"], str):
            raise ValueError("Sidecar section title is invalid.")
    for bookmark in bookmarks:
        if not isinstance(bookmark, dict):
            raise ValueError("Sidecar bookmark is malformed.")
        b_start, b_end = bookmark.get("start_frame"), bookmark.get("end_frame")
        if (type(b_start) is not int or type(b_end) is not int
                or b_start < 0 or b_end < b_start or b_end > total):
            raise ValueError("Sidecar bookmark frames are corrupted.")
        if not isinstance(bookmark.get("id"), str) or not bookmark["id"]:
            raise ValueError("Sidecar bookmark is missing its id.")
        if bookmark.get("section") is not None and bookmark["section"] not in section_ids:
            raise ValueError("Sidecar bookmark references an unknown section.")
    prev_end = 0
    text = None
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("Sidecar entry is malformed.")
        start, end = entry.get("start_frame"), entry.get("end_frame")
        if (type(start) is not int or type(end) is not int
                or start < 0 or end <= start or end > total or start < prev_end):
            raise ValueError("Sidecar entry frames are corrupted.")
        if entry.get("status") not in _STATUSES:
            raise ValueError("Sidecar entry status is invalid.")
        if entry.get("section") not in section_ids:
            raise ValueError("Sidecar entry references an unknown section.")
        pause = entry.get("pause_frames_after")
        if type(pause) is not int or pause < 0:
            raise ValueError("Sidecar entry pause is invalid.")
        speaker = entry.get("speaker")
        if speaker is not None and (not isinstance(speaker, str) or len(speaker) > 500
                                    or "\x00" in speaker):
            raise ValueError("Sidecar entry speaker is malformed.")
        entry_text = entry.get("text")
        if doc["includes_text"]:
            if not isinstance(entry_text, str) or not entry_text or len(entry_text) > MAX_TEXT_CHARS:
                raise ValueError("Sidecar entry text is missing or oversized.")
            if any(0xD800 <= ord(c) <= 0xDFFF for c in entry_text) or "\x00" in entry_text:
                raise ValueError("Sidecar entry text is malformed.")
        else:
            if entry_text is not None:
                raise ValueError("Sidecar entry discloses text without the text choice.")
            entry_text = None
        words = entry.get("words")
        status = entry.get("status")
        if status == "aligned-estimated":
            if not isinstance(words, list) or not words or len(words) > MAX_WORDS_PER_ENTRY:
                raise ValueError("Sidecar aligned entry words are missing or oversized.")
            prev_word_end = start
            for word in words:
                if not isinstance(word, dict):
                    raise ValueError("Sidecar word is malformed.")
                w_from, w_to = word.get("from"), word.get("to")
                if (type(w_from) is not int or type(w_to) is not int
                        or w_from < 0 or w_to <= w_from):
                    raise ValueError("Sidecar word offsets are invalid.")
                if entry_text is not None and w_to > len(entry_text):
                    raise ValueError("Sidecar word offsets escape their entry text.")
                wf_start, wf_end = word.get("start_frame"), word.get("end_frame")
                if (type(wf_start) is not int or type(wf_end) is not int
                        or wf_start < start or wf_end > end or wf_end <= wf_start
                        or wf_start < prev_word_end):
                    raise ValueError("Sidecar word frames are corrupted.")
                prev_word_end = wf_end
        elif words is not None:
            raise ValueError("Sidecar entry discloses words without aligned status.")
        prev_end = end
    source = doc.get("source")
    if source is not None:
        if not isinstance(source, dict):
            raise ValueError("Sidecar source receipt is malformed.")
        for key in ("book_token", "rows_digest", "source_sha256"):
            if not re.fullmatch("[0-9a-f]{64}", str(source.get(key, ""))):
                raise ValueError("Sidecar source receipt is invalid.")
        # R2 app receipts carry settings_digest; legacy 3-key receipts stay
        # parseable here so the app download can refuse them as unbound.
        if "settings_digest" in source and not re.fullmatch(
                "[0-9a-f]{64}", str(source.get("settings_digest", ""))):
            raise ValueError("Sidecar source receipt is invalid.")
    return doc


def get_normalized_source_text(text):
    """Fold for EPUB/script matching: NFKC, casefold, collapse whitespace."""
    return " ".join(unicodedata.normalize("NFKC", text or "").split()).casefold()


def get_readalong_timing_report(sidecar_doc):
    """Small measurement digest for operators (no listening-quality claims)."""
    entries = sidecar_doc.get("entries", [])
    aliged = sum(1 for e in entries if e.get("status") == "aligned-estimated")
    return {"entries": len(entries), "aligned_estimated": aliged,
            "includes_text": bool(sidecar_doc.get("includes_text")),
            "sidecar_bytes": None, "duration_ms": sidecar_doc.get("binding", {}).get("duration_ms")}


# Adapted from ReadAloudMaker (MIT, 2026 ReadAloudMaker contributors, commit
# 5928795e9188222f75ea4776feabc6e6445d6379): reference-validation idea only
# (every timing must resolve inside the audio timeline). No code copied.
