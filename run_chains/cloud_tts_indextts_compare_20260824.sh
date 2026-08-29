#!/usr/bin/env bash
set -euo pipefail

ROOT=/home/ubuntu/alexandria-goals-be3e7ea
BASE=/home/ubuntu/tts_comparison_20260824
SRC="$BASE/src/index-tts"
OUT="$BASE/indextts2"
MODEL="$BASE/models/IndexTTS-2"
mkdir -p "$BASE/src" "$BASE/models" "$OUT"

if [[ ! -d "$SRC/.git" ]]; then
    git clone --depth 1 https://github.com/index-tts/index-tts.git "$SRC"
fi
git -C "$SRC" rev-parse HEAD > "$OUT/source_commit.txt"
cd "$SRC"
uv sync --all-extras
uv run indextts2 download --source huggingface --model-dir "$MODEL"
uv run indextts2 check --model-dir "$MODEL" --device cuda:0 | tee "$OUT/check.txt"
uv pip freeze > "$OUT/pip_freeze.txt"

REFERENCE="$ROOT/ab_test_runtime/reference_spread/ref_spread3.wav"
TEXT="The rain had stopped before dawn, leaving the narrow streets bright and silver. At the end of the lane, a single lamp still burned beside the old library door."
START=$(date +%s.%N)
uv run indextts2 synth \
    --model-dir "$MODEL" \
    --device cuda:0 \
    --fp16 \
    --text "$TEXT" \
    --voice "$REFERENCE" \
    --output "$OUT/audiobook_passage.wav" \
    --force
END=$(date +%s.%N)
ROOT="$ROOT" OUT="$OUT" START="$START" END="$END" TEXT="$TEXT" python3 - <<'PY'
import json
import os
import subprocess
import wave
from pathlib import Path

root = Path(os.environ["ROOT"])
out = Path(os.environ["OUT"])
build = json.loads((root / "ab_test_runtime/reference_spread/build_spread3.json").read_text())
with wave.open(str(out / "audiobook_passage.wav"), "rb") as wav:
    duration = wav.getnframes() / wav.getframerate()
    sample_rate = wav.getframerate()
elapsed = float(os.environ["END"]) - float(os.environ["START"])
result = {
    "model": "IndexTeam/IndexTTS-2",
    "backend": "official index-tts CLI",
    "reference_audio": str(root / build["ref_sample"]),
    "reference_text": build["ref_text"],
    "target_text": os.environ["TEXT"],
    "seed": None,
    "seed_note": "The official CLI exposes no seed option; defaults were retained.",
    "sample_rate": sample_rate,
    "audio_seconds": duration,
    "generation_seconds_including_model_load": elapsed,
    "rtf_including_model_load": elapsed / duration,
    "gpu": subprocess.check_output(
        ["nvidia-smi", "--query-gpu=name,driver_version", "--format=csv,noheader"],
        text=True,
    ).strip(),
}
(out / "result.json").write_text(json.dumps(result, indent=2) + "\n")
print(json.dumps(result, indent=2))
PY
