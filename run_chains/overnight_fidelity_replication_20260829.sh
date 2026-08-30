#!/bin/bash
# Extend the library-voice fidelity replication by eight independent seeds.
#
# WHY MORE SEEDS. Nine seeds at n=20 exist (20260827..20260905, each ~1 h on
# the RX 9070 XT). Goal 2.4 rests on a median that meets its target while 43%
# of individual clips fall outside the band, so the spread is the open
# question and spread is what more independent samples measure.
#
# WHY NOT THE THINGS THAT LOOK MORE URGENT. Goal 1.3's confirmation needs PDNC
# books this adapter never trained on; only six books have gold fixtures and
# the three never-trained ones are the three already measured. Building
# fixtures for the five remaining Austen novels is a hand-checked job
# (Rule 21), not an unattended one. The qwen3.5 quant repeat already ran on
# 2026-08-28 and its rows are scoreable, so re-running it would spend 96
# minutes to learn nothing.
#
# A NEW FILE rather than an edit of the 08-22 chain, because bash reads a
# script by byte offset and rewriting one under a running shell resumes it at
# the wrong place (Rule 23).
#
# gpu_job.sh serializes each seed against any other Alexandria GPU job and
# refuses a dirty tree, so every artifact below names a commit.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

# An interrupted wrapper can release the flock while its child still holds the
# card. Name that predecessor and this supervisor stays CPU-only until it ends.
if [ -n "${WAIT_FOR_PID:-}" ]; then
    while kill -0 "$WAIT_FOR_PID" 2>/dev/null; do
        sleep 30
    done
fi

for seed in 20260906 20260907 20260908 20260909 20260910 20260911 20260912 20260913; do
    name="library_fidelity_seed_${seed}_n20"
    out="ab_test_runtime/experiments/${name}.json"
    # Resume: a finished seed is never recomputed, so an interrupted night
    # continues where it stopped rather than starting over.
    if [ -s "$out" ]; then
        echo "[$(date -u +%FT%TZ)] SKIP $name (already complete)"
        continue
    fi
    echo "[$(date -u +%FT%TZ)] START $name"
    ./gpu_job.sh "$name" \
        ./app/env/bin/python -u app/experiments/library_voice_fidelity.py \
        --lines 20 \
        --seed "$seed" \
        --work "ab_test_runtime/${name}" \
        --out "$out" \
        > "ab_test_runtime/logs/${name}.out" 2>&1 || exit $?
done
echo "[$(date -u +%FT%TZ)] COMPLETE overnight_fidelity_replication_20260829"
