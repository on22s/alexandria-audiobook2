#!/usr/bin/bash
# The space arm, which the first run never measured.
#
# WHY IT EXISTS SEPARATELY. separator_space was refused with rc=7 - 939 MiB
# free against a 4096 MiB floor - because --needs-vram slept a flat 5 seconds
# after stopping llama-server and the driver had not released the memory yet.
# separator_dot started 5 seconds later and ran fine on the same card. The
# reclaim now polls the actual figure (lib/stage.sh), and this runs the one arm
# that was lost, then re-scores all three together.
#
# Without it the separator result rests on two arms: `none` is indistinguishable
# from un-respelled audio (43/74, p=0.20) while `dot` pauses hard (115/118,
# p=1.65e-30). `space` is the arm that says whether it is separators in general
# or that particular character.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/separator_space_20260819"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"

run_stage separator_space 3h --needs-vram -- \
    "$REPO/gpu_job.sh" separator_space \
    "$python" -u "$REPO/app/experiments/measure_respellings.py" \
    --min-books 5 --only-e-row --separator space --limit 120 \
    --work "$runtime/respelling_sep_space" \
    --out "$runtime/experiments/respelling_separator__space.json"
stage_commit_artifacts separator_space "$REPO"

# All three arms together. This now REFUSES rather than writing an empty
# artifact if any arm has no clips, so a repeat of the silent zero is loud.
run_stage separator_pauses_all 1h -- \
    "$python" -u "$REPO/app/experiments/measure_pauses.py" --limit 400 \
    --arm none=respelling_sep_none --arm space=respelling_sep_space \
    --arm dot=respelling_sep_dot \
    --out "$runtime/experiments/respelling_pauses_separators.json"
stage_commit_artifacts separator_pauses_all "$REPO"

stage_summary separator_space_20260819
