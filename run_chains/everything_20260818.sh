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
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"
mkdir -p "$STAGE_LOG_DIR"

reclaim_vram() {
    # -x not -f: Rule 22, pkill -f matches this script's own command line.
    if pgrep -x llama-server >/dev/null 2>&1; then
        stage_note "stopping llama-server to return VRAM"
        pkill -x llama-server
        sleep 5
    fi
}

# ---- 1. Books the pipeline has never generated (LLM; server must be up) ----
stage_note "starting llama-server for the generation stage"
LLAMA_MODEL="${ALEXANDRIA_QWEN3_MODEL:-/home/fakemitch/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf}" \
    "$REPO/ensure_llama_server.sh" > "$STAGE_LOG_DIR/server.log" 2>&1 \
    && stage_note "server up" || stage_note "server start FAILED"

# REQUIRE_VRAM_GB=0 for this stage ONLY: the memory the gate is complaining
# about is the server this job needs. TTS stages below still pass the gate.
run_stage unseen_books 5h -- env REQUIRE_VRAM_GB=0 "$REPO/run_chains/unseen_books.sh"
stage_commit_artifacts unseen_books "$REPO"

# ---- 2. Do the eight failing adapters recover on a rerun? (TTS; no server) --
#
# On 2026-08-07 all five failures of the day recovered on a rerun, two of them
# to ~0.67 - so a rerun is the cheap test before anyone rebuilds a dataset. If
# they fail twice, that is a different claim than failing once, and it is the
# claim that justifies retraining.
reclaim_vram
# The list is a committed file, not /tmp: a chain that reads /tmp depends on
# whatever ran before it, and this one may be restarted tomorrow.
FAILED_LIST="$runtime/failed_adapters.tsv"

recheck_one() {
    local name="$1" adapter="$2" data="$3" score="$4"
    "$REPO/gpu_job.sh" "regate2_$name" \
        "$python" -u "$REPO/app/experiments/verify_adapter_identity.py" \
        --adapter "$REPO/$adapter" --dataset "$REPO/$data" --lines 6 \
        --out "$runtime/experiments/gate_recheck__$name.json" \
        > "$STAGE_LOG_DIR/recheck_$name.log" 2>&1
}

recheck_failures() {
    local failures=0
    while IFS=$'\t' read -r name adapter data score; do
        [ -n "${adapter:-}" ] || continue
        if recheck_one "$name" "$adapter" "$data" "$score"; then
            stage_note "  recheck OK   $name (was $score)"
        else
            stage_note "  recheck FAIL $name (was $score)"
            failures=$((failures + 1))
        fi
    done < "$FAILED_LIST"
    # Report the count rather than swallowing it - eight adapters failing
    # twice is a different claim from eight failing once, and it is the claim
    # that justifies rebuilding a dataset.
    stage_note "  $failures of 8 rechecks errored"
    return 0
}

stage_note "START recheck_failures"
recheck_failures
stage_commit_artifacts recheck_failures "$REPO"

# ---- 3. Replay the evidence that cannot currently be reproduced ------------
run_stage replay 4h -- "$REPO/run_chains/replay_dirty_evidence_20260817.sh"
stage_commit_artifacts replay "$REPO"

reclaim_vram
stage_summary everything
