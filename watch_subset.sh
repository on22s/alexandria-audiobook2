#!/usr/bin/env bash
# watch_subset.sh — poll for the `prep` tmux session and notify when it exits.
# Runs as a detached background job; logs to test_corpus_output/watchdog.log.
# Safe to start while a run is already in progress.

OUT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)/test_corpus_output"
mkdir -p "$OUT_DIR"
LOG="$OUT_DIR/watchdog.log"
SESSION=prep
INTERVAL=60

started=$(date -Iseconds)
echo "[$started] Watchdog started, polling tmux session '$SESSION' every ${INTERVAL}s" > "$LOG"

# Wait until the session actually exists (gives the run a moment to come up if
# the watchdog started a hair faster than the tmux session).
for _ in 1 2 3 4 5; do
    tmux has-session -t "$SESSION" 2>/dev/null && break
    sleep 2
done

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
{
    echo "Watchdog detected completion: $finished"
    echo "Started polling:             $started"
    echo "Zip files in output:         $zip_count"
    ls -lh "$OUT_DIR"/*.zip 2>/dev/null | awk '{print "  " $NF " (" $5 ")"}'
} > "$OUT_DIR/DONE.flag"

# Desktop notification (graceful if no D-Bus session) + terminal bell.
notify-send --app-name "Alexandria subset" -u normal \
    "Alexandria subset complete" \
    "$zip_count zip(s) in $OUT_DIR. See DONE.flag for details." 2>/dev/null || true
printf '\a' >&2

echo "[$finished] Watchdog exiting." >> "$LOG"
