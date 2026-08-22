#!/bin/bash
# Replicate the shipped-voice fidelity measurement across independent samples.
# gpu_job.sh serializes every seed with any already-running Alexandria GPU job.
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT" || exit 1

for seed in 20260827 20260828 20260829 20260831 20260901 20260902 20260903 20260904 20260905; do
    name="library_fidelity_seed_${seed}_n20"
    out="ab_test_runtime/experiments/${name}.json"
    if [ -s "$out" ]; then
        continue
    fi
    ./gpu_job.sh "$name" \
        ./app/env/bin/python -u app/experiments/library_voice_fidelity.py \
        --lines 20 \
        --seed "$seed" \
        --work "ab_test_runtime/${name}" \
        --out "$out" \
        > "ab_test_runtime/logs/${name}.out" 2>&1 || exit $?
done
