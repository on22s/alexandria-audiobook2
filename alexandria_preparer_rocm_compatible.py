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
import librosa
import logging
import json
import subprocess
import zipfile
import shutil
import soundfile as sf
import numpy as np
import traceback
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
    """Use Wav2Vec2 for continuous context-aware transcription."""
    if not TRANSFORMERS_WHISPER_AVAILABLE:
        raise ImportError("Transformers not available")

    logger.info("▶ Initializing Wav2Vec2 ASR (continuous context-aware)...")
    logger.info(f"  ├─ Model: facebook/wav2vec2-large-960h")
    logger.info(f"  ├─ Device: GPU (CUDA/ROCm)")
    logger.info(f"  └─ Language: {language}")

    try:
        from transformers import Wav2Vec2Processor, Wav2Vec2ForCTC
        import torch as torch_module

        logger.debug("Loading Wav2Vec2 processor and model...")
        device_str = "cuda" if torch_module.cuda.is_available() else "cpu"

        # Load pre-trained model
        processor = Wav2Vec2Processor.from_pretrained("facebook/wav2vec2-large-960h")
        model = Wav2Vec2ForCTC.from_pretrained("facebook/wav2vec2-large-960h")
        model = model.to(device_str)
        model.eval()

        logger.info("✓ Wav2Vec2 model loaded to GPU")

        # Configure context window for better understanding
        # Wav2Vec2 can handle up to ~30 seconds per chunk comfortably on modern GPUs
        # Larger chunks = better context but higher memory usage
        # For AMD Radeon RX 9070 XT (16GB+ VRAM): can use 30s chunks
        chunk_length_secs = 30  # Can be 15, 20, 25, or 30 seconds
        chunk_length = chunk_length_secs * 16000  # Convert to samples
        overlap_secs = 3  # Overlap between chunks (3 seconds)
        overlap = overlap_secs * 16000  # Convert to samples
        stride = chunk_length - overlap

        all_logits = []
        num_chunks = (len(audio_16k) - chunk_length) // stride + 1

        logger.info(f"  ├─ Context window: {chunk_length_secs}s (larger = better understanding)")
        logger.info(f"  ├─ Overlap: {overlap_secs}s (for seamless transitions)")
        logger.info(f"  └─ Processing {num_chunks} chunks...")
        logger.debug(f"Processing {num_chunks} chunks with {overlap_secs}s overlap for context preservation...")

        # Process chunks
        for i in range(0, len(audio_16k) - chunk_length + 1, stride):
            chunk = audio_16k[i : i + chunk_length]

            with torch_module.no_grad():
                inputs = processor(chunk, sampling_rate=16000, return_tensors="pt", padding=True)
                inputs = {k: v.to(device_str) for k, v in inputs.items()}

                logits = model(**inputs).logits
                all_logits.append(logits)

        # Combine logits with proper handling of overlaps
        logger.debug("Combining chunk logits with overlap handling...")

        # Process final chunk if audio length is not evenly divisible
        if len(audio_16k) > i + chunk_length:
            remaining = audio_16k[i + chunk_length :]
            if len(remaining) > 0:
                with torch_module.no_grad():
                    inputs = processor(remaining, sampling_rate=16000, return_tensors="pt", padding=True)
                    inputs = {k: v.to(device_str) for k, v in inputs.items()}
                    logits = model(**inputs).logits
                    all_logits.append(logits)

        # Get predictions from logits
        predicted_ids = torch_module.argmax(torch_module.cat(all_logits, dim=1), dim=-1)
        transcription = processor.batch_decode(predicted_ids)[0]

        logger.info(f"✓ Wav2Vec2 transcription complete: {len(transcription.split())} words")

        # Create word segments with timing
        words = transcription.split()
        duration = len(audio_16k) / 16000
        time_per_word = duration / max(1, len(words))

        word_segments = []
        for idx, word in enumerate(words):
            word_segments.append({
                "word": word.strip(),
                "start": idx * time_per_word,
                "end": (idx + 1) * time_per_word
            })

        del processor, model
        clear_vram()

        logger.info(f"✓ Wav2Vec2 complete: {len(word_segments)} words extracted")
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

def annotate_chunks(word_segments, model_path, chunk_size, audio_24k):
    """Create and annotate chunks."""

    logger.info("▶ Loading Gemma 4 LLM model for annotations...")
    logger.info("  ├─ Device: GPU (CUDA/ROCm acceleration)")
    logger.info("  ├─ GPU Layers: All (-1 = fully loaded to GPU)")
    logger.info("  └─ Estimated speed: ~45-50 seconds per chunk")

    try:
        logger.debug(f"Loading GGUF model from: {model_path}")
        llm = Llama(
            model_path=model_path,
            n_gpu_layers=-1,
            n_ctx=4096,
            verbose=False
        )
        logger.info("✓ Gemma 4 model loaded")

        # Verify GPU usage
        if hasattr(llm, 'n_gpu_layers'):
            logger.info(f"  ├─ GPU Layers Loaded: {llm.n_gpu_layers}")
        logger.info(f"  ├─ Model device: {llm.metadata.get('device', 'cuda (via n_gpu_layers=-1)')}")
        logger.info(f"  └─ Context size: {llm.n_ctx} tokens")

        # Log first inference to confirm GPU is being used
        logger.debug("Verifying GPU inference capability with test prompt...")
        test_response = llm.create_chat_completion(
            messages=[{"role": "user", "content": "test"}],
            max_tokens=1
        )
        logger.info(f"✓ GPU inference verified - model responding on GPU")

    except Exception as e:
        logger.error(f"Failed to load LLM model: {e}")
        logger.debug(traceback.format_exc())
        raise

    metadata = []
    segment_idx = 0
    current_words = []
    current_start = 0.0
    context = []

    total_words = len(word_segments)
    logger.info(f"▶ Creating and annotating chunks (target: {chunk_size}s per chunk)...")
    logger.info(f"  Processing {total_words} word segments...")

    # Estimate number of chunks and processing time
    # Rough estimate: ~10-15 words per 10s chunk
    estimated_chunks = max(1, int((len(word_segments) / 12) * (chunk_size / 10)))
    estimated_secs = estimated_chunks * 46  # ~46 seconds per chunk
    estimated_mins = estimated_secs // 60
    estimated_hours = estimated_mins // 60
    estimated_mins = estimated_mins % 60

    if estimated_hours > 0:
        time_str = f"{estimated_hours}h {estimated_mins}m"
    else:
        time_str = f"{estimated_mins}m"

    logger.info(f"  ├─ Estimated chunks: ~{estimated_chunks}")
    logger.info(f"  └─ Estimated time: ~{time_str} ({estimated_secs}s @ ~46s/chunk)")

    for idx, word_data in enumerate(word_segments):
        if "start" not in word_data or "end" not in word_data:
            logger.debug(f"Skipping word {idx}: missing start/end")
            continue

        word = word_data.get("word", "").strip()
        if not word:
            continue

        current_words.append(word)
        current_end = word_data["end"]
        duration = current_end - current_start

        if duration >= chunk_size or idx == len(word_segments) - 1:
            if current_words and duration >= 1.0:
                text = " ".join(current_words)
                logger.debug(f"Creating segment {segment_idx}: {len(current_words)} words, {duration:.2f}s")

                # Build prompt with context
                ctx = " ".join(context[-3:]) if context else ""
                if ctx:
                    prompt = f"Previous: {ctx}\n\nAnnotate for TTS:\n{text}"
                else:
                    prompt = f"Annotate for TTS:\n{text}"

                try:
                    logger.debug(f"Generating annotation for segment {segment_idx} (GPU inference)...")
                    response = llm.create_chat_completion(
                        messages=[{"role": "user", "content": prompt}],
                        max_tokens=512,
                        temperature=0.7
                    )
                    annotated = response["choices"][0]["message"]["content"].strip()
                    logger.debug(f"✓ GPU inference completed - annotation: {annotated[:50]}...")
                    if segment_idx == 0:
                        logger.info(f"✓ Gemma GPU inference confirmed - working on GPU")
                except Exception as e:
                    logger.warning(f"Annotation failed for segment {segment_idx}, using original text: {e}")
                    annotated = text

                # Save audio
                start_samp = max(0, round(current_start * 24000))
                end_samp = min(len(audio_24k), round(current_end * 24000))
                audio_slice = audio_24k[start_samp:end_samp].astype(np.float32)

                if len(audio_slice) > 0:
                    seg_name = f"sample_{segment_idx:04d}.wav"
                    logger.debug(f"Writing audio segment: {seg_name} ({len(audio_slice)} samples)")
                    sf.write(f"dataset_temp/{seg_name}", audio_slice, 24000)

                    metadata.append({
                        "audio_filepath": seg_name,
                        "text": annotated,
                        "duration": len(audio_slice) / 24000
                    })

                    if (segment_idx + 1) % 10 == 0:
                        elapsed_audio = (current_end - current_start)
                        elapsed_proc_secs = (segment_idx + 1) * 46
                        elapsed_proc_mins = elapsed_proc_secs // 60
                        elapsed_proc_hours = elapsed_proc_mins // 60
                        elapsed_proc_mins = elapsed_proc_mins % 60

                        if elapsed_proc_hours > 0:
                            time_str = f"{elapsed_proc_hours}h {elapsed_proc_mins}m"
                        else:
                            time_str = f"{elapsed_proc_mins}m"

                        remaining_chunks = max(0, estimated_chunks - (segment_idx + 1))
                        remaining_secs = remaining_chunks * 46
                        remaining_mins = remaining_secs // 60
                        remaining_hours = remaining_mins // 60
                        remaining_mins = remaining_mins % 60

                        if remaining_hours > 0:
                            remaining_str = f"{remaining_hours}h {remaining_mins}m"
                        else:
                            remaining_str = f"{remaining_mins}m"

                        logger.info(f"  ↳ Progress: {segment_idx + 1}/{estimated_chunks} chunks | Elapsed: {time_str} | Remaining: ~{remaining_str}")

                    segment_idx += 1

                context.append(text)
                if len(context) > 5:
                    context.pop(0)

                current_words = []
                current_start = current_end

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

    try:
        progress.start("Validate inputs")
        validate_inputs(args)
        os.makedirs("dataset_temp", exist_ok=True)
        progress.complete()

        progress.start("Load audio")
        logger.debug(f"Loading 16kHz version from {args.audio}...")
        audio_16k, _ = librosa.load(args.audio, sr=16000, mono=True)
        logger.debug(f"Loading 24kHz version from {args.audio}...")
        audio_24k, _ = librosa.load(args.audio, sr=24000, mono=True)
        duration = len(audio_24k) / 24000
        logger.info(f"  Audio: {duration:.1f}s @ {len(audio_24k)} samples")
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
            metadata = annotate_chunks(word_segments, args.model, args.chunk_size, audio_24k)
            logger.info(f"  Chunks annotated: {len(metadata)}")
        else:
            logger.error("--skip-annotation not yet implemented")
            sys.exit(1)
        progress.complete()

        progress.start("Create output dataset")
        logger.debug("Writing metadata.jsonl...")
        with open("dataset_temp/metadata.jsonl", "w", encoding="utf-8") as f:
            for entry in metadata:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")

        logger.debug("Creating ZIP archive...")
        with zipfile.ZipFile("alexandria_dataset.zip", "w", zipfile.ZIP_DEFLATED) as z:
            for file in os.listdir("dataset_temp"):
                z.write(os.path.join("dataset_temp", file), file)

        shutil.rmtree("dataset_temp")

        durations = [m["duration"] for m in metadata]
        logger.info(f"  Dataset saved: alexandria_dataset.zip")
        progress.complete()

        logger.info("=" * 70)
        logger.info(f"Total segments: {len(metadata)}")
        logger.info(f"Average duration: {np.mean(durations):.2f}s")
        logger.info(f"Total audio: {sum(durations)/60:.1f} minutes")
        logger.info("=" * 70)
        logger.info("✓ SUCCESS: alexandria_dataset.zip ready!")

        return 0

    except KeyboardInterrupt:
        logger.info("Process interrupted by user")
        return 130
    except Exception as e:
        logger.critical(f"Fatal error: {e}")
        logger.debug(traceback.format_exc())
        return 1
    finally:
        if os.path.exists("dataset_temp"):
            try:
                logger.debug("Cleaning up temporary files...")
                shutil.rmtree("dataset_temp")
            except Exception as e:
                logger.warning(f"Failed to cleanup temp directory: {e}")
        logger.info(f"Log file saved to: {log_file}")

if __name__ == "__main__":
    sys.exit(main())
