#!/usr/bin/bash
# Goal 2.9: does the eight-narrator English result survive a split with no
# adjacent sentences across it?
#
# The private narrators scored ~0.15 higher on f0 correlation than either
# public English set. Their val split is clip-level random, so val and train
# hold adjacent sentences of one paragraph (see library_time_split.py). This
# retrains ONE narrator - warm_baritone_30s_m_1, LoRA 0.600 / clone 0.690
# medians on the random split - on a time-ordered split with a >=2 min gap,
# with the library recipe (6 epochs, lr 1e-6, r 64, alpha 128; 150 clips
# rather than 180 because that is what fits before the gap), and scores the
# 20 held-out lines with the same instrument as everything else in 2.9.
#
# It cannot separate session from adjacency: the zip covers 31 minutes of one
# recording. A drop toward 0.3 says adjacency; no drop says session or real.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="${PYTHON:-$REPO/app/env/bin/python}"
[ -x "$python" ] || python="/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python"
config="${CONFIG:-$REPO/app/config.json}"
[ -f "$config" ] || config="/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/config.json"
name=warm_baritone_30s_m_1
work="$runtime/time_split/$name"
STAGE_LOG_DIR="$runtime/logs/private_narrator_time_split_20260913"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"

[ -s "$work/data/train/metadata.jsonl" ] || { stage_note "REFUSING: no time split at $work/data - run library_time_split.py first"; exit 1; }

[ -s "$work/adapter/adapter_model.safetensors" ] || \
run_stage train 1h --needs-vram -- \
    "$REPO/gpu_job.sh" "timesplit_${name}_train" \
    "$python" -u "$REPO/app/train_lora.py" \
    --data_dir "$work/data" --output_dir "$work/adapter" \
    --epochs 6 --lr 1e-6 --lora_r 64 --lora_alpha 128 --seed 1234

[ -s "$work/build.json" ] || \
run_stage build 10m -- \
    "$python" -u "$REPO/app/experiments/library_eval_build.py" \
    --dataset "$work/data" --out "$work/build.json"

[ -s "$work/stop_check/verify_adapter_stops.json" ] || \
run_stage stop_gate 30m --needs-vram -- \
    "$REPO/gpu_job.sh" "timesplit_${name}_stop_gate" \
    "$python" -u "$REPO/app/experiments/verify_adapter_stops.py" \
    --build "$work/build.json" --adapter "$work/adapter" --config "$config" \
    --lines 5 --seed 1234 --max-ratio 3.0 --out "$work/stop_check/verify_adapter_stops.json"

[ -s "$runtime/experiments/time_split__${name}_generate.json" ] || \
run_stage generate 1h --needs-vram -- \
    "$REPO/gpu_job.sh" "timesplit_${name}_generate" \
    "$python" -u "$REPO/app/experiments/ljspeech_generate.py" \
    --build "$work/build.json" --adapter "$work/adapter" --config "$config" \
    --out-dir "$work/generated" --limit 0 --arms lora clone --seed 1234 \
    --out "$runtime/experiments/time_split__${name}_generate.json"

run_stage prosody 30m -- \
    "$python" -u "$REPO/app/experiments/prosody_fidelity.py" \
    --generated "$runtime/experiments/time_split__${name}_generate.json" --limit 0 \
    --out "$runtime/experiments/prosody_time_split__${name}.json"

stage_commit_artifacts time_split "$REPO"
stage_summary private_narrator_time_split_20260913
