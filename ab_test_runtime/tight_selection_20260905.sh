#!/usr/bin/env bash
# Does choosing tighter clips make a better voice? Train both arms and gate them.
#
# Both arms come from one pool at the same size and differ only in selection:
# 200 random against the 200 nearest the pool centroid. They share one held-out
# set reserved before either arm was drawn, so neither trained on it.
#
# Resumable: an existing gate artifact is skipped. Never write "rc=$?" after a
# command substitution - $(date) resets $? (see the repo lint).
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
# A worktree has no app/env of its own. Fall back to the main checkout's,
# the way ready.sh does, so this runs from either.
PY="$R/app/env/bin/python"
if [ ! -x "$PY" ]; then
    PY="$(git -C "$R" worktree list --porcelain 2>/dev/null | head -1 | awk '{print $2}')/app/env/bin/python"
fi
if [ ! -x "$PY" ]; then
    echo "no interpreter found (looked in $R/app/env and the main checkout)" >&2
    exit 1
fi
echo "interpreter: $PY"

# The dedup embedding cache is a 150MB untracked artifact that lives only in
# the main checkout, so a worktree run has to reach for it there - the same
# problem as app/env above, and it cost one queued run that failed loudly and
# skipped rather than training on nothing.
EMB="$R/dedup_analysis/embeddings_cache.pkl"
if [ ! -s "$EMB" ]; then
    EMB="$(git -C "$R" worktree list --porcelain 2>/dev/null | head -1 | awk '{print $2}')/dedup_analysis/embeddings_cache.pkl"
fi
if [ ! -s "$EMB" ]; then
    echo "no embedding cache found; the same-voice filter cannot run" >&2
    exit 1
fi
echo "embeddings: $EMB"
ZIPS="${ALEXANDRIA_ZIPS_DIR:-$HOME/Desktop/zips2}"
WORK="$R/ab_test_runtime/tight_selection"

# The shipped library's settings, so these adapters are comparable to it.
EPOCHS=6; LR=1e-06; RANK=64; ALPHA=128; ACC=8

train_and_gate() {
    local tag="$1" arm="$2"
    local data="$WORK/$tag/$arm"
    local out="$data/adapter"
    local art="$R/ab_test_runtime/experiments/tight_gate__${tag}__${arm}.json"
    if [ -s "$art" ]; then
        echo "SKIP $tag/$arm"
        return 0
    fi
    if [ ! -f "$out/adapter_model.safetensors" ]; then
        "$PY" -u app/train_lora.py --data_dir "$data" --output_dir "$out" \
            --epochs "$EPOCHS" --lr "$LR" --lora_r "$RANK" \
            --lora_alpha "$ALPHA" --gradient_accumulation_steps "$ACC" \
            > "/tmp/tight_${tag}_${arm}_train.log" 2>&1
        local trc=$?
        local ts; ts="$(date -Is)"
        echo "[$ts] TRAIN $tag/$arm rc=$trc"
        if [ ! -f "$out/adapter_model.safetensors" ]; then
            echo "  no adapter produced; skipping gate"
            return 0
        fi
    fi
    "$PY" -u app/experiments/verify_adapter_identity.py \
        --adapter "$out" --dataset "$data" --lines 20 --out "$art" \
        > "/tmp/tight_${tag}_${arm}_gate.log" 2>&1
    local grc=$?
    local gs; gs="$(date -Is)"
    echo "[$gs] GATE  $tag/$arm rc=$grc"
    return 0
}

while IFS='|' read -r tag zip book; do
    [ -z "$tag" ] && continue
    for seed in 20260905 20260906; do
        st="${tag}_s${seed}"
        if [ ! -f "$WORK/$st/arms.json" ]; then
            "$PY" -u app/experiments/build_tight_dataset.py \
                --trained-zip "$ZIPS/_deduped/$zip" \
                --source-dir "$ZIPS/$book" \
                --embeddings "$EMB" \
                --out "$WORK/$st" --seed "$seed" \
                > "/tmp/tight_${st}_build.log" 2>&1
            local_rc=$?
            echo "BUILD $st rc=$local_rc"
        fi
        [ -f "$WORK/$st/arms.json" ] || { echo "SKIP $st - no arms"; continue; }
        train_and_gate "$st" control
        train_and_gate "$st" tight
    done
done <<'EOF'
gardens|narrator_ralph_lister_gardens_of_the_moon_char1_vol01.zip|Ralph Lister Gardens of the Moon-converted
nightfall|narrator_jon_lindstrom_nightfall_and_other_stories_[059341635x]_char1_vol01.zip|Jon Lindstrom Nightfall and Other Stories [059341635X]
altered_carbon|narrator_todd_mclaren_altered_carbon_[b002v1o6x8]_char1_vol01.zip|Todd McLaren Altered Carbon [B002V1O6X8]
water_moon|narrator_cindy_kay_water_moon:_a_novel_[b0d26l1r1d]_char1_vol01.zip|Cindy Kay Water Moon: A Novel [B0D26L1R1D]
wolverine|narrator_qarie_marshall_wolverine:_road_of_bones_[1662042051]_char1_vol01.zip|Qarie Marshall Wolverine: Road of Bones [1662042051]
EOF
echo "ALL DONE"
