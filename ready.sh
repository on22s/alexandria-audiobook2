#!/usr/bin/bash
# Regenerate everything CI checks, then run the verifier. Use before committing.
#
# WHY THIS EXISTS. Four CI failures in two days, every one of them a generated
# file that was not regenerated before the commit: the structural audit twice,
# the legacy attribution audit, the results index, and the unit test inventory.
# None was a code fault. Each cost a four-minute round trip to learn something
# a two-second local command knew.
#
# The verifier already checks all five. The failure is one of ORDER - verify,
# then edit, then commit - so this does the regeneration FIRST and the checking
# second, in one step, leaving nothing to remember.
#
# It regenerates rather than only checking, deliberately: a check tells you the
# index is stale, and the next thing anyone types is the regenerate command.
#
# It also INSTALLS the git hooks, which is what stopped this being a thing to
# remember at all: merges and rebases now rebuild these files by themselves,
# and this script became the safety net rather than the mechanism.
set -uo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
# HOOKS FIRST. The derived-file hooks and the merge driver they need are LOCAL
# git config, so a fresh checkout or a new worktree does not have them and the
# conflicts come straight back. Installing here is idempotent and means the
# habit that already exists - run ./ready.sh - is the whole setup.
"$REPO/tools/install_git_hooks.sh" >/dev/null 2>&1 || \
    echo "   (could not enable git hooks; run tools/install_git_hooks.sh)" >&2

echo "== regenerating what CI checks =="
# ONE COPY of the list and the commands, in tools/regen_derived.sh. This script,
# resolve_generated.sh and both hooks all call it; four hand-maintained copies
# of "which files are derived" is exactly the drift Rule 15 is about.
"$REPO/tools/regen_derived.sh" || exit 1
python="$("$REPO/tools/regen_derived.sh" --python)"

echo
echo "== is what CI checks actually current? =="
# THE AUTHORITY IS --check, NOT A GIT DIFF. RESULTS_INDEX.md embeds its
# generation time to the minute, so it shows a diff on every regeneration even
# when nothing about the data moved - an earlier version of this script refused
# to proceed on exactly that churn. The --check commands compare the DATA and
# are what CI runs, so they decide.
stale=0
for script in audit_experiment_artifacts audit_legacy_attribution collect_results; do
    if ( cd "$REPO" && "$python" "$REPO/$script.py" --check >/dev/null 2>&1 ); then
        echo "   $script  current"
    else
        echo "   $script  STALE - regeneration did not settle it" >&2
        stale=1
    fi
done
[ "$stale" = 0 ] || exit 2

echo
echo "== anything regenerated must be staged =="
# --check VALIDATES THE WORKING TREE, NOT THE COMMIT. This script regenerates
# first, so --check then passes against files that may never be staged - and
# #352 failed CI on a stale unit_test_inventory.json for exactly that reason,
# the PR that adds this script failing the thing it exists to prevent.
#
# The strict form was dropped earlier because RESULTS_INDEX.md embedded a
# minute-resolution timestamp and therefore always differed. #349 made it
# record the date, so regenerating twice in one day produces no diff and this
# can refuse again.
unstaged=$(git -C "$REPO" diff --name-only -- \
    app/tests/unit_test_inventory.json RESULTS_INDEX.md results_index.csv \
    ab_test_runtime/audit LEGACY_ATTRIBUTION_AUDIT_2026-08-05.md)
if [ -n "$unstaged" ]; then
    printf '%s\n' "$unstaged" | sed 's/^/   /'
    echo
    echo "NOT READY: regeneration changed these and they are not staged."
    echo "   git add $(printf '%s ' $unstaged)"
    exit 2
fi
echo "   nothing unstaged"

echo
echo "== release verifier =="
( cd "$REPO/app" && "$python" verify_release.py "$@" )
