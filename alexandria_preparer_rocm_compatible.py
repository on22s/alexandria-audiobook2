#!/usr/bin/env python3
"""
Alexandria Master Preparer - ROCm Compatible Version
Handles CUDA/ROCm version mismatches gracefully
"""

# Suppress known noisy warnings before any other imports
import warnings
warnings.filterwarnings("ignore", message="(?s).*torchcodec is not installed correctly.*")
warnings.filterwarnings("ignore", message=".*expandable_segments not supported.*")
warnings.filterwarnings("ignore", message=".*Flash Efficient attention.*")
warnings.filterwarnings("ignore", message=".*Mem Efficient attention.*")

import os
import sys
import tempfile

from gpu_stats import run_rocm_smi_json, system_has_gpu, rocm_smi_utilization

_gpu_mismatch_warned = False


def resolve_cuda_device(torch_module):
    """Return "cuda" if torch can use the GPU, else "cpu" - warning once per
    process if a GPU is physically present but torch can't see it (wrong-build
    install), rather than silently looking identical to "no GPU at all".
    Only warns once since this gets called once per pipeline phase/feature,
    not in a hot per-chunk loop - repeating the same warning per phase would
    just be noise once the first one has already told the user what's wrong.
    """
    global _gpu_mismatch_warned
    if torch_module.cuda.is_available():
        return "cuda"
    if not _gpu_mismatch_warned:
        has_gpu, vendor = system_has_gpu()
        if has_gpu:
            _gpu_mismatch_warned = True
            logger.warning(
                f"{vendor} GPU detected on this system, but torch can't see it "
                f"(torch.cuda.is_available() is False) - falling back to CPU, "
                f"which will be dramatically slower. This usually means torch "
                f"got installed as the wrong build for this GPU."
            )
    return "cpu"

# Force llama_cpp to load first to ensure system ROCm libs are prioritized over torch's bundled ones.
# Do NOT defer this import — llama_cpp's ggml_cuda_init() must bind to the system HIP libs before
# torch's bundled copies get loaded, otherwise ROCm detection fails at runtime.
Llama = None
try:
    import llama_cpp as _llama_cpp_mod
    _llama_lib_dir = os.path.join(os.path.dirname(_llama_cpp_mod.__file__), "lib")
    _hip_so = os.path.join(_llama_lib_dir, "libggml-hip.so")
    if not os.path.exists(_hip_so) and system_has_gpu()[0]:
        # Only warn when there's actually a GPU to offload to - this used to
        # fire unconditionally, which would also warn on a genuinely
        # GPU-less dev/test machine where a CPU-only build is correct, not
        # a problem.
        import warnings
        warnings.warn(
            "\n\n*** llama-cpp-python is a CPU-only build — GPU acceleration disabled! ***\n"
            "Rebuild llama-cpp-python with ROCm support in this environment.\n"
            "Annotation will run ~9x slower than expected.\n",
            RuntimeWarning, stacklevel=2
        )
    from llama_cpp import Llama
    LLAMA_CPP_AVAILABLE = True
except ImportError:
    LLAMA_CPP_AVAILABLE = False

script_dir = os.path.dirname(os.path.abspath(__file__))

# ROCm environment fixes
os.environ["PYTORCH_HIP_ALLOC_CONF"] = "expandable_segments:True"
os.environ["PYTORCH_ALLOC_CONF"] = "expandable_segments:True"
os.environ["HSA_ENABLE_SDMA"] = "0"
os.environ["GPU_MAX_HW_QUEUES"] = "2"

import argparse
import bisect
import gc
import math
import time
import logging
import json
import re
import subprocess
import difflib
from numbers import Real
from pathlib import Path

# Shared alignment primitives (load_source, lexicon, find_best_match, ...).
# Only used when --source is passed; preparer remains zero-dep on this module
# for the legacy ASR-only workflow because nothing in the chunker calls into
# it unless source_state is populated.
import alexandria_alignment as alignment
from alexandria_run_manifest import (
    LOCK_ENV, RunStateError, acquire_run_lock, cleanup_run_artifacts, ensure_run_manifest,
    run_phase_with_lock,
    get_run_identity, get_sample_path, is_verified_artifact, mark_artifact_complete,
    validate_scratch_path, write_json_atomic, get_file_identity,
)
import zipfile
import shutil
import soundfile as sf
import numpy as np
import traceback
from collections import deque, Counter
from itertools import chain
from typing import List, Dict, Optional
from datetime import datetime

# Deferred imports to avoid HIP/CUDA context contamination between phases
torch = None
librosa = None

def _lazy_import_torch():
    global torch
    if torch is None:
        import torch as t
        torch = t
    return torch

def _lazy_import_librosa():
    global librosa
    if librosa is None:
        import librosa as l
        librosa = l
    return librosa

def _lazy_import_intervaltree():
    global IntervalTree, Interval
    if "IntervalTree" not in globals():
        from intervaltree import IntervalTree as IT, Interval as I
        IntervalTree = IT
        Interval = I
    return IntervalTree, Interval


logger = logging.getLogger("alexandria")


def ensure_preparer_logging():
    """Configure CLI logging once, preserving handlers owned by other callers."""
    for handler in logger.handlers:
        if getattr(handler, '_alexandria_preparer_file', False):
            return handler.baseFilename

    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    log_file = os.path.join(log_dir, f"alexandria_preparer_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
    handlers = []
    try:
        fh = logging.FileHandler(log_file)
        handlers.append(fh)
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter('[%(asctime)s] [%(levelname)s] [%(name)s:%(lineno)d] %(message)s'))
        fh._alexandria_preparer_file = True

        ch = logging.StreamHandler()
        handlers.append(ch)
        ch.setLevel(logging.INFO)
        ch.setFormatter(logging.Formatter('[%(levelname)s] %(message)s'))
    except Exception:
        for handler in handlers:
            handler.close()
        raise

    logger.setLevel(logging.DEBUG)
    for handler in handlers:
        logger.addHandler(handler)
    return fh.baseFilename


# Progress tracker
class ProgressTracker:
    def __init__(self):
        self.steps = []
        self.current_step = 0

    def add_step(self, name):
        self.steps.append(name)

    def start(self, step_name):
        if step_name not in self.steps:
            self.steps.insert(min(self.current_step + 1, len(self.steps)), step_name)
        self.current_step = self.steps.index(step_name)
        progress = f"[{self.current_step + 1}/{len(self.steps)}]"
        logger.info(f"▶ {progress} {step_name}...")

    def complete(self):
        logger.info(f"✓ Step {self.current_step + 1}/{len(self.steps)} completed")

progress = ProgressTracker()
progress.add_step("Validate inputs")
progress.add_step("Load audio")
progress.add_step("Transcribe audio")
progress.add_step("Annotate chunks")
progress.add_step("Create output dataset")


def log_torch_info():
    t = _lazy_import_torch()
    logger.info(f"PyTorch version: {t.__version__}")

# Check available ASR options
WHISPERX_AVAILABLE = False
TRANSFORMERS_WHISPER_AVAILABLE = False
WAV2VEC2_MODEL_REVISION = "f6b48018ad95afcf85637f433dc0fc4f4672ce34"

_WHISPER_CPP_BIN_CANDIDATES = (
    os.path.join(script_dir, "whisper.cpp", "build", "bin", "whisper-cli"),
    os.path.join(script_dir, "whisper.cpp", "build", "bin", "Release",
                 "whisper-cli.exe"),
    os.path.join(script_dir, "whisper.cpp", "build", "Release",
                 "whisper-cli.exe"),
)
WHISPER_CPP_BIN = os.environ.get("ALEXANDRIA_WHISPER_CPP_BIN") or next(
    (path for path in _WHISPER_CPP_BIN_CANDIDATES if os.path.isfile(path)),
    _WHISPER_CPP_BIN_CANDIDATES[0],
)
WHISPER_CPP_MODEL = os.environ.get(
    "ALEXANDRIA_WHISPER_CPP_MODEL",
    os.path.join(script_dir, "models", "whisper.cpp", "ggml-small.en.bin"),
)
WHISPER_CPP_AVAILABLE = (
    os.path.isfile(WHISPER_CPP_BIN)
    and os.access(WHISPER_CPP_BIN, os.X_OK)
    and os.path.isfile(WHISPER_CPP_MODEL)
)
if WHISPER_CPP_AVAILABLE:
    logger.info("✓ whisper.cpp Small.en available")
else:
    logger.debug(
        f"whisper.cpp unavailable (binary={WHISPER_CPP_BIN}, "
        f"model={WHISPER_CPP_MODEL})")

try:
    from whisperx import asr as whisperx_asr
    from whisperx import alignment as whisperx_alignment
    WHISPERX_AVAILABLE = True
    logger.info("✓ WhisperX-ROCm available")
except ImportError as e:
    logger.debug(f"WhisperX not available: {e}")

try:
    from intervaltree import IntervalTree, Interval
    INTERVALTREE_AVAILABLE = True
    logger.info("✓ intervaltree available")
except ImportError as e:
    INTERVALTREE_AVAILABLE = False
    logger.debug(f"intervaltree not available: {e}")

try:
    from transformers import pipeline
    TRANSFORMERS_WHISPER_AVAILABLE = True
    logger.info("✓ Transformers available")
except ImportError as e:
    logger.debug(f"Transformers not available: {e}")

if LLAMA_CPP_AVAILABLE:
    logger.info("✓ llama-cpp-python available")
else:
    logger.info("llama-cpp-python unavailable; ASR phase remains available")

logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("whisperx").setLevel(logging.ERROR)

def clear_vram():
    """Clear GPU memory and sync."""
    gc.collect()
    t = _lazy_import_torch()
    if t.cuda.is_available():
        t.cuda.empty_cache()
        t.cuda.synchronize()
        # Note: GPU cache clearing is logged silently to reduce spam

def get_gpu_stats():
    """Get current GPU memory and utilization stats."""
    t = _lazy_import_torch()
    if not t.cuda.is_available():
        return None

    stats = {}
    try:
        # Memory stats (works for both NVIDIA and AMD ROCm)
        allocated = t.cuda.memory_allocated() / 1e9  # GB
        reserved = t.cuda.memory_reserved() / 1e9    # GB
        total = t.cuda.get_device_properties(0).total_memory / 1e9  # GB

        stats['allocated_gb'] = allocated
        stats['reserved_gb'] = reserved
        stats['total_gb'] = total
        stats['allocated_percent'] = (allocated / total * 100) if total > 0 else 0
    except Exception as e:
        logger.debug(f"Could not get GPU memory stats: {e}")
        return None

    # Utilization via rocm-smi is a separate, independent try - an odd/
    # unparseable value here (a driver returning something other than a
    # clean "N/A" or float) used to fall into the same except as the memory
    # stats above and discard them too, when the memory stats had already
    # been computed successfully and only utilization needs to degrade.
    stats['utilization_percent'] = None
    try:
        data = run_rocm_smi_json(["--showuse"], rocm_smi_path="/opt/rocm/bin/rocm-smi")
        if data:
            # rocm-smi format: {"card0": {"GPU use (%)": "value"}} - key name
            # varies by rocm-smi version, hence the shared helper.
            for card_data in data.values():
                if not isinstance(card_data, dict):
                    continue
                stats['utilization_percent'] = rocm_smi_utilization(card_data)
                break  # Just get first GPU
        else:
            logger.debug("rocm-smi unavailable or returned no parseable JSON")
    except Exception as e:
        logger.debug(f"Could not get GPU utilization via rocm-smi: {e}")

    return stats

def log_gpu_stats(label=""):
    """Log GPU memory and utilization statistics."""
    stats = get_gpu_stats()
    if not stats:
        return

    label_str = f" ({label})" if label else ""
    logger.info(f"GPU Usage{label_str}:")
    logger.info(f"  ├─ Memory: {stats['allocated_gb']:.2f}GB / {stats['total_gb']:.2f}GB ({stats['allocated_percent']:.1f}%)")
    if stats.get('utilization_percent') is not None:
        logger.info(f"  └─ Utilization: {stats['utilization_percent']:.1f}%")
    else:
        logger.info(f"  └─ Utilization: (rocm-smi unavailable)")

def format_duration(seconds):
    """Format seconds as Xh Ym Zs (or smaller unit when applicable)."""
    seconds = max(0, int(seconds))
    hours = seconds // 3600
    mins = (seconds % 3600) // 60
    secs = seconds % 60
    if hours > 0:
        return f"{hours}h {mins}m"
    elif mins > 0:
        return f"{mins}m {secs}s"
    else:
        return f"{secs}s"

# ── Oversized-WAV handling (>4 GiB data-chunk header wrap) ───────────────────
# Standard WAV uses a 32-bit unsigned chunk-size field, so any WAV whose audio
# `data` chunk exceeds 4 GiB wraps that field and reports a bogus header
# duration (only the bytes after the modulus). `soundfile`/`librosa.load`
# honor the wrapped header and silently truncate. Audiobook WAVs at this
# project's rates (44.1 kHz stereo PCM_16) hit the wrap at ~6.8 hours — every
# full-length audiobook in the test corpus is affected.
#
# We detect the wrap by comparing on-disk file size against header-implied
# data size. When detected, we route the load through ffmpeg with
# `-ignore_length 1`, which makes the WAV demuxer ignore the chunk-size field
# and decode until EOF, giving us the full audio. Streaming via subprocess
# also avoids materialising the entire native-rate float32 array in RAM
# (a 27-hour 44.1 kHz mono float32 buffer is ~16 GB; the user has files
# that long).

def _wav_overflow_info(path):
    """Return (is_oversized, true_duration_s, header_duration_s) for a WAV
    file. `is_oversized` is True when the on-disk size implies more audio
    than the header reports (the >4 GiB data-chunk-size wrap). For non-WAV
    files the function returns (False, header_dur, header_dur).
    """
    try:
        info = sf.info(path)
    except Exception as e:
        logger.debug(f"sf.info failed in _wav_overflow_info: {e}")
        return False, 0.0, 0.0
    header_dur = info.duration
    if info.format != 'WAV':
        return False, header_dur, header_dur
    # Bytes per sample frame. soundfile exposes subtypes like PCM_16/PCM_24/PCM_32/FLOAT.
    subtype_bytes = {'PCM_16': 2, 'PCM_24': 3, 'PCM_32': 4, 'FLOAT': 4, 'DOUBLE': 8}
    bps = subtype_bytes.get(info.subtype, 2)
    file_size = os.path.getsize(path)
    # Subtract a generous 1 MB for header/junk chunks — true audio bytes
    # is essentially file_size minus a kilobyte or two of metadata.
    audio_bytes_estimate = max(0, file_size - 1024 * 1024)
    true_dur = audio_bytes_estimate / (info.samplerate * info.channels * bps)
    is_oversized = file_size > 2**32 and true_dur > header_dur * 1.5
    return is_oversized, true_dur if is_oversized else header_dur, header_dur


def _ffmpeg_decode_to_wav(src_path, dst_wav_path, target_sr, mono=True):
    """Decode an audio file to a 16-bit PCM WAV via ffmpeg, ignoring any
    bogus WAV chunk-size header. Returns the resulting file's on-disk size.
    Raises subprocess.CalledProcessError on ffmpeg failure.
    """
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ignore_length", "1",
        "-i", src_path,
        "-ac", "1" if mono else "2",
        "-ar", str(target_sr),
        "-c:a", "pcm_s16le",
        dst_wav_path,
    ]
    logger.debug(f"  ffmpeg decode → {dst_wav_path} ({target_sr}Hz, {'mono' if mono else 'stereo'})")
    subprocess.run(cmd, check=True)
    return os.path.getsize(dst_wav_path)


def decode_audio_to_memmap(src_path, target_sr, output_path, mono=True):
    """Decode to a file-backed float32 array without capturing the full PCM."""
    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error",
        "-ignore_length", "1",
        "-i", src_path,
        "-ac", "1" if mono else "2",
        "-ar", str(target_sr),
        "-f", "f32le", "-acodec", "pcm_f32le",
        "-",
    ]
    logger.debug(f"  ffmpeg decode → {output_path} ({target_sr}Hz)")
    with open(output_path, "wb") as stream:
        subprocess.run(cmd, check=True, stdout=stream)
    return get_decoded_audio_memmap(output_path)


def get_decoded_audio_memmap(output_path):
    """Read a complete, nonempty float32 PCM file as a read-only mapping."""
    size = os.path.getsize(output_path)
    if not size or size % np.dtype(np.float32).itemsize:
        raise ValueError(f"Invalid decoded PCM length: {size} bytes")
    return np.memmap(output_path, dtype=np.float32, mode="r")


def decode_audio_to_asr_streams(src_path, scratch_path, asr_path):
    """Decode once into independently resampled scratch and ASR streams."""
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
        "-ignore_length", "1", "-i", src_path,
        "-ac", "1", "-ar", "24000",
        "-c:a", "pcm_s16le", scratch_path,
        "-ac", "1", "-ar", "16000",
        "-f", "f32le", "-acodec", "pcm_f32le", asr_path,
    ]
    logger.debug(f"  ffmpeg decode → {scratch_path} (24kHz) and {asr_path} (16kHz)")
    subprocess.run(cmd, check=True)
    return get_decoded_audio_memmap(asr_path)


def validate_inputs(args):
    """Validate input files."""
    logger.info("Validating input files...")

    def _missing_path_hint(flag, path):
        """When a path isn't found, log an absolute resolved path + CWD so
        the user can see whether they hit a relative-path-vs-CWD problem.
        Reads as: 'we looked here, and our working dir is X, so try…'.
        """
        resolved = os.path.abspath(path)
        cwd = os.getcwd()
        logger.error(f"{flag}: file not found")
        logger.error(f"  requested        : {path}")
        if resolved != path:
            logger.error(f"  resolved to      : {resolved}")
        logger.error(f"  current dir      : {cwd}")
        if not os.path.isabs(path):
            logger.error(f"  hint: pass an absolute path, or 'cd' into the project "
                         f"directory before running this script "
                         f"(model/source paths are resolved relative to the "
                         f"working dir, not the script's location).")

    if not os.path.exists(args.audio):
        _missing_path_hint("--audio", args.audio)
        sys.exit(1)
    logger.debug(f"Audio file exists: {args.audio}")

    needs_annotation_model = getattr(args, "phase", None) != "asr"
    if needs_annotation_model and not os.path.exists(args.model):
        _missing_path_hint("--model", args.model)
        sys.exit(1)
    if needs_annotation_model:
        logger.debug(f"Model file exists: {args.model}")

    # Validate fallback eagerly so we fail fast on a typo'd path
    if args.fallback_model and not os.path.exists(args.fallback_model):
        _missing_path_hint("--fallback-model", args.fallback_model)
        logger.error("Either fix the path or omit --fallback-model")
        sys.exit(1)

    # Validate --source eagerly too, otherwise a typo only surfaces after
    # ASR transcription (potentially hours into the run).
    source_path = getattr(args, 'source', None)
    if source_path and not os.path.exists(source_path):
        _missing_path_hint("--source", source_path)
        sys.exit(1)
    if source_path:
        logger.debug(f"Source file exists: {source_path}")

    try:
        info = sf.info(args.audio)
        is_oversized, true_dur, header_dur = _wav_overflow_info(args.audio)
        if is_oversized:
            logger.warning(
                f"⚠ Oversized WAV detected: header says {header_dur:.1f}s "
                f"({header_dur/60:.1f} min) but file size implies "
                f"{true_dur:.1f}s ({true_dur/3600:.2f} hr). "
                f"WAV data-chunk-size field is 32-bit and has wrapped — "
                f"ffmpeg `-ignore_length 1` will be used to read the full audio."
            )
            logger.info(
                f"Audio file: {info.samplerate}Hz, {true_dur:.2f}s "
                f"(header reported {header_dur:.2f}s — wrapped), {info.channels}ch"
            )
            return true_dur
        logger.info(f"Audio file: {info.samplerate}Hz, {info.duration:.2f}s, {info.channels}ch")
        return info.duration
    except Exception as e:
        logger.error(f"Invalid audio file: {e}")
        logger.debug(traceback.format_exc())
        sys.exit(1)

def transcribe_with_whisperx_cpu(audio_16k: np.ndarray, language: str = "en") -> tuple:
    """Transcribe using WhisperX on CPU (stable, no GPU conflicts)."""
    if not WHISPERX_AVAILABLE:
        raise ImportError("WhisperX not available")

    # Force CPU to avoid CUDA/ROCm driver conflicts
    device = "cpu"
    compute_type = "int8"

    logger.info(f"Starting WhisperX transcription on CPU (stable mode)...")

    try:
        logger.debug(f"Loading WhisperX base model (device={device}, compute_type={compute_type})...")
        model = whisperx_asr.load_model("base", device, compute_type=compute_type)
        logger.info("✓ WhisperX model loaded")

        logger.info("Transcribing audio (this may take a while on CPU)...")
        result = model.transcribe(audio_16k, batch_size=1, language=language)
        detected_lang = result.get("language", language)
        logger.info(f"✓ Transcription complete, detected language: {detected_lang}")

        del model
        clear_vram()

        logger.debug(f"Loading alignment model for language: {detected_lang}...")
        model_a, metadata_a = whisperx_alignment.load_align_model(
            language_code=detected_lang,
            device=device
        )
        logger.info("✓ Alignment model loaded")

        logger.debug("Running word-level alignment...")
        aligned = whisperx_alignment.align(
            result["segments"],
            model_a,
            metadata_a,
            audio_16k,
            device,
            return_char_alignments=False
        )
        logger.info("✓ Word-level alignment complete")

        del model_a
        clear_vram()

        # Extract word segments
        word_segments = []
        for segment in aligned["segments"]:
            if "words" in segment:
                for word_info in segment["words"]:
                    if "start" in word_info and "end" in word_info:
                        word_segments.append({
                            "word": word_info["word"].strip(),
                            "start": word_info["start"],
                            "end": word_info["end"],
                            "confidence": word_info.get("score", 1.0)
                        })

        logger.info(f"✓ WhisperX complete: {len(word_segments)} words extracted")
        return word_segments, detected_lang

    except Exception as e:
        logger.error(f"WhisperX transcription failed: {e}")
        logger.debug(traceback.format_exc())
        raise

def is_english_language(language: str) -> bool:
    """Use the existing English-only provider's accepted language codes."""
    return language.lower().split("-", 1)[0] == "en"


ASR_CHUNK_SECONDS = 30
ASR_OVERLAP_SECONDS = 3


def get_asr_sample_count(total_samples, sample_rate, limit):
    """Bound every ASR/diarization backend to the requested chunk prefix."""
    if limit is None:
        return total_samples
    if type(limit) is not int or limit <= 0:
        raise ValueError("ASR limit must be a positive integer")
    seconds = ASR_CHUNK_SECONDS + (limit - 1) * (ASR_CHUNK_SECONDS - ASR_OVERLAP_SECONDS)
    return min(total_samples, seconds * sample_rate)


def get_ctc_frame_confidence(logits, torch_module):
    """Return peak frame probabilities without a full-chunk probability tensor."""
    peaks = [
        torch_module.max(torch_module.nn.functional.softmax(block, dim=-1), dim=-1).values
        for block in logits.split(256, dim=1)
    ]
    return torch_module.cat(peaks, dim=1)


def transcribe_with_wav2vec2(audio_16k: np.ndarray, language: str = "en", limit: int = None) -> tuple:
    """Use Wav2Vec2 for continuous context-aware transcription with CTC word alignment."""
    audio_16k = audio_16k[:get_asr_sample_count(len(audio_16k), 16000, limit)]
    if not is_english_language(language):
        raise ValueError(
            "Wav2Vec2 large-960h only supports English; "
            "falling back instead of silently using the wrong model")
    if not TRANSFORMERS_WHISPER_AVAILABLE:
        raise ImportError("Transformers not available")

    logger.info("▶ Initializing Wav2Vec2 ASR (CTC-aligned word timestamps)...")
    logger.info(f"  ├─ Model: facebook/wav2vec2-large-960h")
    logger.info(f"  └─ Language: {language}")

    try:
        from transformers import Wav2Vec2Processor, Wav2Vec2ForCTC
        import torch as torch_module

        logger.debug("Loading Wav2Vec2 processor and model...")
        # Resolved (not assumed) and logged after the fact - this used to
        # unconditionally claim "Device: GPU" and "loaded to GPU" above even
        # when torch.cuda.is_available() was False and it silently fell back
        # to CPU, which is exactly the kind of silent fallback that's
        # supposed to be loud (see resolve_cuda_device).
        device_str = resolve_cuda_device(torch_module)

        processor = Wav2Vec2Processor.from_pretrained(
            "facebook/wav2vec2-large-960h", revision=WAV2VEC2_MODEL_REVISION)
        model = Wav2Vec2ForCTC.from_pretrained(
            "facebook/wav2vec2-large-960h", revision=WAV2VEC2_MODEL_REVISION)
        model = model.to(device_str)
        model.eval()

        logger.info(f"✓ Wav2Vec2 model loaded to {device_str.upper()}")
        log_gpu_stats("after model load")

        # Frame rate: for wav2vec2-large-960h, CNN downsamples 16kHz audio by 320 → 50 frames/sec
        inputs_to_logits_ratio = getattr(model.config, "inputs_to_logits_ratio", 320)
        time_per_frame = inputs_to_logits_ratio / 16000.0  # seconds per logit frame (~0.02s)

        chunk_length_secs = ASR_CHUNK_SECONDS
        chunk_length = chunk_length_secs * 16000
        overlap_secs = ASR_OVERLAP_SECONDS
        overlap = overlap_secs * 16000
        stride = chunk_length - overlap
        half_overlap_secs = overlap_secs / 2.0

        # Compute chunk start positions, ensuring last chunk reaches audio end
        chunk_starts = list(range(0, max(1, len(audio_16k) - chunk_length + 1), stride))
        # Append final chunk for any remaining audio
        if not chunk_starts or chunk_starts[-1] + chunk_length < len(audio_16k):
            tail_start = max(0, len(audio_16k) - chunk_length)
            if not chunk_starts or tail_start > chunk_starts[-1]:
                chunk_starts.append(tail_start)
        num_chunks = len(chunk_starts)

        logger.info(f"  ├─ Context window: {chunk_length_secs}s")
        logger.info(f"  ├─ Overlap: {overlap_secs}s ({1.0/time_per_frame:.0f} Hz frame rate)")
        logger.info(f"  ├─ Word timestamps: CTC frame alignment (true per-word timing)")
        logger.info(f"  └─ Processing {num_chunks} chunks...")
        log_gpu_stats("before chunk processing")

        word_segments = []
        chunk_times = deque(maxlen=10)  # rolling avg for ETA

        for chunk_idx, sample_start in enumerate(chunk_starts):
            if limit is not None and chunk_idx >= limit:
                logger.info(f"Limit of {limit} chunks reached for transcription.")
                break
            chunk_t0 = time.monotonic()
            chunk_end = min(sample_start + chunk_length, len(audio_16k))
            chunk = audio_16k[sample_start:chunk_end]
            chunk_offset_secs = sample_start / 16000.0
            chunk_end_secs = chunk_end / 16000.0

            with torch_module.no_grad():
                inputs = processor(chunk, sampling_rate=16000, return_tensors="pt", padding=True)
                inputs = {k: v.to(device_str) for k, v in inputs.items()}
                logits = model(**inputs).logits
                predicted_ids = torch_module.argmax(logits, dim=-1)
                
                # Get probabilities and confidence scores
                confidence = get_ctc_frame_confidence(logits, torch_module).squeeze(0).cpu().numpy()

            # CTC decode with word-level frame offsets
            decoded = processor.batch_decode(predicted_ids, output_word_offsets=True)
            word_offsets = decoded.word_offsets[0] if decoded.word_offsets else []

            # Determine "owned" region for this chunk to avoid double-counting overlap:
            #   - first chunk owns [chunk_start, chunk_end - half_overlap]
            #   - middle chunks own [chunk_start + half_overlap, chunk_end - half_overlap]
            #   - last chunk owns [chunk_start + half_overlap, audio_end]
            is_first = (chunk_idx == 0)
            is_last = (chunk_idx == num_chunks - 1)
            owned_start = chunk_offset_secs if is_first else chunk_offset_secs + half_overlap_secs
            owned_end = chunk_end_secs if is_last else chunk_end_secs - half_overlap_secs

            for wo in word_offsets:
                word_start = chunk_offset_secs + wo["start_offset"] * time_per_frame
                word_end = chunk_offset_secs + wo["end_offset"] * time_per_frame
                # Use word center to decide ownership (avoids splitting across chunks)
                word_center = (word_start + word_end) / 2.0
                if owned_start <= word_center < owned_end:
                    # Calculate average confidence for the word
                    start_frame = wo["start_offset"]
                    end_frame = wo["end_offset"]
                    word_confidence = np.mean(confidence[start_frame:end_frame]) if end_frame > start_frame else confidence[start_frame]
                    
                    word_segments.append({
                        "word": wo["word"].strip(),
                        "start": word_start,
                        "end": word_end,
                        "confidence": float(word_confidence)
                    })

            chunk_times.append(time.monotonic() - chunk_t0)

            if (chunk_idx + 1) % 50 == 0 or chunk_idx == num_chunks - 1:
                avg_chunk_s = sum(chunk_times) / len(chunk_times)
                remaining = (num_chunks - chunk_idx - 1) * avg_chunk_s
                logger.info(f"  ↳ Chunk {chunk_idx + 1}/{num_chunks} | avg {avg_chunk_s:.2f}s/chunk | ETA {format_duration(remaining)}")
                log_gpu_stats(f"chunk {chunk_idx + 1}/{num_chunks}")

        del processor, model
        clear_vram()

        logger.info(f"✓ Wav2Vec2 complete: {len(word_segments)} words extracted with CTC-aligned timestamps")
        if word_segments:
            logger.debug(f"  First word: '{word_segments[0]['word']}' @ {word_segments[0]['start']:.3f}-{word_segments[0]['end']:.3f}s")
            logger.debug(f"  Last word:  '{word_segments[-1]['word']}' @ {word_segments[-1]['start']:.3f}-{word_segments[-1]['end']:.3f}s")
        return word_segments, language

    except Exception as e:
        logger.error(f"Wav2Vec2 transcription failed: {e}")
        logger.debug(traceback.format_exc())
        raise


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


def transcribe_with_whisper_cpp(audio_16k: np.ndarray,
                                language: str = "en") -> tuple:
    """Transcribe with the persistent whisper.cpp Small.en installation."""
    if not is_english_language(language):
        raise ValueError(
            "whisper.cpp Small.en only supports English; "
            "falling back instead of silently using the wrong model")
    if not WHISPER_CPP_AVAILABLE:
        raise ImportError("whisper.cpp binary or Small.en model is unavailable")

    audio_duration = len(audio_16k) / 16000.0
    logger.info("▶ Initializing whisper.cpp Small.en...")
    logger.info(f"  ├─ Audio: {audio_duration:.1f}s")
    logger.info(f"  ├─ Model: {WHISPER_CPP_MODEL}")
    logger.info(f"  └─ Language: {language}")

    with tempfile.TemporaryDirectory(
            prefix="alexandria_whisper_cpp_") as temp_dir:
        audio_path = os.path.join(temp_dir, "audio.wav")
        output_prefix = os.path.join(temp_dir, "transcript")
        output_path = output_prefix + ".json"
        sf.write(audio_path, audio_16k, samplerate=16000)
        command = [
            WHISPER_CPP_BIN,
            "--model", WHISPER_CPP_MODEL,
            "--file", audio_path,
            "--language", language,
            "--max-len", "1",
            "--split-on-word",
            "--output-json-full",
            "--output-file", output_prefix,
            "--no-prints",
        ]
        started = time.monotonic()
        result = subprocess.run(
            command, capture_output=True, text=True,
            timeout=max(3600, math.ceil(audio_duration * 2)),
            check=False)
        elapsed = time.monotonic() - started
        if result.returncode:
            detail = (result.stderr or result.stdout or "").strip()[-2000:]
            raise RuntimeError(
                f"whisper.cpp exited with code {result.returncode}: {detail}")
        if not os.path.isfile(output_path):
            raise FileNotFoundError(
                "whisper.cpp completed without writing its JSON output")

        with open(output_path, "r", encoding="utf-8") as output_file:
            payload = json.load(output_file)
        word_segments = get_coalesced_whisper_cpp_segments(
            payload.get("transcription"))
        detected_lang = (
            payload.get("result", {}).get("language") or language)
        logger.info(
            f"✓ whisper.cpp complete: {len(word_segments)} timed entries "
            f"in {elapsed:.1f}s ({audio_duration / max(elapsed, 1e-9):.1f}x realtime)")
        return word_segments, detected_lang


DIARIZATION_MODEL_ID = "pyannote/speaker-diarization-community-1"


def _load_diarization_pipeline(hf_token: str, device: str):
    """Load the pyannote diarization pipeline once, or return None with the
    reason logged. Shared by the full diarization pass and the sampled
    speaker-detection pre-check so the model is only loaded once per run."""
    try:
        from pyannote.audio import Pipeline
        import torch as torch_module
    except ImportError:
        logger.error("pyannote.audio not installed. Diarization skipped.")
        return None

    if not hf_token:
        logger.warning("No Hugging Face token provided. Diarization requires a token for model access.")
        logger.warning("Pass --hf-token or set HF_TOKEN environment variable.")
        return None

    logger.info(f"▶ Initializing pyannote.audio diarization (device={device})...")
    pipeline = Pipeline.from_pretrained(DIARIZATION_MODEL_ID, token=hf_token)
    if pipeline is None:
        raise ValueError("Failed to load pyannote pipeline. Check your token and model permissions.")

    pipeline.to(torch_module.device(device))
    return pipeline


def get_speaker_diarization(output, prefer_exclusive: bool = False):
    """Return the Annotation carried by a pyannote Community-1 result."""
    if prefer_exclusive:
        exclusive = getattr(output, "exclusive_speaker_diarization", None)
        if exclusive is not None:
            return exclusive
    diarization = getattr(output, "speaker_diarization", None)
    if diarization is None:
        raise ValueError("Community-1 returned no speaker diarization.")
    return diarization


def get_diarization_audio_input(audio_path: str) -> dict:
    """Decode audio without TorchCodec and return a file-backed waveform.

    PyPI's TorchCodec wheels link CUDA libraries and cannot load with a ROCm
    torch build. SoundFile is already part of the preparer runtime and avoids
    that backend mismatch while preserving all channels.
    """
    import torch as torch_module

    with sf.SoundFile(audio_path) as audio:
        sample_rate = audio.samplerate
        with tempfile.TemporaryFile(dir=os.path.dirname(os.path.abspath(audio_path))) as backing:
            samples = np.memmap(backing, dtype=np.float32, mode="w+",
                                shape=(audio.frames, audio.channels))
            position = 0
            for block in audio.blocks(blocksize=sample_rate * 60,
                                      dtype="float32", always_2d=True):
                samples[position:position + len(block)] = block
                position += len(block)
            samples.flush()
            waveform = torch_module.from_numpy(samples.T)
    return {"waveform": waveform, "sample_rate": sample_rate}


def diarize_audio(audio_path: str, hf_token: str = None, device: str = "cuda",
                  pipeline=None) -> list:
    """Perform speaker diarization using pyannote.audio.
    Requires a Hugging Face token with access to Community-1.
    """
    try:
        if pipeline is None:
            pipeline = _load_diarization_pipeline(hf_token, device)
        if pipeline is None:
            raise RuntimeError('Diarization pipeline is unavailable')

        logger.info(f"Running diarization on {audio_path}...")
        t0 = time.monotonic()
        output = pipeline(get_diarization_audio_input(audio_path))
        elapsed = time.monotonic() - t0
        diarization = get_speaker_diarization(output, prefer_exclusive=True)
        
        speaker_segments = []
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            speaker_segments.append({
                "start": turn.start,
                "end": turn.end,
                "speaker": speaker
            })
            
        logger.info(f"✓ Diarization complete in {format_duration(elapsed)}")
        unique_speakers = sorted(list(set(s["speaker"] for s in speaker_segments)))
        logger.info(f"  └─ Detected {len(unique_speakers)} unique speaker(s): {', '.join(unique_speakers)}")
        
        return speaker_segments

    except Exception as e:
        logger.error(f"✗ Diarization failed: {e}")
        logger.debug(traceback.format_exc())
        raise


DETECTION_WINDOW_SECS = 120
DETECTION_WINDOW_COUNT = 3
DETECTION_MIN_DURATION_SECS = 15 * 60
DETECTION_MIN_SECONDARY_RATIO = 0.10


def _plan_detection_windows(duration_secs: float,
                            window_secs: float = DETECTION_WINDOW_SECS,
                            count: int = DETECTION_WINDOW_COUNT) -> list:
    """Plan (start, stop) sample windows spread through the audio for the
    speaker-detection pre-check. Returns [] for audio shorter than
    DETECTION_MIN_DURATION_SECS, where sampling gains nothing over just
    diarizing the whole file."""
    if duration_secs < DETECTION_MIN_DURATION_SECS or count < 1:
        return []
    windows = []
    for index in range(count):
        center = duration_secs * (index + 0.5) / count
        start = max(0.0, min(center - window_secs / 2, duration_secs - window_secs))
        windows.append((start, start + window_secs))
    return windows


def _is_multi_speaker(window_results: list,
                      min_secondary_ratio: float = DETECTION_MIN_SECONDARY_RATIO) -> tuple:
    """Decide single- vs multi-speaker from per-window diarization talk times.

    Each window_result is {"start", "end", "speaker_seconds": {label: secs}}.
    The decision is strictly per-window - pyannote speaker labels are NOT
    stable across separate pipeline runs, so talk times must never be
    aggregated across windows by label. A window is multi-speaker when >=2
    speakers each hold >= min_secondary_ratio of that window's speech time;
    the file is multi-speaker when any window is. Returns (verdict, evidence).
    """
    verdict = False
    evidence = []
    for window in window_results:
        talk = window.get("speaker_seconds") or {}
        total = sum(talk.values())
        qualified = [label for label, seconds in talk.items()
                     if total and seconds / total >= min_secondary_ratio]
        window_multi = len(qualified) >= 2
        verdict = verdict or window_multi
        evidence.append({
            "start": window.get("start"),
            "end": window.get("end"),
            "speaker_seconds": {label: round(seconds, 1)
                                for label, seconds in sorted(talk.items())},
            "multi_speaker": window_multi,
        })
    return verdict, evidence


def detect_speaker_count(audio_24k_path: str, hf_token: str, device: str,
                         duration_secs: float) -> tuple:
    """Sampled diarization pre-check for --auto-detect-speakers.

    Runs the pyannote pipeline on a few short windows instead of the whole
    audiobook and returns (verdict, evidence, pipeline): verdict True =
    multi-speaker, False = single narrator, None = pre-check could not run
    (short file, pipeline unavailable, or read/diarize failure) and the
    caller should fall back to full diarization. The loaded pipeline is
    returned so a follow-up full pass does not load the model twice.
    """
    windows = _plan_detection_windows(duration_secs)
    if not windows:
        logger.info("  Audio shorter than %ds; skipping sampled detection in favor of full diarization.",
                    DETECTION_MIN_DURATION_SECS)
        return None, [], None
    pipeline = _load_diarization_pipeline(hf_token, device)
    if pipeline is None:
        return None, [], None

    import torch as torch_module  # import known good: _load_diarization_pipeline imported it

    try:
        sample_rate = sf.info(audio_24k_path).samplerate
        window_results = []
        for start, stop in windows:
            data, _ = sf.read(audio_24k_path, start=int(start * sample_rate),
                              stop=int(stop * sample_rate), dtype="float32",
                              always_2d=False)
            waveform = torch_module.from_numpy(data).unsqueeze(0)
            output = pipeline({"waveform": waveform, "sample_rate": sample_rate})
            diarization = get_speaker_diarization(output)
            speaker_seconds = {}
            for turn, _, speaker in diarization.itertracks(yield_label=True):
                speaker_seconds[speaker] = speaker_seconds.get(speaker, 0.0) + (turn.end - turn.start)
            window_results.append({"start": start, "end": stop,
                                   "speaker_seconds": speaker_seconds})
            logger.info(f"  Window {start:.0f}-{stop:.0f}s: "
                        f"{len(speaker_seconds)} speaker(s), talk time "
                        + ", ".join(f"{label}={seconds:.1f}s"
                                    for label, seconds in sorted(speaker_seconds.items())))
    except Exception as e:
        logger.error(f"✗ Speaker detection failed: {e}")
        logger.debug(traceback.format_exc())
        return None, [], pipeline

    verdict, evidence = _is_multi_speaker(window_results)
    return verdict, evidence, pipeline


_TIMESTAMP_BOUNDARY_TOLERANCE_SECS = 0.05


def get_validated_word_segments(word_segments, audio_duration):
    """Return a safe copy of chronological, positive-duration ASR words.

    Empty word records carry no transcript information and are discarded.
    Tiny excursions at the audio boundaries are clamped to account for
    floating-point rounding. Other malformed timing fails the whole backend
    result so ``choose_and_transcribe`` can apply its existing fallback order.
    """
    if not isinstance(audio_duration, Real) or isinstance(audio_duration, bool):
        raise ValueError("audio duration must be a finite number")
    audio_duration = float(audio_duration)
    if not math.isfinite(audio_duration) or audio_duration <= 0:
        raise ValueError("audio duration must be a positive finite number")
    if not isinstance(word_segments, (list, tuple)):
        raise ValueError("ASR word segments must be a list or tuple")

    validated = []
    previous_start = None
    previous_end = None
    tolerance = _TIMESTAMP_BOUNDARY_TOLERANCE_SECS

    for index, segment in enumerate(word_segments):
        if not isinstance(segment, dict):
            raise ValueError(f"ASR word segment {index} is not an object")
        word = segment.get("word", "")
        if not isinstance(word, str):
            raise ValueError(f"ASR word segment {index} has non-text word data")
        word = word.strip()
        if not word:
            continue

        start = segment.get("start")
        end = segment.get("end")
        for label, value in (("start", start), ("end", end)):
            if not isinstance(value, Real) or isinstance(value, bool):
                raise ValueError(
                    f"ASR word segment {index} has non-numeric {label}")
            if not math.isfinite(float(value)):
                raise ValueError(
                    f"ASR word segment {index} has non-finite {label}")
        start = float(start)
        end = float(end)

        if -tolerance <= start < 0:
            start = 0.0
        if audio_duration < end <= audio_duration + tolerance:
            end = audio_duration
        if start < 0 or end > audio_duration:
            raise ValueError(
                f"ASR word segment {index} is outside the audio bounds")
        if end <= start:
            raise ValueError(
                f"ASR word segment {index} has non-positive duration")
        if previous_start is not None and start < previous_start:
            raise ValueError(
                f"ASR word segment {index} starts before the previous word")
        if previous_end is not None and end < previous_end:
            raise ValueError(
                f"ASR word segment {index} ends before the previous word")

        copied = dict(segment)
        copied.update({"word": word, "start": start, "end": end})
        validated.append(copied)
        previous_start = start
        previous_end = end

    if not validated:
        raise ValueError("ASR returned no usable word segments")
    return validated


def choose_and_transcribe(audio_16k: np.ndarray, device: str, language: str, limit: int = None) -> tuple:
    """Transcribe using Wav2Vec2 (continuous context-aware) as primary with fallbacks."""

    audio_16k = audio_16k[:get_asr_sample_count(len(audio_16k), 16000, limit)]

    logger.info("=" * 70)
    logger.info("ASR Method Selection")
    logger.info("=" * 70)
    logger.info(f"Device: {device}")
    logger.info(f"Language: {language}")
    logger.info(f"Available ASR methods:")
    logger.info(f"  - Wav2Vec2 (GPU continuous): {TRANSFORMERS_WHISPER_AVAILABLE}")
    logger.info(f"  - whisper.cpp Small.en: {WHISPER_CPP_AVAILABLE}")
    logger.info(f"  - WhisperX (CPU mode): {WHISPERX_AVAILABLE}")

    # Try Wav2Vec2 first (continuous context-aware)
    if TRANSFORMERS_WHISPER_AVAILABLE:
        logger.info("-" * 70)
        logger.info("▶ Method 1: Wav2Vec2 (Continuous context-aware) [GPU accelerated, 30s chunks with overlap]")
        logger.info("-" * 70)
        try:
            word_segments, detected_lang = transcribe_with_wav2vec2(audio_16k, language, limit=limit)
            word_segments = get_validated_word_segments(
                word_segments, len(audio_16k) / 16000.0)
            logger.info(f"✓ SUCCESS with Wav2Vec2")
            logger.info(f"  ├─ Words extracted: {len(word_segments)}")
            logger.info(f"  ├─ Context preservation: Full audio (30s overlapping chunks)")
            logger.info(f"  └─ Language: {detected_lang} (English-only model)")
            return word_segments, detected_lang
        except Exception as e:
            logger.warning(f"✗ Wav2Vec2 failed: {e}")
            logger.debug(traceback.format_exc())
            logger.info("Falling back to whisper.cpp Small.en...")

    # Fallback to whisper.cpp Small.en
    if WHISPER_CPP_AVAILABLE:
        logger.info("-" * 70)
        logger.info("▶ Method 2: whisper.cpp Small.en [native GPU acceleration]")
        logger.info("-" * 70)
        try:
            word_segments, detected_lang = transcribe_with_whisper_cpp(
                audio_16k, language)
            word_segments = get_validated_word_segments(
                word_segments, len(audio_16k) / 16000.0)
            logger.info("✓ SUCCESS with whisper.cpp Small.en")
            logger.info(f"  ├─ Words extracted: {len(word_segments)}")
            logger.info(f"  └─ Language: {detected_lang} (English-only model)")
            return word_segments, detected_lang
        except Exception as e:
            logger.warning(f"✗ whisper.cpp failed: {e}")
            logger.debug(traceback.format_exc())
            logger.info("Falling back to WhisperX-CPU...")

    # Final fallback to WhisperX-CPU
    if WHISPERX_AVAILABLE:
        logger.info("-" * 70)
        logger.info("▶ Method 3: WhisperX-CPU (Stable fallback) [CPU mode, word-level alignment]")
        logger.info("-" * 70)
        try:
            word_segments, detected_lang = transcribe_with_whisperx_cpu(audio_16k, language)
            word_segments = get_validated_word_segments(
                word_segments, len(audio_16k) / 16000.0)
            logger.info(f"✓ SUCCESS with WhisperX-CPU")
            logger.info(f"  ├─ Words extracted: {len(word_segments)}")
            logger.info(f"  └─ Detected language: {detected_lang}")
            return word_segments, detected_lang
        except Exception as e:
            logger.error(f"✗ WhisperX-CPU also failed: {e}")
            logger.debug(traceback.format_exc())

    logger.critical("=" * 70)
    logger.critical("✗ CRITICAL: No ASR method available!")
    logger.critical(
        "Run the Pinokio installer to build whisper.cpp, or install WhisperX.")
    logger.critical("=" * 70)
    sys.exit(1)

# ── Annotation output sanitisation ────────────────────────────────────────────
# These run on the LLM's annotation BEFORE we write it to metadata.jsonl, so
# downstream consumers (alexandria_compare.py and any TTS trainer that
# tokenises on whitespace) don't have to peel the same artefacts apart.
_EMPHASIS_PATTERN = re.compile(r'\*([^*]+)\*')
_DOTS_PATTERN     = re.compile(r'\.{3,}')
_WS_COLLAPSE      = re.compile(r'\s+')


def _sanitize_annotation(text: str) -> str:
    """Clean up common LLM annotation quirks before writing to JSONL.

    1) Multi-word emphasis '*Trull Sengar*' → '*Trull* *Sengar*' so per-word
       prosody markers survive whitespace-tokenisation downstream.
    2) Pad '...' / '....' pause runs so they sit as their own tokens rather
       than fusing into adjacent words ('YOU...*DERONDL*...THE' otherwise
       collapses to one garbled token in any naive parser).
    3) Collapse any doubled whitespace introduced by step 2.
    """
    text = _EMPHASIS_PATTERN.sub(
        lambda m: ' '.join(f'*{w}*' for w in m.group(1).split()) or m.group(0),
        text,
    )
    text = _DOTS_PATTERN.sub(r' \g<0> ', text)
    text = _WS_COLLAPSE.sub(' ', text).strip()
    return text


def get_validated_annotation(annotated_raw, text, source_words, merge_alignment):
    """Admit prosody-only output before optional source-backed word restoration."""
    if alignment.get_annotation_word_tokens(annotated_raw) != alignment.get_annotation_word_tokens(text):
        raise ValueError("Annotation changed spoken words")
    if source_words is not None:
        return merge_alignment.merge_annotations_with_source(annotated_raw, source_words)
    return _sanitize_annotation(annotated_raw)


# ── Chunk-boundary selection ──────────────────────────────────────────────────
def _find_best_cut(word_starts, word_ends, words, chunk_start,
                   min_pause: float = 0.25,
                   lookback_frac: float = 0.30) -> tuple:
    """Pick the cut point for an over-size chunk. Returns
    `(last_word_idx_inclusive, strategy)` where strategy is one of
    'sentence_end', 'pause', 'too_few_words', or 'fallback' — the strategy
    is used by the caller for histogram logging so we can see which
    branch dominates and tune the parameters.

    The chunker previously cut at the first word that crossed the target
    duration — wherever that landed. This helper prefers natural boundaries
    within the last `lookback_frac` of the chunk's accumulated time so the
    emitted WAV ends on a breath/clause/sentence break instead of mid-phrase.

    Preference order:
      1. Word ending in sentence punctuation (.!?) within the lookback
         window — best for TTS coherence, take the LATEST one.
      2. Word followed by the longest pause ≥ `min_pause` seconds in the
         same window — natural breath/clause break.
      3. The final word (the pre-fix behaviour) when neither is available.
    """
    n = len(words)
    if n < 4:
        return n - 1, 'too_few_words'
    chunk_end   = word_ends[-1]
    chunk_dur   = chunk_end - chunk_start
    threshold_t = chunk_end - chunk_dur * lookback_frac

    # 1) Sentence-end cut — walk backward from end, take the latest in window
    for i in range(n - 1, -1, -1):
        if word_ends[i] < threshold_t:
            break
        bare = words[i].rstrip(') ”"\'')
        if bare.endswith(('.', '!', '?')):
            return i, 'sentence_end'

    # 2) Pause cut — largest gap in the window
    best_idx = None
    best_gap = min_pause
    for i in range(n - 1):
        if word_ends[i] < threshold_t:
            continue
        gap = word_starts[i + 1] - word_ends[i]
        if gap >= best_gap:
            best_gap = gap
            best_idx = i
    if best_idx is not None:
        return best_idx, 'pause'

    # 3) Fall back to current behaviour
    return n - 1, 'fallback'


def _build_provisional_entries_for_anchor(word_segments, chunk_size, max_entries=30):
    """Pack the first N chunks' worth of ASR words into the entry shape
    alignment.auto_anchor / alignment.estimate_alignment_quality expect.

    Auto-anchor needs to see actual chunk text to figure out where the audio
    first lines up with the source. But the real chunker hasn't run yet (it
    depends on the source-cursor we're trying to derive). Build provisional
    chunks via the same duration-threshold rule the real chunker uses — no
    pause-aware look-back, no LLM, just enough to feed the anchor.
    """
    entries = []
    current_words = []
    current_start = None
    for word_data in word_segments:
        if "start" not in word_data or "end" not in word_data:
            continue
        word = word_data.get("word", "").strip()
        if not word:
            continue
        if current_start is None:
            current_start = word_data["start"]
        current_words.append(word)
        current_end = word_data["end"]
        if current_end - current_start >= chunk_size:
            entries.append({
                'text':  " ".join(current_words),
                'start': current_start,
                'end':   current_end,
            })
            current_words = []
            current_start = None
            if len(entries) >= max_entries:
                break
    return entries


def _log_word_segment_stats(word_segments, label="ASR word segments"):
    """Summarise the ASR word_segments at INFO so the user has a feel for
    the audio's word density / pause structure before chunking starts.

    Pause percentiles especially matter: if the corpus has lots of >0.5s
    gaps, the pause-aware chunker will produce clean sentence-ending
    chunks; if it's all sub-0.2s gaps (rapid-fire dialogue), most cuts
    will land in the sentence-end or fallback branch.
    """
    if not word_segments:
        logger.info(f"  {label}: empty")
        return

    # Filter to entries that have valid start/end + non-empty word text
    valid = [
        w for w in word_segments
        if 'start' in w and 'end' in w and w.get('word', '').strip()
    ]
    if not valid:
        logger.info(f"  {label}: {len(word_segments)} entries, 0 valid")
        return

    first_t = valid[0]['start']
    last_t  = valid[-1]['end']
    total_t = last_t - first_t

    durations = [w['end'] - w['start'] for w in valid]
    gaps = [
        valid[i + 1]['start'] - valid[i]['end']
        for i in range(len(valid) - 1)
    ]
    gaps = [g for g in gaps if g >= 0]   # filter rare ASR overlaps

    logger.info(f"  {label}: {len(valid):,} words over {total_t:.1f}s "
                f"(rate {len(valid)/max(total_t,1e-9):.1f} words/s)")
    logger.info(f"    ├─ word duration  : median {_percentile(durations, 50):.3f}s, "
                f"p95 {_percentile(durations, 95):.3f}s, max {max(durations):.3f}s")
    if gaps:
        logger.info(f"    ├─ inter-word gap : median {_percentile(gaps, 50):.3f}s, "
                    f"p95 {_percentile(gaps, 95):.3f}s, max {max(gaps):.3f}s")
        long_gaps = sum(1 for g in gaps if g >= 0.5)
        logger.info(f"    └─ pauses ≥ 0.5s  : {long_gaps:,} "
                    f"({100*long_gaps/len(gaps):.1f}% of gaps)")
    else:
        logger.info(f"    └─ inter-word gap : n/a (single word)")


def _percentile(xs, p):
    """Tiny percentile helper used by the end-of-chunker summary."""
    if not xs:
        return 0.0
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(p / 100.0 * (len(xs) - 1)))))
    return xs[k]


def _build_source_state(source_path: str,
                        source_start: int = None,
                        source_start_text: str = None,
                        no_auto_anchor: bool = False,
                        entries_for_anchor: list = None):
    """Load + clean the source, build the proper-noun lexicon, tokenise into
    parallel display/match word lists, and pick the initial cursor.

    Returns a dict with everything the chunker needs to align ASR chunks
    against the source:
      {
        'orig_display': [...],   # source words, original capitalisation
        'orig_match'  : [...],   # source words, normalised for fuzzy matching
        'cursor'      : N,       # current source-word index
      }

    `entries_for_anchor` should be the first few ASR chunks (already
    available at this point) so we can use auto_anchor to find where the
    audio's prose lines up with the source text. Audio intros (credits,
    narrator notes) often have no source equivalent — the anchor jumps past
    them to the prologue's first real sentence.
    """
    logger.info(f"▶ Loading source for guided chunking: {source_path}")
    source_text = alignment.load_source(source_path)
    source_text = alignment.clean_source_text(source_text)
    logger.info(f"  ├─ Source: {len(source_text):,} characters")

    # Build per-book proper-noun lexicon (character names + recurring
    # capitalised terms). Used by alignment._step_threshold to relax the
    # boundary acceptance bar for ASR-mangled Japanese romanisations like
    # 'coodo'↔'kudou' that sit far below the default 0.55 fuzzy bar.
    proper_nouns = alignment._build_proper_nouns(source_text)
    if proper_nouns:
        sample = ', '.join(sorted(proper_nouns)[:8])
        more = f' +{len(proper_nouns) - 8} more' if len(proper_nouns) > 8 else ''
        logger.info(f"  ├─ {len(proper_nouns)} recurring proper nouns ({sample}{more})")

    orig_display, orig_match = alignment.get_source_word_lists(source_text)
    logger.info(f"  ├─ {len(orig_display):,} source words")

    # Pick initial cursor
    anchor_entry_idx = 0
    if source_start is not None:
        cursor = max(0, min(source_start, len(orig_match)))
        logger.info(f"  └─ Starting at source word {cursor} (--source-start)")
    elif source_start_text:
        pos = alignment.find_text_in_source(source_start_text, orig_match)
        if pos < 0:
            sys.exit(
                f"--source-start-text: could not confidently locate "
                f"{source_start_text!r} in the source. Try a longer or more "
                f"distinctive phrase, or use --source-start N."
            )
        cursor = pos
        logger.info(f"  └─ Starting at source word {cursor} (matched --source-start-text)")
    elif no_auto_anchor:
        cursor = 0
        logger.info(f"  └─ Auto-anchor disabled; starting at source word 0")
    elif entries_for_anchor:
        # Use auto_anchor with the first ~20 chunks to find where the audio's
        # prose lines up with the source. Anchor entries are built from the
        # ASR word_segments accumulated so far (before chunking) — we pack
        # them into the same shape compare's auto_anchor expects.
        anchor_idx, anchor_pos, anchor_ratio = alignment.auto_anchor(
            entries_for_anchor, orig_match
        )
        if anchor_ratio > 0:
            logger.info(f"  └─ Auto-anchor: entry {anchor_idx} → source word {anchor_pos} "
                        f"({anchor_ratio:.1%} match)")
            cursor = anchor_pos
            anchor_entry_idx = anchor_idx
        else:
            logger.warning(f"  └─ Auto-anchor found no confident match in the first "
                           f"{min(20, len(entries_for_anchor))} chunks; starting at word 0")
            cursor = 0
    else:
        cursor = 0
        logger.info(f"  └─ No anchor data; starting at source word 0")

    return {
        'orig_display': orig_display,
        'orig_match':   orig_match,
        'cursor':       cursor,
        'anchor_entry_idx': anchor_entry_idx,
        'proper_nouns': proper_nouns,
    }


def _read_audio_segment(audio_24k_source, start_s, end_s):
    """Read an audio segment by time range from either an in-memory array or a soundfile path."""
    start_samp = max(0, round(start_s * 24000))
    end_samp = round(end_s * 24000)

    if isinstance(audio_24k_source, (str, os.PathLike)):
        with sf.SoundFile(str(audio_24k_source)) as f:
            end_samp = min(end_samp, f.frames)
            if end_samp <= start_samp:
                return np.zeros(0, dtype=np.float32)
            f.seek(start_samp)
            data = f.read(end_samp - start_samp, dtype="float32", always_2d=False)
        return data
    else:
        end_samp = min(end_samp, len(audio_24k_source))
        slice_view = audio_24k_source[start_samp:end_samp]
        if slice_view.dtype != np.float32:
            return slice_view.astype(np.float32)
        return slice_view


def get_annotation_word_pairs(word_segments, resume_point):
    """Yield qualifying words with lookahead across blank/incomplete ASR rows."""
    previous = None
    for index, word in enumerate(word_segments):
        if ('start' not in word or 'end' not in word
                or word['start'] < resume_point or not word.get('word', '').strip()):
            continue
        if previous is not None:
            yield previous[0], previous[1], word
        previous = (index, word)
    if previous is not None:
        yield previous[0], previous[1], None


def get_source_checkpoint_identity(source_state):
    """Bind saved indices to the exact normalized word sequence."""
    if source_state is None:
        return None
    import hashlib
    words = source_state['orig_match']
    digest = hashlib.sha256(json.dumps(words, ensure_ascii=False,
                                      separators=(',', ':')).encode('utf-8')).hexdigest()
    return {'version': 1, 'word_count': len(words), 'words_sha256': digest}


def get_resumed_source_cursor(entries, source_state, source_identity, keep_unaligned=False):
    """Validate durable positions before restoring the final saved cursor."""
    if not entries or source_state is None:
        return None
    cursor = None
    for entry in entries:
        saved = entry.get('source_position')
        if 'source_position' not in entry:
            cursor = None
            continue
        if (not isinstance(saved, dict) or saved.get('identity') != source_identity
                or not isinstance(saved.get('identity'), dict)
                or type(saved['identity'].get('version')) is not int
                or type(saved['identity'].get('word_count')) is not int):
            raise RunStateError('Source checkpoint word sequence does not match this source')
        cursor = saved.get('cursor')
        if type(cursor) is not int or not 0 <= cursor <= source_identity['word_count']:
            raise RunStateError('Source checkpoint cursor is outside the source word sequence')
    if cursor is None:
        # In strict source mode every saved text came from an accepted source
        # span. A unique exact match can recover its end without fuzzy guesses.
        # keep-unaligned permits ASR-only text, so this proof does not apply.
        words = alignment.to_words(entries[-1]['text'])
        source = source_state['orig_match']
        matches = []
        if words and not keep_unaligned:
            for start in range(len(source) - len(words) + 1):
                if source[start:start + len(words)] == words:
                    matches.append(start + len(words))
                    if len(matches) > 1:
                        break
        if len(matches) != 1:
            raise RunStateError(
                'Legacy source checkpoint has no unique exact source position; '
                'preserve dataset_temp and recover its source cursor explicitly '
                'before resuming, or run without --resume to restart annotation')
        cursor = matches[0]
        logger.warning('Recovered legacy source cursor from a unique exact saved source span')
    return cursor


def _load_existing_checkpoint(temp_dir):
    """Read existing metadata.jsonl checkpoint and return (entries, resume_time, next_segment_idx).

    Tolerates a truncated/corrupt trailing line (common after power loss while
    line-buffered append was in flight): keeps the good prefix and stops at
    the first bad line. Anything after a bad line is suspect — the file may be
    fsync-ordered with later writes that landed after a gap — so we discard
    the tail rather than trying to recover it.

    Critically: if a bad line is found, the checkpoint file is immediately
    rewritten with only the good prefix. Without this, every subsequent resume
    would hit the same bad line, truncate at the same point, and sweep the
    newly-appended WAVs — making recovery impossible after repeated crashes.
    """
    checkpoint_path = os.path.join(temp_dir, "metadata.jsonl")
    entries = []
    if not os.path.exists(checkpoint_path):
        return entries, 0.0, 0

    good_lines = []
    truncated = False
    previous_idx = -1
    try:
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            for line_no, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    if not isinstance(entry, dict):
                        raise RunStateError('entry is not an object')
                    name = entry.get('audio_filepath')
                    wav_path = get_sample_path(temp_dir, name)
                    segment_idx = int(name[len('sample_'):-len('.wav')])
                    if segment_idx <= previous_idx:
                        raise RunStateError('sample indices are not increasing')
                    for field in ('start', 'end', 'duration'):
                        value = entry.get(field)
                        if isinstance(value, bool) or not isinstance(value, Real) or not math.isfinite(value):
                            raise RunStateError(f'{field} is not finite numeric data')
                    if entry['start'] < 0 or entry['end'] < entry['start'] or entry['duration'] < 0:
                        raise RunStateError('invalid sample timeline')
                    if not isinstance(entry.get('text'), str):
                        raise RunStateError('sample text is not a string')
                    if not wav_path.is_file():
                        raise RunStateError(f'referenced sample WAV is missing: {wav_path}')
                    entry = dict(entry)
                    entry['wav_path'] = str(wav_path)
                    entries.append(entry)
                    previous_idx = segment_idx
                    good_lines.append(raw if raw.endswith("\n") else raw + "\n")
                except json.JSONDecodeError as e:
                    logger.warning(
                        f"Checkpoint line {line_no} unparseable ({e}); "
                        f"keeping {len(entries)} good entries and stopping."
                    )
                    truncated = True
                    break
                except (RunStateError, KeyError, TypeError, ValueError, OverflowError) as e:
                    raise RunStateError(f'Checkpoint line {line_no} is unsafe: {e}') from e
    except RunStateError:
        raise
    except Exception as e:
        raise RunStateError(f'Could not read checkpoint {checkpoint_path}: {e}') from e

    if truncated:
        try:
            with open(checkpoint_path, "w", encoding="utf-8") as f:
                f.writelines(good_lines)
                f.flush()
                os.fsync(f.fileno())
            logger.info(f"  Checkpoint rewritten to {len(entries)} good entries (corrupt tail removed)")
        except Exception as e:
            raise RunStateError(
                f'Could not rewrite checkpoint after truncation: {e}') from e

    if not entries:
        return [], 0.0, 0

    resume_time = max(e['end'] for e in entries)
    next_idx = previous_idx + 1

    return entries, resume_time, next_idx


def _sweep_orphan_wavs(temp_dir, next_segment_idx):
    """Delete sample_NNNN.wav files at or above next_segment_idx.

    These are WAVs written before the matching metadata entry was committed
    (or before its kernel buffer flushed). Without this sweep, a multi-resume
    sequence can leave high-index orphan WAVs in dataset_temp/ that get
    packaged into the final ZIP with no metadata pointing at them.
    """
    if not os.path.isdir(temp_dir):
        return 0
    removed = 0
    for name in os.listdir(temp_dir):
        if not (name.startswith("sample_") and name.endswith(".wav")):
            continue
        try:
            idx = int(name[len("sample_"):-len(".wav")])
        except ValueError:
            continue
        if idx >= next_segment_idx:
            try:
                os.remove(os.path.join(temp_dir, name))
                removed += 1
            except Exception as e:
                logger.warning(f"Failed to remove orphan {name}: {e}")
    if removed:
        msg = f"  ├─ Swept {removed} orphan WAV(s) at idx ≥ {next_segment_idx}"
        if removed > 1:
            logger.warning(msg + " — unexpectedly large; verify next_segment_idx is correct")
        else:
            logger.info(msg)
    return removed


LLM_REQUESTED_GPU_LAYERS = 99


def _load_llm(model_path):
    """Load a GGUF LLM via llama-cpp-python, requesting GPU offload.

    `verbose=True` surfaces llama-cpp's own offload count
    (e.g. `offloaded 65/65 layers to GPU`) into stderr so we can verify
    whether the model actually landed on GPU or silently fell back.

    NOTE: do NOT call torch.cuda.empty_cache() / synchronize() before this.
    On ROCm those calls leave the device in a state where llama-cpp's
    ggml_cuda_init() reports "no ROCm-capable device is detected" and
    falls back to pure CPU — observed regressing per-chunk from ~11s
    (partial GPU) to ~13s (no GPU). Just delete the GC-eligible Python
    refs to Wav2Vec2 (already done in the caller) and let HIP keep the
    device context warm.
    """
    logger.debug(f"Loading GGUF model from: {model_path}")
    gc.collect()  # let dead Wav2Vec2 tensor refs drop without touching torch.cuda
    try:
        llm = Llama(
            model_path=model_path,
            n_gpu_layers=LLM_REQUESTED_GPU_LAYERS,   # explicit count > total; -1 was misinterpreted on some HIP builds
            n_ctx=8192,
            verbose=True,      # let llama-cpp's own "offloaded N/M layers" line into the log
        )
    except BrokenPipeError:
        # llama_cpp prints system info to stderr during init; if stderr is
        # piped (e.g. | tee | head), the pipe may close before the print
        # completes. Retry with verbose=False — the model still loads fine.
        logger.debug("BrokenPipeError during Llama init (stderr pipe closed); retrying with verbose=False")
        llm = Llama(
            model_path=model_path,
            n_gpu_layers=LLM_REQUESTED_GPU_LAYERS,
            n_ctx=8192,
            verbose=False,
        )
    logger.info(f"✓ LLM loaded: {os.path.basename(model_path)}")
    if hasattr(llm, 'n_gpu_layers'):
        logger.info(f"  ├─ Requested GPU layers: {llm.n_gpu_layers}")
    logger.info(f"  └─ Reported model device: {llm.metadata.get('device', 'unknown')}")

    # Verify model responsiveness; completion alone does not prove GPU offload.
    logger.debug("Verifying model responsiveness with test prompt...")
    llm.create_chat_completion(
        messages=[{"role": "user", "content": "test"}],
        max_tokens=1
    )
    logger.info(f"✓ Model responsiveness verified")
    return llm


# System prompt tuned for terse, structured TTS annotation output.
# Works well with instruction-following models (Qwen, Llama Instruct, Gemma Instruct).
TTS_ANNOTATION_SYSTEM_PROMPT = (
    "You are a TTS annotation tool. Given a text segment from an audiobook, "
    "output ONLY the annotated text with these markers and nothing else:\n"
    "- Pauses: use ... for natural pauses, .... for longer pauses\n"
    "- Emphasis: wrap stressed words in *asterisks*\n"
    "- Tone: punctuation conveys prosody (?, !, ,, .)\n"
    "Output the annotated text directly with no preamble, no explanation, "
    "no alternatives, no quotation marks around the output."
)

# System prompt for batch annotation mode (multiple chunks per LLM call).
# Returns a JSON array of annotated texts.
TTS_ANNOTATION_BATCH_SYSTEM_PROMPT = (
    "You are a TTS annotation tool. You will be given numbered text segments.\n"
    "For EACH segment, add these markers:\n"
    "- Pauses: ... for short pauses, .... for longer pauses\n"
    "- Emphasis: *asterisks* around stressed words\n"
    "- Tone: use ?, !, ,, . to convey prosody\n\n"
    "CRITICAL: You MUST return a valid JSON array of strings, one annotated text per segment.\n"
    "The array must have exactly the same number of elements as input segments.\n"
    "Example output: [\"*Sherlock* looked at the clock...\", \"The room was silent....\", \"*Watson* entered quietly!\"]\n"
    "Return ONLY the JSON array. Do NOT include explanations, code blocks, or any other text."
)


def _annotate_batch(llm, batch_data, alignment, batch_size, timing, stats):
    """Annotate a batch of chunks with a single LLM call.

    batch_data: list of dicts with keys: text, ctx, segment_idx, source_words_for_merge, audio_slice, etc.
    Returns list of (segment_idx, annotated_text) tuples.
    Falls back to per-chunk annotation if batch parsing fails.
    """
    if batch_size <= 1 or len(batch_data) == 1:
        # Single chunk — use per-chunk mode
        return None  # Signal caller to use per-chunk path

    # Build batch prompt
    segments_text = []
    for i, item in enumerate(batch_data):
        ctx = item.get("ctx", "")
        prefix = f"Previous context: {ctx}\n\n" if ctx else ""
        segments_text.append(f"{i+1}. {prefix}Annotate this segment:\n{item['text']}")

    user_prompt = "\n\n".join(segments_text)

    try:
        t0_llm = time.monotonic()
        response = llm.create_chat_completion(
            messages=[
                {"role": "system", "content": TTS_ANNOTATION_BATCH_SYSTEM_PROMPT},
                {"role": "user", "content": user_prompt},
            ],
            max_tokens=512 * batch_size,
            temperature=0.3,
        )
        timing['llm_infer'] += time.monotonic() - t0_llm
        raw_output = response["choices"][0]["message"]["content"].strip()

        # Extract JSON array from output (handle markdown code blocks, extra text)
        annotations = []

        # Try to find JSON array in output
        json_match = None
        if raw_output.startswith("["):
            json_match = raw_output
            # IMPROVEMENT B: Try to extract [first..last] in case there's trailing text
            try:
                json.loads(json_match)
            except json.JSONDecodeError:
                try:
                    end = json_match.rindex("]") + 1
                    json_match = json_match[:end]
                except ValueError:
                    json_match = None
        elif "```" in raw_output:
            # Extract from code block
            parts = raw_output.split("```")
            for part in parts:
                if part.strip().startswith("["):
                    json_match = part.strip()
                    if json_match.startswith("json"):
                        json_match = json_match[4:].strip()
                    try:
                        json.loads(json_match)
                    except json.JSONDecodeError:
                        try:
                            end = json_match.rindex("]") + 1
                            json_match = json_match[:end]
                        except ValueError:
                            json_match = None
                    break
        elif "[" in raw_output:
            # Find first [ and last ]
            start = raw_output.index("[")
            end = raw_output.rindex("]") + 1
            json_match = raw_output[start:end]

        if json_match:
            try:
                parsed = json.loads(json_match)
                if isinstance(parsed, list):
                    annotations = parsed
            except json.JSONDecodeError:
                pass

        # If JSON parsing failed, try numbered format as fallback
        if not annotations:
            lines = raw_output.split("\n")
            numbered = {}
            for line in lines:
                match = re.match(r"^\s*(\d+)[\.\)]\s*(.+)$", line)
                if match:
                    num = int(match.group(1))
                    text = match.group(2).strip()
                    if not 1 <= num <= len(batch_data) or num in numbered:
                        raise ValueError(f"Invalid or duplicate annotation number: {num}")
                    numbered[num] = text
            if set(numbered) == set(range(1, len(batch_data) + 1)):
                annotations = [numbered[num] for num in range(1, len(batch_data) + 1)]

        if len(annotations) != len(batch_data):
            raise ValueError(f"Expected {len(batch_data)} annotations, got {len(annotations)}. Output: {raw_output[:200]}")

        if any(not isinstance(annotation, str) or not annotation.strip()
               for annotation in annotations):
            raise ValueError("Batch annotations must be nonempty strings")

        # Process each annotation only after the whole response is validated.
        results = []
        sanitize_changed = 0
        for item, annotated_raw in zip(batch_data, annotations):
            t0_sanitize = time.monotonic()
            annotated = get_validated_annotation(
                annotated_raw, item["text"], item.get("source_words_for_merge"), alignment)
            timing['sanitize'] += time.monotonic() - t0_sanitize

            results.append((item["segment_idx"], annotated))
            if annotated != annotated_raw:
                sanitize_changed += 1

        stats['llm_success'] += len(results)
        stats['sanitize_changed'] += sanitize_changed
        return results

    except Exception as e:
        stats['llm_batch_fail'] = stats.get('llm_batch_fail', 0) + 1
        logger.warning(f"Batch annotation failed ({len(batch_data)} chunks), falling back to per-chunk: {e}")
        logger.debug(f"llm-batch-fail: {traceback.format_exc()}")
        return None  # Signal caller to use per-chunk fallback


def _save_chunk_metadata(item, annotated, character, narrator_style, book_title,
                         metadata, checkpoint_file, stats, timing, segment_idx, temp_dir):
    """Save audio WAV and write metadata for a single chunk.
    
    Used by both per-chunk and batch annotation modes.
    """
    audio_slice = item["audio_slice"]
    chunk_word_data = item["chunk_word_data"]
    current_start = item["current_start"]
    chunk_end_time = item["chunk_end_time"]
    actual_duration = len(audio_slice) / 24000.0
    expected_duration = chunk_end_time - current_start
    if abs(actual_duration - expected_duration) > 0.1:
        stats['audio_short'] += 1
        logger.warning(
            f"audio slice mismatch idx={segment_idx} "
            f"expected {expected_duration:.3f}s got {actual_duration:.3f}s "
            f"(req {current_start:.3f}-{chunk_end_time:.3f}s)"
        )

    if len(audio_slice) > 0:
        stats['chunk_durations'].append(actual_duration)
        timing['kept_chunks'] += 1
        seg_name = f"sample_{segment_idx:04d}.wav"
        wav_path = os.path.join(temp_dir, seg_name)
        t0_wav = time.monotonic()
        sf.write(wav_path, audio_slice, 24000)
        wav_fd = os.open(wav_path, os.O_RDWR)
        try:
            os.fsync(wav_fd)
        finally:
            os.close(wav_fd)
        directory_fd = os.open(temp_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        timing['wav_write'] += time.monotonic() - t0_wav

        entry = {
            "audio_filepath": seg_name,
            "text": annotated,
            "duration": actual_duration,
            "start": current_start,
            "end": chunk_end_time,
            "speaker": Counter(w.get("speaker", "UNKNOWN") for w in chunk_word_data).most_common(1)[0][0],
            "speaker_labels": sorted({w.get("speaker", "UNKNOWN") for w in chunk_word_data}, key=str),
            "wav_path": wav_path,
        }
        if item.get('source_position') is not None:
            entry['source_position'] = item['source_position']
        # Enrichment is chunk-level metadata copied onto each reconstructed
        # word. Collapse it back to one value per saved audio chunk.
        for key in ("speaker_attribution", "emotional_tone"):
            value = next((w.get(key) for w in chunk_word_data
                          if w.get(key) and w.get(key) != "N/A"), None)
            if value:
                entry[key] = value
        enriched_style = next((w.get("narration_style") for w in chunk_word_data
                               if w.get("narration_style") and w.get("narration_style") != "N/A"), None)
        if character:
            entry["character"] = character
        if narrator_style:
            entry["narrator_style"] = narrator_style
        elif enriched_style:
            entry["narrator_style"] = enriched_style
        if book_title:
            entry["book_title"] = book_title
        metadata.append(entry)

        checkpoint_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
        checkpoint_file.flush()
        os.fsync(checkpoint_file.fileno())


def apply_wave_tags(wav_path, entry):
    """Set one value per ID3 frame so resumed tagging is idempotent."""
    import mutagen.wave
    import mutagen.id3

    audio_tags = mutagen.wave.WAVE(wav_path)
    if audio_tags.tags is None:
        audio_tags.add_tags()
    if entry.get('book_title'):
        audio_tags.tags.setall('TALB', [mutagen.id3.TALB(
            encoding=3, text=entry['book_title'])])
    if entry.get('character'):
        audio_tags.tags.setall('TPE1', [mutagen.id3.TPE1(
            encoding=3, text=entry['character'])])
    if entry.get('narrator_style'):
        audio_tags.tags.setall('COMM', [mutagen.id3.COMM(
            encoding=3, text=entry['narrator_style'], lang='eng', desc='')])
    audio_tags.tags.setall('TIT2', [mutagen.id3.TIT2(
        encoding=3, text=entry['audio_filepath'])])
    audio_tags.save()


def _calculate_chunk_snr(chunk_audio: np.ndarray) -> float:
    """Calculate the Signal-to-Noise Ratio (SNR) for an audio chunk.

    Uses a pure-numpy approach: signal power = mean of squared samples,
    noise power = mean energy of quietest 10% of 2048-sample blocks.
    Avoids librosa.util.frame() which creates expensive 2D arrays.
    """
    if chunk_audio.size == 0:
        return -100.0  # Represents silent or empty chunk

    signal_power = np.mean(chunk_audio ** 2)
    if signal_power == 0:
        return -100.0

    # Block-based noise estimation (2048-sample blocks)
    block_size = 2048
    n_blocks = len(chunk_audio) // block_size
    if n_blocks == 0:
        return 100.0  # Too short to estimate noise

    # Compute per-block energy efficiently using reshape (no loop, no 2D copy)
    n_samples = n_blocks * block_size
    blocks = chunk_audio[:n_samples].reshape(n_blocks, block_size)
    block_energies = np.mean(blocks ** 2, axis=1)

    # Noise = mean energy of quietest 10% of blocks
    n_quiet = max(1, int(len(block_energies) * 0.1))
    noise_power = np.mean(np.partition(block_energies, n_quiet)[:n_quiet])

    if noise_power <= 0:
        return 100.0  # No detectable noise

    return 10 * np.log10(signal_power / noise_power)

def _check_llm_fail_rate(stats):
    """Fail loud (once) if the LLM-annotation failure rate crosses 50% of
    chunks processed so far - a dead/misconfigured LLM server would otherwise
    silently fill the dataset with unannotated raw text, indistinguishable
    from a healthy run except for per-chunk warnings buried in the log. Does
    not abort the run: annotation failure isn't fatal, the raw-text fallback
    is still a usable degraded result. See FIXED.md F-116."""
    total = stats['llm_success'] + stats['llm_fail']
    if total >= 20 and not stats.get('llm_fail_rate_warned') and stats['llm_fail'] / total > 0.5:
        stats['llm_fail_rate_warned'] = True
        logger.error(
            f"⚠ LLM annotation failure rate is {stats['llm_fail']}/{total} "
            f"({stats['llm_fail'] / total:.0%}) - the LLM server may be down "
            f"or misconfigured. The run will continue (chunks fall back to "
            f"unannotated raw text) but check the LLM connection."
        )

def annotate_chunks(word_segments, model_path, chunk_size, audio_24k_source,
                    resume=False, audio_source_path=None, fallback_model_path=None,
                    source_state=None, source_threshold=0.65, keep_unaligned=False,
                    min_chunk_duration=2.0, min_confidence=0.85, min_snr=15,
                    book_title=None, character=None, narrator_style=None,
                    batch_size=1, run_identity=None, summary_output=None):
    """Create and annotate chunks with periodic checkpointing and resume support.

    audio_24k_source: either a numpy array (in-memory) or a path to a 24kHz WAV file.
    audio_source_path: the original input audio path, used to validate resume safety.
    fallback_model_path: optional secondary GGUF to load if model_path fails.

    source_state: when provided (from --source), each chunk is fuzzy-aligned
    against the source text BEFORE the LLM annotates. High-confidence matches
    (>= source_threshold) have their text replaced with the source's spelling
    so character names and dialect spellings come out correct. Below-threshold
    chunks are dropped (audio-only material) unless keep_unaligned=True.
    Pass None to run the pre-source legacy ASR-only flow with no behaviour
    change.
    """
    temp_dir = "dataset_temp"
    os.makedirs(temp_dir, exist_ok=True)
    checkpoint_path = os.path.join(temp_dir, "metadata.jsonl")

    if run_identity is None:
        raise RunStateError('Annotation requires a validated run identity')
    ensure_run_manifest(temp_dir, run_identity, fresh=False)

    source_identity = get_source_checkpoint_identity(source_state)
    if resume:
        existing_entries, resume_time, next_segment_idx = _load_existing_checkpoint(temp_dir)
        restored_cursor = get_resumed_source_cursor(existing_entries, source_state, source_identity, keep_unaligned)
        if restored_cursor is not None:
            source_state['cursor'] = restored_cursor
        if existing_entries:
            logger.info(f"▶ Resuming from checkpoint: {len(existing_entries)} segments already processed")
            logger.info(f"  ├─ Run identity verified: {audio_source_path}")
            logger.info(f"  ├─ Resume time: {resume_time:.2f}s")
            logger.info(f"  ├─ Next segment index: {next_segment_idx}")
            _sweep_orphan_wavs(temp_dir, next_segment_idx)
            logger.info(f"  └─ Resume state clean")
        else:
            logger.info("▶ --resume specified but no checkpoint found, starting fresh")
            _sweep_orphan_wavs(temp_dir, 0)  # wipe any stale WAVs from a prior run
            existing_entries, resume_time, next_segment_idx = [], 0.0, 0
    else:
        if os.path.exists(checkpoint_path):
            logger.warning(
                f"▶ Discarding unfinished annotation checkpoint for {audio_source_path}; "
                "pass --resume to continue it."
            )
        for name in os.listdir(temp_dir):
            if name == 'metadata.jsonl' or re.fullmatch(r'sample_[0-9]+\.wav', name):
                path = os.path.join(temp_dir, name)
                if os.path.isdir(path) and not os.path.islink(path):
                    raise RunStateError(f'Generated path is unexpectedly a directory: {path}')
                os.remove(path)
        existing_entries, resume_time, next_segment_idx = [], 0.0, 0

    remaining_word_pairs = get_annotation_word_pairs(word_segments, resume_time)
    first_word_pair = next(remaining_word_pairs, None)
    annotation_word_pairs = chain(
        (first_word_pair,) if first_word_pair is not None else (),
        remaining_word_pairs,
    )
    llm = None
    active_model_path = model_path
    if first_word_pair is not None:
        logger.info("▶ Loading LLM for annotations...")
        logger.info(f"  ├─ Primary model: {os.path.basename(model_path)}")
        if fallback_model_path:
            logger.info(f"  ├─ Fallback model: {os.path.basename(fallback_model_path)}")
        logger.info(f"  ├─ Requested GPU layers: {LLM_REQUESTED_GPU_LAYERS} (actual offload reported by llama-cpp)")
        logger.info("  └─ Checkpoint: fsync per chunk (durable across power loss)")

        try:
            llm = _load_llm(model_path)
        except Exception as primary_err:
            logger.error(f"✗ Failed to load primary model {model_path}: {primary_err}")
            logger.debug(traceback.format_exc())
            if fallback_model_path and os.path.exists(fallback_model_path):
                logger.warning(f"▶ Falling back to: {fallback_model_path}")
                try:
                    llm = _load_llm(fallback_model_path)
                    active_model_path = fallback_model_path
                except Exception as fallback_err:
                    logger.error(f"✗ Fallback model also failed: {fallback_err}")
                    logger.debug(traceback.format_exc())
                    raise
            else:
                raise

        log_gpu_stats(f"after LLM load ({os.path.basename(active_model_path)})")

    else:
        logger.info("No annotation words remain; reusing validated checkpoint audio")

    # ── Detailed timing instrumentation ──────────────────────────────────────
    # Track where time is spent per chunk to identify optimization opportunities.
    # Logged every 100 chunks and at the end of annotation.
    timing = {
        'audio_read': 0.0,      # _read_audio_segment for SNR check
        'snr_calc': 0.0,        # _calculate_chunk_snr
        'alignment': 0.0,       # source-guided alignment (3 tiers)
        'llm_infer': 0.0,       # LLM create_chat_completion
        'sanitize': 0.0,        # _sanitize_annotation or merge_with_source
        'wav_write': 0.0,       # sf.write + fsync
        'batch_tag': 0.0,       # batch mutagen tagging (post-loop)
        'dropped_chunks': 0,    # chunks dropped before LLM
        'kept_chunks': 0,       # chunks that went through full pipeline
    }

    # ── Batch annotation buffer ─────────────────────────────────────────────
    # When batch_size > 1, collect chunks here and annotate them together.
    logger.info(f"  ├─ Batch size       : {batch_size} {'(batch mode)' if batch_size > 1 else '(per-chunk mode)'}")
    batch_buffer = [] if batch_size > 1 else None
    batch_started_at = None

    # ── Pre-chunk diagnostic: word density, gap distribution ─────────────────
    # Lets the user see what kind of audio they're working with before the
    # 60+ hour annotation starts. If gaps are uniformly tiny, expect the
    # pause-cut branch to be useless and most chunks to fall back. If gaps
    # span a wide range, expect sentence-end and pause cuts to dominate.
    _log_word_segment_stats(word_segments, label="ASR word segments")
    logger.info(f"  ├─ Source mode    : {'enabled' if source_state else 'disabled (ASR-only)'}")
    if source_state:
        logger.info(f"  ├─ Threshold      : {source_threshold:.2f} "
                    f"({'keep-unaligned' if keep_unaligned else 'strict-drop'})")
        logger.info(f"  └─ Initial cursor : source word {source_state['cursor']}")

    # ── Per-run summary metrics (logged at the end of annotate_chunks) ───────
    stats = {
        'cut_strategy':    Counter(),   # which look-back path picked the cut
        'source_action':   Counter(),   # 'replace' / 'keep_asr' / 'dropped' / 'dropped_short' / 'deduplicated' / 'dropped_low_quality'
        'llm_success':     0,
        'llm_fail':        0,
        'llm_batch_fail':  0,
        'sanitize_changed':0,           # times _sanitize_annotation altered text
        'chunk_durations': [],          # for end-of-run distribution stats
        'audio_short':     0,           # times audio slice was shorter than expected
        'reanchor_backward': 0,         # large backward re-anchor jumps (source/audio mismatch signal)
        'realign_events':  0,
        'reanchor_events': 0,
    }

    metadata = list(existing_entries)
    segment_idx = next_segment_idx
    current_words = []
    current_word_starts = []   # parallel to current_words — for pause-aware cuts
    current_word_ends   = []   # parallel to current_words — for pause-aware cuts
    current_start = resume_time  # Start fresh after the resume point
    context = deque(maxlen=5)

    # Pre-populate context from last 5 resumed entries for continuity
    for prior in metadata[-5:]:
        context.append(prior.get("text", ""))

    prev_raw_text = context[-1] if context else ""
    prev_raw_speaker = existing_entries[-1].get("speaker", "UNKNOWN") if existing_entries else None

    def apply_annotation_batch(start_index):
        """Annotate and durably save one full or partial buffered batch."""
        count = len(batch_buffer)
        batch_results = _annotate_batch(
            llm, batch_buffer, alignment, count, timing, stats
        )
        for i, item in enumerate(batch_buffer):
            idx = start_index + i
            annotated = None
            if batch_results is not None and i < len(batch_results):
                annotated = batch_results[i][1]
            if annotated is None:
                ctx_fallback = " ".join(list(context)[-2:]) if context else ""
                user_prompt = f"Previous context: {ctx_fallback}\n\nAnnotate this segment:\n{item['text']}" if ctx_fallback else f"Annotate this segment:\n{item['text']}"
                try:
                    response = llm.create_chat_completion(
                        messages=[
                            {"role": "system", "content": TTS_ANNOTATION_SYSTEM_PROMPT},
                            {"role": "user", "content": user_prompt},
                        ],
                        max_tokens=512,
                        temperature=0.3,
                    )
                    annotated_raw = response["choices"][0]["message"]["content"].strip()
                    if not annotated_raw:
                        raise RuntimeError("LLM returned an empty response")
                    annotated = get_validated_annotation(
                        annotated_raw, item["text"], item.get("source_words_for_merge"), alignment)
                    stats['llm_success'] += 1
                except Exception as e:
                    stats['llm_fail'] += 1
                    _check_llm_fail_rate(stats)
                    logger.warning(f"Batch fallback LLM failed for chunk {idx}: {e}")
                    annotated = item["text"]

            _save_chunk_metadata(
                item, annotated, character, narrator_style, book_title,
                metadata, checkpoint_file, stats, timing, idx, temp_dir,
            )
            context.append(item["text"])
        batch_buffer.clear()
        return count

    total_words = len(word_segments)
    logger.info(f"▶ Creating and annotating chunks (target: {chunk_size}s per chunk)...")
    logger.info(f"  Processing {total_words} word segments...")

    # Estimate based on remaining audio
    estimated_chunks_total = max(1, int((len(word_segments) / 12) * (chunk_size / 10)))
    estimated_chunks_remaining = max(1, estimated_chunks_total - segment_idx)
    logger.info(f"  ├─ Estimated total chunks: ~{estimated_chunks_total}")
    if segment_idx > 0:
        logger.info(f"  ├─ Already completed: {segment_idx}")
        logger.info(f"  └─ Remaining to process: ~{estimated_chunks_remaining}")
    else:
        logger.info(f"  └─ Initial ETA will appear after first chunk completes")
    log_gpu_stats("before annotation loop")

    annotation_start_time = time.monotonic()
    chunk_times = deque(maxlen=20)  # rolling window for dynamic ETA

    # Open checkpoint in line-buffered mode (buffering=1) so each entry hits
    # the kernel buffer immediately, then fsync per chunk so power loss can't
    # lose work the code thinks was persisted. fsync is ~10-50ms on SSD vs
    # ~12s per chunk, so the overhead is <0.5%.
    checkpoint_file = open(checkpoint_path, "a", encoding="utf-8", buffering=1)

    started = False  # True once we've consumed the first qualifying word

    try:
        for idx, word_data, next_word_data in annotation_word_pairs:
            word_start_time = word_data["start"]
            word = word_data["word"].strip()

            # Pin chunk start to the first qualifying word so resumed runs
            # don't include leading silence between resume_point and the first word.
            if not started:
                current_start = word_start_time
                started = True

            current_words.append(word_data)
            current_word_starts.append(word_start_time)
            current_word_ends.append(word_data["end"])
            current_end = word_data["end"]
            duration = current_end - current_start

            is_final = next_word_data is None
            is_speaker_turn = (next_word_data is not None
                               and next_word_data.get('speaker', 'UNKNOWN')
                               != word_data.get('speaker', 'UNKNOWN'))
            
            # ── Smart Clip Length ───────────────────────────────────────────────
            # Dynamically split on natural speech boundaries instead of rigid caps.
            # Start evaluating boundaries at 70% of target chunk size. If we don't
            # find a natural break by 150%, force a cut using the lookback heuristic.
            target_dur = chunk_size * 0.70
            max_dur = chunk_size * 1.50
            
            is_good_break = False
            cut_strategy = None
            
            if duration >= target_dur and not is_final:
                bare_word = word.rstrip(') ”"\'')
                if bare_word.endswith(('.', '!', '?')):
                    is_good_break = True
                    cut_strategy = 'sentence_end'
                elif idx < len(word_segments) - 1:
                    next_start = word_segments[idx + 1].get("start", current_end)
                    if next_start - current_end >= 0.4:
                        is_good_break = True
                        cut_strategy = 'pause'

            if is_final or is_speaker_turn or is_good_break or duration >= max_dur:
                if is_final:
                    cut_at = len(current_words) - 1
                    cut_strategy = 'is_final'
                elif is_speaker_turn:
                    cut_at = len(current_words) - 1
                    cut_strategy = 'speaker_turn'
                elif is_good_break:
                    cut_at = len(current_words) - 1
                else:
                    cut_at, cut_strategy = _find_best_cut(
                        current_word_starts, current_word_ends,
                        [w['word'] for w in current_words], current_start,
                    )
                stats['cut_strategy'][cut_strategy] += 1
                if is_speaker_turn:
                    started = False

                chunk_word_data = current_words[:cut_at + 1]
                chunk_words    = [w['word'] for w in chunk_word_data]
                chunk_end_time = current_word_ends[cut_at]
                chunk_duration = chunk_end_time - current_start
                trimmed_tail   = len(current_words) - 1 - cut_at  # words carried forward
                logger.debug(
                    f"chunk-emit idx={segment_idx} "
                    f"t={current_start:.2f}-{chunk_end_time:.2f}s "
                    f"dur={chunk_duration:.2f}s words={len(chunk_words)} "
                    f"cut={cut_strategy} carry_tail={trimmed_tail}"
                )

                # Always define these for the current chunk BEFORE the >=1.0s
                # block: the rejected-chunk logging further down also runs for
                # sub-1.0s chunks (outside that block), so leaving them unset
                # NameErrors on the first sub-1s chunk and reads the previous
                # chunk's values on later ones.
                text = " ".join(chunk_words)
                drop_chunk = False
                reason_rejected = None

                if chunk_words and chunk_duration >= 1.0:
                    chunk_t0 = time.monotonic()

                    # 1. Quality Filtering: Check duration
                    if chunk_duration < min_chunk_duration:
                        drop_chunk = True
                        reason_rejected = f"too_short ({chunk_duration:.2f}s < {min_chunk_duration}s)"
                        stats['source_action']['dropped_short'] += 1
                        logger.info(f"  ↪ DROPPED chunk at {current_start:.2f}s (too short: {chunk_duration:.2f}s)")
                    
                    if not drop_chunk:
                        # Read audio for confidence and SNR checks
                        t0 = time.monotonic()
                        audio_slice = _read_audio_segment(audio_24k_source, current_start, chunk_end_time)
                        timing['audio_read'] += time.monotonic() - t0

                        # Check confidence
                        confidences = [w.get("confidence", 1.0) for w in chunk_word_data]
                        avg_confidence = np.mean(confidences) if confidences else 1.0
                        if avg_confidence < min_confidence:
                            drop_chunk = True
                            reason_rejected = f"low_confidence ({avg_confidence:.2f} < {min_confidence})"
                            stats['source_action']['dropped_low_quality'] += 1
                            timing['dropped_chunks'] += 1
                            logger.info(f"  ↪ DROPPED chunk at {current_start:.2f}s ({reason_rejected})")

                        # Check SNR
                        if not drop_chunk:
                            t0 = time.monotonic()
                            snr = _calculate_chunk_snr(audio_slice)
                            timing['snr_calc'] += time.monotonic() - t0
                            if snr < min_snr:
                                drop_chunk = True
                                reason_rejected = f"low_snr ({snr:.1f}dB < {min_snr}dB)"
                                stats['source_action']['dropped_low_quality'] += 1
                                timing['dropped_chunks'] += 1
                                logger.info(f"  ↪ DROPPED chunk at {current_start:.2f}s ({reason_rejected})")

                    # 2. Deduplication: Check for narrator retakes
                    if (not drop_chunk and prev_raw_text
                            and prev_raw_speaker == chunk_word_data[0].get("speaker", "UNKNOWN")):
                        # Use SequenceMatcher for a fuzzy text similarity check.
                        # Narrator retakes often vary slightly in wording or ASR noise.
                        sm = difflib.SequenceMatcher(None, prev_raw_text.lower(), text.lower())
                        similarity = sm.ratio()
                        if similarity > 0.85:
                            drop_chunk = True
                            reason_rejected = f"duplicate (similarity {similarity:.2f} > 0.85)"
                            stats['source_action']['deduplicated'] += 1
                            logger.info(f"  ↪ DROPPED chunk at {current_start:.2f}s (duplicate/retake detected)")

                    # ── Source-guided alignment (only when --source is set) ──
                    source_words_for_merge = None
                    if not drop_chunk and source_state is not None:
                        t0_align = time.monotonic()
                        chunk_match_words = alignment.to_words(text)
                        cursor_before = source_state['cursor']
                        sa_start, sa_end, sa_ratio = alignment.find_best_match(
                            chunk_match_words,
                            source_state['orig_match'],
                            cursor_before,
                            proper_nouns=source_state['proper_nouns'],
                        )
                        # Three-tier recovery, mirroring compare's run() loop:
                        #
                        #   tier 0: find_best_match (already done above) —
                        #     narrow ±200 word window around cursor.
                        #   tier 1: realign — wide forward search up to 3000
                        #     source words past cursor. Cheap, catches the
                        #     common case where audio skipped a paragraph or
                        #     two of source.
                        #   tier 2: find_anchor_position — full-source scan.
                        #     Expensive but rare; catches catastrophic loss
                        #     where audio jumped chapters, or the EPUB's
                        #     front-matter order put content far from where
                        #     the cursor expected it.
                        #
                        # Tier 0 entry is gated on source_threshold itself (not
                        # a hardcoded catastrophic-only bar) so recovery fires
                        # for every chunk that wouldn't otherwise be accepted,
                        # not just severe ASR/source drift. Tier 1 -> tier 2
                        # escalation is the logical complement of tier 1's own
                        # acceptance bar, so every tier-1 rejection gets a
                        # tier-2 attempt rather than only catastrophic ones.
                        # See FIXED.md F-113/F-114.
                        if sa_ratio < source_threshold and len(chunk_match_words) >= 5:
                            r_start, r_end, r_ratio = alignment.realign(
                                chunk_match_words,
                                source_state['orig_match'],
                                cursor_before,
                                proper_nouns=source_state['proper_nouns'],
                            )
                            if r_ratio >= 0.55 and r_ratio > sa_ratio + 0.15:
                                stats['realign_events'] += 1
                                logger.debug(
                                    f"source-realign idx={segment_idx} "
                                    f"local {sa_ratio:.3f} → wide {r_ratio:.3f} "
                                    f"cursor {cursor_before}→{r_end} "
                                    f"(jumped {r_end - cursor_before} words)"
                                )
                                sa_start, sa_end, sa_ratio = r_start, r_end, r_ratio
                            else:
                                # Tier 2: full-source scan. Same logic compare
                                # uses for catastrophic alignment loss. Requires
                                # both an absolute bar (>=0.60) AND a clear
                                # improvement over the local ratio (+0.40) so
                                # we don't false-positive on chunks that
                                # genuinely have no source equivalent (audio-
                                # only credits, narrator inserts, etc.) —
                                # those should still be dropped.
                                a_start, a_end, a_ratio = alignment.find_anchor_position(
                                    chunk_match_words,
                                    source_state['orig_match'],
                                    overlap_ratio_hint=0.6,
                                )
                                if a_ratio >= 0.6 and a_ratio > sa_ratio + 0.4:
                                    stats['reanchor_events'] += 1
                                    # Trim the wide-anchor window down to the
                                    # actual aligned region so the source span
                                    # we use is tight, not the full +slop window.
                                    t_start, t_end = alignment.trim_span_to_alignment(
                                        chunk_match_words,
                                        source_state['orig_match'],
                                        a_start, a_end,
                                        source_state['proper_nouns'],
                                    )
                                    if t_end > t_start:
                                        a_start, a_end = t_start, t_end
                                        a_ratio = alignment._ratio(
                                            chunk_match_words,
                                            source_state['orig_match'][a_start:a_end],
                                        )
                                    _jump = a_end - cursor_before
                                    logger.info(
                                        f"  ↪ chunk {segment_idx}: full-source re-anchor "
                                        f"ratio={a_ratio:.3f} cursor "
                                        f"{cursor_before}→{a_end} (jumped "
                                        f"{_jump:+d} words)"
                                    )
                                    if _jump < -5000:
                                        stats['reanchor_backward'] += 1
                                        logger.warning(
                                            f"  ⚠ Large backward re-anchor ({_jump:+d} words) — "
                                            f"source structure may not match this audio file"
                                        )
                                    sa_start, sa_end, sa_ratio = a_start, a_end, a_ratio
                        if sa_ratio >= source_threshold:
                            asr_preview = (text[:60] + '…') if len(text) > 60 else text
                            source_words_for_merge = source_state['orig_display'][sa_start:sa_end]
                            text = ' '.join(source_words_for_merge)
                            source_state['cursor'] = sa_end
                            stats['source_action']['replace'] += 1
                            src_preview = (text[:60] + '…') if len(text) > 60 else text
                            logger.debug(
                                f"source-replace idx={segment_idx} ratio={sa_ratio:.3f} "
                                f"cursor {cursor_before}→{sa_end} (+{sa_end - cursor_before}) "
                                f"asr={asr_preview!r} src={src_preview!r}"
                            )
                        elif keep_unaligned:
                            stats['source_action']['keep_asr'] += 1
                            logger.info(
                                f"  ↪ chunk {segment_idx} kept (ASR text); "
                                f"source ratio {sa_ratio:.2f} < {source_threshold} "
                                f"(cursor stays at {cursor_before})"
                            )
                            # Cursor stays put — don't advance through source
                            # we couldn't confidently match.
                        else:
                            stats['source_action']['dropped'] += 1
                            logger.info(
                                f"  ↪ DROPPED chunk at {current_start:.2f}s "
                                f"(source ratio {sa_ratio:.2f} < {source_threshold}, "
                                f"cursor stays at {cursor_before})"
                            )
                            asr_preview = (text[:80] + '…') if len(text) > 80 else text
                            logger.debug(f"dropped chunk asr={asr_preview!r}")
                            drop_chunk = True
                            reason_rejected = f"low_source_ratio ({sa_ratio:.2f} < {source_threshold})"

                        timing['alignment'] += time.monotonic() - t0_align

                if drop_chunk and reason_rejected:
                    # Log rejected chunk separately for review
                    with open(os.path.join(temp_dir, "rejected_chunks.jsonl"), "a", encoding="utf-8") as rf:
                        json.dump({
                            "segment_idx": segment_idx,
                            "reason": reason_rejected,
                            "text": text,
                            "start": current_start,
                            "end": chunk_end_time,
                            "duration": chunk_duration
                        }, rf)
                        rf.write("\n")

                if chunk_words and chunk_duration >= 1.0 and not drop_chunk:
                    # Build context for continuity
                    ctx = " ".join(list(context)[-2:]) if context else ""

                    source_position = ({'identity': source_identity,
                                        'cursor': source_state['cursor']}
                                       if source_state is not None else None)
                    if batch_size > 1:
                        # Collect into batch buffer
                        if not batch_buffer:
                            batch_started_at = chunk_t0
                        batch_buffer.append({
                            "segment_idx": segment_idx,
                            "text": text,
                            "ctx": ctx,
                            "source_words_for_merge": source_words_for_merge,
                            "audio_slice": audio_slice,
                            "chunk_word_data": chunk_word_data,
                            "current_start": current_start,
                            "chunk_end_time": chunk_end_time,
                            "chunk_duration": chunk_duration,
                            "source_position": source_position,
                        })

                        # Process batch when full
                        if len(batch_buffer) >= batch_size:
                            flushed_count = apply_annotation_batch(segment_idx)
                            # IMPROVEMENT 1: Add timing/ETA logging for batch mode
                            completed = segment_idx + flushed_count
                            # Divide by batch_size: this sample spans the whole
                            # batch's LLM call, but the ETA below multiplies the
                            # average by remaining CHUNKS — so store per-chunk time
                            # to avoid inflating the ETA ~batch_size×.
                            chunk_times.append((time.monotonic() - batch_started_at) / flushed_count)
                            batch_started_at = None
                            if completed // 10 > segment_idx // 10:
                                avg_chunk_s = sum(chunk_times) / len(chunk_times)
                                elapsed_s = time.monotonic() - annotation_start_time
                                remaining_chunks = max(0, estimated_chunks_total - completed)
                                remaining_s = remaining_chunks * avg_chunk_s
                                logger.info(
                                    f"  ↳ Progress: {completed} chunks | Estimated total: ~{estimated_chunks_total} "
                                    f"| Avg: {avg_chunk_s:.1f}s/chunk "
                                    f"| Elapsed: {format_duration(elapsed_s)} "
                                    f"| ETA: {format_duration(remaining_s) if remaining_chunks or next_word_data is None else 'unknown'}"
                                )
                                log_gpu_stats(f"annotation segment {completed} (estimated total ~{estimated_chunks_total})")
                            if completed // 100 > segment_idx // 100:
                                total_timed = timing['audio_read'] + timing['snr_calc'] + timing['alignment'] + \
                                              timing['llm_infer'] + timing['sanitize'] + timing['wav_write']
                                logger.info(
                                    f"  ⏱ Timing breakdown (chunk {completed}): "
                                    f"audio_read={timing['audio_read']:.1f}s "
                                    f"snr={timing['snr_calc']:.1f}s "
                                    f"alignment={timing['alignment']:.1f}s "
                                    f"llm={timing['llm_infer']:.1f}s "
                                    f"sanitize={timing['sanitize']:.1f}s "
                                    f"wav_write={timing['wav_write']:.1f}s "
                                    f"total_timed={total_timed:.1f}s "
                                    f"dropped={timing['dropped_chunks']} "
                                    f"kept={timing['kept_chunks']}"
                                )
                            # Skip per-chunk processing for batched chunks
                            segment_idx += batch_size
                            prev_raw_text = text
                            prev_raw_speaker = chunk_word_data[0].get("speaker", "UNKNOWN")
                            chunk_t0 = time.monotonic()
                            # Carry-forward MUST happen before continue (BUG 1 fix)
                            current_words       = current_words[cut_at + 1:]
                            current_word_starts = current_word_starts[cut_at + 1:]
                            current_word_ends   = current_word_ends[cut_at + 1:]
                            current_start = current_word_starts[0] if current_word_starts else chunk_end_time
                            continue  # Skip the per-chunk code below

                        # Batch not full yet — buffer this chunk and skip per-chunk processing (BUG D fix)
                        current_words       = current_words[cut_at + 1:]
                        current_word_starts = current_word_starts[cut_at + 1:]
                        current_word_ends   = current_word_ends[cut_at + 1:]
                        current_start = current_word_starts[0] if current_word_starts else chunk_end_time
                        prev_raw_text = text
                        # Context is updated by full-flush and tail paths — don't double-append here (BUG E fix)
                        continue  # Skip the per-chunk code below

                    # Per-chunk mode (batch_size=1)
                    # Get annotation via per-chunk LLM call
                    user_prompt = f"Previous context: {ctx}\n\nAnnotate this segment:\n{text}" if ctx else f"Annotate this segment:\n{text}"

                    try:
                        t0_llm = time.monotonic()
                        response = llm.create_chat_completion(
                            messages=[
                                {"role": "system", "content": TTS_ANNOTATION_SYSTEM_PROMPT},
                                {"role": "user", "content": user_prompt},
                            ],
                            max_tokens=512,
                            temperature=0.3,
                        )
                        timing['llm_infer'] += time.monotonic() - t0_llm
                        annotated_raw = response["choices"][0]["message"]["content"].strip()
                        if not annotated_raw:
                            raise RuntimeError("LLM returned an empty response")

                        t0_sanitize = time.monotonic()
                        annotated = get_validated_annotation(
                            annotated_raw, text, source_words_for_merge, alignment)
                        timing['sanitize'] += time.monotonic() - t0_sanitize

                        stats['llm_success'] += 1
                        if annotated != annotated_raw:
                            stats['sanitize_changed'] += 1

                        if segment_idx == next_segment_idx:
                            logger.info(f"✓ LLM annotation response received - {os.path.basename(active_model_path)}")
                    except Exception as e:
                        stats['llm_fail'] += 1
                        _check_llm_fail_rate(stats)
                        logger.warning(f"Annotation failed for segment {segment_idx}, using original text: {e}")
                        annotated = text

                    # Save audio and write metadata
                    _save_chunk_metadata(
                        {
                            "audio_slice": audio_slice,
                            "chunk_word_data": chunk_word_data,
                            "current_start": current_start,
                            "chunk_end_time": chunk_end_time,
                            "chunk_duration": chunk_duration,
                            "source_position": source_position,
                        },
                        annotated, character, narrator_style, book_title,
                        metadata, checkpoint_file, stats, timing,
                        segment_idx, temp_dir,
                    )

                    chunk_times.append(time.monotonic() - chunk_t0)

                    # Dynamic ETA from rolling average
                    if (segment_idx + 1) % 10 == 0:
                        avg_chunk_s = sum(chunk_times) / len(chunk_times)
                        elapsed_s = time.monotonic() - annotation_start_time
                        remaining_chunks = max(0, estimated_chunks_total - segment_idx - 1)
                        remaining_s = remaining_chunks * avg_chunk_s
                        logger.info(
                            f"  ↳ Progress: {segment_idx + 1}/{estimated_chunks_total} chunks "
                            f"| Avg: {avg_chunk_s:.1f}s/chunk "
                            f"| Elapsed: {format_duration(elapsed_s)} "
                            f"| ETA: {format_duration(remaining_s)}"
                        )
                        log_gpu_stats(f"annotation segment {segment_idx + 1}/{estimated_chunks_total}")

                    # Periodic timing breakdown (every 100 chunks)
                    if (segment_idx + 1) % 100 == 0:
                        total_timed = timing['audio_read'] + timing['snr_calc'] + timing['alignment'] + \
                                      timing['llm_infer'] + timing['sanitize'] + timing['wav_write']
                        logger.info(
                            f"  ⏱ Timing breakdown (chunk {segment_idx + 1}): "
                            f"audio_read={timing['audio_read']:.1f}s "
                            f"snr={timing['snr_calc']:.1f}s "
                            f"alignment={timing['alignment']:.1f}s "
                            f"llm={timing['llm_infer']:.1f}s "
                            f"sanitize={timing['sanitize']:.1f}s "
                            f"wav_write={timing['wav_write']:.1f}s "
                            f"total_timed={total_timed:.1f}s "
                            f"dropped={timing['dropped_chunks']} "
                            f"kept={timing['kept_chunks']}"
                        )

                    segment_idx += 1
                    prev_raw_text = text  # Record for next chunk deduplication check
                    prev_raw_speaker = chunk_word_data[0].get("speaker", "UNKNOWN")

                    context.append(text)
                # Carry the post-cut tail forward as the start of the next chunk.
                # The chunker previously dropped everything; now any words that
                # the look-back trimmed off become the seed of the next chunk.
                current_words       = current_words[cut_at + 1:]
                current_word_starts = current_word_starts[cut_at + 1:]
                current_word_ends   = current_word_ends[cut_at + 1:]
                current_start = current_word_starts[0] if current_word_starts else chunk_end_time

    # ── Process remaining batch buffer ────────────────────────────────────────
    # Any chunks left in the batch buffer need to be annotated and saved.
        if batch_buffer and len(batch_buffer) > 0:
            logger.info(f"▶ Processing remaining batch of {len(batch_buffer)} chunks...")
            tail_count = apply_annotation_batch(segment_idx)
        # BUG B fix: increment segment_idx for tail batch
            segment_idx += tail_count

    finally:
        checkpoint_file.close()
        del llm
        clear_vram()

    # ── Batch metadata tagging (avoids per-chunk WAV open/read/write) ─────────
    t0_tag = time.monotonic()
    try:
        taggable = [e for e in metadata if e.get("wav_path") and os.path.exists(e["wav_path"])]
        if taggable:
            logger.info(f"▶ Batch-writing ID3 tags to {len(taggable)} WAV files...")
            for entry in taggable:
                try:
                    wav_path = entry["wav_path"]
                    apply_wave_tags(wav_path, entry)
                except Exception as tag_e:
                    logger.warning(f"Failed to write metadata tags to {entry.get('audio_filepath', '?')}: {tag_e}")
            logger.info(f"  ✓ Batch tagging complete")
    except ImportError:
        logger.debug("mutagen not installed, skipping batch metadata tagging")
    timing['batch_tag'] = time.monotonic() - t0_tag

    # Clean up internal fields from metadata before writing outputs
    for entry in metadata:
        entry.pop("wav_path", None)
        entry.pop("book_title", None)

    # ── Timing summary ───────────────────────────────────────────────────────
    total_timed = sum(v for k, v in timing.items() if k not in ('dropped_chunks', 'kept_chunks'))
    logger.info("─" * 60)
    logger.info("ANNOTATION TIMING BREAKDOWN")
    logger.info("─" * 60)
    for key, label in [
        ('audio_read', 'Audio slice read (SNR check)'),
        ('snr_calc', 'SNR calculation'),
        ('alignment', 'Source-guided alignment'),
        ('llm_infer', 'LLM inference'),
        ('sanitize', 'Sanitization / merge'),
        ('wav_write', 'WAV write + fsync'),
        ('batch_tag', 'Batch ID3 tagging'),
    ]:
        pct = 100 * timing[key] / max(total_timed, 1e-9)
        logger.info(f"  {label:<30} {timing[key]:>8.1f}s  ({pct:5.1f}%)")
    logger.info(f"  {'TOTAL TIMED':<30} {total_timed:>8.1f}s  (100.0%)")
    logger.info(f"  Dropped chunks (before LLM):    {timing['dropped_chunks']}")
    logger.info(f"  Kept chunks (full pipeline):     {timing['kept_chunks']}")
    if timing['kept_chunks'] > 0:
        per_kept = total_timed / timing['kept_chunks']
        logger.info(f"  Avg time per kept chunk:         {per_kept:.3f}s")
    logger.info("─" * 60)

    # ── Comprehensive end-of-chunker summary ──────────────────────────────────
    # Most of what's useful for iterating on the chunker / source mode is in
    # the histograms below. Compare run-to-run to see which cut strategy is
    # firing, how many chunks the source mode dropped vs. replaced, whether
    # the LLM is failing repeatedly, etc.
    chunks_emitted_this_run = segment_idx - next_segment_idx
    logger.info(f"✓ Annotation complete: {segment_idx} total segments "
                f"({chunks_emitted_this_run} new this run)")

    # Cut-strategy histogram (per chunk-emit decision, includes dropped chunks)
    if stats['cut_strategy']:
        total_cuts = sum(stats['cut_strategy'].values())
        logger.info(f"  Cut strategy distribution ({total_cuts} chunks):")
        for strategy in ('speaker_turn', 'sentence_end', 'pause', 'fallback', 'is_final',
                         'too_few_words', 'undersized'):
            count = stats['cut_strategy'].get(strategy, 0)
            if count:
                pct = 100 * count / total_cuts
                logger.info(f"    {strategy:<14} : {count:>6} ({pct:5.1f}%)")

    # Source-mode histogram (only when --source was used)
    if source_state is not None:
        sa_replace = stats['source_action']['replace']
        sa_keep    = stats['source_action']['keep_asr']
        sa_drop    = stats['source_action']['dropped']
        sa_short   = stats['source_action']['dropped_short']
        sa_dedup   = stats['source_action']['deduplicated']
        sa_low_qual = stats['source_action']['dropped_low_quality']
        sa_total   = sa_replace + sa_keep + sa_drop + sa_short + sa_dedup + sa_low_qual
        if sa_total:
            logger.info(f"  Source-guided actions ({sa_total} chunks aligned):")
            logger.info(f"    replace        : {sa_replace:>6} ({100*sa_replace/sa_total:5.1f}%)")
            logger.info(f"    keep_asr       : {sa_keep:>6} ({100*sa_keep/sa_total:5.1f}%)")
            logger.info(f"    dropped        : {sa_drop:>6} ({100*sa_drop/sa_total:5.1f}%)")
            if sa_short:
                logger.info(f"    dropped_short  : {sa_short:>6} ({100*sa_short/sa_total:5.1f}%)")
            if sa_dedup:
                logger.info(f"    deduplicated   : {sa_dedup:>6} ({100*sa_dedup/sa_total:5.1f}%)")
            if sa_low_qual:
                logger.info(f"    dropped_low_qual: {sa_low_qual:>6} ({100*sa_low_qual/sa_total:5.1f}%)")
        _src_cursor = source_state['cursor']
        _src_total  = len(source_state['orig_match'])
        _src_pct    = 100 * _src_cursor / _src_total if _src_total else 0
        logger.info(f"  Source cursor finished at word "
                    f"{_src_cursor:,} / {_src_total:,} ({_src_pct:.0f}% coverage)")
        if _src_pct < 60:
            logger.warning(
                f"  ⚠ Low source coverage ({_src_pct:.0f}%): the EPUB likely contains "
                f"more content than this audio file (e.g. multiple volumes)."
            )
        if stats['reanchor_backward'] >= 2:
            logger.warning(
                f"  ⚠ {stats['reanchor_backward']} large backward re-anchors detected — "
                f"source text structure does not align well with this audio."
            )

    # LLM + sanitisation
    llm_total = stats['llm_success'] + stats['llm_fail']
    if llm_total:
        _san_pct = 100 * stats['sanitize_changed'] / llm_total if llm_total else 0
        logger.info(f"  LLM annotations: {stats['llm_success']} ok, "
                    f"{stats['llm_fail']} failed "
                    f"({stats['sanitize_changed']} cleaned by sanitiser, {_san_pct:.0f}%)")
        if _san_pct > 80:
            logger.warning(
                f"  ⚠ High sanitiser rate ({_san_pct:.0f}%): LLM output is consistently "
                f"reformatted — review prompt template or model output format."
            )

    if stats['llm_batch_fail']:
        logger.info(f"  LLM batch attempts: {stats['llm_batch_fail']} failed; per-chunk fallback used")

    # Duration distribution of emitted chunks (audio-side, not source-side)
    durs = stats['chunk_durations']
    if durs:
        logger.info(f"  Emitted chunk durations (n={len(durs)}):")
        logger.info(f"    min  {min(durs):.2f}s   p50 {_percentile(durs, 50):.2f}s   "
                    f"p95 {_percentile(durs, 95):.2f}s   max {max(durs):.2f}s")
        # Useful flag: if many chunks are way under chunk_size, the cut
        # logic is picking earlier breaks than the user expects.
        short_count = sum(1 for d in durs if d < chunk_size * 0.5)
        if short_count:
            logger.info(f"    chunks < 50% target ({chunk_size * 0.5:.1f}s): "
                        f"{short_count} ({100*short_count/len(durs):.1f}%)")

    if stats['audio_short']:
        logger.warning(f"  ⚠ Audio-slice mismatches: {stats['audio_short']} chunk(s) "
                       f"shorter than expected — likely end-of-file or read truncation")

    if metadata:
        total_duration = sum(m["duration"] for m in metadata)
        logger.info(f"  Total audio in dataset    : {total_duration:.1f}s "
                    f"({total_duration/60:.1f} min)")
        logger.info(f"  Average segment duration  : {total_duration/len(metadata):.2f}s")

    if summary_output:
        save_annotation_summary(summary_output, run_identity, metadata,
                                len(existing_entries), stats, source_state)
    return metadata


def get_preparer_report_path(report_path, run_identity, asr_output=None, scratch_audio=None):
    path = Path(report_path).resolve()
    inputs = [run_identity['audio'], run_identity['source']]
    if any(path == Path(item['path']).resolve() for item in inputs if item is not None):
        raise RunStateError('Report cannot overwrite an audio/source input')
    reserved = [asr_output or 'dataset_temp/asr_segments.json',
                scratch_audio or 'dataset_temp/audio_24k_scratch.wav']
    if (path.is_relative_to(Path('dataset_temp').resolve())
            or any(path == Path(item).resolve() for item in reserved)):
        raise RunStateError('Report cannot overwrite preparer phase artifacts')
    return path


def validate_preparer_report_inputs(run_identity):
    for item in (run_identity['audio'], run_identity['source']):
        if item is not None and get_file_identity(item['path']) != item:
            raise RunStateError('Preparer report input changed')


def save_annotation_summary(output, run_identity, metadata, resumed_segments, stats, source_state):
    options = run_identity['options']
    path = get_preparer_report_path(output, run_identity,
        asr_output=options.get('asr_output'), scratch_audio=options.get('scratch_audio'))
    validate_preparer_report_inputs(run_identity)
    counters = {key: dict(value) if isinstance(value, Counter) else list(value) if isinstance(value, list) else value
                for key, value in stats.items()}
    report = {'version': 1, 'phase': 'annotation_complete', 'identity': run_identity,
              'counter_scope': 'current_attempt',
              'totals': {'segments_total': len(metadata),
                         'segments_this_run': len(metadata) - resumed_segments,
                         'resumed_segments': resumed_segments,
                         'dataset_seconds': float(sum(item['duration'] for item in metadata))},
              'counters': counters, 'source_cursor': None}
    if source_state is not None:
        report['source_cursor'] = {'word': source_state['cursor'],
                                   'total_words': len(source_state['orig_match'])}
    write_json_atomic(report, path)

def get_source_alignment_prescan(word_segments, chunk_size, source_path, source_threshold,
                                 source_start=None, source_start_text=None,
                                 no_auto_anchor=False):
    """Use the annotation path's provisional chunks, anchoring and thresholds."""
    entries = _build_provisional_entries_for_anchor(word_segments, chunk_size, max_entries=30)
    state = _build_source_state(source_path, source_start=source_start,
        source_start_text=source_start_text, no_auto_anchor=no_auto_anchor,
        entries_for_anchor=entries)
    quality = alignment.estimate_alignment_quality(entries, state['orig_match'], state['cursor'],
        start_entry_idx=state['anchor_entry_idx'], threshold=source_threshold,
        proper_nouns=state['proper_nouns'])
    return state, quality


def save_source_alignment_prescan(word_segments, args, run_identity):
    """Publish an actual sampled estimate, bound to unchanged source/audio bytes."""
    report_path = get_preparer_report_path(args.alignment_report, run_identity,
        asr_output=args.asr_output, scratch_audio=args.scratch_audio)
    validate_preparer_report_inputs(run_identity)
    state, quality = get_source_alignment_prescan(word_segments, args.chunk_size, args.source,
        source_start=args.source_start, source_start_text=args.source_start_text,
        no_auto_anchor=args.no_auto_anchor, source_threshold=args.source_threshold)
    average, sampled, below, review = quality
    if not sampled:
        raise RunStateError('Alignment pre-scan produced no evaluable samples')
    validate_preparer_report_inputs(run_identity)
    report = {'version': 1, 'identity': run_identity, 'scope': 'initial_provisional_chunks',
              'quality': {'average_ratio': average, 'sampled': sampled,
                          'below_60_percent': below, 'review_needed': review},
              'anchor': {'source_word': state['cursor'], 'entry_index': state['anchor_entry_idx']}}
    write_json_atomic(report, report_path)


# ── Output zip naming from source metadata ────────────────────────────────────
# When --output is left at the default (or another well-known placeholder), try
# to derive a self-describing zip name from the source ePub's title/author, or
# fall back to the source file's stem. Keeps generated dataset names useful
# without forcing the caller to compute one.
_NAME_GENERIC_OUTPUTS = {"alexandria_dataset.zip", "dataset.zip", "output.zip"}
_NAME_MAX_PART_LEN = 80  # per-token cap; long ebook titles can run 100+ chars
_NAME_DASH_TRANSLATE = str.maketrans({"—": "-", "–": "-", "−": "-"})
_NAME_SANITIZE_RE = re.compile(r"[^A-Za-z0-9_-]")
_NAME_COLLAPSE_RE = re.compile(r"([_-])[_-]+")  # _-_ → -, ___ → _


def _sanitize_name_part(text) -> str:
    """Make a single naming token filesystem-safe: normalize unicode dashes,
    spaces→_, strip everything else, collapse runs of _ and -.

    Coerce to str() because pathological ePubs can yield BeautifulSoup tags or
    tuples from Dublin Core fields, and we'd rather degrade to a sanitized
    repr than crash the pipeline before processing starts.
    """
    text = str(text).strip().translate(_NAME_DASH_TRANSLATE).replace(" ", "_")
    text = _NAME_SANITIZE_RE.sub("", text)
    text = _NAME_COLLAPSE_RE.sub(r"\1", text)
    return text[:_NAME_MAX_PART_LEN].strip("_-")


def extract_metadata_for_naming(source_path: str) -> tuple[Optional[str], Optional[str]]:
    """Return (title, author) from an ePub, or (None, None) on any failure.

    Non-epub sources and missing/parse-failing ePubs return (None, None) so the
    caller can fall through to the filename-stem branch without special-casing.
    """
    if not source_path or not os.path.exists(source_path):
        return None, None
    if not source_path.lower().endswith(".epub"):
        return None, None
    if not getattr(alignment, "EPUB_AVAILABLE", False):
        return None, None
    try:
        book = alignment.epub.read_epub(source_path, options={"ignore_ncx": True})
        title_md = book.get_metadata("DC", "title")
        author_md = book.get_metadata("DC", "creator")
        title = title_md[0][0] if title_md else None
        author = author_md[0][0] if author_md else None
        return title, author
    except Exception as e:
        logger.warning(f"Could not read ePub metadata from {source_path}: {e}")
        return None, None


def generate_zip_filename(source_path: Optional[str],
                          title: Optional[str],
                          author: Optional[str]) -> str:
    """Derive a zip filename from ePub metadata, source stem, or fall back to
    'alexandria_dataset.zip'. Sanitization keeps only [A-Za-z0-9_-]."""
    safe_title = _sanitize_name_part(title) if title else ""
    safe_author = _sanitize_name_part(author) if author else ""
    if safe_title and safe_author:
        return f"{safe_title}_{safe_author}.zip"
    if safe_title:
        return f"{safe_title}.zip"
    if source_path:
        stem = os.path.splitext(os.path.basename(source_path))[0]
        safe_stem = _sanitize_name_part(stem)
        if safe_stem:
            return f"{safe_stem}.zip"
    return "alexandria_dataset.zip"


def maybe_autoname_output(output: str, source_path: Optional[str], title: Optional[str] = None, author: Optional[str] = None) -> str:
    """Replace a generic --output value with a derived name, preserving the
    user-supplied directory if one was given. Returns the original `output`
    unchanged when the caller pinned a non-generic name (anything outside
    _NAME_GENERIC_OUTPUTS) so an explicit choice is always respected."""
    basename = os.path.basename(output) if output else ""
    if basename not in _NAME_GENERIC_OUTPUTS:
        return output
    derived = generate_zip_filename(source_path, title, author)
    parent = os.path.dirname(output) if output else ""
    return os.path.join(parent, derived) if parent else derived


SPEAKER_GAP_BRIDGE_MAX_DISTANCE_SECS = 0.5


def _assign_speakers_to_words(word_segments, speaker_segments):
    """Assign words inside speaker turns and short same-speaker VAD gaps."""
    if not speaker_segments:
        for word in word_segments:
            word["speaker"] = "UNKNOWN"
        return word_segments, {"UNKNOWN"}

    ordered_speaker_segments = sorted(
        speaker_segments,
        key=lambda segment: segment["start"],
    )
    speaker_segment_starts = [
        segment["start"] for segment in ordered_speaker_segments
    ]

    IntervalTree, Interval = _lazy_import_intervaltree()
    tree = IntervalTree()
    for seg in ordered_speaker_segments:
        tree.add(Interval(seg["start"], seg["end"], seg["speaker"]))

    unassigned_words = 0
    bridged_words = 0
    speaker_counts = Counter()

    for word in word_segments:
        word_mid_point = (word["start"] + word["end"]) / 2
        overlapping_intervals = tree.at(word_mid_point)
        
        if overlapping_intervals:
            speaker = next(iter(overlapping_intervals)).data
            word["speaker"] = speaker
            speaker_counts[speaker] += 1
        else:
            following_index = bisect.bisect_right(
                speaker_segment_starts,
                word_mid_point,
            )
            previous_segment = (
                ordered_speaker_segments[following_index - 1]
                if following_index > 0 else None
            )
            following_segment = (
                ordered_speaker_segments[following_index]
                if following_index < len(ordered_speaker_segments) else None
            )
            can_bridge_gap = (
                previous_segment is not None
                and following_segment is not None
                and previous_segment["speaker"] == following_segment["speaker"]
                and 0 <= word_mid_point - previous_segment["end"]
                <= SPEAKER_GAP_BRIDGE_MAX_DISTANCE_SECS
                and 0 <= following_segment["start"] - word_mid_point
                <= SPEAKER_GAP_BRIDGE_MAX_DISTANCE_SECS
            )
            if can_bridge_gap:
                speaker = previous_segment["speaker"]
                word["speaker"] = speaker
                speaker_counts[speaker] += 1
                bridged_words += 1
            else:
                word["speaker"] = "UNKNOWN"
                unassigned_words += 1

    logger.info("▶ Speaker assignment summary:")
    for speaker, count in speaker_counts.most_common():
        logger.info(f"  - {speaker}: {count} words")
    if bridged_words > 0:
        logger.info(
            f"  - Bridged {bridged_words} word(s) across short "
            "same-speaker diarization gaps"
        )
    if unassigned_words > 0:
        logger.warning(f"  - UNKNOWN: {unassigned_words} words (no speaker segment overlap)")
        
    unique_speakers = set(speaker_counts.keys())
    if unassigned_words > 0:
        unique_speakers.add("UNKNOWN")
        
    return word_segments, unique_speakers


def get_training_eligible_chunks(metadata):
    """Return verified single-speaker rows and mixed/unverifiable counts."""
    eligible = []
    mixed = 0
    unverifiable = 0
    for entry in metadata:
        labels = entry.get("speaker_labels")
        if (not isinstance(labels, list) or not labels
                or any(not isinstance(label, str) or not label.strip() for label in labels)):
            unverifiable += 1
        elif len(set(labels)) > 1:
            mixed += 1
        elif entry.get("speaker", "UNKNOWN") not in labels:
            unverifiable += 1
        else:
            eligible.append(entry)
    return eligible, mixed, unverifiable


def _create_zip_dataset(metadata: List[Dict], output_path: str, val_split: float = 0.10, zip_max_files: int = 200, unique_speakers: set = None):
    """Bundle annotated chunks and metadata into segmented ZIP files (volumes),
    grouped by speaker, character, and narrator style."""
    temp_dir = "dataset_temp"
    
    if not metadata:
        raise RunStateError("No metadata to save to ZIP.")

    for entry in metadata:
        wav_path = get_sample_path(temp_dir, entry.get('audio_filepath'))
        if not wav_path.is_file():
            raise RunStateError(f'Missing sample WAV for ZIP: {wav_path}')

    metadata, mixed, unverifiable = get_training_eligible_chunks(metadata)
    logger.info(f"Training ZIP eligibility: {len(metadata)} single-speaker chunks; "
                f"excluded {mixed} mixed and {unverifiable} unverifiable chunks. "
                "Raw WAVs and checkpoint metadata are retained.")
    if not metadata:
        raise RunStateError("No eligible single-speaker chunks to save to ZIP; "
                            "mixed or unverifiable chunks were excluded.")

    # 1. Group metadata by speaker, then (character, style)
    groups = {}
    for entry in metadata:
        speaker = entry.get("speaker", "UNKNOWN")
        char = entry.get("character", "narrator")
        style = entry.get("narrator_style", "default")
        key = (speaker, char, style)
        groups.setdefault(key, []).append(entry)

    logger.info(f"▶ Creating segmented ZIP volumes (max {zip_max_files} files/vol, val_split={val_split:.0%})")
    logger.info(f"  ├─ Unique speaker/character/style combinations: {len(groups)}")

    import random
    base, ext = os.path.splitext(output_path)
    manifest_path = output_path + '.volumes.json'
    previous_volumes = []
    if os.path.exists(manifest_path):
        with open(manifest_path, encoding='utf-8') as stream:
            previous_volumes = json.load(stream)['volumes']
        output_name = os.path.basename(output_path)
        output_stem = os.path.splitext(output_name)[0]
        if not isinstance(previous_volumes, list) or any(
                not isinstance(name, str) or os.path.dirname(name)
                or not name.endswith(ext)
                or not (name == output_name or name.startswith(output_stem + '_'))
                for name in previous_volumes):
            raise RunStateError(f'Invalid ZIP volume manifest: {manifest_path}')
    planned = []
    used_paths = set()
    for (speaker, char, style), group_metadata in groups.items():
        num_vols = (len(group_metadata) + zip_max_files - 1) // zip_max_files
        safe_speaker = _sanitize_name_part(speaker)
        safe_char = _sanitize_name_part(char)
        safe_style = _sanitize_name_part(style)
        for vol_idx in range(num_vols):
            parts = [base]
            if unique_speakers and len(unique_speakers) > 1:
                parts.append(safe_speaker)
            if safe_char != 'narrator':
                parts.append(safe_char)
            if safe_style != 'default':
                parts.append(safe_style)
            if num_vols > 1:
                parts.append(f'vol{vol_idx + 1:02d}')
            vol_path = '_'.join(parts) + ext if len(parts) > 1 else output_path
            if vol_path in used_paths:
                import hashlib
                suffix = hashlib.sha256(repr((speaker, char, style)).encode('utf-8')).hexdigest()[:10]
                stem, extension = os.path.splitext(vol_path)
                vol_path = f'{stem}_{suffix}{extension}'
                if vol_path in used_paths:
                    raise RunStateError(f'ZIP volume filename collision: {vol_path}')
            used_paths.add(vol_path)
            planned.append((group_metadata[vol_idx * zip_max_files:(vol_idx + 1) * zip_max_files], vol_idx, vol_path))

    # Older runs had no manifest. Refuse an ambiguous stale volume rather than
    # claiming completion while training might pick it up as current output.
    if not os.path.exists(manifest_path):
        import glob
        for candidate in glob.glob(glob.escape(base) + '_*' + glob.escape(ext)):
            if candidate not in used_paths and re.search(r'_vol\d+' + re.escape(ext) + r'$', candidate):
                raise RunStateError(f'Unmanaged old ZIP volume: {candidate}')

    total_train = 0
    total_val = 0
    total_vols = 0

    for vol_metadata, vol_idx, vol_path in planned:

        # Partition this volume into train/val
        indices = list(range(len(vol_metadata)))
        random.seed(42 + vol_idx) # stable per volume
        random.shuffle(indices)

        v_count = int(len(vol_metadata) * val_split)
        v_indices = set(indices[:v_count])

        train_meta = []
        val_meta = []

        # Write to a temp path and atomically replace vol_path only once the
        # zip is fully written, so a crash mid-write never leaves a
        # truncated/corrupt file at the permanent path, and a re-run never
        # destroys the prior good volume before the new one is confirmed
        # good. See FIXED.md F-115.
        tmp_vol_path = vol_path + ".tmp"
        try:
            with zipfile.ZipFile(tmp_vol_path, "w", zipfile.ZIP_DEFLATED) as z:
                for i, entry in enumerate(vol_metadata):
                    wav_name = entry["audio_filepath"]
                    src_path = str(get_sample_path(temp_dir, wav_name))
                    if not os.path.isfile(src_path):
                        raise RunStateError(f'Missing sample WAV for ZIP: {src_path}')

                    is_val = (i in v_indices)
                    folder = "val" if is_val else "train"
                    zip_wav_path = f"{folder}/{wav_name}"

                    zip_entry = entry.copy()
                    zip_entry["audio_filepath"] = zip_wav_path

                    if is_val:
                        val_meta.append(zip_entry)
                        total_val += 1
                    else:
                        train_meta.append(zip_entry)
                        total_train += 1

                    z.write(src_path, zip_wav_path)

                # Write partitioned metadata.jsonl files
                if train_meta:
                    train_jsonl = "\n".join([json.dumps(e, ensure_ascii=False) for e in train_meta]) + "\n"
                    z.writestr("train/metadata.jsonl", train_jsonl)
                if val_meta:
                    val_jsonl = "\n".join([json.dumps(e, ensure_ascii=False) for e in val_meta]) + "\n"
                    z.writestr("val/metadata.jsonl", val_jsonl)

                # Volume manifest
                vol_manifest = sorted(train_meta + val_meta, key=lambda x: x["audio_filepath"])
                if vol_manifest:
                    manifest_jsonl = "\n".join([json.dumps(e, ensure_ascii=False) for e in vol_manifest]) + "\n"
                    z.writestr("metadata.jsonl", manifest_jsonl)
            os.replace(tmp_vol_path, vol_path)
        except Exception:
            if os.path.exists(tmp_vol_path):
                os.remove(tmp_vol_path)
            raise

        total_vols += 1
        logger.info(f"  ✓ Volume {total_vols} saved: {vol_path} ({len(vol_metadata)} segments)")

    new_names = [os.path.basename(path) for _, _, path in planned]
    for name in previous_volumes:
        stale_path = os.path.join(os.path.dirname(output_path), name)
        if name not in new_names and os.path.exists(stale_path):
            os.remove(stale_path)
    manifest_tmp = manifest_path + '.tmp'
    try:
        with open(manifest_tmp, 'w', encoding='utf-8') as stream:
            json.dump({'volumes': new_names}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(manifest_tmp, manifest_path)
    finally:
        if os.path.exists(manifest_tmp):
            os.remove(manifest_tmp)
    durations = [m["duration"] for m in metadata]
    logger.info("=" * 70)
    logger.info(f"Total segments across {total_vols} volume(s): {len(metadata)}")
    if durations:
        logger.info(f"  ├─ Total Train: {total_train} segments")
        logger.info(f"  ├─ Total Val  : {total_val} segments")
        logger.info(f"  ├─ Average duration: {np.mean(durations):.2f}s")
        logger.info(f"  └─ Total audio: {sum(durations)/60:.1f} minutes")
    logger.info("=" * 70)
    logger.info(f"✓ ALL VOLUMES COMPLETED.")


def build_phase_command(phase: str, argv: List[str], *, output: Optional[str] = None) -> List[str]:
    """Forward user arguments while replacing only the internal phase option."""
    forwarded = []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == '--phase':
            index += 2
            continue
        if arg.startswith('--phase='):
            index += 1
            continue
        forwarded.append(arg)
        index += 1
    if output is not None:
        forwarded.extend(['--output', output])
    return [sys.executable, __file__, '--phase', phase, *forwarded]


def main():
    global WAV2VEC2_MODEL_REVISION
    parser = argparse.ArgumentParser(
        description="Alexandria Master Preparer - ROCm Compatible"
    )

    parser.add_argument("--audio", required=True, help="Input audio file")
    parser.add_argument("--model",
                        help="Primary GGUF model (recommended: Qwen2.5-14B-Instruct-Q6_K.gguf)")
    parser.add_argument("--fallback-model",
                        help="Optional fallback GGUF model if --model fails to load "
                             "(e.g., Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf)")
    parser.add_argument("--skip-annotation", action="store_true")
    parser.add_argument("--chunk-size", type=float, default=10.0)
    parser.add_argument("--min-chunk-duration", type=float, default=2.0, help="Minimum duration for an audio chunk to be kept (default: 2.0s)")
    parser.add_argument("--min-confidence", type=float, default=0.85, help="Minimum average word confidence for a chunk to be kept (default: 0.85)")
    parser.add_argument("--min-snr", type=int, default=15, help="Minimum Signal-to-Noise Ratio (SNR) in dB for a chunk to be kept (default: 15)")
    parser.add_argument("--batch-size", type=int, default=1,
                        help="Number of chunks to annotate per LLM call (default: 1). "
                             "Higher values (3-5) reduce LLM overhead by ~30-50%% but "
                             "increase prompt length. Set to 1 for per-chunk annotation.")
    parser.add_argument("--lang", default="en")
    parser.add_argument("--asr-model-revision", default=WAV2VEC2_MODEL_REVISION,
                        help="Pinned Wav2Vec2 revision used for reproducible ASR")
    parser.add_argument("--limit", type=int, default=None, help="Limit number of chunks to process")
    parser.add_argument("--phase", choices=["asr", "enrich", "annotate"], help="Run only a specific phase (internal use for ROCm isolation)")
    parser.add_argument("--asr-output", help="Path to save/load ASR word segments (default: dataset_temp/asr_segments.json)")
    parser.add_argument("--alignment-report", help="Sampled source-alignment JSON report; requires --phase asr and --source")
    parser.add_argument("--summary-output", help="Structured annotation-attempt summary JSON destination")
    parser.add_argument("--scratch-audio", help="Path to 24k scratch WAV (default: dataset_temp/audio_24k.wav)")
    parser.add_argument("--output", default="alexandria_dataset.zip",
                        help="Output ZIP path. If left at the default — or set to "
                             "'dataset.zip' / 'output.zip' — a name is auto-derived "
                             "from --source (ePub title+author, or filename stem). "
                             "Any other value is used verbatim.")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from existing dataset_temp/ instead of starting over")
    parser.add_argument("--val-split", type=float, default=0.10,
                        help="Ratio of segments to carve out for validation (default: 0.10)")
    parser.add_argument("--zip-max-files", type=int, default=200,
                        help="Maximum files per ZIP volume (default: 200)")

    # ── Source-guided mode ────────────────────────────────────────────────────
    # When --source is provided, each ASR chunk is fuzzy-aligned against the
    # source text. Chunks whose alignment ratio meets --source-threshold get
    # their text replaced with the source spelling before the LLM annotates,
    # so character names and dialect spellings come out correct. Chunks below
    # threshold are dropped (likely audio-only material — credits, narrator
    # intros, content the source doesn't have) unless --keep-unaligned is set.
    parser.add_argument("--source", metavar="PATH",
                        help="Optional source EPUB or TXT. Enables source-guided "
                             "chunking: chunk text is replaced with the source's "
                             "spelling for high-confidence alignments, audio-only "
                             "passages are dropped.")
    parser.add_argument("--source-threshold", type=float, default=0.65, metavar="N",
                        help="Minimum alignment ratio to keep a chunk in source-guided "
                             "mode (default: 0.65). Chunks below this are dropped unless "
                             "--keep-unaligned is set.")
    parser.add_argument("--keep-unaligned", action="store_true",
                        help="When --source is set, keep chunks that fall below "
                             "--source-threshold and use their ASR text instead of "
                             "dropping them (default: strict-drop).")
    parser.add_argument("--source-start", type=int, metavar="N",
                        help="Start source alignment at word N (skip auto-anchor)")
    parser.add_argument("--source-start-text", metavar="TEXT",
                        help="Search source for TEXT and start alignment there")
    parser.add_argument("--no-auto-anchor", action="store_true",
                        help="When --source is set, disable auto-anchor (start at word 0)")
    
    # ── Metadata Tagging ──────────────────────────────────────────────────────
    parser.add_argument("--book-title", help="Book title for metadata tagging (overrides ePub title)")
    parser.add_argument("--character", help="Character name for metadata tagging")
    parser.add_argument("--narrator-style", help="Narrator style for metadata tagging")
    
    # ── Speaker Diarization ───────────────────────────────────────────────────
    parser.add_argument("--diarize", action="store_true", help="Enable speaker diarization using pyannote.audio")
    parser.add_argument("--hf-token", default=os.environ.get("HF_TOKEN"), help="Hugging Face token for pyannote access")
    parser.add_argument("--auto-detect-speakers", action="store_true", help="Auto-detect narrator count by analyzing audio and logging the number of unique speakers.")
    
    # ── LLM Enrichment ────────────────────────────────────────────────────────
    parser.add_argument("--enrich-with-llm", action="store_true", help="Enable LLM-based transcript enrichment.")
    parser.add_argument("--llm-model-path", help="Path to the GGUF LLM model file for transcript enrichment. Required if --enrich-with-llm is set.")
    parser.add_argument("--enrich-speaker-attribution", action="store_true", help="Instruct LLM to extract speaker attribution.")
    parser.add_argument("--enrich-narration-style", action="store_true", help="Instruct LLM to extract narration style.")
    parser.add_argument("--enrich-emotional-tone", action="store_true", help="Instruct LLM to extract emotional tone.")

    args = parser.parse_args()
    log_file = ensure_preparer_logging()
    logger.info("=== Alexandria Master Preparer Started ===")
    logger.info(f"Log file: {log_file}")
    logger.info(f"Python version: {sys.version}")
    WAV2VEC2_MODEL_REVISION = args.asr_model_revision

    # The API validates the same values before staging large uploads.
    sys.path.append(os.path.join(script_dir, "app"))
    from preparer_numeric_settings import validate_preparer_numeric_settings
    try:
        validate_preparer_numeric_settings(vars(args))
    except ValueError as exc:
        parser.error(str(exc))
    if args.limit is not None and args.limit <= 0:
        parser.error('--limit must be positive')

    if args.skip_annotation:
        parser.error("--skip-annotation is not implemented; omit it and provide --model")
    if args.alignment_report and (args.phase != 'asr' or not args.source):
        parser.error('--alignment-report requires --phase asr and --source')
    if (args.auto_detect_speakers or args.diarize) and not args.hf_token:
        parser.error("Speaker diarization requires --hf-token or the HF_TOKEN environment variable")

    needs_annotation_model = args.phase != "asr"
    if needs_annotation_model and not args.model:
        parser.error("--model is required")
    if needs_annotation_model and not LLAMA_CPP_AVAILABLE:
        parser.error("llama-cpp-python is required for annotation")

    if args.enrich_with_llm and not args.llm_model_path:
        parser.error("--llm-model-path is required when --enrich-with-llm is set.")

    # Auto-derive --output filename from --source metadata when the caller left
    # the default (or another generic placeholder). Pinned names pass through.
    needs_metadata = (os.path.basename(args.output) in _NAME_GENERIC_OUTPUTS
                      or (args.phase == 'annotate' and not args.book_title))
    epub_title, epub_author = (extract_metadata_for_naming(args.source)
                              if args.source and needs_metadata else (None, None))
    book_title = args.book_title or epub_title
    derived_output = maybe_autoname_output(args.output, args.source, epub_title, epub_author)
    if derived_output != args.output:
        logger.info(f"Auto-derived output filename: {derived_output} "
                    f"(was: {args.output})")
        args.output = derived_output

    try:
        run_lock_fd = acquire_run_lock('dataset_temp')
        run_identity = get_run_identity(args)
        start_fresh = (args.phase is None or args.phase == 'asr') and not args.resume
        ensure_run_manifest('dataset_temp', run_identity, fresh=start_fresh)
    except (RunStateError, OSError) as error:
        parser.error(str(error))

    def run_phase(command):
        return run_phase_with_lock(command, run_lock_fd)

    # ── Phase Orchestration ──────────────────────────────────────────────────
    # ROCm HIP contexts from PyTorch (Wav2Vec2) and llama-cpp often conflict
    # if initialized in the same process. We split them into separate phases.
    if args.phase is None:
        logger.info("=" * 70)
        logger.info("Alexandria Master Preparer - Phase Orchestrator (ROCm Isolation)")
        logger.info("=" * 70)
        
        # 1. Run ASR Phase (if not already completed and resuming)
        asr_output_path = args.asr_output or os.path.join("dataset_temp", "asr_segments.json")
        should_run_asr = True
        if args.resume and is_verified_artifact(
                'dataset_temp', run_identity, 'asr', asr_output_path):
            logger.info(f"▶ ASR output found at {asr_output_path}, skipping ASR phase due to --resume")
            should_run_asr = False

        if should_run_asr:
            asr_cmd = build_phase_command('asr', sys.argv[1:], output=args.output)

            logger.info("▶ Launching ASR Phase...")
            res = run_phase(asr_cmd)
            if res.returncode != 0:
                logger.error(f"ASR Phase failed with exit code {res.returncode}")
                sys.exit(res.returncode)

        # 2. Run LLM Enrichment Phase (if requested)
        enriched_output_path = os.path.join("dataset_temp", "enriched_segments.json")
        should_run_enrich = args.enrich_with_llm
        if should_run_enrich and args.resume and is_verified_artifact(
                'dataset_temp', run_identity, 'enriched', enriched_output_path):
            logger.info(f"▶ Enriched output found at {enriched_output_path}, skipping enrichment phase due to --resume")
            should_run_enrich = False

        if should_run_enrich:
            if not args.llm_model_path:
                logger.error("▶ LLM enrichment requested but --llm-model-path not provided")
                sys.exit(1)

            # Run enrichment as a subprocess to reuse the same chunking logic
            enrich_cmd = build_phase_command('enrich', sys.argv[1:], output=args.output)

            logger.info("▶ Launching LLM Enrichment Phase...")
            res = run_phase(enrich_cmd)
            if res.returncode != 0:
                logger.error(f"LLM Enrichment Phase failed with exit code {res.returncode}")
                sys.exit(res.returncode)

        # 3. Run Annotation Phase
        ann_cmd = build_phase_command('annotate', sys.argv[1:], output=args.output)

        logger.info("▶ Launching Annotation Phase...")
        res = run_phase(ann_cmd)
        if res.returncode == 0:
            # Clean up scratch audio and temp dir on success
            audio_24k_scratch = args.scratch_audio or os.path.join("dataset_temp", "audio_24k_scratch.wav")
            if args.scratch_audio is None and os.path.exists(audio_24k_scratch):
                try:
                    os.remove(audio_24k_scratch)
                    logger.debug(f"Removed scratch audio: {audio_24k_scratch}")
                except Exception:
                    pass
            try:
                cleanup_run_artifacts('dataset_temp', run_identity)
                logger.debug("Cleaned up this run's dataset_temp artifacts")
            except Exception as error:
                logger.error(f'Could not safely clean run artifacts: {error}')
                sys.exit(1)
            logger.info(f"Log file saved to: {log_file}")
        sys.exit(res.returncode)

    # ── Individual Phase Execution ───────────────────────────────────────────
    
    # Standard paths for intermediate files
    temp_dir = "dataset_temp"
    os.makedirs(temp_dir, exist_ok=True)
    asr_output_path = args.asr_output or os.path.join(temp_dir, "asr_segments.json")
    audio_24k_path = args.scratch_audio or os.path.join(temp_dir, "audio_24k_scratch.wav")

    audio_24k_scratch = audio_24k_path  # for the finally block cleanup

    decode_temp_dir = None
    diarization_temp_dir = None
    try:
        if args.phase == "asr":
            t = _lazy_import_torch()
            device = resolve_cuda_device(t)

            logger.info("-" * 70)
            logger.info(f"PHASE: ASR (Device: {device})")
            logger.info("-" * 70)
            log_torch_info()

            progress.start("Validate inputs")
            validate_inputs(args)
            progress.complete()

            progress.start("Load audio")
            validate_scratch_path(temp_dir, run_identity, audio_24k_path)
            logger.debug(f"Loading audio from {args.audio} (single read)...")
            load_t0 = time.monotonic()
            is_oversized, input_duration, _ = _wav_overflow_info(args.audio)

            if is_oversized or input_duration > 600:
                logger.info("  Using file-backed ffmpeg loader for long audio")
                decode_temp_dir = tempfile.TemporaryDirectory(
                    prefix="alexandria_asr_", dir=temp_dir)
                audio_16k = decode_audio_to_asr_streams(
                    args.audio, audio_24k_path,
                    os.path.join(decode_temp_dir.name, "audio_16k.f32"))
                sf_info_24k = sf.info(audio_24k_path)
                duration_secs = sf_info_24k.duration
                logger.info(f"  Audio: {duration_secs:.1f}s @ {sf_info_24k.frames} samples (loaded in {time.monotonic()-load_t0:.1f}s)")
            else:
                l = _lazy_import_librosa()
                audio_native, native_sr = l.load(args.audio, sr=None, mono=True)
                logger.debug(f"  Native sample rate: {native_sr}Hz, duration: {len(audio_native)/native_sr:.1f}s")

                if native_sr == 16000:
                    audio_16k = audio_native
                else:
                    logger.debug(f"  Resampling to 16kHz (in memory)...")
                    audio_16k = l.resample(audio_native, orig_sr=native_sr, target_sr=16000)

                if native_sr == 24000:
                    audio_24k = audio_native
                else:
                    logger.debug(f"  Resampling to 24kHz (in memory)...")
                    audio_24k = l.resample(audio_native, orig_sr=native_sr, target_sr=24000)

                if native_sr not in (16000, 24000):
                    del audio_native
                    gc.collect()

                duration_secs = len(audio_24k) / 24000
                logger.info(f"  Audio: {duration_secs:.1f}s @ {len(audio_24k)} samples (loaded in {time.monotonic()-load_t0:.1f}s)")

                logger.debug(f"  Spilling 24kHz audio to scratch file: {audio_24k_scratch}")
                sf.write(audio_24k_scratch, audio_24k, 24000, subtype="PCM_16")
                del audio_24k
                gc.collect()

                scratch_size_mb = os.path.getsize(audio_24k_scratch) / (1024 * 1024)
                logger.info(f"  ├─ Scratch audio: {audio_24k_scratch} ({scratch_size_mb:.1f} MB, PCM_16) - freed from RAM")

            mark_artifact_complete(temp_dir, run_identity, 'scratch', audio_24k_path)
            progress.complete()

            original_samples = len(audio_16k)
            audio_16k = audio_16k[:get_asr_sample_count(original_samples, 16000, args.limit)]
            diarization_audio_path = audio_24k_path
            diarization_duration = len(audio_16k) / 16000.0
            if len(audio_16k) < original_samples:
                logger.info(f"ASR limit bounds every backend to {diarization_duration:.1f}s.")
                if args.diarize or args.auto_detect_speakers:
                    diarization_temp_dir = tempfile.TemporaryDirectory(
                        prefix="alexandria_diarization_prefix_", dir=temp_dir)
                    diarization_audio_path = os.path.join(diarization_temp_dir.name, "prefix.wav")
                    with sf.SoundFile(audio_24k_path) as source:
                        remaining = min(source.frames, round(diarization_duration * source.samplerate))
                        with sf.SoundFile(diarization_audio_path, mode="w", samplerate=source.samplerate,
                                          channels=source.channels, subtype="PCM_16") as destination:
                            while remaining:
                                block = source.read(min(65536, remaining), always_2d=True)
                                if not len(block):
                                    raise ValueError("Scratch audio ended before the bounded prefix")
                                destination.write(block)
                                remaining -= len(block)

            # Diarize speakers if requested. --auto-detect-speakers first runs a
            # cheap sampled pre-check to decide whether the full pass is needed;
            # an explicit --diarize wins and skips the pre-check.
            run_diarization = args.diarize
            detection_pipeline = None
            if args.auto_detect_speakers and not args.diarize:
                progress.start("Detect speakers")
                device_str = resolve_cuda_device(_lazy_import_torch())
                verdict, evidence, detection_pipeline = detect_speaker_count(
                    diarization_audio_path, args.hf_token, device=device_str,
                    duration_secs=diarization_duration)
                if verdict is False:
                    logger.info(f"  ✓ Single narrator detected across {len(evidence)} sampled "
                                "window(s); skipping full diarization.")
                elif verdict is True:
                    logger.info(f"  ✓ Multiple speakers detected in sampled window(s); "
                                "running full diarization.")
                else:
                    logger.info("  Pre-check inconclusive; falling back to full diarization.")
                run_diarization = verdict is not False
                progress.complete()

            if run_diarization:
                progress.start("Diarize speakers")
                device_str = resolve_cuda_device(_lazy_import_torch())
                speaker_segments = diarize_audio(diarization_audio_path, args.hf_token, device=device_str,
                                                 pipeline=detection_pipeline)
                diarization_path = os.path.join(temp_dir, "diarization.json")
                write_json_atomic(speaker_segments, diarization_path)
                mark_artifact_complete(temp_dir, run_identity, 'diarization',
                                       diarization_path)
                if speaker_segments:
                    logger.info(f"  ✓ Found {len(speaker_segments)} speaker segments. Saved to {diarization_path}")
                else:
                    logger.warning("  ⚠ Diarization produced no segments. Continuing without speaker data.")
                progress.complete()

            if detection_pipeline is not None:
                detection_pipeline = None
                clear_vram()

            progress.start("Transcribe audio")
            word_segments, detected_lang = choose_and_transcribe(audio_16k, device, args.lang, limit=args.limit)
            logger.info(f"  Detected language: {detected_lang}")

            # Save ASR results for next phase
            logger.info(f"▶ Saving ASR segments to {asr_output_path}...")
            write_json_atomic({
                "detected_lang": detected_lang,
                "word_segments": word_segments,
                "audio_duration": duration_secs
            }, asr_output_path)
            mark_artifact_complete(temp_dir, run_identity, 'asr', asr_output_path)

            del audio_16k
            clear_vram()
            progress.complete()
            if args.alignment_report:
                save_source_alignment_prescan(word_segments, args, run_identity)
            logger.info("✓ ASR Phase completed successfully.")
            return 0

        elif args.phase == "enrich":
            logger.info("-" * 70)
            logger.info("PHASE: LLM Enrichment")
            logger.info("-" * 70)

            if not is_verified_artifact(temp_dir, run_identity, 'asr', asr_output_path):
                logger.error(f"ASR results are not complete for this run at {asr_output_path}.")
                sys.exit(1)

            if not args.llm_model_path:
                logger.error("--llm-model-path is required for enrichment phase")
                sys.exit(1)

            enriched_output_path = os.path.join("dataset_temp", "enriched_segments.json")
            
            logger.info(f"▶ Loading ASR results from {asr_output_path}...")
            with open(asr_output_path, "r", encoding="utf-8") as f:
                asr_data = json.load(f)

            # Extract word_segments and group into chunks for enrichment
            word_segments = asr_data.get("word_segments", [])
            
            # Group words into chunks (e.g., 10 seconds per chunk)
            chunk_duration = args.chunk_size
            chunks = []
            current_chunk_words = []
            chunk_start = None
            chunk_end = None
            
            for word in word_segments:
                if chunk_start is None:
                    chunk_start = word.get("start", 0)
                    chunk_end = chunk_start + chunk_duration
                
                if word.get("start", 0) <= chunk_end:
                    current_chunk_words.append(word)
                else:
                    # Save current chunk and start new one
                    if current_chunk_words:
                        chunk_text = " ".join(w.get("word", "") for w in current_chunk_words)
                        chunks.append({
                            "text": chunk_text,
                            "start": current_chunk_words[0].get("start", 0),
                            "end": current_chunk_words[-1].get("end", 0),
                            "speaker": "UNKNOWN",
                            "words": current_chunk_words
                        })
                    current_chunk_words = [word]
                    chunk_start = word.get("start", 0)
                    chunk_end = chunk_start + chunk_duration
            
            # Don't forget the last chunk
            if current_chunk_words:
                chunk_text = " ".join(w.get("word", "") for w in current_chunk_words)
                chunks.append({
                    "text": chunk_text,
                    "start": current_chunk_words[0].get("start", 0),
                    "end": current_chunk_words[-1].get("end", 0),
                    "speaker": "UNKNOWN",
                    "words": current_chunk_words
                })
            
            logger.info(f"  Created {len(chunks)} chunks for LLM enrichment")
            
            # Save chunks for llm_enricher.py
            asr_chunks_path = os.path.join("dataset_temp", "asr_chunks_for_enrich.json")
            with open(asr_chunks_path, "w", encoding="utf-8") as f:
                json.dump(chunks, f, indent=2)

            # Build command for llm_enricher.py
            enrich_cmd = [sys.executable, os.path.join(os.path.dirname(__file__), "llm_enricher.py"),
                         "--model-path", args.llm_model_path,
                         "--input-file", asr_chunks_path,
                         "--output-file", enriched_output_path]
            if args.enrich_speaker_attribution:
                enrich_cmd.append("--speaker-attribution")
            if args.enrich_narration_style:
                enrich_cmd.append("--narration-style")
            if args.enrich_emotional_tone:
                enrich_cmd.append("--emotional-tone")

            logger.info(f"▶ Running LLM enrichment: {' '.join(enrich_cmd)}")
            res = subprocess.run(enrich_cmd)
            if res.returncode != 0:
                logger.error(f"LLM enrichment failed with exit code {res.returncode}")
                sys.exit(res.returncode)

            if not os.path.exists(enriched_output_path):
                logger.error(f"Enrichment output file not found at {enriched_output_path}")
                sys.exit(1)

            # Load enriched chunks and flatten back to word_segments format
            # so the annotation phase can process them correctly
            logger.info(f"▶ Loading enriched chunks for word-level reconstruction...")
            with open(enriched_output_path, "r", encoding="utf-8") as f:
                enriched_chunks = json.load(f)

            # Flatten enriched chunks back to word_segments, adding enrichment metadata
            enriched_word_segments = []
            for chunk in enriched_chunks:
                chunk_words = chunk.get("words", [])
                for word in chunk_words:
                    enriched_word = dict(word)  # copy original word segment
                    # Add enrichment metadata from the chunk
                    enriched_word["speaker_attribution"] = chunk.get("speaker_attribution", "N/A")
                    enriched_word["narration_style"] = chunk.get("narration_style", "N/A")
                    enriched_word["emotional_tone"] = chunk.get("emotional_tone", "N/A")
                    enriched_word_segments.append(enriched_word)

            # Save in the same format as ASR output
            enriched_asr_data = {
                "word_segments": enriched_word_segments,
                "detected_lang": asr_data.get("detected_lang", "en"),
            }
            write_json_atomic(enriched_asr_data, enriched_output_path)
            mark_artifact_complete(temp_dir, run_identity, 'enriched',
                                   enriched_output_path)

            logger.info(f"  Reconstructed {len(enriched_word_segments)} word segments from {len(enriched_chunks)} enriched chunks")
            logger.info("✓ LLM Enrichment Phase completed successfully.")
            return 0

        elif args.phase == "annotate":
            logger.info("-" * 70)
            logger.info(f"PHASE: Annotation")
            logger.info("-" * 70)

            if not is_verified_artifact(temp_dir, run_identity, 'asr', asr_output_path):
                logger.error(f"ASR results are not complete for this run at {asr_output_path}.")
                sys.exit(1)

            # Reuse only scratch owned by this exact run; recreate if absent.
            validate_scratch_path(temp_dir, run_identity, audio_24k_path)
            if not os.path.exists(audio_24k_path):
                logger.info(f"▶ Scratch audio not found at {audio_24k_path}, recreating from source...")
                is_oversized, input_duration, _ = _wav_overflow_info(args.audio)
                if is_oversized or input_duration > 600:
                    logger.info("  Using file-backed ffmpeg loader for long audio")
                    _ffmpeg_decode_to_wav(args.audio, audio_24k_path, 24000, mono=True)
                else:
                    l = _lazy_import_librosa()
                    audio_native, native_sr = l.load(args.audio, sr=None, mono=True)
                    if native_sr == 24000:
                        audio_24k = audio_native
                    else:
                        logger.debug(f"  Resampling to 24kHz (in memory)...")
                        audio_24k = l.resample(audio_native, orig_sr=native_sr, target_sr=24000)
                    sf.write(audio_24k_path, audio_24k, 24000, subtype="PCM_16")
                    del audio_native, audio_24k
                logger.info(f"  Scratch audio recreated: {audio_24k_path}")
                mark_artifact_complete(temp_dir, run_identity, 'scratch', audio_24k_path)

            # Only a completed enrichment phase for this run may supply words.
            enriched_output_path = os.path.join("dataset_temp", "enriched_segments.json")
            use_enriched = args.enrich_with_llm and is_verified_artifact(
                temp_dir, run_identity, 'enriched', enriched_output_path)
            if args.enrich_with_llm and not use_enriched:
                raise RunStateError('Requested enrichment artifact is incomplete or stale')

            if use_enriched:
                logger.info(f"▶ Loading enriched results from {enriched_output_path}...")
                with open(enriched_output_path, "r", encoding="utf-8") as f:
                    enriched_data = json.load(f)
                word_segments = enriched_data["word_segments"]
                detected_lang = enriched_data.get("detected_lang", "en")
                logger.info("  Using LLM-enriched transcript data")
            else:
                logger.info(f"▶ Loading ASR results from {asr_output_path}...")
                with open(asr_output_path, "r", encoding="utf-8") as f:
                    asr_data = json.load(f)
                    word_segments = asr_data["word_segments"]
                    detected_lang = asr_data["detected_lang"]

            # Load diarization results if they exist
            diarization_path = os.path.join(temp_dir, "diarization.json")
            speaker_segments = []
            diarization_verified = ((args.diarize or args.auto_detect_speakers)
                                     and is_verified_artifact(
                                         temp_dir, run_identity, 'diarization',
                                         diarization_path))
            if args.diarize and not diarization_verified:
                raise RunStateError('Requested diarization artifact is incomplete or stale')
            if diarization_verified:
                logger.info(f"▶ Loading diarization results from {diarization_path}...")
                with open(diarization_path, "r") as f:
                    speaker_segments = json.load(f)
                if speaker_segments and not INTERVALTREE_AVAILABLE:
                    logger.error("✗ 'intervaltree' package required for speaker diarization assignment. Install with: pip install intervaltree")
                    sys.exit(1)
                if speaker_segments:
                    word_segments, unique_speakers = _assign_speakers_to_words(word_segments, speaker_segments)
                else:
                    unique_speakers = {"UNKNOWN"}
                    for word in word_segments:
                        word["speaker"] = "UNKNOWN"
            else:
                unique_speakers = {"UNKNOWN"}
                for word in word_segments:
                    word["speaker"] = "UNKNOWN"

            # ── Optional: source-guided chunking ──────────────────────────────────
            source_state = None
            if args.source:
                source_state, quality = get_source_alignment_prescan(
                    word_segments, args.chunk_size, args.source,
                    source_start=args.source_start, source_start_text=args.source_start_text,
                    no_auto_anchor=args.no_auto_anchor, source_threshold=args.source_threshold)
                avg, n_sampled, low_ct, _ = quality
                if n_sampled >= 10:
                    pct_low = low_ct / n_sampled
                    if (avg < 0.50 or pct_low > 0.40) and not args.keep_unaligned:
                        sys.exit(
                            f"\n⚠ Source/audio divergence too high to proceed:\n"
                            f"  Sampled {n_sampled} chunks — avg alignment {avg:.0%}, "
                            f"{low_ct} ({pct_low:.0%}) below 60%.\n"
                            f"  Usually means a wrong edition or different translation.\n"
                            f"  Re-run without --source, or pass --keep-unaligned to "
                            f"accept the ASR text for low-confidence chunks."
                        )

            progress.start("Annotate chunks")
            metadata = annotate_chunks(
                word_segments,
                args.model,
                args.chunk_size,
                audio_24k_scratch,
                resume=args.resume,
                audio_source_path=args.audio,
                fallback_model_path=args.fallback_model,
                source_state=source_state,
                source_threshold=args.source_threshold,
                keep_unaligned=args.keep_unaligned,
                min_chunk_duration=args.min_chunk_duration,
                min_confidence=args.min_confidence,
                min_snr=args.min_snr,
                book_title=book_title,
                character=args.character,
                narrator_style=args.narrator_style,
                batch_size=args.batch_size,
                run_identity=run_identity,
                summary_output=args.summary_output,
            )
            logger.info(f"  Chunks annotated: {len(metadata)}")
            progress.complete()

            progress.start("Create output dataset")
            _create_zip_dataset(metadata, args.output, val_split=args.val_split, zip_max_files=args.zip_max_files, unique_speakers=unique_speakers)
            progress.complete()

            logger.info("✓ Annotation Phase completed successfully.")
            return 0

    except KeyboardInterrupt:
        logger.warning("⚠ Process interrupted by user")
        logger.info(f"Partial results preserved in dataset_temp/ - rerun with --resume to continue")
        return 130
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        logger.debug(traceback.format_exc())
        logger.info(f"Partial results preserved in dataset_temp/ - rerun with --resume to continue")
        return 1
    finally:
        if diarization_temp_dir is not None:
            diarization_temp_dir.cleanup()
        if decode_temp_dir is not None:
            decode_temp_dir.cleanup()
        # Only clean up the scratch audio file after the final phase (annotation)
        # or if we are not using the phase orchestration.
        # Preserve it during the 'asr' and 'enrich' phases so 'annotate' can use
        # it - 'enrich' never touches audio_24k_scratch itself, but deleting it
        # here would still force 'annotate' to re-decode/resample from the
        # original source (it has a recreate-if-missing fallback, so this was
        # never a correctness bug, just wasted work whenever --enrich-with-llm
        # is combined with the phase orchestrator).
        if (args.scratch_audio is None and args.phase not in ("asr", "enrich")
                and os.path.exists(audio_24k_scratch)):
            try:
                os.remove(audio_24k_scratch)
                logger.debug(f"Removed scratch audio: {audio_24k_scratch}")
            except Exception as e:
                logger.warning(f"Failed to remove scratch audio: {e}")

        logger.info(f"Log file saved to: {log_file}")

if __name__ == "__main__":
    sys.exit(main())
