#!/usr/bin/bash
# Three measurements that need no new generation, only scoring.
#
# Committed rather than left as the scratchpad waiter that first ran them, so
# the chain that produced the numbers can be read beside them. It waits by
# NAME, which is reproducible, instead of the hardcoded PID the scratchpad copy
# needed - a chain outside run_chains/ cannot be matched by name, which is
# precisely why it belongs here.
#
# 1. A SCORABLE CHINESE ANCHOR FOR 2.1. That goal's Chinese cell cannot be
#    scored at all: the narrator matched herself at 0.691 while synthetic arms
#    reached 0.765, so the ceiling sits BELOW the arms and the comparison is
#    void. anchor_length_probe already settled why - truncating English clips
#    to the Chinese median of 3.17s drops its anchor from 0.7834 to 0.6320, so
#    short clips break ECAPA rather than the speaker being unusable. SSB0748 is
#    a second Chinese speaker at 4.48s median whose audio is already generated
#    for both arms, so this is ECAPA over existing files. 4.48s may still be
#    too short; that is a real answer either way, and turns an open question
#    into a stated requirement for a longer Chinese corpus.
#
# 2. THE FOUR-ARM SEPARATOR TABLE FOR 5.5. none/space/dot/hyphen at the same
#    1,600 terms. It refuses rather than writing an empty artifact if an arm
#    has no clips - and the dot arm must be COMPLETE before this runs, or the
#    table compares a biased subset, since terms are ordered by book count and
#    a truncated arm is the commonest words only.
#
# 3. ECAPA ON THE LONG-REFERENCE ARM. The long-reference chain scores pitch and
#    voice quality; 2.1 is a speaker-similarity goal sitting at 93% of ceiling
#    against a 95% target, so if a longer reference helps anywhere it should
#    show here. Skipped, not failed, when that arm has not run - its manifests
#    come from a chain that may be queued behind this one, and a stage that
#    dies on a missing file reads like a broken run rather than an ordering
#    fact.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python=""   # set after queue.sh is sourced
STAGE_LOG_DIR="$runtime/logs/anchor_and_separator_table_20260826"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"
source "$REPO/run_chains/lib/queue.sh"
python=$(resolve_python "$REPO") || {
    echo "no interpreter: looked in this checkout and the main one" >&2
    exit 1; }

refuse_if_dirty "$REPO" || exit 1

for chain in second_english_eval_20260820.sh unseen_books_20260819b.sh \
             attribution_context_20260820.sh longref_arm_20260826.sh; do
    wait_for_chain "$chain"
done

run_stage anchor_ssb0748 1h -- \
    "$python" -u "$REPO/app/experiments/ljspeech_score.py" \
    --generated "$runtime/experiments/aishell3_SSB0748_generate.json" \
    --limit 0 --out "$runtime/experiments/aishell3_SSB0748_score.json"
stage_commit_artifacts anchor_ssb0748 "$REPO"

# THE DOT ARM MUST BE FINISHED. It was interrupted at 487 of 1600 once already,
# and a partial arm here does not make the table smaller, it makes it wrong.
dot="$runtime/experiments/respelling_dot_allrows_n1600.json"
if ! "$python" - "$dot" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
sys.exit(0 if d.get("status") == "complete" else 1)
PYEOF
then
    stage_note "SKIP pauses_four_arms: the dot arm is not complete"
else
    run_stage pauses_four_arms 1h -- \
        "$python" -u "$REPO/app/experiments/measure_pauses.py" --limit 800 \
        --arm none=respelling_none_allrows \
        --arm space=respelling_space_allrows \
        --arm dot=respelling_dot_allrows \
        --arm hyphen_wide=respelling_hyphen_allrows \
        --out "$runtime/experiments/respelling_pauses_allrows_4arm.json"
    stage_commit_artifacts pauses_four_arms "$REPO"

    run_stage selectivity_all 30m -- \
        "$python" -u "$REPO/app/experiments/respelling_selectivity.py" \
        --out "$runtime/experiments/respelling_selectivity_allrows.json"
    stage_commit_artifacts selectivity_all "$REPO"
fi

for lang in en ja zh; do
    gen="$runtime/experiments/longref__${lang}_generate.json"
    if [ ! -f "$gen" ]; then
        stage_note "SKIP longref_ecapa_$lang: $gen does not exist yet"
        continue
    fi
    run_stage "longref_ecapa_$lang" 1h -- \
        "$python" -u "$REPO/app/experiments/ljspeech_score.py" \
        --generated "$gen" --limit 0 \
        --out "$runtime/experiments/longref__${lang}_score.json"
    stage_commit_artifacts "longref_ecapa_$lang" "$REPO"
done

run_stage indexes 20m -- "$python" -u "$REPO/refresh_indexes.py"
stage_summary anchor_and_separator_table_20260826

echo
echo "HOW TO READ IT."
echo "  1. aishell3_SSB0748_score.json: is the human-vs-human anchor now ABOVE"
echo "     both synthetic arms? If yes, 2.1 has a scorable Chinese cell for the"
echo "     first time. If no, 4.48s is still too short and the goal needs a"
echo "     longer Chinese corpus - a requirement, not an open question."
echo "  2. the four-arm table decides which separator ships for 5.5."
echo "  3. longref scores say whether a 10-12s reference moves 2.1's 93%."
