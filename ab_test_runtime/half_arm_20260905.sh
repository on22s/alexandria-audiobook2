#!/usr/bin/env bash
# The third point on the curve: threshold or monotonic?
#
# The two-arm result says selection works - tight beat random on 34 of 52
# books, mean +0.056, Wilcoxon p=0.0007 - but HOW MUCH tighter the arm was does
# not predict how much it won by (r=-0.011, p=0.94). Two mechanisms fit that,
# and they differ in what you would actually do:
#
#   threshold   middle ~= tight > control  -> drop the worst clips, stop there
#   monotonic   tight > middle > control   -> tighten as hard as you can
#
# Only seed 20260905. A third arm answers a new question; a second seed adds
# repeats to one already answered.
#
# Resumable: an existing gate artifact is skipped. Never write "rc=$?" after a
# command substitution - $(date) resets $? (see the repo lint).
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
ZIPS="${ALEXANDRIA_ZIPS_DIR:-$HOME/Desktop/zips2}"
WORK="$R/ab_test_runtime/tight_selection"
LIST="$R/ab_test_runtime/tight_selection_books.json"
SEED=20260905

MAIN="$(git -C "$R" worktree list --porcelain 2>/dev/null | head -1 | awk '{print $2}')"
PY="$R/app/env/bin/python"; [ -x "$PY" ] || PY="$MAIN/app/env/bin/python"
[ -x "$PY" ] || { echo "no interpreter" >&2; exit 1; }
EMB="$R/dedup_analysis/embeddings_cache.pkl"
[ -s "$EMB" ] || EMB="$MAIN/dedup_analysis/embeddings_cache.pkl"
[ -s "$EMB" ] || { echo "no embedding cache" >&2; exit 1; }
export ALEXANDRIA_SIBLING_PYTHON="${ALEXANDRIA_SIBLING_PYTHON:-/home/fakemitch/pinokio/api/alexandria-audiobook.git/app/env/bin/python}"
[ -x "$ALEXANDRIA_SIBLING_PYTHON" ] || { echo "no speechbrain interpreter" >&2; exit 1; }
echo "interpreter: $PY"; echo "ecapa: $ALEXANDRIA_SIBLING_PYTHON"

EPOCHS=6; LR=1e-06; RANK=64; ALPHA=128; ACC=8

mapfile -t ENTRIES < <("$PY" -c "
import json
d=json.load(open('$LIST'))
for b in d['books']:
    print(b['dataset']+'\t'+b['book'])")
echo "datasets: ${#ENTRIES[@]}"

for entry in "${ENTRIES[@]}"; do
    zip="${entry%%$'\t'*}"; book="${entry##*$'\t'}"
    tag="$(printf '%s' "${zip%.zip}" | tr -c 'A-Za-z0-9' '_' | cut -c1-60)_s${SEED}"
    art="$R/ab_test_runtime/experiments/tight_gate__${tag}__half.json"
    [ -s "$art" ] && { echo "SKIP $tag"; continue; }
    # The middle arm is only meaningful beside the two it sits between.
    if [ ! -s "$R/ab_test_runtime/experiments/tight_gate__${tag}__tight.json" ]; then
        echo "SKIP $tag - no tight arm to place a middle between yet"
        continue
    fi
    if [ ! -d "$WORK/$tag/half" ]; then
        "$PY" -u app/experiments/build_half_arm.py \
            --trained-zip "$ZIPS/_deduped/$zip" --source-dir "$ZIPS/$book" \
            --embeddings "$EMB" --out "$WORK/$tag" --seed "$SEED" \
            > "/tmp/half_${tag}_build.log" 2>&1
        brc=$?
        echo "BUILD $tag rc=$brc"
    fi
    [ -d "$WORK/$tag/half" ] || { echo "SKIP $tag - no half arm"; continue; }
    out="$WORK/$tag/half/adapter"
    if [ ! -f "$out/adapter_model.safetensors" ]; then
        "$PY" -u app/train_lora.py --data_dir "$WORK/$tag/half" \
            --output_dir "$out" --epochs "$EPOCHS" --lr "$LR" --lora_r "$RANK" \
            --lora_alpha "$ALPHA" --gradient_accumulation_steps "$ACC" \
            > "/tmp/half_${tag}_train.log" 2>&1
        trc=$?; ts="$(date -Is)"
        echo "[$ts] TRAIN $tag/half rc=$trc"
        [ -f "$out/adapter_model.safetensors" ] || { echo "  no adapter"; continue; }
    fi
    "$PY" -u app/experiments/verify_adapter_identity.py --adapter "$out" \
        --dataset "$WORK/$tag/half" --lines 6 --out "$art" \
        > "/tmp/half_${tag}_gate.log" 2>&1
    grc=$?; gs="$(date -Is)"
    echo "[$gs] GATE  $tag/half rc=$grc"
done
echo "ALL DONE"
