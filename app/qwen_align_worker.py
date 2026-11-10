# coding=utf-8
"""Qwen3-ForcedAligner worker (#1844) — runs ONLY in the qwen-env interpreter.

Protocol: argv[1] = job JSON, argv[2] = result JSON. Heavy imports are
lazy and local-only: the model is loaded with local_files_only=True from
a provisioned local directory, and the parent enforces HF_HUB_OFFLINE=1,
so this worker never downloads weights or touches credentials. The model
is loaded exactly once; all rows and all sequential slices share it.

The alignment call follows the official Qwen3-ASR API
(QwenLM/Qwen3-ASR, pinned commit 7c6daf77a2421100f5fb066495372c00129d39ff;
Hugging Face Qwen/Qwen3-ForcedAligner-0.6B):
``Qwen3ForcedAligner.from_pretrained(model_dir, dtype=..., device_map=...)``
then ``model.align(audio=path, text=text, language=name)`` returning one
ForcedAlignResult whose items carry .text/.start_time/.end_time seconds.

Long-row repair applied here (parent diagnostics v4/v6 recipe), all on
the private model instance in this process only:

* Encoder attention mask (upstream issue 213 / PR 103): the bundled
  non-FA2 audio encoder computes cu_seqlens but omits its block-diagonal
  attention mask. Each encoder layer is wrapped so a missing mask is
  filled via the encoder tower's own ``_prepare_attention_mask``.
  FlashAttention-2 keeps its native semantics; any unexpected backend,
  missing helper, missing layers, or an already-wrapped layer fails
  closed. No site-packages patch, no global monkeypatch, no stale
  geometry cache.
* Sequential measured-cursor windows: per row, slices start at the
  measured previous token end (frame-rounded, no overlapping prior
  speech), grow in 3-second steps up to the 180-second cap while the
  last token ends within 3 seconds of the slice end (until the row end
  or hard call cap is reached), and advance only
  by actual PCM frames (a final sub-500ms remainder grows if at least
  one frame advances). Every loop is bounded (calls per window, total
  windows, slice duration) and strict forward progress is required.
* Score decoder: official token texts are kept, but times come from
  the borrowed maximum-score monotonic decoder
  (``qwen_score_decoder``; hangry-labs/Qwen3-ASR-STT @97ad227, Apache
  2.0) over real model timestamp scores captured via a thinker
  forward hook. No clamping, stretching or invented times; if scores
  cannot be captured the worker fails instead of silently returning
  official argmax times.

Validation of the returned tokens lives in the parent (qwen_align);
this worker additionally refuses its own non-positive / non-monotonic /
out-of-bounds slices before reporting them.
"""
import json
import math
import re
import shutil
import sys
import tempfile
import wave
from functools import lru_cache
from pathlib import Path
from qwen_runtime import MAX_RESULT_BYTES

MAX_JOB_BYTES = 64 * 1024 * 1024
MAX_TEXT_CHARS = 20000
MAX_ROWS = 10000
MAX_WINDOWS_PER_ROW = 10000
MAX_ALIGN_SECONDS = 180.0
WINDOW_TAIL_SECONDS = 3.0
WINDOW_GROWTH_SECONDS = 3.0
MAX_CALLS_PER_WINDOW = 64
SUPPORTED_ALIGNER_LANGUAGES = (
    "Chinese", "English", "Cantonese", "French", "German", "Italian",
    "Japanese", "Korean", "Portuguese", "Russian", "Spanish",
)


def _write_result(path, doc):
    payload = json.dumps(doc, ensure_ascii=False).encode("utf-8")
    fits = len(payload) <= MAX_RESULT_BYTES
    if not fits:
        payload = json.dumps(_worker_error(
            "alignment result exceeds its size limit; choose chunk timing."),
            ensure_ascii=False).encode("utf-8")
        if len(payload) > MAX_RESULT_BYTES:
            raise ValueError("Result budget cannot hold an error response.")
    Path(path).write_bytes(payload)
    return fits


def _get_result_row_bytes(row_id, tokens, windows, has_previous_rows):
    """Exact extra bytes for a row in both JSON result dictionaries."""
    key = json.dumps(row_id, ensure_ascii=False).encode("utf-8")
    values = (tokens, windows)
    return sum(len(key) + 2 + len(json.dumps(value, ensure_ascii=False).encode("utf-8"))
               + (2 if has_previous_rows else 0) for value in values)


def _worker_error(message):
    return {"ok": False, "error": str(message)}


def _apply_encoder_mask_adapter(model):
    """Install the pinned upstream block-diagonal mask on this instance.

    Returns the attention implementation name. Raises on any unexpected
    backend/layout or repeated installation.
    """
    try:
        tower = model.model.thinker.audio_tower
    except AttributeError as error:
        raise ValueError("encoder tower is unavailable") from error
    config = getattr(tower, "config", None)
    impl = getattr(config, "_attn_implementation", None)
    if impl == "flash_attention_2":
        # FA2 already consumes cu_seqlens boundaries; semantics intact.
        return impl
    if impl not in ("sdpa", "eager"):
        raise ValueError("unexpected encoder attention backend %r" % (impl,))
    prepare = getattr(tower, "_prepare_attention_mask", None)
    if not callable(prepare):
        raise ValueError("encoder attention mask helper is unavailable")
    layers = getattr(tower, "layers", None)
    if not layers:
        raise ValueError("encoder layers are unavailable")
    for layer in layers:
        original = layer.forward
        if getattr(original, "_aa2_mask_wrapped", False):
            raise ValueError("encoder mask adapter already installed")

        def forward(hidden, cu_seqlens, attention_mask=None,
                    _orig=original, _prepare=prepare, **kwargs):
            if attention_mask is None:
                attention_mask = _prepare(hidden, cu_seqlens)
            return _orig(hidden, cu_seqlens,
                         attention_mask=attention_mask, **kwargs)

        forward._aa2_mask_wrapped = True
        layer.forward = forward
    return impl


def _write_slice_wav(source_path, dest_path, start_frame, end_frame):
    """Copy one frame-accurate PCM slice to a temp WAV (streamed).

    Emits EXACTLY ``end_frame - start_frame`` frames of the source's
    original PCM bytes (channel/width/rate preserved via setparams).
    Remaining work is tracked in FRAMES and each ``readframes`` call
    is given a frame count derived from the actual frame width —
    passing a byte count to ``readframes`` would emit extra audio
    beyond the planned window. Blocks stay bounded (~1 MiB).
    """
    if type(start_frame) is not int or type(end_frame) is not int \
            or end_frame <= start_frame:
        raise ValueError("alignment slice frame range is invalid")
    with wave.open(str(source_path), "rb") as source:
        params = source.getparams()
        frame_bytes = params.nchannels * params.sampwidth
        if params.nchannels <= 0 or params.sampwidth <= 0 \
                or params.framerate <= 0 or frame_bytes <= 0:
            raise ValueError("row WAV geometry is invalid")
        if start_frame < 0 or end_frame > source.getnframes():
            raise ValueError("alignment slice exceeds the row audio")
        source.setpos(start_frame)
        block_frames = max(1, (1024**2) // frame_bytes)
        remaining_frames = end_frame - start_frame
        emitted_frames = 0
        with wave.open(str(dest_path), "wb") as out:
            out.setparams(params)
            while remaining_frames > 0:
                want = min(block_frames, remaining_frames)
                block = source.readframes(want)
                if not block or len(block) % frame_bytes:
                    raise ValueError("row WAV is shorter than the frame plan")
                frames_in_block = len(block) // frame_bytes
                if frames_in_block <= 0 or frames_in_block > want:
                    raise ValueError("row WAV is shorter than the frame plan")
                out.writeframes(block)
                emitted_frames += frames_in_block
                remaining_frames -= frames_in_block
        if emitted_frames != end_frame - start_frame:
            raise ValueError("alignment slice emitted the wrong frame count")
    return emitted_frames / float(params.framerate)


def _check_slice_geometry(slice_path, expected_frames, expected_seconds,
                          expected_rate, expected_channels, expected_width):
    """Verify a produced slice WAV header before any inference call.

    The <=180s audio cap and all downstream frame provenance are
    checked against the actual produced header, never against a
    declared return value. Any inconsistency raises; nothing is
    clamped or repaired.
    """
    with wave.open(str(slice_path), "rb") as stream:
        frames = stream.getnframes()
        rate = stream.getframerate()
        channels = stream.getnchannels()
        width = stream.getsampwidth()
    if frames != expected_frames or rate != expected_rate \
            or channels != expected_channels or width != expected_width:
        raise ValueError("alignment slice WAV geometry is inconsistent")
    actual_seconds = frames / float(rate)
    if actual_seconds != expected_seconds or actual_seconds > MAX_ALIGN_SECONDS:
        raise ValueError("alignment slice WAV duration is inconsistent")


def _get_predicted_token_count(model, piece, language):
    processor = getattr(model, "aligner_processor", None)
    encode = getattr(processor, "encode_timestamp", None)
    if not callable(encode):
        raise ValueError("aligner processor is unavailable")
    encoded = encode(piece, language)
    word_list = encoded[0] if isinstance(encoded, tuple) else encoded
    if not isinstance(word_list, (list, tuple)):
        raise ValueError("aligner processor returned a malformed word list")
    return 2 * len(word_list)


def _check_predicted_scores(model, piece, language, slice_seconds, step, predicted_tokens=None):
    """Bound predicted timestamp scores BEFORE encoder inference.

    Uses the SDK's own ``aligner_processor.encode_timestamp`` — the
    exact word list ``align()`` will decode — so the predicted
    timestamp-token count is exact, not estimated. Feasible bins are
    those whose time can fall inside this slice (stricly, so selected
    bins are frame-honest); bins beyond the slice could only produce
    out-of-bounds times, so the decoder search is bounded to feasible
    columns. A caller may supply the exact token count captured for
    this unchanged piece, avoiding repeated tokenization on growth.
    """
    from qwen_score_decoder import MAX_SCORE_CELLS, MAX_TIMESTAMP_TOKENS
    if predicted_tokens is None:
        predicted_tokens = _get_predicted_token_count(model, piece, language)
    # Harmonized to the strict real bound: a bin whose time exceeds
    # the slice audio can never be a frame-honest token time, so the
    # decoder search is bounded to bins inside this slice.
    feasible_bins = int(math.floor((slice_seconds + 1e-9) / step)) + 1
    if feasible_bins < predicted_tokens // 2 + 1:
        raise ValueError("slice has too few timestamp bins for a positive word")
    if predicted_tokens > MAX_TIMESTAMP_TOKENS \
            or predicted_tokens * max(feasible_bins, 0) > MAX_SCORE_CELLS:
        raise ValueError("predicted timestamp scores exceed decoder bounds")
    return feasible_bins


def _align_slice(model, slice_path, piece, language, decode, slice_seconds=None,
                 get_predicted_tokens=None):
    """One real aligner call; times from the score decoder, never official."""
    if slice_seconds is None:
        with wave.open(str(slice_path), "rb") as stream:
            slice_seconds = stream.getnframes() / float(stream.getframerate())
    segment = getattr(model, "timestamp_segment_time", None)
    if not isinstance(segment, (int, float)) or isinstance(segment, bool) \
            or not math.isfinite(segment) or segment <= 0:
        raise ValueError("timestamp segment time is unavailable")
    step = float(segment) / 1000.0
    feasible_bins = _check_predicted_scores(
        model, piece, language, slice_seconds, step,
        predicted_tokens=get_predicted_tokens() if get_predicted_tokens is not None else None)
    captured = {}

    def hook(_module, _args, kwargs, output):
        from qwen_score_decoder import get_timestamp_score_shape
        if "scores" in captured:
            raise ValueError("alignment produced multiple timestamp score forwards")
        scores = output.logits[0][kwargs["input_ids"][0] == model.timestamp_token_id]
        shape = getattr(scores, "shape", None)
        if isinstance(shape, tuple) and len(shape) == 2 \
                and isinstance(shape[1], int) and feasible_bins < shape[1]:
            scores = scores[:, :feasible_bins]
        # Crop on the original device and enforce the decoder's shared
        # shape bound BEFORE a float/device copy can allocate the matrix.
        get_timestamp_score_shape(scores)
        captured["scores"] = scores.detach().float().cpu()

    thinker = model.model.thinker
    handle = thinker.register_forward_hook(hook, with_kwargs=True)
    try:
        results = model.align(audio=str(slice_path), text=piece, language=language)
    finally:
        handle.remove()
    if "scores" not in captured:
        raise ValueError("timestamp scores were not captured")
    if not isinstance(results, list) or len(results) != 1:
        raise ValueError("alignment returned an unexpected result count")
    texts = [str(item.text) for item in results[0]]
    if not texts:
        raise ValueError("alignment returned no tokens")
    scores = captured["scores"]
    bins = decode(scores)
    if len(bins) != 2 * len(texts):
        raise ValueError("timestamp score count does not match token count")
    tokens = [{"text": text,
               "start_time": round(float(bins[2 * j]) * step, 3),
               "end_time": round(float(bins[2 * j + 1]) * step, 3)}
              for j, text in enumerate(texts)]
    return tokens


def _serialize_worker_dtype(dtype):
    """Canonical wire vocabulary derived from the actual torch.dtype.

    The parent pins 'float32'/'float16'/'bfloat16'; str(torch.float32)
    is 'torch.float32', which the strict provider check must refuse.
    Mapping is by identity against the real torch dtype objects
    available in this interpreter, never by echoing the request.
    """
    name = getattr(dtype, "__name__", None) if dtype is not None else None
    if name in ("float32", "float16", "bfloat16"):
        return name
    text = str(dtype) if dtype is not None else ""
    for canonical in ("float32", "float16", "bfloat16"):
        if text == canonical or text == "torch." + canonical:
            return canonical
    raise ValueError("worker dtype is not in the pinned vocabulary")


def _check_slice_tokens(tokens, slice_seconds):
    previous_end = 0.0
    for token in tokens:
        start, end = token["start_time"], token["end_time"]
        if not (math.isfinite(start) and math.isfinite(end)):
            raise ValueError("alignment token times are not finite")
        if not 0.0 <= start < end <= slice_seconds + 1e-9:
            raise ValueError("alignment token is reversed or out of bounds")
        if start < previous_end:
            raise ValueError("alignment tokens are not monotonic")
        previous_end = end
    return tokens[-1]["end_time"]


def _align_row(model, row, language, decode, work_dir):
    from qwen_align import get_absolute_token_time
    with wave.open(str(row["wav_path"]), "rb") as stream:
        rate = stream.getframerate()
        total_frames = stream.getnframes()
        channels = stream.getnchannels()
        width = stream.getsampwidth()
    if not rate or total_frames <= 0:
        raise ValueError("row WAV is malformed")
    text = row["text"]
    windows = row.get("windows") or [{
        "char_start": 0, "char_end": len(text),
        "est_start_sec": 0.0, "est_end_sec": float(row.get("duration_sec") or 0)}]
    cursor_frame = 0
    accepted = []
    merged = []
    for index, plan in enumerate(windows):
        piece = text[plan["char_start"]:plan["char_end"]]
        planned_seconds = float(plan["est_end_sec"]) - float(plan["est_start_sec"])
        if not math.isfinite(planned_seconds) or planned_seconds <= 0:
            raise ValueError("planned window duration is invalid")
        end_frame = min(total_frames, cursor_frame + int(round(planned_seconds * rate)))
        if end_frame <= cursor_frame:
            raise ValueError("planned window has no frames")
        calls = 0
        tokens = None
        slice_path = work_dir / ("slice-%d.wav" % index)
        @lru_cache(maxsize=1)
        def get_window_token_count():
            return _get_predicted_token_count(model, piece, language)
        while True:
            if calls >= MAX_CALLS_PER_WINDOW:
                raise ValueError("sequential call bound exhausted")
            calls += 1
            if (end_frame - cursor_frame) / float(rate) > MAX_ALIGN_SECONDS + 1e-9:
                raise ValueError("alignment slice exceeds the aligner limit")
            slice_seconds = _write_slice_wav(
                row["wav_path"], slice_path, cursor_frame, end_frame)
            if slice_seconds > MAX_ALIGN_SECONDS + 1e-9:
                raise ValueError("alignment slice exceeds the aligner limit")
            # Cap/provenance are checked against the produced WAV
            # header, not only the declared return value.
            _check_slice_geometry(
                slice_path, end_frame - cursor_frame, slice_seconds,
                rate, channels, width)
            tokens = _align_slice(model, slice_path, piece, language, decode,
                                  slice_seconds, get_window_token_count)
            last_end = _check_slice_tokens(tokens, slice_seconds)
            cap_frame = cursor_frame + int(round(MAX_ALIGN_SECONDS * rate))
            # Tail headroom schedules retries; it cannot demand a call
            # beyond the hard cap. At the row or call boundary, accept
            # only the same fully validated real tokens/PCM bounds.
            if end_frame >= total_frames \
                    or end_frame == cap_frame \
                    or last_end <= slice_seconds - WINDOW_TAIL_SECONDS + 1e-9:
                break
            wider = min(total_frames,
                        end_frame + int(round(WINDOW_GROWTH_SECONDS * rate)),
                        cap_frame)
            if wider <= end_frame:
                raise ValueError("bounded growth exhausted")
            end_frame = wider
        slice_seconds = (end_frame - cursor_frame) / float(rate)
        # Advance to the next actual frame at or after the measured
        # end: rounding down could start the next slice before the
        # preceding word's end by a fraction of a frame. Model token
        # times themselves are never clamped or shifted.
        advance = int(math.ceil(tokens[-1]["end_time"] * rate - 1e-9))
        new_cursor = cursor_frame + advance
        if new_cursor <= cursor_frame:
            raise ValueError("no forward progress")
        if new_cursor > total_frames:
            raise ValueError("measured cursor exceeds the row audio")
        offset = cursor_frame / float(rate)
        accepted.append({
            "char_start": plan["char_start"], "char_end": plan["char_end"],
            "start_frame": cursor_frame, "end_frame": end_frame,
            "offset_sec": offset, "slice_duration_sec": slice_seconds,
            "calls": calls, "tokens": tokens})
        # Merged absolute times keep full frame-derived precision
        # (no re-rounding to milliseconds): the parent re-derives
        # offset + token time and requires exact agreement.
        for token in tokens:
            merged.append({"text": token["text"],
                           "start_time": get_absolute_token_time(cursor_frame, rate, token["start_time"]),
                           "end_time": get_absolute_token_time(cursor_frame, rate, token["end_time"])})
        cursor_frame = new_cursor
    return merged, accepted


def main(argv):
    if len(argv) != 3:
        print("usage: qwen_align_worker.py <job.json> <result.json>", file=sys.stderr)
        return 2
    job_path, result_path = argv[1], argv[2]
    try:
        if Path(job_path).stat().st_size > MAX_JOB_BYTES:
            _write_result(result_path, _worker_error("invalid job: oversized."))
            return 1
        with open(job_path, "rb") as handle:
            raw_job = handle.read(MAX_JOB_BYTES + 1)
        if len(raw_job) > MAX_JOB_BYTES:
            _write_result(result_path, _worker_error("invalid job: oversized."))
            return 1
        job = json.loads(raw_job.decode("utf-8"))
        if not isinstance(job, dict):
            raise ValueError("job is not an object")
    except (OSError, ValueError, UnicodeDecodeError):
        _write_result(result_path, _worker_error("invalid job."))
        return 1

    try:
        import torch
        from qwen_asr import Qwen3ForcedAligner
        from qwen_score_decoder import decode_timestamp_bins
    except ImportError as error:
        _write_result(result_path, _worker_error(
            "missing module %s; install qwen-asr in the qwen-env interpreter "
            "(app/requirements-qwen.txt)" % getattr(error, "name", error)))
        return 1

    model_dir = job.get("model_dir")
    device = job.get("device")
    dtype_name = job.get("dtype")
    language = job.get("language")
    rows = job.get("rows")
    if not isinstance(model_dir, str) or not model_dir or not Path(model_dir).is_dir():
        _write_result(result_path, _worker_error("model directory is unavailable."))
        return 1
    if not isinstance(device, str) or not re.fullmatch(r"cpu|cuda:[0-9]+", device or ""):
        _write_result(result_path, _worker_error("job device is invalid."))
        return 1
    if device == "cpu":
        if dtype_name is not None and dtype_name != "float32":
            _write_result(result_path, _worker_error("job dtype does not match the pinned CPU policy."))
            return 1
        dtype_name = "float32"
    elif dtype_name not in ("float16", "bfloat16"):
        _write_result(result_path, _worker_error("job dtype does not match the pinned GPU policy."))
        return 1
    if language not in SUPPORTED_ALIGNER_LANGUAGES:
        _write_result(result_path, _worker_error("job language is unsupported."))
        return 1
    if not isinstance(rows, list) or not rows or len(rows) > MAX_ROWS:
        _write_result(result_path, _worker_error("job has no rows."))
        return 1
    for row in rows:
        if (not isinstance(row, dict) or not isinstance(row.get("id"), str)
                or not isinstance(row.get("wav_path"), str)
                or not isinstance(row.get("text"), str)
                or not row["text"].strip() or len(row["text"]) > MAX_TEXT_CHARS
                or not Path(row["wav_path"]).is_file()):
            _write_result(result_path, _worker_error("job rows are malformed."))
            return 1
        windows = row.get("windows")
        if windows is not None:
            if not isinstance(windows, list) or not windows \
                    or len(windows) > MAX_WINDOWS_PER_ROW:
                _write_result(result_path, _worker_error("job windows are malformed."))
                return 1
            for window in windows:
                if (not isinstance(window, dict)
                        or type(window.get("char_start")) is not int
                        or type(window.get("char_end")) is not int
                        or not 0 <= window["char_start"] < window["char_end"] <= len(row["text"])
                        or type(window.get("est_start_sec")) not in (int, float)
                        or type(window.get("est_end_sec")) not in (int, float)
                        or not math.isfinite(window["est_start_sec"])
                        or not math.isfinite(window["est_end_sec"])
                        or not window["est_end_sec"] > window["est_start_sec"]
                        or window["est_end_sec"] - window["est_start_sec"] > MAX_ALIGN_SECONDS):
                    _write_result(result_path, _worker_error("job windows are malformed."))
                    return 1

    if dtype_name == "float16":
        dtype = torch.float16
    elif dtype_name == "bfloat16":
        dtype = torch.bfloat16
    else:
        dtype = torch.float32
    try:
        model = Qwen3ForcedAligner.from_pretrained(
            model_dir, dtype=dtype, device_map=device, local_files_only=True)
    except Exception as error:
        _write_result(result_path, _worker_error(
            "could not load the local model (%s): %s" % (type(error).__name__, str(error)[:400])))
        return 1
    try:
        adapter = _apply_encoder_mask_adapter(model)
    except Exception as error:
        _write_result(result_path, _worker_error(
            "encoder mask adapter refused (%s): %s" % (type(error).__name__, str(error)[:400])))
        return 1

    version = "unknown"
    try:
        from importlib import metadata as _metadata
        version = _metadata.version("qwen-asr")
    except Exception:
        pass
    provider = {"qwen_asr_version": version, "device": device,
                "dtype": _serialize_worker_dtype(dtype),
                "encoder_adapter": adapter, "decoder": "qwen_score_decoder_v1"}
    tokens = {}
    windows_out = {}
    result_bytes = len(json.dumps(
        {"ok": True, "tokens": {}, "windows": {}, "provider": provider},
        ensure_ascii=False).encode("utf-8"))
    # Scratch slices live under the parent-owned export staging
    # tree (passed in the job), so a hard-killed worker cannot
    # orphan qwen-slices-* in OS temp: the parent's staging
    # cleanup reclaims them even though this child died.
    _slice_dir = job.get("slice_dir") if isinstance(job, dict) else None
    try:
        if isinstance(_slice_dir, str) and _slice_dir:
            parent_dir = Path(_slice_dir)
            parent_dir.mkdir(parents=True, exist_ok=True)
            work_dir = Path(tempfile.mkdtemp(prefix="qwen-slices-", dir=str(parent_dir)))
        else:
            work_dir = Path(tempfile.mkdtemp(prefix="qwen-slices-"))
    except OSError as error:
        _write_result(result_path, _worker_error(
            "could not create alignment scratch (%s): %s" % (type(error).__name__, str(error)[:400])))
        return 1

    try:
        for row in rows:
            try:
                merged, accepted = _align_row(
                    model, row, language, decode_timestamp_bins, work_dir)
            except Exception as error:
                _write_result(result_path, _worker_error(
                    "alignment failed on row %s (%s): %s" % (
                        row["id"], type(error).__name__, str(error)[:400])))
                return 1
            result_bytes += _get_result_row_bytes(
                row["id"], merged, accepted, bool(tokens))
            if result_bytes > MAX_RESULT_BYTES:
                _write_result(result_path, _worker_error(
                    "alignment result exceeds its size limit; choose chunk timing."))
                return 1
            tokens[row["id"]] = merged
            windows_out[row["id"]] = accepted
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    fits = _write_result(result_path, {
        "ok": True, "tokens": tokens, "windows": windows_out,
        "provider": provider})
    return 0 if fits else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
