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
python="${PYTHON:-$REPO/app/env/bin/python}"
[ -x "$python" ] || python="/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/env/bin/python"
ADAPTER="${ADAPTER:-breathy_alto_50s_f_fantasy}"
STAGE_LOG_DIR="$runtime/logs/prune_retrain_20260913"
work="$runtime/prune_retrain_20260913/$ADAPTER"
mkdir -p "$STAGE_LOG_DIR" "$work/zips" "$work/models" "$work/datasets"
source "$REPO/run_chains/lib/stage.sh"

MAIN=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
ZIP="$("$python" - "$ADAPTER" <<'EOF'
import json, sys
d = json.load(open("/home/fakemitch/pinokio/api/alexandria-audiobook2.git/lora_models/manifest.json"))
print([x["zip_source"] for x in d if x["id"] == sys.argv[1]][0])
EOF
)"
[ -s "$ZIP" ] || { stage_note "REFUSING: no source zip for $ADAPTER"; exit 1; }
base="$(basename "$ZIP" .zip)"

[ -s "$work/pruned/prune_report.json" ] || \
run_stage prune 2h -- \
    "$python" -u "$REPO/app/experiments/prune_prosodic_clips.py" \
    --zip "$ZIP" --out "$work/pruned" \
    --whisper-cpp-bin "$MAIN/whisper.cpp/build/bin/whisper-cli" \
    --whisper-cpp-model "$MAIN/whisper.cpp/models/ggml-base.en.bin"

# Two zips in one folder: the original (control retrain) and the pruned copy.
[ -s "$work/zips/${base}_control.zip" ] || cp "$ZIP" "$work/zips/${base}_control.zip"
[ -s "$work/zips/${base}_pruned.zip" ] || \
    ( cd "$work/pruned" && zip -q -r "$work/zips/${base}_pruned.zip" train val )

[ -s "$work/models/manifest.json" ] || \
run_stage train 3h --needs-vram -- \
    "$REPO/gpu_job.sh" "prune_retrain_${ADAPTER}" \
    "$python" -u "$REPO/batch_train_lora.py" \
    --zips_dir "$work/zips" --datasets_dir "$work/datasets" \
    --models_dir "$work/models" --manifest "$work/models/manifest.json" \
    --python "$python" --keep_datasets

run_stage fidelity 2h --needs-vram -- \
    "$REPO/gpu_job.sh" "prune_fidelity_${ADAPTER}" \
    "$python" -u "$REPO/app/experiments/library_voice_fidelity.py" \
    --models "$work/models" --zips "$work/zips" --lines 20 \
    --work "$work/fidelity_work" \
    --out "$runtime/experiments/prune_retrain__${ADAPTER}__fidelity.json"
stage_commit_artifacts prune_retrain "$REPO"
stage_summary prune_retrain_20260913
