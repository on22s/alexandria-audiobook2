#!/usr/bin/bash
# Is reference typicality a lever on speaker similarity, or was #367 length?
#
# METTS (TASLP 2024) perturbs the FORMANTS of its reference to strip speaker
# timbre, on the grounds that formants are set by the vocal tract and "represent
# their vocal identity", and reports that beating both SALN and speaker-
# adversarial training on speaker cosine similarity. We want the opposite of
# their perturbation - we are keeping timbre - but the claim underneath has
# never been tested here.
#
# #367 replaced one reference with a better one and moved goals 2.5 and 2.6.
# It changed LENGTH AND TYPICALITY TOGETHER and said so. This holds the duration
# band constant and varies only distance from the speaker's own median f0 and
# vocal-tract length: arm 0 is the nearest candidate (what #367 picked), the
# last arm is the farthest measured, the rest are even quantiles between.
#
# A FLAT RESULT CLOSES THE QUESTION and is worth the same GPU time: it would
# mean reference choice is not a lever on goal 2.1, which sits at 93% of a 95%
# target with no other lever identified.
#
# COST, measured: the longref arm generated and scored 150 LJSpeech utterances
# per language. Four arms of the same size is four times that work. No arm of
# this exact shape has run, so scale from the longref stage's own log rather
# than from this comment.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
work="$runtime/reference_spread"
STAGE_LOG_DIR="$runtime/logs/reference_spread_20260821"
mkdir -p "$STAGE_LOG_DIR" "$work"
source "$REPO/run_chains/lib/stage.sh" || exit 1

for f in app/experiments/reference_spread.py \
         app/experiments/reference_spread_compare.py; do
    [ -f "$REPO/$f" ] || { echo "REFUSING: $REPO/$f is missing (PR pending)."; exit 1; }
done

base="$runtime/ljspeech_eval/build.json"
[ -f "$base" ] || { echo "REFUSING: no base build at $base"; exit 1; }
adapter="$runtime/ljspeech_eval/adapter"
[ -d "$adapter" ] || { echo "REFUSING: no adapter at $adapter"; exit 1; }


ARMS=4
run_stage spread_build 30m -- \
    "$python" -u "$REPO/app/experiments/reference_spread.py" \
    --build "$base" --out-dir "$work" --arms "$ARMS" \
    --audio-root "$REPO" \
    --out "$runtime/experiments/reference_spread__en.json"

if ! is_stage_successful spread_build; then
    stage_summary reference_spread_20260821
    exit 1
fi

# The MANIFEST is the authority on which arms exist, not the filesystem. A
# corpus with few usable candidates yields fewer arms than requested, and
# probing for each build file would read as an artifact-existence skip - the
# pattern test_chain_skip_guards refuses, correctly, because elsewhere it hides
# a half-finished run.
built=$("$python" - "$runtime/experiments/reference_spread__en.json" "$work" <<'PYEOF'
import json, pathlib, sys
try:
    with open(sys.argv[1], encoding='utf-8') as handle:
        doc = json.load(handle)
    arms = doc['arms']
    if not isinstance(arms, list) or not arms:
        raise ValueError('manifest needs nonempty arms')
    ids = [a['arm'] for a in arms]
    if any(type(i) is not int or i < 0 for i in ids) or len(set(ids)) != len(ids):
        raise ValueError('manifest arm IDs must be unique nonnegative integers')
    if any(not (pathlib.Path(sys.argv[2]) / f'build_spread{i}.json').is_file() for i in ids):
        raise ValueError('manifest lists a missing build')
except (OSError, ValueError, TypeError, KeyError) as error:
    print(f'unusable spread manifest: {error}', file=sys.stderr)
    sys.exit(1)
print(' '.join(str(i) for i in ids))
PYEOF
) || { echo "REFUSING: the spread manifest is unreadable or incomplete."; exit 1; }
[ -n "$built" ] || { echo "REFUSING: the manifest lists no arms."; exit 1; }
echo "arms built: $built"

# Keep CPU scoring out of the gaps between queued generation arms.
for i in $built; do
    build="$work/build_spread$i.json"
    REQUIRE_VRAM_GB=4 run_stage "spread_gen_$i" 3h --needs-vram --requires-ok spread_build -- \
        "$REPO/gpu_job.sh" "spread_gen_$i" \
        "$python" -u "$REPO/app/experiments/ljspeech_generate.py" \
        --build "$build" --adapter "$adapter" --out-dir "$work/arm$i" \
        --arms clone --limit 0 \
        --out "$runtime/experiments/reference_spread__en_generate_arm$i.json"
    stage_commit_artifacts "spread_gen_$i" "$REPO" "$runtime/experiments/reference_spread__en.json" "$runtime/experiments/reference_spread__en_generate_arm$i.json"
done

scores=()
for i in $built; do
    score="$runtime/experiments/reference_spread__en_score_arm$i.json"
    run_stage "spread_score_$i" 1h --requires-ok "spread_gen_$i" -- \
        "$python" -u "$REPO/app/experiments/ljspeech_score.py" \
        --generated "$runtime/experiments/reference_spread__en_generate_arm$i.json" \
        --limit 0 --out "$score"
    run_stage "spread_score_check_$i" 0 --requires-ok "spread_score_$i" -- \
        "$python" "$REPO/app/experiments/reference_spread_compare.py" --check-score "$score"
    if is_stage_successful "spread_score_check_$i"; then
        scores+=("$i=$score")
    fi
    stage_commit_artifacts "spread_score_$i" "$REPO" "$score"
done

if [ "${#scores[@]}" -ge 2 ]; then
    run_stage spread_compare 20m -- \
        "$python" -u "$REPO/app/experiments/reference_spread_compare.py" \
        --spread "$runtime/experiments/reference_spread__en.json" \
        --score "${scores[@]}" \
        --out "$runtime/experiments/reference_spread__en_compare.json"
    stage_commit_artifacts spread_compare "$REPO" "$runtime/experiments/reference_spread__en_compare.json"
else
    echo "only ${#scores[@]} arm(s) scored; a correlation needs at least two"
fi

stage_summary reference_spread_20260821
