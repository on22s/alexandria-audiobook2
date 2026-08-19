#!/usr/bin/bash
# Rule B, tested only where rule A had something to fix and did not fix it.
#
# Rule A's measurement produced the selection rule this depends on: respelling
# helps only where the plain spelling is already wrong. Rescored over 7,607
# terms by whole-word recovery:
#
#     plain already right   1,138 terms   respelling BREAKS 73% of them
#     plain wrong           6,469 terms   respelling RESCUES 13% of them
#
# THE NUMBERS IN THIS HEADER USED TO READ "0.0-0.2 helped 38%", from a scorer
# that counted a word's kana appearing ANYWHERE in the transcript, in any
# order. Half its perfect scores never contained the word. The direction
# survived rescoring and the magnitude did not, so blanket respelling still
# makes an audiobook worse - it is simply a smaller win than advertised where
# it does help. Rule B is measured only where the plain form failed to produce
# the word, rule A did not rescue it, and rule B differs from rule A.
#
# ~25 minutes. Queued behind everything else; it waits on the GPU lock.
set -uo pipefail

# ARTIFACT EXISTS IS NOT ARTIFACT FINISHED. A run killed mid-way leaves a file
# that looks complete - respelling_e_row__ay_n1200.json sits in this repository
# at 1129 of 1200 terms - and a chain skipping on existence would skip it
# forever, on a subset biased toward the commonest items. Ask the artifact.
artifact_complete() {
    "$1" - "$2" <<'PYEOF' 2>/dev/null
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
if d.get("status") == "complete":
    sys.exit(0)
if d.get("status") == "partial":
    sys.exit(1)
r, c = d.get("results"), d.get("candidates_considered")
sys.exit(0 if isinstance(r, list) and isinstance(c, int) and len(r) >= c else 1)
PYEOF
}
repo="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$repo/ab_test_runtime"; python="$repo/app/env/bin/python"
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock" GPU_QLOG="$runtime/logs/gpu_jobq.log"
out="$runtime/experiments/respelling_rule_b.json"
# artifact_complete() was defined above and never called - see the note in
# app/tests/test_chain_skip_guards.py. A partial file must not read as done.
if [ -e "$out" ] && artifact_complete "$python" "$out"; then
    echo "already measured: $out"; exit 0
fi
[ -e "$out" ] && echo "re-running: $out exists but is incomplete"
"$repo/gpu_job.sh" respelling_rule_b \
    timeout --signal=INT --kill-after=60s 7200 \
    "$python" -u "$repo/app/experiments/measure_respellings.py" \
    --rule b --min-books 5 \
    --only-failed "$runtime/experiments/respelling_measure.json" \
    --work "$runtime/respelling_measure_b" --out "$out" \
  && echo "RULE B COMPLETE -> $out" \
  || echo "RULE B FAILED (rule A's results are unaffected)"
echo
echo "Compare against respelling_measure.json on the same terms: if rule B"
echo "rescues words rule A could not, the derivation was the limit. If neither"
echo "helps, respelling has a ceiling and those words need something else."
