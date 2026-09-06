#!/usr/bin/env bash
# Does a smaller candidate list get picked from better?
#
# THE ONLY LEAD THE LITERATURE CREDITS THAT IS NOT AN ORACLE. arXiv 2307.03734
# reports 0.40 end-to-end against 0.78 once candidates are restricted to
# coreference-resolved mentions, and arXiv 2608.02359 measures coreference
# mentions as +12 points over alias-only candidates on non-explicit quotes.
# Every other published PDNC figure is measured with the gold character list
# supplied, so this is the one method effect that transfers to our setting.
#
# THE THRESHOLD IS REGISTERED, NOT CHOSEN AFTERWARDS. The unrestricted arm
# reads 0.656 over these 2,494 rows. Restricting to characters named in the
# quote's own window shows 8 candidates instead of 74 and keeps the right
# speaker 92.7% of the time (candidate_restriction.json, measured offline and
# replicated by a second implementation). So this arm beats the unrestricted
# one ONLY if it picks correctly on more than 0.656/0.927 = 70.7% of the rows
# it retains. Anything less is a loss even if the raw number rises.
#
# WHAT A NULL WOULD MEAN. That the cheap version of the intervention does not
# work - name matching drops a speaker referred to only by pronoun, which is
# most of the 7.3 points of recall lost. It would NOT rule out the lead: a
# coreference model is the next step and not a different idea.
#
# Same fixtures, same seed, same limit as the w3200 arm it is compared against,
# so the two are paired on identical quotations.
#
# Resumable by artifact. Never write "rc=$?" after a command substitution.
set -u
R="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$R" || exit 1
MAIN="$(git -C "$R" worktree list --porcelain 2>/dev/null | head -1 | awk '{print $2}')"
PY="$R/app/env/bin/python"; [ -x "$PY" ] || PY="$MAIN/app/env/bin/python"
[ -x "$PY" ] || { echo "no interpreter" >&2; exit 1; }
ART="$R/ab_test_runtime/experiments/two_stage_attribution_restricted.json"
[ -s "$ART" ] && { echo "SKIP - artifact exists"; exit 0; }
FIX=""
for b in prideandprejudice theawakening thesignofthefour; do
    f="$R/app/fixtures/attribution_gold_pdnc_${b}_w3200.json"
    [ -s "$f" ] && FIX="$FIX $f"
done
[ -n "$FIX" ] || { echo "no w3200 fixtures found" >&2; exit 1; }
echo "fixtures:$(echo "$FIX" | wc -w)"
# $? AFTER A PIPELINE IS THE LAST COMMAND'S STATUS, NOT THE PYTHON'S. The first
# version piped through `tail -40` and then read `rc=$?`, so it read tail's
# status - which is always 0. On 2026-09-06 the run died instantly with
# `RuntimeError: no llama.cpp /props at http://127.0.0.1:8090/v1`, and the
# chain printed `RUN rc=0` and `ALL DONE`, and gpu_job.sh logged OK. A green
# exit over a crash, which is the failure this repository keeps finding in
# other people's code and had here in its own.
#
# PIPESTATUS[0] is the python's status. The chain also EXITS with it, so the
# queue records a failure instead of a success - without that, the log is
# still wrong even when this line is right.
set -o pipefail
"$PY" -u app/experiments/two_stage_attribution.py \
    --fixtures $FIX --restrict-candidates \
    --limit 0 --seed 20260819 --tag restricted \
    --out "$ART" 2>&1 | tail -40
rc=${PIPESTATUS[0]}
echo "[$(date -Is)] RUN rc=$rc"
if [ "$rc" -ne 0 ]; then
    echo "the run FAILED; no artifact was written. Common cause: no llama.cpp" >&2
    echo "server on the base URL. Start one serving qwen3-14b, then retry." >&2
    exit "$rc"
fi
[ -s "$ART" ] || { echo "run reported success but wrote no artifact" >&2; exit 1; }
echo "ALL DONE $(date -Is)"
