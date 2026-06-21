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

logger = logging.getLogger(__name__)


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
