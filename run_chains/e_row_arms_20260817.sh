#!/usr/bin/bash
# Is "eh" the wrong spelling for Japanese /e/? Three alternatives, same terms.
#
# THE FINDING THIS TESTS. Over 7,607 rescored terms, whole-word recovery is
# 15.0% overall - but 6.6% for any word containing an -eh mora against 18.0%
# for words without one. The gap survives a length control at every length:
#
#     kana length      with -eh      without
#          2             9.1%         28.5%
#          3             7.7%         19.5%
#          4             6.5%         15.2%
#          5             1.7%          6.7%
#
# A consistent 2.3-3.9x penalty over 2,031 terms. That is the single largest
# structural weakness in the derivation table, and unlike the near-miss edits -
# which turned out to be scattered, 490 two-edit failures with no dominant
# pattern - it names one row that can be changed in one place.
#
# WHAT IT DOES NOT ESTABLISH. Why. "seh" may read as a schwa, or the model may
# simply prefer other shapes; those predict the same penalty. So the
# replacement is measured rather than argued, which is what --e-spelling is
# for.
#
# PAIRED ON THE SAME TERMS. Each arm measures the identical 400 terms, and the
# "eh" baseline for those terms already exists in respelling_measure.json from
# the main run - so no fourth arm is needed and the comparison is within-term
# rather than between samples. Terms are taken in book-count order, which is
# deterministic, so all three arms see the same set.
#
# ~57 min per arm at the measured 8.5 s/term, so about 3 hours. Blocks on the
# GPU lock; it will wait for whatever is running.
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

REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
export GPU_LOCK="$runtime/logs/alexandria_gpu.lock"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"
LIMIT="${E_ROW_LIMIT:-400}"

note() { echo "[$(date -u +%FT%TZ)] $*"; }

for spelling in e ay ei; do
    out="$runtime/experiments/respelling_e_row__${spelling}.json"
    if [ -e "$out" ]; then note "SKIP $spelling (artifact exists)"; continue; fi
    note "START e-row arm: $spelling"
    "$REPO/gpu_job.sh" "e_row_$spelling" \
        timeout --signal=INT --kill-after=60s 7200 \
        "$python" -u "$REPO/app/experiments/measure_respellings.py" \
        --min-books 5 --only-e-row --e-spelling "$spelling" --limit "$LIMIT" \
        --work "$runtime/respelling_e_row_$spelling" --out "$out" \
        && note "OK   $spelling" || note "FAIL $spelling (continuing)"
done

note "E-ROW ARMS COMPLETE"
echo
echo "HOW TO READ IT. Compare each arm against the SAME terms in"
echo "respelling_measure_rescored.json, which holds the eh baseline:"
echo
echo "  app/env/bin/python app/experiments/rescore_respellings.py \\"
echo "      --artifact ab_test_runtime/experiments/respelling_e_row__ay.json \\"
echo "      --out /tmp/e_ay_rescored.json"
echo
echo "The number that matters is recovers_word on the shared terms, not the"
echo "arm's own total - the arms differ only in that row, so any difference"
echo "elsewhere is noise. If none of the three beats eh, the row is not the"
echo "problem and the 6.6%/18.0% gap needs another explanation."
