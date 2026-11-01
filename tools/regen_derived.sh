#!/usr/bin/env bash
# Regenerate every derived file in this repository. THE ONE COPY.
#
# WHY THIS FILE EXISTS AT ALL. The same list - which files are derived, which
# command rebuilds each, and where the interpreter is - was written out
# separately in ready.sh and in resolve_generated.sh, and the git hooks would
# have made a third and fourth copy. Rule 15: two independently maintained
# answers to one question drift, and this particular question is the one that
# has failed CI five times. Everything that regenerates now calls this.
#
# It is deliberately silent about POLICY. It does not stage, commit, check, or
# refuse; it rebuilds and reports what moved. ready.sh decides whether a moved
# file blocks a commit, the hooks decide whether to stage it, and
# resolve_generated.sh decides whether a conflict was safe to resolve this way.
set -uo pipefail

case "$#:${1:-}" in
    0:|1:--quiet|1:--paths|1:--python|1:--metadata) ;;
    *)
        echo "Usage: regen_derived.sh [--quiet|--paths|--python|--metadata]" >&2
        exit 2
        ;;
esac

# DROP THE GIT ENVIRONMENT A HOOK WOULD HAND US, before anything runs git.
# Hooks export GIT_DIR - in a worktree, one with no GIT_WORK_TREE beside it -
# and every git command below inherits it. MEASURED 2026-08-20: in that state
# update_test_inventory.py's `git ls-files` returns nothing, so the inventory
# regenerates EMPTY of the very module just staged, and its own --check then
# cheerfully agrees, printing "Unit test inventory matches discovery" over a
# file CI rejects. A fallback that returns a plausible answer is the dangerous
# kind ([[Rule 21]]); this is that failure with a git environment instead of a
# missing import. Unsetting here covers every caller at once, which is the
# reason this file exists ([[Rule 15]]).
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX

REPO="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -n "${REPO:-}" ] || { echo "not inside a git repository" >&2; exit 1; }

# THE DERIVED SET, and the only place it is written down. `--paths` prints it
# so callers can stage or diff exactly these without keeping their own copy.
derived_paths() {
    cat <<'PATHS'
RESULTS_INDEX.md
results_index.csv
LEGACY_ATTRIBUTION_AUDIT_2026-08-05.md
ab_test_runtime/audit/artifact_structural_audit.json
ab_test_runtime/audit/legacy_attribution_audit.json
ab_test_runtime/audit/goal_evidence_audit.json
app/tests/unit_test_inventory.json
PATHS
}

if [ "${1:-}" = "--paths" ]; then derived_paths; exit 0; fi

# THE VENV LIVES IN THE MAIN CHECKOUT. Rule 24 puts development in a worktree,
# which has no app/env because it is not tracked, and a bare python3 gets a
# ModuleNotFoundError halfway through - which reads as a broken branch and is
# not one. Ask git where the main checkout is and borrow its interpreter.
#
# AA2_PYTHON is the one explicit override: a PATH to an interpreter, never a
# shell command. A machine can legitimately keep a reusable interpreter outside
# app/env (on Windows, the venv's Scripts/python.exe). An override that silently
# fell back would hide a misconfigured path until a regeneration failed halfway
# through ([[Rule 21]]), so a bad value fails here, before anything runs.
if [ -n "${AA2_PYTHON:-}" ]; then
    python="$AA2_PYTHON"
    if [ ! -f "$python" ] || [ ! -x "$python" ]; then
        echo "regen_derived: AA2_PYTHON is not an executable file: $python" >&2
        echo "regen_derived: set it to an interpreter path, not a shell command" >&2
        exit 2
    fi
    case "$python" in
        /*|[A-Za-z]:[\\/]*) ;;
        *) python="$(cd -- "$(dirname -- "$python")" && pwd -P)/$(basename -- "$python")" ;;
    esac
else
    # Preserve POSIX first. Windows venvs keep the interpreter in Scripts/.
    python="$REPO/app/env/bin/python"
    [ -x "$python" ] || python="$REPO/app/env/Scripts/python.exe"
    if [ ! -x "$python" ]; then
        # Preserve every byte of the path: `worktree list --porcelain` prints
        # `worktree <path>`, and this workspace's paths contain spaces. The
        # previous `awk '{print $2}'` cut such a path at the first space and
        # pointed at a checkout that does not exist.
        main_checkout=$(git -C "$REPO" worktree list --porcelain 2>/dev/null)
        case "$main_checkout" in
            worktree\ *)
                main_checkout=${main_checkout#worktree }
                main_checkout=${main_checkout%%$'\n'*}
                python="$main_checkout/app/env/bin/python"
                [ -x "$python" ] || python="$main_checkout/app/env/Scripts/python.exe"
                ;;
        esac
    fi
    [ -x "$python" ] || {
        echo "regen_derived: no interpreter (AA2_PYTHON unset; looked for app/env/bin/python and app/env/Scripts/python.exe here and in the main checkout)" >&2
        exit 1; }
fi

# `--python` prints the resolved interpreter and stops. ready.sh needs it for
# the --check pass, and resolving it in a second place is how the worktree
# fallback above would end up existing twice and drifting once.
if [ "${1:-}" = "--python" ]; then echo "$python"; exit 0; fi
# Combined query for callers that need both shared answers. No generators run.
if [ "${1:-}" = "--metadata" ]; then
    printf '%s\n' "$python"
    derived_paths
    exit 0
fi

quiet=0
[ "${1:-}" = "--quiet" ] && quiet=1
say() { [ "$quiet" = 1 ] || echo "$@"; }

for script in audit_experiment_artifacts audit_legacy_attribution collect_results; do
    ( cd "$REPO" && "$python" "$REPO/tools/audit/$script.py" >/dev/null 2>&1 ) || {
        echo "regen_derived: $script FAILED" >&2; exit 1; }
    say "   $script"
done
( cd "$REPO" && "$python" "$REPO/app/experiments/goal_evidence_audit.py" >/dev/null 2>&1 ) || {
    echo "regen_derived: goal_evidence_audit FAILED" >&2; exit 1; }
say "   goal evidence audit"
( cd "$REPO/app" && "$python" update_test_inventory.py >/dev/null ) || {
    echo "regen_derived: update_test_inventory FAILED" >&2; exit 1; }
say "   unit test inventory"

# Older GOALS.md headers carried this pointer. Keep rebuilding it for the
# pointer-only resolver, but never classify the surrounding prose as derived.
if [ -f "$REPO/GOALS.md" ]; then
    line=$(grep -n '^# Part II' "$REPO/GOALS.md" | cut -d: -f1)
    if [ -n "${line:-}" ]; then
        # -i.bak: GNU and BSD (macOS) sed both accept an attached suffix.
        sed -i.bak "30s/met goals begin at line [0-9]*/met goals begin at line $line/" \
            "$REPO/GOALS.md"
        # Unconditional: a stray backup would leave the tree dirty for gpu_job.sh.
        rm -f "$REPO/GOALS.md.bak"
        say "   GOALS.md line pointer"
    fi
fi
