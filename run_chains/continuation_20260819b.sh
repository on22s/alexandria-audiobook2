#!/usr/bin/bash
# 2026-08-19b: a corrected COPY. The first run of this chain lost five stages
# to rc=7 (NO_VRAM). It starts llama-server for the two-stage arm and never
# reclaims it, so every TTS stage after that found 1568 MiB free against a
# 4096 MiB floor and was refused - reported as "6 of 9 stages failed" for a
# card that was merely still full. The TTS stages now declare --needs-vram.
#
# Also: two_stage_full and index18_dialogue_map already completed, and
# unseen_books runs from the corrected copy that skips books already generated.
# Picks up when overnight_20260819.sh stops, and keeps working.
#
# That chain carries a 14:30 deadline because it was written expecting the
# machine to be wanted back then. It is not - the return time was an estimate,
# not a stop - so this continues past it. It is a SEPARATE FILE rather than an
# edit, because bash reads a script incrementally and rewriting a running one
# resumes it at a meaningless offset (Rule 23, and an hour of idle GPU on
# 2026-08-18).
#
# No deadline here. Work stops when the work is done, or when the queue is
# paused for gaming.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
inputs="$runtime/results/collect_all_20260722-155801/inputs"
STAGE_LOG_DIR="$runtime/logs/continuation_20260819b"
source "$REPO/run_chains/lib/stage.sh"
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"
mkdir -p "$STAGE_LOG_DIR"

running() {
    pgrep -f "run_chains/$1" 2>/dev/null | grep -qv -e "^$$\$" -e "^$PPID\$"
}

stage_note "waiting for the overnight chain to finish"
while running overnight_20260819.sh; do sleep 300; done
stage_note "overnight chain done; continuing"

start_server() {
    LLAMA_MODEL="${ALEXANDRIA_QWEN3_MODEL:-/home/fakemitch/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf}" \
        "$REPO/ensure_llama_server.sh" > "$STAGE_LOG_DIR/server.log" 2>&1 \
        && stage_note "llama-server up" || stage_note "llama-server FAILED"
}

# ---- 1. The two-stage result, at full size ---------------------------------
# The overnight arm caps at 200 quotes per book to get an answer by morning.
# If it is promising, the number that decides anything is the whole gold set:
# 1,270 quotes in Pride and Prejudice alone, and the published comparison is
# over all of it.
# two_stage_full and index18_dialogue_map completed in the first run
# (two_stage_attribution_full.json, 2,494 quotes). Not repeated - and the
# llama-server they needed is deliberately NOT started here, because every
# remaining stage is TTS and wants that memory.

# ---- 3. The books nothing has generated yet --------------------------------
run_stage unseen_books_rest 8h --needs-vram -- \
    env REQUIRE_VRAM_GB=0 "$REPO/run_chains/unseen_books_20260819b.sh"
stage_commit_artifacts unseen_books_rest "$REPO"

# ---- 4. Finish the respelling separator question ---------------------------
# Three arms against the shipped hyphen, on identical terms. The first was cut
# off at 50 of 120 by the pause and resumes from its own work directory.
for sep in none space dot; do
    out="$runtime/experiments/respelling_separator__${sep}.json"
    run_stage "separator_$sep" 3h --needs-vram -- \
        "$REPO/gpu_job.sh" "separator_$sep" \
        "$python" -u "$REPO/app/experiments/measure_respellings.py" \
        --min-books 5 --only-e-row --separator "$sep" --limit 120 \
        --work "$runtime/respelling_sep_$sep" --out "$out"
    stage_commit_artifacts "separator_$sep" "$REPO"
done

run_stage separator_pauses 1h --needs-vram -- \
    "$python" -u "$REPO/app/experiments/measure_pauses.py" --limit 400 \
    --arm none=respelling_sep_none --arm space=respelling_sep_space \
    --arm dot=respelling_sep_dot \
    --out "$runtime/experiments/respelling_pauses_separators.json"

# ---- 5. Everything still unreplayable --------------------------------------
run_stage replay_to_completion 6h --needs-vram -- \
    "$REPO/run_chains/replay_dirty_evidence_20260817.sh"
stage_commit_artifacts replay_to_completion "$REPO"

run_stage indexes 20m -- "$python" -u "$REPO/refresh_indexes.py"
stage_summary continuation_20260819b
