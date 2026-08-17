#!/bin/bash
# Does the preflight actually rescue the book it was built for?
#
# Daisy Miller failed at chunk 2 of 22. Its apostrophes had been stripped by a
# lossy conversion - "She s got to give me some candy" - which is a shape the
# model has no reason to reproduce, so coverage failed and the retries failed
# the same way.
#
# preflight_source now restores 468 contractions in memory before the first
# LLM call. This run is the claim's only real test: the unit tests prove the
# repair happens, not that generation gets past chunk 2. Nothing here is
# reportable until this finishes.
#
# Runs under gpu_job.sh so it waits for the PDNC chain rather than contending
# for the card.
set -uo pipefail
REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
export GPU_LOCK="${GPU_LOCK:-$HOME/.alexandria_gpu.lock}"
export GPU_QLOG="$REPO/ab_test_runtime/logs/gpu_jobq.log"
cd "$REPO/app"
"$REPO/gpu_job.sh" daisymiller_preflight timeout 21600 \
    "$REPO/app/env/bin/python" -u generate_script.py \
    "$REPO/ab_test_runtime/pdnc/data/DaisyMiller/novel_text.txt" \
    --output "$REPO/ab_test_runtime/pdnc_generated/DaisyMiller.json"
echo "rc=$? $(date -u +%FT%TZ)"
