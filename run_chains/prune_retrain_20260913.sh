#!/usr/bin/bash
# Does Chalamandaris-style pruning rescue a REBUILD-class adapter?
#
# The 2026-08-07 library audit found five adapters whose datasets are not one
# voice (dataset_tone_spread r=0.58 against fidelity), and retraining them on
# the same data reproduces the same average-of-several-people. Chalamandaris
# et al. (LREC 2014) pruned audiobook phrases by (F0 mean, F0 std) Mahalanobis
# distance and by alignment score before training, and the two rules together
# beat the unpruned voice (p < 0.001 on paragraphs). prune_prosodic_clips.py is
# that rule pair; this trains the SAME dataset twice with the library recipe -
# original and pruned - and scores both with the audit's own fidelity scorer,
# so the comparison is paired and the control is a fresh retrain, not the
# shipped adapter.
#
# Prediction, written first: pruning raises ECAPA fidelity for a dataset whose
# mixture is prosodic (a narrator doing character voices) and does nothing for
# one whose mixture is different people (diarization failure) - the paper's
# rule cannot tell two similar-pitched speakers apart.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
source "$REPO/run_chains/lib/queue.sh" || exit 1
if [ -n "${PYTHON:-}" ]; then
    python="$PYTHON"
    [ -x "$python" ] || { echo "REFUSING: configured PYTHON is not executable" >&2; exit 1; }
else
    python=$(resolve_python "$REPO") || { echo "REFUSING: no interpreter" >&2; exit 1; }
fi
ADAPTER="${ADAPTER:-breathy_alto_50s_f_fantasy}"
STAGE_LOG_DIR="$runtime/logs/prune_retrain_20260913"
work="$runtime/prune_retrain_20260913/$ADAPTER"
mkdir -p "$STAGE_LOG_DIR" "$work/zips" "$work/models" "$work/datasets"
source "$REPO/run_chains/lib/stage.sh" || exit 1

ZIP="$("$python" - "$ADAPTER" "$REPO/lora_models/manifest.json" <<'EOF'
import json, sys
d = json.load(open(sys.argv[2]))
print([x["zip_source"] for x in d if x["id"] == sys.argv[1]][0])
EOF
)" || { stage_note "REFUSING: cannot resolve source zip for $ADAPTER"; exit 1; }
[ -s "$ZIP" ] || { stage_note "REFUSING: no source zip for $ADAPTER"; exit 1; }
base="$(basename "$ZIP" .zip)"

if [ -s "$work/pruned/prune_report.json" ]; then
    STAGE_TOTAL=$((STAGE_TOTAL + 1))
    record_stage_result prune 0
    stage_note "SKIP prune (cached report)"
else
    run_stage prune 2h -- \
        "$python" -u "$REPO/app/experiments/prune_prosodic_clips.py" \
        --zip "$ZIP" --out "$work/pruned" \
        --whisper-cpp-bin "$REPO/whisper.cpp/build/bin/whisper-cli" \
        --whisper-cpp-model "$REPO/whisper.cpp/models/ggml-base.en.bin"
fi

# Two zips in one folder: the original (control retrain) and the pruned copy.
run_stage package_control 0 -- bash -c \
    '[ -s "$2" ] || cp -- "$1" "$2"' package "$ZIP" "$work/zips/${base}_control.zip"
run_stage package_pruned 0 --requires-ok prune -- bash -c \
    '[ -s "$2" ] || { cd -- "$1" && zip -q -r "$2" metadata.jsonl train val; }' \
    package "$work/pruned" "$work/zips/${base}_pruned.zip"

run_stage train 3h --needs-vram --requires-ok package_control --requires-ok package_pruned -- \
    "$REPO/gpu_job.sh" "prune_retrain_${ADAPTER}" \
    "$python" -u "$REPO/tools/voice_lab/batch_train_lora.py" \
    --zips_dir "$work/zips" --datasets_dir "$work/datasets" \
    --models_dir "$work/models" --manifest "$work/models/manifest.json" \
    --python "$python" --keep_datasets

run_stage fidelity 2h --needs-vram --requires-ok train -- \
    "$REPO/gpu_job.sh" "prune_fidelity_${ADAPTER}" \
    "$python" -u "$REPO/app/experiments/library_voice_fidelity.py" \
    --models "$work/models" --zips "$work/zips" --lines 20 --ecapa-python "$python" \
    --work "$work/fidelity_work" \
    --out "$runtime/experiments/prune_retrain__${ADAPTER}__fidelity.json"
stage_commit_artifacts prune_retrain "$REPO" "$runtime/experiments/prune_retrain__${ADAPTER}__fidelity.json"
stage_summary prune_retrain_20260913
