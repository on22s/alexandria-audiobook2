# Shared stage runner for run_chains/*.sh.  source "$(dirname "$0")/lib/stage.sh"
#
# WHY THIS EXISTS. 21 of 30 chains captured a per-item exit code, printed it,
# and never looked at it again. Bash discards each loop iteration's status and
# `set -e` does not reach inside a loop body, so on 2026-08-18 all 67 adapters
# of the re-gate failed with rc=2 while the chain printed REGATE COMPLETE and
# exited 0. The driver logged "OK regate". Two GPU hours measured nothing and
# looked like a finished stage.
#
# Databricks names that shape SUCCESS_WITH_FAILURES - a run green enough to
# notify success while containing real failures - and the remedy is a strict
# gate that fails when the parts did. That is what run_stage/stage_summary are.
#
# The other half is task-spooler's distinction between `-d` (run after the last
# job ENDS) and `-W` (run after it ends WELL, exit 0). Our chains have always
# silently meant the first. `run_stage --requires-ok NAME` makes the second
# sayable, so a stage that needs its predecessor's output cannot start on a
# predecessor that died.
#
# Nothing here takes the GPU lock: gpu_job.sh does that, and a stage that needs
# the card still goes through it.

STAGE_FAILURES=0
STAGE_TOTAL=0
declare -A STAGE_RESULT=()

stage_note() { echo "[$(date -u +%FT%TZ)] $*"; }

# run_stage <name> <timeout-spec> [--requires-ok OTHER]... -- <command...>
#
# Records the outcome under <name> so later stages can require it, writes the
# stage's own log, and counts failures for stage_summary.
# --needs-vram: stop llama-server before running this stage.
#
# WHY IT IS OPT-IN. llama-server is deliberately started OUTSIDE the lock so
# consecutive LLM stages share one 8.4 GB load; reclaiming before every stage
# would throw that away. But nothing ever reclaims it either, and on 2026-08-19
# continuation_20260819.sh started a server for its LLM stage and then had
# FIVE consecutive TTS stages refused with rc=7 (1568 MiB free, needs 4096) -
# the chain reported "6 of 9 failed" for a card that was simply still full.
# The stage that needs the memory is the one that knows, so it asks.
run_stage() {
    local name="$1" limit="$2"; shift 2
    local requires=() needs_vram=0
    while [ "${1:-}" = "--requires-ok" ] || [ "${1:-}" = "--needs-vram" ]; do
        if [ "$1" = "--needs-vram" ]; then
            needs_vram=1; shift
        else
            requires+=("$2"); shift 2
        fi
    done
    [ "${1:-}" = "--" ] && shift

    if [ "$needs_vram" = 1 ] && pgrep -x llama-server >/dev/null 2>&1; then
        # -x, never -f: `pkill -f` matches the command line of whatever is
        # doing the matching and has killed a shell in this repo (Rule 22).
        stage_note "  reclaiming VRAM from llama-server before $name"
        pkill -x llama-server
        # WAIT FOR THE MEMORY, NOT FOR A FIXED FIVE SECONDS. The driver frees
        # VRAM some time after the process exits, and a flat `sleep 5` lost the
        # separator_space arm on 2026-08-19: gpu_job.sh measured 939 MiB free
        # against a 4096 MiB floor and refused the stage, then the next stage
        # started five seconds later and ran perfectly on the same card. Poll
        # the same number gpu_job.sh gates on, so the two cannot disagree.
        local waited=0 free
        while [ "$waited" -lt "${STAGE_VRAM_WAIT:-90}" ]; do
            free=$(rocm-smi --showmeminfo vram 2>/dev/null \
                   | grep -im1 'total used memory' | grep -oE '[0-9]+' | tail -1)
            [ -n "$free" ] || break          # cannot tell: do not spin on it
            # `free` here is USED bytes; stop once the card is mostly clear.
            [ "$free" -lt 2147483648 ] && break
            sleep 3; waited=$((waited + 3))
        done
        stage_note "  VRAM reclaimed after ${waited}s"
    fi

    # -W semantics. A missing predecessor is NOT treated as satisfied: if the
    # chain was resumed and the earlier stage never ran in this process, we
    # cannot claim it ended well.
    local dep
    for dep in "${requires[@]}"; do
        if [ "${STAGE_RESULT[$dep]:-missing}" != "ok" ]; then
            stage_note "SKIP  $name (requires $dep, which is ${STAGE_RESULT[$dep]:-missing})"
            STAGE_RESULT["$name"]="skipped"
            return 0
        fi
    done

    local log="${STAGE_LOG_DIR:-/tmp}/${name}.log"
    mkdir -p "$(dirname "$log")"
    STAGE_TOTAL=$((STAGE_TOTAL + 1))
    stage_note "START $name (cap $limit, log ${log})"
    local started rc
    started=$(date +%s)
    # --signal=INT first so a python job runs its own cleanup and writes its
    # checkpoint; --kill-after is the backstop for one that ignores it.
    timeout --signal=INT --kill-after=120s "$limit" "$@" > "$log" 2>&1
    rc=$?
    local took=$(( $(date +%s) - started ))

    if [ "$rc" -eq 0 ]; then
        STAGE_RESULT["$name"]="ok"
        stage_note "OK    $name (${took}s)"
    else
        STAGE_RESULT["$name"]="failed:$rc"
        STAGE_FAILURES=$((STAGE_FAILURES + 1))
        # 124 is timeout's own code and means the cap was too small, which is a
        # different problem from the job failing - say which.
        if [ "$rc" -eq 124 ]; then
            stage_note "TIMEOUT $name after ${took}s (cap $limit) - see $log"
        else
            stage_note "FAIL  $name rc=$rc (${took}s) - see $log"
        fi
    fi
    return 0
}

# Commit artifacts between stages so one stage's OUTPUT cannot dirty the tree
# and get the next stage refused by gpu_job.sh's gate. Staged by path: this
# tree is shared with other sessions and `git add -A` would sweep up their
# work-in-progress.
stage_commit_artifacts() {
    local what="$1" repo="${2:-$PWD}"

    # DID THIS STAGE TURN A MEASUREMENT INTO AN EMPTY ONE? Every chain commits
    # through here, so it is the one place that can ask. dataset_ref_audit.json
    # went from 101 measured rows to `results: []` with no explanation, was
    # committed by this function, and sat on main for two days - the only
    # surviving copy of the rows was on two old feature branches nobody had
    # pruned yet.
    #
    # It commits ANYWAY and says so, rather than refusing. Refusing would leave
    # the tree dirty and the dirty-tree gate would then turn one bad artifact
    # into a dead queue - and the data was never actually lost, git had it all
    # along. What failed was that nobody looked. So the finding goes into the
    # commit message, where `git log` keeps it, and into the stage log.
    local shrink_report="" python="$repo/app/env/bin/python"
    if [ -x "$python" ]; then
        shrink_report=$("$python" "$repo/app/experiments/check_artifact_shrinkage.py" \
                        --repo "$repo" 2>&1 | grep -E "rows$|LOST ROWS" || true)
    fi

    git -C "$repo" add ab_test_runtime/experiments/ >/dev/null 2>&1 || return 0
    git -C "$repo" diff --cached --quiet && return 0

    local body="Committed by a chain so the dirty-tree gate does not refuse the next
stage on this stage's own output."
    if printf '%s' "$shrink_report" | grep -q "LOST ROWS"; then
        stage_note "  !! $what SHRANK AN ARTIFACT WITHOUT EXPLAINING IT:"
        printf '%s\n' "$shrink_report" | sed 's/^/     /'
        body="$body

ARTIFACTS LOST ROWS WITH NO EXPLANATION IN THIS STAGE:
$shrink_report

A run that measures less than the one before it either failed or changed what
it measures. This commit records which, because the previous time it happened
the empty version was indistinguishable from a success."
    fi

    git -C "$repo" commit -q -m "Artifacts from the $what stage

$body

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>" && stage_note "committed $what artifacts"
}

# THE STRICT GATE. Call this LAST. Nothing may run after it that could restore
# a zero exit - that is explicitly how the Databricks version of this bug comes
# back.
stage_summary() {
    local name="${1:-chain}"
    stage_note "SUMMARY $name: $((STAGE_TOTAL - STAGE_FAILURES))/$STAGE_TOTAL stages ok"
    local key
    for key in "${!STAGE_RESULT[@]}"; do
        [ "${STAGE_RESULT[$key]}" = "ok" ] || stage_note "  $key = ${STAGE_RESULT[$key]}"
    done
    if [ "$STAGE_FAILURES" -gt 0 ]; then
        stage_note "$name FAILED: $STAGE_FAILURES of $STAGE_TOTAL stages"
        return 1
    fi
    return 0
}
