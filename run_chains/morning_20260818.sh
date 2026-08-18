#!/usr/bin/bash
# Restart what the overnight queue left undone. Written 2026-08-18 07:5xZ.
#
# WHAT WENT WRONG, so this does not repeat it:
#
# 1. replay hit its 4h cap having rewritten 35 artifacts. Every rewrite
#    modifies a tracked file, so the tree went dirty and gpu_job.sh REFUSED
#    all three replication blocks behind it. The card sat idle for 80 minutes.
#    Those artifacts are now committed, and this chain COMMITS ARTIFACTS
#    BETWEEN STAGES for the same reason - one stage's output must not gate the
#    next stage's start.
#
# 2. unseen_books aborted instantly: "no qwen3 server on 8090". The overnight
#    driver stops llama-server between stages to reclaim VRAM - correct for
#    the TTS stages, fatal for a stage that needs the server and does not
#    start one itself. It is started here before that stage and only there.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
LOG="$runtime/logs/overnight_20260818"
mkdir -p "$LOG"
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"
DEADLINE=$(date -d "2026-08-18 11:30" +%s)

note() { echo "[$(date -u +%FT%TZ)] $*"; }

commit_artifacts() {
    # Stage by path: this tree is shared with other sessions, and `git add -A`
    # would sweep in whatever they are mid-edit.
    git -C "$REPO" add ab_test_runtime/experiments/ >/dev/null 2>&1
    if ! git -C "$REPO" diff --cached --quiet; then
        git -C "$REPO" commit -q -m "Artifacts from the $1 stage

Committed by the morning chain so the dirty-tree gate does not refuse
the next stage on this stage's own output.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>" && note "committed $1 artifacts"
    fi
}

time_left() { echo $(( DEADLINE - $(date +%s) )); }

# ---- 1. The replication the -ay result needs, refused last night ----------
for limit in 800 1200 1600; do
    [ "$(time_left)" -lt 4200 ] && { note "STOP before $limit: $(time_left)s left"; break; }
    out="$runtime/experiments/respelling_e_row__ay_n${limit}.json"
    [ -e "$out" ] && { note "SKIP $limit (exists)"; continue; }
    note "START ay block to $limit"
    "$REPO/gpu_job.sh" "e_row_ay_n$limit" \
        timeout --signal=INT --kill-after=60s 4200 \
        "$python" -u "$REPO/app/experiments/measure_respellings.py" \
        --min-books 5 --only-e-row --e-spelling ay --limit "$limit" \
        --work "$runtime/respelling_e_row_ay" --out "$out" \
        > "$LOG/ay_n$limit.log" 2>&1 && note "OK $limit" || note "FAIL $limit"
    [ -f "$out" ] && "$python" "$REPO/app/experiments/pair_e_row.py" "$out" \
        >> "$runtime/reports/overnight_20260818/e_row_paired.txt" 2>&1
    commit_artifacts "e_row_ay_n$limit"
done

# ---- 2. unseen_books, this time with the server it needs ------------------
if [ "$(time_left)" -gt 5400 ]; then
    note "starting llama-server for unseen_books"
    "$REPO/ensure_llama_server.sh" > "$LOG/server.log" 2>&1 || note "server start failed"
    note "START unseen_books"
    timeout --signal=INT --kill-after=120s "$(time_left)" \
        "$REPO/run_chains/unseen_books.sh" > "$LOG/unseen_books_retry.log" 2>&1 \
        && note "OK unseen_books" || note "FAIL unseen_books rc=$?"
    commit_artifacts unseen_books
else
    note "SKIP unseen_books: only $(time_left)s left"
fi

note "MORNING CHAIN COMPLETE"
