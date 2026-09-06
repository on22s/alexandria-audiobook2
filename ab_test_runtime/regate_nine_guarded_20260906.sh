#!/usr/bin/env bash
# Re-measure the nine rebuilt adapters on holdouts that are actually one voice.
#
# WHY THIS RUN EXISTS. The nine-adapter comparison behind "rebuilding is
# baseline-dependent" was scored on build_tight_dataset val splits, and those
# were never checked for voice. They are worse than the re-gate holdouts:
# 22.2% of clips are a different person against 10.7%, 8 of 9 affected. One is
# catastrophic - husky_baritone_20s_m_supernatural is 17 of 20 foreign, median
# anchor cosine 0.006 - and that is precisely the adapter used as the headline
# example of "rebuilding harms a working voice", 0.548 -> 0.413. Both numbers
# are measured against someone else, so that -0.136 means nothing.
#
# WHAT SURVIVES AND WHAT DOES NOT. The correlation is not produced by the
# contaminated adapter: r=-0.843 over all nine, r=-0.819 (p=0.013) with it
# dropped. So "gain depends on baseline" holds. "Rebuilding damages a working
# voice" rests on the broken measurement and is what this run repairs.
#
# THE HOLDOUT IS REBUILT, NOT REUSED. build_unseen_holdout now verifies each
# candidate clip against anchors from the adapter's own trained zip (#489), so
# a fresh holdout is one voice by construction. Both arms - the SHIPPED adapter
# and the REBUILT one - are scored on that same fresh holdout, which is what
# makes the pair comparable; scoring one arm on new clips and reading the other
# from a stored number would repeat the rigged comparison that blocked goal 2.7
# for weeks.
#
# The gate reads --lines 12 of the 20 written. The pool is shuffled with a
# seed, so that is a random 12, and #489 pins the shuffle with a test.
#
# Resumable by artifact. Never write "rc=$?" after a command substitution.
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
ZIPS="${ALEXANDRIA_ZIPS_DIR:-$HOME/Desktop/zips2}"
WORK="$R/ab_test_runtime/guarded_holdout"
MAIN="$(git -C "$R" worktree list --porcelain 2>/dev/null | head -1 | awk '{print $2}')"
PY="$R/app/env/bin/python"; [ -x "$PY" ] || PY="$MAIN/app/env/bin/python"
[ -x "$PY" ] || { echo "no interpreter" >&2; exit 1; }
EMB="$R/dedup_analysis/embeddings_cache.pkl"
[ -s "$EMB" ] || EMB="$MAIN/dedup_analysis/embeddings_cache.pkl"
[ -s "$EMB" ] || { echo "no embedding cache" >&2; exit 1; }
export ALEXANDRIA_SIBLING_PYTHON="${ALEXANDRIA_SIBLING_PYTHON:-/home/fakemitch/pinokio/api/alexandria-audiobook.git/app/env/bin/python}"
[ -x "$ALEXANDRIA_SIBLING_PYTHON" ] || { echo "no speechbrain interpreter" >&2; exit 1; }
# Count adapters; a worktree HAS lora_models because git tracks the manifest.
has () { find "$1" -name adapter_model.safetensors 2>/dev/null | head -1; }
LORA="$R/lora_models"; [ -n "$(has "$LORA")" ] || LORA="$MAIN/lora_models"
[ -n "$(has "$LORA")" ] || { echo "no shipped adapters" >&2; exit 1; }
echo "shipped adapters: $LORA"

# The rebuilt adapters live in whichever tree built them.
find_rebuilt () {
    local n="$1" p
    for p in "$R/ab_test_runtime/tight_rebuild/$n/tight/adapter" \
             "$R/ab_test_runtime/tight_rebuild_newly_exposed/$n/tight/adapter" \
             "$MAIN/ab_test_runtime/tight_rebuild/$n/tight/adapter" \
             "$MAIN/ab_test_runtime/tight_rebuild_newly_exposed/$n/tight/adapter" \
             "/home/fakemitch/aa2-tight/ab_test_runtime/tight_rebuild/$n/tight/adapter"; do
        [ -f "$p/adapter_model.safetensors" ] && { echo "$p"; return 0; }
    done
    return 1
}

gate () {
    local name="$1" adapter="$2" arm="$3" data="$4"
    local art="$R/ab_test_runtime/experiments/guarded__${name}__${arm}.json"
    [ -s "$art" ] && { echo "SKIP $name/$arm"; return 0; }
    [ -f "$adapter/adapter_model.safetensors" ] || { echo "MISSING $name/$arm"; return 0; }
    "$PY" -u app/experiments/verify_adapter_identity.py --adapter "$adapter" \
        --dataset "$data" --lines 12 --out "$art" \
        > "/tmp/grd_${name}_${arm}.log" 2>&1
    local rc=$?; local ts; ts="$(date -Is)"
    echo "[$ts] GATE $name/$arm rc=$rc"
    return 0
}

while IFS=$'\t' read -r name zip book; do
    [ -z "$name" ] && continue
    dir="$WORK/$name"
    if [ ! -f "$dir/holdout.json" ]; then
        "$PY" -u app/experiments/build_unseen_holdout.py \
            --trained-zip "$ZIPS/_deduped/$zip" --source-dir "$ZIPS/$book" \
            --embeddings "$EMB" --out "$dir" --lines 20 \
            > "/tmp/grd_${name}_build.log" 2>&1
        brc=$?
        echo "BUILD $name rc=$brc"
    fi
    [ -f "$dir/holdout.json" ] || { echo "SKIP $name - no guarded holdout"; continue; }
    rej="$("$PY" -c "import json,sys;d=json.load(open(sys.argv[1]));print(d.get('clips_rejected_wrong_voice'))" "$dir/holdout.json" 2>/dev/null)"
    echo "  $name rejected_wrong_voice=$rej"
    reb="$(find_rebuilt "$name")" || { echo "SKIP $name - no rebuilt adapter"; continue; }
    gate "$name" "$LORA/$name" shipped "$dir"
    gate "$name" "$reb"        rebuilt "$dir"
done <<'EOF'
husky_baritone_20s_m_supernatural	narrator_keith_silverstein_kizumonogatari_char1_vol01.zip	Keith Silverstein KIZUMONOGATARI-converted
velvety_mezzo_30s_f_gothic	narrator_dracula_[audible_edition]_[b0078pa1oa]_char9_vol01.zip	Dracula [Audible Edition] [B0078PA1OA]
breathy_alto_50s_f_fantasy	narrator_mare_trevathan_the_godking's_legacy_char1_vol01.zip	Mare Trevathan The Godking's Legacy-converted
crisp_mezzo_30s_f	narrator_miranda_parkin_my_happy_marriage_vol._1_char2_vol01.zip	Miranda Parkin My Happy Marriage, Vol. 1-converted
warm_baritone_40s_m_1	narrator_dracula_[audible_edition]_[b0078pa1oa]_char8_vol01.zip	Dracula [Audible Edition] [B0078PA1OA]
breathy_tenor_18s_m_supernatural	narrator_keith_silverstein_kizumonogatari_char2_vol01.zip	Keith Silverstein KIZUMONOGATARI-converted
breathy_mezzo_20s_f_scifi	narrator_luci_christian_full_metal_panic_char2_vol01.zip	Luci Christian Full Metal Panic-converted
warm_alto_40s_f_1	narrator_miranda_parkin_my_happy_marriage_vol._1_char1_vol01.zip	Miranda Parkin My Happy Marriage, Vol. 1-converted
warm_tenor_20s_m	narrator_suzie_yeung_even_if_these_tears_disappear_tonight_char4_vol01.zip	Suzie Yeung Even If These Tears Disappear Tonight-converted
EOF
echo "ALL DONE $(date -Is)"
