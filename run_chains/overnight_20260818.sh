#!/usr/bin/bash
# Keep the card busy from 2026-08-17 evening to 2026-08-18 noon.
#
# ORDER IS BY WHAT THE EVIDENCE BASE IS MISSING, not by what is convenient.
# 25 of 30 goals cite no artifact, and every method finding in the repo rests
# on four Japanese light novels in translation. So generalisation evidence
# (PDNC, 28 annotated public-domain English novels) outranks re-running things
# already measured on the same four books.
#
# REPLAY GOES LAST, DELIBERATELY. It is the only stage that OVERWRITES
# committed artifacts, and the moment it does the working tree is dirty - at
# which point gpu_job.sh's dirty-tree gate correctly refuses everything after
# it. Running it last converts that from a queue-killer into a non-event.
# Do not reorder without dealing with that.
#
# THE SERVER IS STOPPED BETWEEN STAGES. llama-server is started outside the
# lock on purpose (consecutive LLM evals share one 8.4 GB load) and nothing
# ever reclaims it. On 2026-08-17 that cost 14 adapters to OOM: one job held
# the lock, a non-job held 14.77 GiB. A TTS stage after an LLM stage needs
# that memory back, so each stage boundary reclaims it explicitly.
#
# Every stage continues on failure. A stage that dies at 2am must not take the
# remaining ten hours with it.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
LOG="$runtime/logs/overnight_20260818"
mkdir -p "$LOG"
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"

note() { echo "[$(date -u +%FT%TZ)] $*"; }

reclaim_vram() {
    # -x, not -f: pkill -f matches this script's own command line, which has
    # killed a shell in this repo twice.
    if pgrep -x llama-server >/dev/null 2>&1; then
        note "stopping llama-server to return VRAM to the next stage"
        pkill -x llama-server
        sleep 5
    fi
}

stage() {
    local name="$1" limit="$2"; shift 2
    note "START $name (cap ${limit})"
    reclaim_vram
    if timeout --signal=INT --kill-after=120s "$limit" "$@" \
            > "$LOG/$name.log" 2>&1; then
        note "OK    $name"
    else
        note "FAIL  $name rc=$? (see $LOG/$name.log)"
    fi
}

note "OVERNIGHT START"

# 1. The re-gate. 87 gate artifacts carry no provenance and goal 2.7 is built
#    on them; this is the stage that lets that goal cite something replayable.
stage regate 5h "$REPO/run_chains/regate_with_provenance_20260817.sh"

# 2-4. PDNC. All three pilots exist and each chain opens its sealed
#    confirmatory set only if its pilot gate passed - so a stage that "does
#    nothing" here is the gate working, not a failure. This is the only
#    evidence in the queue that speaks to generalisation beyond the four
#    light novels (goals 1.3, 3.1).
for intervention in evidence sequence targeted_sequence; do
    stage "pdnc_$intervention" 3h \
        env ALEXANDRIA_PDNC_INTERVENTION="$intervention" \
        "$REPO/run_chains/pdnc_context_evidence.sh"
done

# 5. Books the pipeline has never generated. index18 exposed five distinct
#    blockers no previously-tested book had; these are where the next ones are.
stage unseen_books 6h "$REPO/run_chains/unseen_books.sh"

# 6. LAST, for the reason in the header.
stage replay 4h "$REPO/run_chains/replay_dirty_evidence_20260817.sh"

reclaim_vram
note "OVERNIGHT COMPLETE"
