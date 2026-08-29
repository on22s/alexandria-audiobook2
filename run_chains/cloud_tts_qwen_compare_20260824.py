#!/usr/bin/env python3
import json
import platform
import subprocess
import time
from pathlib import Path

import soundfile as sf
import torch
from qwen_tts import Qwen3TTSModel

ROOT = Path("/home/ubuntu/alexandria-goals-be3e7ea")
OUT = Path("/home/ubuntu/tts_comparison_20260824/qwen3_tts")
OUT.mkdir(parents=True, exist_ok=True)
build = json.loads((ROOT / "ab_test_runtime/reference_spread/build_spread3.json").read_text())
reference = ROOT / build["ref_sample"]
target = (
    "The rain had stopped before dawn, leaving the narrow streets bright and silver. "
    "At the end of the lane, a single lamp still burned beside the old library door."
)

torch.manual_seed(20260824)
started = time.time()
model = Qwen3TTSModel.from_pretrained(
    "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
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
result = {
    "model": "Qwen/Qwen3-TTS-12Hz-1.7B-Base",
    "backend": "qwen-tts",
    "reference_audio": str(reference),
    "reference_text": build["ref_text"],
    "target_text": target,
    "seed": 20260824,
    "sample_rate": sample_rate,
    "audio_seconds": duration,
    "load_seconds": loaded - started,
    "generation_seconds": finished - loaded,
    "rtf": (finished - loaded) / duration,
    "gpu": subprocess.check_output(
        ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
        text=True,
    ).strip(),
    "python": platform.python_version(),
    "torch": torch.__version__,
}
(OUT / "result.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
