#!/usr/bin/env bash
# Can tight selection rescue the shipped adapters that are actually broken?
#
# Nine shipped adapters score below the 0.45 usability gate. The selection
# result predicts a large gain for exactly these: over 54 books the gain went
# +0.100 on weak baselines against +0.010 on strong ones (r=-0.598), and three
# of these were confirmed broken over 13 and 21 independent seeds rather than
# unlucky draws.
#
# FIVE OF THE NINE ARE ELIGIBLE. The other four come from cast productions -
# Dracula's nine narrators, Waking Gods' twelve - where the per-pair voice
# guard leaves too small a same-voice pool. Two Dracula characters DO qualify
# here, because the guard filters within the book rather than rejecting it.
#
# THE COMPARISON IS PAIRED ON ONE HELD-OUT SET. Both the rebuilt adapter and
# the SHIPPED one are scored on the same clips, reserved before the training
# set was drawn, so neither trained on them. Comparing a fresh gate against a
# stored library score would repeat the rigged comparison that blocked goal
# 2.7 for weeks: those numbers were measured on clips the shipped adapter had
# memorised.
#
# Resumable by artifact. Never write "rc=$?" after a command substitution.
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
ZIPS="${ALEXANDRIA_ZIPS_DIR:-$HOME/Desktop/zips2}"
WORK="$R/ab_test_runtime/tight_rebuild"
MAIN="$(git -C "$R" worktree list --porcelain 2>/dev/null | head -1 | awk '{print $2}')"
PY="$R/app/env/bin/python"; [ -x "$PY" ] || PY="$MAIN/app/env/bin/python"
[ -x "$PY" ] || { echo "no interpreter" >&2; exit 1; }
EMB="$R/dedup_analysis/embeddings_cache.pkl"
[ -s "$EMB" ] || EMB="$MAIN/dedup_analysis/embeddings_cache.pkl"
[ -s "$EMB" ] || { echo "no embedding cache" >&2; exit 1; }
export ALEXANDRIA_SIBLING_PYTHON="${ALEXANDRIA_SIBLING_PYTHON:-/home/fakemitch/pinokio/api/alexandria-audiobook.git/app/env/bin/python}"
[ -x "$ALEXANDRIA_SIBLING_PYTHON" ] || { echo "no speechbrain interpreter" >&2; exit 1; }
LORA="$R/lora_models"; [ -d "$LORA" ] || LORA="$MAIN/lora_models"
[ -d "$LORA" ] || { echo "no shipped adapters at lora_models" >&2; exit 1; }
echo "interpreter: $PY"; echo "shipped adapters: $LORA"

EPOCHS=6; LR=1e-06; RANK=64; ALPHA=128; ACC=8

gate () {
    local name="$1" adapter="$2" arm="$3" data="$4"
    local art="$R/ab_test_runtime/experiments/tight_rebuild__${name}__${arm}.json"
    [ -s "$art" ] && { echo "SKIP $name/$arm"; return 0; }
    [ -f "$adapter/adapter_model.safetensors" ] || { echo "MISSING $name/$arm"; return 0; }
    "$PY" -u app/experiments/verify_adapter_identity.py --adapter "$adapter" \
        --dataset "$data" --lines 12 --out "$art" \
        > "/tmp/rb_${name}_${arm}.log" 2>&1
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
            > "/tmp/rb_${name}_build.log" 2>&1
        brc=$?
        echo "BUILD $name rc=$brc"
    fi
    [ -f "$dir/arms.json" ] || { echo "SKIP $name - no arms"; continue; }
    out="$dir/tight/adapter"
    if [ ! -f "$out/adapter_model.safetensors" ]; then
        "$PY" -u app/train_lora.py --data_dir "$dir/tight" --output_dir "$out" \
            --epochs "$EPOCHS" --lr "$LR" --lora_r "$RANK" \
            --lora_alpha "$ALPHA" --gradient_accumulation_steps "$ACC" \
            > "/tmp/rb_${name}_train.log" 2>&1
        trc=$?; ts="$(date -Is)"
        echo "[$ts] TRAIN $name rc=$trc"
    fi
    # Both arms, one held-out set: the rebuilt adapter and the shipped one.
    gate "$name" "$out"          rebuilt "$dir/tight"
    gate "$name" "$LORA/$name"   shipped "$dir/tight"
done <<'EOF'
velvety_mezzo_30s_f_gothic	narrator_dracula_[audible_edition]_[b0078pa1oa]_char9_vol01.zip	Dracula [Audible Edition] [B0078PA1OA]
husky_baritone_20s_m_supernatural	narrator_keith_silverstein_kizumonogatari_char1_vol01.zip	Keith Silverstein KIZUMONOGATARI-converted
warm_baritone_40s_m_1	narrator_dracula_[audible_edition]_[b0078pa1oa]_char8_vol01.zip	Dracula [Audible Edition] [B0078PA1OA]
breathy_alto_50s_f_fantasy	narrator_mare_trevathan_the_godking's_legacy_char1_vol01.zip	Mare Trevathan The Godking's Legacy-converted
crisp_mezzo_30s_f	narrator_miranda_parkin_my_happy_marriage_vol._1_char2_vol01.zip	Miranda Parkin My Happy Marriage, Vol. 1-converted
EOF
echo "ALL DONE $(date -Is)"
