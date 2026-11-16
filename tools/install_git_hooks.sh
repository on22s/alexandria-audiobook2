#!/usr/bin/env bash
# Turn on the derived-file hooks and the merge driver they depend on.
#
# Both settings are LOCAL git config: they are not cloned, and .gitattributes
# alone does nothing without them. `merge=ours` in .gitattributes is not a
# built-in - git resolves it through merge.ours.driver, and with that unset it
# is silently ignored and the file conflicts exactly as before. Verified the
# hard way on 2026-08-20.
#
# Idempotent. ready.sh calls it, so the usual way to get these is to run
# ./ready.sh once in a new checkout.
set -uo pipefail
REPO="$(git rev-parse --show-toplevel 2>/dev/null)"
[ -n "${REPO:-}" ] || { echo "not inside a git repository" >&2; exit 1; }

# Unit workers share this repository's config. Installation belongs to the
# readiness parent, never a shard. Independent fixture repositories still run
# the real installer, preserving their installation/merge-hook coverage.
if [ -n "${AA2_LOCAL_UNIT_COMMON_DIR:-}" ]; then
    common=$(git -C "$REPO" rev-parse --path-format=absolute --git-common-dir) || exit 1
    if [ "$common" = "$AA2_LOCAL_UNIT_COMMON_DIR" ]; then
        echo "unit worker: shared hook configuration is read-only"
        exit 0
    fi
fi

ensure_setting() {
    local actual
    actual=$(git -C "$REPO" config --local --get "$1")
    if [ "$actual" != "$2" ]; then
        git -C "$REPO" config --local "$1" "$2" || return 1
    fi
}

# `true` succeeds without writing, which leaves git's %A - our side - in place.
# The hooks rebuild it immediately afterwards; this only stops the conflict.
ensure_setting merge.ours.driver true || exit 1
ensure_setting merge.ours.name "keep ours; derived files are rebuilt by the hooks" || exit 1
# core.hooksPath lives in the shared config, so every worktree gets these too.
ensure_setting core.hooksPath .githooks || exit 1
echo "hooks enabled: core.hooksPath=.githooks, merge.ours.driver=true"
