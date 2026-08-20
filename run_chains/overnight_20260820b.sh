#!/usr/bin/bash
# Fills 05:00 -> 13:00 on 2026-08-20, after decide_respelling drains.
#
# WHAT THE NIGHT ESTABLISHED. Respelling applied to everything loses: 131 wins
# against 219 losses over 1,582 terms, p=2.96e-06. Split by population it is
# two different facts - it rescues 10.2% of the words the plain reading fails
# (131 of 1,281) and breaks 72.8% of the words the plain reading already says
# (219 of 301). Blanket application costs 88 words; applying it only where the
# plain reading fails gains 131 and can cost nothing.
#
# WHAT IS STILL THIN. The rescue rate rests on 1,281 failed terms and the
# breakage rate on only 301 successful ones - the narrower of the two, and the
# one carrying the whole argument for a condition. Stage 1 doubles the sample.
# It reuses the existing work directory, so the first 1,600 terms are skipped
# rather than regenerated and the cost is only the new ones.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/overnight_20260820b"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"

# Wait for the chain already running rather than fighting it for the lock, so
# the queue log reads in order. gpu_job.sh would serialise us anyway.
running() { pgrep -f "run_chains/$1" 2>/dev/null | grep -qv -e "^$$\$" -e "^$PPID\$"; }
stage_note "waiting for decide_respelling_20260820 to finish"
while running decide_respelling_20260820.sh; do sleep 120; done
stage_note "it is done; continuing"

run_stage none_allrows_3200 6h --needs-vram -- \
    "$REPO/gpu_job.sh" none_allrows_3200 \
    "$python" -u "$REPO/app/experiments/measure_respellings.py" \
    --min-books 5 --separator none --limit 3200 \
    --work "$runtime/respelling_none_allrows" \
    --out "$runtime/experiments/respelling_none_allrows_n3200.json"
stage_commit_artifacts none_allrows_3200 "$REPO"

# The book that has now failed three times, at chunks 29, 29 and 27 - a
# stochastic collapse rather than one bad passage. It reports failure loudly
# now, so a fourth failure is information rather than a silent OK.
run_stage grimgar06_retry 5h --needs-vram -- \
    env REQUIRE_VRAM_GB=0 "$REPO/run_chains/unseen_books_20260819b.sh"
stage_commit_artifacts grimgar06_retry "$REPO"

run_stage indexes 20m -- "$python" -u "$REPO/refresh_indexes.py"
stage_summary overnight_20260820b
