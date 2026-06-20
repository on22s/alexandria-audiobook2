"""Tiny, dependency-free device-resolution helpers shared between app/tts.py
(the FastAPI server's TTSEngine) and app/train_lora.py (an independent
subprocess that runs in a separate ROCm-enabled venv without app/env's other
dependencies). torch is imported lazily inside each function - never at
module level - so this file can be imported by either environment regardless
of whether torch is installed there yet.
"""
import os


def resolve_device(device_str):
    """Resolve 'auto' to the best available torch device: cuda > mps > cpu."""
    if device_str != "auto":
        return device_str
    try:
        import torch
        if torch.cuda.is_available():
            return "cuda"
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return "mps"
    except ImportError:
        pass
    return "cpu"


def enable_rocm_optimizations():
    """Apply ROCm-specific optimizations. No-op on NVIDIA/CPU.

    1. FLASH_ATTENTION_TRITON_AMD_ENABLE: Lets qwen_tts whisper encoder
       use native flash attention via Triton AMD backend.
    2. MIOPEN_FIND_MODE=2: Forces MIOpen to use fast-find instead of
       exhaustive search, avoiding workspace allocation failures that
       cause fallback to slow GEMM algorithms.
    3. MIOPEN_LOG_LEVEL=4: Suppress noisy MIOpen workspace warnings.
    4. triton_key shim: Bridges pytorch-triton-rocm's get_cache_key()
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
            triton_compiler.triton_key = lambda: f"pytorch-triton-rocm-{triton.__version__}"
    except ImportError:
        pass
