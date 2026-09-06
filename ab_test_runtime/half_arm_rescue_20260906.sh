#!/usr/bin/env bash
# Does the STRONGER recipe rescue the three genuinely-broken adapters?
#
# Nine adapters have now been rebuilt with the tight arm and gated against
# their shipped versions on one held-out set. Pooled, that is a null result -
# mean +0.053, Wilcoxon p=0.65 - but the null is two real effects cancelling:
#
#     gain vs shipped baseline    r = -0.843   p = 0.0043   n=9
#
# which replicates, stronger, the r=-0.598 the selection experiment measured
# over 54 books. Rebuilding HELPS a genuinely broken adapter and HARMS a
# working one (husky_baritone_20s_m_supernatural went 0.548 -> 0.413 and fell
# below the gate). So only the broken end is worth another arm:
#
#   velvety_mezzo_30s_f_gothic   shipped 0.057 -> tight 0.363
#   warm_baritone_40s_m_1        shipped 0.171 -> tight 0.475
#   breathy_alto_50s_f_fantasy   shipped 0.291 -> tight 0.277   (tight failed)
#
# THE HALF ARM IS THE STRONGER RECIPE: 100 well-chosen clips beat the 200 the
# tight arm uses by +0.0301 over 51 books (36/51, p=0.00032). It was
# deliberately NOT used for the nine, because mixing recipes would have
# confounded rescue-vs-not with recipe-vs-recipe. That comparison is now done,
# so this is the follow-up it earned.
#
# THE POOL IS REUSED, NOT REDRAWN. These work directories are the ones the
# tight arm was built from, copied intact from the worktree the first batch ran
# in. build_half_arm can reproduce a draw from a seed, but reusing the actual
# clips makes the held-out set identical BY CONSTRUCTION rather than by an
# argument about determinism - and the gate below reads its clips from
# `$dir/tight`, the very directory the shipped and tight arms were scored on.
# Three arms, one held-out set, no seed argument to trust.
#
# AND IT IS CHECKED ANYWAY. A held-out clip that leaks into the half arm's
# training set would inflate exactly the number this run exists to produce, so
# the chain compares the two file lists and REFUSES that adapter if they
# intersect. Rule 21: the instrument is checked before it is believed.
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
echo "interpreter: $PY"

EPOCHS=6; LR=1e-06; RANK=64; ALPHA=128; ACC=8
N=100; FULL_N=200; HELD=20; SEED=20260905

# Refuse the adapter if any held-out clip is in the half arm's training set.
#
# KEYED ON (source_volume, text), NOT ON THE FILENAME. A first version compared
# basenames and was silently incapable of ever firing: every split renumbers
# from zero, so held-out clips are val_0000.wav.. while training clips are
# train_0000.wav.., and the same clip in both splits carries two different
# names. It reported CLEAN on a file compared against itself. It is now checked
# against three cases with known answers before being trusted - train/val
# CLEAN, val/val LEAK 20 of 20, control-train/val CLEAN.
leaks () {
    "$PY" - "$1" "$2" <<'PYEOF'
import json, sys
def keys(p):
    out = set()
    with open(p, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            text = (row.get("text") or "").strip()
            if text:
                out.add((row.get("source_volume") or "", text))
    return out
train, val = keys(sys.argv[1]), keys(sys.argv[2])
if not train or not val:
    print("UNREADABLE")
    raise SystemExit(2)
both = train & val
print("LEAK %d of %d" % (len(both), len(val)) if both else "CLEAN")
raise SystemExit(1 if both else 0)
PYEOF
}

while IFS=$'\t' read -r name zip book; do
    [ -z "$name" ] && continue
    dir="$WORK/$name"
    art="$R/ab_test_runtime/experiments/half_rescue__${name}__half.json"
    [ -s "$art" ] && { echo "SKIP $name"; continue; }
    [ -f "$dir/arms.json" ] || { echo "SKIP $name - no pool at $dir"; continue; }
    [ -s "$dir/tight/val/metadata.jsonl" ] || { echo "SKIP $name - no held-out"; continue; }

    if [ ! -s "$dir/half/train/metadata.jsonl" ]; then
        "$PY" -u app/experiments/build_half_arm.py \
            --trained-zip "$ZIPS/_deduped/$zip" --source-dir "$ZIPS/$book" \
            --embeddings "$EMB" --out "$dir" --n "$N" --full-n "$FULL_N" \
            --held-out "$HELD" --seed "$SEED" \
            > "/tmp/half3_${name}_build.log" 2>&1
        brc=$?
        echo "BUILD $name rc=$brc"
    fi
    [ -s "$dir/half/train/metadata.jsonl" ] || { echo "SKIP $name - no half arm"; continue; }

    verdict="$(leaks "$dir/half/train/metadata.jsonl" "$dir/tight/val/metadata.jsonl")"
    lrc=$?
    echo "LEAKCHECK $name $verdict"
    [ "$lrc" -eq 0 ] || { echo "REFUSED $name - held-out is not disjoint"; continue; }

    out="$dir/half/adapter"
    if [ ! -f "$out/adapter_model.safetensors" ]; then
        "$PY" -u app/train_lora.py --data_dir "$dir/half" --output_dir "$out" \
            --epochs "$EPOCHS" --lr "$LR" --lora_r "$RANK" \
            --lora_alpha "$ALPHA" --gradient_accumulation_steps "$ACC" \
            > "/tmp/half3_${name}_train.log" 2>&1
        trc=$?; ts="$(date -Is)"
        echo "[$ts] TRAIN $name rc=$trc"
    fi
    [ -f "$out/adapter_model.safetensors" ] || { echo "SKIP $name - no adapter"; continue; }

    # Scored on $dir/tight - the SAME directory the shipped and tight arms were
    # scored on, so all three numbers share one held-out set.
    "$PY" -u app/experiments/verify_adapter_identity.py --adapter "$out" \
        --dataset "$dir/tight" --lines 12 --out "$art" \
        > "/tmp/half3_${name}_gate.log" 2>&1
    rc=$?; ts="$(date -Is)"
    echo "[$ts] GATE $name/half rc=$rc"
done <<'EOF'
velvety_mezzo_30s_f_gothic	narrator_dracula_[audible_edition]_[b0078pa1oa]_char9_vol01.zip	Dracula [Audible Edition] [B0078PA1OA]
warm_baritone_40s_m_1	narrator_dracula_[audible_edition]_[b0078pa1oa]_char8_vol01.zip	Dracula [Audible Edition] [B0078PA1OA]
breathy_alto_50s_f_fantasy	narrator_mare_trevathan_the_godking's_legacy_char1_vol01.zip	Mare Trevathan The Godking's Legacy-converted
EOF
echo "ALL DONE $(date -Is)"
