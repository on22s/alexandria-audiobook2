#!/usr/bin/bash
# Is it the hyphen that makes the voice pause?
#
# WHERE THIS CAME FROM. A blinded listening test rejected the respellings the
# ASR metric preferred - 0 of 4 on the terms that justified the -ay change -
# and six of eight notes said the same thing unprompted: weird pauses, robotic,
# "the biggest problem is the pausing". Measuring internal silence over 400
# terms found respelled clips pause where plain ones do not (341 of 384, sign
# test p=1.1e-58) and found NO difference between the two vowel rows (p=0.10).
# So the pause belongs to the form of a respelling, not to its vowels, and the
# hyphen is the obvious suspect.
#
# THREE ARMS, SAME TERMS, ONE VARIABLE. `seh-n-seh-ee` against `sehnsehee`,
# `seh n seh ee` and `seh·n·seh·ee`. Each writes its own artifact so the
# comparison stays paired and replayable.
#
# The ASR score is recorded but it is NOT the verdict here: it is the
# instrument that disagreed with the ear in the first place. The verdict is
# pauses measured on the produced audio, and then a second listening test.
set -uo pipefail

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python="$REPO/app/env/bin/python"
STAGE_LOG_DIR="$runtime/logs/separator_arms"
source "$REPO/run_chains/lib/stage.sh"
export GPU_QLOG="$runtime/logs/gpu_jobq.log"

artifact_complete() {
    "$1" "$REPO/app/experiments/respelling_completion.py" "$2" "$3"
}

for sep in none space dot; do
    out="$runtime/experiments/respelling_separator__${sep}.json"
    if [ -e "$out" ] && artifact_complete "$python" "$out" "120"; then
        stage_note "SKIP $sep (complete)"; continue
    fi
    run_stage "separator_$sep" 2h -- \
        "$REPO/gpu_job.sh" "separator_$sep" \
        "$python" -u "$REPO/app/experiments/measure_respellings.py" \
        --min-books 5 --only-e-row --separator "$sep" --limit 120 \
        --work "$runtime/respelling_sep_$sep" --out "$out"
    stage_commit_artifacts "separator_$sep" "$REPO" "$out"
done

# Pauses on what was just produced. This is the measurement that answers the
# question; the arms above only make the audio to measure.
run_stage separator_pauses 40m -- \
    "$python" -u "$REPO/app/experiments/measure_pauses.py" --limit 400 \
    --arm none=respelling_sep_none --arm space=respelling_sep_space \
    --arm dot=respelling_sep_dot \
    --out "$runtime/experiments/respelling_pauses_separators.json"

stage_summary separator_arms
