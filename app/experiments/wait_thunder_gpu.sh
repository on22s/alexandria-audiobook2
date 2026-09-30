#!/usr/bin/env bash
# Invoke through gpu_job.sh: retain its lock and wait for legacy external jobs.
set -euo pipefail
test "$#" -gt 0 || { echo 'No command supplied' >&2; exit 2; }
while true; do
    if ! pids=$(nvidia-smi --query-compute-apps=pid --format=csv,noheader,nounits); then
        echo 'Cannot inspect GPU ownership; refusing to start' >&2
        exit 2
    fi
    if [[ ! "$pids" =~ [0-9] ]]; then
        break
    fi
    echo "WAIT: GPU is occupied by $pids"
    sleep 20
done
exec "$@"
