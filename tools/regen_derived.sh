#!/usr/bin/bash
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
ab_test_runtime/audit
app/tests/unit_test_inventory.json
GOALS.md
PATHS
}

if [ "${1:-}" = "--paths" ]; then derived_paths; exit 0; fi

# THE VENV LIVES IN THE MAIN CHECKOUT. Rule 24 puts development in a worktree,
# which has no app/env because it is not tracked, and a bare python3 gets a
# ModuleNotFoundError halfway through - which reads as a broken branch and is
# not one. Ask git where the main checkout is and borrow its interpreter.
python="$REPO/app/env/bin/python"
if [ ! -x "$python" ]; then
    main_checkout=$(git -C "$REPO" worktree list --porcelain 2>/dev/null \
                    | awk '/^worktree /{print $2; exit}')
    [ -n "${main_checkout:-}" ] && python="$main_checkout/app/env/bin/python"
fi
[ -x "$python" ] || {
    echo "regen_derived: no interpreter (looked for app/env/bin/python here and in the main checkout)" >&2
    exit 1; }

# `--python` prints the resolved interpreter and stops. ready.sh needs it for
# the --check pass, and resolving it in a second place is how the worktree
# fallback above would end up existing twice and drifting once.
if [ "${1:-}" = "--python" ]; then echo "$python"; exit 0; fi

quiet=0
[ "${1:-}" = "--quiet" ] && quiet=1
say() { [ "$quiet" = 1 ] || echo "$@"; }

for script in audit_experiment_artifacts audit_legacy_attribution collect_results; do
    ( cd "$REPO" && "$python" "$REPO/$script.py" >/dev/null 2>&1 ) || {
        echo "regen_derived: $script FAILED" >&2; exit 1; }
    say "   $script"
done
( cd "$REPO/app" && "$python" update_test_inventory.py >/dev/null 2>&1 ) || {
    echo "regen_derived: update_test_inventory FAILED" >&2; exit 1; }
say "   unit test inventory"

# GOALS.md carries a derived line number in its header. It is one sed rather
# than a generator, but it is derived, it conflicts for the same reason as the
# indexes, and leaving it out of the one list is how it would be forgotten.
if [ -f "$REPO/GOALS.md" ]; then
    line=$(grep -n '^# Part II' "$REPO/GOALS.md" | cut -d: -f1)
    if [ -n "${line:-}" ]; then
        sed -i "30s/met goals begin at line [0-9]*/met goals begin at line $line/" \
            "$REPO/GOALS.md"
        say "   GOALS.md line pointer"
    fi
fi
