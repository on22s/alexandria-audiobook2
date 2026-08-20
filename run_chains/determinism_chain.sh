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
    exec "$REPO/gpu_job.sh" "determinism_chain" \
        env ALEXANDRIA_GPU_LOCK_HELD=1 "$0" "$@"
fi
# NO GPU_LOCK EXPORT. This line used to name $HOME/.alexandria_gpu.lock, a
# third lock file that serialised against neither the repo lock the other
# chains use nor gpu_job.sh's own - and it sat BELOW the self-re-exec above,
# so this chain's outer wrapper and its inner jobs took different locks.
# gpu_job.sh now defaults to the repo lock; letting it decide is the point.
export GPU_QLOG="$REPO/ab_test_runtime/logs/gpu_jobq.log"
# wait for the gate verification to finish first
cd "$REPO/app"
"$REPO/gpu_job.sh" training_determinism \
  timeout 10800 "$REPO/app/env/bin/python" -u experiments/training_determinism.py --runs 3 \
  > "$REPO/ab_test_runtime/logs/training_determinism.log" 2>&1
echo "rc=$? $(date -u +%FT%TZ)"
tail -8 "$REPO/ab_test_runtime/logs/training_determinism.log"
