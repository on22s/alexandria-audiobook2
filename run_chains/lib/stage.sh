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

source "$(dirname "${BASH_SOURCE[0]}")/server_cleanup.sh" || return 1

STAGE_FAILURES=0
STAGE_TOTAL=0
declare -A STAGE_RESULT=()

stage_note() { echo "[$(date -u +%FT%TZ)] $*"; }

validate_stage_files() {
    local path missing=0
    for path in "$@"; do
        if [ ! -f "$path" ] || [ ! -r "$path" ]; then
            stage_note "REFUSING: required file is missing or unreadable: $path"
            missing=1
        fi
    done
    [ "$missing" -eq 0 ]
}

record_stage_result() {
    local name="$1" rc="$2"
    if [ "$rc" -eq 0 ]; then
        STAGE_RESULT["$name"]="ok"
    else
        STAGE_RESULT["$name"]="failed:$rc"
        STAGE_FAILURES=$((STAGE_FAILURES + 1))
    fi
}

is_stage_successful() {
    [ "${STAGE_RESULT[$1]:-missing}" = ok ]
}

is_stage_dependency_name_valid() {
    [ -n "${1:-}" ] && [[ "$1" != --* ]]
}

# <name> <cap> <checker argv...> -- <run_stage options/worker argv...>
run_validated_cached_stage() {
    local name="$1" cap="$2" rc
    shift 2
    local checker=()
    while [ "$#" -gt 0 ] && [ "$1" != -- ]; do
        checker+=("$1"); shift
    done
    if [ "$#" -eq 0 ] || [ "${#checker[@]}" -eq 0 ]; then
        stage_note "REFUSING: completion checker and worker are required"
        exit 1
    fi
    shift
    local args=("$@") i=0
    while [ "${args[$i]:-}" = --needs-vram ] || [ "${args[$i]:-}" = --requires-ok ]; do
        if [ "${args[$i]}" = --requires-ok ]; then
            if ! is_stage_dependency_name_valid "${args[$((i + 1))]:-}"; then
                stage_note "REFUSING: --requires-ok needs a dependency name"
                STAGE_TOTAL=$((STAGE_TOTAL + 1))
                record_stage_result "$name" 2
                return 2
            fi
            if ! is_stage_successful "${args[$((i + 1))]}"; then
                run_stage "$name" "$cap" "$@"
                exit 1
            fi
            i=$((i + 2))
        else
            i=$((i + 1))
        fi
    done
    "${checker[@]}"
    rc=$?
    if [ "$rc" -eq 0 ]; then
        STAGE_TOTAL=$((STAGE_TOTAL + 1))
        record_stage_result "$name" 0
        stage_note "SKIP $name (validated cache)"
        return 0
    fi
    if [ "$rc" -ne 1 ]; then
        stage_note "REFUSING: cannot validate $name cache"
        exit 1
    fi
    run_stage "$name" "$cap" "$@"
    if ! is_stage_successful "$name"; then
        stage_note "REFUSING: $name failed; retaining its evidence"
        exit 1
    fi
    if ! "${checker[@]}"; then
        record_stage_result "$name" 1
        stage_note "REFUSING: $name did not publish a complete output"
        exit 1
    fi
}

# run_stage <name> <timeout-spec> [--requires-ok OTHER]... -- <command...>
#
# Records the outcome under <name> so later stages can require it, writes the
# stage's own log, and counts failures for stage_summary.
# --needs-vram: request supervised reclamation after the queue acquires its lock.
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
            if ! is_stage_dependency_name_valid "${2:-}"; then
                stage_note "REFUSING: --requires-ok needs a dependency name"
                STAGE_TOTAL=$((STAGE_TOTAL + 1))
                record_stage_result "$name" 2
                return 2
            fi
            requires+=("$2"); shift 2
        fi
    done
    [ "${1:-}" = "--" ] && shift

    # -W semantics. A missing predecessor is NOT treated as satisfied: if the
    # chain was resumed and the earlier stage never ran in this process, we
    # cannot claim it ended well.
    local dep
    for dep in "${requires[@]}"; do
        if ! is_stage_successful "$dep"; then
            stage_note "SKIP  $name (requires $dep, which is ${STAGE_RESULT[$dep]:-missing})"
            STAGE_RESULT["$name"]="skipped"
            STAGE_TOTAL=$((STAGE_TOTAL + 1))
            STAGE_FAILURES=$((STAGE_FAILURES + 1))
            return 0
        fi
    done


    local log="${STAGE_LOG_DIR:-/tmp}/${name}.log"
    mkdir -p "$(dirname "$log")"
    STAGE_TOTAL=$((STAGE_TOTAL + 1))
    stage_note "START $name (cap $limit, log ${log})"
    local started rc diagnostic timed_out=0
    started=$(date +%s)
    diagnostic=$(mktemp "${log}.timeout.XXXXXX") || {
        record_stage_result "$name" 1
        stage_note "FAIL  $name (could not create timeout diagnostic)"
        return 0
    }
    # --signal=INT first so a python job runs its own cleanup and writes its
    # checkpoint; --kill-after is the backstop for one that ignores it.
    if [ "$needs_vram" = 1 ]; then
        local queue="$(readlink -f -- "$(dirname "${BASH_SOURCE[0]}")/../../gpu_job.sh")"
        local command=("$@")
        # Existing queued stages retain their name/argv. Direct commands and
        # self-queuing chains enter once here; inherited owner proof lets the
        # latter skip their own acquisition without nesting flock.
        if [ "$(readlink -f -- "$1")" != "$queue" ]; then
            command=("$queue" "$name" "$@")
        fi
        GPU_RECLAIM_VRAM=1 timeout --verbose --signal=INT --kill-after=120s "$limit" bash -c 'exec "$@" 2>&1' stage-worker "${command[@]}" > "$log" 2> "$diagnostic"
        rc=$?
    else
        timeout --verbose --signal=INT --kill-after=120s "$limit" bash -c 'exec "$@" 2>&1' stage-worker "$@" > "$log" 2> "$diagnostic"
        rc=$?
    fi
    if [ "$rc" -eq 124 ] && [ -s "$diagnostic" ]; then
        timed_out=1
    fi
    cat "$diagnostic" >> "$log"
    rm -f "$diagnostic"
    local took=$(( $(date +%s) - started ))

    record_stage_result "$name" "$rc"
    if [ "${STAGE_RESULT[$name]}" = "ok" ]; then
        stage_note "OK    $name (${took}s)"
    else
        # The command may itself exit 124. Only timeout's private diagnostic
        # proves its timer fired; worker stderr remains in the stage log.
        if [ "$timed_out" -eq 1 ]; then
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
    local what="${1:-unnamed}" repo="${2:-$PWD}"
    if [ "$#" -lt 3 ]; then
        stage_note "REFUSING artifact commit: explicit experiment files are required"
        record_stage_artifact_failure "$what" 2
        return 2
    fi
    local artifact_paths=()
    shift 2
    local root path source relative rc
    if root=$(git -C "$repo" rev-parse --show-toplevel); then
        :
    else
        rc=$?
        record_stage_artifact_failure "$what" "$rc"
        return "$rc"
    fi
    repo="$root"
    artifact_paths=()
    for path in "$@"; do
        if [[ "$path" = /* ]]; then
            source="$path"
        else
            source="$root/$path"
        fi
        relative=$(realpath -m --relative-to="$root" -- "$source") || {
            record_stage_artifact_failure "$what" 2
            return 2
        }
        if [[ "$relative" != ab_test_runtime/experiments/* ]] || [ -d "$root/$relative" ]; then
            stage_note "REFUSING artifact commit: expected individual experiment files"
            record_stage_artifact_failure "$what" 2
            return 2
        fi
        if [ ! -e "$root/$relative" ] && [ ! -L "$root/$relative" ]; then
            if git -C "$repo" ls-files --error-unmatch -- ":(literal)$relative" >/dev/null 2>&1; then
                :
            else
                rc=$?
                if [ "$rc" -eq 1 ]; then
                    continue
                fi
                record_stage_artifact_failure "$what" "$rc"
                return "$rc"
            fi
        fi
        artifact_paths+=(":(literal)$relative")
    done
    [ "${#artifact_paths[@]}" -gt 0 ] || return 0

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

    local rc
    if git -C "$repo" add -- "${artifact_paths[@]}" >/dev/null; then
        :
    else
        rc=$?
        record_stage_artifact_failure "$what" "$rc"
        return "$rc"
    fi
    if git -C "$repo" diff --cached --quiet -- "${artifact_paths[@]}"; then
        return 0
    else
        rc=$?
        if [ "$rc" -ne 1 ]; then
            record_stage_artifact_failure "$what" "$rc"
            return "$rc"
        fi
    fi

    local body="Committed by a chain so the dirty-tree gate does not refuse the next
stage on this stage's own output."
    if printf '%s' "$shrink_report" | grep -q "LOST ROWS"; then
        stage_note "  !! $what SHRANK AN ARTIFACT WITHOUT EXPLAINING IT:"
        printf '%s\n' "$shrink_report" | sed 's/^/     /'
        body="$body

ARTIFACTS LOST ROWS OBSERVED WHILE COMMITTING THIS STAGE:
$shrink_report

A run that measures less than the one before it either failed or changed what
it measures. This commit records which, because the previous time it happened
the empty version was indistinguishable from a success."
    fi

    if git -C "$repo" commit -q --only -m "Artifacts from the $what stage

$body

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>" \
        -- "${artifact_paths[@]}"; then
        stage_note "committed $what artifacts"
    else
        rc=$?
        record_stage_artifact_failure "$what" "$rc"
        return "$rc"
    fi
}

record_stage_artifact_failure() {
    local what="$1" rc="$2"
    STAGE_TOTAL=$((STAGE_TOTAL + 1))
    record_stage_result "artifacts:$what" "$rc"
    stage_note "FAIL  artifacts:$what rc=$rc (artifact commit did not complete)"
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
