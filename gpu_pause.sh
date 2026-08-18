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
    # The job that currently holds the lock: the last START in the queue log
    # with no matching terminal marker after it, CONFIRMED against a live
    # process before it is reported.
    #
    # The log alone is a record of what was SUPPOSED to happen. On 2026-08-17
    # the e_row_e arm finished - artifact complete, 400 of 400, and the next
    # arm queued one second later - and no OK or FAILED line was ever written.
    # Cause still unknown. The consequence was not: `status` went on reporting
    # a finished job as running for 24 minutes, so "the card is still busy"
    # was false, and a paused-for-gaming user was told to keep waiting for
    # nothing. A missing marker must degrade to "cannot tell", never to a
    # confident wrong answer.
    local name
    name=$(logged_job)
    [ -n "$name" ] && job_is_live "$name" && echo "$name"
    return 0
}

logged_job() {
    tail -200 "$QLOG" 2>/dev/null | awk '
        /START    / {name=$3}
        # INTERRUPTED and STOPPED are terminal too. A marker set that lags the
        # writer is how a finished job goes on looking busy.
        /OK       |FAILED   |REFUSED  |NO_VRAM  |KILLED   |LOCK_FAILED|INTERRUPTED |STOPPED  / {name=""}
        END {print name}'
}

job_is_live() {
    # -f, because the wrapper is only identifiable by its full command line.
    # The trailing space keeps `e_row_e` from matching `e_row_ei`, and pgrep -f
    # matching THIS script's own command line is the accident that killed my
    # shell twice in this repo, so exclude our own pid and our parent's.
    pgrep -f "gpu_job.sh $1 " 2>/dev/null \
        | grep -qv -e "^$$\$" -e "^$PPID\$"
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
    # WHAT IS WAITING, not just what is running. Until gpu_job.sh wrote pending
    # markers there was no way to ask this: queued jobs are blocked processes
    # and a chain that died left the same evidence as one patiently waiting -
    # the confusion behind two idle stretches on 2026-08-18. task-spooler has
    # `ts -l` for exactly this; these markers are the small version.
    pending_dir="${GPU_PENDING_DIR:-$REPO/ab_test_runtime/logs/pending}"
    if [ -d "$pending_dir" ]; then
        for marker in "$pending_dir"/*; do
            [ -e "$marker" ] || continue
            marker_name=$(cut -f1 "$marker" 2>/dev/null)
            marker_pid=$(cut -f2 "$marker" 2>/dev/null)
            # A marker whose process is gone is a LIE, and saying so is the
            # point: it means a chain died holding a place in the queue.
            if kill -0 "$marker_pid" 2>/dev/null; then
                [ "$marker_name" = "$job" ] || echo "  waiting: $marker_name (pid $marker_pid)"
            else
                echo "  STALE:   $marker_name (pid $marker_pid is gone; chain died)"
            fi
        done
    fi
    logged=$(logged_job)
    if [ -z "$job" ] && [ -n "$logged" ]; then
        # Say which of the two sources disagreed rather than picking one
        # silently: the log is what was supposed to happen, the process table
        # is what is happening.
        echo "note: the queue log's last START is '$logged' with no result"
        echo "      line, but no such process exists - it finished without"
        echo "      being recorded. The card is free."
    fi
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
