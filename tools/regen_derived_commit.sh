#!/usr/bin/bash
# Rebuild derived files and, if any moved, commit them. Shared by the
# post-merge and post-rewrite hooks; the caller passes its own name for the
# message so a reader can tell which path produced the commit.
#
# GUARDS, each paid for:
#  - Recursion. The commit below runs with --no-verify and sets a marker, so
#    neither pre-commit nor a nested post-rewrite (git commit does not fire
#    one, but --amend does) can re-enter this.
#  - Mid-operation. During a rebase or a conflicted merge, HEAD is detached or
#    a MERGE_HEAD is pending, and committing here would land on the wrong
#    thing. Bail out; ready.sh still checks before the push.
#  - Dirty tree. Only the derived paths are staged and committed, never -a, so
#    unrelated work in progress is untouched.
set -uo pipefail
caller="${1:-hook}"
[ -n "${ALEXANDRIA_REGEN_HOOK:-}" ] && exit 0
export ALEXANDRIA_REGEN_HOOK=1

REPO="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
[ -n "${REPO:-}" ] || exit 0
GITDIR="$(git -C "$REPO" rev-parse --git-dir)"
for pending in rebase-merge rebase-apply MERGE_HEAD CHERRY_PICK_HEAD; do
    [ -e "$GITDIR/$pending" ] && exit 0
done
git -C "$REPO" symbolic-ref -q HEAD >/dev/null || exit 0   # detached: not ours to commit on

"$REPO/tools/regen_derived.sh" --quiet || {
    echo "$caller: could not regenerate derived files; run ./ready.sh before pushing" >&2
    exit 0; }

paths=$("$REPO/tools/regen_derived.sh" --paths)
# shellcheck disable=SC2086
if git -C "$REPO" diff --quiet -- $paths; then exit 0; fi

# REFUSE TO SWEEP UP ANYTHING ELSE. This commits the index, so anything else
# already staged would ride along. After a clean auto-merge the index matches
# the merge commit, so this is normally empty; if it is not, something else is
# going on and a hook is the wrong place to guess.
other=$(git -C "$REPO" diff --cached --name-only | grep -vFf <(printf '%s\n' $paths) || true)
if [ -n "${other:-}" ]; then
    echo "$caller: derived files are stale but other changes are staged; run ./ready.sh" >&2
    exit 0
fi

# shellcheck disable=SC2086
git -C "$REPO" add -- $paths

# PLUMBING, NOT `git commit`. MEASURED 2026-08-20: git still holds MERGE_HEAD
# while post-merge runs, so `git commit -- <paths>` dies with "cannot do a
# partial commit during a merge", and a pathspec-free `git commit` would see
# MERGE_HEAD and build a SECOND merge commit. commit-tree writes exactly the
# index that was just built, with exactly one parent, and cares about neither.
tree=$(git -C "$REPO" write-tree) || exit 0
[ "$tree" = "$(git -C "$REPO" rev-parse HEAD^{tree})" ] && exit 0
new=$(git -C "$REPO" commit-tree "$tree" -p HEAD -m "Rebuild derived files after $caller" \
    -m "The merge driver keeps our side of every derived file so the merge does
not stop on a conflict that means nothing. Our side is the stale side, so
this rebuilds them against the merged tree. See .gitattributes.") || exit 0
git -C "$REPO" update-ref HEAD "$new" "$(git -C "$REPO" rev-parse HEAD)" \
    && echo "$caller: rebuilt derived files into a follow-up commit"
