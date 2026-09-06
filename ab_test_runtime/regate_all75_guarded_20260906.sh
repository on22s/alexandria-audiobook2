#!/usr/bin/env bash
# Re-gate all 75 shipped adapters on holdouts that are ONE VOICE.
#
# WHY AGAIN. The previous re-gate (regate_vf__*, 68 measured) used the
# volume-level guard, which keeps a volume whose centroid resembles the trained
# one - and a volume of a multi-voice book contains every character in it.
# Measured over those 68 holdouts and 1,360 clips: 10.7% of held-out clips are
# a different person, 28 of 68 holdouts carry at least one, char2-or-higher
# adapters average 22.3% against 5.9% for char1. Every conclusion in goal 2.7
# is currently written as provisional because of it.
#
# The clip-level guard (#489) verifies each candidate against anchors from the
# adapter's own trained zip and refuses when the speechbrain interpreter is
# missing rather than waving clips through. #493 refuses a SHORT holdout too,
# after one book that is ~90% another voice quietly produced six clips and the
# gate scored a median over 6 lines while every other adapter used 12.
#
# WHAT A REFUSAL MEANS HERE. Not a failure of the run: a book too contaminated
# to yield 12 verified clips cannot be measured, and saying so is the result.
# The old scheme's answer for those was a number.
#
# OVERSAMPLE 5, not the default 3. Contamination is known to reach 90% on at
# least one book, and verification is cheap (CPU, ~1 min) against a gate that
# is not. Drawing 100 candidates for 20 lines costs little and turns several
# likely refusals into measurements. It does NOT rescue a book with no clean
# clips, and nothing here pretends otherwise.
#
# Resumable by artifact. Never write "rc=$?" after a command substitution.
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
ZIPS="${ALEXANDRIA_ZIPS_DIR:-$HOME/Desktop/zips2}"
WORK="$R/ab_test_runtime/guarded_regate_all"
MAIN="$(git -C "$R" worktree list --porcelain 2>/dev/null | head -1 | awk '{print $2}')"
PY="$R/app/env/bin/python"; [ -x "$PY" ] || PY="$MAIN/app/env/bin/python"
[ -x "$PY" ] || { echo "no interpreter" >&2; exit 1; }
EMB="$R/dedup_analysis/embeddings_cache.pkl"
[ -s "$EMB" ] || EMB="$MAIN/dedup_analysis/embeddings_cache.pkl"
[ -s "$EMB" ] || { echo "no embedding cache" >&2; exit 1; }
export ALEXANDRIA_SIBLING_PYTHON="${ALEXANDRIA_SIBLING_PYTHON:-/home/fakemitch/pinokio/api/alexandria-audiobook.git/app/env/bin/python}"
[ -x "$ALEXANDRIA_SIBLING_PYTHON" ] || { echo "no speechbrain interpreter" >&2; exit 1; }
has () { find "$1" -name adapter_model.safetensors 2>/dev/null | head -1; }
LORA="$R/lora_models"; [ -n "$(has "$LORA")" ] || LORA="$MAIN/lora_models"
[ -n "$(has "$LORA")" ] || { echo "no shipped adapters" >&2; exit 1; }
LIST="$R/ab_test_runtime/regate_all75_list.tsv"
[ -s "$LIST" ] || { echo "no adapter list at $LIST" >&2; exit 1; }
echo "shipped adapters: $LORA"
echo "adapters listed: $(grep -c . "$LIST")"

built=0; refused=0; gated=0
while IFS=$'\t' read -r name zip book libscore; do
    [ -z "$name" ] && continue
    art="$R/ab_test_runtime/experiments/guarded_regate__${name}.json"
    [ -s "$art" ] && { echo "SKIP $name"; continue; }
    dir="$WORK/$name"
    if [ ! -f "$dir/holdout.json" ]; then
        "$PY" -u app/experiments/build_unseen_holdout.py \
            --trained-zip "$ZIPS/_deduped/$zip" --source-dir "$ZIPS/$book" \
            --embeddings "$EMB" --out "$dir" --lines 20 --oversample 5 \
            > "/tmp/gra_${name}_build.log" 2>&1
        brc=$?
        if [ ! -f "$dir/holdout.json" ]; then
            refused=$((refused+1))
            echo "REFUSED $name rc=$brc - $(tail -1 "/tmp/gra_${name}_build.log" | cut -c1-100)"
            continue
        fi
        built=$((built+1))
    fi
    rej="$("$PY" -c "import json,sys;d=json.load(open(sys.argv[1]));print('%s rejected, %s written' % (d.get('clips_rejected_wrong_voice'), d.get('written')))" "$dir/holdout.json" 2>/dev/null)"
    echo "  $name  $rej"
    "$PY" -u app/experiments/verify_adapter_identity.py \
        --adapter "$LORA/$name" --dataset "$dir" --lines 12 --out "$art" \
        > "/tmp/gra_${name}_gate.log" 2>&1
    rc=$?; gated=$((gated+1)); ts="$(date -Is)"
    echo "[$ts] GATE $name rc=$rc (library $libscore)"
done < "$LIST"
echo "built=$built refused=$refused gated=$gated"
echo "ALL DONE $(date -Is)"
