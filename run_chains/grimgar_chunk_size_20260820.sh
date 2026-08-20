#!/usr/bin/bash
# grimgar06 has failed four times. The cause is now visible; test the fix.
#
# WHAT THE LOG ACTUALLY SAYS. Every attempt dies the same way: a chunk fails
# quality validation because the model returned only ~86% of the source. The
# run on 2026-08-20 reported source_token_recall 0.856 and 0.869 against a 0.90
# floor, naming the passages that went missing. The failures cluster on chunks
# 27 and 29 of 70 across four attempts, so this is not the stochastic collapse
# it was filed as - a few chunks of this book reliably lose about a seventh of
# their content.
#
# THE HYPOTHESIS. Chunk size is 6,000 characters and the failing chunk is 5,870
# - near the top of the range. A shorter chunk asks the model to hold less at
# once, and recall is a coverage problem rather than a comprehension one.
#
# It is a hypothesis, not a diagnosis: it could equally be something about that
# passage's formatting, in which case a smaller chunk will fail too and the
# next place to look is the text itself. Either outcome is worth more than a
# fifth identical retry.
#
# The book is regenerated to a SEPARATE output so the failing run stays on disk
# for comparison, and 3,000 is run before 4,000 so the cheaper decisive answer
# comes first.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
inputs="$runtime/results/collect_all_20260722-155801/inputs"
STAGE_LOG_DIR="$runtime/logs/grimgar_chunk_size_20260820"
out="$runtime/grimgar_chunk_size"
mkdir -p "$STAGE_LOG_DIR" "$out"
source "$REPO/run_chains/lib/stage.sh"

running() { pgrep -f "run_chains/$1" 2>/dev/null | grep -qv -e "^$$\$" -e "^$PPID\$"; }
for chain in overnight_20260820b.sh settle_2_6_20260820.sh second_english_eval_20260820.sh; do
    stage_note "waiting for $chain"
    while running "$chain"; do sleep 120; done
done

for size in 3000 4000; do
    run_stage "grimgar06_chunk$size" 5h --needs-vram -- \
        env REQUIRE_LLM=1 REQUIRE_VRAM_GB=0 \
        "$REPO/gpu_job.sh" "grimgar_chunk$size" \
        timeout --signal=INT --kill-after=120s 16200 \
        "$python" -u "$REPO/app/generate_script.py" "$inputs/grimgar06.txt" \
        --chunk-size "$size" \
        --output "$out/grimgar06_chunk$size.json"
    stage_commit_artifacts "grimgar06_chunk$size" "$REPO"

    # Stop at the first size that produces the book. Running the second after a
    # success would measure nothing and cost hours the queue has better uses
    # for.
    if [ -s "$out/grimgar06_chunk$size.json" ]; then
        stage_note "chunk size $size produced a book; not trying larger"
        break
    fi
done

run_stage indexes 20m -- "$python" -u "$REPO/refresh_indexes.py"
stage_summary grimgar_chunk_size_20260820
