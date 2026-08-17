#!/bin/bash
# Hold the GPU queue so the card can be used for something else.
#
# WHY A FLAG AND NOT A KILL. The queue is hours long and every job in it is
# resumable - measure_respellings skips terms it already has, the re-gate skips
# adapters whose artifact exists, replay skips finished artifacts. So the cheap
# way to free the card is to stop STARTING work, not to destroy work in flight.
#
# PAUSING DOES NOT FREE VRAM BY ITSELF. A job already running keeps the card
# until it exits. `status` reports what is still resident so the difference
# between "queue is held" and "card is free" is visible rather than assumed -
# a paused queue with a job still running looks identical to a free card if
# you only check the flag.
#
# Usage:
#   ./gpu_pause.sh on          hold the queue; the running job finishes
#   ./gpu_pause.sh on --now    also interrupt the running job (see below)
#   ./gpu_pause.sh off         release the queue
#   ./gpu_pause.sh status      flag state, running job, VRAM in use
#
# --now IS NOT THE DEFAULT, deliberately. Chains skip a stage whose artifact
# already exists, and several jobs write their artifact incrementally as a
# checkpoint. Interrupting one leaves a partial artifact that a later run reads
# as finished. Interrupting is safe for the jobs that key their work per item;
# it is not universally safe, so it is opt-in and says so.
set -uo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
FLAG="${GPU_PAUSE_FLAG:-$REPO/ab_test_runtime/logs/gpu_paused}"
QLOG="${GPU_QLOG:-$REPO/ab_test_runtime/logs/gpu_jobq.log}"

stamp() { date -u +%FT%TZ; }

vram_used_mib() {
    rocm-smi --showmeminfo vram 2>/dev/null \
        | grep -im1 'total used memory' | grep -oE '[0-9]+' | tail -1 \
        | awk '{printf "%d", $1/1048576}'
}

running_job() {
    # The job that currently holds the lock, from the queue log: the last
    # START with no matching OK/FAILED after it.
    tail -40 "$QLOG" 2>/dev/null | awk '
        /START    / {name=$3}
        /OK       |FAILED   |REFUSED  |NO_VRAM  /  {name=""}
        END {print name}'
}

case "${1:-status}" in
  on)
    mkdir -p "$(dirname "$FLAG")"
    date -u +%FT%TZ > "$FLAG"
    echo "$(stamp) PAUSED   queue held by gpu_pause" >> "$QLOG"
    echo "queue paused: no further job will START until 'gpu_pause.sh off'."
    job=$(running_job)
    if [ -n "$job" ]; then
        if [ "${2:-}" = "--now" ]; then
            echo "interrupting the running job '$job' (SIGINT, graceful)."
            echo "WARNING: a job that writes its artifact incrementally may"
            echo "leave a partial file that a later chain reads as finished."
            pkill -INT -f "gpu_job.sh $job" 2>/dev/null
            echo "$(stamp) INTERRUPTED $job (gpu_pause --now)" >> "$QLOG"
        else
            echo "'$job' is still running and keeps the card until it exits."
            echo "run 'gpu_pause.sh status' to see when VRAM is actually free,"
            echo "or 'gpu_pause.sh on --now' to interrupt it."
        fi
    fi
    ;;
  off)
    rm -f "$FLAG"
    echo "$(stamp) RESUMED  queue released by gpu_pause" >> "$QLOG"
    echo "queue released; waiting jobs will start on their next check."
    ;;
  status)
    if [ -f "$FLAG" ]; then
        echo "queue: PAUSED since $(cat "$FLAG" 2>/dev/null)"
    else
        echo "queue: running"
    fi
    job=$(running_job)
    echo "running job: ${job:-none}"
    used=$(vram_used_mib)
    echo "VRAM in use: ${used:-unknown} MiB"
    if [ -f "$FLAG" ] && [ -n "$job" ]; then
        echo
        echo "NOTE: paused but '$job' still holds the card. The queue is held;"
        echo "the GPU is not free until that job exits."
    fi
    ;;
  *)
    echo "usage: gpu_pause.sh [on [--now] | off | status]" >&2
    exit 2
    ;;
esac
