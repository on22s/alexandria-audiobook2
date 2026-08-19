#!/bin/bash
# Idempotent llama-server for a given LoRA adapter.
#
# OWNERSHIP IS THE BUG THIS FIXES. Previously one script started the server and
# a different one pkilled it on exit, so whether a server existed depended on
# which script happened to run last. A retry died with SERVER_GONE for exactly
# that reason on 2026-08-04.
#
# The inversion: the CALLER never kills the server. This script reuses a
# healthy one already serving the requested adapter, and replaces it only when
# something different is needed. A server therefore outlives the job that
# started it, which is what lets consecutive evals share one load.
#
# Usage:
#   ./ensure_llama_server.sh [adapter.gguf]
#
# Environment:
#   LLAMA_BIN     llama-server binary      (default: llama-server from PATH)
#   LLAMA_MODEL   base GGUF                (required)
#   LLAMA_PORT    port                     (default 8090)
#   LLAMA_CTX     context length           (default 32768)
#   LLAMA_LOG     server log path          (default ~/llama_server.log)
set -uo pipefail

ADAPTER="${1:-}"
# RESOLVE FROM PATH FIRST. This defaulted to a hand-built
# ~/llama.cpp/build/bin/llama-server, and when that path was empty the script
# exited 2 - which read as "no llama.cpp on this machine" while a maintained
# one sat on PATH the whole time. On this box llama-server comes from the
# `llama.cpp-hip` package, so `pacman -Syu` keeps it current; preferring a
# hand-built copy would silently pin every run to whatever was compiled once
# and never updated, and would quietly change the engine underneath a
# comparison whose earlier arms used the packaged build.
#
# LLAMA_BIN still overrides, for deliberately testing a specific build.
BIN="${LLAMA_BIN:-$(command -v llama-server 2>/dev/null || echo "$HOME/llama.cpp/build/bin/llama-server")}"
# ONE DEFAULT, HERE. Four chains pasted the same GGUF path inline to satisfy
# the "required" check below, and every caller that forgot - unseen_books.sh,
# which starts a server only when none is running - aborted with
# "LLAMA_MODEL is required" at the moment it needed one, wasting the slot. The
# script that serves the model is the right place to know which model that is;
# ALEXANDRIA_QWEN3_MODEL still overrides, and an explicit LLAMA_MODEL still
# wins over both.
DEFAULT_MODEL="${ALEXANDRIA_QWEN3_MODEL:-$HOME/.lmstudio/models/lmstudio-community/Qwen3-14B-GGUF/Qwen3-14B-Q4_K_M.gguf}"
MODEL="${LLAMA_MODEL:-$DEFAULT_MODEL}"
PORT="${LLAMA_PORT:-8090}"
CTX="${LLAMA_CTX:-32768}"
LOG="${LLAMA_LOG:-$HOME/llama_server.log}"
STAMP="$HOME/.llama_server_adapter"
URL="http://127.0.0.1:${PORT}/v1/models"

[ -x "$BIN" ] || { echo "ensure_llama_server: no binary at $BIN" >&2; exit 2; }
[ -n "$MODEL" ] || { echo "ensure_llama_server: LLAMA_MODEL is required" >&2; exit 2; }
# A default that does not exist is worse than no default: it would start a
# server against a missing file and fail later, somewhere less obvious.
[ -f "$MODEL" ] || { echo "ensure_llama_server: no model file at $MODEL" >&2; exit 2; }
[ -f "$MODEL" ] || { echo "ensure_llama_server: no model at $MODEL" >&2; exit 2; }
[ -n "$ADAPTER" ] && [ ! -f "$ADAPTER" ] && {
    echo "ensure_llama_server: no adapter at $ADAPTER" >&2; exit 2; }

# -f is load-bearing. llama-server answers 503 while it loads weights, and
# `curl -s` exits 0 on a 503 - which is precisely how an eval once fired at a
# server that was not up yet and died on HTTP 503. Without -f this whole
# readiness check is decorative.
ready() { curl -sf --max-time 5 "$URL" >/dev/null 2>&1; }

if ready && [ "$(cat "$STAMP" 2>/dev/null)" = "$ADAPTER" ]; then
    echo "ensure_llama_server: reusing server already serving ${ADAPTER:-base}"
    exit 0
fi

# -x, NOT -f. `pkill -f llama-server` matches ANY command line containing the
# string, including the shell that invoked this script - it killed a test
# harness mid-command here, exit 144, before the server was ever launched. The
# exact-name form matches the process, which is what was meant.
pkill -x llama-server 2>/dev/null
sleep 5
# REASONING OFF, AND AN ALIAS. Both were missing here. Without them the
# PR #308 remeasurement died on chunk 4/90:
#
#   Warning: Could not find JSON array in SEGMENT response (attempt 1)
#   Token escalation exhausted: effective budget cannot grow beyond 512
#   Error: pass 1 (segment) failed on chunk 4/90
#
# Qwen3 emits reasoning before its answer, so a pass that asks for a JSON array
# spends its budget thinking and returns prose. Every structured-output pass in
# this repo needs thinking off; prose passes do not care, which is why a
# conversational smoke test looks fine on a server that cannot do the work.
#
# `--reasoning off` is llama.cpp's own flag for this, not the
# `--chat-template-kwargs '{"enable_thinking":false}'` that
# run_chains/pdnc_context_evidence.sh uses. That form works but is
# template-specific - it depends on the Qwen3 Jinja template honouring that
# key - where --reasoning is the documented, model-agnostic control.
#
# Measured on this box, same prompt asking for a JSON array:
#     reasoning on  -> 213 completion tokens, 669 reasoning characters
#     reasoning off ->   5 completion tokens, 0, correct at max_tokens=64
# That 208-token gap is the whole failure: the segment pass caps at 512 over a
# long chunk, so with reasoning on there is no room left for the array.
#
# The alias makes the served id match config.json's model_name instead of the
# full .gguf path. llama.cpp ignores the field on requests, so it works either
# way - but provenance records what was served, and "qwen3-14b" is the name
# every artifact already uses.
ARGS=(-m "$MODEL" --port "$PORT" --host 127.0.0.1 -ngl 99 -c "$CTX" --parallel 1
      --alias "${LLAMA_ALIAS:-qwen3-14b}")
if [ "${LLAMA_THINKING:-0}" = "1" ]; then
    echo "ensure_llama_server: WARNING - reasoning ENABLED; structured-output" >&2
    echo "ensure_llama_server: passes (segment/attribute) will likely fail." >&2
else
    ARGS+=(--reasoning off)
fi
[ -n "$ADAPTER" ] && ARGS+=(--lora "$ADAPTER")
"$BIN" "${ARGS[@]}" > "$LOG" 2>&1 &

for _ in $(seq 1 120); do
    if ready; then
        printf '%s' "$ADAPTER" > "$STAMP"
        echo "ensure_llama_server: ready for ${ADAPTER:-base}"
        exit 0
    fi
    sleep 10
done

# Fail loudly rather than letting the caller run against nothing.
echo "ensure_llama_server: SERVER_NEVER_READY for ${ADAPTER:-base}; see $LOG" >&2
exit 1
