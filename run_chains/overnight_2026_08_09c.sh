#!/bin/bash
# Overnight, ~10 hours. Finish goal 3.1's dataset on the shipped model.
#
# THE GOAL. 3.1 wants "chunks completing without manual intervention, >= 99%,
# on the shipped model". Two of the four books have that number on qwen3-14b -
# mushoku16 45/45 and owarimonogatari3 110/110, both 100%. grimgar03 has never
# finished, and index18 has never been RUN, because the source gate refused it
# over 6,662 replacement characters until today.
#
# Both blockers were removed today: the faithful-duplicate fix (grimgar03's
# title repetition) and the graded source gate plus the encoding repair
# (index18 at 0.259%, under the 0.50% limit). So this is the first time all
# four books CAN be attempted.
#
# STAGE 1 IS DIAGNOSTIC, AND RUNS FIRST BECAUSE IT IS CHEAP. grimgar03 failed
# its rerun at chunk 11 on a COVERAGE validation - the response not reproducing
# the full source span - after passing that same chunk in the previous run.
# Script generation runs at temperature 0.6, so chunk outcomes vary. Running
# chunk 11 alone several times measures how often it actually succeeds, which
# is the difference between "this book is unlucky" and "the coverage gate has a
# second defect". A whole-book rerun cannot separate those and costs 2.5 hours
# to learn one bit.
#
# STAGE 2 IS THE NEW CAPABILITY. index18 has been excluded from every goal that
# measures on it. Generating it is worth more than a third grimgar03 attempt
# because it turns three books into four for 1.1, 1.3, 3.1 and 5.3.
#
# STAGE 3 AND 4 measure grimgar03's completion rate rather than assuming it.
# Two attempts, because one success would not distinguish a reliable book from
# a coin flip, and run 1 (49/49) versus run 2 (died at 11) already suggests it
# is closer to a coin flip.
set -uo pipefail
REPO="$(cd "$(dirname "$0")/.." && pwd)" || exit 1
L="$REPO/ab_test_runtime/logs"
PY="$REPO/app/env/bin/python"
IN="$REPO/ab_test_runtime/results/collect_all_20260722-155801/inputs"
OUT="$REPO/ab_test_runtime/goal31"
BACKUP="$L/config.json.overnight_backup"
# NO GPU_LOCK EXPORT. This line used to name $HOME/.alexandria_gpu.lock, a
# third lock file that serialised against neither the repo lock the other
# chains use nor gpu_job.sh's own - and it sat BELOW the self-re-exec above,
# so this chain's outer wrapper and its inner jobs took different locks.
# gpu_job.sh now defaults to the repo lock; letting it decide is the point.
export GPU_QLOG="$L/gpu_jobq.log"
source "$REPO/run_chains/lib/config_backup.sh" || exit 1
restore_llm_campaign_state() {
    restore_config_backup "$BACKUP" "$REPO/app/config.json"
}
export OVERNIGHT_STARTUP_VRAM_GB="${OVERNIGHT_STARTUP_VRAM_GB:-${REQUIRE_VRAM_GB:-4}}"
export LLAMA_PORT=8090
source "$REPO/run_chains/lib/llm_campaign.sh" || exit 4
ensure_llm_campaign_lease overnight_2026_08_09c "$REPO/run_chains/overnight_2026_08_09c.sh" "$@" || exit $?
env REQUIRE_VRAM_GB="$OVERNIGHT_STARTUP_VRAM_GB" bash "$REPO/gpu_job.sh" \
    --check-vram overnight_2026_08_09c.startup || exit $?
mkdir -p "$L" "$OUT"
cd "$REPO/app" || exit 1
STAGE_LOG_DIR="$L"
source "$REPO/run_chains/lib/stage.sh" || exit 1
restore_config_backup "$BACKUP" "$REPO/app/config.json" || exit 1

save_config_backup "$REPO/app/config.json" "$BACKUP" || exit 1
"$PY" - "$REPO/app/config.json" <<'PYEOF' || exit 1
import json, sys
p = sys.argv[1]
d = json.load(open(p, encoding="utf-8"))
for key in ("llm", "llm_local"):
    if isinstance(d.get(key), dict):
        d[key]["model_name"] = "qwen3-14b"
json.dump(d, open(p, "w", encoding="utf-8"), indent=2)
print("config -> qwen3-14b")
PYEOF

MODEL="${ALEXANDRIA_QWEN3_MODEL:-$HOME/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf}"
LLAMA_MODEL="$MODEL" LLAMA_PORT=8090 LLAMA_CTX=32768 LLAMA_THINKING=0 \
    LLAMA_ALIAS=qwen3-14b LLAMA_LOG="$L/llama_server_qwen3.log" \
    "$REPO/ensure_llama_server.sh" > "$L/overnight_server_start.log" 2>&1 || {
        echo "ABORT: canonical server not ready; see $L/overnight_server_start.log"
        exit 1
    }
LLM_CAMPAIGN_SERVER_READY=1
echo "endpoint ready $(date -u +%FT%TZ)"

stage() {
    local name="$1"; shift
    if [ "$name" = g31_recount ]; then
        run_stage "$name" 0 -- "$@"
    else
        run_stage "$name" 0 -- env REQUIRE_LLM=1 REQUIRE_VRAM_GB=0 \
            bash "$REPO/run_chains/lib/llm_job.sh" "$name" "$@"
    fi
    tail -4 "$L/$name.log" | sed 's/^/  /' | cut -c1-115
}

# 1. Chunk 11 in isolation, five times (~25m).
stage g31_chunk11 timeout 3600 "$PY" -u experiments/chunk_retry_probe.py \
    --source "$IN/grimgar03.txt" --chunk 11 --repeats 5 \
    --out "$REPO/ab_test_runtime/experiments/chunk11_stability.json"

# 2. index18, repaired - the first time this book has ever been generated.
stage g31_index18 timeout 21600 "$PY" -u generate_script.py \
    "$REPO/ab_test_runtime/repaired_inputs/index18.repaired.txt" \
    --output "$OUT/index18.json"

# 3 and 4. grimgar03 twice, to measure a completion rate rather than assume one.
stage g31_grimgar_a timeout 21600 "$PY" -u generate_script.py \
    "$IN/grimgar03.txt" --output "$OUT/grimgar03_a.json"

stage g31_grimgar_b timeout 21600 "$PY" -u generate_script.py \
    "$IN/grimgar03.txt" --output "$OUT/grimgar03_b.json"

# 5. Read completion off whatever the night produced, attributable by model.
stage g31_recount "$PY" -u experiments/chunk_completion.py \
    --scripts "$OUT" \
    --out "$REPO/ab_test_runtime/experiments/chunk_completion_goal31.json"

stage_summary overnight_2026_08_09c
