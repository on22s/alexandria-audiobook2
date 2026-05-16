#!/usr/bin/env python3
"""
Alexandria Master Preparer - ROCm Compatible Version
Handles CUDA/ROCm version mismatches gracefully
"""

import os
import sys
import tempfile

# Add insanely-fast-whisper-rocm to path if available
script_dir = os.path.dirname(os.path.abspath(__file__))
ifw_path = os.path.join(script_dir, "insanely-fast-whisper-rocm")
if os.path.exists(ifw_path):
    sys.path.insert(0, ifw_path)

# ROCm environment fixes
os.environ["PYTORCH_HIP_ALLOC_CONF"] = "expandable_segments:True"
os.environ["PYTORCH_ALLOC_CONF"] = "expandable_segments:True"
os.environ["HSA_ENABLE_SDMA"] = "0"
os.environ["GPU_MAX_HW_QUEUES"] = "2"

import argparse
import torch
import gc
import time
import librosa
import logging
import json
import subprocess
import zipfile
import shutil
import soundfile as sf
import numpy as np
import traceback
from collections import deque
from typing import List, Dict, Optional
from datetime import datetime

# Setup logging
log_dir = "logs"
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, f"alexandria_preparer_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")

logger = logging.getLogger("alexandria")
logger.setLevel(logging.DEBUG)

# File handler (detailed)
fh = logging.FileHandler(log_file)
fh.setLevel(logging.DEBUG)
file_format = logging.Formatter('[%(asctime)s] [%(levelname)s] [%(name)s:%(lineno)d] %(message)s')
fh.setFormatter(file_format)

# Console handler (info and above)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
console_format = logging.Formatter('[%(levelname)s] %(message)s')
ch.setFormatter(console_format)

logger.addHandler(fh)
logger.addHandler(ch)

# Progress tracker
class ProgressTracker:
    def __init__(self):
        self.steps = []
        self.current_step = 0

    def add_step(self, name):
        self.steps.append(name)

    def start(self, step_name):
        self.current_step = next((i for i, s in enumerate(self.steps) if s == step_name), 0)
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

logger.info(f"=== Alexandria Master Preparer Started ===")
logger.info(f"Log file: {log_file}")
logger.info(f"Python version: {sys.version}")
logger.info(f"PyTorch version: {torch.__version__}")

# Check available ASR options
INSANELY_FAST_WHISPER_AVAILABLE = False
WHISPERX_AVAILABLE = False
TRANSFORMERS_WHISPER_AVAILABLE = False

INSANELY_FAST_WHISPER_AVAILABLE = os.path.exists(
    os.path.join(script_dir, "insanely-fast-whisper-rocm")
)
if INSANELY_FAST_WHISPER_AVAILABLE:
    logger.info("✓ Insanely Fast Whisper (ROCm) available")
else:
    logger.debug("Insanely Fast Whisper not found in project directory")

try:
    from whisperx import asr as whisperx_asr
    from whisperx import alignment as whisperx_alignment
    WHISPERX_AVAILABLE = True
    logger.info("✓ WhisperX-ROCm available")
except ImportError as e:
    logger.debug(f"WhisperX not available: {e}")

try:
    from transformers import pipeline
    TRANSFORMERS_WHISPER_AVAILABLE = True
    logger.info("✓ Transformers available")
except ImportError as e:
    logger.debug(f"Transformers not available: {e}")

try:
    from llama_cpp import Llama
    logger.info("✓ llama-cpp-python available")
except ImportError:
    logger.critical("llama-cpp-python required. Install with: pip install llama-cpp-python")
    sys.exit(1)

logging.getLogger("transformers").setLevel(logging.ERROR)
logging.getLogger("whisperx").setLevel(logging.ERROR)

def clear_vram():
    """Clear GPU memory and sync."""
    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()
        torch.cuda.synchronize()
        # Note: GPU cache clearing is logged silently to reduce spam

def get_gpu_stats():
    """Get current GPU memory and utilization stats."""
    if not torch.cuda.is_available():
        return None

    stats = {}
    try:
        # Memory stats (works for both NVIDIA and AMD ROCm)
        allocated = torch.cuda.memory_allocated() / 1e9  # GB
        reserved = torch.cuda.memory_reserved() / 1e9    # GB
        total = torch.cuda.get_device_properties(0).total_memory / 1e9  # GB

        stats['allocated_gb'] = allocated
        stats['reserved_gb'] = reserved
        stats['total_gb'] = total
        stats['allocated_percent'] = (allocated / total * 100) if total > 0 else 0

        # Try to get utilization via rocm-smi for AMD GPUs
        try:
            result = subprocess.run(
                ['/opt/rocm/bin/rocm-smi', '--showuse', '--json'],
                capture_output=True,
                text=True,
                timeout=5
            )
            if result.returncode == 0:
                # Filter out warning lines and parse JSON
                json_lines = [line for line in result.stdout.split('\n') if line.strip().startswith('{')]
                if json_lines:
                    data = json.loads(json_lines[0])
                    # rocm-smi format: {"card0": {"GPU use (%)": "value"}}
                    for card_key, card_data in data.items():
                        gpu_use_str = card_data.get('GPU use (%)', 'N/A')
                        if gpu_use_str != 'N/A':
                            stats['utilization_percent'] = float(gpu_use_str)
                        break  # Just get first GPU
            else:
                logger.debug(f"rocm-smi returned error: {result.returncode}, stderr: {result.stderr}")
                stats['utilization_percent'] = None
        except FileNotFoundError as e:
            logger.debug(f"rocm-smi not found: {e}")
            stats['utilization_percent'] = None
        except (subprocess.TimeoutExpired, json.JSONDecodeError, ValueError) as e:
            logger.debug(f"rocm-smi parse error: {e}")
            stats['utilization_percent'] = None
        except Exception as e:
            logger.debug(f"rocm-smi unexpected error: {e}")
            stats['utilization_percent'] = None

    except Exception as e:
        logger.debug(f"Could not get GPU stats: {e}")
        return None

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

def validate_inputs(args):
    """Validate input files."""
    logger.info("Validating input files...")

    if not os.path.exists(args.audio):
        logger.error(f"Audio file not found: {args.audio}")
        sys.exit(1)
    logger.debug(f"Audio file exists: {args.audio}")

    if not args.skip_annotation and not os.path.exists(args.model):
        logger.error(f"Model file not found: {args.model}")
        sys.exit(1)
    if not args.skip_annotation:
        logger.debug(f"Model file exists: {args.model}")

    try:
        info = sf.info(args.audio)
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
                            "end": word_info["end"]
                        })

        logger.info(f"✓ WhisperX complete: {len(word_segments)} words extracted")
        return word_segments, detected_lang

    except Exception as e:
        logger.error(f"WhisperX transcription failed: {e}")
        logger.debug(traceback.format_exc())
        raise

def transcribe_with_wav2vec2(audio_16k: np.ndarray, language: str = "en") -> tuple:
    """Use Wav2Vec2 for continuous context-aware transcription with CTC word alignment."""
    if not TRANSFORMERS_WHISPER_AVAILABLE:
        raise ImportError("Transformers not available")

    logger.info("▶ Initializing Wav2Vec2 ASR (CTC-aligned word timestamps)...")
    logger.info(f"  ├─ Model: facebook/wav2vec2-large-960h")
    logger.info(f"  ├─ Device: GPU (CUDA/ROCm)")
    logger.info(f"  └─ Language: {language}")

    try:
        from transformers import Wav2Vec2Processor, Wav2Vec2ForCTC
        import torch as torch_module

        logger.debug("Loading Wav2Vec2 processor and model...")
        device_str = "cuda" if torch_module.cuda.is_available() else "cpu"

        processor = Wav2Vec2Processor.from_pretrained("facebook/wav2vec2-large-960h")
        model = Wav2Vec2ForCTC.from_pretrained("facebook/wav2vec2-large-960h")
        model = model.to(device_str)
        model.eval()

        logger.info("✓ Wav2Vec2 model loaded to GPU")
        log_gpu_stats("after model load")

        # Frame rate: for wav2vec2-large-960h, CNN downsamples 16kHz audio by 320 → 50 frames/sec
        inputs_to_logits_ratio = getattr(model.config, "inputs_to_logits_ratio", 320)
        time_per_frame = inputs_to_logits_ratio / 16000.0  # seconds per logit frame (~0.02s)

        chunk_length_secs = 30
        chunk_length = chunk_length_secs * 16000
        overlap_secs = 3
        overlap = overlap_secs * 16000
        stride = chunk_length - overlap
        half_overlap_secs = overlap_secs / 2.0

        audio_duration = len(audio_16k) / 16000.0

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
                    word_segments.append({
                        "word": wo["word"].strip(),
                        "start": word_start,
                        "end": word_end
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

def transcribe_with_whisper_v3(audio_16k: np.ndarray, device: str) -> tuple:
    """Use OpenAI Whisper-large-v3 (best model, CPU stable)."""
    if not TRANSFORMERS_WHISPER_AVAILABLE:
        raise ImportError("Transformers not available")

    logger.info("Starting Whisper-large-v3 transcription...")

    try:
        # Use CPU to avoid CUDA/ROCm issues, v3 is fast enough
        pipe_device = -1
        logger.debug(f"Loading Whisper-large-v3 pipeline (device=CPU)...")

        pipe = pipeline(
            "automatic-speech-recognition",
            model="openai/whisper-large-v3",
            device=pipe_device,
            return_timestamps="word"
        )
        logger.info("✓ Whisper-large-v3 pipeline loaded")

        logger.info("Transcribing audio with Whisper-v3...")
        result = pipe(audio_16k, return_timestamps="word")

        word_segments = []
        if "chunks" in result:
            for chunk in result["chunks"]:
                if "timestamp" in chunk and chunk["timestamp"]:
                    start, end = chunk["timestamp"]
                    if start is not None and end is not None:
                        word_segments.append({
                            "word": chunk["text"].strip(),
                            "start": start,
                            "end": end
                        })

        del pipe
        clear_vram()

        logger.info(f"✓ Whisper-v3 complete: {len(word_segments)} words transcribed")
        return word_segments, "en"

    except Exception as e:
        logger.error(f"Whisper-v3 failed: {e}")
        logger.debug(traceback.format_exc())
        raise

def transcribe_with_insanely_fast_whisper(audio_16k: np.ndarray, language: str = "en") -> tuple:
    """Use Insanely Fast Whisper ROCm (optimized for AMD GPUs)."""
    if not INSANELY_FAST_WHISPER_AVAILABLE:
        raise ImportError("Insanely Fast Whisper not available")

    logger.info("▶ Initializing Insanely Fast Whisper (ROCm optimized)...")
    audio_duration = len(audio_16k) / 16000
    logger.info(f"  ├─ Audio: {audio_duration:.1f}s @ {audio_16k.shape[0]} samples")
    logger.info(f"  └─ Language: {language}")

    temp_audio_file = None
    temp_output_file = None
    temp_dir = None

    try:
        # Create temporary directory
        temp_dir = tempfile.mkdtemp(prefix="alexandria_audio_")
        logger.debug(f"Created temp directory: {temp_dir}")

        temp_audio_file = os.path.join(temp_dir, "temp_audio.wav")
        temp_output_file = os.path.join(temp_dir, "output.json")

        # Write audio to temporary file
        logger.debug(f"Writing audio to temporary file: {temp_audio_file}")
        sf.write(temp_audio_file, audio_16k, samplerate=16000)

        # Verify file was written
        if os.path.exists(temp_audio_file):
            file_size = os.path.getsize(temp_audio_file)
            logger.debug(f"✓ Audio file written successfully ({file_size} bytes)")
        else:
            raise RuntimeError(f"Failed to write audio file: {temp_audio_file}")

        logger.info("▶ Starting transcription...")
        logger.debug(f"  Expected duration: {audio_duration:.1f}s (this may take several minutes)")

        # Prepare CLI command
        ifw_module_path = os.path.join(script_dir, "insanely-fast-whisper-rocm")
        logger.debug(f"Insanely Fast Whisper module path: {ifw_module_path}")
        logger.debug(f"Module exists: {os.path.exists(ifw_module_path)}")

        # Use current Python executable (which has the correct ROCm environment)
        python_exe = sys.executable
        logger.debug(f"Python executable: {python_exe}")
        logger.debug(f"Working directory: {os.getcwd()}")
        logger.debug(f"sys.executable Python version: {sys.version}")

        # Use pre-downloaded model or HuggingFace model ID
        model_name = "openai/whisper-base"
        local_model_path = os.path.join(script_dir, "models", "whisper-base")
        if os.path.exists(local_model_path):
            model_name = local_model_path
            logger.debug(f"Using local model: {local_model_path}")
        else:
            logger.debug(f"Using HuggingFace model: {model_name}")

        cmd = [
            python_exe,
            "-m", "insanely_fast_whisper_rocm.cli",
            "transcribe",
            temp_audio_file,  # Positional argument for audio file
            "--model", model_name,
            "--language", language,
            "--timestamp-type", "word",
            "--output", temp_output_file
        ]

        logger.info(f"Command: {' '.join(cmd)}")

        # Set PYTHONPATH to include the insanely-fast-whisper-rocm module
        cmd_env = os.environ.copy()
        cmd_env["PYTHONPATH"] = f"{ifw_module_path}:{cmd_env.get('PYTHONPATH', '')}".rstrip(":")
        logger.debug(f"PYTHONPATH: {cmd_env.get('PYTHONPATH', 'not set')}")

        # Set LD_LIBRARY_PATH to use conda's FFmpeg 7 libraries (REPLACE system FFmpeg 8.1)
        conda_prefixes = [
            os.environ.get("CONDA_PREFIX", ""),
            "/home/fakemitch/pinokio/bin/miniconda",  # Pinokio's conda location
        ]

        for prefix in conda_prefixes:
            if prefix and os.path.exists(prefix):
                conda_lib = os.path.join(prefix, "lib")
                if os.path.exists(conda_lib):
                    # REPLACE LD_LIBRARY_PATH entirely with conda libraries first
                    cmd_env["LD_LIBRARY_PATH"] = conda_lib
                    logger.info(f"  ├─ FFmpeg: Using conda FFmpeg 7 (v{conda_lib})")
                    logger.debug(f"  └─ LD_LIBRARY_PATH: {cmd_env['LD_LIBRARY_PATH']}")
                    break

        # Run subprocess
        logger.info("▶ Running Whisper transcription (this may take several minutes)...")
        logger.debug("  Subprocess environment: FFmpeg 7 + ROCm optimization")
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600, env=cmd_env)

        if result.returncode == 0:
            logger.info(f"✓ Transcription subprocess completed successfully")
        else:
            logger.warning(f"⚠ Transcription subprocess returned code: {result.returncode}")

        logger.debug(f"  Output length: {len(result.stdout)} chars, Error length: {len(result.stderr)} chars")

        # Parse GPU info from subprocess output
        if result.stdout:
            import re as regex_module
            cuda_match = regex_module.search(r'ASR using device:\s*(cuda:\d+)', result.stdout)
            if cuda_match:
                device_info = cuda_match.group(1)
                logger.info(f"  ├─ GPU Device: {device_info} (confirmed in subprocess)")

            torch_match = regex_module.search(r'cuda_available=(\w+)', result.stdout)
            if torch_match:
                cuda_status = torch_match.group(1)
                logger.info(f"  ├─ CUDA Available: {cuda_status}")

            hip_match = regex_module.search(r'hip=([0-9.]+)', result.stdout)
            if hip_match:
                hip_version = hip_match.group(1)
                logger.info(f"  └─ HIP (ROCm) Version: {hip_version}")

            logger.debug(f"Subprocess stdout:\n{result.stdout}")

        if result.stderr:
            logger.debug(f"Subprocess stderr:\n{result.stderr}")

        if result.returncode != 0:
            logger.error(f"Transcription command failed with return code {result.returncode}")
            logger.error(f"stderr: {result.stderr}")
            raise RuntimeError(f"Transcription failed: {result.stderr}")

        # The tool ignores --output and saves to transcripts/ with its own naming
        # Parse stdout to find where it actually saved the JSON
        import re
        actual_json_file = None

        if result.stdout:
            # Look for "Saved JSON to: <path>" in the output
            match = re.search(r'Saved JSON to:\s*(.+?)(?:\n|$)', result.stdout)
            if match:
                actual_json_file = match.group(1).strip()
                logger.debug(f"Found JSON path in stdout: {actual_json_file}")

        if not actual_json_file:
            logger.error(f"Could not find JSON output path in subprocess stdout")
            logger.debug(f"Temp directory contents: {os.listdir(temp_dir) if os.path.exists(temp_dir) else 'temp_dir not found'}")
            raise FileNotFoundError(f"Could not determine where insanely-fast-whisper saved JSON output")

        # The path in stdout is relative to where the subprocess was run
        # We need to check current working directory
        if not os.path.isabs(actual_json_file):
            actual_json_file = os.path.join(os.getcwd(), actual_json_file)

        if not os.path.exists(actual_json_file):
            logger.error(f"JSON file not found at expected path: {actual_json_file}")
            raise FileNotFoundError(f"JSON output file not found: {actual_json_file}")

        file_size = os.path.getsize(actual_json_file)
        logger.info(f"✓ JSON found: {actual_json_file} ({file_size} bytes)")

        # Load and parse JSON
        logger.debug(f"Loading JSON from: {actual_json_file}")
        with open(actual_json_file, 'r') as f:
            result_json = json.load(f)

        logger.debug(f"JSON keys: {list(result_json.keys())}")

        detected_lang = result_json.get("language", language)
        logger.info(f"✓ Detected language: {detected_lang}")

        # Extract word segments
        logger.info("▶ Extracting word segments...")
        word_segments = []
        chunks = result_json.get("chunks")
        chunk_count = len(chunks) if chunks else 0
        logger.debug(f"  Processing {chunk_count} chunks...")

        if chunks:
            for idx, chunk in enumerate(chunks):
                timestamp = chunk.get("timestamp")
                text = chunk.get("text", "").strip()

                if timestamp and len(timestamp) >= 2:
                    start, end = timestamp[0], timestamp[1]
                    if start is not None and end is not None:
                        word_segments.append({
                            "word": text,
                            "start": start,
                            "end": end
                        })
                        if idx < 5:  # Log first few entries
                            logger.debug(f"  Chunk {idx}: '{text}' [{start:.3f}-{end:.3f}]")
                else:
                    logger.debug(f"  Chunk {idx}: Missing timestamp - {chunk}")

        clear_vram()
        logger.info(f"✓ Insanely Fast Whisper complete: {len(word_segments)} words extracted")
        logger.debug(f"First 5 words: {word_segments[:5]}")
        logger.debug(f"Last 5 words: {word_segments[-5:]}")

        return word_segments, detected_lang

    except Exception as e:
        logger.error(f"Insanely Fast Whisper transcription failed: {e}")
        logger.debug(traceback.format_exc())
        raise
    finally:
        # Clean up temporary files
        logger.debug("Cleaning up temporary files...")

        if temp_audio_file and os.path.exists(temp_audio_file):
            try:
                os.remove(temp_audio_file)
                logger.debug(f"✓ Removed audio file: {temp_audio_file}")
            except Exception as e:
                logger.warning(f"Failed to clean up audio file: {e}")

        if temp_output_file and os.path.exists(temp_output_file):
            try:
                os.remove(temp_output_file)
                logger.debug(f"✓ Removed output file: {temp_output_file}")
            except Exception as e:
                logger.warning(f"Failed to clean up output file: {e}")

        # Clean up the JSON file created by the tool in transcripts/ directory
        if 'actual_json_file' in locals() and actual_json_file:
            try:
                if os.path.exists(actual_json_file):
                    os.remove(actual_json_file)
                    logger.debug(f"✓ Removed JSON output: {actual_json_file}")
                # Try to remove transcripts directory if empty
                transcripts_dir = os.path.dirname(actual_json_file)
                if os.path.exists(transcripts_dir) and not os.listdir(transcripts_dir):
                    os.rmdir(transcripts_dir)
                    logger.debug(f"✓ Removed empty transcripts directory")
            except Exception as e:
                logger.debug(f"Note: Could not clean up JSON file: {e}")

        # Clean up temp directory
        if temp_dir and os.path.exists(temp_dir):
            try:
                remaining = os.listdir(temp_dir)
                if remaining:
                    logger.debug(f"Remaining files in {temp_dir}: {remaining}")
                shutil.rmtree(temp_dir, ignore_errors=True)
                logger.debug(f"✓ Removed temp directory: {temp_dir}")
            except Exception as e:
                logger.warning(f"Failed to clean up temp directory: {e}")

def choose_and_transcribe(audio_16k: np.ndarray, device: str, language: str) -> tuple:
    """Transcribe using Wav2Vec2 (continuous context-aware) as primary with fallbacks."""

    logger.info("=" * 70)
    logger.info("ASR Method Selection")
    logger.info("=" * 70)
    logger.info(f"Device: {device}")
    logger.info(f"Language: {language}")
    logger.info(f"Available ASR methods:")
    logger.info(f"  - Wav2Vec2 (GPU continuous): {TRANSFORMERS_WHISPER_AVAILABLE}")
    logger.info(f"  - InFastWhisper (ROCm): {INSANELY_FAST_WHISPER_AVAILABLE}")
    logger.info(f"  - WhisperX (CPU mode): {WHISPERX_AVAILABLE}")

    # Try Wav2Vec2 first (continuous context-aware)
    if TRANSFORMERS_WHISPER_AVAILABLE:
        logger.info("-" * 70)
        logger.info("▶ Method 1: Wav2Vec2 (Continuous context-aware) [GPU accelerated, 30s chunks with overlap]")
        logger.info("-" * 70)
        try:
            word_segments, detected_lang = transcribe_with_wav2vec2(audio_16k, language)
            logger.info(f"✓ SUCCESS with Wav2Vec2")
            logger.info(f"  ├─ Words extracted: {len(word_segments)}")
            logger.info(f"  ├─ Context preservation: Full audio (30s overlapping chunks)")
            logger.info(f"  └─ Detected language: {detected_lang}")
            return word_segments, detected_lang
        except Exception as e:
            logger.warning(f"✗ Wav2Vec2 failed: {e}")
            logger.debug(traceback.format_exc())
            logger.info("Falling back to Insanely Fast Whisper...")

    # Fallback to Insanely Fast Whisper (ROCm optimized)
    if INSANELY_FAST_WHISPER_AVAILABLE:
        logger.info("-" * 70)
        logger.info("▶ Method 2: Insanely Fast Whisper (ROCm optimized) [GPU accelerated, 30s chunks]")
        logger.info("-" * 70)
        try:
            word_segments, detected_lang = transcribe_with_insanely_fast_whisper(audio_16k, language)
            logger.info(f"✓ SUCCESS with Insanely Fast Whisper")
            logger.info(f"  ├─ Words extracted: {len(word_segments)}")
            logger.info(f"  └─ Detected language: {detected_lang}")
            return word_segments, detected_lang
        except Exception as e:
            logger.warning(f"✗ Insanely Fast Whisper failed: {e}")
            logger.debug(traceback.format_exc())
            logger.info("Falling back to WhisperX-CPU...")

    # Final fallback to WhisperX-CPU
    if WHISPERX_AVAILABLE:
        logger.info("-" * 70)
        logger.info("▶ Method 3: WhisperX-CPU (Stable fallback) [CPU mode, word-level alignment]")
        logger.info("-" * 70)
        try:
            word_segments, detected_lang = transcribe_with_whisperx_cpu(audio_16k, language)
            logger.info(f"✓ SUCCESS with WhisperX-CPU")
            logger.info(f"  ├─ Words extracted: {len(word_segments)}")
            logger.info(f"  └─ Detected language: {detected_lang}")
            return word_segments, detected_lang
        except Exception as e:
            logger.error(f"✗ WhisperX-CPU also failed: {e}")
            logger.debug(traceback.format_exc())

    logger.critical("=" * 70)
    logger.critical("✗ CRITICAL: No ASR method available!")
    logger.critical("Install with: pip install insanely-fast-whisper-rocm whisperx")
    logger.critical("=" * 70)
    sys.exit(1)

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


def _load_existing_checkpoint(temp_dir):
    """Read existing metadata.jsonl checkpoint and return (entries, resume_time, next_segment_idx)."""
    checkpoint_path = os.path.join(temp_dir, "metadata.jsonl")
    entries = []
    if not os.path.exists(checkpoint_path):
        return entries, 0.0, 0

    try:
        with open(checkpoint_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                entries.append(json.loads(line))
    except Exception as e:
        logger.warning(f"Could not parse checkpoint {checkpoint_path}: {e}")
        return [], 0.0, 0

    if not entries:
        return [], 0.0, 0

    resume_time = max(e.get("end", 0.0) for e in entries)
    next_idx = max(int(e["audio_filepath"].split("_")[1].split(".")[0]) for e in entries) + 1
    return entries, resume_time, next_idx


def _wipe_temp_dir(temp_dir):
    """Remove all preparer-generated files from temp_dir but keep the directory itself."""
    if not os.path.exists(temp_dir):
        return
    for name in os.listdir(temp_dir):
        full_path = os.path.join(temp_dir, name)
        try:
            if os.path.isfile(full_path) or os.path.islink(full_path):
                os.remove(full_path)
            elif os.path.isdir(full_path):
                shutil.rmtree(full_path)
        except Exception as e:
            logger.warning(f"Failed to remove {full_path}: {e}")


def _check_source_marker(temp_dir, audio_source_path):
    """Return True if temp_dir's .source marker matches audio_source_path."""
    marker_path = os.path.join(temp_dir, ".source")
    if not os.path.exists(marker_path):
        return False
    try:
        with open(marker_path, "r", encoding="utf-8") as f:
            stored = f.read().strip()
        return stored == os.path.abspath(audio_source_path)
    except Exception:
        return False


def _write_source_marker(temp_dir, audio_source_path):
    """Write the .source marker so future runs can verify this temp_dir's owner."""
    marker_path = os.path.join(temp_dir, ".source")
    with open(marker_path, "w", encoding="utf-8") as f:
        f.write(os.path.abspath(audio_source_path))


def annotate_chunks(word_segments, model_path, chunk_size, audio_24k_source,
                    resume=False, audio_source_path=None):
    """Create and annotate chunks with periodic checkpointing and resume support.

    audio_24k_source: either a numpy array (in-memory) or a path to a 24kHz WAV file.
    audio_source_path: the original input audio path, used to validate resume safety.
    """
    temp_dir = "dataset_temp"
    os.makedirs(temp_dir, exist_ok=True)
    checkpoint_path = os.path.join(temp_dir, "metadata.jsonl")

    # Determine if existing dataset_temp/ belongs to this audio file
    marker_matches = (
        audio_source_path is not None
        and _check_source_marker(temp_dir, audio_source_path)
    )

    if resume and marker_matches:
        existing_entries, resume_time, next_segment_idx = _load_existing_checkpoint(temp_dir)
        if existing_entries:
            logger.info(f"▶ Resuming from checkpoint: {len(existing_entries)} segments already processed")
            logger.info(f"  ├─ Source verified: {audio_source_path}")
            logger.info(f"  ├─ Resume time: {resume_time:.2f}s")
            logger.info(f"  └─ Next segment index: {next_segment_idx}")
        else:
            logger.info("▶ --resume specified but no checkpoint found, starting fresh")
            existing_entries, resume_time, next_segment_idx = [], 0.0, 0
    else:
        if resume and not marker_matches:
            logger.warning(
                "▶ --resume specified, but dataset_temp/ belongs to a different source file "
                "(or has no marker). Wiping and starting fresh to avoid corrupting another run."
            )
        elif os.listdir(temp_dir):
            logger.info("▶ Wiping stale dataset_temp/ contents for fresh start")
        _wipe_temp_dir(temp_dir)
        existing_entries, resume_time, next_segment_idx = [], 0.0, 0

    # Always (re)write the source marker for the current run
    if audio_source_path is not None:
        _write_source_marker(temp_dir, audio_source_path)

    logger.info("▶ Loading Gemma 4 LLM model for annotations...")
    logger.info("  ├─ Device: GPU (CUDA/ROCm acceleration)")
    logger.info("  ├─ GPU Layers: All (-1 = fully loaded to GPU)")
    logger.info("  └─ Periodic checkpoint: every 50 segments")

    try:
        logger.debug(f"Loading GGUF model from: {model_path}")
        llm = Llama(
            model_path=model_path,
            n_gpu_layers=-1,
            n_ctx=4096,
            verbose=False
        )
        logger.info("✓ Gemma 4 model loaded")

        if hasattr(llm, 'n_gpu_layers'):
            logger.info(f"  ├─ GPU Layers Loaded: {llm.n_gpu_layers}")
        logger.info(f"  └─ Model device: {llm.metadata.get('device', 'cuda (via n_gpu_layers=-1)')}")

        logger.debug("Verifying GPU inference capability with test prompt...")
        llm.create_chat_completion(
            messages=[{"role": "user", "content": "test"}],
            max_tokens=1
        )
        logger.info(f"✓ GPU inference verified - model responding on GPU")
        log_gpu_stats("after Gemma model load and test")

    except Exception as e:
        logger.error(f"Failed to load LLM model: {e}")
        logger.debug(traceback.format_exc())
        raise

    metadata = list(existing_entries)
    segment_idx = next_segment_idx
    current_words = []
    current_start = resume_time  # Start fresh after the resume point
    context = deque(maxlen=5)

    # Pre-populate context from last 5 resumed entries for continuity
    for prior in metadata[-5:]:
        context.append(prior.get("text", ""))

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

    # Open checkpoint file in append mode so we don't lose previous entries
    checkpoint_file = open(checkpoint_path, "a", encoding="utf-8")

    try:
        for idx, word_data in enumerate(word_segments):
            if "start" not in word_data or "end" not in word_data:
                continue

            word_start_time = word_data["start"]
            # Skip words before the resume point
            if word_start_time < current_start:
                continue

            word = word_data.get("word", "").strip()
            if not word:
                continue

            current_words.append(word)
            current_end = word_data["end"]
            duration = current_end - current_start

            if duration >= chunk_size or idx == len(word_segments) - 1:
                if current_words and duration >= 1.0:
                    chunk_t0 = time.monotonic()
                    text = " ".join(current_words)

                    # Build prompt with context (deque-based, last 3 entries)
                    ctx = " ".join(list(context)[-3:]) if context else ""
                    if ctx:
                        prompt = f"Previous: {ctx}\n\nAnnotate for TTS:\n{text}"
                    else:
                        prompt = f"Annotate for TTS:\n{text}"

                    try:
                        response = llm.create_chat_completion(
                            messages=[{"role": "user", "content": prompt}],
                            max_tokens=512,
                            temperature=0.7
                        )
                        annotated = response["choices"][0]["message"]["content"].strip()
                        if segment_idx == next_segment_idx:
                            logger.info(f"✓ Gemma GPU inference confirmed - working on GPU")
                    except Exception as e:
                        logger.warning(f"Annotation failed for segment {segment_idx}, using original text: {e}")
                        annotated = text

                    # Read audio segment (in-memory slice or disk seek)
                    audio_slice = _read_audio_segment(audio_24k_source, current_start, current_end)

                    if len(audio_slice) > 0:
                        seg_name = f"sample_{segment_idx:04d}.wav"
                        sf.write(os.path.join(temp_dir, seg_name), audio_slice, 24000)

                        entry = {
                            "audio_filepath": seg_name,
                            "text": annotated,
                            "duration": len(audio_slice) / 24000,
                            "start": current_start,
                            "end": current_end
                        }
                        metadata.append(entry)

                        # Append to checkpoint immediately (durable across crashes)
                        checkpoint_file.write(json.dumps(entry, ensure_ascii=False) + "\n")
                        if (segment_idx + 1) % 50 == 0:
                            checkpoint_file.flush()
                            os.fsync(checkpoint_file.fileno())

                        chunk_times.append(time.monotonic() - chunk_t0)

                        # Dynamic ETA from rolling average
                        if (segment_idx + 1) % 10 == 0:
                            avg_chunk_s = sum(chunk_times) / len(chunk_times)
                            completed_this_run = segment_idx - next_segment_idx + 1
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

                        segment_idx += 1

                    context.append(text)
                    current_words = []
                    current_start = current_end
    finally:
        checkpoint_file.close()
        del llm
        clear_vram()

    logger.info(f"✓ Annotation complete: {segment_idx} segments created")
    if metadata:
        total_duration = sum(m["duration"] for m in metadata)
        logger.info(f"  ├─ Total audio in dataset: {total_duration:.1f}s")
        logger.info(f"  └─ Average segment duration: {total_duration/len(metadata):.2f}s")
    return metadata

def main():
    parser = argparse.ArgumentParser(
        description="Alexandria Master Preparer - ROCm Compatible"
    )

    parser.add_argument("--audio", required=True, help="Input audio file")
    parser.add_argument("--model", help="Gemma GGUF model")
    parser.add_argument("--skip-annotation", action="store_true")
    parser.add_argument("--chunk-size", type=float, default=10.0)
    parser.add_argument("--lang", default="en")
    parser.add_argument("--output", default="alexandria_dataset.zip",
                        help="Output ZIP path (default: alexandria_dataset.zip)")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from existing dataset_temp/ instead of starting over")

    args = parser.parse_args()

    if not args.skip_annotation and not args.model:
        parser.error("--model required unless --skip-annotation")

    device = "cuda" if torch.cuda.is_available() else "cpu"

    logger.info("=" * 70)
    logger.info("Alexandria Master Preparer - ROCm Compatible Edition")
    logger.info("=" * 70)
    logger.info(f"Device: {device}")
    if device == "cuda":
        logger.info(f"  ├─ GPU: {torch.cuda.get_device_name(0)}")
        logger.info(f"  ├─ CUDA Available: {torch.cuda.is_available()}")
        logger.info(f"  └─ GPU Count: {torch.cuda.device_count()}")
    else:
        logger.warning("⚠ GPU not available - running on CPU (slower)")
    logger.info(f"Arguments: audio={args.audio}, model={args.model}, chunk_size={args.chunk_size}, lang={args.lang}")

    audio_24k_scratch = ".alexandria_audio_24k.wav"
    completed_successfully = False

    try:
        progress.start("Validate inputs")
        validate_inputs(args)
        os.makedirs("dataset_temp", exist_ok=True)
        progress.complete()

        progress.start("Load audio")
        logger.debug(f"Loading audio from {args.audio} (single read)...")
        load_t0 = time.monotonic()
        audio_native, native_sr = librosa.load(args.audio, sr=None, mono=True)
        logger.debug(f"  Native sample rate: {native_sr}Hz, duration: {len(audio_native)/native_sr:.1f}s")

        if native_sr == 16000:
            audio_16k = audio_native
        else:
            logger.debug(f"  Resampling to 16kHz (in memory)...")
            audio_16k = librosa.resample(audio_native, orig_sr=native_sr, target_sr=16000)

        if native_sr == 24000:
            audio_24k = audio_native
        else:
            logger.debug(f"  Resampling to 24kHz (in memory)...")
            audio_24k = librosa.resample(audio_native, orig_sr=native_sr, target_sr=24000)

        if native_sr not in (16000, 24000):
            del audio_native
            gc.collect()

        duration_secs = len(audio_24k) / 24000
        logger.info(f"  Audio: {duration_secs:.1f}s @ {len(audio_24k)} samples (loaded in {time.monotonic()-load_t0:.1f}s)")

        # Spill 24kHz audio to disk so RAM is free during the 60+ hour annotation
        logger.debug(f"  Spilling 24kHz audio to scratch file: {audio_24k_scratch}")
        sf.write(audio_24k_scratch, audio_24k, 24000)
        del audio_24k
        gc.collect()
        scratch_size_mb = os.path.getsize(audio_24k_scratch) / (1024 * 1024)
        logger.info(f"  ├─ Scratch audio: {audio_24k_scratch} ({scratch_size_mb:.1f} MB) - freed from RAM")
        progress.complete()

        progress.start("Transcribe audio")
        word_segments, detected_lang = choose_and_transcribe(audio_16k, device, args.lang)
        logger.info(f"  Detected language: {detected_lang}")
        logger.info(f"  Segments extracted: {len(word_segments)}")
        del audio_16k
        clear_vram()
        progress.complete()

        progress.start("Annotate chunks")
        if not args.skip_annotation:
            metadata = annotate_chunks(
                word_segments,
                args.model,
                args.chunk_size,
                audio_24k_scratch,
                resume=args.resume,
                audio_source_path=args.audio,
            )
            logger.info(f"  Chunks annotated: {len(metadata)}")
        else:
            logger.error("--skip-annotation not yet implemented")
            sys.exit(1)
        progress.complete()

        progress.start("Create output dataset")
        # metadata.jsonl is already written incrementally during annotation
        logger.debug(f"Creating ZIP archive: {args.output}")
        with zipfile.ZipFile(args.output, "w", zipfile.ZIP_DEFLATED) as z:
            for file in sorted(os.listdir("dataset_temp")):
                # Skip hidden files (e.g., any future scratch artifacts)
                if file.startswith("."):
                    continue
                z.write(os.path.join("dataset_temp", file), file)

        durations = [m["duration"] for m in metadata]
        logger.info(f"  Dataset saved: {args.output}")
        progress.complete()

        logger.info("=" * 70)
        logger.info(f"Total segments: {len(metadata)}")
        if durations:
            logger.info(f"Average duration: {np.mean(durations):.2f}s")
            logger.info(f"Total audio: {sum(durations)/60:.1f} minutes")
        logger.info("=" * 70)
        logger.info(f"✓ SUCCESS: {args.output} ready!")

        completed_successfully = True
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
        # Always clean up the scratch audio file (not needed for resume)
        if os.path.exists(audio_24k_scratch):
            try:
                os.remove(audio_24k_scratch)
                logger.debug(f"Removed scratch audio: {audio_24k_scratch}")
            except Exception as e:
                logger.warning(f"Failed to remove scratch audio: {e}")

        # Only remove dataset_temp on successful completion (preserves resume state on failure)
        if completed_successfully and os.path.exists("dataset_temp"):
            try:
                shutil.rmtree("dataset_temp")
                logger.debug("Cleaned up dataset_temp/")
            except Exception as e:
                logger.warning(f"Failed to cleanup temp directory: {e}")

        logger.info(f"Log file saved to: {log_file}")

if __name__ == "__main__":
    sys.exit(main())
