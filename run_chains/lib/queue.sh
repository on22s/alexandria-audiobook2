#!/usr/bin/bash
# Wait for other chains, without the two traps this repo has already paid for.
#
# WHY A SHARED HELPER. Six chains were written in one day as one-off waiters
# living outside the repository, each hardcoding a PID because the chain it
# waited for was itself outside `run_chains/` and so could not be matched by
# name. A PID is unrepeatable: the script cannot be re-run tomorrow, and it
# cannot be committed as the record of how a result was produced. Chains that
# live in `run_chains/` can wait by NAME, which is reproducible - so putting
# them here is what removes the need for the PIDs.
#
# TRAP ONE, and it has killed a shell in this repo four times: `pgrep -f`
# matches the command line of whatever is doing the matching. A waiter looking
# for its own name finds itself and waits forever. Excluding $$ and $PPID is
# not optional ([[Rule 22]]).
#
# TRAP TWO: waiting for a chain that is not running yet returns immediately and
# the waiter starts, taking the card out from under work that was about to
# begin. `wait_for_chain` therefore takes an optional grace period: it will
# wait that long for the chain to APPEAR before concluding it is finished.

chain_running() {
    pgrep -f "run_chains/$1" 2>/dev/null \
        | grep -qv -e "^$$\$" -e "^$PPID\$"
}

# wait_for_chain <script-name> [appear-grace-seconds]
wait_for_chain() {
    local name="$1" grace="${2:-0}" waited=0
    if [ "$grace" -gt 0 ] && ! chain_running "$name"; then
        echo "[$(date -u +%FT%TZ)] $name not started yet; allowing ${grace}s for it to appear"
        while [ "$waited" -lt "$grace" ] && ! chain_running "$name"; do
            sleep 10; waited=$((waited + 10))
        done
    fi
    if ! chain_running "$name"; then
        echo "[$(date -u +%FT%TZ)] $name is not running; continuing"
        return 0
    fi
    echo "[$(date -u +%FT%TZ)] waiting for $name"
    while chain_running "$name"; do sleep 120; done
    echo "[$(date -u +%FT%TZ)] $name finished"
}

# REFUSE A DIRTY TREE UP FRONT. gpu_job.sh refuses each job individually, so a
# chain launched from a dirty tree reaches its summary in minutes having run
# nothing - 22 such refusals in one day, all reading `uncommitted changes`
# ([[Rule 24]]). Generated artifacts are excluded, matching gpu_job.sh's own
# list, because a run rewriting its outputs is not a run whose code changed.
refuse_if_dirty() {
    local repo="$1"
    local dirty
    dirty=$(git -C "$repo" status --porcelain \
            -- ':(exclude)ab_test_runtime/experiments/*.json' \
               ':(exclude)ab_test_runtime/audit/*.json' \
               ':(exclude)RESULTS_INDEX.md' ':(exclude)results_index.csv' \
               ':(exclude)LEGACY_ATTRIBUTION_AUDIT_*.md' 2>/dev/null \
            | grep -v '^??' || true)
    if [ -n "${dirty:-}" ]; then
        echo "REFUSING: the tree is dirty, so gpu_job.sh would reject every stage:"
        printf '%s\n' "$dirty" | sed 's/^/    /'
        return 1
    fi
    return 0
}

# THE VENV LIVES IN THE MAIN CHECKOUT. Development happens in a worktree, which
# has no app/env because it is not tracked, so a chain read from one finds no
# interpreter. One copy here rather than one per chain ([[Rule 15]]): a second
# would drift, and this one already had to learn the fallback twice.
resolve_python() {
    local repo="$1" python="$1/app/env/bin/python" main_checkout
    if [ ! -x "$python" ]; then
        main_checkout=$(git -C "$repo" worktree list --porcelain 2>/dev/null \
                        | awk '/^worktree /{print $2; exit}')
        [ -n "${main_checkout:-}" ] && python="$main_checkout/app/env/bin/python"
    fi
    [ -x "$python" ] || return 1
    printf '%s' "$python"
}
