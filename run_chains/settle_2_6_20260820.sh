#!/usr/bin/bash
# Goal 2.6 hangs on ONE cell. Settle it rather than leave it ambiguous.
#
# Chinese CLONE vocal tract length is 1.064x against a 0.95-1.05 target, at
# n=100. Every other cell of 2.6 is inside its band, and the Chinese LoRA arm
# of this same measure is 1.032x - so the miss belongs to zero-shot cloning,
# not to the language.
#
# WHY THIS IS NOT OBVIOUSLY SAMPLE SIZE. That was the answer twice before, but
# both times the failing cells were measured at n=12 and came inside at n=100.
# This one IS n=100. Doubling to 200 will either bring 1.064 inside 1.05 -
# closing the goal - or confirm the miss is real, at which point 2.6 stops
# being "one cell away" and becomes a statement about cloning that the document
# should make plainly. Either outcome is worth the run; only the ambiguity is
# not.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/settle_2_6_20260820"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"

running() { pgrep -f "run_chains/$1" 2>/dev/null | grep -qv -e "^$$\$" -e "^$PPID\$"; }
stage_note "waiting for overnight_20260820b to finish"
while running overnight_20260820b.sh; do sleep 120; done
stage_note "it is done; continuing"

run_stage pitch_quality_n200 5h --needs-vram -- \
    "$REPO/gpu_job.sh" pitch_quality_n200 \
    "$python" -u "$REPO/app/experiments/pitch_quality_probe.py" \
    --lines 200 \
    --out "$runtime/experiments/pitch_quality_probe_n200.json"
stage_commit_artifacts pitch_quality_n200 "$REPO"

run_stage indexes 20m -- "$python" -u "$REPO/refresh_indexes.py"
stage_summary settle_2_6_20260820
