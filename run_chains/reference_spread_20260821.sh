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
source "$REPO/run_chains/lib/stage.sh"

for f in app/experiments/reference_spread.py \
         app/experiments/reference_spread_compare.py; do
    [ -f "$REPO/$f" ] || { echo "REFUSING: $REPO/$f is missing (PR pending)."; exit 1; }
done

base="$runtime/ljspeech_eval/build.json"
[ -f "$base" ] || { echo "REFUSING: no base build at $base"; exit 1; }
adapter="$runtime/ljspeech_eval/adapter"
[ -d "$adapter" ] || { echo "REFUSING: no adapter at $adapter"; exit 1; }

# TTS needs the card; llama-server holds ~8.7 GB. pkill -x matches the exact
# binary and cannot match this shell (Rule 22).
pkill -x llama-server 2>/dev/null && { echo "evicted llama-server"; sleep 20; }

ARMS=4
run_stage spread_build 30m -- \
    "$python" -u "$REPO/app/experiments/reference_spread.py" \
    --build "$base" --out-dir "$work" --arms "$ARMS" \
    --audio-root "$REPO" \
    --out "$runtime/experiments/reference_spread__en.json"

# The MANIFEST is the authority on which arms exist, not the filesystem. A
# corpus with few usable candidates yields fewer arms than requested, and
# probing for each build file would read as an artifact-existence skip - the
# pattern test_chain_skip_guards refuses, correctly, because elsewhere it hides
# a half-finished run.
built=$("$python" - "$runtime/experiments/reference_spread__en.json" <<'PYEOF'
import json, sys
try:
    doc = json.load(open(sys.argv[1]))
except Exception:
    sys.exit(1)
print(" ".join(str(a["arm"]) for a in doc.get("arms") or []))
PYEOF
) || { echo "REFUSING: the spread manifest is unreadable; no arm was built."; exit 1; }
[ -n "$built" ] || { echo "REFUSING: the manifest lists no arms."; exit 1; }
echo "arms built: $built"

scores=()
for i in $built; do
    build="$work/build_spread$i.json"
    run_stage "spread_gen_$i" 3h -- \
        env REQUIRE_VRAM_GB=4 "$REPO/gpu_job.sh" "spread_gen_$i" \
        "$python" -u "$REPO/app/experiments/ljspeech_generate.py" \
        --build "$build" --adapter "$adapter" --out-dir "$work/arm$i" \
        --arms clone --limit 0 \
        --out "$runtime/experiments/reference_spread__en_generate_arm$i.json"
    run_stage "spread_score_$i" 1h -- \
        "$python" -u "$REPO/app/experiments/ljspeech_score.py" \
        --generated "$runtime/experiments/reference_spread__en_generate_arm$i.json" \
        --limit 0 \
        --out "$runtime/experiments/reference_spread__en_score_arm$i.json"
    scores+=("$i=$runtime/experiments/reference_spread__en_score_arm$i.json")
    stage_commit_artifacts "spread_arm_$i" "$REPO"
done

if [ "${#scores[@]}" -ge 2 ]; then
    run_stage spread_compare 20m -- \
        "$python" -u "$REPO/app/experiments/reference_spread_compare.py" \
        --spread "$runtime/experiments/reference_spread__en.json" \
        --score "${scores[@]}" \
        --out "$runtime/experiments/reference_spread__en_compare.json"
    stage_commit_artifacts spread_compare "$REPO"
else
    echo "only ${#scores[@]} arm(s) scored; a correlation needs at least two"
fi

stage_summary reference_spread_20260821
