#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/ubuntu/alexandria-goals-be3e7ea
BASE=/home/ubuntu/tts_comparison_20260824
SRC="$BASE/src/chatterbox"
ENV="$BASE/envs/chatterbox"
OUT="$BASE/chatterbox_v3"
mkdir -p "$BASE/src" "$BASE/envs" "$OUT"

if [[ ! -d "$SRC/.git" ]]; then
    git clone --depth 1 https://github.com/resemble-ai/chatterbox.git "$SRC"
fi
git -C "$SRC" rev-parse HEAD > "$OUT/source_commit.txt"
if [[ ! -x "$ENV/bin/python" ]]; then
    uv venv --python 3.11 "$ENV"
fi
uv pip install --python "$ENV/bin/python" "$SRC"
"$ENV/bin/python" -m pip freeze > "$OUT/pip_freeze.txt"

ROOT="$ROOT" OUT="$OUT" "$ENV/bin/python" - <<'PY'
import json
import os
import platform
import subprocess
import time
from pathlib import Path

import torch
import torchaudio as ta
from chatterbox.mtl_tts import ChatterboxMultilingualTTS

root = Path(os.environ["ROOT"])
out = Path(os.environ["OUT"])
build = json.loads((root / "ab_test_runtime/reference_spread/build_spread3.json").read_text())
reference = root / build["ref_sample"]
target = (
    "The rain had stopped before dawn, leaving the narrow streets bright and silver. "
    "At the end of the lane, a single lamp still burned beside the old library door."
)
torch.manual_seed(20260824)
started = time.time()
model = ChatterboxMultilingualTTS.from_pretrained(device="cuda", t3_model="v3")
loaded = time.time()
wav = model.generate(target, language_id="en", audio_prompt_path=str(reference))
finished = time.time()
audio_path = out / "audiobook_passage.wav"
ta.save(str(audio_path), wav.cpu(), model.sr)
duration = wav.shape[-1] / model.sr
result = {
    "model": "Chatterbox-Multilingual-V3",
    "backend": "official resemble-ai/chatterbox source",
    "reference_audio": str(reference),
    "reference_text": build["ref_text"],
    "target_text": target,
    "seed": 20260824,
    "sample_rate": model.sr,
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
(out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
PY
