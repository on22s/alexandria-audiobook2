#!/usr/bin/bash
# Two questions the 119-term separator arms raised but cannot answer.
#
# 1. SEPARATORS CAUSE THE PAUSES, GRADED - none 43/74 p=0.20 (indistinguishable
#    from un-respelled audio), space 87/107 p=3.8e-11, dot 115/118 p=1.65e-30.
#    That is a clean result on 119 terms and the direction is unambiguous.
#
# 2. BUT NO FORM IMPROVED RECOVERY on those same terms - none 5 wins/7 losses
#    p=0.77, space 8/8 p=1.0, dot 4/12 p=0.08. If respelling buys nothing on
#    this subset, "which separator" is the wrong question and "does respelling
#    earn its place here" is the right one.
#
# Both need more terms. The arms above are --only-e-row --min-books 5, the
# hardest and narrowest slice; the shipped measurement covers 7,775 terms. This
# runs `none` - the only form that adds no pauses - at 400 terms against the
# shipped hyphen, which is the comparison that decides whether to change the
# default.
#
# It also retries grimgar06, which failed genuinely on 2026-08-19 (rc=1, chunk
# 29/70 failed validation after 114 retries, no output written) - a stochastic
# collapse, not a harness fault, so a rerun is a real attempt rather than a
# repeat of the same broken thing.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/separator_scale_20260819"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"

run_stage separator_none_n400 4h --needs-vram -- \
    "$REPO/gpu_job.sh" separator_none_n400 \
    "$python" -u "$REPO/app/experiments/measure_respellings.py" \
    --min-books 5 --only-e-row --separator none --limit 400 \
    --work "$runtime/respelling_sep_none" \
    --out "$runtime/experiments/respelling_separator__none_n400.json"
stage_commit_artifacts separator_none_n400 "$REPO"

# Pauses over the widened `none` arm, against plain. Refuses rather than
# writing an empty artifact if an arm has no clips.
run_stage separator_pauses_n400 1h -- \
    "$python" -u "$REPO/app/experiments/measure_pauses.py" --limit 400 \
    --arm none=respelling_sep_none --arm space=respelling_sep_space \
    --arm dot=respelling_sep_dot \
    --out "$runtime/experiments/respelling_pauses_separators_n400.json"
stage_commit_artifacts separator_pauses_n400 "$REPO"

# The book that genuinely failed. Its three companions are already on disk and
# are skipped in seconds by the corrected copy.
run_stage grimgar06_retry 6h --needs-vram -- \
    env REQUIRE_VRAM_GB=0 "$REPO/run_chains/unseen_books_20260819b.sh"
stage_commit_artifacts grimgar06_retry "$REPO"

run_stage indexes 20m -- "$python" -u "$REPO/refresh_indexes.py"
stage_summary separator_scale_20260819
