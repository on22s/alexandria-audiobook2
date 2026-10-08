#!/usr/bin/bash
# The remaining GPU work, in one restartable chain.
#
# ORDER IS BY WHAT HAS NO RESULT AT ALL, not by cost. unseen_books has produced
# nothing across three attempts (an abort on a missing server, a refusal on the
# VRAM gate, and a two-hour hang on an LLM call with no timeout); everything
# else here refines evidence that already exists. It runs first and gets the
# largest share.
#
# THE SERVER IS STARTED ONCE AND STOPPED ONCE. llama-server is started outside
# the lock deliberately and nothing reclaims it, so an LLM stage wants it
# resident and a TTS stage wants it gone. That is why the VRAM gate is waived
# for the LLM stage only, and why reclaim happens between the two groups rather
# than between every stage.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/everything_20260818"
source "$REPO/run_chains/lib/stage.sh"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"
mkdir -p "$STAGE_LOG_DIR"

reclaim_vram() {
    # Reclamation is a supervised queued operation, including final cleanup.
    # It must not disturb an experiment that currently holds the GPU lease.
    # No extra cap on queue waiting; server-stop and VRAM polling stay bounded.
    run_stage "$1" 0 --needs-vram -- "$REPO/gpu_job.sh" "$1" true
}

# ---- 1. Books the pipeline has never generated (LLM; server must be up) ----
stage_note "starting llama-server for the generation stage"
LLAMA_MODEL="${ALEXANDRIA_QWEN3_MODEL:-/home/fakemitch/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf}" \
    "$REPO/ensure_llama_server.sh" > "$STAGE_LOG_DIR/server.log" 2>&1 \
    && stage_note "server up" || stage_note "server start FAILED"

# REQUIRE_VRAM_GB=0 for this stage ONLY: the memory the gate is complaining
# about is the server this job needs. TTS stages below still pass the gate.
run_stage unseen_books 5h -- env REQUIRE_VRAM_GB=0 "$REPO/run_chains/unseen_books.sh"
# This generator writes under unseen_books, outside the experiment commit scope.

# ---- 2. Do the eight failing adapters recover on a rerun? (TTS; no server) --
#
# On 2026-08-07 all five failures of the day recovered on a rerun, two of them
# to ~0.67 - so a rerun is the cheap test before anyone rebuilds a dataset. If
# they fail twice, that is a different claim than failing once, and it is the
# claim that justifies retraining.
reclaim_vram reclaim_before_recheck
# The list is a committed file, not /tmp: a chain that reads /tmp depends on
# whatever ran before it, and this one may be restarted tomorrow.
FAILED_LIST="$runtime/failed_adapters.tsv"

# EXIT 3 IS A VERDICT. ANYTHING ELSE IS AN ABSENCE.
#
# verify_adapter_identity.py exits 3 when it measured the adapter and the score
# was below threshold, 2 when it could not measure at all, and gpu_job.sh has
# its own codes on top: 5 for a dirty tree, 4 for the lock, 7 for VRAM, 124 for
# a timeout. This function used to collapse every one of those into "recheck
# FAIL $name", and on 2026-08-19 that printed eight verdict-shaped lines for a
# night in which SIX of the eight jobs were refused before they started - the
# tree was dirty - and only two were ever measured. A reader would have
# concluded that eight adapters failed a retest, which is the kind of claim
# that gets a dataset rebuilt.
recheck_one() {
    local name="$1" adapter="$2" data="$3" out="$4"
    "$REPO/gpu_job.sh" "regate2_$name" \
        "$python" -u "$REPO/app/experiments/verify_adapter_identity.py" \
        --adapter "$REPO/$adapter" --dataset "$REPO/$data" --lines 6 \
        --out "$out" \
        > "$STAGE_LOG_DIR/recheck_$name.log" 2>&1
}

# The score this run measured, read from the artifact rather than echoed back
# from the input file. "(was 0.0342)" is the OLD number and says nothing about
# the recheck; printing it beside the word FAIL is what made the absent runs
# unreadable.
recheck_score() {
    "$python" "$REPO/app/experiments/recheck_result.py" "$1" "$2" "$3"
}

recheck_failures() {
    local measured=0 below=0 never_ran=0 total=0 rc score attempt new
    recheck_artifacts=()
    if [ ! -f "$FAILED_LIST" ] || [ ! -r "$FAILED_LIST" ] || [ ! -s "$FAILED_LIST" ]; then
        stage_note "  recheck NOT MEASURED: adapter roster is missing, unreadable or empty: $FAILED_LIST"
        return 1
    fi
    while IFS=$'\t' read -r name adapter data score; do
        [ -n "${adapter:-}" ] || continue
        total=$((total + 1))
        # A new private output path cannot contain a previous run's verdict.
        if ! attempt=$(mktemp -d "$STAGE_LOG_DIR/recheck.XXXXXXXX"); then
            never_ran=$((never_ran + 1))
            stage_note "  recheck NOT MEASURED $name (cannot isolate output)"
            continue
        fi
        recheck_one "$name" "$adapter" "$data" "$attempt/result.json"
        rc=$?
        if ! new=$(recheck_score "$attempt/result.json" "$adapter" "$rc") || \
            ! mv -- "$attempt/result.json" "$runtime/experiments/gate_recheck__$name.json"; then
            never_ran=$((never_ran + 1))
            stage_note "  recheck NOT MEASURED $name (rc=$rc, no fresh valid verdict) - see $STAGE_LOG_DIR/recheck_$name.log and $attempt"
            continue
        fi
        recheck_artifacts+=("$runtime/experiments/gate_recheck__$name.json")
        rmdir -- "$attempt"
        case "$rc" in
            0)  measured=$((measured + 1))
                stage_note "  recheck PASS  $name  was $score, now $new" ;;
            3)  measured=$((measured + 1)); below=$((below + 1))
                stage_note "  recheck BELOW $name  was $score, now $new" ;;
            *)  never_ran=$((never_ran + 1))
                stage_note "  recheck NOT MEASURED $name (rc=$rc, no run) - see $STAGE_LOG_DIR/recheck_$name.log" ;;
        esac
    done < "$FAILED_LIST"
    if [ "$total" -eq 0 ]; then
        stage_note "  recheck NOT MEASURED: adapter roster contains no adapter rows: $FAILED_LIST"
        return 1
    fi
    stage_note "  $measured of $total measured; $below below threshold; $never_ran never ran"
    # FAIL LOUD. A stage that measured nothing must not report success, or the
    # chain summary calls the night a pass (Rule 8).
    if [ "$never_ran" -gt 0 ]; then
        stage_note "  recheck_failures INCOMPLETE: $never_ran of $total never ran"
        return 1
    fi
    return 0
}

# COUNTED LIKE ANY OTHER STAGE. Called bare, its return value went nowhere and
# a night in which nothing was measured still reached "SUMMARY everything" as
# though the stage had passed.
stage_note "START recheck_failures"
STAGE_TOTAL=$((STAGE_TOTAL + 1))
if recheck_failures; then
    STAGE_RESULT[recheck_failures]=ok
else
    STAGE_RESULT[recheck_failures]=incomplete
    STAGE_FAILURES=$((STAGE_FAILURES + 1))
fi
stage_commit_artifacts recheck_failures "$REPO" "${recheck_artifacts[@]}"

# ---- 3. Replay the evidence that cannot currently be reproduced ------------
run_stage replay 4h -- "$REPO/run_chains/replay_dirty_evidence_20260817.sh"
# The child commits each dispatched artifact by its exact path.

reclaim_vram reclaim_after_replay
stage_summary everything
