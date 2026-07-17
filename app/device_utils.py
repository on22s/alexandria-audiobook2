"""Tiny, dependency-free device-resolution helpers shared between app/tts.py
(the FastAPI server's TTSEngine) and app/train_lora.py (an independent
subprocess that runs in a separate ROCm-enabled venv without app/env's other
dependencies). torch is imported lazily inside each function - never at
module level - so this file can be imported by either environment regardless
of whether torch is installed there yet.
"""
import os
import sys

# Canonical implementation lives in gpu_stats.py at the repo root, shared
# with the standalone alexandria_*.py scripts the same way app/utils.py
# re-exports run_rocm_smi_json from there - this file is imported by
# train_lora.py running under a *different* venv/cwd (the ROCm interpreter,
# possibly invoked from a sibling repo's cwd), so the path is resolved from
# __file__, not cwd.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from gpu_stats import system_has_gpu, is_oom_failure  # noqa: F401


def normalize_device(device_str, allow_auto=True):
    """Return the canonical PyTorch device name for accepted user syntax."""
    value = str(device_str or "auto").strip().lower()
    if value in ("rocm", "hip"):
        return "cuda"
    if value == "auto" and allow_auto:
        return value
    if value in ("cpu", "cuda", "mps"):
        return value
    if value.startswith("cuda:") and value[5:].isdigit():
        return value
    allowed = "auto, cpu, cuda, cuda:N, mps, rocm, or hip"
    raise ValueError(f"Unsupported device '{device_str}'; expected {allowed}")


def resolve_device(device_str):
    """Resolve 'auto' to the best available torch device: cuda > mps > cpu.

    Logs a loud warning - not a quiet fallback - when a GPU is physically
    present but torch can't see it. That combination means an install/build
    mismatch (wrong-vendor torch wheel, or a later pip/uv install silently
    replacing a GPU build with a generic one), not the ordinary "this
    machine has no GPU" case, and it's easy to miss precisely because both
    cases look identical from the outside: everything just runs on CPU.
    """
    device_str = normalize_device(device_str)
    if device_str != "auto":
        return device_str
    torch_unavailable_reason = None
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
        torch_unavailable_reason = "torch.cuda.is_available() is False"
    except ImportError:
        torch_unavailable_reason = "torch isn't importable in this environment"

    # Run this check on every CPU fallback, not just "torch imported fine but
    # found no GPU" - a missing torch install entirely (e.g. the wrong venv,
    # or a partially-failed install) should get the same loud signal, not
    # silently look identical to a machine that genuinely has no GPU.
    has_gpu, vendor = system_has_gpu()
    if has_gpu:
        print(
            f"WARNING: {vendor} GPU detected on this system, but falling back to "
            f"CPU anyway ({torch_unavailable_reason}) - this will be dramatically "
            f"slower. This usually means torch/torchaudio got installed as the "
            f"wrong build for this GPU. Re-run install.js, or check whether a "
            f"later pip/uv install replaced the GPU-specific build with a "
            f"generic one from PyPI.",
            flush=True,
        )
    return "cpu"


def enable_rocm_optimizations():
    """Apply ROCm-specific optimizations. No-op on NVIDIA/CPU.

    1. FLASH_ATTENTION_TRITON_AMD_ENABLE: Lets qwen_tts whisper encoder
       use native flash attention via Triton AMD backend.
    2. MIOPEN_FIND_MODE=2: Forces MIOpen to use fast-find instead of
       exhaustive search, avoiding workspace allocation failures that
       cause fallback to slow GEMM algorithms.
    3. MIOPEN_LOG_LEVEL=4: Suppress noisy MIOpen workspace warnings.
    4. triton_key shim: Bridges older ROCm Triton builds' get_cache_key()
       to the triton_key() that PyTorch's inductor expects.
    """
    try:
        import torch
        if not (hasattr(torch.version, "hip") and torch.version.hip):
            return  # not ROCm
    except ImportError:
        return

    os.environ.setdefault("MIOPEN_FIND_MODE", "2")
    os.environ.setdefault("MIOPEN_LOG_LEVEL", "4")
    os.environ.setdefault("FLASH_ATTENTION_TRITON_AMD_ENABLE", "TRUE")

    try:
        from triton.compiler import compiler as triton_compiler
        if not hasattr(triton_compiler, "triton_key"):
            import triton
            triton_compiler.triton_key = lambda: f"triton-rocm-{triton.__version__}"
    except ImportError:
        pass
