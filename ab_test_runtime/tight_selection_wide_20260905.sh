#!/usr/bin/env bash
# The tight-vs-random selection experiment, across every eligible dataset.
#
# Breadth first: the seed loop is OUTER, so one pass covers every book before
# any book gets a second seed. If the night runs out, what exists is a wide
# result rather than a deep one on the first few - and this project's standing
# weakness is that findings rest on too few books.
#
# Resumable: an existing gate artifact is skipped. Never write "rc=$?" after a
# command substitution - $(date) resets $? (see the repo lint).
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
ZIPS="${ALEXANDRIA_ZIPS_DIR:-$HOME/Desktop/zips2}"
WORK="$R/ab_test_runtime/tight_selection"
LIST="$R/ab_test_runtime/tight_selection_books.json"

MAIN="$(git -C "$R" worktree list --porcelain 2>/dev/null | head -1 | awk '{print $2}')"
PY="$R/app/env/bin/python"; [ -x "$PY" ] || PY="$MAIN/app/env/bin/python"
[ -x "$PY" ] || { echo "no interpreter" >&2; exit 1; }
EMB="$R/dedup_analysis/embeddings_cache.pkl"
[ -s "$EMB" ] || EMB="$MAIN/dedup_analysis/embeddings_cache.pkl"
[ -s "$EMB" ] || { echo "no embedding cache" >&2; exit 1; }
# The ECAPA scorer runs under the sibling repo's interpreter, and the gate
# derives that path from its OWN location - which from a worktree points at a
# directory that does not exist, so every gate reported NOT MEASURED. Name it.
export ALEXANDRIA_SIBLING_PYTHON="${ALEXANDRIA_SIBLING_PYTHON:-/home/fakemitch/pinokio/api/alexandria-audiobook.git/app/env/bin/python}"
[ -x "$ALEXANDRIA_SIBLING_PYTHON" ] || {
    echo "no speechbrain interpreter at $ALEXANDRIA_SIBLING_PYTHON" >&2; exit 1; }
echo "interpreter: $PY"; echo "embeddings: $EMB"
echo "ecapa: $ALEXANDRIA_SIBLING_PYTHON"

EPOCHS=6; LR=1e-06; RANK=64; ALPHA=128; ACC=8

train_and_gate() {
    local tag="$1" arm="$2"
    local data="$WORK/$tag/$arm" out="$WORK/$tag/$arm/adapter"
    local art="$R/ab_test_runtime/experiments/tight_gate__${tag}__${arm}.json"
    [ -s "$art" ] && { echo "SKIP $tag/$arm"; return 0; }
    if [ ! -f "$out/adapter_model.safetensors" ]; then
        "$PY" -u app/train_lora.py --data_dir "$data" --output_dir "$out" \
            --epochs "$EPOCHS" --lr "$LR" --lora_r "$RANK" \
            --lora_alpha "$ALPHA" --gradient_accumulation_steps "$ACC" \
            > "/tmp/tw_${tag}_${arm}_train.log" 2>&1
        local trc=$?; local ts; ts="$(date -Is)"
        echo "[$ts] TRAIN $tag/$arm rc=$trc"
        [ -f "$out/adapter_model.safetensors" ] || { echo "  no adapter"; return 0; }
    fi
    "$PY" -u app/experiments/verify_adapter_identity.py --adapter "$out" \
        --dataset "$data" --lines 20 --out "$art" \
        > "/tmp/tw_${tag}_${arm}_gate.log" 2>&1
    local grc=$?; local gs; gs="$(date -Is)"
    echo "[$gs] GATE  $tag/$arm rc=$grc"
    return 0
}

mapfile -t ENTRIES < <("$PY" -c "
import json,sys
d=json.load(open('$LIST'))
for b in d['books']:
    print(b['dataset']+'\t'+b['book'])")
echo "datasets: ${#ENTRIES[@]}"

for seed in 20260905 20260906; do
    for entry in "${ENTRIES[@]}"; do
        zip="${entry%%$'\t'*}"; book="${entry##*$'\t'}"
        tag="$(printf '%s' "${zip%.zip}" | tr -c 'A-Za-z0-9' '_' | cut -c1-60)_s${seed}"
        if [ ! -f "$WORK/$tag/arms.json" ]; then
            "$PY" -u app/experiments/build_tight_dataset.py \
                --trained-zip "$ZIPS/_deduped/$zip" --source-dir "$ZIPS/$book" \
                --embeddings "$EMB" --out "$WORK/$tag" --seed "$seed" \
                > "/tmp/tw_${tag}_build.log" 2>&1
            brc=$?
            echo "BUILD $tag rc=$brc"
        fi
        [ -f "$WORK/$tag/arms.json" ] || { echo "SKIP $tag - no arms"; continue; }
        train_and_gate "$tag" control
        train_and_gate "$tag" tight
    done
done
echo "ALL DONE"
