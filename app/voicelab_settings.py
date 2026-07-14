"""Pure path resolution shared by Voice Lab's router and profiler script."""

import os


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
