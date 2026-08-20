#!/usr/bin/bash
# Finish a respelling arm that was cut short, from its own checkpoint.
#
# WHY THIS IS A SCRIPT AND NOT A ONE-LINER. The dot arm was interrupted at 487
# of 1600 by `gpu_pause.sh on --now`, and the chain that would have resumed it
# EXITED when the pause released its job - so nothing was going to finish it,
# and the four-arm table downstream reads all four work directories. A partial
# arm there does not make the table smaller, it makes it wrong: terms are
# ordered by book count, so a truncated arm is the commonest words only.
#
# That failure is not specific to `dot`, which is why this takes the arm as an
# argument. Any arm can be interrupted the same way.
#
# RESUMES, DOES NOT RESTART. measure_respellings reads its partial artifact and
# skips clips already present in --work, so this costs the remaining terms.
# Artifact-exists is NOT artifact-finished: a file at 487 of 1600 looks
# complete to `[ -f ]`, so completeness is asked of the artifact itself.
set -uo pipefail

SEP="${1:-}"
LIMIT="${2:-1600}"
if [ -z "$SEP" ]; then
    echo "usage: $(basename "$0") <none|space|dot|hyphen> [limit]" >&2
    exit 2
fi

REPO="$(cd "$(dirname "$0")/.." && pwd)"
runtime="$REPO/ab_test_runtime"
python=""   # set after queue.sh is sourced
STAGE_LOG_DIR="$runtime/logs/resume_partial_arm_20260826"
mkdir -p "$STAGE_LOG_DIR"
source "$REPO/run_chains/lib/stage.sh"
source "$REPO/run_chains/lib/queue.sh"
python=$(resolve_python "$REPO") || {
    echo "no interpreter: looked in this checkout and the main one" >&2
    exit 1; }

refuse_if_dirty "$REPO" || exit 1

out="$runtime/experiments/respelling_${SEP}_allrows_n${LIMIT}.json"
work="$runtime/respelling_${SEP}_allrows"

# COMPLETE / INCOMPLETE / CANNOT TELL are three answers, not two. The first
# version collapsed the third into "incomplete" and proceeded, so a missing
# interpreter started a five-hour re-run of an arm that was already finished.
# A check that cannot run must refuse, never fall through to the work.
set +e
"$python" - "$out" <<'PYEOF'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
except FileNotFoundError:
    sys.exit(1)            # nothing yet: resuming is correct
except Exception as exc:   # unreadable: say so rather than guess
    print(f"cannot read {sys.argv[1]}: {exc}", file=sys.stderr)
    sys.exit(2)
sys.exit(0 if d.get("status") == "complete" else 1)
PYEOF
state=$?
set -e
case "$state" in
    0) echo "$SEP is already complete; nothing to do"; exit 0 ;;
    1) : ;;
    *) echo "REFUSING: could not determine whether $SEP is complete." >&2
       echo "  Resuming blind would either redo a finished arm or skip a" >&2
       echo "  partial one. Fix the artifact or the interpreter first." >&2
       exit 1 ;;
esac

"$python" - "$out" <<'PYEOF' || true
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(f"  resuming from {len(d.get('results', []))} of "
          f"{d.get('candidates_considered')} terms")
except Exception:
    print("  no artifact yet; starting from the beginning")
PYEOF

run_stage "resume_${SEP}" 5h --needs-vram -- \
    "$REPO/gpu_job.sh" "resume_${SEP}" \
    "$python" -u "$REPO/app/experiments/measure_respellings.py" \
    --min-books 5 --separator "$SEP" --limit "$LIMIT" \
    --work "$work" --out "$out"
stage_commit_artifacts "resume_${SEP}" "$REPO"
stage_summary resume_partial_arm_20260826

"$python" - "$out" <<'PYEOF'
import json, sys
d = json.load(open(sys.argv[1]))
print(f"  {sys.argv[1].split('/')[-1]} -> {d.get('status')} "
      f"{len(d.get('results', []))} of {d.get('candidates_considered')}")
PYEOF
