#!/bin/bash
# A dependent LLM command inside a campaign that already owns the GPU lease.
set -uo pipefail
REPO="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../.." && pwd -P)" || exit 4
NAME="${1:-}"; shift || exit 2
[ "$#" -gt 0 ] || exit 2
# This canonical action verifies the inherited owner before preflight work.
bash "$REPO/gpu_job.sh" --check-llm "$NAME" || exit $?
exec "$@"
