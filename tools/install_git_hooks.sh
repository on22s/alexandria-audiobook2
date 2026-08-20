#!/usr/bin/bash
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

# `true` succeeds without writing, which leaves git's %A - our side - in place.
# The hooks rebuild it immediately afterwards; this only stops the conflict.
git -C "$REPO" config merge.ours.driver true
git -C "$REPO" config merge.ours.name "keep ours; derived files are rebuilt by the hooks"
# core.hooksPath lives in the shared config, so every worktree gets these too.
git -C "$REPO" config core.hooksPath .githooks
echo "hooks enabled: core.hooksPath=.githooks, merge.ours.driver=true"
