#!/bin/bash
# Serialise GPU work and make its failures loud.
#
# WHY THIS EXISTS. Long experiment runs get chained - train, then evaluate,
# then serve and evaluate again - and every chain written ad hoc so far
# repeated the same three mistakes:
#
#   1. `while pgrep -f <something>; do sleep; done` as a wait. That guesses at
#      what else is running and races between "is it running?" and "start
#      mine", and pgrep also matches the shell that ran it.
#   2. Ambiguous server ownership. One script started llama-server and a
#      different one pkilled it on exit, so whether a server existed depended
#      on script ordering.
#   3. No mutual exclusion. Nothing prevented two GPU jobs overlapping except
#      the author sequencing them correctly by hand.
#
# Two runs died on that on 2026-08-04 - an eval that fired at a server still
# loading its weights, and a retry that found the server killed by its parent.
# Neither failure was scientific, and both cost GPU hours.
#
# Usage:
#   ./gpu_job.sh <name> <command...>
#
# Environment:
#   GPU_LOCK   lock file path      (default ~/.gpu.lock)
#   GPU_QLOG   queue log path      (default ~/gpu_jobq.log)
#
# Guarantees:
#   - exactly one job holds the GPU at a time, via flock on a real file. A
#     second invocation BLOCKS rather than racing.
#   - start, end and exit code are appended to the queue log, so what ran and
#     what it returned survives without terminal scrollback.
#     Retention: 8 MiB per file, three archives, and the active START carried
#     forward. A pre-existing oversized log is archived intact and ages out.
#   - a non-zero exit is recorded with a FAILED marker and propagated, instead
#     of being swallowed by the next command in a chain.
#
# Reclamation is opt-in via GPU_RECLAIM_VRAM=1 and runs under the same
# supervised lease as the job. This wrapper does not retry or interpret results.
set -uo pipefail

# Bash may search PATH while leaving a bare name in BASH_SOURCE. Resolve that
# search and symlink aliases before deriving any repository state paths.
SCRIPT_PATH="${BASH_SOURCE[0]}"
if [ ! -f "$SCRIPT_PATH" ]; then
    SCRIPT_PATH=$(type -P -- "$SCRIPT_PATH") || {
        echo "gpu_job: cannot locate its script" >&2
        exit 4
    }
fi
SCRIPT_PATH=$(readlink -f -- "$SCRIPT_PATH") || {
    echo "gpu_job: cannot resolve its script path" >&2
    exit 4
}
REPO=$(cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd -P) || exit 4

# One source policy for the queue gate and the Python experiment manifest.
# Only declared generated outputs are excluded; suffixes cannot tell an input
# from a note, fixture, configuration file or extensionless executable.
SOURCE_TRACKED_PATHS=(':(exclude)ab_test_runtime/experiments/*.json'
    ':(exclude)ab_test_runtime/audit/*.json' ':(exclude)RESULTS_INDEX.md'
    ':(exclude)results_index.csv' ':(exclude)LEGACY_ATTRIBUTION_AUDIT_*.md')
SOURCE_UNTRACKED_PATHS=(':(exclude)ab_test_runtime/*')

get_untracked_source_files() {
    git -C "$1" ls-files --others --exclude-standard -z -- "${SOURCE_UNTRACKED_PATHS[@]}"
}

get_source_state() {
    local root="$1" modified hash state list_fd list_pid
    local -a untracked
    if ! git -C "$root" rev-parse --git-dir >/dev/null 2>&1; then
        printf 'unknown\0\0'
        return
    fi
    modified=$(git -C "$root" status --porcelain --untracked-files=no -- "${SOURCE_TRACKED_PATHS[@]}") || {
        printf 'unknown\0\0'
        return
    }
    exec {list_fd}< <(get_untracked_source_files "$root")
    list_pid=$!
    mapfile -d '' -t untracked <&"$list_fd"
    exec {list_fd}<&-
    if ! wait "$list_pid"; then
        printf 'dirty:unknown\0%s\0' "$modified"
        return
    fi
    state=clean
    if [ -n "$modified" ] || [ "${#untracked[@]}" -gt 0 ]; then
        # Actual tracked changes plus untracked names, modes and bytes. NUL
        # boundaries and sha256sum's escaped filenames preserve odd names.
        if hash=$({
            git -C "$root" diff --binary HEAD -- "${SOURCE_TRACKED_PATHS[@]}" || exit 1
            printf '%s\0' "$modified"
            if [ "${#untracked[@]}" -gt 0 ]; then
                (cd -- "$root" && {
                    stat -c '%a:%F:%N' -- "${untracked[@]}" &&
                    sha256sum -- "${untracked[@]}"
                }) || exit 1
            fi
        } | sha256sum | cut -c1-12); then
            state="dirty:$hash"
        else
            state=dirty:unknown
        fi
    fi
    printf '%s\0%s\0' "$state" "$modified"
    if [ "${#untracked[@]}" -gt 0 ]; then
        printf '%s\0' "${untracked[@]}"
    fi
}

# NUL-delimited read-only interface: state, tracked status, untracked paths.
if [ "${1:-}" = "--print-source-state" ]; then
    [ "$#" -eq 2 ] || { echo "gpu_job: --print-source-state needs a repository" >&2; exit 2; }
    get_source_state "$2"
    exit $?
fi

# ONE LOCK, and it is the one the chains use. This defaulted to
# $HOME/.gpu.lock while 21 chain lines export
# ab_test_runtime/logs/alexandria_gpu.lock and 15 more export
# $HOME/.alexandria_gpu.lock - three files, none of which serialise against
# each other. Measured 2026-08-19 with a chain job running: the repo lock was
# HELD and both home-directory locks were FREE, so a hand-run ./gpu_job.sh
# would have taken a different lock, found it free, and run a second job on
# the card the queue exists to protect.
if [ -n "${GPU_LOCK:-}" ]; then
    # OPERATOR-SUPPLIED: take it exactly as given. Creating the directory for
    # it would turn a mistyped path into a brand-new lock that is always free -
    # the same failure as the three-lock split above, one caller at a time.
    LOCK="$GPU_LOCK"
else
    # ABSOLUTE. `dirname "$0"` is relative whenever the script is invoked as
    # ./gpu_job.sh, and a relative lock path is a DIFFERENT FILE for a caller
    # with a different working directory - the split this block exists to end.
    LOCK="${REPO}/ab_test_runtime/logs/alexandria_gpu.lock"
    if [ "${1:-}" != "--check-lock-owner" ]; then
        mkdir -p "$(dirname "$LOCK")" 2>/dev/null   # fresh clone has no logs/ yet
    fi
fi
# Read-only inheritance proof: the claimed owner must be a kernel-reported
# ancestor holding an exclusive flock on fd9 for this exact lock inode.
if [ "${1:-}" = "--check-lock-owner" ]; then
    refuse_lock_owner() {
        echo "gpu_job.sh: cannot verify inherited GPU lock: $*" >&2
        exit 1
    }
    [ "$#" -eq 2 ] || [ "$#" -eq 3 ] || refuse_lock_owner "expected owner PID and optional descriptor"
    owner_fd="${3-9}"
    case "$owner_fd" in
        ''|0*|*[!0-9]*) refuse_lock_owner "invalid owner descriptor" ;;
    esac
    owner="$2"
    case "$owner" in
        ''|0*|*[!0-9]*) refuse_lock_owner "invalid owner PID" ;;
    esac
    cursor="$$"
    owner_found=0
    seen=" "
    while [ "$cursor" -gt 0 ]; do
        if [ "$cursor" = "$owner" ]; then
            owner_found=1
            break
        fi
        case "$seen" in
            *" $cursor "*) refuse_lock_owner "cyclic or changing process ancestry" ;;
        esac
        seen="$seen$cursor "
        parent=""
        [ -r "/proc/$cursor/status" ] || refuse_lock_owner "process ancestry is unreadable"
        while IFS=$' \t' read -r field value; do
            if [ "$field" = "PPid:" ]; then
                parent="$value"
                break
            fi
        done < "/proc/$cursor/status"
        case "$parent" in
            ''|*[!0-9]*) refuse_lock_owner "invalid kernel parent PID for $cursor: '${parent}'" ;;
        esac
        cursor="$parent"
    done
    [ "$owner_found" -eq 1 ] || refuse_lock_owner "owner PID is not an ancestor"
    lock_identity=$(stat -Lc '%d:%i' -- "$LOCK") || refuse_lock_owner "authoritative lock is unreadable"
    owner_identity=$(stat -Lc '%d:%i' -- "/proc/$owner/fd/$owner_fd") || refuse_lock_owner "owner fd$owner_fd is unreadable"
    [ "$lock_identity" = "$owner_identity" ] || refuse_lock_owner "owner fd$owner_fd names another file"
    awk '$1 == "lock:" && $3 == "FLOCK" && $4 == "ADVISORY" && $5 == "WRITE" { held=1 }
         END { exit !held }' "/proc/$owner/fdinfo/$owner_fd" || refuse_lock_owner "owner fd$owner_fd has no acquired exclusive flock"
    # A disappearing owner or replaced file during the probe must fail closed.
    [ "$(stat -Lc '%d:%i' -- "/proc/$owner/fd/$owner_fd")" = "$lock_identity" ] || refuse_lock_owner "owner fd$owner_fd changed"
    [ "$(stat -Lc '%d:%i' -- "$LOCK")" = "$lock_identity" ] || refuse_lock_owner "authoritative lock changed"
    exit 0
fi

# ASK, DO NOT PARSE. app/experiments/gpu_guard.py used to recover this path by
# regexing the assignment above, which meant reformatting one shell line
# silently changed which file Python thought was the lock - and its fallback
# was a plausible-looking path rather than an error, so the break would not
# have announced itself. This makes gpu_job.sh the single source of the answer
# (Rule 15) and a wrong answer impossible to get quietly.
if [ "${1:-}" = "--print-lock" ]; then
    echo "$LOCK"
    exit 0
fi

# THE SAME SPLIT THE LOCK HAD, one file over. 37 of 51 chains export GPU_QLOG
# pointing at the repo log; the other 14 - and every hand-run job - wrote their
# provenance to $HOME/gpu_jobq.log instead, where gpu_pause.sh cannot see it and
# no analysis reads it. 110 lines of queue history accumulated there between
# 2026-08-05 and 2026-08-19, including entire ASR benchmark runs that appear
# nowhere in the repository. Archived as
# ab_test_runtime/logs/gpu_jobq_home_archive_20260819.log; the default now
# points where everything else already does.
if [ -n "${GPU_QLOG:-}" ]; then
    QLOG="$GPU_QLOG"
else
    QLOG="${REPO}/ab_test_runtime/logs/gpu_jobq.log"
    mkdir -p "$(dirname "$QLOG")" 2>/dev/null
fi

if [ "${1:-}" = "--print-qlog" ]; then
    echo "$QLOG"
    exit 0
fi

ACTION=run
if [ "${1:-}" = "--check-llm" ]; then
    ACTION=check_llm
    shift
fi
if [ "${1:-}" = "--check-vram" ]; then
    ACTION=check_vram
    shift
fi
if [ "${1:-}" = "--write-owner-result" ]; then
    ACTION=write_owner_result
    shift
fi
NAME="${1:-}"
[ -z "$NAME" ] && { echo "usage: gpu_job.sh <name> <command...>" >&2; exit 2; }
if [[ "$NAME" == *"/"* || "$NAME" == *"\\"* || "$NAME" =~ [[:cntrl:]] ]]; then
    echo "gpu_job: job name must not contain path separators or control characters" >&2
    exit 2
fi
shift
if [ "$ACTION" = check_vram ] || [ "$ACTION" = check_llm ]; then
    [ "$#" -eq 0 ] || exit 2
    bash "$SCRIPT_PATH" --check-lock-owner "${ALEXANDRIA_GPU_LOCK_PID:-0}" "${ALEXANDRIA_GPU_LOCK_FD:-9}" || exit 4
elif [ "$ACTION" = write_owner_result ]; then
    [ "$#" -eq 2 ] || exit 2
    case "$1" in ''|*[!0-9]*) exit 2;; esac
    [ "${#1}" -le 3 ] && [ "$1" -le 255 ] || exit 2
    case "$2" in ''|0*|*[!0-9]*) exit 2;; esac
    RESULT_CODE="$1"
    PENDING_OWNER="$2"
    bash "$SCRIPT_PATH" --check-lock-owner "${ALEXANDRIA_GPU_LOCK_PID:-0}" || exit 4
else
    [ "$#" -eq 0 ] && { echo "gpu_job.sh: no command given" >&2; exit 2; }
fi

stamp() { date -u +%FT%TZ; }

# Queue records are required; an unrecorded run must never look successful.
source "$REPO/run_chains/lib/gpu_queue_log.sh" || exit 4
write_queue_log() {
    append_queue_log "$@" || exit 8
}

if [ "$ACTION" = check_llm ]; then
    source "$REPO/run_chains/lib/gpu_llm.sh" || exit 4
    check_gpu_llm
    exit 0
fi

if [ "$ACTION" = check_vram ]; then
    source "$REPO/run_chains/lib/gpu_vram.sh" || exit 4
    check_gpu_vram
    exit 0
fi

PENDING_DIR="${GPU_PENDING_DIR:-${REPO}/ab_test_runtime/logs/pending}"
PENDING_FILE="$PENDING_DIR/${PENDING_OWNER:-$$}.$NAME"
source "$REPO/run_chains/lib/gpu_pending.sh" || exit 4
# DEFINED HERE, ABOVE THE TRAP THAT CALLS THEM. bash defines a function when
# it READS that line, and both of these used to sit below `wait`. A job
# interrupted while waiting ran the TERM trap, which called log_result - a
# function bash had not reached yet - so the `type log_result` guard silently
# declined and no terminal marker was written at all. The guard turned a
# crash into silence, which is precisely the failure it existed to prevent.
# NOTIFY WHEN THE CARD GOES IDLE. task-spooler mails you when a job finishes
# (`ts -m`); pterm push is the local equivalent and this repo had zero uses of
# it. An idle GPU at 2am should announce itself rather than be discovered
# hours later by someone reading a log. Only fires when NOTHING is pending -
# a job finishing with more queued behind it is not idleness.
#
# Best-effort by design: no notifier, or a failing one, must never change the
# job's exit code, so everything here is swallowed.
notify_if_idle() {
    local outcome="$1"
    rm -f "$PENDING_FILE" 2>/dev/null
    local waiting=0 marker
    for marker in "$PENDING_DIR"/*; do
        [ -f "$marker" ] || continue
        if is_pending_marker_live "$marker"; then
            waiting=$((waiting + 1))
        fi
    done
    [ "${waiting:-0}" -gt 0 ] && return 0
    command -v pterm >/dev/null 2>&1 || return 0

    # OFF SWITCH FIRST. GPU_NOTIFY=0 silences this entirely; a notification is
    # a courtesy and must never be something the user has to tolerate.
    [ "${GPU_NOTIFY:-1}" = "0" ] && return 0

    # AND A COOLDOWN, because "the queue just drained" is one event and this
    # function fires per JOB. The re-gate chain runs 67 jobs, each finishing
    # with nothing queued behind it in that instant, so it produced 67 desktop
    # alerts saying the same thing. One notification per quiet period is the
    # information; the other 66 are noise that trains you to ignore all of them.
    # NOT in PENDING_DIR: everything there is a claim that a job is waiting,
    # and a stamp file sitting among them reads as exactly the stale marker
    # `gpu_pause.sh status` exists to report.
    local stamp_file
    stamp_file="$(dirname "$QLOG")/.gpu_last_notify"
    local now last
    now=$(date +%s)
    last=$(cat "$stamp_file" 2>/dev/null || echo 0)
    [ $((now - last)) -lt "${GPU_NOTIFY_COOLDOWN:-900}" ] && return 0
    echo "$now" > "$stamp_file" 2>/dev/null
    pterm push "GPU idle: $NAME $outcome, nothing queued" >/dev/null 2>&1 || true
}

# EVERY STARTED JOB GETS EXACTLY ONE TERMINAL MARKER, from one function that
# every exit path reaches.
#
# THE BUG THIS FIXES, observed and unexplained until now. On 2026-08-17
# e_row_e finished - artifact complete, 400 of 400, the next arm queued one
# second later - and neither OK nor FAILED was ever written. The markers were
# echoed only on the normal fall-through, so any exit through the INT/TERM
# trap (`exit 130` / `exit 143`) left a START with no terminal line. Nothing
# downstream can tell that from a job still running: `gpu_pause.sh status`
# insisted the card was busy for 24 minutes while it sat idle, and I relayed
# that to the user as fact.
#
# Guarded against double-logging, because the trap also fires after the normal
# path has already written its line.
log_result() {
    [ "${RESULT_LOGGED:-0}" = 1 ] && return 0
    RESULT_LOGGED=1
    local code="$1"
    if [ "$code" -eq 0 ]; then
        write_queue_log "$(stamp) OK       $NAME"
        notify_if_idle "finished"
    elif [ "$code" -eq 129 ] || [ "$code" -eq 130 ] || [ "$code" -eq 143 ]; then
        # Interrupted is not the same as failed: the job was told to stop, so
        # its absence of a result is expected rather than a defect to chase.
        write_queue_log "$(stamp) INTERRUPTED $NAME rc=$code"
        notify_if_idle "interrupted"
    else
        # Loud on purpose. A chained job that fails quietly gets read as a result.
        write_queue_log "$(stamp) FAILED   $NAME rc=$code"
        echo "gpu_job: $NAME FAILED rc=$code" >&2
        notify_if_idle "FAILED rc=$code"
    fi
}


if [ "$ACTION" = write_owner_result ]; then
    trap 'rm -f "$PENDING_FILE" 2>/dev/null' EXIT
    log_result "$RESULT_CODE"
    exit 0
fi

write_queue_log "$(stamp) QUEUED   $NAME"

# A PENDING MARKER, BECAUSE A WAITING JOB AND A DEAD CHAIN LOOK IDENTICAL.
# Everything queued behind the lock is just a blocked process; nothing lists
# it. Twice on 2026-08-18 a chain died leaving "QUEUED x" as the last word in
# the log, and the queue looked busy while the card sat idle - 80 minutes once,
# an hour the second time. task-spooler answers this with `ts -l`; this is the
# poor relation of that, one file per waiting job, removed on exit however the
# job ends. `gpu_pause.sh status` reads them.
# OVERRIDABLE, LIKE EVERY OTHER PATH HERE. Hardcoding it meant the test suite
# wrote markers into the REAL pending directory - four stale entries showed up
# in `gpu_pause.sh status` within an hour, each claiming a chain had died. That
# is the fourth variable to leak from the harness into live state after
# GPU_LOCK, GPU_QLOG and GPU_PAUSE_FLAG; isolated_env pins this one too.
trap 'rm -f "$PENDING_FILE" 2>/dev/null' EXIT
if ! mkdir -p "$PENDING_DIR" || ! save_pending_marker "$PENDING_FILE" "$NAME" "$$" "$(stamp)"; then
    echo "gpu_job: cannot create pending marker $PENDING_FILE; refusing to queue $NAME" >&2
    write_queue_log "$(stamp) PENDING_FAILED $NAME"
    exit 8
fi


# The trap is set BEFORE the pause wait and the lock: a job interrupted while
# still queued must not leave a marker claiming it is pending forever.
# STARTED is set once the job is actually running; before that the early exits
# (usage, REFUSED, NO_VRAM, NO_LLM) write their own markers and must not get a
# second one. `type log_result` guards the window where the trap exists but the
# function is not defined yet.
trap 'ec=$?; rm -f "$PENDING_FILE" 2>/dev/null; \
      if [ "${STARTED:-0}" = 1 ] && type log_result >/dev/null 2>&1; then \
          log_result "$ec"; fi' EXIT

# Waiting jobs own no worker yet; stop explicitly before another pause sleep.
trap 'exit 130' INT
trap 'exit 143' TERM
trap 'exit 129' HUP

# WAIT FOR THE PAUSE FLAG BEFORE TAKING THE LOCK, not after. A job that holds
# the lock while waiting would block the queue AND look like it was working;
# waiting first means a paused queue is simply idle, and `gpu_pause.sh status`
# can tell the truth about what still holds the card.
PAUSE_FLAG="${GPU_PAUSE_FLAG:-${REPO}/ab_test_runtime/logs/gpu_paused}"
if [ -f "$PAUSE_FLAG" ]; then
    write_queue_log "$(stamp) HELD     $NAME (queue paused)"
    echo "gpu_job: queue is paused; $NAME is waiting. Release with:" >&2
    echo "gpu_job:   ./gpu_pause.sh off" >&2
    while [ -f "$PAUSE_FLAG" ]; do sleep 20; done
    write_queue_log "$(stamp) RELEASED $NAME"
fi

exec 9>"$LOCK" || {
    write_queue_log "$(stamp) LOCK_FAILED $NAME (cannot open $LOCK)"
    echo "gpu_job: cannot open lock file $LOCK" >&2
    exit 4
}
# The lock MUST be a gate, not a suggestion. `set -e` is deliberately not on
# here (the wrapped command's exit code has to survive), so an unchecked
# `flock` would fall through to running the command on failure - defeating the
# entire purpose of this script. A concurrent GPU job is exactly what cost 42
# minutes of training on 2026-08-04.
if ! flock 9; then
    write_queue_log "$(stamp) LOCK_FAILED $NAME (flock failed)"
    echo "gpu_job: failed to acquire GPU lock; refusing to run $NAME" >&2
    exit 4
fi
# DEPLOYMENT IDENTITY, written BEFORE the job starts rather than reconstructed
# after it fails. On 2026-08-04 two jobs died because the box was running a
# superseded copy of this very script and calling a helper that did not exist
# there. Nothing announced either; both were found by reading logs afterwards.
# A commit, a dirty-tree hash and a SHA-256 of the executable would have made
# both visible at the moment they happened.
#
# Recorded on a best-effort basis: a missing `git` or an unreadable file must
# degrade to "unknown" and must never stop the job. Identity is evidence, not
# a gate.
# ONE definition of "is this tree dirty", because the stamp below and the gate
# further down must never disagree about it - a gate that lets through what the
# provenance line calls dirty is worse than no gate.
tree_state() {
    get_source_state "$REPO" | {
        local state
        IFS= read -r -d '' state
        printf '%s\n' "$state"
    }
}
dirty_state=$(tree_state)

identity() {
    local commit script_sha gpu command_text
    commit=$(git -C "${REPO}" rev-parse --short HEAD 2>/dev/null) \
        || commit=unknown
    local dirty="$dirty_state"
    script_sha=$(sha256sum "$SCRIPT_PATH" 2>/dev/null | cut -c1-12)
    gpu=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
    [ -z "$gpu" ] && gpu=$(rocm-smi --showproductname 2>/dev/null \
        | grep -oPm1 '(?<=Card Series:).*' | xargs) 
    printf -v command_text '%q ' "$@"
    echo "$(stamp) IDENT    $NAME commit=$commit tree=$dirty" \
         "gpu_job_sha=${script_sha:-unknown} host=$(hostname)" \
         "gpu=${gpu:-unknown} cmd=${command_text% }"
}
write_queue_log "$(identity "$@")"

# A DIRTY TREE IS NOW A GATE, NOT JUST A NOTE. The identity block above has
# recorded `tree=dirty` since 2026-08-04 and nothing ever read it: 86 of 178
# recorded runs - 48% - produced evidence from code that was never committed.
# `respelling_rule_b.json` is one of them, and its source existed only in one
# machine's working directory; the artifact was committed, the code was not,
# and that was found by accident weeks later.
#
# An experiment whose code cannot be recovered is not reproducible, and an
# irreproducible number is worth less than no number, because it still gets
# quoted. Refusing costs one commit. Not refusing costs the result.
#
# The override exists because a genuine mid-debug run is a real thing:
#
#     ALLOW_DIRTY_TREE=1 ./gpu_job.sh <name> <cmd...>
#
# It is deliberately noisy and still stamps `tree=dirty`, so an overridden run
# is never mistaken afterwards for a clean one.
case "$dirty_state" in
  dirty:*)
    if [ "${ALLOW_DIRTY_TREE:-0}" = "1" ]; then
        # CAPTURE THE DIFF INSTEAD OF ONLY NAMING IT. The override exists for
        # "I know it is dirty, run anyway" - which is exactly where the code
        # state is least recoverable, because the next edit erases it. Logging
        # DIRTY_RUN and nothing else left the artifact permanently
        # unreproducible: a note saying "this was dirty, good luck".
        #
        # Weights & Biases does this for every run with local modifications,
        # writing diff.patch "relative to HEAD" alongside the run, so an
        # uncommitted state stays inspectable afterwards. Same idea, minus the
        # server: `git apply` the patch on the recorded commit and you have
        # the tree that produced the artifact.
        #
        # Untracked harness files are appended with --no-index, because a new
        # experiment script is untracked for exactly as long as it takes to
        # write and run it - the case the dirty gate exists for - and
        # `git diff HEAD` cannot see it. --no-index exits 1 on difference,
        # which is its normal result here, so its status is deliberately
        # ignored rather than treated as failure.
        # FIFTH KNOB THAT POINTED AT REAL SHARED STATE. The suite runs
        # this path on every dirty-tree test and left 202 patch files in
        # the working tree, untracked and unwanted. isolated_env pins it
        # like GPU_LOCK, GPU_QLOG, GPU_PAUSE_FLAG and GPU_PENDING_DIR.
        patch_dir="${GPU_PATCH_DIR:-${REPO}/ab_test_runtime/logs/dirty_patches}"
        patch_file="$patch_dir/${NAME}-$(date -u +%Y%m%dT%H%M%SZ).patch"
        if mkdir -p "$patch_dir" 2>/dev/null; then
            {
                git -C "${REPO}" diff --binary HEAD 2>/dev/null
                # Same exclusion as tree_state: ab_test_runtime/ is where
                # runs write, and scanning it here walked 2,305 untracked
                # files under 145 GB - 11s before every ALLOW_DIRTY_TREE START.
                while IFS= read -r -d '' f; do
                    git -C "$REPO" diff --no-index --binary -- /dev/null "$f" 2>/dev/null
                done < <(get_untracked_source_files "$REPO")
            } > "$patch_file"
            write_queue_log "$(stamp) DIRTY_RUN $NAME (ALLOW_DIRTY_TREE=1) patch=${patch_file##*/}"
            echo "gpu_job: uncommitted state saved to $patch_file" >&2
            echo "gpu_job: reproduce with: git checkout $(git -C "${REPO}" rev-parse --short HEAD 2>/dev/null) && git apply $patch_file" >&2
        else
            # Say so rather than proceeding silently: an override whose code
            # state was NOT captured is a different thing from one that was.
            write_queue_log "$(stamp) DIRTY_RUN $NAME (ALLOW_DIRTY_TREE=1) patch=UNSAVED"
            echo "gpu_job: WARNING - could not write a patch to $patch_dir" >&2
        fi
        echo "gpu_job: WARNING - $NAME is running from uncommitted changes." >&2
        echo "gpu_job: its artifact will not be reproducible from any commit." >&2
    else
        write_queue_log "$(stamp) REFUSED  $NAME (uncommitted changes)"
        echo "gpu_job: refusing to run $NAME from a dirty tree." >&2
        echo "gpu_job: uncommitted changes:" >&2
        git -C "${REPO}" diff --stat HEAD >&2 2>/dev/null
        echo "gpu_job: commit them, or re-run with ALLOW_DIRTY_TREE=1 if this" >&2
        echo "gpu_job: is a throwaway whose output nobody will cite." >&2
        exit 5
    fi
    ;;
esac

# OPT-IN LLM PREFLIGHT. Most jobs here are TTS and need no language model, so
# this is off by default and the chains that need one ask for it:
#
#     REQUIRE_LLM=1 ./gpu_job.sh <name> <cmd...>
#
# The PR #308 remeasurement ran on 2026-08-16 with nothing listening on 8090.
# It recorded rc=1 and wrote an artifact with an empty results list, which
# reads as "the experiment failed" rather than "there was no engine", and it
# stayed undiagnosed for a day. The lock cannot catch that - it serialises the
# card and propagates exit codes, it has no idea whether a server exists - so
# the check lives here, next to the other gate, rather than in each chain.
source "$REPO/run_chains/lib/gpu_llm.sh" || exit 4
check_gpu_llm

# VRAM IS NOT COVERED BY THE LOCK, and on 2026-08-17 that cost 14 adapters.
#
# The lock serialises JOBS. llama-server is not a job - ensure_llama_server.sh
# starts it deliberately outside the lock, because "the CALLER never kills the
# server" is what lets consecutive LLM evals share one 8.4 GB load. That fixed
# a real 2026-08-04 ownership bug, and it has no lifecycle end: nothing ever
# reclaims the memory.
#
# So the lock's guarantee - exactly one job at a time - was true and useless.
# One job held the lock while a non-job held 14.77 GiB, and regate_with_provenance
# OOMed on 14 consecutive adapters:
#
#     HIP out of memory. Tried to allocate 2.00 MiB.
#     GPU 0 has a total capacity of 15.92 GiB of which 0 bytes is free.
#
# That knowledge existed in exactly one place beforehand -
# run_chains/moss_vs_lora.sh, "stopping llama-server to free VRAM for the 8B
# model". One chain knew; every other job was on its own. It belongs here,
# where every GPU job already passes.
#
# UNKNOWN IS NOT ZERO. If rocm-smi cannot answer, this warns and continues -
# the same third answer tree_state gives for a non-repo. A missing memory
# provider does not block the card. Detected ROCm memory with an unverified
# selection is different: it must not pass on an unrelated card reading.
source "$REPO/run_chains/lib/gpu_vram.sh" || exit 4
# Reclaiming jobs check the same capacity policy inside their supervised
# command, after cleanup. Ordinary jobs retain their pre-start admission.
if [ "${GPU_RECLAIM_VRAM:-0}" != 1 ]; then
    check_gpu_vram
fi

# TAKE THE WHOLE PROCESS GROUP DOWN, not just the wrapper. Borrowed from
# codex's transient systemd units, which set KillMode=control-group for
# exactly this reason - and the reason is not theoretical: after the
# regate chain was killed on 2026-08-17 a python child survived holding
# 2.11 GiB of the card, which then starved the next job. verify_release.py
# already implements the same idea in Python (stop_process_group); the lock
# wrapper had nothing.
#
# The job runs in its own process group so a signal reaches every descendant,
# and the trap escalates INT -> KILL rather than trusting the first signal.
cleanup_group() {
    local sig="${1:-INT}" started="$SECONDS" elapsed
    # Only the owner may escalate/reap. Killing it would release FD9 over its
    # children; it retains the full20s grace and the lease after wrapper loss.
    trap '' INT TERM HUP
    if [ -n "${JOB_PGID:-}" ]; then
        kill -"$sig" "$JOB_PGID" 2>/dev/null
        while kill -0 "$JOB_PGID" 2>/dev/null; do
            wait "$JOB_PGID" || true
        done
        elapsed=$((SECONDS-started))
        if [ "$elapsed" -ge 20 ]; then
            write_queue_log "$(stamp) KILLED   $NAME (owner reaped after ${elapsed}s on $sig)"
        else
            write_queue_log "$(stamp) STOPPED  $NAME (owner reaped on $sig after ${elapsed}s)"
        fi
    fi
}
trap 'cleanup_group INT; exit 130' INT
trap 'cleanup_group TERM; exit 143' TERM
trap 'cleanup_group HUP; exit 129' HUP

OWNER_PYTHON="${GPU_OWNER_PYTHON:-$(command -v python3)}"
OWNER_SCRIPT="$REPO/app/gpu_queue_owner.py"
if [ ! -x "$OWNER_PYTHON" ] || [ ! -f "$OWNER_SCRIPT" ] || [ ! -f "$REPO/app/subprocess_ownership.py" ]; then
    write_queue_log "$(stamp) FAILED   $NAME rc=4 (GPU owner unavailable)"
    echo "gpu_job: refusing to run $NAME without the GPU command owner and Python3." >&2
    exit 4
fi

write_queue_log "$(stamp) START    $NAME"
STARTED=1

# TELL THE CHILD THE LOCK IS ALREADY HELD. Several chains re-exec themselves
# through this script to acquire the lock (the ALEXANDRIA_GPU_LOCK_HELD idiom).
# Without this export, running such a chain UNDER gpu_job.sh nests one flock
# inside another and deadlocks against its own parent - verified, it hangs
# until killed rather than failing. Exporting the sentinel makes the idiom
# idempotent: the outermost gpu_job.sh holds the lock, and every chain inside
# it runs directly.
export ALEXANDRIA_GPU_LOCK_HELD=1
# Paired with the pid that holds the lock, so a shell that inherits the
# sentinel from a chain cannot keep claiming to be a queued job after that job
# has ended (see app/experiments/gpu_guard.py).
export ALEXANDRIA_GPU_LOCK_PID=$$
export ALEXANDRIA_GPU_LOCK_FD=9

# JOB CONTROL OFF, EXPLICITLY. `setsid` only calls setsid(2) directly when it
# is NOT already a process group leader; if it is, it forks first. With job
# control on (`set -m`) bash puts each background job in its own group, so
# setsid forks, `$!` is the short-lived setsid wrapper, and `wait` returns 0
# THE INSTANT IT EXITS while the real job runs on. gpu_job.sh would then log
# OK, release the flock, and let the next queued job start concurrently with
# one that never finished - the exact overlap this whole script exists to
# prevent.
#
# Measured, and then measured again to find the limit:
#     set -m; setsid sleep 5 & p=$!; wait $p   -> returns 0 in milliseconds
#                                                 with sleep still running
# So the mechanism is real. It is NOT currently reachable here: a script only
# has job control if it runs `set -m` itself, and `bash -m script` cannot
# enable it without a controlling terminal ("no job control in this shell",
# $- = hB). This line is therefore hardening, not a bugfix - it pins an
# assumption that today holds by default. No test guards it, deliberately: a
# test for an unreachable state cannot fail, and one that cannot fail
# advertises coverage that does not exist.
# BACKGROUND WORK SHOULD LOSE TO THE FOREGROUND. Nothing here set priority, so
# a job competed with the desktop as an equal: on 2026-08-18 a book generation
# kept llama-server at 169% CPU while a game ran, and pausing the QUEUE could
# not help because the already-running job was scheduled at parity. Load
# average sat near 9.
#
# nice 15 leaves the job full use of an idle machine and makes it yield the
# moment anything interactive wants the CPU. ionice -c3 does the same for
# disk. Both are best-effort: a missing tool must not stop the job, so each is
# added only if present.
#
# GPU_JOB_NICE=0 disables it, for a run that genuinely should compete.
launcher=(setsid)
if [ "${GPU_JOB_NICE:-15}" != "0" ] && command -v nice >/dev/null 2>&1; then
    launcher+=(nice -n "${GPU_JOB_NICE:-15}")
fi
if command -v ionice >/dev/null 2>&1; then
    launcher+=(ionice -c3)
fi

if [ "${GPU_RECLAIM_VRAM:-0}" = 1 ]; then
    set -- bash "$REPO/run_chains/lib/reclaim_vram.sh" "$SCRIPT_PATH" "$NAME" "$@"
fi

set +m
# The reaper inherits FD9; its actual command closes descriptors. Parent
# death therefore keeps the lease owned until every descendant is reaped.
"${launcher[@]}" "$OWNER_PYTHON" -B "$OWNER_SCRIPT" "$$" "$SCRIPT_PATH" "$NAME" "$@" &
JOB_PGID=$!
wait "$JOB_PGID"
rc=$?

log_result "$rc"
exit "$rc"
