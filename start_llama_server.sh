#!/bin/bash
# Start llama.cpp for the LLM side of this project, idempotently.
#
# WHY THIS EXISTS. three_pass_vs_single ran on 2026-08-06 and produced nothing:
# no LLM server was up, every book failed with "Connection error", and the run
# still logged OK because the harness exited 0 after writing an artifact that
# recorded those failures. The comparison has never actually run, and the
# reason was that starting the server was an undocumented manual step.
#
# Defaults to the 12 GB Qwen2.5-14B in this repo, on the GPU. Set LLAMA_NGL=0
# to keep it on CPU when the card is busy with TTS: a 14B Q6_K answers on CPU,
# slowly, which is enough to verify wiring and not enough to run a book.
set -uo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
source "$REPO/run_chains/lib/server_cleanup.sh" || exit 2
BIN="${LLAMA_BIN:-$(command -v llama-server)}"
MODEL="${LLAMA_MODEL:-$REPO/Qwen2.5-14B-Instruct-Q6_K.gguf}"
PORT="${LLAMA_PORT:-8090}"
# 32768 with an f16 KV cache wanted a 6 GiB buffer on top of 12 GB of weights
# and OOM'd on this 15.9 GB card - three_pass_vs_single died on it at
# 11:03 UTC on 2026-08-06 after a silent ten-minute readiness wait. 16384 with
# a q8_0 cache needs about 1.5 GiB and leaves headroom. Raise LLAMA_CTX only
# with a card that has the room.
CTX="${LLAMA_CTX:-16384}"
KV="${LLAMA_KV:-q8_0}"
NGL="${LLAMA_NGL:-99}"
LOG="${LLAMA_LOG:-$REPO/ab_test_runtime/logs/llama_server.log}"
ADAPTER="${1:-${LLAMA_ADAPTER:-}}"
URL="http://127.0.0.1:${PORT}/v1/models"
READY_PYTHON="$(command -v python3)"
READY_VALIDATOR="$REPO/app/llama_server_process.py"
ARGS=(-m "$MODEL" --port "$PORT" --host 127.0.0.1 -ngl "$NGL" -c "$CTX"
      -ctk "$KV" -ctv "$KV" --parallel 1)
if [ -n "$ADAPTER" ]; then
    ARGS+=(--lora "$ADAPTER")
fi

if [ -z "$BIN" ] || [ ! -x "$BIN" ]; then
    echo "no llama-server binary (set LLAMA_BIN)" >&2
    exit 2
fi
if [ ! -f "$MODEL" ]; then
    echo "no model at $MODEL (set LLAMA_MODEL)" >&2
    exit 2
fi
if [ -n "$ADAPTER" ] && [ ! -f "$ADAPTER" ]; then
    echo "no adapter at $ADAPTER" >&2
    exit 2
fi

if [ ! -x "$READY_PYTHON" ] || [ ! -f "$READY_VALIDATOR" ] || [ ! -f "$REPO/app/llama_server_identity.py" ]; then
    echo "server launch identity validator unavailable" >&2
    exit 2
fi
mkdir -p "$(dirname "$LOG")" || exit 2
# Serialize inspection and launch; the server must not inherit this lock.
if ! exec 8>"${LOG}.lock" || ! flock 8; then
    echo "cannot acquire server lifecycle lock at ${LOG}.lock" >&2
    exit 2
fi

# -f is load-bearing. llama-server answers 503 while it loads weights and
# `curl -s` exits 0 on a 503 - which is exactly how an eval once fired at a
# server that was not up yet and died. Without -f this check is decorative.
healthy() { curl -sf --max-time 5 "$URL" >/dev/null 2>&1; }
ready() {
    local models props adapters
    models=$(curl -sf --max-time 5 "$URL" 2>/dev/null) || return 1
    props=$(curl -sf --max-time 5 "${URL%/v1/models}/props" 2>/dev/null) || return 1
    adapters=$(curl -sf --max-time 5 "${URL%/v1/models}/lora-adapters" 2>/dev/null) || return 1
    printf '%s\0' "$models" "$props" "$adapters" | \
        "$READY_PYTHON" -B "$REPO/app/llama_server_identity.py" --default-alias "$MODEL" "$ADAPTER" "$CTX" 1 1 || return 1
    "$READY_PYTHON" -B "$READY_VALIDATOR" --check-launch "$PORT" "$BIN" "${ARGS[@]}"
}

if ready; then
    echo "llama-server already up on :$PORT"
    exit 0
fi

if healthy; then
    echo "healthy service on :$PORT does not match requested launch; refusing reuse" >&2
    exit 2
fi
# Preserve diagnostics from every previous attempt before starting a new one.
if [ -f "$LOG" ]; then
    mv -- "$LOG" "$LOG.$(date +%Y%m%d_%H%M%S).$$" || exit 2
fi
nohup "$BIN" "${ARGS[@]}" > "$LOG" 2>&1 8>&- &
PID=$!

for _ in $(seq 1 120); do
    if ready; then
        echo "llama-server ready on :$PORT (ngl=$NGL, ctx=$CTX, kv=$KV${ADAPTER:+, lora=$(basename "$ADAPTER")})"
        exit 0
    fi
    # A dead process will never become ready. Without this the script waited
    # the full ten minutes on a server that had already OOM'd in three seconds,
    # and reported only "never ready" - true, but not the reason.
    if ! kill -0 "$PID" 2>/dev/null; then
        echo "SERVER_DIED after $(( SECONDS ))s on :$PORT" >&2
        grep -iE 'error|failed|out of memory' "$LOG" | tail -4 >&2
        stop_owned_server "$PID" || exit 1
        exit 1
    fi
    sleep 5
done
echo "SERVER_NEVER_READY on :$PORT; see $LOG" >&2
stop_owned_server "$PID" || exit 1
exit 1
