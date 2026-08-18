#!/usr/bin/bash
# Run unseen_books with the server it needs, until the machine is wanted back.
#
# A NEW FILE ON PURPOSE. The morning chain died at 11:25Z with "syntax error
# near unexpected token `done'" because it was EDITED WHILE RUNNING: bash reads
# a script incrementally, by byte offset, so rewriting the file under a running
# shell makes it resume at a meaningless position. The queue then sat idle for
# an hour. Never edit a running chain - copy it, or write a new one.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
LOG="$runtime/logs/overnight_20260818"
mkdir -p "$LOG"
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"
DEADLINE=$(date -d "2026-08-18 11:20" +%s)

note() { echo "[$(date -u +%FT%TZ)] $*"; }
time_left() { echo $(( DEADLINE - $(date +%s) )); }

note "starting llama-server"
LLAMA_MODEL="${ALEXANDRIA_QWEN3_MODEL:-/home/fakemitch/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf}" \
    "$REPO/ensure_llama_server.sh" > "$LOG/server2.log" 2>&1 \
    && note "server up" || note "server start FAILED (unseen_books will abort)"

# REQUIRE_VRAM_GB=0, DELIBERATELY. The VRAM gate refused this at 12:24Z:
# 1642 MiB free, needs 4096. The 14 GB was llama-server - the server THIS JOB
# NEEDS, started fifteen seconds earlier by this same script. The gate exists
# to stop a TTS or training job OOMing against a stale server holding memory
# nobody is using; an LLM job that talks to that server allocates almost
# nothing itself and wants it resident. Refusing here is the gate answering a
# question that does not apply, which is what its own error message offers
# this override for ("if this job is small").
#
# This is not a licence to waive it elsewhere: a stage that loads a TTS or
# LoRA model must still pass, and the 14 adapters lost on 2026-08-17 are why.
#
# Books are ordered smallest first inside the chain, so a truncated run still
# finishes whole books rather than leaving four half-done ones.
note "START unseen_books with $(time_left)s"
timeout --signal=INT --kill-after=120s "$(time_left)" \
    env REQUIRE_VRAM_GB=0 "$REPO/run_chains/unseen_books.sh" > "$LOG/unseen_books_run.log" 2>&1 \
    && note "OK unseen_books" || note "FAIL unseen_books rc=$?"

git -C "$REPO" add ab_test_runtime/experiments/ >/dev/null 2>&1
git -C "$REPO" diff --cached --quiet || git -C "$REPO" commit -q -m "Artifacts from the unseen_books run

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>" && note "artifacts committed"
note "COMPLETE"
