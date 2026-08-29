#!/usr/bin/bash
set -uo pipefail

REPO="/home/ubuntu/alexandria-goals-be3e7ea"
PY="/home/ubuntu/alexandria-tts-env/bin/python"
RUNTIME="$REPO/ab_test_runtime"
WORK="$RUNTIME/reference_spread"
ADAPTER="$RUNTIME/ljspeech_eval/adapter"
export PYTHONNOUSERSITE=1

cd "$REPO"
for arm in 1 2 3; do
    build="$WORK/build_spread${arm}.json"
    gen="$RUNTIME/experiments/reference_spread__en_generate_arm${arm}.json"
    score="$RUNTIME/experiments/reference_spread__en_score_arm${arm}.json"
    [ -f "$build" ] || { echo "missing build: $build" >&2; exit 1; }
    "$PY" -u app/experiments/ljspeech_generate.py \
        --build "$build" --adapter "$ADAPTER" --out-dir "$WORK/arm${arm}" \
        --arms clone --limit 0 --out "$gen"
    "$PY" -u app/experiments/ljspeech_score.py \
        --generated "$gen" --limit 0 --out "$score"
done

"$PY" -u app/experiments/reference_spread_compare.py \
    --spread "$RUNTIME/experiments/reference_spread__en.json" \
    --score \
      "0=$RUNTIME/experiments/reference_spread__en_score_arm0.json" \
      "1=$RUNTIME/experiments/reference_spread__en_score_arm1.json" \
      "2=$RUNTIME/experiments/reference_spread__en_score_arm2.json" \
      "3=$RUNTIME/experiments/reference_spread__en_score_arm3.json" \
    --out "$RUNTIME/experiments/reference_spread__en_compare.json"
