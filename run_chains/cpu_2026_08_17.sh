#!/usr/bin/bash
# CPU work for 2026-08-17, to run ALONGSIDE the GPU queue rather than behind it.
#
# NOTHING HERE TAKES THE GPU LOCK, deliberately. The card is booked until
# roughly 13:45 by respelling_widen, and every stage below is EPUB parsing,
# regex counting or the test suite. Queueing this work behind the lock would
# waste eight hours of an idle CPU for no reason.
#
# A failing stage is logged and the queue CONTINUES, same as the overnight
# chains: one bad stage must not cost the rest.
set -uo pipefail

repo="$(cd "$(dirname "$0")/.." && pwd)"
# Artifacts belong with every other artifact, which may be a different checkout
# than the code being run. Override with RUNTIME=... when they are the same.
runtime="${RUNTIME:-$repo/ab_test_runtime}"
# NOT $PYTHON: Pinokio exports it pointing at a miniforge path that does not
# exist here, and because it is SET the `:-` default never applies.
python="${CHAIN_PYTHON:-$repo/app/env/bin/python}"
mkdir -p "$runtime/logs" "$runtime/experiments"

failures=0
note() { echo "[$(date -u +%FT%TZ)] $*"; }
attempt() {
    local name="$1" artifact="$2"; shift 2
    if [ -e "$artifact" ]; then note "SKIP $name (artifact exists)"; return 0; fi
    note "START $name"
    if "$@"; then note "OK   $name"; else
        failures=$((failures + 1)); note "FAIL $name (continuing)"
    fi
}

# ------------------------------------------------------------------ 1. reach
# Re-attribute every candidate term with the widened back-matter condition.
#
# The old run produced `ja: 7775, zh: 0` and `resolved_by_back_matter: {}` -
# not because the markers fail, but because they were only consulted for books
# whose publisher was `unknown`, and Seven Seas declares `mixed`. Its 627 books
# are the largest source of Chinese titles in this library and never reached
# the test.
#
# Written to a NEW artifact, not over the old one. The comparison between them
# is the evidence that the fix did something, and overwriting would destroy it.
attempt lexicon_attribution_v2 "$runtime/experiments/lexicon_attributed_v2.json" \
    timeout 3600 "$python" -u "$repo/app/experiments/lexicon_language_attribution.py" \
    --checkpoint "$runtime/lexicon_scan/checkpoint.json" \
    --candidates "$runtime/experiments/lexicon_corpus_candidates.json" \
    --out "$runtime/experiments/lexicon_attributed_v2.json"

# ------------------------------------------------------------------ 2. gates
# The overnight chain's release_verification FAILED at 23:03 UTC with one unit
# test failure, and nothing has established which test or whether it still
# fails. Runs unconditionally - no artifact guard - because a stale pass is
# worse than no answer.
note "START release_verification"
if (cd "$repo/app" && timeout 1800 "$python" -u verify_release.py \
        --json-report "$runtime/experiments/cpu_chain_release.json"); then
    note "OK   release_verification"
else
    failures=$((failures + 1)); note "FAIL release_verification (continuing)"
fi

note "CPU QUEUE COMPLETE with $failures failed stage(s)"
echo
echo "WHAT TO READ FIRST:"
echo "  ab_test_runtime/experiments/lexicon_backmatter_probe.json"
echo "     - validation first: how often the marker test contradicts a"
echo "       publisher that is not in doubt. If that number is not ~0, stop;"
echo "       nothing downstream of it is trustworthy."
echo "  ab_test_runtime/experiments/lexicon_attributed_v2.json"
echo "     - compare verdict tallies against lexicon_attributed.json. Any"
echo "       zh or straddles verdict at all is new; the old run had none."
