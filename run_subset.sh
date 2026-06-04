#!/usr/bin/env bash
# run_subset.sh — focused 4-book corpus run (post-WAV-wrap-fix).
# Pairs are hardcoded so the run is reproducible regardless of what's in
# Desktop/books at any given moment.

set -u

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &> /dev/null && pwd)
OUT_DIR="$SCRIPT_DIR/test_corpus_output"
MODEL="Qwen2.5-14B-Instruct-Q6_K.gguf"
FALLBACK="Gemma-4-E4B-Uncensored-HauhauCS-Aggressive-Q8_K_P.gguf"
mkdir -p "$OUT_DIR"

# audio | source pairs (separator is `|`)
PAIRS=(
    "/home/fakemitch/Desktop/New folder/J Michael Tatum Spice and Wolf, Vol. 10-converted.wav|/home/fakemitch/Desktop/books/Spice and Wolf - Volume 10 [Yen Press][Kobo].epub"
    "/home/fakemitch/Desktop/New folder/Cliff Kurt Mushoku Tensei-converted.wav|/home/fakemitch/Desktop/books/Mushoku Tensei - Volume 01.epub"
    "/home/fakemitch/Desktop/New folder/Cherami Leigh Cyberpunk 2077-converted.wav|/home/fakemitch/Desktop/books/Cyberpunk 2077.epub"
    "/home/fakemitch/Desktop/New folder/Michael Kramer The Hero of Ages-converted.wav|/home/fakemitch/Desktop/books/Hero of Ages .epub"
)

notify() {
    # Best-effort desktop notification + audible bell. Failures are ignored
    # so the wrapper still completes if the desktop session is gone.
    local title="$1"; local body="$2"
    notify-send --app-name "Alexandria subset" -u normal "$title" "$body" 2>/dev/null || true
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
        echo "Wrapper exited 130 (user aborted). Stopping subset run."
        echo "Stopped after $i/${#PAIRS[@]}" > "$OUT_DIR/ABORTED.flag"
        notify "Alexandria subset aborted" "Stopped after $i/${#PAIRS[@]} books. See $OUT_DIR/"
        exit 130
    fi
done

elapsed=$(( $(date +%s) - start_epoch ))
hrs=$(( elapsed / 3600 ))
mins=$(( (elapsed % 3600) / 60 ))
echo ""
echo "All ${#PAIRS[@]} runs complete in ${hrs}h ${mins}m."

# Sentinel + notification so the user knows it's done without polling tmux.
{
    echo "Completed: $(date -Iseconds)"
    echo "Elapsed:   ${hrs}h ${mins}m"
    echo "Books:     ${#PAIRS[@]}"
    echo "Outputs:"
    ls -lh "$OUT_DIR"/*.zip 2>/dev/null | awk '{print "  " $NF, "(" $5 ")"}'
} > "$OUT_DIR/DONE.flag"

notify "Alexandria subset complete" "${#PAIRS[@]} books done in ${hrs}h ${mins}m. See $OUT_DIR/"
