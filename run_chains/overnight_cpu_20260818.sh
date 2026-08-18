#!/usr/bin/bash
# CPU work that runs ALONGSIDE the overnight GPU queue, not after it.
#
# Only gpu_job.sh stages take the lock, so scoring stored artifacts, running
# the unit suite and writing reports all proceed while the card is busy.
#
# IT MUST NOT DIRTY THE WORKING TREE. gpu_job.sh refuses to start a job when
# tracked files are modified, so a CPU stage that rewrites a committed index
# would silently kill the GPU queue running beside it. Everything here either
# reads, or writes a NEW file under reports/ - untracked .md and .json are not
# dirt to that gate, by design. Index refreshes are left for the morning, on
# purpose.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
OUT="$runtime/reports/overnight_20260818"
LOG="$runtime/logs/overnight_20260818"
mkdir -p "$OUT" "$LOG"

note() { echo "[$(date -u +%FT%TZ)] $*"; }

# ---------------------------------------------------------------- e-row arms
#
# The -e arm is in and loses to -eh four-fold (2.0% vs 7.9% whole-word
# recovery, McNemar p=1.9e-4, with the plain control non-significant at
# p=0.39). ay and ei decide whether the -eh ROW is the problem at all: if all
# three alternatives lose, the 6.6%/18.0% penalty needs another explanation
# and the row is exonerated.
#
# Each arm is scored the moment its artifact appears rather than at the end,
# so a crash at 3am still leaves the arms that finished.
score_arms() {
    local deadline=$((SECONDS + 14400))
    local pending="e ay ei"
    while [ -n "$pending" ] && [ "$SECONDS" -lt "$deadline" ]; do
        local still=""
        for arm in $pending; do
            local art="$runtime/experiments/respelling_e_row__${arm}.json"
            if [ -f "$art" ] && ! pgrep -f "e-spelling $arm " >/dev/null 2>&1; then
                note "scoring arm $arm"
                "$python" "$REPO/app/experiments/pair_e_row.py" "$art" \
                    >> "$OUT/e_row_paired.txt" 2>&1 \
                    || note "arm $arm scoring failed"
            else
                still="$still $arm"
            fi
        done
        pending="${still# }"
        [ -n "$pending" ] && sleep 300
    done
    [ -n "$pending" ] && note "arms never appeared:$pending"
}

note "CPU OVERNIGHT START"

score_arms

# ------------------------------------------------------- suite and verifier
# Read-only, and the thing most likely to catch a stage having broken
# something while nobody was watching.
note "unit suite + release verifier"
( cd "$REPO/app" && "$python" verify_release.py \
    --json-report "$OUT/release-report.json" ) > "$LOG/verify.log" 2>&1 \
    && note "verifier OK" || note "verifier FAILED (see $LOG/verify.log)"

# -------------------------------------------------------- evidence snapshot
# What the morning needs to know: which goals still cite nothing, and which
# artifacts remain unreplayable. Written to reports/, never to the index.
#
# --out is NOT optional here. Its default target,
# ab_test_runtime/experiments/goal_evidence_audit.json, is a TRACKED file:
# rewriting it dirties the working tree, and gpu_job.sh would then refuse
# every remaining stage of the GPU queue running beside this script. Checked
# before running it, not after.
note "evidence snapshot"
"$python" "$REPO/app/experiments/goal_evidence_audit.py" \
    --out "$OUT/goal_evidence_audit.json" \
    > "$OUT/goal_evidence.txt" 2>&1 || note "goal audit failed"

note "CPU OVERNIGHT COMPLETE"
