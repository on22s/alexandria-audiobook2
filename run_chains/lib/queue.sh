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
    local wanted="$1" proc pid executable index script path
    local -a argv
    case "$wanted" in ""|*/*) echo "Invalid chain name: $wanted" >&2; return 2;; esac
    if [ ! -d /proc/self ]; then
        echo "Cannot inspect running chains: /proc is unavailable" >&2
        return 2
    fi
    for proc in /proc/[0-9]*; do
        pid=${proc##*/}
        [ "$pid" = "$$" ] || [ "$pid" = "$BASHPID" ] || [ "$pid" = "$PPID" ] && continue
        executable=$(readlink "$proc/exe" 2>/dev/null) || continue
        case "${executable##*/}" in bash|sh|dash|zsh|ksh) ;; *) continue;; esac
        mapfile -d '' -t argv < "$proc/cmdline" 2>/dev/null || continue
        index=1
        while [ "$index" -lt "${#argv[@]}" ]; do
            case "${argv[index]}" in
                --) index=$((index + 1)); break;;
                -c*|--command*|-s) index=${#argv[@]}; break;;
                -o|+o|-O|+O|--rcfile|--init-file) index=$((index + 2));;
                --norc|--noprofile|--posix|--restricted|--verbose|--debugger|--login) index=$((index + 1));;
                -*|+*)
                    # Combined c/s options read commands instead of a script argument.
                    case "${argv[index]}" in *c*|*s*) index=${#argv[@]}; break;; esac
                    index=$((index + 1));;
                *) break;;
            esac
        done
        [ "$index" -lt "${#argv[@]}" ] || continue
        script=${argv[index]}
        case "$script" in /*) path="$script";; *) path="$proc/cwd/$script";; esac
        path=$(readlink -f -- "$path" 2>/dev/null) || continue
        [ "${path##*/}" = "$wanted" ] || continue
        [ "$(basename -- "$(dirname -- "$path")")" = run_chains ] || continue
        return 0
    done
    return 1
}

# wait_for_chain <script-name> [appear-grace-seconds]
wait_for_chain() {
    local name="$1" grace="${2:-0}" waited=0 delay status
    while :; do
        if chain_running "$name"; then
            break
        else
            status=$?
            [ "$status" -eq 1 ] || return "$status"
        fi
        if [ "$waited" -ge "$grace" ]; then
            echo "[$(date -u +%FT%TZ)] $name is not running; continuing"
            return 0
        fi
        [ "$waited" -ne 0 ] || echo "[$(date -u +%FT%TZ)] $name not started yet; allowing ${grace}s for it to appear"
        delay=$((grace - waited))
        [ "$delay" -le 5 ] || delay=5
        sleep "$delay"; waited=$((waited + delay))
    done
    echo "[$(date -u +%FT%TZ)] waiting for $name"
    while :; do
        if chain_running "$name"; then
            sleep 5
        else
            status=$?
            [ "$status" -eq 1 ] || return "$status"
            break
        fi
    done
    echo "[$(date -u +%FT%TZ)] $name finished"
}

# Reuse the GPU wrapper's read-only source policy before launching a chain.
# Its NUL-delimited interface preserves tracked status and odd untracked paths.
refuse_if_dirty() {
    local repo="$1" gate="${BASH_SOURCE[0]%/*}/../../gpu_job.sh" source_fd source_pid
    local -a source_state
    exec {source_fd}< <(bash "$gate" --print-source-state "$repo")
    source_pid=$!
    mapfile -d '' -t source_state <&"$source_fd"
    exec {source_fd}<&-
    if ! wait "$source_pid" || [ "${source_state[0]:-unknown}" = unknown ]; then
        echo "REFUSING: could not inspect the repository's working tree." >&2
        return 1
    fi
    if [ "${source_state[0]:-unknown}" != clean ]; then
        echo "REFUSING: the tree is dirty, so gpu_job.sh would reject every stage:"
        printf '%s\n' "${source_state[@]:1}" | sed '/^$/d; s/^/    /'
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
