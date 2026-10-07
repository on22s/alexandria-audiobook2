#!/usr/bin/env python3
import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel

# Ran on the cloud instance from /home/ubuntu/alexandria-goals-be3e7ea,
# writing to /home/ubuntu/tts_comparison_20260824/qwen3_tts. Both are that
# machine's paths, so take them from the environment as the sibling shell
# chains already do, and default to this checkout.
ROOT = Path(os.environ.get("ROOT") or Path(__file__).resolve().parent.parent)
OUT = Path(os.environ.get("OUT") or ROOT / "ab_test_runtime/tts_comparison/qwen3_tts")
OUT.mkdir(parents=True, exist_ok=True)
build = json.loads((ROOT / "ab_test_runtime/reference_spread/build_spread3.json").read_text())
reference = ROOT / build["ref_sample"]
target = (
    "The rain had stopped before dawn, leaving the narrow streets bright and silver. "
    "At the end of the lane, a single lamp still burned beside the old library door."
)

if os.environ.get("ALEXANDRIA_GPU_LOCK_HELD") != "1":
    os.execv(
        str(Path(__file__).resolve().parent.parent / "gpu_job.sh"),
        ["gpu_job.sh", "cloud_tts_qwen_compare_20260824", sys.executable,
         str(Path(__file__).resolve())],
    )

owner_check = subprocess.run(
    ["bash", str(Path(__file__).resolve().parent.parent / "gpu_job.sh"),
     "--check-lock-owner", os.environ.get("ALEXANDRIA_GPU_LOCK_PID", "")],
    check=False, timeout=30,
)
if owner_check.returncode:
    raise SystemExit("Cannot verify inherited GPU lock ownership")

from cloud_comparison_provenance import (ensure_comparison_model_snapshot,
                                         get_comparison_package_versions)
package_versions = get_comparison_package_versions(
    ["qwen-tts"], {"qwen-tts": os.environ.get("QWEN_TTS_VERSION")})
torch.manual_seed(20260824)
started = time.time()
model_identity = ensure_comparison_model_snapshot(
    "Qwen/Qwen3-TTS-12Hz-1.7B-Base", os.environ.get("QWEN_MODEL_REVISION"))

model = Qwen3TTSModel.from_pretrained(
    model_identity["snapshot_path"],
    device_map="cuda:0",
    dtype=torch.bfloat16,
    attn_implementation="sdpa",
)
loaded = time.time()
wavs, sample_rate = model.generate_voice_clone(
    text=target,
    language="English",
    ref_audio=str(reference),
    ref_text=build["ref_text"],
)
finished = time.time()
audio_path = OUT / "audiobook_passage.wav"
sf.write(audio_path, wavs[0], sample_rate)
duration = len(wavs[0]) / sample_rate
try:
    gpu = subprocess.check_output(
        ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
        text=True, timeout=5,
    ).strip()
    if not gpu:
        raise ValueError("GPU metadata probe returned empty output")
except (OSError, subprocess.SubprocessError, ValueError) as error:
    gpu = None
    gpu_metadata = {"status": "unavailable", "error": f"{type(error).__name__}: {error}"}
else:
    gpu_metadata = {"status": "measured"}
result = {
    "model": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    "backend": "qwen-tts",
    "model_identity": model_identity,
    "package_versions": package_versions,
    "reference_audio": str(reference),
    "reference_text": build["ref_text"],
    "target_text": target,
    "seed": 20260824,
    "sample_rate": sample_rate,
    "audio_seconds": duration,
    "load_seconds": loaded - started,
    "generation_seconds": finished - loaded,
    "rtf": (finished - loaded) / duration,
    "gpu": gpu,
    "gpu_metadata": gpu_metadata,
    "python": platform.python_version(),
    "torch": torch.__version__,
}
(OUT / "result.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
