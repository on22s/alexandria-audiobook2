#!/bin/bash
# Runs only inside the queue's descendant owner, which retains the GPU lease.
set -uo pipefail
WRAPPER="$1" NAME="$2"; shift 2
REPO="$(dirname "$WRAPPER")"
bash "$WRAPPER" --check-lock-owner "${ALEXANDRIA_GPU_LOCK_PID:-0}" "${ALEXANDRIA_GPU_LOCK_FD:-9}" || exit 4
source "$REPO/run_chains/lib/server_cleanup.sh" || exit 4
# Use the same lifecycle transaction as ensure_llama_server.sh so cleanup
# cannot race its inspection/replacement/stamp publication.
if ! exec 8>"$HOME/.llama_server_adapter.lock" || ! flock 8; then
    echo "gpu_job: cannot acquire server lifecycle lock" >&2
    exit 4
fi
BIN="$(get_llama_server_binary)"
PYTHON="$(command -v python3)"
echo "reclaiming VRAM from configured llama-server before $NAME"
"$PYTHON" -B "$REPO/app/llama_server_process.py" "${LLAMA_PORT:-8090}" "$BIN" || exit 4
wait_for_server_vram_release || true
# Capacity decisions use the queue's one policy, even after unknown telemetry
# or the existing polling cap. The owner still holds its lease throughout.
bash "$WRAPPER" --check-vram "$NAME" || exit $?
unset GPU_RECLAIM_VRAM
# Do not hand lifecycle FD8 to a long-lived worker.
exec 8>&-
exec "$@"
