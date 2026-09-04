#!/usr/bin/env bash
# Score the shipped (contaminated) adapter and its clean retrain on clips
# NEITHER has seen. Goal 2.7's blocked comparison, made fair.
#
# The holdout sets are rebuilt here rather than committed: they are 20 wavs per
# adapter and the builder is deterministic given its seed, so the manifest
# (holdout.json) is the artifact worth keeping and the audio is regenerated.
#
# Resumable: an existing artifact is skipped, so an interrupted run continues.
# Never write "rc=$?" after a command substitution - $(date) resets $? and the
# failure is recorded as a success (see the repo lint).
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
PY="$R/app/env/bin/python"
ZIPS="${ALEXANDRIA_ZIPS_DIR:-$HOME/Desktop/zips2}"
LINES=20

build_holdout() {
    local name="$1" zip="$2" book="$3"
    local dir="$R/ab_test_runtime/unseen_holdout/$name"
    if [ -s "$dir/val/metadata.jsonl" ]; then
        return 0
    fi
    "$PY" -u app/experiments/build_unseen_holdout.py \
        --trained-zip "$ZIPS/_deduped/$zip" \
        --source-dir "$ZIPS/$book" \
        --out "$dir" --lines "$LINES"
}

run_one() {
    local name="$1" adapter="$2" arm="$3"
    local out="$R/ab_test_runtime/experiments/unseen_gate__${name}__${arm}.json"
    if [ -s "$out" ]; then
        echo "SKIP $name/$arm"
        return 0
    fi
    if [ ! -f "$adapter/adapter_model.safetensors" ]; then
        echo "MISSING $name/$arm -> $adapter"
        return 1
    fi
    "$PY" -u app/experiments/verify_adapter_identity.py \
        --adapter "$adapter" \
        --dataset "$R/ab_test_runtime/unseen_holdout/$name" \
        --lines "$LINES" --out "$out" \
        > "/tmp/unseen_${name}_${arm}.log" 2>&1
    local rc=$?
    local stamp
    stamp="$(date -Is)"
    echo "[$stamp] DONE $name/$arm rc=$rc"
    return 0
}

while IFS='|' read -r name clean zip book; do
    [ -z "$name" ] && continue
    if ! build_holdout "$name" "$zip" "$book"; then
        echo "SKIP $name - holdout refused"
        continue
    fi
    run_one "$name" "$R/lora_models/$name" shipped
    run_one "$name" "$R/$clean" clean
done <<'EOF'
breathy_tenor_50s_m_fantasy|ab_test_runtime/decontaminate/batch1/breathy_tenor_50s_m_fantasy/adapter|narrator_ralph_lister_gardens_of_the_moon_char1_vol01.zip|Ralph Lister Gardens of the Moon-converted
husky_tenor_30s_m_literary|ab_test_runtime/retrain_honest/husky_tenor_30s_m_literary/adapter|narrator_cindy_kay_water_moon:_a_novel_[b0d26l1r1d]_char1_vol01.zip|Cindy Kay Water Moon: A Novel [B0D26L1R1D]
husky_tenor_30s_m|ab_test_runtime/retrain_honest/husky_tenor_30s_m/adapter|narrator_various_waking_gods_[b01ngublbw]_char1_vol01.zip|Various Waking Gods [B01NGUBLBW]
silky_baritone_40s_m_scifi|ab_test_runtime/decontaminate/batch3/silky_baritone_40s_m_scifi/adapter|narrator_todd_mclaren_altered_carbon_[b002v1o6x8]_char1_vol01.zip|Todd McLaren Altered Carbon [B002V1O6X8]
breathy_baritone_40s_m_military_2|ab_test_runtime/decontaminate/batch1/breathy_baritone_40s_m_military_2/adapter|narrator_qarie_marshall_wolverine:_road_of_bones_[1662042051]_char1_vol01.zip|Qarie Marshall Wolverine: Road of Bones [1662042051]
husky_baritone_40s_m_scifi|ab_test_runtime/decontaminate/batch2/husky_baritone_40s_m_scifi/adapter|narrator_jon_lindstrom_nightfall_and_other_stories_[059341635x]_char1_vol01.zip|Jon Lindstrom Nightfall and Other Stories [059341635X]
EOF
echo "ALL DONE"
