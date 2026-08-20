#!/bin/bash
set -uo pipefail
REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git

# HOLD THE REAL LOCK INSTEAD OF GUESSING WHO IS RUNNING. The one-line poll this
# replaces used the `identit[y]` bracket trick, which correctly avoids matching
# its own shell - but still raced between "nothing is running" and "start
# mine", and still enumerated script names, so any GPU job not on its list was
# invisible. gpu_job.sh's flock does not care what the other job is called.
# The sentinel is exported by gpu_job.sh, so this is safe when nested.
if [ "${ALEXANDRIA_GPU_LOCK_HELD:-0}" != 1 ]; then
    exec "$REPO/gpu_job.sh" "ref_audit_chain" \
        env ALEXANDRIA_GPU_LOCK_HELD=1 "$0" "$@"
fi
# NO GPU_LOCK EXPORT. This line used to name $HOME/.alexandria_gpu.lock, a
# third lock file that serialised against neither the repo lock the other
# chains use nor gpu_job.sh's own - and it sat BELOW the self-re-exec above,
# so this chain's outer wrapper and its inner jobs took different locks.
# gpu_job.sh now defaults to the repo lock; letting it decide is the point.
export GPU_QLOG="$REPO/ab_test_runtime/logs/gpu_jobq.log"
cd "$REPO/app"

# 1. REF AUDIT — would a medoid reference beat the sample-0 default?
"$REPO/gpu_job.sh" dataset_ref_audit \
  timeout 10800 "$REPO/app/env/bin/python" -u experiments/repair_dataset_ref.py \
  > "$REPO/ab_test_runtime/logs/dataset_ref_audit.log" 2>&1
echo "ref_audit rc=$?"
tail -4 "$REPO/ab_test_runtime/logs/dataset_ref_audit.log"

# 2. RECLASSIFY — the RETRAIN/REBUILD verdicts were computed from 4-clip
#    adapter scores, and three adapters moved by more than 0.15 at ten clips
#    (warm_tenor_20s_m 0.725 -> 0.090). Verdicts resting on those numbers need
#    recomputing before anyone acts on them.
"$REPO/gpu_job.sh" consistency_n10 \
  timeout 10800 "$REPO/app/env/bin/python" -u experiments/dataset_speaker_consistency.py \
    --fidelity "$REPO/ab_test_runtime/experiments/library_voice_fidelity_n10.json" \
    --out "$REPO/ab_test_runtime/experiments/dataset_speaker_consistency_n10.json" \
  > "$REPO/ab_test_runtime/logs/consistency_n10.log" 2>&1
echo "reclassify rc=$?"
sed -n '/SUMMARY/,$p' "$REPO/ab_test_runtime/logs/consistency_n10.log" | head -8
echo "REF CHAIN DONE $(date -u +%FT%TZ)"
