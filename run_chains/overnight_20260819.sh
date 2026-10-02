#!/usr/bin/bash
# Overnight 2026-08-18 -> 2026-08-19. Everything left, ordered so the answers
# that decide other work land first. Stops at 14:30 so the machine is free
# before you are back.
#
# Three chains are already queued and waiting on the lock (separator arms,
# the 8-adapter recheck, replay); gpu_job.sh serialises everything, so this
# adds work rather than competing with it.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
inputs="$runtime/results/collect_all_20260722-155801/inputs"
STAGE_LOG_DIR="$runtime/logs/overnight_20260819"
source "$REPO/run_chains/lib/stage.sh"
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"
DEADLINE=$(date -d "2026-08-19 14:30" +%s)
left() { echo $(( DEADLINE - $(date +%s) )); }
if [ "$(left)" -le 0 ]; then
    stage_note "REFUSING: the overnight deadline has expired"
    exit 1
fi

reclaim_vram() {
    # Queue ownership serializes server cleanup with every active GPU job.
    # Queue waiting stays unbounded; existing cleanup/VRAM bounds still apply.
    run_stage "$1" 0 --needs-vram -- "$REPO/gpu_job.sh" "$1" true
}

start_server() {
    LLAMA_MODEL="${ALEXANDRIA_QWEN3_MODEL:-/home/fakemitch/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf}" \
        "$REPO/ensure_llama_server.sh" > "$STAGE_LOG_DIR/server.log" 2>&1 \
        && stage_note "llama-server up" || stage_note "llama-server FAILED to start"
}

mkdir -p "$STAGE_LOG_DIR"

# ---- 1. Is the corpus sound? Cheap, and it decides what the rest is worth ---
run_stage source_encoding_audit 10m -- \
    "$python" -u "$REPO/app/experiments/audit_source_encoding.py"

# ---- 2. The attribution question, on books with answer keys -----------------
# The published formulation reaches 90.6% on PDNC with an 8B model; ours scores
# 61.7% with a 14B. This is the arm that says whether the gap is the question
# or the model, and everything else about attribution waits on it.
start_server
run_stage two_stage_attribution 4h -- \
    env REQUIRE_LLM=1 REQUIRE_VRAM_GB=0 \
    "$REPO/gpu_job.sh" two_stage_attribution \
    "$python" -u "$REPO/app/experiments/two_stage_attribution.py" --limit 200
stage_commit_artifacts two_stage_attribution "$REPO" "$runtime/experiments/two_stage_attribution__current.json" "$runtime/experiments/two_stage_attribution__current.json.ckpt" "$runtime/experiments/two_stage_attribution__current.json.ckpt.stale"

# ---- 3. The narrator prior, on the book it was built for --------------------
# mushoku18 is first person, its narrator speaks aloud, and 51% of its spoken
# lines stayed with NARRATOR. Same book, same settings, one variable.
if [ "$(left)" -gt 7200 ]; then
    run_stage mushoku18_narrator 5h -- \
        env REQUIRE_LLM=1 REQUIRE_VRAM_GB=0 \
        "$REPO/gpu_job.sh" mushoku18_narrator \
        "$python" -u "$REPO/app/generate_script.py" "$inputs/mushoku18.txt" \
        --narrator RUDEUS \
        --output "$runtime/unseen_books/mushoku18_narrator.json"
    # This generator writes under unseen_books, outside the experiment commit scope.
fi

# ---- 4. index18, now that its text is not corrupt ---------------------------
# The old file had no quotation marks at all; 32 artifacts and several goal
# claims rest on that. This is the first generation from a clean extraction.
if [ "$(left)" -gt 7200 ]; then
    run_stage index18_clean 4h -- \
        env REQUIRE_LLM=1 REQUIRE_VRAM_GB=0 \
        "$REPO/gpu_job.sh" index18_clean \
        "$python" -u "$REPO/app/generate_script.py" "$inputs/index18.txt" \
        --output "$runtime/unseen_books/index18_clean.json"
    # This generator writes under unseen_books, outside the experiment commit scope.
fi

# ---- 5. The fourth unseen book, resumed from its checkpoint -----------------
if [ "$(left)" -gt 5400 ]; then
    run_stage unseen_books_resume 3h -- \
        env REQUIRE_VRAM_GB=0 "$REPO/run_chains/unseen_books.sh"
    # This generator writes under unseen_books, outside the experiment commit scope.
fi

# ---- 6. TTS work, which wants the card to itself ---------------------------
reclaim_vram reclaim_before_tts
if [ "$(left)" -gt 3600 ]; then
    run_stage e_row_second_voice 2h -- \
        "$REPO/gpu_job.sh" e_row_second_voice \
        "$python" -u "$REPO/app/experiments/measure_respellings.py" \
        --min-books 5 --only-e-row --e-spelling ay --limit 200 \
        --work "$runtime/respelling_voice2" \
        --out "$runtime/experiments/respelling_e_row__ay_voice2.json"
    stage_commit_artifacts e_row_second_voice "$REPO" "$runtime/experiments/respelling_e_row__ay_voice2.json"
fi

# ---- 7. Whatever the night has left ----------------------------------------
if [ "$(left)" -gt 3600 ]; then
    run_stage replay_remaining "$(left)s" -- \
        "$REPO/run_chains/replay_dirty_evidence_20260817.sh"
    # The child commits each dispatched artifact by its exact path.
fi

reclaim_vram reclaim_after_replay

# CPU work, safe to run beside anything and independent of the card.
run_stage dialogue_map_corpus 30m -- \
    "$python" -u "$REPO/app/experiments/measure_dialogue_attribution.py" \
    "$runtime/unseen_books/mushoku18.json" \
    "$runtime/unseen_books/mushoku23.json" \
    "$runtime/unseen_books/arc4_volume10wn.json"
run_stage indexes 20m -- "$python" -u "$REPO/refresh_indexes.py"

stage_summary overnight_20260819
