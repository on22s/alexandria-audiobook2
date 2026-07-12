#!/usr/bin/env bash
# run_smoke.sh — single-book smoke test for preparer iteration.
# Runs Full Metal Panic (~6.5 hr audio, ~19 hr wall) so the durability
# fixes from c6529e7 (orphan WAV sweep, per-chunk fsync, tolerant
# checkpoint loader) get exercised on a real book without committing
# to the 6-day subset.

set -u

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
OUT_DIR="$SCRIPT_DIR/test_corpus_output"
MODEL="Qwen2.5-14B-Instruct-Q6_K.gguf"
FALLBACK="Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf"
mkdir -p "$OUT_DIR"

PAIRS=(
    "/home/fakemitch/Desktop/New folder/Luci Christian Full Metal Panic-converted.wav|/home/fakemitch/Desktop/books/Full Metal Panic.epub"
    "/home/fakemitch/Desktop/New folder/Cherami Leigh Cyberpunk 2077-converted.wav|/home/fakemitch/Desktop/books/Cyberpunk 2077.epub"
    "/home/fakemitch/Desktop/New folder/Brittney Karbowski Reincarnated Slime-converted.wav|/home/fakemitch/Desktop/books/Ascendance of a Bookworm Part 1 volume 1.epub"
    "/home/fakemitch/Desktop/New folder/Cliff Kurt Mushoku Tensei-converted.wav|/home/fakemitch/Desktop/books/86--Eighty-Six V11 - Dies Passionis.epub"
    "/home/fakemitch/Desktop/New folder/Cristina Vee Nekomonogatari-converted.wav|/home/fakemitch/Desktop/books/A Natural History of Dragons.epub"
)

notify() {
    local title="$1"; local body="$2"
    notify-send --app-name "Alexandria smoke" -u normal "$title" "$body" 2>/dev/null || true
    printf '\a' >&2
}

start_epoch=$(date +%s)
i=0
for pair in "${PAIRS[@]}"; do
    i=$((i+1))
    audio="${pair%%|*}"
    source="${pair#*|}"
    stem=$(basename "$audio" | sed 's/\.[^.]*$//')
    # Pass the generic 'dataset.zip' placeholder so the preparer's auto-naming
    # derives the final filename from the --source ePub's title/author. The
    # directory portion is preserved by maybe_autoname_output().
    output="$OUT_DIR/dataset.zip"

    echo ""
    echo "═════════════════════════════════════════════════════════════════"
    echo "[$i/${#PAIRS[@]}] $stem"
    echo "  audio  : $audio"
    echo "  source : $source"
    echo "  output : $output"
    echo "═════════════════════════════════════════════════════════════════"

    "$SCRIPT_DIR/run_with_restart.sh" \
        --audio "$audio" \
        --model "$MODEL" \
        --fallback-model "$FALLBACK" \
        --source "$source" \
        --output "$output" \
        --chunk-size 10.0 \
        --limit 5 \
        --lang en

    rc=$?
    if (( rc == 130 )); then
        echo ""
        echo "Wrapper exited 130 (user aborted). Stopping smoke run."
        echo "Stopped after $i/${#PAIRS[@]}" > "$OUT_DIR/ABORTED.flag"
        notify "Alexandria smoke aborted" "Stopped after $i/${#PAIRS[@]} books. See $OUT_DIR/"
        exit 130
    fi
    if (( rc != 0 )); then
        echo "Wrapper failed for book $i (exit $rc)." >&2
        exit "$rc"
    fi
done

elapsed=$(( $(date +%s) - start_epoch ))
hrs=$(( elapsed / 3600 ))
mins=$(( (elapsed % 3600) / 60 ))
echo ""
echo "Smoke run complete in ${hrs}h ${mins}m."

{
    echo "Completed: $(date -Iseconds)"
    echo "Elapsed:   ${hrs}h ${mins}m"
    echo "Books:     ${#PAIRS[@]}"
    echo "Outputs:"
    ls -lh "$OUT_DIR"/*.zip 2>/dev/null | awk '{print "  " $NF, "(" $5 ")"}'
} > "$OUT_DIR/DONE.flag"

notify "Alexandria smoke complete" "${#PAIRS[@]} book(s) done in ${hrs}h ${mins}m. See $OUT_DIR/"
