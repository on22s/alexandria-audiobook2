"""Shared rocm-smi JSON-parsing helper.

Lives at the repo root (not inside app/) so both the FastAPI app (via
app/utils.py's re-export) and the standalone root-level scripts
(alexandria_batch_processor.py, alexandria_preparer_rocm_compatible.py)
can import it without either side needing to reach across the app/
package boundary.
"""

import json
import logging
import os
import platform
import shutil
import subprocess
from typing import Optional

logger = logging.getLogger(__name__)


def rocm_smi_utilization(card_data: dict) -> Optional[float]:
    """Parse a rocm-smi card's utilization percent from whichever of the 3
    known key-name variants it reports under, or None if absent/unparseable.

    Shared between app/app.py (both _gpu_stats_via_rocm_smi and
    get_gpu_stats) and alexandria_preparer_rocm_compatible.py's own
    get_gpu_stats - the preparer's copy used to check only one of the 3
    variants ("GPU use (%)"), silently drifting from the app's broader
    check until consolidated here.
    """
    for key in ("GPU use (%)", "GPU Use (%)", "GPU Activity"):
        v = card_data.get(key)
        if v not in (None, "N/A"):
            try:
                return float(v)
            except (ValueError, TypeError):
                pass
    return None


def nvidia_smi_utilization(timeout=5):
    """Sample the primary NVIDIA GPU's utilization percent via nvidia-smi, or
    None if the binary is missing, times out, exits non-zero, or produces
    unparseable output. Mirrors rocm_smi_utilization's failure handling so
    NVIDIA hosts (e.g. Thunder) can capture a comparable metric to AMD's.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=utilization.gpu",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        logger.debug("nvidia-smi not found")
        return None
    except subprocess.TimeoutExpired:
        logger.debug(f"nvidia-smi timed out after {timeout}s")
        return None
    except Exception as e:
        logger.debug(f"nvidia-smi unexpected error: {e}")
        return None
    if result.returncode != 0:
        logger.debug(f"nvidia-smi returned error: {result.returncode}, stderr: {result.stderr}")
        return None
    first_line = next((line.strip() for line in result.stdout.splitlines() if line.strip()), None)
    if not first_line:
        return None
    try:
        return float(first_line)
    except ValueError:
        return None


def sample_gpu_utilization(rocm_smi_path="rocm-smi", timeout=2):
    """Best-effort GPU utilization percent for whichever backend is present
    (NVIDIA via nvidia-smi, AMD via rocm-smi), or None if neither succeeds.

    Single dispatch point so callers (e.g. tts_benchmark.py, run either
    locally or on Thunder) don't need to know in advance which backend the
    host they're running on actually has.
    """
    nvidia_value = nvidia_smi_utilization(timeout=timeout)
    if nvidia_value is not None:
        return nvidia_value
    data = run_rocm_smi_json(["--showuse"], rocm_smi_path=rocm_smi_path, timeout=timeout)
    if not data:
        return None
    for card_data in data.values():
        if isinstance(card_data, dict):
            value = rocm_smi_utilization(card_data)
            if value is not None:
                return value
    return None


OOM_MARKERS = (
    "out of memory", "outofmemory", "cuda out of memory", "cuda error",
    "hip out of memory", "hip error", "cublas_status_alloc_failed",
    "cannot allocate memory", "alloc failed",
)


def is_oom_failure(err) -> bool:
    """True if an error message looks like a GPU/VRAM out-of-memory condition,
    so the caller can step concurrency down and retry rather than give up.

    Shared between app/project.py (parallel TTS generation) and
    app/train_lora.py (LoRA training, a separate-venv subprocess that can't
    import app/project.py directly) so both recognize the same set of
    OOM-message variants instead of train_lora.py's own narrower copy
    silently drifting from whatever markers project.py adds later.
    """
    return any(marker in str(err).lower() for marker in OOM_MARKERS)


def system_has_gpu():
    """Best-effort, torch-independent check for whether this machine has a
    GPU at all (NVIDIA via nvidia-smi, AMD via rocm-smi, Apple Silicon via
    platform detection). Returns (has_gpu: bool, vendor_label: str | None).

    This says nothing about whether torch (or llama-cpp, etc.) can actually
    USE the GPU - just whether one is physically present. Comparing this
    against torch.cuda.is_available() (or an inference library's own
    GPU-offload state) is what catches a wrong-build install, e.g. a
    CUDA-only torch wheel on an AMD box: from the outside, "GPU present but
    the library can't see it" and "no GPU at all" both just look like
    everything quietly running on CPU, unless something checks the hardware
    independently.
    """
    if platform.system() == "Darwin" and platform.machine() == "arm64":
        return True, "Apple Silicon (Metal)"

    if shutil.which("nvidia-smi"):
        try:
            r = subprocess.run(["nvidia-smi", "-L"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return True, "NVIDIA"
        except (OSError, subprocess.TimeoutExpired):
            pass

    rocm_smi = shutil.which("rocm-smi") or "/opt/rocm/bin/rocm-smi"
    if os.path.exists(rocm_smi):
        try:
            r = subprocess.run([rocm_smi, "--showproductname"], capture_output=True, text=True, timeout=5)
            if r.returncode == 0 and r.stdout.strip():
                return True, "AMD/ROCm"
        except (OSError, subprocess.TimeoutExpired):
            pass

    return False, None


def run_rocm_smi_json(args, rocm_smi_path="rocm-smi", timeout=5):
    """Run `<rocm_smi_path> <args> --json` and return the parsed per-card dict, or None.

    Filters stdout down to JSON-looking lines first, since rocm-smi sometimes
    prints warnings to stdout ahead of the JSON payload. Returns None if the
    binary is missing, times out, exits non-zero, or produces no JSON.
    """
    try:
        result = subprocess.run(
            [rocm_smi_path] + list(args) + ["--json"],
            capture_output=True, text=True, timeout=timeout
        )
    except FileNotFoundError:
        logger.debug(f"{rocm_smi_path} not found")
        return None
    except subprocess.TimeoutExpired:
        logger.debug(f"{rocm_smi_path} timed out after {timeout}s")
        return None
    except Exception as e:
        logger.debug(f"{rocm_smi_path} unexpected error: {e}")
        return None

    if result.returncode != 0:
        logger.debug(f"{rocm_smi_path} returned error: {result.returncode}, stderr: {result.stderr}")
        return None

    # rocm-smi sometimes prints warnings to stdout ahead of the JSON, and
    # the JSON payload itself may be pretty-printed across several lines.
    # Parse everything from the first line that opens the JSON object so a
    # multi-line payload isn't truncated to just "{".
    lines = result.stdout.split('\n')
    for i, line in enumerate(lines):
        if line.strip().startswith('{'):
            try:
                return json.loads('\n'.join(lines[i:]))
            except (json.JSONDecodeError, ValueError) as e:
                logger.debug(f"{rocm_smi_path} JSON parse error: {e}")
                return None
    return None
