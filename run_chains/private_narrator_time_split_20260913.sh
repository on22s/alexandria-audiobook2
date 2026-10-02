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
source "$REPO/run_chains/lib/queue.sh" || exit 1
if [ "${PYTHON+x}" = x ]; then
    python="$PYTHON"
    [ -x "$python" ] || { echo "REFUSING: configured PYTHON is not executable" >&2; exit 1; }
else
    python=$(resolve_python "$REPO") || { echo "REFUSING: no interpreter" >&2; exit 1; }
fi
if [ "${CONFIG+x}" = x ]; then
    config="$CONFIG"
    [ -f "$config" ] || { echo "REFUSING: configured CONFIG does not exist" >&2; exit 1; }
else
    config="$REPO/app/config.json"
    if [ ! -f "$config" ]; then
        main_checkout=$(git -C "$REPO" worktree list --porcelain | sed -n 's/^worktree //p' | head -n 1)
        config="$main_checkout/app/config.json"
    fi
    [ -f "$config" ] || { echo "REFUSING: no configuration" >&2; exit 1; }
fi
name=warm_baritone_30s_m_1
work="$runtime/time_split/$name"
STAGE_LOG_DIR="$runtime/logs/private_narrator_time_split_20260913"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh" || exit 1

[ -s "$work/data/train/metadata.jsonl" ] || { stage_note "REFUSING: no time split at $work/data - run library_time_split.py first"; exit 1; }

run_validated_cached_stage train 1h \
    "$python" "$REPO/app/experiments/ljspeech_completion.py" train "$work/adapter" --repo "$REPO" \
    -- --needs-vram -- \
    "$REPO/gpu_job.sh" "timesplit_${name}_train" \
    "$python" -u "$REPO/app/train_lora.py" \
    --data_dir "$work/data" --output_dir "$work/adapter" \
    --epochs 6 --lr 1e-6 --lora_r 64 --lora_alpha 128 --seed 1234

run_validated_cached_stage build 10m \
    "$python" "$REPO/app/experiments/ljspeech_completion.py" library_build "$work/build.json" --repo "$REPO" --dataset "$work/data" \
    -- --requires-ok train -- \
    "$python" -u "$REPO/app/experiments/library_eval_build.py" \
    --dataset "$work/data" --out "$work/build.json"

run_validated_cached_stage stop_gate 30m \
    "$python" "$REPO/app/experiments/stop_gate_completion.py" "$work/stop_check/verify_adapter_stops.json" \
    -- --needs-vram --requires-ok build -- \
    "$REPO/gpu_job.sh" "timesplit_${name}_stop_gate" \
    "$python" -u "$REPO/app/experiments/verify_adapter_stops.py" \
    --build "$work/build.json" --adapter "$work/adapter" --config "$config" \
    --lines 5 --seed 1234 --max-ratio 3.0 --out "$work/stop_check/verify_adapter_stops.json"

run_validated_cached_stage generate 1h \
    "$python" "$REPO/app/experiments/ljspeech_completion.py" generate "$runtime/experiments/time_split__${name}_generate.json" --repo "$REPO" --build "$work/build.json" \
    -- --needs-vram --requires-ok stop_gate -- \
    "$REPO/gpu_job.sh" "timesplit_${name}_generate" \
    "$python" -u "$REPO/app/experiments/ljspeech_generate.py" \
    --build "$work/build.json" --adapter "$work/adapter" --config "$config" \
    --out-dir "$work/generated" --limit 0 --arms lora clone --seed 1234 \
    --out "$runtime/experiments/time_split__${name}_generate.json"

run_stage prosody 30m --requires-ok generate -- \
    "$python" -u "$REPO/app/experiments/prosody_fidelity.py" \
    --generated "$runtime/experiments/time_split__${name}_generate.json" --limit 0 \
    --out "$runtime/experiments/prosody_time_split__${name}.json"

stage_commit_artifacts time_split "$REPO" "$runtime/experiments/time_split__${name}_generate.json" "$runtime/experiments/prosody_time_split__${name}.json"
stage_summary private_narrator_time_split_20260913
