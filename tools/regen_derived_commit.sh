#!/usr/bin/bash
# Rebuild derived files and, if any moved, commit them. Shared by the
# post-merge and post-rewrite hooks; the caller passes its own name for the
# message so a reader can tell which path produced the commit.
#
# GUARDS, each paid for:
#  - Recursion. The commit below runs with --no-verify and sets a marker, so
#    neither pre-commit nor a nested post-rewrite (git commit does not fire
#    one, but --amend does) can re-enter this.
#  - Mid-operation. The test is "is HEAD a branch", NOT "is a pending operation
#    file present". MEASURED 2026-08-20: git still holds MERGE_HEAD while
#    post-merge runs, so a guard that bailed on MERGE_HEAD disabled this hook
#    completely - the merge came out clean, quiet, and with an index missing
#    the artifact it had just merged in. The safety check reintroduced the
#    exact failure it was guarding. During a rebase HEAD is detached, which
#    the symbolic-ref test below catches on its own.
#  - Dirty tree. Only the derived paths are staged and committed, never -a, so
#    unrelated work in progress is untouched.
set -uo pipefail
caller="${1:-hook}"
[ -n "${ALEXANDRIA_REGEN_HOOK:-}" ] && exit 0
export ALEXANDRIA_REGEN_HOOK=1

# DROP THE GIT ENVIRONMENT GIT HANDED US. Hooks inherit GIT_DIR - and, in a
# worktree, GIT_DIR points at .git/worktrees/<name> with no GIT_WORK_TREE
# beside it. In that state `git rev-parse --show-toplevel` FAILS, so this
# script exited at its first line and the hook did nothing at all: the merge
# came out clean and quiet with a stale index, which is the exact failure the
# hook exists to prevent, hidden behind a silent `|| exit 0`. Measured
# 2026-08-20 - it took a file-based probe to see it, because there was no
# output to read. Unset them and let git rediscover from the working
# directory, which is the top of the worktree the merge happened in.
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_PREFIX

REPO="$(git rev-parse --show-toplevel 2>/dev/null)" || exit 0
[ -n "${REPO:-}" ] || exit 0
git -C "$REPO" symbolic-ref -q HEAD >/dev/null || exit 0   # detached: not ours to commit on

# EXACTLY AT UPSTREAM MEANS THERE IS NOTHING TO DO, and this test comes before
# the regeneration so a pull on the live checkout costs nothing at all.
#
# That checkout sits on main at origin/main and runs the GPU queue
# ([[Rule 24]]). Its derived files are whatever main has, which CI already
# validated, so regenerating there can only differ because of artifacts a
# RUNNING job has written and not yet committed - and committing those would
# bake in-flight state into main and leave an unpushed commit behind.
#
# Not hypothetical. Two such commits accumulated there by 2026-08-20 and turned
# the next `git pull --ff-only` into "Not possible to fast-forward", with one of
# them holding the only copy of a chain script that a naive reset would have
# deleted. A hook that recreated that on every pull would be a worse bug than
# the conflicts it was written to remove.
#
# It tests EQUALITY, not merely "has an upstream": a feature branch always has
# one and always needs the rebuild.
upstream=$(git -C "$REPO" rev-parse --quiet --verify '@{upstream}' 2>/dev/null || true)
if [ -n "${upstream:-}" ] && [ "$upstream" = "$(git -C "$REPO" rev-parse HEAD)" ]; then
    exit 0
fi

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
