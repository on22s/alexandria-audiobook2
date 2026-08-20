#!/bin/bash
set -uo pipefail
REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
# NO GPU_LOCK EXPORT. This line used to name $HOME/.alexandria_gpu.lock, a
# third lock file that serialised against neither the repo lock the other
# chains use nor gpu_job.sh's own - and it sat BELOW the self-re-exec above,
# so this chain's outer wrapper and its inner jobs took different locks.
# gpu_job.sh now defaults to the repo lock; letting it decide is the point.
export GPU_QLOG="$REPO/ab_test_runtime/logs/gpu_jobq.log"
cd "$REPO/app"
# Two adapters so a result is not one dataset's quirk: the most extreme case,
# and one whose reference was fine, where the prediction is a SMALL gap.
for a in husky_baritone_20s_m_anime husky_tenor_30s_m_literary; do
  "$REPO/gpu_job.sh" "refintervene_$a" \
    timeout 10800 "$REPO/app/env/bin/python" -u experiments/reference_intervention.py \
      --adapter "$a" \
      --out "$REPO/ab_test_runtime/experiments/reference_intervention__$a.json" \
    > "$REPO/ab_test_runtime/logs/refintervene_$a.log" 2>&1
  echo "$a rc=$?"
  grep -E 'medoid|worst|VERDICT' "$REPO/ab_test_runtime/logs/refintervene_$a.log" | tail -4
done
echo "INTERVENTION DONE $(date -u +%FT%TZ)"
