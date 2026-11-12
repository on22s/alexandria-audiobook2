"""Owned export workers for read-along sidecar (#1720) and EPUB read-aloud (#1779).

Both run inside the existing export ownership pattern: integrity admission,
ExportSourceSnapshot capture, frame-exact PCM timeline, fresh encode through
the ordinary encoder/quality/admission path, verify_sources(), short set
publication. Sidecar/chunk export is CPU-only. Optional Qwen inference
runs only in the owned isolated subprocess; no billable calls.

Audio authority: every build fresh-encodes the MP3 from the current PCM
timeline (export_pcm_mp3 + export-quality admission) and binds the sidecar
or EPUB to that fresh file's exact bytes. A pre-existing cloned_audiobook.mp3
is never trusted, rehashed or embedded: edited text/pauses without a merge
cannot bind a stale MP3, and wrong/changed audio fails closed.

Synchronized coverage: every admitted row must have decodable audio. A
missing/corrupt clip (`_load_chunks_with_audio` skip) fails the whole
build closed before any publication — this code has no legitimate
non-spoken skip policy (chapter export likewise refuses skipped rows),
so a partial timeline is never published as a synchronized artifact.

Frame truth: entry bounds and total frames come from the shared combined
PCM plan (`write_combined_pcm` -> `get_combined_source_plan` /
`get_combined_pcm_plan`, verified against the produced PCM by the
assembler). Bounds are the plan's `source_frame_ranges` in the final
promoted layout; pause gaps are the gaps between those ranges. No
byte-count division by the output width and no rounded `iter_timeline`
anchors govern timing. MP3 decoded sample counts include codec delay /
priming and padding and are never claimed equal to PCM frames; the
sidecar binds PCM frame coordinates, the MP3 is the audible delivery.

Publication: fresh MP3 + sidecar/EPUB (+ EPUB receipt) publish through
`ExportSourceSnapshot.ensure_publication_transaction` with `admit()` per
mutation, backups and reverse rollback (chapter-export pattern). Ordinary
renames are not crash-atomic: a hard kill between renames can leave a
mismatched pair; recovery is a fresh rebuild. Rollback covers ordinary
exceptions (second-target failure, stale admission, cancellation) where
the process survives to restore prior bytes.
"""
import copy
import hashlib
import json
import os
import re
from contextlib import contextmanager
from pathlib import Path

from audio_pcm_producer import PCMSourceStore
from audio_streaming import write_combined_pcm
from export_publication import ExportSourceSnapshot
from export_quality import (get_export_file_identity, get_export_file_version,
                            get_export_quality_options, get_pcm_export_encoding_plan,
                            get_admitted_export_report, ensure_export_stage_current)
from readalong_sidecar import (build_readalong_sidecar, get_audio_file_sha256,
                               map_word_spans_to_frames, get_readalong_timing_report)
from speech_text import get_speech_preparation
from tts import ExportCancelled, ensure_audio_export_active, iter_timeline, get_trim_bounds_ms
from audio_generation_owner import AudioGenerationCancelled

_HEX64_RE = re.compile(r"[0-9a-f]{64}")
MAX_RETAINED_EPUB_BYTES = 2 * 1024**3


@contextmanager
def _open_retained_epub(path, cancel_check=None):
    """Bind a regular source descriptor; read-only backup hardlinks are valid."""
    from audio_take_history import _open_regular, _version
    ensure_audio_export_active(cancel_check)
    try:
        stream, before = _open_regular(
            path, Path(path).parent, MAX_RETAINED_EPUB_BYTES,
            allow_hardlinks=True, file_description="Retained EPUB")
        with stream:
            yield stream
            if (_version(os.fstat(stream.fileno())) != _version(before)
                    or _version(os.lstat(path)) != _version(before)):
                raise ValueError("retained upload changed during read")
    except (ValueError, OSError) as error:
        raise ValueError("Retained EPUB is unavailable or outside its regular-file bound: %s" % error) from error


def get_retained_epub_bytes(path, cancel_check=None):
    """Read one bounded retained upload once, checking its descriptor version."""
    import io
    with _open_retained_epub(path, cancel_check) as stream, io.BytesIO() as content:
        size = 0
        while True:
            ensure_audio_export_active(cancel_check)
            block = stream.read(min(1024**2, MAX_RETAINED_EPUB_BYTES - size + 1))
            if not block:
                break
            size += len(block)
            if size > MAX_RETAINED_EPUB_BYTES:
                raise ValueError("Retained EPUB grew beyond its bound.")
            content.write(block)
        return content.getvalue()


def get_retained_epub_sha256(path, cancel_check=None):
    """Stream the same bounded, version-checked source contract on download."""
    from audio_source_io import read_bounded_audio_hash
    with _open_retained_epub(path, cancel_check) as stream:
        digest, _size = read_bounded_audio_hash(
            stream, MAX_RETAINED_EPUB_BYTES,
            lambda: ensure_audio_export_active(cancel_check))
        return digest

MP3_BITRATE = "128k"


_PREFERENCE_DEVICE_RE = re.compile(r"^(auto|cpu|cuda:[0-9]+)$")
_PREFERENCE_LANGUAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def get_readaloud_timing_preference(data_dir):
    """Persisted per-build timing preference; missing file defaults.

    A present but unreadable or out-of-shape preference is corrupt:
    it raises ValueError so callers can refuse with actionable
    guidance instead of silently running a different mode. A missing
    first-install file remains the valid auto default. In chunk mode
    the language scalar is intentionally not gated (it is irrelevant
    there), but its charset is still bounded so persisted state is
    always a safe token.
    """
    import json as _json
    import stat as _stat
    path = os.path.join(data_dir, "readalong_timing.json")
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {"timing_mode": "auto", "timing_device": "auto",
                "timing_language": None}
    except OSError as error:
        raise ValueError(
            "Persisted timing preference is corrupt (unreadable JSON); "
            "restore a valid preference and rebuild.") from error
    # Bounded regular-file read: three small scalars only, so a
    # present file that is a symlink/FIFO/oversized is corrupt state
    # and is refused explicitly rather than silently defaulted.
    if _stat.S_ISLNK(info.st_mode) or not _stat.S_ISREG(info.st_mode) \
            or info.st_size > 16384:
        raise ValueError(
            "Persisted timing preference is corrupt (not a bounded "
            "regular file); restore a valid preference and rebuild.")
    try:
        flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) \
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if not _stat.S_ISREG(opened.st_mode) or opened.st_size != info.st_size:
                raise ValueError("changed during read")
            data = b""
            while True:
                block = os.read(descriptor, 8192)
                if not block:
                    break
                data += block
                if len(data) > 16384:
                    raise ValueError("oversized")
        finally:
            os.close(descriptor)
        raw = _json.loads(data.decode("utf-8"))
    except (OSError, ValueError, RecursionError) as error:
        raise ValueError(
            "Persisted timing preference is corrupt (unreadable JSON); "
            "restore a valid preference and rebuild.") from error
    return get_validated_readaloud_timing_preference(raw)


def get_validated_readaloud_timing_preference(raw):
    """Validate the same bounded scalar contract before reads and writes."""
    if not isinstance(raw, dict):
        raise ValueError(
            "Persisted timing preference is corrupt (not an object); "
            "restore a valid preference and rebuild.")
    if set(raw) - {"timing_mode", "timing_device", "timing_language"}:
        raise ValueError(
            "Persisted timing preference is corrupt (unknown fields); "
            "restore a valid preference and rebuild.")
    mode = raw.get("timing_mode")
    device = raw.get("timing_device")
    language = raw.get("timing_language")
    if mode not in ("auto", "qwen", "chunk"):
        raise ValueError(
            "Persisted timing preference is corrupt (invalid mode); "
            "restore a valid preference and rebuild.")
    if not isinstance(device, str) or len(device) > 32 or not _PREFERENCE_DEVICE_RE.match(device):
        raise ValueError(
            "Persisted timing preference is corrupt (invalid device); "
            "restore a valid preference and rebuild.")
    if language is not None and (not isinstance(language, str)
                                 or len(language) > 32
                                 or (language and not _PREFERENCE_LANGUAGE_RE.match(language))):
        raise ValueError(
            "Persisted timing preference is corrupt (invalid language); "
            "restore a valid preference and rebuild.")
    return {"timing_mode": mode, "timing_device": device,
            "timing_language": language}


def save_readaloud_timing_preference(preference, data_dir):
    """Persist the user's timing choice; caller holds book state."""
    from utils import atomic_json_write
    preference = get_validated_readaloud_timing_preference(preference)
    return atomic_json_write(preference, os.path.join(data_dir, "readalong_timing.json"),
                             return_written_bytes=True, allow_nonatomic_fallback=False)


def require_timing_preference_unchanged(expected, root_dir):
    """Mid-build fence: the persisted preference must still match admission.

    Internal-builder contract (#1844 raw12/40, adjudicated): the
    strict application route always passes the admission preference
    dict, so a non-dict expectation only occurs for direct internal
    builder invocations (unit builds) and disables the fence there.
    No public/app path can reach publication without the dict.
    """
    if not isinstance(expected, dict):
        return
    current = get_readaloud_timing_preference(root_dir)
    if (current.get("timing_mode") != expected.get("timing_mode")
            or current.get("timing_device") != expected.get("timing_device")
            or current.get("timing_language") != expected.get("timing_language")):
        raise ValueError(
            "Timing settings changed during the build; refusing to publish. "
            "Rebuild with the current settings.")


def get_sidecar_output_path(data_dir):
    return os.path.join(data_dir, "readalong.sidecar.json")


def get_epub_output_path(data_dir):
    return os.path.join(data_dir, "readaloud.epub")


def get_epub_receipt_output_path(data_dir):
    """R2 seam: minimal EPUB receipt published beside the EPUB (same transaction)."""
    return os.path.join(data_dir, "readaloud.receipt.json")


def get_current_settings_digest(manager, pause_defaults):
    """Digest of output-affecting settings (pause defaults + config bytes).

    R2 receipt addition: pauses/config changes can stale a synchronized
    export even when book/rows/source are unchanged, so app receipts bind
    this digest and the download recomputes it from live state.
    """
    from editor_poll_snapshot import get_snapshot_digest
    config_sha = None
    config_path = getattr(manager, "config_path", None)
    if isinstance(config_path, str) and os.path.isfile(config_path):
        config_sha = hashlib.sha256(Path(config_path).read_bytes()).hexdigest()
    return get_snapshot_digest([list(pause_defaults), config_sha])


def get_validated_epub_receipt(raw):
    """Strictly validate the EPUB receipt seam (R2 download consumer).

    Shape: {book_token, rows_digest, source_sha256, settings_digest,
    retained_epub_sha256, audio_sha256, epub_sha256} — all 64-hex.
    No paths are disclosed. Legacy 6-key receipts fail closed as unbound.

    #1844 v2 shape adds {timing_mode, timing_device, timing_dtype,
    timing_language, timing_provider} recording the Qwen/chunk
    choice and provider identity of the artifact. #1844 v3 (new Qwen
    builds) adds human-readable {timing_model, timing_qwen_asr_version}
    (AC8); v2 and legacy receipts still validate and resolve as
    before. Arbitrary extra keys are rejected in every shape.
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > 1024 * 16:
        raise ValueError("EPUB read-aloud receipt is empty or oversized.")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("EPUB read-aloud receipt is malformed.") from error
    if not isinstance(doc, dict):
        raise ValueError("EPUB read-aloud receipt is malformed.")
    legacy = {"book_token", "rows_digest", "source_sha256", "settings_digest",
              "retained_epub_sha256", "audio_sha256", "epub_sha256"}
    timing = {"timing_mode", "timing_device", "timing_dtype", "timing_language",
              "timing_provider"}
    timing_v3 = timing | {"timing_model", "timing_qwen_asr_version"}
    if set(doc) == legacy:
        timing_resolved = "chunk"
    elif set(doc) in (legacy | timing, legacy | timing_v3):
        if doc.get("timing_mode") not in ("qwen", "chunk"):
            raise ValueError("EPUB read-aloud receipt timing mode is invalid.")
        for key in ("timing_device", "timing_dtype", "timing_language"):
            value = doc.get(key)
            if not isinstance(value, str) or not value or len(value) > 128:
                raise ValueError("EPUB read-aloud receipt timing field is invalid.")
        if (not isinstance(doc.get("timing_provider"), str)
                or _HEX64_RE.fullmatch(doc["timing_provider"]) is None):
            raise ValueError("EPUB read-aloud receipt provider identity is invalid.")
        if set(doc) == legacy | timing_v3:
            model = doc.get("timing_model")
            version = doc.get("timing_qwen_asr_version")
            if not isinstance(model, str) or not model or len(model) > 128:
                raise ValueError("EPUB read-aloud receipt timing model is invalid.")
            if (not isinstance(version, str) or not version
                    or len(version) > 64 or version == "unknown"):
                raise ValueError("EPUB read-aloud receipt qwen-asr version is invalid.")
        timing_resolved = doc["timing_mode"]
    else:
        raise ValueError("EPUB read-aloud receipt is malformed.")
    for key in doc:
        if key in timing_v3:
            continue
        if not isinstance(doc[key], str) or _HEX64_RE.fullmatch(doc[key]) is None:
            raise ValueError("EPUB read-aloud receipt identity is invalid.")
    return doc


def get_epub_receipt_timing_mode(receipt):
    """Legacy (7-key) receipts resolve to chunk semantics."""
    if isinstance(receipt, dict) and "timing_mode" in receipt:
        return receipt["timing_mode"]
    return "chunk"


def get_export_report_for_staged(path, extra=None):
    identity = get_export_file_identity(path)
    report = dict(identity)
    if extra:
        report.update(extra)
    return report


def _bounds_from_plan(plan, expected_count):
    """Entry bounds/total from the shared combined PCM plan (frame truth).

    The assembler already verified ``plan`` against the produced PCM
    (``_publish_pcm_plan``). We reuse its ``source_frame_ranges`` — already
    in the final promoted layout with ``(gap + source)`` promotion,
    prefix resampling and silence promotion applied — rather than
    re-deriving sizes from byte counts or rounded ms anchors. A zero-frame
    or overlapping range fails closed; no 1-frame is ever invented.
    """
    if not isinstance(plan, dict):
        raise ValueError("PCM assembler did not provide a frame plan.")
    layout = (plan.get("channels"), plan.get("frame_rate"), plan.get("sample_width"))
    total_frames = plan.get("frames")
    ranges = plan.get("source_frame_ranges")
    if (len(layout) != 3 or any(type(v) is not int or v <= 0 for v in layout)
            or type(total_frames) is not int or total_frames <= 0
            or not isinstance(ranges, list) or len(ranges) != expected_count):
        raise ValueError("PCM frame plan disagrees with the timeline.")
    bounds = []
    prev_end = 0
    for value in ranges:
        if (not isinstance(value, (list, tuple)) or len(value) != 2
                or any(type(v) is not int or isinstance(v, bool) for v in value)):
            raise ValueError("PCM frame plan has a malformed source range.")
        start, end = int(value[0]), int(value[1])
        if end <= start:
            raise ValueError(
                "Synchronized build refused: an admitted clip has no "
                "representable PCM frames in the export timeline.")
        if start < prev_end or end > total_frames:
            raise ValueError("PCM frame plan source ranges overlap or escape the total.")
        bounds.append((start, end))
        prev_end = end
    return bounds, layout, int(total_frames)


def _row_identity(row):
    return (row.get("uid") if isinstance(row.get("uid"), str) else None,
            row.get("id") if isinstance(row.get("id"), int) and not isinstance(row.get("id"), bool) else None)


def _require_full_admitted_coverage(source_snapshot, chunks_with_audio, skipped):
    """Synchronized artifacts require every admitted row, in order.

    Validates real identities (uid/id) against the snapshot, and the
    skipped outcome. A mock or future loader that lies about ``skipped``
    still fails when a row is missing/reordered. There is no explicit
    non-spoken skip policy: every admitted row must decode.
    """
    if not isinstance(skipped, int) or isinstance(skipped, bool) or skipped < 0:
        raise ValueError("Audio loader reported an invalid skipped count.")
    if skipped:
        raise ValueError(
            f"Synchronized read-along build refused: {skipped} admitted "
            "chunk(s) have missing/corrupt audio. Generate the audio and "
            "rebuild; a partial timeline is not a synchronized export.")
    snapshot_rows = source_snapshot.chunks
    if len(chunks_with_audio) != len(snapshot_rows):
        raise ValueError(
            "Synchronized read-along build refused: admitted audio does "
            "not cover every editor row. Rebuild after regenerating the "
            "audio.")
    loaded = [chunk for chunk, _segment in chunks_with_audio]
    if loaded == snapshot_rows:
        return
    for position, (got, want) in enumerate(zip(loaded, snapshot_rows)):
        if _row_identity(got) != _row_identity(want) or got != want:
            raise ValueError(
                "Synchronized read-along build refused: admitted audio "
                f"row {position} does not match the editor row. Retry the export.")


def _publish_pair_transaction(source_snapshot, pairs, cancel_check, staging_path, retain):
    """Publish staged (source, target, report) pairs with rollback.

    ``retain`` is a one-element list the caller supplies; it is set when
    rollback itself fails so the caller keeps the staging directory
    (backups + recovery files) instead of erasing it. Mirrors the chapter
    export pattern (``ensure_publication_transaction`` + ``admit()`` per
    mutation); publication keeps the book/owner through rollback.
    """
    from direct_path_admission import is_plain_path

    outputs = {source: report for source, _target, report in pairs}
    backup_dir = Path(staging_path) / "old"
    backup_dir.mkdir(exist_ok=True)
    touched = []

    def publish(source, target):
        index = len(touched)
        backup = str(backup_dir / f"{index}-{Path(target).name}")
        if not is_plain_path(target):
            raise ValueError("Read-along publication target must not use file or directory aliases.")
        existed = os.path.lexists(target)
        if existed:
            get_export_file_version(target)
            os.replace(target, backup)
        touched.append((target, backup, existed))
        os.replace(source, target)
        report = outputs.pop(source)
        version = get_export_file_version(target)
        if version[:4] != report["file_version"][:4]:
            raise ValueError("Finished read-along output changed during publication. Retry the export.")
        outputs[target] = {**report, "file_version": version}

    with source_snapshot.ensure_publication_transaction(outputs) as admit:
        try:
            for source, target, _report in pairs:
                with admit():
                    publish(source, target)
                ensure_audio_export_active(cancel_check)
        except BaseException as error:
            failures = []
            for target, backup, existed in reversed(touched):
                try:
                    if existed:
                        os.replace(backup, target)
                    elif os.path.lexists(target):
                        os.unlink(target)
                except OSError as rollback_error:
                    failures.append(f"{target}: {rollback_error}")
            if failures:
                retain[0] = True
                raise RuntimeError(
                    "Read-along export rollback failed; recovery files kept at "
                    f"{staging_path}: " + "; ".join(failures)) from error
            raise


def _resolve_word_runtime(manager, book_token=None, runtime=None):
    """Resolve (book_token, runtime) via the real helpers; None = reuse off."""
    resolved_book_token = book_token
    resolved_runtime = runtime
    if resolved_book_token is None or resolved_runtime is None:
        try:
            from whisper_cpp_runtime import get_whisper_cpp_runtime
            from editor_poll_snapshot import get_snapshot_digest
            from book_state_transaction import get_book_operation_identity
            resolved_book_token = get_snapshot_digest(
                get_book_operation_identity(manager.root_dir))
            root_dir = os.path.dirname(manager.script_path) if hasattr(manager, "script_path") else manager.root_dir
            rt = get_whisper_cpp_runtime(root_dir)
            if rt.get("available"):
                resolved_runtime = {**rt, **{k: str(Path(rt[k]).resolve()) for k in ("binary", "model")}}
            else:
                resolved_runtime = None
        except Exception:
            resolved_runtime = None
    return resolved_book_token, resolved_runtime


def _get_word_cache_for_row(manager, index, row, book_token, runtime, cancel_check):
    """Reuse-only lookup: never runs ASR. Returns (cached_dict|None, ambiguous)."""
    from editor_word_timing import get_cached_word_timing, get_timing_cache_path, get_timing_inputs
    uid = row.get("uid")
    revision = row.get("audio_revision")
    try:
        binding, _path = get_timing_inputs(manager, index, book_token, uid, revision, runtime,
                                           lambda i, t, u: _verify_identity(manager, i, t, u))
    except Exception:
        # Missing take, changed take, or unavailable provider: missing, never invented.
        return None, False
    cache = get_timing_cache_path(manager, book_token, uid)
    try:
        cached = get_cached_word_timing(cache, binding)
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as error:
        # A present-but-invalid cache is ambiguous, not missing.
        if "ambiguous" in str(error).lower() or "invalid" in str(error).lower() or "malformed" in str(error).lower():
            return None, True
        return None, False
    if cached is None:
        return None, False
    return cached, False


def _verify_identity(manager, index, book_token, uid):
    # Caller holds book state via the snapshot pattern; keep this pure.
    chunks = manager.load_chunks() if hasattr(manager, "load_chunks") else []
    if not 0 <= index < len(chunks) or chunks[index].get("uid") != uid:
        raise ValueError("Editor row changed during sidecar build.")


def _live_index_map(manager):
    """Map uid -> live row index for get_timing_inputs (real helper)."""
    try:
        live = manager.load_chunks() if hasattr(manager, "load_chunks") else []
    except Exception:
        return {}
    mapping = {}
    for idx, row in enumerate(live):
        if isinstance(row, dict) and isinstance(row.get("uid"), str):
            mapping.setdefault(row["uid"], idx)
    return mapping


def _assemble_with_store(manager, source_snapshot, cancel_check, staging_path, sources):
    """Shared timeline + PCM + fresh encode. Returns (timeline_data, fresh)."""
    pause_ms, same_speaker_pause_ms = source_snapshot.pause_defaults
    chunks_with_audio, skipped = manager._load_chunks_with_audio(
        cancel_check=cancel_check, chunks=source_snapshot.chunks,
        segment_transform=sources.save_segment)
    if not chunks_with_audio:
        raise ValueError("No audio segments found")
    # Synchronized coverage fails closed before any PCM/publication work.
    _require_full_admitted_coverage(source_snapshot, chunks_with_audio, skipped)
    orig_tokens = [token for _chunk, token in chunks_with_audio]
    decoded = ((chunk, sources.load_segment(token)) for chunk, token in chunks_with_audio)
    timeline = []
    for chunk, segment, start_ms in iter_timeline(
            decoded, pause_ms, same_speaker_pause_ms, cancel_check=cancel_check):
        timeline.append((chunk, sources.save_segment(segment), start_ms))
        del segment
    if not timeline:
        raise ValueError("No audio segments found")
    if len(timeline) != len(source_snapshot.chunks):
        raise ValueError("Synchronized read-along build refused: timeline lost admitted rows.")
    segments = [seg for _chunk, seg, _start in timeline]
    speakers = [chunk["speaker"] for chunk, _seg, _start in timeline]
    pause_overrides = [chunk.get("pause_after") for chunk, _seg, _start in timeline]
    raw = str(staging_path / "combined.pcm")
    plans = []
    settings = write_combined_pcm(segments, speakers, raw, pause_ms,
                                  same_speaker_pause_ms, pause_overrides, cancel_check,
                                  source_store=sources, pcm_plan_callback=plans.append)
    if settings is None:
        raise ValueError("Audio layout is not frame-plan compatible; read-along build refused.")
    if len(plans) != 1:
        raise ValueError("PCM assembler did not provide a frame plan.")
    plan = plans[0]
    bounds, layout, total_frames = _bounds_from_plan(plan, len(timeline))
    # Independent PCM check: file size must be exactly plan frames.
    pcm_bytes = os.path.getsize(raw)
    expected_bytes = int(total_frames) * int(layout[0]) * int(layout[2])
    if pcm_bytes != expected_bytes:
        raise ValueError("Produced PCM disagrees with its frame plan; refusing to publish.")
    # Fresh encode through the ordinary encoder + quality admission path.
    quality_options = get_export_quality_options(None)
    quality_plan = get_pcm_export_encoding_plan(plan, format_name='mp3', options=quality_options)
    fresh_mp3 = str(staging_path / "cloned_audiobook.mp3")
    quality_reports = []
    from audio_export import export_pcm_mp3
    export_pcm_mp3(raw, fresh_mp3, settings, cancel_check, bitrate=MP3_BITRATE,
                   quality_plan=quality_plan, quality_callback=quality_reports.append)
    quality_report = get_admitted_export_report(
        fresh_mp3, quality_plan,
        quality_reports[-1] if quality_reports else None, cancel_check)
    ensure_export_stage_current(fresh_mp3, quality_report, cancel_check)
    fresh_sha256, fresh_bytes = get_audio_file_sha256(fresh_mp3, cancel_check=cancel_check)
    if quality_report.get("sha256") != fresh_sha256:
        raise ValueError("Fresh MP3 identity disagrees with quality admission; refusing to publish.")
    timeline_data = {"timeline": timeline, "orig_tokens": orig_tokens, "bounds": bounds,
                     "layout": layout, "total_frames": int(total_frames),
                     "pause_ms": pause_ms, "same_speaker_pause_ms": same_speaker_pause_ms,
                     "skipped": skipped, "live_map": _live_index_map(manager),
                     "pcm_path": raw}
    fresh = {"path": fresh_mp3, "sha256": fresh_sha256, "bytes": fresh_bytes,
             "quality_report": quality_report}
    return timeline_data, fresh


def _captured_source_path(source_snapshot, exclude=()):
    """The single captured original-source path (``input_file_path``).

    It is the only snapshot identity key that is neither metadata
    (state/script/config) nor a resolved chunk-audio path nor an
    explicitly supplied extra. Any other count fails closed.
    """
    snapshot = source_snapshot
    manager = snapshot.manager
    state_path = os.path.join(snapshot.root, "state.json")
    known = {state_path,
             getattr(manager, "script_path", None),
             snapshot.path_contract.get("script_path") if isinstance(snapshot.path_contract, dict) else None,
             getattr(manager, "config_path", None),
             snapshot.path_contract.get("config_path") if isinstance(snapshot.path_contract, dict) else None}
    known.discard(None)
    known.update(set(snapshot.paths.values()))
    known.update(set(exclude))
    candidates = [path for path in snapshot.identities if path not in known]
    if len(candidates) != 1:
        raise ValueError(
            "Read-along source capture is ambiguous; refusing to publish "
            "without a single bound source identity.")
    return candidates[0]


def _require_app_source_receipt(source_snapshot, exclude=()):
    """Required app publication receipt from captured snapshot facts.

    Uses ``ExportSourceSnapshot`` facts for book, rows, source and pauses.
    The settings digest also reads config bytes, whose captured identity
    is rechecked before publication. An unavailable identity raises because the
    app download must be able to verify book/rows/source. The portable
    sidecar helper keeps an optional receipt for standalone use; app
    builds always pass the receipt returned here.
    """
    from editor_poll_snapshot import get_snapshot_digest

    book_token = get_snapshot_digest(list(source_snapshot.book))
    rows_digest = source_snapshot.saved_rows_digest
    if not isinstance(book_token, str) or _HEX64_RE.fullmatch(book_token) is None:
        raise ValueError("Read-along book identity is unavailable; refusing to publish.")
    if not isinstance(rows_digest, str) or _HEX64_RE.fullmatch(rows_digest) is None:
        raise ValueError("Read-along editor-rows identity is unavailable; refusing to publish.")
    source_path = _captured_source_path(source_snapshot, exclude=exclude)
    identity = source_snapshot.identities.get(source_path)
    if (not isinstance(identity, dict)
            or not isinstance(identity.get("sha256"), str)
            or _HEX64_RE.fullmatch(identity["sha256"]) is None):
        raise ValueError("Read-along source identity is unavailable; refusing to publish.")
    # Bind captured pauses plus the current config bytes. The snapshot's
    # source verification rejects config drift before publication, so a
    # receipt cannot publish with settings from a different source version.
    settings_digest = get_current_settings_digest(
        source_snapshot.manager, source_snapshot.pause_defaults)
    return {"book_token": book_token, "rows_digest": rows_digest,
            "source_sha256": identity["sha256"],
            "settings_digest": settings_digest}


def _source_receipt(manager, source_snapshot):
    """Backwards-compatible thin wrapper (now fail-closed)."""
    return _require_app_source_receipt(source_snapshot)


# Signed-8 internal PCM -> unsigned-8 WAV bytes (bias 128), the same
# rule as audio_streaming/audio_export (audioop.bias(data, 1, 128)).
# A 256-byte table keeps the conversion bounded and streaming.
_SIGNED8_TO_UNSIGNED8 = bytes((value + 128) % 256 for value in range(256))


def _write_row_slice_wav(pcm_stream, path, layout, start_frame, end_frame,
                         cancel_check=None):
    """Write one row's PCM slice as a WAV for the Qwen worker.

    Supports the core PCM contract widths (1, 2, 4) — the pinned
    official Qwen decode path (soundfile/librosa into mono float32)
    imposes no 16-bit requirement. Widths 2/4 (signed little-endian)
    are written byte-exact; width 1 is internal SIGNED PCM biased to
    UNSIGNED WAV bytes (bias 128) once per streamed block. Rate,
    channels, exact frame range and waveform are preserved; source
    PCM/MP3 data is never modified (staging copy only). Streams in
    1 MiB blocks so an up-to-180 s slice is never held twice in
    memory, and polls cancellation during the copy.
    """
    channels, rate, width = layout
    if type(width) is not int or width not in (1, 2, 4):
        raise ValueError(
            "Qwen alignment staging needs standard PCM samples of "
            "width 1, 2 or 4; this export uses %r-byte samples." % (width,))
    frame_bytes = channels * width
    n_frames = int(end_frame) - int(start_frame)
    if n_frames <= 0:
        raise ValueError("Alignment slice has no frames.")
    pcm_stream.seek(int(start_frame) * frame_bytes)
    remaining = n_frames * frame_bytes
    import wave as _wave
    with _wave.open(str(path), "wb") as stream:
        stream.setnchannels(channels)
        stream.setsampwidth(width)
        stream.setframerate(rate)
        while remaining > 0:
            if cancel_check is not None and cancel_check():
                raise ExportCancelled("Export cancelled")
            block = pcm_stream.read(min(1024**2, remaining))
            if not block:
                raise ValueError("Assembled PCM is shorter than the frame plan.")
            if width == 1:
                block = block.translate(_SIGNED8_TO_UNSIGNED8)
            stream.writeframes(block)
            remaining -= len(block)
    return n_frames / float(rate)


def _validate_worker_provider(provider, capability):
    """Strict production check of the worker's provider report.

    Text/time structural validation cannot prove which model
    executed, so the report returned by the worker is compared
    against the pinned job/runtime contract (device, dtype,
    qwen-asr version, decoder/adapter report) before any token is
    published. A missing or mismatched report refuses the build.
    This is the strict production entry; legacy component fixtures
    must supply a matching report rather than bypass this check.
    """
    if not isinstance(provider, dict) or not provider:
        raise ValueError(
            "Qwen alignment worker returned no provider report; "
            "refusing to publish unverified timings.")
    device = provider.get("device")
    if not isinstance(device, str) or not device:
        raise ValueError("Qwen alignment worker provider device is missing.")
    pinned_device = capability.get("device") if isinstance(capability, dict) else None
    if pinned_device and device != pinned_device:
        raise ValueError(
            "Qwen alignment worker ran on device %r, not the pinned "
            "%r; refusing to publish." % (device, pinned_device))
    dtype = provider.get("dtype")
    if not isinstance(dtype, str) or not dtype:
        raise ValueError("Qwen alignment worker provider dtype is missing.")
    pinned_dtype = capability.get("dtype") if isinstance(capability, dict) else None
    if pinned_dtype and dtype != pinned_dtype:
        raise ValueError(
            "Qwen alignment worker ran with dtype %r, not the pinned "
            "%r; refusing to publish." % (dtype, pinned_dtype))
    version = provider.get("qwen_asr_version")
    if not isinstance(version, str) or version in ("", "unknown"):
        raise ValueError(
            "Qwen alignment worker qwen-asr version is unverified; "
            "refusing to publish.")
    pinned_version = capability.get("qwen_asr_version") if isinstance(capability, dict) else None
    if pinned_version and version != pinned_version:
        raise ValueError(
            "Qwen alignment worker reported qwen-asr %r, not the pinned "
            "%r; refusing to publish." % (version, pinned_version))
    for key in ("decoder", "encoder_adapter"):
        value = provider.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(
                "Qwen alignment worker provider %s report is missing." % key)


def _require_qwen_provider_current(timing_runtime, capability, timing, cancel_check):
    """Recheck captured runtime/model/code before launch and publication."""
    from qwen_runtime import (get_qwen_provider_digest,
                              get_qwen_runtime_digest,
                              get_qwen_runtime_identity)
    try:
        fresh_runtime = get_qwen_runtime_identity(
            timing_runtime["python"], cancel_check=cancel_check)
        if (get_qwen_runtime_digest(fresh_runtime)
                != timing_runtime.get("runtime_digest")):
            raise ValueError(
                "Qwen runtime changed during the build (interpreter "
                "or package replaced); refusing to publish.")
        current_digest = get_qwen_provider_digest(
            capability["model_dir"], runtime_identity={
                "qwen_asr_version": timing.get("qwen_asr_version"),
                "torch_version": timing.get("torch_version"),
                "device": timing.get("device"),
                "dtype": timing.get("dtype"),
                "identity": fresh_runtime},
            cancel_check=cancel_check)
    except ValueError as error:
        if cancel_check() and "cancelled" in str(error).lower():
            raise ExportCancelled("Export cancelled") from error
        raise ValueError(str(error)) from error
    if current_digest != timing.get("provider_digest"):
        raise ValueError(
            "Qwen provider changed during the build (model or "
            "interpreter replaced); refusing to publish.")


def collect_aligned_rows(data, language_name, capability, cancel_check, staging_path,
                         task_lease_fd=None, prepared_texts=None, before_worker=None,
                         worker_env=None):
    """Run real Qwen alignment against the captured assembled PCM.

    Audio comes from ``data["pcm_path"]`` at the exact promoted
    ``source_frame_ranges`` — never from rounded timeline anchors. One
    streamed row WAV is written per row (never the whole book, never
    one WAV per window); the single loaded-model worker then performs
    bounded sequential slices per row: measured previous-end cursor,
    20-second estimated text target, 3-second tail and 3-second
    growth, every real call within the 180-second cap. Window guesses
    only schedule inference: the parent independently validates the
    worker's actual windows (frame offsets, text scopes, call bound)
    and re-runs the strict token + exact-coverage guards per window
    and on the merged row. Returns (aligned_rows, report) where
    aligned_rows fits build_epub_readaloud's aligned_rows shape and
    tokens carry row-relative seconds.
    """
    from qwen_align import (MAX_TEXT_CHARS, count_units, cover_ranges,
                            get_aligner_windows, split_sentences,
                            validate_sequential_windows)
    from qwen_runtime import (MAX_JOB_BYTES, get_qwen_worker_command,
                              run_qwen_alignment_full)

    layout = data["layout"]
    bounds = data["bounds"]
    timeline = data["timeline"]
    pcm_path = data["pcm_path"]
    align_dir = Path(staging_path) / "qwen"
    align_dir.mkdir(parents=True, exist_ok=True)

    if len(timeline) > 10000 or len(bounds) > 10000:
        raise ValueError("Qwen alignment refused: too many rows for one build.")
    # Planning pass: bounds are enforced before any WAV is written and
    # before inference, so an oversized book fails without disk churn.
    rows = []
    total_windows = 0
    total_chars = 0
    estimated_job_bytes = 0
    for position, ((chunk, _segment, _start_ms), (start_frame, end_frame)) in enumerate(zip(timeline, bounds)):
        ensure_audio_export_active(cancel_check)
        if prepared_texts is not None:
            if position >= len(prepared_texts) or not isinstance(prepared_texts[position], str):
                raise ValueError("Qwen alignment refused: missing prepared text for row %d." % position)
            prepared = prepared_texts[position]
        else:
            prepared = get_speech_preparation(chunk.get("text", ""), chunk.get("instruct"))["text"]
        if len(prepared) > MAX_TEXT_CHARS:
            raise ValueError("Qwen alignment refused: row %d exceeds its size limit." % position)
        sentences = cover_ranges(prepared, split_sentences(prepared))
        if not sentences:
            raise ValueError("Qwen alignment refused: row %d has no sentences." % position)
        total_frames = int(end_frame) - int(start_frame)
        row_duration = total_frames / float(layout[1])
        units = [{"start": s, "end": e, "words": count_units(prepared[s:e])}
                 for s, e in sentences]
        try:
            windows = get_aligner_windows(row_duration, units, text=prepared)
        except ValueError as error:
            raise ValueError("Qwen alignment row %d could not be planned: %s" % (position, str(error)[:512])) from error
        total_windows += len(windows)
        total_chars += len(prepared)
        # The actual job payload carries each row's text once plus
        # per-window metadata (char offsets/estimates, no piece text),
        # so the estimate counts prepared once, not twice via pieces.
        estimated_job_bytes += len(prepared.encode("utf-8")) + 512 * len(windows)
        for window in windows:
            piece = prepared[window["char_start"]:window["char_end"]]
            if not piece.strip():
                raise ValueError("Qwen alignment window has no text.")
        if total_windows > 10000 or total_chars > 10 * 1024 * 1024 \
                or estimated_job_bytes > MAX_JOB_BYTES:
            raise ValueError("Qwen alignment refused: book exceeds its size limit.")
        rows.append({"position": position, "text": prepared,
                     "start_frame": int(start_frame), "end_frame": int(end_frame),
                     "duration": row_duration, "windows": windows,
                     "total_frames": total_frames})
    jobs = []
    with open(pcm_path, "rb") as pcm_stream:
        for row in rows:
            position = row["position"]
            start_frame, end_frame = row["start_frame"], row["end_frame"]
            ensure_audio_export_active(cancel_check)
            wav_path = align_dir / ("row-%d.wav" % position)
            _write_row_slice_wav(
                pcm_stream, wav_path, layout,
                int(start_frame), int(end_frame),
                cancel_check=cancel_check)
            jobs.append({
                "id": "r%d" % position,
                "wav_path": str(wav_path), "text": row["text"],
                "duration_sec": row["duration"],
                "windows": [{
                    "char_start": window["char_start"],
                    "char_end": window["char_end"],
                    "est_start_sec": window["start_sec"],
                    "est_end_sec": window["end_sec"],
                } for window in row["windows"]],
            })

    job_path = align_dir / "job.json"
    result_path = align_dir / "result.json"
    slice_dir = align_dir / "slices"
    slice_dir.mkdir(parents=True, exist_ok=True)
    job_payload = json.dumps({
        "model_dir": capability["model_dir"], "device": capability["device"],
        "dtype": capability.get("dtype"), "language": language_name,
        "qwen_asr_version": capability.get("qwen_asr_version"),
        "slice_dir": str(slice_dir),
        "rows": jobs}, ensure_ascii=False)
    job_bytes = job_payload.encode("utf-8")
    if len(job_bytes) > MAX_JOB_BYTES:
        raise ValueError("Qwen alignment job exceeds its size limit.")
    job_path.write_bytes(job_bytes)
    if before_worker is not None:
        before_worker()
    tokens_by_id, windows_by_id, provider = run_qwen_alignment_full(
        get_qwen_worker_command(capability["python"]), job_path, result_path,
        cancel_check, task_lease_fd=task_lease_fd, worker_env=worker_env)
    _validate_worker_provider(provider, capability)

    expected_ids = {"r%d" % row["position"] for row in rows}
    if set(tokens_by_id) != expected_ids or set(windows_by_id) != expected_ids:
        raise ValueError(
            "Qwen alignment worker returned wrong row keys; refusing to publish.")

    aligned_rows = []
    window_total = 0
    for row in rows:
        row_id = "r%d" % row["position"]
        try:
            row_tokens = validate_sequential_windows(
                row["text"], row["windows"], windows_by_id.get(row_id),
                tokens_by_id.get(row_id),
                sample_rate=int(layout[1]), total_frames=int(row["total_frames"]),
                row_duration_sec=row["duration"])
        except ValueError as error:
            raise ValueError("Qwen alignment row %d failed validation: %s" % (row['position'], str(error)[:512])) from error
        aligned_rows.append({"tokens": row_tokens})
        window_total += len(row["windows"])
    report = {"rows": len(aligned_rows), "windows": window_total,
              "slices": window_total}
    return aligned_rows, report


def build_sidecar_export(manager, cancel_check, progress_callback=None, *,
                         includes_text=False, chunks=None, audio_path=None,
                         book_token=None, runtime=None):
    """Build the sidecar bound to a freshly encoded MP3. Returns (ok, msg)."""
    from export_staging import create_export_staging
    from project import _loading_progress
    audio_path = audio_path or os.path.join(manager.root_dir, "cloned_audiobook.mp3")
    output_path = get_sidecar_output_path(manager.root_dir)
    staging = create_export_staging(manager.root_dir, "sidecar")
    pending = str(staging.path / "readalong.sidecar.json")
    retain = [False]
    try:
        source_snapshot = ExportSourceSnapshot(manager, chunks, cancel_check)
        # App builds require the receipt seam before any assembly.
        app_receipt = _require_app_source_receipt(source_snapshot)
        with PCMSourceStore(manager.root_dir, lambda: ensure_audio_export_active(cancel_check)) as sources:
            data, fresh = _assemble_with_store(
                manager, source_snapshot, cancel_check, staging.path, sources)
            timeline = data["timeline"]
            bounds = data["bounds"]
            layout = data["layout"]
            total_frames = data["total_frames"]
            live_map = data["live_map"]
            resolved_book_token, resolved_runtime = _resolve_word_runtime(manager, book_token, runtime)
            entries = []
            for position, ((chunk, segment, start_ms), (start_frame, end_frame)) in enumerate(zip(timeline, bounds)):
                ensure_audio_export_active(cancel_check)
                prepared = get_speech_preparation(chunk.get("text", ""), chunk.get("instruct"))["text"]
                text = prepared if includes_text else None
                # Pause frames are the actual gaps in the produced PCM
                # (next start - current end); defaults/overrides were baked
                # into the shared plan that produced those bounds.
                if position + 1 < len(bounds):
                    pause_frames = int(bounds[position + 1][0] - end_frame)
                else:
                    pause_frames = 0
                entries.append({"index": position, "chunk": chunk, "prepared": prepared,
                                "text": text, "start_frame": start_frame, "end_frame": end_frame,
                                "pause_frames_after": pause_frames,
                                "speaker": chunk.get("speaker")})
            # Sections are the chapters every other export uses (#1921): one
            # decision, so a status window is not a section here alone.
            starts = sorted({0} | {first for _, first, _ in manager._chapter_groups(
                [row["chunk"] for row in entries], False)}) if entries else []
            for position, row in enumerate(entries):
                row["section"] = f"sec-{len([p for p in starts if p <= position]):04d}"
                status, words = "missing", None
                if resolved_runtime is not None and resolved_book_token is not None:
                    uid = row["chunk"].get("uid")
                    live_index = live_map.get(uid, position) if isinstance(uid, str) else position
                    cached, ambiguous = _get_word_cache_for_row(
                        manager, live_index, row["chunk"], resolved_book_token,
                        resolved_runtime, cancel_check)
                    if ambiguous:
                        status = "ambiguous"
                    elif cached is not None:
                        chunk_shape = None
                        trim_head_seconds = None
                        trim_tail_seconds = None
                        try:
                            from audio_pcm_producer import get_pcm_source_shape
                            original = sources.load_segment(data["orig_tokens"][position])
                            chunk_shape = get_pcm_source_shape(original)
                            head_ms, end_ms = get_trim_bounds_ms(original, cancel_check=cancel_check)
                            trim_head_seconds = head_ms / 1000.0
                            trim_tail_seconds = max(0, (len(original) - end_ms) / 1000.0)
                        except Exception:
                            status = "ambiguous" if trim_head_seconds is None else status
                            words = None
                            chunk_shape = None
                        if chunk_shape is not None and trim_head_seconds is not None:
                            words, status = map_word_spans_to_frames(
                                cached, chunk_shape, layout, row["start_frame"], row["end_frame"],
                                trim_head_seconds=trim_head_seconds,
                                trim_tail_seconds=trim_tail_seconds or 0.0)
                            if status == "aligned-estimated" and not words:
                                status = "ambiguous"
                                words = None
                row["status"] = status
                row["words"] = words
                del row["chunk"]
            binding = {"audio_sha256": fresh["sha256"], "audio_bytes": fresh["bytes"],
                       "sample_rate": int(layout[1]), "channels": int(layout[0]),
                       "sample_width": int(layout[2]), "frames": int(total_frames)}
            doc, raw_bytes = build_readalong_sidecar(
                entries=entries, binding=binding, includes_text=includes_text,
                cancel_check=cancel_check, source_receipt=app_receipt)
            Path(pending).write_bytes(raw_bytes)
            sidecar_report = get_export_report_for_staged(pending, {"sidecar_bytes": len(raw_bytes)})
            sidecar_report["timing"] = get_readalong_timing_report(doc)
        ensure_audio_export_active(cancel_check)
        source_snapshot.verify_sources()
        ensure_export_stage_current(fresh["path"], fresh["quality_report"], cancel_check)
        # One immutable export: fresh MP3 + sidecar published together with
        # rollback (chapter pattern), not sequential replaces.
        _publish_pair_transaction(
            source_snapshot,
            [(fresh["path"], audio_path, fresh["quality_report"]),
             (pending, output_path, sidecar_report)],
            cancel_check, staging.path, retain)
        return True, f"readalong.sidecar.json ({len(raw_bytes)} bytes)"
    except ExportCancelled:
        return False, "Export cancelled"
    except Exception as error:
        return False, f"Read-along sidecar failed: {error} Rebuild the export and retry."
    finally:
        if not retain[0]:
            try:
                os.unlink(pending)
            except OSError:
                pass
        try:
            staging.finish(remove=not retain[0])
        except OSError:
            pass


def get_retained_epub_candidate_path(source_txt, cancel_check=None, *, origin_hashes=None):
    """Locate a candidate; callers verify the actual bytes they consume.

    Canonical paths keep legacy download semantics. A renamed fallback
    requires current provenance and exhaustive, bounded ambiguity checks.
    """
    import stat
    from archive_utils import MAX_ARCHIVE_MEMBERS, MAX_ARCHIVE_BYTES
    from publication_source import get_publication_source_capture
    from source_upload_publication import get_retained_epub_path
    if cancel_check is not None:
        ensure_audio_export_active(cancel_check)
    retained_path = get_retained_epub_path(source_txt)
    if os.path.isfile(retained_path):
        return retained_path
    if origin_hashes is None:
        captured = get_publication_source_capture(source_txt)
        if captured is None or captured.get("binding_status") != "current":
            raise ValueError("Source provenance is stale; re-upload the source book.")
        origin_hashes = {row["origin"]["sha256"] for row in captured["document"].get("candidates", [])}
    wants = frozenset(origin_hashes)
    if not wants:
        raise ValueError("This book has no retained EPUB provenance.")
    matches = []
    scanned = 0
    scanned_bytes = 0
    with os.scandir(os.path.dirname(retained_path)) as candidates:
        for entry in candidates:
            if cancel_check is not None:
                ensure_audio_export_active(cancel_check)
            scanned += 1
            if scanned > MAX_ARCHIVE_MEMBERS:
                raise ValueError("Retained-source search exceeds its entry budget; restore the source filename or re-upload.")
            if not entry.name.endswith(".source.epub"):
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError as error:
                raise ValueError("Retained-source search could not inspect every candidate; restore the source filename or re-upload.") from error
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= MAX_RETAINED_EPUB_BYTES:
                continue
            size = info.st_size
            scanned_bytes += size
            if scanned_bytes > MAX_ARCHIVE_BYTES:
                raise ValueError("Retained-source search exceeds its byte budget; restore the source filename or re-upload.")
            try:
                # Exact initial-size cap: growth cannot escape the cumulative
                # budget, and cancellation is polled between bounded reads.
                if get_audio_file_sha256(entry.path, limit=size, cancel_check=cancel_check)[0] in wants:
                    matches.append(entry.path)
            except ExportCancelled:
                raise
            except (OSError, ValueError) as error:
                raise ValueError("Retained-source search could not inspect every candidate; restore the source filename or re-upload.") from error
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise ValueError("Multiple retained EPUB files match this source provenance; refusing to guess. Resolve the duplicate retained files first.")
    raise ValueError("Retained EPUB bytes are missing for this book; re-upload the EPUB to enable read-aloud export.")


def _locate_retained_epub(manager, cancel_check, source_txt=None):
    """Return (retained_bytes, source_txt_path) verifying provenance hashes.

    ``source_txt`` may be the snapshot-captured original for the build;
    otherwise it is read from state.json. Either way the retained bytes
    are hash-verified against the publication capture (origin sha) and
    the build uses those in-memory bytes — the retained file is never
    re-read after this point. That is the immutable-provenance guard:
    later mutation of the retained file cannot change the built EPUB,
    and the snapshot separately guards the original TXT identity.
    """
    from publication_source import get_publication_source_capture
    if source_txt is None:
        from utils import safe_load_json
        state = safe_load_json(os.path.join(manager.root_dir, "state.json"), {})
        source_txt = state.get("input_file_path")
    if not isinstance(source_txt, str) or not source_txt:
        raise ValueError("No source book is selected. Select a book that came from an EPUB upload.")
    captured = get_publication_source_capture(source_txt)
    if captured is None or captured.get("binding_status") != "current":
        raise ValueError("Source provenance is stale; re-upload the source book before EPUB read-aloud export.")
    origins = captured["document"].get("candidates", [])
    if not origins:
        raise ValueError("This book did not come from an EPUB upload; EPUB read-aloud needs the retained source EPUB.")
    retained_path = get_retained_epub_candidate_path(
        source_txt, cancel_check,
        origin_hashes={row["origin"]["sha256"] for row in origins})
    retained_bytes = get_retained_epub_bytes(retained_path, cancel_check)
    if hashlib.sha256(retained_bytes).hexdigest() not in {row["origin"]["sha256"] for row in origins}:
        raise ValueError("Retained EPUB bytes do not match the source provenance; refusing to export.")
    # Script text staleness: extracted TXT must still hash to the binding.
    from project_scope_files import capture_scope_file, MAX_SCRIPT_BYTES
    source_path = Path(source_txt)
    source_facts, _ = capture_scope_file(
        source_path.parent.resolve(), source_path.name,
        limit=MAX_SCRIPT_BYTES, cancel_check=cancel_check)
    if not source_facts['exists'] or source_facts['sha256'] != captured["document"]["binding"]["sha256"]:
        raise ValueError("Source text changed since upload; mappings would be stale.")
    return retained_bytes, source_txt


def build_epub_export(manager, cancel_check, progress_callback=None, *, chunks=None,
                      audio_path=None, timing=None):
    """Build the source-preserving EPUB 3 read-aloud. Returns (ok, msg).

    ``timing`` (optional, #1844) pins the admission-time decision:
    {"timing_mode": "qwen"|"chunk", "device", "language" (aligner name
    or None to derive from the EPUB), "provider_digest",
    "capability": full capability dict for qwen mode}. A qwen build
    aligns the assembled PCM through the owned worker before packaging;
    any alignment failure fails the whole build without publication.
    """
    from export_staging import create_export_staging
    from project import _loading_progress
    audio_path = audio_path or os.path.join(manager.root_dir, "cloned_audiobook.mp3")
    output_path = get_epub_output_path(manager.root_dir)
    receipt_output_path = get_epub_receipt_output_path(manager.root_dir)
    staging = create_export_staging(manager.root_dir, "epub-readaloud")
    pending = str(staging.path / "readaloud.epub")
    pending_receipt = str(staging.path / "readaloud.receipt.json")
    retain = [False]
    try:
        source_snapshot = ExportSourceSnapshot(manager, chunks, cancel_check)
        # App seam: required receipt from captured snapshot facts; located
        # retained bytes are hash-verified in-memory provenance (see
        # _locate_retained_epub). Receipt joins the same transaction below.
        app_receipt = _require_app_source_receipt(source_snapshot)
        captured_source = _captured_source_path(source_snapshot)
        retained_bytes, located_source = _locate_retained_epub(
            manager, cancel_check, source_txt=captured_source)
        if located_source != captured_source:
            raise ValueError("Retained EPUB source disagrees with the captured snapshot; refusing to export.")
        retained_sha256 = hashlib.sha256(retained_bytes).hexdigest()
        language = None
        timing_runtime = None
        if timing is not None and timing.get("timing_mode") == "qwen":
            from io import BytesIO
            from epub_readaloud import get_epub_language_from_path
            from qwen_align import get_aligner_language_name
            capability = timing.get("capability") or {}
            language = timing.get("language")
            if not language:
                tag = get_epub_language_from_path(BytesIO(retained_bytes))
                if tag in ("", "und"):
                    raise ValueError("Qwen alignment needs a language: the EPUB has no dc:language. Choose one in the EPUB settings and rebuild.")
                language = get_aligner_language_name(tag, capability.get("languages"))
            else:
                language = get_aligner_language_name(language, capability.get("languages"))
            timing_runtime = (timing.get("runtime") or capability.get("runtime_identity")
                              or capability.get("runtime"))
            if not isinstance(timing_runtime, dict) or not isinstance(timing_runtime.get("python"), str):
                raise ValueError("Qwen runtime identity is unavailable; admit a new build with the current runtime.")

        with PCMSourceStore(manager.root_dir, lambda: ensure_audio_export_active(cancel_check)) as sources:
            data, fresh = _assemble_with_store(
                manager, source_snapshot, cancel_check, staging.path, sources)
            timeline = data["timeline"]
            bounds = data["bounds"]
            layout = data["layout"]
            script_entries = []
            for position, ((chunk, _seg, _start), (start_frame, end_frame)) in enumerate(zip(timeline, bounds)):
                prepared = get_speech_preparation(chunk.get("text", ""), chunk.get("instruct"))["text"]
                script_entries.append({"index": position, "prepared_text": prepared,
                                       "start_frame": start_frame, "end_frame": end_frame,
                                       "sample_rate": int(layout[1])})
            from epub_readaloud import build_epub_readaloud
            fresh_bytes = Path(fresh["path"]).read_bytes()
            aligned_rows = None
            timing_summary = {"mode": "chunk", "label": "chunk-estimated"}
            if timing is not None and timing.get("timing_mode") == "qwen":
                # AC8: the validated qwen-asr version (pinned runtime
                # contract, re-checked against the worker report in
                # collect_aligned_rows) must be human-readable in the
                # report/log/receipt — a digest alone is not enough.
                # Unknown/missing versions refuse before alignment.
                _version = timing.get("qwen_asr_version")
                if not isinstance(_version, str) or not _version or _version == "unknown":
                    raise ValueError(
                        "Qwen alignment version is unverified; refusing to publish.")
                from qwen_runtime import MODEL_NAME as _QWEN_MODEL_NAME
                capability = dict(timing.get("capability") or {})
                capability["device"] = timing.get("device") or "cpu"
                capability["dtype"] = timing.get("dtype") or capability.get("dtype")
                try:
                    aligned_rows, alignment = collect_aligned_rows(
                        data, language, capability, cancel_check, staging.path,
                        task_lease_fd=timing.get("task_lease_fd"),
                        worker_env=timing.get("worker_env"),
                        prepared_texts=[entry["prepared_text"] for entry in script_entries],
                        before_worker=lambda: _require_qwen_provider_current(
                            timing_runtime, capability, timing, cancel_check))
                except ValueError as error:
                    if cancel_check() and "cancelled" in str(error).lower():
                        raise ExportCancelled("Export cancelled") from error
                    raise
                timing_summary = {"mode": "qwen",
                                  "device": timing.get("device"),
                                  "dtype": timing.get("dtype"),
                                  "language": language,
                                  "model": _QWEN_MODEL_NAME,
                                  "qwen_asr_version": _version,
                                  "provider_digest": timing.get("provider_digest"),
                                  **alignment}
                if progress_callback is not None:
                    progress_callback(
                        "Qwen alignment verified (mode=qwen, device=%s, dtype=%s, "
                        "language=%s, %s, qwen-asr %s): %d rows, %d windows."
                        % (timing.get("device"), timing.get("dtype"), language,
                           _QWEN_MODEL_NAME, _version,
                           alignment["rows"], alignment["windows"]))
            if timing_summary.get("mode") == "chunk" and progress_callback is not None:
                progress_callback("Chunk timing selected (chunk-estimated, CPU).")
            try:
                epub_bytes, report = build_epub_readaloud(
                    retained_epub_bytes=retained_bytes, audio_bytes=fresh_bytes,
                    audio_filename="audiobook.mp3", script_entries=script_entries,
                    cancel_check=cancel_check, aligned_rows=aligned_rows)
            except ValueError as error:
                if cancel_check() and "cancelled" in str(error).lower():
                    raise ExportCancelled("Export cancelled") from error
                raise
            Path(pending).write_bytes(epub_bytes)
            publish_report = get_export_report_for_staged(pending, {"epub_bytes": len(epub_bytes)})
            publish_report["epub"] = report
            publish_report["timing"] = timing_summary
            # Minimal receipt for R2: binds book/rows/source/retained/audio/epub.
            epub_sha256 = hashlib.sha256(epub_bytes).hexdigest()
            if fresh["sha256"] != hashlib.sha256(fresh_bytes).hexdigest():
                raise ValueError("Fresh MP3 changed during EPUB build; refusing to publish.")
            if timing_summary.get("mode") == "qwen":
                _require_qwen_provider_current(timing_runtime, capability, timing, cancel_check)
                require_timing_preference_unchanged(
                    timing.get("preference"), manager.root_dir)
            receipt_doc = {**app_receipt,
                           "retained_epub_sha256": retained_sha256,
                           "audio_sha256": fresh["sha256"],
                           "epub_sha256": epub_sha256}
            if timing_summary.get("mode") == "qwen":
                receipt_doc.update({
                    "timing_mode": "qwen",
                    "timing_device": str(timing_summary.get("device") or ""),
                    "timing_dtype": str(timing_summary.get("dtype") or ""),
                    "timing_language": str(timing_summary.get("language") or ""),
                    "timing_model": str(timing_summary.get("model") or ""),
                    "timing_qwen_asr_version": str(timing_summary.get("qwen_asr_version") or ""),
                    "timing_provider": timing_summary.get("provider_digest")})
            receipt_bytes = json.dumps(
                receipt_doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            get_validated_epub_receipt(receipt_bytes)
            Path(pending_receipt).write_bytes(receipt_bytes)
            receipt_report = get_export_report_for_staged(pending_receipt, {"receipt_bytes": len(receipt_bytes)})
        ensure_audio_export_active(cancel_check)
        source_snapshot.verify_sources()
        ensure_export_stage_current(fresh["path"], fresh["quality_report"], cancel_check)
        ensure_export_stage_current(pending, publish_report, cancel_check)
        ensure_export_stage_current(pending_receipt, receipt_report, cancel_check)
        _publish_pair_transaction(
            source_snapshot,
            [(fresh["path"], audio_path, fresh["quality_report"]),
             (pending, output_path, publish_report),
             (pending_receipt, receipt_output_path, receipt_report)],
            cancel_check, staging.path, retain)
        return True, f"readaloud.epub ({report['spine_documents']} spine documents, {report['bytes']} bytes)"
    except ExportCancelled:
        return False, "Export cancelled"
    except AudioGenerationCancelled as error:
        if cancel_check():
            return False, "Export cancelled"
        return False, f"EPUB read-aloud failed: {error}"
    except Exception as error:
        # Surface fail-closed reasons verbatim; never substitute text-only EPUB.
        return False, f"EPUB read-aloud failed: {error}"
    finally:
        if not retain[0]:
            for staged in (pending, pending_receipt):
                try:
                    os.unlink(staged)
                except OSError:
                    pass
        try:
            staging.finish(remove=not retain[0])
        except OSError:
            pass
