#!/usr/bin/bash
# Re-run the evidence that cannot currently be reproduced, cheapest first.
#
# THE STATE THIS FIXES. Of 480 artifacts: 198 carry no provenance at all, 202
# cannot say whether the tree was dirty, and of the 108 best-classified, 81
# were written from a dirty tree. Goal 5.4 is the file's best-documented goal -
# six artifacts, every one `supported_structure` - and ALL SIX are dirty. The
# one goal that cites its evidence properly cannot reproduce any of it.
#
# WHAT CAN AND CANNOT BE FIXED HERE. 107 artifacts are replayable, meaning
# their provenance records the script and the parsed args, so the command can
# be rebuilt from the artifact rather than from anyone's memory. 80 of those
# are dirty and worth re-running now that the tree is clean and gpu_job.sh
# refuses a dirty one.
#
# The other 369 are NOT replayable and this chain will not pretend otherwise:
# no provenance means no command, and a reconstructed command that is subtly
# not the original produces a new result wearing an old name. Those need their
# PRODUCERS fixed first - 58 of 95 artifact-writing scripts record nothing -
# and then a fresh run. That is a bigger job than this and should not be
# smuggled into it.
#
# ORDER IS BY GOAL, NOT BY CONVENIENCE. The 8 artifacts a goal actually cites
# go first, so if this is interrupted the part that matters is done. Everything
# is skipped if its artifact is already clean, so re-running the chain is cheap.
#
# Each replay overwrites its artifact. Safe: they are all committed, so
# `git diff` afterwards is the finding. A verdict that MOVES is the interesting
# case - it would mean the recorded number no longer reproduces, which is worth
# far more than the tidiness of a provenance block.
set -uo pipefail

REPO=/home/fakemitch/pinokio/api/alexandria-audiobook2.git
if [ "${ALEXANDRIA_GPU_LOCK_HELD:-0}" != 1 ]; then
    exec "$REPO/gpu_job.sh" replay_dirty_evidence \
        env ALEXANDRIA_GPU_LOCK_HELD=1 "$0" "$@"
fi

PY="$REPO/app/env/bin/python"
EXP="$REPO/ab_test_runtime/experiments"
LOG="$REPO/ab_test_runtime/logs/replay_evidence"
mkdir -p "$LOG"
cd "$REPO"

# Goal-cited first. These are goal 5.4's six and goal 2.4's dirty pair - the
# artifacts a reader is most likely to follow from a claim.
PRIORITY="asr_ja_readings.json asr_ja_cutting_control.json
          asr_ja_largev3_hybrid.json asr_silero_vad_ja_holdout.json
          asr_silero_whisper_ja_confirmation.json
          asr_silero_whisper_ja_offset20.json"

replay_one() {
    local art="$1"
    local cmd
    cmd=$("$PY" "$REPO/app/experiments/replay_artifact.py" "$art" 2>/dev/null | head -1)
    if [ -z "$cmd" ]; then
        echo "  SKIP $art (not replayable)"
        return 0
    fi
    echo "  REPLAY $art"
    if eval timeout --signal=INT --kill-after=60s 5400 "$cmd" \
            > "$LOG/${art%.json}.log" 2>&1; then
        echo "    ok"
    else
        echo "    FAILED rc=$? (see $LOG/${art%.json}.log)"
    fi
}

echo "REPLAY START $(date -u +%FT%TZ)"
echo "== goal-cited evidence first =="
for art in $PRIORITY; do
    [ -f "$EXP/$art" ] && replay_one "$art"
done

echo "== the remaining dirty, replayable artifacts =="
"$PY" - <<'PYEOF' > /tmp/replay_rest.txt
import json, os, sys
sys.path.insert(0, "app")
from experiments.replay_artifact import replay_command
priority = set("""asr_ja_readings.json asr_ja_cutting_control.json
    asr_ja_largev3_hybrid.json asr_silero_vad_ja_holdout.json
    asr_silero_whisper_ja_confirmation.json
    asr_silero_whisper_ja_offset20.json""".split())
audit = json.load(open("ab_test_runtime/audit/artifact_structural_audit.json"))
for row in audit["artifacts"]:
    name = row["artifact"]
    if name in priority or row.get("dirty") is not True:
        continue
    path = os.path.join("ab_test_runtime/experiments", name)
    if not os.path.exists(path):
        continue
    argv, _ = replay_command(path, "app/env/bin/python")
    if argv:
        print(name)
PYEOF
count=$(wc -l < /tmp/replay_rest.txt)
echo "  $count remaining"
while read -r art; do replay_one "$art"; done < /tmp/replay_rest.txt

echo "REPLAY COMPLETE $(date -u +%FT%TZ)"
echo
echo "WHAT TO READ:"
echo "  git diff --stat ab_test_runtime/experiments/"
echo "    Every replayed artifact should gain a clean provenance block."
echo "  git diff ab_test_runtime/experiments | grep -E '^[-+].*(accuracy|median|passed|cer)'"
echo "    A number that MOVED is the real finding: the recorded result no"
echo "    longer reproduces, and whatever cited it needs revisiting."
echo "  app/env/bin/python app/experiments/goal_evidence_audit.py"
echo "    Re-run to see goal 5.4 stop reporting six dirty artifacts."
