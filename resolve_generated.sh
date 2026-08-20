#!/usr/bin/bash
# Resolve a merge whose only conflicts are in generated files, by regenerating.
#
# WHY. Five pull requests in one morning came back CONFLICTING, every one of
# them on the same handful of DERIVED files - RESULTS_INDEX.md,
# results_index.csv, ab_test_runtime/audit/*.json, and GOALS.md's
# "met goals begin at line N" pointer. None was a disagreement about content.
# Any merge into a branch that regenerated an index conflicts with any other
# branch that regenerated it, because both rewrote the same lines.
#
# These files cannot simply be untracked: goal 6.3 requires the committed index
# to describe the committed artifacts. So the fix is not to stop generating
# them, it is to make resolving them a single command instead of a five-command
# dance done from memory at the end of a long session.
#
# MOSTLY OBSOLETE SINCE 2026-08-20. .gitattributes now marks these files
# merge=ours and the post-merge/post-rewrite hooks rebuild them, so an ordinary
# merge no longer conflicts on them at all. This stays for the cases the hooks
# cannot reach: a checkout where tools/install_git_hooks.sh was never run, and
# GOALS.md, which carries real prose and so cannot be given a merge driver.
#
# It REFUSES if anything outside the generated set is conflicted, because those
# are real disagreements and resolving them by regeneration would silently
# discard one side. That happened for real on 2026-08-20: one branch moved goal
# 1.2 to Part II while another added evidence to it in Part I, and taking
# either side alone would have lost work.
set -uo pipefail

# THE REPO YOU ARE STANDING IN, not where this script happens to live. Taking
# it from $0 meant running the script from another checkout silently inspected
# THIS one and reported "nothing is conflicted" while a real conflict sat
# unresolved in front of you - which is worse than not having the script.
REPO="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -n "${REPO:-}" ] || { echo "not inside a git repository" >&2; exit 1; }
GENERATED='^(RESULTS_INDEX\.md|results_index\.csv|LEGACY_ATTRIBUTION_AUDIT_.*\.md|ab_test_runtime/audit/.*\.json|app/tests/unit_test_inventory\.json)$'

conflicted=$(git -C "$REPO" diff --name-only --diff-filter=U)
[ -n "$conflicted" ] || { echo "nothing is conflicted"; exit 0; }

real=$(printf '%s\n' "$conflicted" | grep -vE "$GENERATED" || true)
# GOALS.md is a special case: it conflicts on a DERIVED line number that a test
# recomputes, but it also carries real prose. Only its pointer hunk is safe.
goals_only_pointer=0
if printf '%s\n' "$real" | grep -qx "GOALS.md"; then
    hunks=$(grep -c '^<<<<<<<' "$REPO/GOALS.md" || echo 0)
    pointer=$(grep -A2 '^<<<<<<<' "$REPO/GOALS.md" \
              | grep -c 'met goals begin at line' || echo 0)
    if [ "$hunks" = "1" ] && [ "$pointer" -ge 1 ]; then
        goals_only_pointer=1
        real=$(printf '%s\n' "$real" | grep -vx "GOALS.md" || true)
    fi
fi

if [ -n "$(printf '%s' "$real" | tr -d '[:space:]')" ]; then
    echo "REFUSING: these conflicts are not generated files and need a human:"
    printf '%s\n' "$real" | sed 's/^/   /'
    exit 2
fi

if [ "$goals_only_pointer" = 1 ]; then
    echo "resolving GOALS.md's line pointer (recomputed below)"
    "$("$REPO/tools/regen_derived.sh" --python)" - "$REPO/GOALS.md" <<'PYEOF'
import re, sys
path = sys.argv[1]
text = open(path, encoding="utf-8").read()
text = re.sub(r"<<<<<<< HEAD\n(.*?)=======\n.*?>>>>>>> [^\n]*\n",
              lambda m: m.group(1), text, flags=re.S)
open(path, "w", encoding="utf-8").write(text)
PYEOF
fi

# Only needed now. The refusal above must work in any checkout, including one
# with no venv - otherwise a real conflict exits with "no interpreter found",
# which is the wrong reason.
echo "regenerating"
"$REPO/tools/regen_derived.sh" || exit 1

# shellcheck disable=SC2046
git -C "$REPO" add -- $("$REPO/tools/regen_derived.sh" --paths) 2>/dev/null || true

still=$(git -C "$REPO" diff --name-only --diff-filter=U)
if [ -n "$still" ]; then
    echo "STILL CONFLICTED:" >&2
    printf '%s\n' "$still" | sed 's/^/   /' >&2
    exit 2
fi
echo
echo "resolved. verify with ./ready.sh, then: git commit --no-edit"
