#!/usr/bin/bash
# Does the -ay advantage hold on terms it has never seen?
#
# WHY THIS EXISTS. -ay beat the shipped -eh roughly two to one on whole-word
# recovery (15.3% against 7.9%, McNemar p=5.2e-4) with the plain control
# non-significant, on 391 terms. That is the strongest respelling result the
# project has, and it is one arm on one sample - and the plain control in that
# same run showed 34 of 391 verdicts flipping on IDENTICAL input, so this
# pipeline's noise floor is not small.
#
# HOW IT REPLICATES. measure_respellings takes terms in book-count order, so
# --limit 800 is the original 400 plus 400 terms the arm has never measured.
# Reusing the existing --work directory means the first 400 clips are skipped
# rather than regenerated: each block costs one fresh block, not a whole rerun.
# Score only the NEW terms against the same -eh baseline and the comparison is
# a genuine held-out replication rather than a rerun of the same sample.
#
# WHY IT WAITS. It runs after the overnight queue rather than beside it - one
# GPU, and gpu_job.sh would serialise it anyway; waiting keeps the queue log
# readable. It stops at the deadline instead of starting a block it cannot
# finish, because a half-measured block is not a smaller result, it is a
# biased one: terms are ordered, so a truncated block is the commonest terms
# only.
set -uo pipefail

# ARTIFACT EXISTS IS NOT ARTIFACT FINISHED. A run killed mid-way leaves a file
# that looks complete - respelling_e_row__ay_n1200.json sits in this repository
# at 1129 of 1200 terms - and a chain skipping on existence would skip it
# forever, on a subset biased toward the commonest items. Ask the artifact.
artifact_complete() {
    "$1" - "$2" <<'PYEOF' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
if d.get("status") == "complete":
    sys.exit(0)
if d.get("status") == "partial":
    sys.exit(1)
r, c = d.get("results"), d.get("candidates_considered")
sys.exit(0 if isinstance(r, list) and isinstance(c, int) and len(r) >= c else 1)
PYEOF
}

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
LOG="$runtime/logs/overnight_20260818"
mkdir -p "$LOG"
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"

# 16:30Z = 11:30am CDT, half an hour before the machine is wanted back.
DEADLINE="${E_ROW_DEADLINE:-1787070600}"
BLOCK_SECONDS=4200   # ~55 min measured for a 400-term block, plus headroom

note() { echo "[$(date -u +%FT%TZ)] $*"; }

note "REPLICATION WAITING for the overnight queue to drain"
# Match the driver by its path and exclude our own pid: `pgrep -f` matches the
# command line of whatever is doing the matching, which has killed a shell in
# this repo twice.
queue_running() {
    pgrep -f "run_chains/overnight_20260818.sh" 2>/dev/null \
        | grep -qv -e "^$$\$" -e "^$PPID\$"
}

while queue_running; do
    [ "$(date +%s)" -ge "$DEADLINE" ] && { note "deadline reached while waiting"; exit 0; }
    sleep 300
done
note "queue drained; starting replication blocks"

for limit in 800 1200 1600; do
    remaining=$(( DEADLINE - $(date +%s) ))
    if [ "$remaining" -lt "$BLOCK_SECONDS" ]; then
        note "STOP: ${remaining}s left, a $limit-term block needs ~${BLOCK_SECONDS}s"
        break
    fi
    out="$runtime/experiments/respelling_e_row__ay_n${limit}.json"
    if [ -e "$out" ] && artifact_complete "$python" "$out"; then
        note "SKIP $limit (artifact exists)"; continue
    fi
    note "START ay block to $limit terms"
    "$REPO/gpu_job.sh" "e_row_ay_n$limit" \
        timeout --signal=INT --kill-after=60s "$BLOCK_SECONDS" \
        "$python" -u "$REPO/app/experiments/measure_respellings.py" \
        --min-books 5 --only-e-row --e-spelling ay --limit "$limit" \
        --work "$runtime/respelling_e_row_ay" --out "$out" \
        > "$LOG/ay_n$limit.log" 2>&1 \
        && note "OK    $limit" || note "FAIL  $limit (continuing)"

    if [ -f "$out" ]; then
        "$python" "$REPO/app/experiments/pair_e_row.py" "$out" \
            >> "$runtime/reports/overnight_20260818/e_row_paired.txt" 2>&1 \
            || note "scoring $limit failed"
    fi
done

note "REPLICATION COMPLETE"
echo
echo "HOW TO READ IT. Each block INCLUDES the terms before it, so the"
echo "interesting comparison is not the headline rate - it is whether the"
echo "-ay advantage over -eh survives on the terms added since the last"
echo "block. If it shrinks as the sample grows, the original 391-term result"
echo "was a lucky draw and the derivation table should not be changed on it."
