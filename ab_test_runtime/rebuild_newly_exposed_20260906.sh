#!/usr/bin/env bash
# Can tight selection rescue the adapters the RE-GATE exposed?
#
# The 75-adapter re-gate on voice-verified clips (regate_vf__*.json, 68 of 75
# measured, 7 refused for pools too small) moved 54 of 68 scores DOWNWARD. The
# library was optimistic, not pessimistic: its scores came from each dataset's
# own val split, chosen before the same-voice guard existed.
#
# Four adapters the library called healthy fall below the 0.45 gate once
# measured that way, and none were in the earlier rebuild of the nine known
# failures. They are the untested half of that result:
#
#   breathy_mezzo_20s_f_scifi          0.586 -> 0.403
#   breathy_tenor_18s_m_supernatural   0.483 -> 0.360
#   warm_alto_40s_f_1                  0.479 -> 0.423
#   warm_tenor_20s_m                   0.657 -> 0.349   (the largest fall)
#
# WHY THE SAME PROCEDURE AS rebuild_failing_20260905.sh, NOT THE BETTER ONE.
# The half arm (100 clips) beat the tight arm by +0.0301 over 51 books, so it
# is the stronger recipe and it is deliberately NOT used here. These four are
# only interpretable NEXT TO the five already rebuilt, and a different recipe
# would confound rescue-vs-not with recipe-vs-recipe. Nine adapters under one
# procedure answers a question; five under one and four under another answers
# neither. If tight rescues these, the half arm is the obvious follow-up.
#
# ALL FOUR ARE FROM MULTI-VOICE BOOKS (char1/char2/char4). The per-pair voice
# guard filters WITHIN a book rather than rejecting it, so this is not
# disqualifying - but a pool may still come out too small, and then
# build_tight_dataset writes no arms.json and the run says SKIP rather than
# training on whatever was left. That refusal is the correct outcome, not a
# failure of the chain.
#
# THE COMPARISON IS PAIRED ON ONE HELD-OUT SET. Both the rebuilt adapter and
# the SHIPPED one are scored on the same clips, reserved before the training
# set was drawn, so neither trained on them. Comparing a fresh gate against a
# stored library score would repeat the rigged comparison that blocked goal
# 2.7 for weeks.
#
# Resumable by artifact. Never write "rc=$?" after a command substitution.
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
ZIPS="${ALEXANDRIA_ZIPS_DIR:-$HOME/Desktop/zips2}"
WORK="$R/ab_test_runtime/tight_rebuild_newly_exposed"
MAIN="$(git -C "$R" worktree list --porcelain 2>/dev/null | head -1 | awk '{print $2}')"
PY="$R/app/env/bin/python"; [ -x "$PY" ] || PY="$MAIN/app/env/bin/python"
[ -x "$PY" ] || { echo "no interpreter" >&2; exit 1; }
EMB="$R/dedup_analysis/embeddings_cache.pkl"
[ -s "$EMB" ] || EMB="$MAIN/dedup_analysis/embeddings_cache.pkl"
[ -s "$EMB" ] || { echo "no embedding cache" >&2; exit 1; }
export ALEXANDRIA_SIBLING_PYTHON="${ALEXANDRIA_SIBLING_PYTHON:-/home/fakemitch/pinokio/api/alexandria-audiobook.git/app/env/bin/python}"
[ -x "$ALEXANDRIA_SIBLING_PYTHON" ] || { echo "no speechbrain interpreter" >&2; exit 1; }
# TEST FOR THE ADAPTERS, NOT FOR THE DIRECTORY. A worktree HAS a lora_models
# directory - git tracks the manifest - but none of the 75 adapter_model
# .safetensors are tracked, so the directory exists and is empty. `-d` passed,
# the fallback never fired, and all five shipped arms reported MISSING while
# the run carried on. Count what is inside.
count_adapters () { find "$1" -name adapter_model.safetensors 2>/dev/null | head -1; }
LORA="$R/lora_models"
[ -n "$(count_adapters "$LORA")" ] || LORA="$MAIN/lora_models"
[ -n "$(count_adapters "$LORA")" ] || { echo "no shipped adapters found under $R or $MAIN" >&2; exit 1; }
echo "interpreter: $PY"; echo "shipped adapters: $LORA"

EPOCHS=6; LR=1e-06; RANK=64; ALPHA=128; ACC=8

gate () {
    local name="$1" adapter="$2" arm="$3" data="$4"
    local art="$R/ab_test_runtime/experiments/rebuild_newexp__${name}__${arm}.json"
    [ -s "$art" ] && { echo "SKIP $name/$arm"; return 0; }
    [ -f "$adapter/adapter_model.safetensors" ] || { echo "MISSING $name/$arm"; return 0; }
    "$PY" -u app/experiments/verify_adapter_identity.py --adapter "$adapter" \
        --dataset "$data" --lines 12 --out "$art" \
        > "/tmp/rbne_${name}_${arm}.log" 2>&1
    local rc=$?; local ts; ts="$(date -Is)"
    echo "[$ts] GATE $name/$arm rc=$rc"
    return 0
}

while IFS=$'\t' read -r name zip book; do
    [ -z "$name" ] && continue
    dir="$WORK/$name"
    if [ ! -f "$dir/arms.json" ]; then
        "$PY" -u app/experiments/build_tight_dataset.py \
            --trained-zip "$ZIPS/_deduped/$zip" --source-dir "$ZIPS/$book" \
            --embeddings "$EMB" --out "$dir" --seed 20260905 \
            > "/tmp/rbne_${name}_build.log" 2>&1
        brc=$?
        echo "BUILD $name rc=$brc"
    fi
    [ -f "$dir/arms.json" ] || { echo "SKIP $name - no arms"; continue; }
    out="$dir/tight/adapter"
    if [ ! -f "$out/adapter_model.safetensors" ]; then
        "$PY" -u app/train_lora.py --data_dir "$dir/tight" --output_dir "$out" \
            --epochs "$EPOCHS" --lr "$LR" --lora_r "$RANK" \
            --lora_alpha "$ALPHA" --gradient_accumulation_steps "$ACC" \
            > "/tmp/rbne_${name}_train.log" 2>&1
        trc=$?; ts="$(date -Is)"
        echo "[$ts] TRAIN $name rc=$trc"
    fi
    # Both arms, one held-out set: the rebuilt adapter and the shipped one.
    gate "$name" "$out"          rebuilt "$dir/tight"
    gate "$name" "$LORA/$name"   shipped "$dir/tight"
done <<'EOF'
breathy_mezzo_20s_f_scifi	narrator_luci_christian_full_metal_panic_char2_vol01.zip	Luci Christian Full Metal Panic-converted
breathy_tenor_18s_m_supernatural	narrator_keith_silverstein_kizumonogatari_char2_vol01.zip	Keith Silverstein KIZUMONOGATARI-converted
warm_alto_40s_f_1	narrator_miranda_parkin_my_happy_marriage_vol._1_char1_vol01.zip	Miranda Parkin My Happy Marriage, Vol. 1-converted
warm_tenor_20s_m	narrator_suzie_yeung_even_if_these_tears_disappear_tonight_char4_vol01.zip	Suzie Yeung Even If These Tears Disappear Tonight-converted
EOF
echo "ALL DONE $(date -Is)"
