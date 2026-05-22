#!/usr/bin/env bash
# run_with_restart.sh — auto-resume wrapper for alexandria_preparer / batch.
#
# Why: a 60-hour LLM annotation run can crash for many reasons (driver hiccup,
# OOM, power blip, accidental Ctrl-C, my-Claude-session-ran-out-of-credits-
# halfway-through-helping-you). The preparer already writes a per-segment
# checkpoint after every chunk, so the only thing missing was a process
# supervisor that re-launches with --resume until the run completes cleanly.
#
# Usage:
#   ./run_with_restart.sh <preparer-args...>
#
# Examples:
#   ./run_with_restart.sh \
#     --audio "/home/fakemitch/Desktop/audiobook.wav" \
#     --model models/Qwen2.5-14B-Instruct-Q6_K.gguf \
#     --source "/home/fakemitch/Desktop/books/book.epub" \
#     --output "/home/fakemitch/Desktop/output.zip"
#
# Behaviour:
#   - First attempt: runs without --resume (fresh start).
#   - Every subsequent attempt: adds --resume so the preparer picks up where
#     the previous attempt's checkpoint left off.
#   - Exits successfully when the preparer exits 0.
#   - Bails out after MAX_RETRIES non-zero exits in a row (default 20),
#     so a hard configuration error doesn't make this loop forever.
#   - Logs each attempt to stdout with a timestamp so you can see in the
#     terminal scrollback what happened across restarts.
#   - SIGINT (Ctrl-C) propagates to the preparer; pressing Ctrl-C TWICE
#     within 5 s breaks out of the loop completely (otherwise the wrapper
#     would restart after a single Ctrl-C).
#
# Notes:
#   - Run this in a tmux/screen session so closing your terminal doesn't kill
#     the wrapper: `tmux new -s prep './run_with_restart.sh <args>'`
#   - The preparer's source-marker check protects against accidentally
#     resuming into a different audio file's progress — see PREPARER_GUIDE.md.

set -u
shopt -s -o pipefail

MAX_RETRIES=${MAX_RETRIES:-20}
BACKOFF_SECONDS=${BACKOFF_SECONDS:-10}
SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
PREPARER="$SCRIPT_DIR/alexandria_preparer_rocm_compatible.py"
PYTHON="$SCRIPT_DIR/app/env/bin/python"

if [[ ! -x "$PYTHON" ]]; then
    echo "ERROR: python not found at $PYTHON" >&2
    echo "       run this from the alexandria-audiobook2 project directory" >&2
    exit 2
fi
if [[ ! -f "$PREPARER" ]]; then
    echo "ERROR: preparer script not found at $PREPARER" >&2
    exit 2
fi

# Detect a double-Ctrl-C so the user has a clean way to abort the whole loop.
LAST_INT=0
on_int() {
    local now
    now=$(date +%s)
    if (( now - LAST_INT < 5 )); then
        echo ""
        echo "[$(date '+%H:%M:%S')] Two SIGINTs within 5s — breaking out of the restart loop."
        echo "                 Partial work is preserved in dataset_temp/; rerun with --resume to continue."
        exit 130
    fi
    LAST_INT=$now
    echo ""
    echo "[$(date '+%H:%M:%S')] SIGINT received — preparer should be shutting down."
    echo "                 Hit Ctrl-C again within 5s to abort the wrapper too, otherwise it'll restart."
}
trap on_int INT

attempt=0
extra_args=()
while true; do
    attempt=$((attempt + 1))
    if (( attempt > MAX_RETRIES )); then
        echo ""
        echo "[$(date '+%H:%M:%S')] ✗ Gave up after $MAX_RETRIES consecutive failures."
        echo "                 Last preparer log:"
        # Show the tail of the newest preparer log so the user has an immediate
        # diagnostic clue without digging.
        latest_log=$(ls -t "$SCRIPT_DIR/logs/alexandria_preparer_"*.log 2>/dev/null | head -1)
        if [[ -n "$latest_log" ]]; then
            echo "                 ${latest_log}"
            tail -20 "$latest_log" | sed 's/^/                   /'
        fi
        exit 1
    fi

    echo ""
    echo "─────────────────────────────────────────────────────────────────────"
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Attempt $attempt/$MAX_RETRIES"
    if (( attempt > 1 )); then
        echo "                       Forcing --resume from previous checkpoint"
    fi
    echo "─────────────────────────────────────────────────────────────────────"

    # Build the argv. Pass through everything the user supplied, plus --resume
    # from the second attempt onward.
    if (( attempt == 1 )); then
        "$PYTHON" "$PREPARER" "$@"
        rc=$?
    else
        # Make --resume idempotent — only add it if the user didn't already
        # supply it (e.g. they're rerunning after an aborted previous run).
        already_resume=0
        for arg in "$@"; do
            if [[ "$arg" == "--resume" ]]; then
                already_resume=1
                break
            fi
        done
        if (( already_resume == 1 )); then
            "$PYTHON" "$PREPARER" "$@"
        else
            "$PYTHON" "$PREPARER" "$@" --resume
        fi
        rc=$?
    fi

    if (( rc == 0 )); then
        echo ""
        echo "[$(date '+%H:%M:%S')] ✓ Preparer exited cleanly. Done."
        exit 0
    fi

    # SIGINT (130) — if user pressed Ctrl-C once, give them 5s to hit it again
    # and abort. If they didn't, fall through to the normal restart path.
    if (( rc == 130 )); then
        echo ""
        echo "[$(date '+%H:%M:%S')] Preparer was interrupted (exit 130). Restarting in $BACKOFF_SECONDS s..."
        echo "                 Press Ctrl-C again now to abort the wrapper."
    else
        echo ""
        echo "[$(date '+%H:%M:%S')] ✗ Preparer exited $rc. Restarting in $BACKOFF_SECONDS s..."
    fi
    sleep "$BACKOFF_SECONDS"
done
