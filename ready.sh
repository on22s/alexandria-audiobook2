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
set -uo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"
# THE VENV LIVES IN THE MAIN CHECKOUT, and Rule 24 says development happens in
# a worktree - which has no app/env, because it is not tracked. Falling back to
# a bare python3 gets a ModuleNotFoundError for fastapi halfway through the
# suite, which looks like a broken branch and is not. Ask git where the main
# checkout is and borrow its interpreter.
python="$REPO/app/env/bin/python"
if [ ! -x "$python" ]; then
    main_checkout=$(git -C "$REPO" worktree list --porcelain 2>/dev/null \
                    | awk '/^worktree /{print $2; exit}')
    [ -n "${main_checkout:-}" ] && python="$main_checkout/app/env/bin/python"
fi
[ -x "$python" ] || { echo "no interpreter: looked for app/env/bin/python here "\
                           "and in the main checkout" >&2; exit 1; }
echo "interpreter: $python"

echo "== regenerating what CI checks =="
"$python" "$REPO/app/update_test_inventory.py" >/dev/null || exit 1
echo "   unit test inventory"
for script in audit_experiment_artifacts audit_legacy_attribution collect_results; do
    ( cd "$REPO" && "$python" "$REPO/$script.py" >/dev/null 2>&1 ) || {
        echo "   $script FAILED to regenerate" >&2; exit 1; }
    echo "   $script"
done

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
echo "== files to stage (a RESULTS_INDEX.md timestamp-only diff is noise) =="
git -C "$REPO" diff --name-only -- \
    app/tests/unit_test_inventory.json RESULTS_INDEX.md results_index.csv \
    ab_test_runtime/audit LEGACY_ATTRIBUTION_AUDIT_2026-08-05.md \
    | sed 's/^/   /' || true

echo
echo "== release verifier =="
( cd "$REPO/app" && "$python" verify_release.py "$@" )
