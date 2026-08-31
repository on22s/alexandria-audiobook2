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
STAGE_LOG_DIR="$REPO/ab_test_runtime/logs/overnight_20260830b"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"

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
    # THIS STAGE MUST RECLAIM THE CARD FIRST, and the first version of this
    # chain did not. Stage 1 starts llama-server through ensure_llama_server,
    # which deliberately OUTLIVES the job that started it so consecutive LLM
    # stages share one load. Nothing stops it afterwards. So on 2026-08-31 this
    # stage was refused at 01:49 with 1350 MiB free against a 4096 MiB floor,
    # the chain printed exit=0, and the card sat idle for SEVEN HOURS until a
    # person requeued it by hand.
    #
    # run_stage --needs-vram exists for exactly this and its own comment
    # records the same failure on 2026-08-19, five stages lost the same way.
    # Calling ./gpu_job.sh directly skipped it. It also polls for the memory
    # rather than sleeping a fixed interval, because the driver frees VRAM some
    # time after the process exits.
    echo "[$(date -u +%FT%TZ)] STAGE 2: $name  (74 adapters, ~4 h estimated)"
    run_stage "$name" 5h --needs-vram -- \
        ./gpu_job.sh "$name" \
        ./app/env/bin/python -u app/experiments/library_voice_fidelity.py \
        --lines 20 --seed "$SEED" \
        --work "ab_test_runtime/${name}" --out "$out"
    stage_summary overnight_20260830b_stage2
fi
echo "[$(date -u +%FT%TZ)] COMPLETE overnight_20260830b"
