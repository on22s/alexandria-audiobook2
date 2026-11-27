#!/usr/bin/env bash
# Self-test for tools/mutation_check.sh, run entirely in throwaway repositories.
#
# The wrapper mutates a scratch copy and must never write to the checkout, but
# the dangerous paths are all in its setup: inherited Git state (a hook exports
# GIT_DIR, and `git -C scratch init/add` then targets the LIVE repository), an
# untracked-data copy, signal cleanup, and the report/tmp paths. This test
# builds its own small Git repositories under $TMPDIR, copies the wrapper into
# each one, and asserts the invariants directly:
#
#   * GIT_DIR pointing at a linked worktree's gitdir, a main .git, and an
#     unrelated repository leaves every config, index and HEAD byte-identical;
#   * untracked .env, config.json, dataset_temp/ and voices_fake/ (under app/
#     and the repository root) never reach the scratch, while a tracked file's
#     local edit does;
#   * INT, TERM and HUP stop mutmut's whole group and remove the scratch; a
#     wrapper SIGKILL is documented and the printed pgid can still stop it;
#   * a report or tmp path inside the checkout is refused before anything is
#     created;
#   * an interpreter path containing a space still runs (mutmut parses the
#     runner with shlex, so the path must be escaped);
#   * a normal run classifies the mutant and leaves the repository unchanged.
#
# Run it via .github/workflows/mutation.yml, or locally:
#   tools/mutation_check_selftest.sh
# It needs mutmut in AA2_MUTATION_PYTHON / app/env/bin/python / python3 and
# skips (exit 0) when mutmut is absent. POSIX + bash only, like the wrapper.
set -euo pipefail

# The self-test itself may be started from a Git hook (that is the case the
# first test reproduces); clear the same state the wrapper clears so its own
# setup commands target the throwaway repositories they name.
for _git_env in ${!GIT_@}; do
    unset "$_git_env"
done
unset _git_env ARGV0

HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
ROOT=$(cd "$HERE/.." && pwd)
WRAPPER=$HERE/mutation_check.sh

fail() { echo "selftest: FAIL: $*" >&2; exit 1; }
note() { echo "selftest: $*"; }

PY=${AA2_MUTATION_PYTHON:-}
if [ -z "$PY" ]; then
    if [ -x "$ROOT/app/env/bin/python" ]; then
        PY=$ROOT/app/env/bin/python
    else
        PY=$(command -v python3 || true)
    fi
fi
[ -n "$PY" ] || fail "no Python interpreter"
case "$PY" in
    */*) PY="$(cd "$(dirname "$PY")" && pwd)/$(basename "$PY")" ;;
    *) PY=$(command -v "$PY" || true) ;;
esac
if ! "$PY" -c 'import mutmut' >/dev/null 2>&1; then
    echo "selftest: SKIP: mutmut is not installed in '${PY:-python3}'" >&2
    exit 0
fi

T=$(mktemp -d "${TMPDIR:-/tmp}/aa2-mutation-selftest.XXXXXX") || fail "cannot create temp dir"
cleanup() {
    # A wrapper SIGKILL case deliberately leaves mutmut running until the
    # documented `kill -- -<pgid>`; make sure nothing survives the script.
    if [ -n "${LEFTOVER_PGID:-}" ]; then
        kill -KILL -- "-$LEFTOVER_PGID" 2>/dev/null || true
    fi
    rm -rf "$T"
}
trap cleanup EXIT

TMP_PARENT=$T/tmp
REPORT_PARENT=$T/report

hash_tree() {
    "$PY" - "$1" <<'PY'
import hashlib, os, subprocess, sys
root = os.path.abspath(sys.argv[1])
h = hashlib.sha256()
for dirpath, dirnames, filenames in os.walk(root):
    if '.git' in dirnames:
        dirnames.remove('.git')
    dirnames.sort()
    for name in sorted(filenames):
        path = os.path.join(dirpath, name)
        rel = os.path.relpath(path, root)
        h.update(rel.encode() + b'\0')
        if os.path.islink(path):
            h.update(b'link:' + os.readlink(path).encode())
        else:
            with open(path, 'rb') as fh:
                h.update(fh.read())
h.update(subprocess.check_output(
    ['git', '-C', root, 'status', '--porcelain=v1', '-uall', '--ignored']))
index = os.path.join(root, '.git', 'index')
if os.path.exists(index):
    with open(index, 'rb') as fh:
        h.update(fh.read())
print(h.hexdigest())
PY
}

hash_gitdir() {
    "$PY" - "$1" <<'PY'
import hashlib, os, sys
root = os.path.abspath(sys.argv[1])
h = hashlib.sha256()
for dirpath, dirnames, filenames in os.walk(root):
    dirnames.sort()
    for name in sorted(filenames):
        path = os.path.join(dirpath, name)
        rel = os.path.relpath(path, root)
        h.update(rel.encode() + b'\0')
        if os.path.islink(path):
            h.update(b'link:' + os.readlink(path).encode())
        else:
            with open(path, 'rb') as fh:
                h.update(fh.read())
print(h.hexdigest())
PY
}

make_repo() {
    local repo=$1
    mkdir -p "$repo/app/tests" "$repo/tools"
    cp "$WRAPPER" "$repo/tools/mutation_check.sh"
    chmod +x "$repo/tools/mutation_check.sh"
    cat > "$repo/app/tiny.py" <<'PY'
def add(a, b):
    return a + b
PY
    : > "$repo/app/tests/__init__.py"
    cat > "$repo/app/tests/test_tiny.py" <<'PY'
import unittest

from tiny import add


class TinyTests(unittest.TestCase):
    def test_add(self):
        self.assertEqual(5, add(2, 3))
PY
    printf 'original\n' > "$repo/app/tracked.txt"
    git init -q --template= "$repo"
    git -C "$repo" config user.email selftest@example.invalid
    git -C "$repo" config user.name "mutation selftest"
    git -C "$repo" add -A
    git -C "$repo" -c core.hooksPath=/dev/null commit -qm init
}

wrapper_env() {
    # Common environment for a wrapper run; callers may add GIT_DIR etc.
    env AA2_MUTATION_PYTHON="$PY" \
        AA2_MUTATION_TMP="$TMP_PARENT" \
        AA2_MUTATION_REPORT_DIR="$REPORT_PARENT" \
        "$@"
}

run_wrapper() {
    # run_wrapper <repo> <log> [VAR=VALUE...]
    local repo=$1 log=$2
    shift 2
    ( cd "$repo" && wrapper_env "$@" bash "$repo/tools/mutation_check.sh" tiny test_tiny ) > "$log" 2>&1
}

scratch_dirs() {
    find "$TMP_PARENT" -mindepth 1 -maxdepth 1 -type d -name 'aa2-mutation.*' 2>/dev/null || true
}

wait_for_pgid() {
    local log=$1 i
    for i in $(seq 1 200); do
        if grep -q 'mutmut process group' "$log" 2>/dev/null; then
            sed -n 's/.*mutmut process group \([0-9][0-9]*\).*/\1/p' "$log" | tail -1
            return 0
        fi
        sleep 0.05
    done
    return 1
}

wait_gone() {
    local pid=$1 i
    for i in $(seq 1 100); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 0.05
    done
    return 1
}

checks=0

# --- 1. Inherited GIT_DIR must not redirect a single Git call. -----------------
SAFE=$T/safe
MAIN=$T/main
OTHER=$T/other
make_repo "$SAFE"
make_repo "$MAIN"
make_repo "$OTHER"
git -C "$MAIN" worktree add -q "$T/linked"

before_safe=$(hash_tree "$SAFE")
before_main=$(hash_gitdir "$MAIN/.git")
before_linked=$(hash_gitdir "$MAIN/.git/worktrees/linked")
before_other=$(hash_gitdir "$OTHER/.git")

for target in "$MAIN/.git/worktrees/linked" "$MAIN/.git" "$OTHER/.git"; do
    log=$T/git-env.log
    if ! run_wrapper "$SAFE" "$log" "GIT_DIR=$target"; then
        fail "wrapper failed with GIT_DIR=$target: $(tail -5 "$log")"
    fi
    grep -q 'killed 1' "$log" || fail "wrapper did not classify the mutant with GIT_DIR=$target"
    checks=$((checks + 1))
done
[ "$(hash_gitdir "$MAIN/.git")" = "$before_main" ] || fail "GIT_DIR target's .git changed"
[ "$(hash_gitdir "$MAIN/.git/worktrees/linked")" = "$before_linked" ] || \
    fail "linked worktree gitdir changed"
[ "$(hash_gitdir "$OTHER/.git")" = "$before_other" ] || fail "unrelated repository .git changed"
[ "$(hash_tree "$SAFE")" = "$before_safe" ] || fail "wrapper checkout changed"
note "inherited GIT_DIR is ignored; target repos byte-identical"

# --- 2. Only tracked files reach the scratch. --------------------------------
printf 'SECRET=1\n' > "$SAFE/app/.env"
printf '{"secret": true}\n' > "$SAFE/app/config.json"
mkdir -p "$SAFE/app/dataset_temp" "$SAFE/app/voices_fake"
printf 'secret\n' > "$SAFE/app/dataset_temp/secret.txt"
printf 'secret\n' > "$SAFE/app/voices_fake/voice.wav"
printf 'SECRET=1\n' > "$SAFE/.env"
printf '{"secret": true}\n' > "$SAFE/config.json"
mkdir -p "$SAFE/dataset_temp" "$SAFE/voices_fake"
printf 'secret\n' > "$SAFE/dataset_temp/secret.txt"
printf 'secret\n' > "$SAFE/voices_fake/voice.wav"
printf 'edited\n' >> "$SAFE/app/tracked.txt"

log=$T/tracked.log
if ! wrapper_env AA2_MUTATION_KEEP=1 bash "$SAFE/tools/mutation_check.sh" tiny test_tiny > "$log" 2>&1; then
    fail "tracked-copy run failed: $(tail -5 "$log")"
fi
scratch=$(scratch_dirs | head -1)
[ -n "$scratch" ] || fail "AA2_MUTATION_KEEP=1 left no scratch to inspect"
for path in app/.env app/config.json app/dataset_temp app/voices_fake \
            .env config.json dataset_temp voices_fake; do
    [ ! -e "$scratch/repo/$path" ] || fail "untracked $path reached the scratch"
    checks=$((checks + 1))
done
grep -q 'edited' "$scratch/repo/app/tracked.txt" || fail "tracked local edit was not copied"
[ -d "$scratch/repo/.git" ] || fail "scratch has no Git index"
rm -rf "$scratch"
note "untracked data is excluded; tracked working-tree edit is copied"

# --- 3. A fake interpreter for deterministic signal tests. --------------------
FAKE=$T/fake-python
cat > "$FAKE" <<'FAKE_PY'
#!/usr/bin/env bash
# Satisfy the wrapper's import/version probes, then pretend to run mutmut.
case "${1:-}" in
    -c)
        case "${2:-}" in
            *__version__*) echo 2.4.5 ;;
        esac
        exit 0
        ;;
    -m)
        if [ "${2:-}" = mutmut ]; then
            echo "fake mutmut started"
            exec sleep 300
        fi
        ;;
esac
exit 0
FAKE_PY
chmod +x "$FAKE"

SIG_REPO=$T/sig
make_repo "$SIG_REPO"
before_sig=$(hash_tree "$SIG_REPO")

start_sig() {
    local log=$1
    set -m
    # exec so $! is the wrapper itself, not a subshell that would swallow the
    # signal and leave the wrapper running.
    ( cd "$SIG_REPO" && exec env AA2_MUTATION_PYTHON="$FAKE" \
        AA2_MUTATION_TMP="$TMP_PARENT" AA2_MUTATION_REPORT_DIR="$REPORT_PARENT" \
        bash "$SIG_REPO/tools/mutation_check.sh" tiny test_tiny ) > "$log" 2>&1 &
    BG=$!
    set +m
}

for sig in INT TERM HUP; do
    log=$T/sig-$sig.log
    start_sig "$log"
    pgid=$(wait_for_pgid "$log") || { kill -KILL "$BG" 2>/dev/null || true; fail "$sig wrapper did not start"; }
    kill -"$sig" "$BG" 2>/dev/null || true
    if wait "$BG"; then rc=0; else rc=$?; fi
    case "$sig" in INT) want=130 ;; TERM) want=143 ;; HUP) want=129 ;; esac
    [ "$rc" = "$want" ] || fail "SIG$sig exited $rc, expected $want"
    wait_gone "$pgid" || fail "SIG$sig left mutmut running"
    [ -z "$(scratch_dirs)" ] || fail "SIG$sig left a scratch tree"
    checks=$((checks + 1))
done

# SIGKILL of the wrapper cannot clean up: the documented pgid must stop mutmut.
log=$T/sig-KILL.log
start_sig "$log"
pgid=$(wait_for_pgid "$log") || { kill -KILL "$BG" 2>/dev/null || true; fail "KILL wrapper did not start"; }
kill -KILL "$BG"
if wait "$BG"; then rc=0; else rc=$?; fi
[ "$rc" = 137 ] || fail "SIGKILL wrapper exited $rc, expected 137"
kill -0 "$pgid" 2>/dev/null || fail "mutmut should still run after wrapper SIGKILL"
LEFTOVER_PGID=$pgid
kill -TERM -- "-$pgid" 2>/dev/null || true
wait_gone "$pgid" || fail "kill -- -<pgid> did not stop mutmut"
LEFTOVER_PGID=
[ -n "$(scratch_dirs)" ] || fail "SIGKILL should leave a scratch tree for manual cleanup"
rm -rf "$(scratch_dirs)"
checks=$((checks + 1))

# Killing mutmut itself must fail the run cleanly, not hang.
log=$T/kill-mutmut.log
start_sig "$log"
pgid=$(wait_for_pgid "$log") || { kill -KILL "$BG" 2>/dev/null || true; fail "mutmut-kill wrapper did not start"; }
kill -KILL "$pgid"
if wait "$BG"; then rc=0; else rc=$?; fi
[ "$rc" = 1 ] || fail "mutmut-killed wrapper exited $rc, expected 1"
grep -q 'mutmut failed' "$log" || fail "mutmut-killed run did not report the failure"
[ -z "$(scratch_dirs)" ] || fail "mutmut-killed run left a scratch tree"
wait_gone "$pgid" || fail "killed mutmut is still reported alive"
checks=$((checks + 1))

[ "$(hash_tree "$SIG_REPO")" = "$before_sig" ] || fail "signal runs changed the checkout"
note "INT/TERM/HUP/kill/mutmut-kill cleanup verified; checkout unchanged"

# --- 4. Report and tmp paths inside the checkout are refused. ----------------
run_refused() {
    # run_refused <label> <log> <VAR=VALUE>...
    local label=$1 log=$2
    shift 2
    local rc=0
    run_wrapper "$SAFE" "$log" "$@" || rc=$?
    [ "$rc" = 2 ] || fail "$label exited $rc, expected 2"
    grep -q 'outside the checkout' "$log" || fail "$label was not explained"
    checks=$((checks + 1))
}
run_refused "AA2_MUTATION_REPORT_DIR inside the checkout" "$T/refuse-report.log" \
    "AA2_MUTATION_REPORT_DIR=$SAFE/reports"
run_refused "AA2_MUTATION_TMP inside the checkout" "$T/refuse-tmp.log" \
    "AA2_MUTATION_TMP=$SAFE/tmp"
ln -s "$SAFE/reports" "$T/escape"
run_refused "symlinked report dir into the checkout" "$T/refuse-link.log" \
    "AA2_MUTATION_REPORT_DIR=$T/escape"
[ ! -e "$SAFE/reports" ] || fail "refused report path was created"
[ ! -e "$SAFE/tmp" ] || fail "refused tmp path was created"
note "paths inside the checkout are refused before anything is created"

# --- 5. An interpreter path containing a space must survive shlex. -----------
SPACE_DIR="$T/dir with space"
mkdir -p "$SPACE_DIR"
cat > "$SPACE_DIR/python" <<EOF
#!/bin/sh
exec "$PY" "\$@"
EOF
chmod +x "$SPACE_DIR/python"
log=$T/space-python.log
if ! wrapper_env AA2_MUTATION_PYTHON="$SPACE_DIR/python" \
        bash "$SAFE/tools/mutation_check.sh" tiny test_tiny > "$log" 2>&1; then
    fail "interpreter path with a space failed: $(tail -5 "$log")"
fi
grep -q 'killed 1' "$log" || fail "space-path interpreter did not run the tests"
grep -q "python: $SPACE_DIR/python" "$log" || fail "space-path interpreter was not used"
checks=$((checks + 1))
note "interpreter path with a space is quoted for mutmut's runner"

# --- 6. A normal run leaves the checkout unchanged. --------------------------
before_normal=$(hash_tree "$SAFE")
log=$T/normal.log
if ! run_wrapper "$SAFE" "$log"; then
    fail "normal run failed: $(tail -5 "$log")"
fi
grep -q 'killed 1' "$log" || fail "normal run did not classify the mutant"
[ -z "$(scratch_dirs)" ] || fail "normal run left a scratch tree"
[ "$(hash_tree "$SAFE")" = "$before_normal" ] || fail "normal run changed the checkout"
checks=$((checks + 1))
note "normal run completes and leaves the checkout unchanged"

echo "selftest: PASS ($checks checks)"
