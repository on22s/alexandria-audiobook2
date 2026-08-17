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
REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
L="$REPO/ab_test_runtime/logs"
PY="$REPO/app/env/bin/python"
IN="$REPO/ab_test_runtime/results/collect_all_20260722-155801/inputs"
OUT="$REPO/ab_test_runtime/goal31"
BACKUP="$L/config.json.overnight_backup"
export GPU_LOCK="${GPU_LOCK:-$HOME/.alexandria_gpu.lock}"
export GPU_QLOG="$L/gpu_jobq.log"
mkdir -p "$L" "$OUT"
cd "$REPO/app"

restore_config() {
    [ -f "$BACKUP" ] && command cp -f "$BACKUP" "$REPO/app/config.json" && \
        echo "restored app/config.json"
    # Stop only the server this script started, by the PID it recorded.
    if [ -n "${SERVER_PID:-}" ] && kill -0 "$SERVER_PID" 2>/dev/null; then
        kill "$SERVER_PID" 2>/dev/null; sleep 5
        kill -0 "$SERVER_PID" 2>/dev/null && kill -9 "$SERVER_PID" 2>/dev/null
        echo "stopped llama-server $SERVER_PID"
    fi
}
trap restore_config EXIT INT TERM

command cp -f "$REPO/app/config.json" "$BACKUP"
"$PY" - <<'PYEOF'
import json
p = "/home/fakemitch/pinokio/api/alexandria-audiobook2.git/app/config.json"
d = json.load(open(p, encoding="utf-8"))
for key in ("llm", "llm_local"):
    if isinstance(d.get(key), dict):
        d[key]["model_name"] = "qwen3-14b"
json.dump(d, open(p, "w", encoding="utf-8"), indent=2)
print("config -> qwen3-14b")
PYEOF

MODEL=~/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf
nohup llama-server -m "$MODEL" --port 8090 --host 127.0.0.1 -c 32768 -np 1 \
    -ngl 999 --alias qwen3-14b \
    --chat-template-kwargs '{"enable_thinking":false}' \
    > "$L/llama_server_qwen3.log" 2>&1 &
SERVER_PID=$!
echo "llama-server $SERVER_PID starting"

# A run against a dead endpoint reports 0% completion and reads like a model
# result. Refuse instead.
for i in $(seq 1 40); do
    sleep 15
    curl -s -m 5 http://127.0.0.1:8090/v1/models 2>/dev/null | grep -q qwen3 && break
done
if ! curl -s -m 20 http://127.0.0.1:8090/v1/models | grep -q qwen3; then
    echo "ABORT: server never came up"; exit 1
fi
echo "endpoint ready $(date -u +%FT%TZ)"

stage() {
    local name="$1"; shift
    echo ""
    echo "=== $name  $(date -u +%FT%TZ) ==="
    "$REPO/gpu_job.sh" "$name" "$@" > "$L/$name.log" 2>&1
    echo "  rc=$?"
    tail -4 "$L/$name.log" | sed 's/^/  /' | cut -c1-115
    return 0        # one failed book must not strand the rest of the night
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

echo ""
echo "OVERNIGHT DONE $(date -u +%FT%TZ)"
