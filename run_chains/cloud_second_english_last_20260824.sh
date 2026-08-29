#!/usr/bin/bash
set -uo pipefail

REPO="/home/ubuntu/alexandria-goals-be3e7ea"
PY="/home/ubuntu/alexandria-tts-env/bin/python"
NAME="silky_baritone_30s_m"
ROOT="$REPO/ab_test_runtime/retrain_honest/$NAME"
WORK="$REPO/ab_test_runtime/second_english_eval/$NAME"
BUILD="$WORK/build.json"
OUT="$REPO/ab_test_runtime/experiments/second_english__${NAME}_generate.json"
SCORE="$REPO/ab_test_runtime/experiments/prosody_second_english__${NAME}.json"

mkdir -p "$WORK" "$(dirname "$OUT")"
for path in "$ROOT/data" "$ROOT/adapter/adapter_model.safetensors"; do
    [ -e "$path" ] || { echo "missing required input: $path" >&2; exit 1; }
done

cd "$REPO"
export PYTHONNOUSERSITE=1
"$PY" -u app/experiments/library_eval_build.py \
    --dataset "$ROOT/data" --out "$BUILD"
"$PY" -u app/experiments/ljspeech_generate.py \
    --build "$BUILD" --adapter "$ROOT/adapter" \
    --out-dir "$WORK/audio" --limit 0 --arms lora clone --out "$OUT"
"$PY" -u app/experiments/prosody_fidelity.py \
    --generated "$OUT" --limit 0 --out "$SCORE"
