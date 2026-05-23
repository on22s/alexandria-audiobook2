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
    output="$OUT_DIR/dataset_${stem}.zip"

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
        --lang en

    rc=$?
    if (( rc == 130 )); then
        echo ""
        echo "Wrapper exited 130 (user aborted). Stopping smoke run."
        echo "Stopped after $i/${#PAIRS[@]}" > "$OUT_DIR/ABORTED.flag"
        notify "Alexandria smoke aborted" "Stopped after $i/${#PAIRS[@]} books. See $OUT_DIR/"
        exit 130
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
