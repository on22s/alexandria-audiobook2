#!/usr/bin/env bash
# watch_subset.sh — poll for the `prep` tmux session and notify when it exits.
# Runs as a detached background job; logs to test_corpus_output/watchdog.log.
# Safe to start while a run is already in progress.

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
source "$SCRIPT_DIR/run_chains/lib/subset_outputs.sh" || exit 1
OUT_DIR="$SCRIPT_DIR/test_corpus_output"
mkdir -p "$OUT_DIR"
LOG="$OUT_DIR/watchdog.log"
SESSION=prep
INTERVAL=60

started=$(date -Iseconds)
echo "[$started] Watchdog started, polling tmux session '$SESSION' every ${INTERVAL}s" > "$LOG"

# Wait until the session actually exists (gives the run a moment to come up if
# the watchdog started a hair faster than the tmux session).
observed=0
for _ in 1 2 3 4 5; do
    if tmux has-session -t "$SESSION" 2>/dev/null; then
        observed=1
        break
    fi
    sleep 2
done
if [ "$observed" -eq 0 ]; then
    message="tmux session '$SESSION' never appeared; completion is unverified."
    echo "[$(date -Iseconds)] $message" >> "$LOG"
    echo "$message" >&2
    exit 1
fi

# Now poll until it's gone.
while tmux has-session -t "$SESSION" 2>/dev/null; do
    sleep "$INTERVAL"
done

finished=$(date -Iseconds)
zip_count=$(ls "$OUT_DIR"/*.zip 2>/dev/null | wc -l)
{
    echo "[$finished] tmux session '$SESSION' is gone."
    echo "  Zip files in $OUT_DIR: $zip_count"
} >> "$LOG"

# Sentinel file so a quick `ls test_corpus_output/` shows DONE.flag at a glance.
if ! {
    echo "Watchdog detected completion: $finished"
    echo "Started polling:             $started"
    echo "Zip files in output:         $zip_count"
    print_subset_outputs "$OUT_DIR"
} > "$OUT_DIR/DONE.flag"; then
    rm -f -- "$OUT_DIR/DONE.flag"
    echo "Could not record subset outputs." >&2
    exit 1
fi

# Desktop notification (graceful if no D-Bus session) + terminal bell.
notify-send --app-name "Alexandria subset" -u normal \
    "Alexandria subset complete" \
    "$zip_count zip(s) in $OUT_DIR. See DONE.flag for details." 2>/dev/null || true
printf '\a' >&2

echo "[$finished] Watchdog exiting." >> "$LOG"
