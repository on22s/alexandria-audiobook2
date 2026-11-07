"""Owned export workers for read-along sidecar (#1720) and EPUB read-aloud (#1779).

Both run inside the existing export ownership pattern: integrity admission,
ExportSourceSnapshot capture, frame-exact PCM timeline, fresh encode through
the ordinary encoder/quality/admission path, verify_sources(), short set
publication. CPU-only; no inference, no billable calls.

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

_HEX64_RE = re.compile(r"[0-9a-f]{64}")

MP3_BITRATE = "128k"


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
    """
    if not isinstance(raw, bytes) or not raw or len(raw) > 1024 * 16:
        raise ValueError("EPUB read-aloud receipt is empty or oversized.")
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("EPUB read-aloud receipt is malformed.") from error
    if not isinstance(doc, dict) or set(doc) != {
            "book_token", "rows_digest", "source_sha256", "settings_digest",
            "retained_epub_sha256", "audio_sha256", "epub_sha256"}:
        raise ValueError("EPUB read-aloud receipt is malformed.")
    for key, value in doc.items():
        if not isinstance(value, str) or _HEX64_RE.fullmatch(value) is None:
            raise ValueError("EPUB read-aloud receipt identity is invalid.")
    return doc


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
    outputs = {source: report for source, _target, report in pairs}
    backup_dir = Path(staging_path) / "old"
    backup_dir.mkdir(exist_ok=True)
    touched = []

    def publish(source, target):
        index = len(touched)
        backup = str(backup_dir / f"{index}-{Path(target).name}")
        existed = os.path.lexists(target)
        if existed:
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
    fresh_sha256, fresh_bytes = get_audio_file_sha256(fresh_mp3)
    if quality_report.get("sha256") != fresh_sha256:
        raise ValueError("Fresh MP3 identity disagrees with quality admission; refusing to publish.")
    timeline_data = {"timeline": timeline, "orig_tokens": orig_tokens, "bounds": bounds,
                     "layout": layout, "total_frames": int(total_frames),
                     "pause_ms": pause_ms, "same_speaker_pause_ms": same_speaker_pause_ms,
                     "skipped": skipped, "live_map": _live_index_map(manager)}
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
            starts = []
            for position, row in enumerate(entries):
                chunk = row["chunk"]
                start_flag = bool(chunk.get("_chapter_start") or
                                  (chunk.get("chapter_origin") or {}).get("is_heading"))
                if position == 0 or start_flag:
                    starts.append(position)
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
    from source_upload_publication import get_retained_epub_path
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
    retained_path = get_retained_epub_path(source_txt)
    if not os.path.isfile(retained_path):
        # Fallback scan by exact origin hash (renamed books). Deterministic:
        # sorted names; more than one distinct retained file with the same
        # origin hash is ambiguity and must surface, never pick a first.
        uploads_dir = os.path.dirname(retained_path)
        wants = {row["origin"]["sha256"] for row in origins}
        matches = []
        for name in sorted(os.listdir(uploads_dir)):
            if not name.endswith(".source.epub"):
                continue
            candidate = os.path.join(uploads_dir, name)
            try:
                if get_audio_file_sha256(candidate)[0] in wants:
                    matches.append(candidate)
            except (OSError, ValueError):
                continue
        if len(matches) == 1:
            retained_path = matches[0]
        elif len(matches) > 1:
            raise ValueError("Multiple retained EPUB files match this source provenance; refusing to guess. Resolve the duplicate retained files first.")
        else:
            raise ValueError("Retained EPUB bytes are missing for this book; re-upload the EPUB to enable read-aloud export.")
    retained_bytes = Path(retained_path).read_bytes()
    if hashlib.sha256(retained_bytes).hexdigest() not in {row["origin"]["sha256"] for row in origins}:
        raise ValueError("Retained EPUB bytes do not match the source provenance; refusing to export.")
    # Script text staleness: extracted TXT must still hash to the binding.
    from publication_source import get_publication_source_capture as _capture
    txt_bytes = Path(source_txt).read_bytes() if os.path.isfile(source_txt) else b""
    if hashlib.sha256(txt_bytes).hexdigest() != captured["document"]["binding"]["sha256"]:
        raise ValueError("Source text changed since upload; mappings would be stale.")
    return retained_bytes, source_txt


def build_epub_export(manager, cancel_check, progress_callback=None, *, chunks=None,
                      audio_path=None):
    """Build the source-preserving EPUB 3 read-aloud. Returns (ok, msg)."""
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
            epub_bytes, report = build_epub_readaloud(
                retained_epub_bytes=retained_bytes, audio_bytes=fresh_bytes,
                audio_filename="audiobook.mp3", script_entries=script_entries,
                cancel_check=cancel_check)
            Path(pending).write_bytes(epub_bytes)
            publish_report = get_export_report_for_staged(pending, {"epub_bytes": len(epub_bytes)})
            publish_report["epub"] = report
            # Minimal receipt for R2: binds book/rows/source/retained/audio/epub.
            epub_sha256 = hashlib.sha256(epub_bytes).hexdigest()
            if fresh["sha256"] != hashlib.sha256(fresh_bytes).hexdigest():
                raise ValueError("Fresh MP3 changed during EPUB build; refusing to publish.")
            receipt_doc = {**app_receipt,
                           "retained_epub_sha256": retained_sha256,
                           "audio_sha256": fresh["sha256"],
                           "epub_sha256": epub_sha256}
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
