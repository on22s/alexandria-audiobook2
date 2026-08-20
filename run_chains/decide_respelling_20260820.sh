#!/usr/bin/bash
# Does respelling earn its place at all?
#
# WHERE THIS STANDS. On 1,582 terms across every row, with no separator, the
# un-respelled control BEAT the respelling: 19.0% against 13.5%, 131 wins to
# 219 losses, p=2.96e-06. At 795 terms the same arm was p=0.12, so the effect
# grew with the sample rather than washing out. The listener independently
# picked un-respelled audio more than any other single form (5 of 9), and the
# separator work showed the pauses that make respellings sound robotic.
#
# THE CONFOUND THIS RESOLVES. The shipped hyphen form measured 14.6% against
# 14.6% over 7,775 terms - no help and no harm - while the no-separator form
# measured actively harmful. Those are DIFFERENT TERM SETS, so the comparison
# is not sound: 1,582 terms sorted by book count are the commonest words, and
# 7,775 reaches far rarer ones. Stage 1 runs the shipped hyphen over exactly
# the same 1,600-term slice, so hyphen, none and plain can be compared pairwise
# on identical terms. That is the number that should decide whether respelling
# stays on by default.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/decide_respelling_20260820"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"

run_stage hyphen_allrows_1600 6h --needs-vram -- \
    "$REPO/gpu_job.sh" hyphen_allrows_1600 \
    "$python" -u "$REPO/app/experiments/measure_respellings.py" \
    --min-books 5 --separator hyphen --limit 1600 \
    --work "$runtime/respelling_hyphen_allrows" \
    --out "$runtime/experiments/respelling_hyphen_allrows_n1600.json"
stage_commit_artifacts hyphen_allrows_1600 "$REPO"

# Pauses over the same three forms on the same terms, now that all of them
# exist at this size. Refuses rather than writing an empty artifact.
run_stage pauses_allrows 1h -- \
    "$python" -u "$REPO/app/experiments/measure_pauses.py" --limit 800 \
    --arm none=respelling_none_allrows \
    --arm hyphen_wide=respelling_hyphen_allrows \
    --out "$runtime/experiments/respelling_pauses_allrows.json"
stage_commit_artifacts pauses_allrows "$REPO"

run_stage indexes 20m -- "$python" -u "$REPO/refresh_indexes.py"
stage_summary decide_respelling_20260820
