#!/bin/bash
# Goal 1.3's confirmation, then one fidelity seed at full adapter coverage.
#
# TWO JOBS, BOTH OF WHICH MEASURE SOMETHING NOT MEASURED BEFORE.
#
#   goal 1.3   the balanced adapter over five never-trained Austen novels and
#              three of its own training books, 10,114 quotations x 2 arms.
#              ~3h45m at the measured 16.5 s/batch over 816 batches. The chain
#              runs held-out first, so a short night still answers the goal.
#
#   fidelity   ONE seed, not eight. Every previous seed scored 18 of 75
#              adapters because 56 resolved their dataset to the literal string
#              "data"; #422 fixed that and all 75 now find their zip. So this
#              seed measures roughly four times the work of its predecessors:
#              the 60 min/seed recorded on 2026-08-30 was 18 adapters, and 74
#              should be nearer 4 h. That estimate is DERIVED, not measured -
#              no seed has ever run at this coverage - so one seed runs and the
#              rate comes from it, rather than committing a night to eight.
#
# Total is therefore ~8 h and both halves are resumable: goal13 skips books
# whose artifact exists, and the fidelity seed is skipped outright if its
# artifact is already there.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO" || exit 1
SEED="${FIDELITY_SEED:-20260914}"

echo "[$(date -u +%FT%TZ)] STAGE 1: goal 1.3 confirmation"
./run_chains/goal_13_confirmation_20260830.sh
rc=$?
echo "[$(date -u +%FT%TZ)] STAGE 1 exit=$rc"

# Stage 2 runs whatever stage 1 did: the fidelity seed shares no inputs with
# goal13, so a partial attribution run is no reason to skip a voice
# measurement. --requires-ok would be wrong here.
name="library_fidelity_seed_${SEED}_n20"
out="ab_test_runtime/experiments/${name}.json"
if [ -s "$out" ]; then
    echo "[$(date -u +%FT%TZ)] SKIP $name (already complete)"
else
    echo "[$(date -u +%FT%TZ)] STAGE 2: $name  (74 adapters, ~4 h estimated)"
    ./gpu_job.sh "$name" \
        ./app/env/bin/python -u app/experiments/library_voice_fidelity.py \
        --lines 20 --seed "$SEED" \
        --work "ab_test_runtime/${name}" --out "$out" \
        > "ab_test_runtime/logs/${name}.out" 2>&1
    echo "[$(date -u +%FT%TZ)] STAGE 2 exit=$?"
fi
echo "[$(date -u +%FT%TZ)] COMPLETE overnight_20260830b"
