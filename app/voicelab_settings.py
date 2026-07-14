"""Pure path resolution shared by Voice Lab's router and profiler script."""

import hashlib
import os
import re


def get_profiler_paths(root_dir: str, data_dir: str | None = None) -> dict[str, str]:
    """Return checkout-local defaults for the Voice Lab profiling stage."""
    root = os.path.abspath(root_dir)
    data = os.path.abspath(data_dir or root)
    models_dir = os.path.join(data, "lora_models")
    return {
        "manifest": os.path.join(models_dir, "manifest.json"),
        "model": os.path.join(root, "Qwen2.5-14B-Instruct-Q6_K.gguf"),
        "output_csv": os.path.join(models_dir, "voice_profiles.csv"),
    }


def get_deduped_zip_name(narrator: str, zip_name: str) -> str:
    """Return a readable, collision-resistant flat filename for one narrator ZIP."""
    stem, extension = os.path.splitext(zip_name)
    safe_narrator = re.sub(r"[^A-Za-z0-9._-]+", "_", narrator).strip("._") or "narrator"
    safe_stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "dataset"
    identity = hashlib.sha256(f"{narrator}\0{zip_name}".encode("utf-8")).hexdigest()[:10]
    return f"{safe_narrator}__{safe_stem}__{identity}{extension.lower()}"
