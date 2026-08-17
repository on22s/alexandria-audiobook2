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
#   - a non-zero exit is recorded with a FAILED marker and propagated, instead
#     of being swallowed by the next command in a chain.
#
# It deliberately does NOT manage servers, retry, or interpret results. Those
# belong to the job.
set -uo pipefail

LOCK="${GPU_LOCK:-$HOME/.gpu.lock}"
QLOG="${GPU_QLOG:-$HOME/gpu_jobq.log}"

NAME="${1:-}"
[ -z "$NAME" ] && { echo "usage: gpu_job.sh <name> <command...>" >&2; exit 2; }
shift
[ "$#" -eq 0 ] && { echo "gpu_job.sh: no command given" >&2; exit 2; }

stamp() { date -u +%FT%TZ; }

echo "$(stamp) QUEUED   $NAME" >> "$QLOG"
exec 9>"$LOCK" || {
    echo "$(stamp) LOCK_FAILED $NAME (cannot open $LOCK)" >> "$QLOG"
    echo "gpu_job: cannot open lock file $LOCK" >&2
    exit 4
}
# The lock MUST be a gate, not a suggestion. `set -e` is deliberately not on
# here (the wrapped command's exit code has to survive), so an unchecked
# `flock` would fall through to running the command on failure - defeating the
# entire purpose of this script. A concurrent GPU job is exactly what cost 42
# minutes of training on 2026-08-04.
if ! flock 9; then
    echo "$(stamp) LOCK_FAILED $NAME (flock failed)" >> "$QLOG"
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
    # "Cannot tell" is a THIRD answer and must not be spelled "dirty". Without
    # this check an exported tree, a container without git, or any non-repo
    # directory takes the failure branch below and gets reported - and, once
    # the gate existed, refused - as though it had uncommitted changes.
    if ! git -C "$(dirname "$0")" rev-parse --git-dir >/dev/null 2>&1; then
        echo unknown
        return
    fi
    local root modified untracked
    root=$(dirname "$0")
    modified=$(git -C "$root" status --porcelain --untracked-files=no 2>/dev/null)
    # AN UNTRACKED HARNESS IS THE CASE THIS MISSED. `git diff HEAD` sees
    # modified TRACKED files only, so a brand-new experiment script - which is
    # untracked for exactly as long as it takes to write and run it - was
    # invisible. trim_silence_build.py produced goal 5.4's alignment result
    # that way: reported as a headline while existing on one machine.
    #
    # Untracked .md and scratch files are deliberately NOT dirt. Counting them
    # made an earlier version of this flag true on every run, which is the same
    # as being false. This mirrors `app/experiments/manifest.py::_git_state`
    # exactly, and `test_the_shell_gate_agrees_with_the_python_provenance`
    # fails if the two ever disagree.
    untracked=$(git -C "$root" ls-files --others --exclude-standard \
                    -- "$root/app/experiments" 2>/dev/null | grep -c '\.py$')
    if [ -z "$modified" ] && [ "${untracked:-0}" -eq 0 ]; then
        echo clean
        return
    fi
    # A hash of the uncommitted state, so two runs from the same commit but
    # different working trees are distinguishable.
    local hash
    hash=$(printf '%s\n%s\n' "$modified" "$untracked" \
           | sha256sum 2>/dev/null | cut -c1-12)
    echo "dirty:${hash:-unknown}"
}
dirty_state=$(tree_state)

identity() {
    local commit script_sha gpu
    commit=$(git -C "$(dirname "$0")" rev-parse --short HEAD 2>/dev/null) \
        || commit=unknown
    local dirty="$dirty_state"
    script_sha=$(sha256sum "$0" 2>/dev/null | cut -c1-12)
    gpu=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)
    [ -z "$gpu" ] && gpu=$(rocm-smi --showproductname 2>/dev/null \
        | grep -oPm1 '(?<=Card Series:).*' | xargs) 
    echo "$(stamp) IDENT    $NAME commit=$commit tree=$dirty" \
         "gpu_job_sha=${script_sha:-unknown} host=$(hostname)" \
         "gpu=${gpu:-unknown} cmd=$*"
}
identity "$@" >> "$QLOG"

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
        echo "$(stamp) DIRTY_RUN $NAME (ALLOW_DIRTY_TREE=1)" >> "$QLOG"
        echo "gpu_job: WARNING - $NAME is running from uncommitted changes." >&2
        echo "gpu_job: its artifact will not be reproducible from any commit." >&2
    else
        echo "$(stamp) REFUSED  $NAME (uncommitted changes)" >> "$QLOG"
        echo "gpu_job: refusing to run $NAME from a dirty tree." >&2
        echo "gpu_job: uncommitted changes:" >&2
        git -C "$(dirname "$0")" diff --stat HEAD >&2 2>/dev/null
        echo "gpu_job: commit them, or re-run with ALLOW_DIRTY_TREE=1 if this" >&2
        echo "gpu_job: is a throwaway whose output nobody will cite." >&2
        exit 5
    fi
    ;;
esac

echo "$(stamp) START    $NAME" >> "$QLOG"

"$@"
rc=$?

if [ "$rc" -eq 0 ]; then
    echo "$(stamp) OK       $NAME" >> "$QLOG"
else
    # Loud on purpose. A chained job that fails quietly gets read as a result.
    echo "$(stamp) FAILED   $NAME rc=$rc" >> "$QLOG"
    echo "gpu_job: $NAME FAILED rc=$rc" >&2
fi
exit "$rc"
