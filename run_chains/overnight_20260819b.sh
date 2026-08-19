#!/usr/bin/bash
# 2026-08-19 evening -> 2026-08-20 13:00 CDT.
#
# ORDERED BY WHAT TODAY ACTUALLY OPENED UP, not by what is left over.
#
# Today's separator result is clean and graded: `none` is indistinguishable
# from un-respelled audio (43/74, p=0.20), `space` pauses (87/107, p=3.8e-11),
# `dot` pauses hardest (115/118, p=1.65e-30). Removing the separator removes
# the pauses.
#
# The same arms showed respelling buying NO recovery in any form - none 16
# wins/20 losses at 391 terms, p=0.62. But every one of those arms is
# --only-e-row, the single worst row (6.6% against 18.0% elsewhere), so that
# says nothing about respelling generally. Stage 1 drops the row filter and
# runs the no-separator form across ALL terms, which is the comparison that
# would justify changing the shipped default. It is first because it is the
# only stage that can change what ships.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/overnight_20260819b"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"

# 1. Does no-separator respelling help ACROSS ALL ROWS? The shipped hyphen
#    measurement covers 7,775 terms at 14.6% plain / 14.6% respelled - no net
#    gain. If `none` beats that pairwise, the default should change; if it
#    matches it, respelling is decoration and that is worth knowing too.
run_stage separator_none_allrows 5h --needs-vram -- \
    "$REPO/gpu_job.sh" separator_none_allrows \
    "$python" -u "$REPO/app/experiments/measure_respellings.py" \
    --min-books 5 --separator none --limit 800 \
    --work "$runtime/respelling_none_allrows" \
    --out "$runtime/experiments/respelling_none_allrows.json"
stage_commit_artifacts separator_none_allrows "$REPO"

# 2. The one PDNC intervention worth another look. Scored paired this morning,
#    sequence-aware came out at p=0.054 against a 5.5% run-to-run floor - the
#    shape a lucky draw takes. A repeat on the same pilot books says which it
#    was, and costs far less than opening the sealed twenty-book set.
#    Driven directly rather than through pdnc_context_evidence.sh: that
#    wrapper skips when the pilot artifact already exists, so asking it for a
#    repeat would have silently done nothing and reported success. --tag gives
#    the repeat its own artifact instead of overwriting the run it is being
#    compared against.
# NO --needs-vram HERE. This is the one LLM stage in the chain: reclaiming
# VRAM would stop the llama-server it requires, and REQUIRE_LLM=1 would then
# refuse the job. The TTS stages around it reclaim, which is what frees the
# card for them.
start_server() {
    "$REPO/ensure_llama_server.sh" > "$STAGE_LOG_DIR/server.log" 2>&1 \
        && stage_note "llama-server up" \
        || stage_note "llama-server FAILED - the PDNC stage will refuse"
}
start_server
run_stage pdnc_sequence_repeat 4h -- \
    env REQUIRE_LLM=1 REQUIRE_VRAM_GB=0 \
    "$REPO/gpu_job.sh" pdnc_sequence_repeat \
    "$python" -u "$REPO/app/experiments/pdnc_context_evidence.py" \
    --phase pilot --intervention sequence --tag repeat2
stage_commit_artifacts pdnc_sequence_repeat "$REPO"

# 3. The book that genuinely failed twice. Its three companions are on disk and
#    skipped in seconds; this now exits non-zero if it produces nothing.
run_stage grimgar06_retry 5h --needs-vram -- \
    env REQUIRE_VRAM_GB=0 "$REPO/run_chains/unseen_books_20260819b.sh"
stage_commit_artifacts grimgar06_retry "$REPO"

# 4. Widen the no-separator arm if the night still has room. Same work
#    directory, so the first 800 terms are skipped rather than regenerated.
run_stage separator_none_1600 5h --needs-vram -- \
    "$REPO/gpu_job.sh" separator_none_1600 \
    "$python" -u "$REPO/app/experiments/measure_respellings.py" \
    --min-books 5 --separator none --limit 1600 \
    --work "$runtime/respelling_none_allrows" \
    --out "$runtime/experiments/respelling_none_allrows_n1600.json"
stage_commit_artifacts separator_none_1600 "$REPO"

run_stage indexes 20m -- "$python" -u "$REPO/refresh_indexes.py"
stage_summary overnight_20260819b
