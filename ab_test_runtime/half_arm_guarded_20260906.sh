#!/usr/bin/env bash
# The half arm, scored on holdouts that are actually one voice.
#
# The half-arm result (100 clips beating 200 by +0.0359 on three adapters) was
# scored on build_tight_dataset val splits, which are 22.2% a different person.
# Its NUMBER was never re-measured, but its PREMISE fell: the three adapters
# were chosen as "genuinely broken" and, on guarded holdouts,
# warm_baritone_40s_m_1 reads 0.670 shipped - it was never broken at all, and
# rebuilding it was a -0.316 loss rather than a +0.304 rescue.
#
# THIS IS THREE GATES, NOT A REBUILD. The guarded holdouts already exist from
# regate_nine_guarded_20260906, and the half adapters already exist from
# half_arm_rescue_20260906. Scoring the half arm against the SAME holdout the
# shipped and tight arms were scored on completes a three-way comparison on one
# set of clips - which is the only way the half-vs-tight difference means
# anything. Rebuilding any part of it would break that pairing for no gain.
#
# WHAT THIS CANNOT ANSWER. Three adapters, chosen for a property two of them
# turned out not to have. Whatever half-vs-tight reads here is a measurement on
# three arbitrary adapters, not on "broken" ones, and carries no p-value worth
# quoting. Reading it as a replication of the +0.0301 over 51 books would
# repeat the mistake this whole sequence has been correcting.
#
# Resumable by artifact. Never write "rc=$?" after a command substitution.
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
MAIN="$(git -C "$R" worktree list --porcelain 2>/dev/null | head -1 | awk '{print $2}')"
PY="$R/app/env/bin/python"; [ -x "$PY" ] || PY="$MAIN/app/env/bin/python"
[ -x "$PY" ] || { echo "no interpreter" >&2; exit 1; }
export ALEXANDRIA_SIBLING_PYTHON="${ALEXANDRIA_SIBLING_PYTHON:-/home/fakemitch/pinokio/api/alexandria-audiobook.git/app/env/bin/python}"
[ -x "$ALEXANDRIA_SIBLING_PYTHON" ] || { echo "no speechbrain interpreter" >&2; exit 1; }

find_half () {
    local n="$1" p
    for p in "$R/ab_test_runtime/tight_rebuild/$n/half/adapter" \
             "$MAIN/ab_test_runtime/tight_rebuild/$n/half/adapter" \
             "/home/fakemitch/aa2-tight/ab_test_runtime/tight_rebuild/$n/half/adapter"; do
        [ -f "$p/adapter_model.safetensors" ] && { echo "$p"; return 0; }
    done
    return 1
}

for name in velvety_mezzo_30s_f_gothic warm_baritone_40s_m_1 breathy_alto_50s_f_fantasy; do
    art="$R/ab_test_runtime/experiments/guarded__${name}__half.json"
    [ -s "$art" ] && { echo "SKIP $name"; continue; }
    dir="$R/ab_test_runtime/guarded_holdout/$name"
    [ -f "$dir/holdout.json" ] || { echo "SKIP $name - no guarded holdout"; continue; }
    half="$(find_half "$name")" || { echo "SKIP $name - no half adapter"; continue; }
    "$PY" -u app/experiments/verify_adapter_identity.py --adapter "$half" \
        --dataset "$dir" --lines 12 --out "$art" \
        > "/tmp/hg_${name}.log" 2>&1
    rc=$?; ts="$(date -Is)"
    echo "[$ts] GATE $name/half rc=$rc"
done
echo "ALL DONE $(date -Is)"
