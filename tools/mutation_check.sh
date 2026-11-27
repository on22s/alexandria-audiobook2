#!/usr/bin/env bash
# On-demand mutation testing (mutmut) for one app/ module and its test modules.
#
#   tools/mutation_check.sh app/dialogue_spans.py test_dialogue_spans test_pass_quality
#   tools/mutation_check.sh dialogue_spans tests.test_dialogue_spans
#
# WHY A SCRATCH COPY. mutmut writes each mutant into the source file and restores
# it afterwards. The GPU queue runs jobs from the live checkout (CLAUDE.md Rule
# 24), so mutating that tree in place - even for a second - is not acceptable.
# This script copies app/, the tracked repo-root modules and packages (utils.py
# puts the repo root on sys.path for gpu_stats.py, tools.audit, ...) and
# builtin_lora/ into a scratch tree and runs mutmut there. The checkout is only
# read; user data under app/uploads and app/logs is never copied.
#
# NOT PART OF THE REQUIRED CI PATH. Mutation testing is minutes-to-hours per
# module; .github/workflows/mutation.yml runs it manually (workflow_dispatch).
#
# SCOPE: POSIX with bash only (Linux, macOS, CI). The wrapper uses process
# groups and bash traps for signal cleanup; Windows is not supported. (mutmut
# 2.4.5 starts its workers with multiprocessing's spawn method, not fork.)
#
# Python: mutmut must be installed in the interpreter named by
# AA2_MUTATION_PYTHON, or in app/env (local dev), or in python3 on PATH:
#   python -m pip install -r app/requirements-mutation.txt
# See app/requirements-mutation.txt for why mutmut 2.x is pinned.
#
# Environment:
#   AA2_MUTATION_PYTHON    interpreter with mutmut installed (default: app/env
#                          python, else python3); bare names and relative paths
#                          are resolved to absolute paths before cd into scratch
#   AA2_MUTATION_TMP       parent for the scratch tree (default: $TMPDIR or /tmp)
#   AA2_MUTATION_REPORT_DIR  where to copy report.txt, results.txt,
#                          survivors.diff and run.log (default:
#                          $AA2_MUTATION_TMP/aa2-mutation-reports/<module>)
#   AA2_MUTATION_KEEP=1    keep the scratch tree (and .mutmut-cache) for debugging
#
# Limitations:
#   - Inside a Git repository, only tracked repo-root files and packages
#     (`git ls-files`) are copied into the scratch tree; untracked root-level
#     .py modules are not copied.
#   - Normal exit, SIGINT and SIGTERM remove the scratch tree automatically, but
#     SIGKILL cannot run traps and will leave $tmp_parent/aa2-mutation.* behind
#     for manual cleanup.
#   - PYTHONHASHSEED is pinned to 0 for repeatable results. In particular,
#     mutant 414 in dialogue_spans._next_plain_mark flips between killed and
#     survived across hash seeds because the test iterates an unordered set.
#   - mutmut 2.4.5 aborts its spawn worker on SIGINT and can then deadlock in
#     results_queue.get(). The wrapper therefore runs mutmut in its own process
#     group and kills that group on INT/TERM rather than forwarding the signal.
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

die() { echo "mutation_check: $*" >&2; exit 2; }

usage() {
    cat >&2 <<'EOF'
Usage: tools/mutation_check.sh <module> <test_module> [test_module...]

On-demand mutation testing (mutmut) for one app/ module and its test modules.

Examples:
  tools/mutation_check.sh app/dialogue_spans.py test_dialogue_spans test_pass_quality
  tools/mutation_check.sh dialogue_spans tests.test_dialogue_spans

Environment:
  AA2_MUTATION_PYTHON      interpreter with mutmut installed (default: app/env
                           python, else python3)
  AA2_MUTATION_TMP         parent for the scratch tree (default: $TMPDIR or /tmp)
  AA2_MUTATION_REPORT_DIR  where to copy report.txt, results.txt, survivors.diff
                           and run.log (default: $AA2_MUTATION_TMP/
                           aa2-mutation-reports/<module>)
  AA2_MUTATION_KEEP=1      keep the scratch tree (and .mutmut-cache) for debugging
EOF
    exit 2
}

[ $# -ge 2 ] || usage

module=$1
shift
test_args=("$@")

# --- Normalise the module argument: app/x.py, app/x, x.py and x all work. ---
module=${module#./}
module=${module#app/}
module=${module%.py}
case "$module" in
    */*|'') die "module must be a single file in app/, got '$module'" ;;
esac
[ -f "$ROOT/app/$module.py" ] || die "no such module: app/$module.py"

# --- Normalise the test arguments: test_x, test_x.py, app/tests/test_x.py,
#     tests.test_x all become tests.test_x. ---
test_modules=()
for t in "${test_args[@]}"; do
    b=${t##*/}
    b=${b%.py}
    b=${b#tests.}
    case "$b" in
        test_*) ;;
        *) die "test module must start with test_: '$t'" ;;
    esac
    [ -f "$ROOT/app/tests/$b.py" ] || die "no such test module: app/tests/$b.py"
    test_modules+=("tests.$b")
done

# --- Interpreter with mutmut. ---
if [ -n "${AA2_MUTATION_PYTHON:-}" ]; then
    PY=$AA2_MUTATION_PYTHON
elif [ -x "$ROOT/app/env/bin/python" ]; then
    PY=$ROOT/app/env/bin/python
else
    PY=
fi
if [ -z "${PY:-}" ]; then
    PY=$(command -v python3 || true)
fi
# A bare command name (CI sets AA2_MUTATION_PYTHON=python) or a relative path
# containing a slash (e.g. AA2_MUTATION_PYTHON=app/env/bin/python) must become
# an absolute path before mutmut cd's into the scratch copy; avoid readlink -f
# so a virtualenv symlink keeps its sys.prefix.
if [ -n "${PY:-}" ] && [ "${PY#*/}" = "$PY" ]; then
    PY=$(command -v "$PY" || true)
elif [ -n "${PY:-}" ] && [ "${PY#/}" = "$PY" ]; then
    py_dir=$(dirname "$PY")
    py_base=$(basename "$PY")
    if [ -d "$py_dir" ]; then
        PY="$(cd "$py_dir" && pwd)/$py_base"
    fi
fi
[ -n "${PY:-}" ] && [ -f "$PY" ] && [ -x "$PY" ] || die "no interpreter; set AA2_MUTATION_PYTHON"
if ! "$PY" -c 'import mutmut' >/dev/null 2>&1; then
    die "mutmut is not installed in $PY; run '$PY -m pip install -r app/requirements-mutation.txt'"
fi
MUTMUT_VERSION=$("$PY" -c 'import mutmut; print(getattr(mutmut, "__version__", "unknown"))' 2>/dev/null || echo unknown)

# --- Scratch tree. Nothing below ever writes to $ROOT. ---
tmp_parent=${AA2_MUTATION_TMP:-${TMPDIR:-/tmp}}
mkdir -p "$tmp_parent"
WORK=$(mktemp -d "$tmp_parent/aa2-mutation.XXXXXX") || die "cannot create scratch dir"
if [ -n "${AA2_MUTATION_REPORT_DIR:-}" ]; then
    report_dir=$AA2_MUTATION_REPORT_DIR
else
    report_dir=$tmp_parent/aa2-mutation-reports/$module
fi
cleanup() {
    [ "${cleaned:-0}" = 1 ] && return 0
    cleaned=1
    if [ "${AA2_MUTATION_KEEP:-0}" = 1 ]; then
        echo "mutation_check: scratch kept at $WORK" >&2
    else
        rm -rf "$WORK"
    fi
}
# Note: SIGKILL cannot run traps and will leave $WORK behind for manual removal.
trap cleanup EXIT
# mutmut 2.4.5 runs its workers with multiprocessing spawn. On SIGINT it aborts
# a worker and can then block forever in results_queue.get(). Signals are
# therefore handled here: the mutmut job runs in its own process group, and
# stop_child kills that whole group so the wrapper always exits and cleanup runs.
child=
stop_child() {
    sig=$1
    if [ -n "${child:-}" ] && kill -0 "$child" 2>/dev/null; then
        kill -TERM -- "-$child" 2>/dev/null || kill -TERM "$child" 2>/dev/null || true
        for _ in 1 2 3 4 5 6 7 8 9 10; do
            kill -0 "$child" 2>/dev/null || break
            sleep 0.2
        done
        kill -KILL -- "-$child" 2>/dev/null || kill -KILL "$child" 2>/dev/null || true
        wait "$child" 2>/dev/null || true
    fi
    child=
    cleanup
    exit $((128 + sig))
}
trap 'stop_child 2' INT
trap 'stop_child 15' TERM

publish_report() {
    mkdir -p "$report_dir"
    # Remove stale files from any earlier run so a failed run cannot leave
    # previous results.txt or survivors.diff beside a new run.log.
    rm -f "$report_dir/report.txt" "$report_dir/results.txt" \
          "$report_dir/survivors.diff" "$report_dir/run.log"
    cp -a "$WORK/report/." "$report_dir/"
}

REPO=$WORK/repo
mkdir -p "$REPO/app"
# app/ is the working tree (so a local edit under test is what runs), minus
# virtualenvs (app/env is ~15 GB in a live Pinokio checkout; the runner invokes
# the absolute $PY directly) and caches that would only confuse mutmut.
while IFS= read -r -d '' entry; do
    name=${entry##*/}
    case "$name" in
        env|venv|.venv|__pycache__|.mutmut-cache|.pytest_cache) continue ;;
        # User/runtime data, never part of a test run (Rule 27): uploads,
        # application logs and generated audio output.
        uploads|logs|output_audio|output_audio_cloned) continue ;;
    esac
    if [ -d "$entry" ] && { [ -f "$entry/pyvenv.cfg" ] || [ -d "$entry/conda-meta" ]; }; then
        continue
    fi
    cp -a "$entry" "$REPO/app/$name"
done < <(find "$ROOT/app" -mindepth 1 -maxdepth 1 -print0)
find "$REPO/app" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$REPO/app" \( -name '*.pyc' -o -name '.mutmut-cache' -o -name '.pytest_cache' \) -delete 2>/dev/null || true
# Root-level modules and packages the app/tests import from the repo root:
# gpu_stats.py and alexandria_file_lock.py (bare root modules), and
# `from tools.audit...` / `from tools.voice_lab...` in the tests. Only TRACKED
# paths are copied, so a local untracked root module is not silently picked up.
# Large data/asset trees and app/ (copied from the working tree above) are
# skipped; tools/listening_lab is 6.8 MB of JavaScript assets with no Python
# imports from the test suite.
if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    while IFS= read -r -d '' f; do
        case "$f" in
            app/*|ab_test_runtime/*|docs/*|codebase-analysis-docs/*| \
            run_chains/*|fork-adoption-evidence/*|reports/*| \
            .github/*|.githooks/*|.claude/*|tools/listening_lab/*) continue ;;
        esac
        dir=$(dirname "$f")
        [ "$dir" = . ] || mkdir -p "$REPO/$dir"
        cp -p "$ROOT/$f" "$REPO/$f"
    done < <(git -C "$ROOT" ls-files -z)
else
    for f in "$ROOT"/*.py; do [ -f "$f" ] && cp -p "$f" "$REPO/"; done
    [ -d "$ROOT/tools" ] && cp -a "$ROOT/tools" "$REPO/tools"
    [ -d "$ROOT/builtin_lora" ] && cp -a "$ROOT/builtin_lora" "$REPO/builtin_lora"
fi

# Small tracked data files that the selected test modules name literally (for
# example test_attribution_phonetic_identity reads one closed_set JSON under
# ab_test_runtime/experiments). The full ab_test_runtime tree is ~1 GB of
# tracked run artifacts, so only files the tests actually name are copied.
for t in "${test_modules[@]}"; do
    test_file=$ROOT/app/tests/${t#tests.}.py
    [ -f "$test_file" ] || continue
    while IFS= read -r ref; do
        [ -f "$ROOT/$ref" ] || continue
        git -C "$ROOT" ls-files --error-unmatch -- "$ref" >/dev/null 2>&1 || continue
        dir=$(dirname "$ref")
        [ "$dir" = . ] || mkdir -p "$REPO/$dir"
        cp -p "$ROOT/$ref" "$REPO/$ref"
    done < <(grep -oE 'ab_test_runtime/[A-Za-z0-9_./-]+' "$test_file" | sort -u)
done

# The audit helpers confirm gold fixtures against the Git index (tracked link
# targets), so a scratch copy with no metadata fails read_gold_fixture. Give
# the scratch its own index. -f overrides .gitignore, which is also copied.
if command -v git >/dev/null 2>&1; then
    git -C "$REPO" init -q
    git -C "$REPO" add -A -f
fi

# --- mutmut config. The runner is the same invocation GUIDE.md uses: from app/,
#     `python -m unittest tests.test_x ...`, so tests keep their package import
#     context. setup.cfg is written into the scratch copy only. mutmut 2 reads
#     pyproject.toml [tool.mutmut] before setup.cfg, so paths_to_mutate,
#     tests_dir and runner are also passed on the CLI, which overrides those
#     three keys in any future app/pyproject.toml [tool.mutmut] section. Other
#     keys in such a section (e.g. dict_synonyms) would still apply. ---
runner="$PY -m unittest ${test_modules[*]}"
cat > "$REPO/app/setup.cfg" <<EOF
[mutmut]
paths_to_mutate = $module.py
tests_dir = tests
runner = $runner
EOF

export PYTHONPATH="$REPO/app:$REPO"
unset ARGV0 PYTHONHOME PYTHONSTARTUP LD_LIBRARY_PATH APPDIR APPIMAGE GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE
export ALEXANDRIA_GPU_LOCK_HELD=0 CUDA_VISIBLE_DEVICES=-1 HIP_VISIBLE_DEVICES=-1 ROCR_VISIBLE_DEVICES=-1
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 OMP_NUM_THREADS=2 OPENBLAS_NUM_THREADS=1 MKL_NUM_THREADS=1
# Pin the hash seed so repeated runs classify the same mutants the same way.
# dialogue_spans mutant 414 iterates _CLOSERS_BY_OPENER["„"], an unordered set,
# and flips between killed and survived across seeds.
export PYTHONHASHSEED=0

mkdir -p "$WORK/report"
report=$WORK/report/report.txt
log=$WORK/report/run.log
diff_file=$WORK/report/survivors.diff

{
    echo "module: app/$module.py"
    echo "tests:  ${test_modules[*]}"
    echo "python: $PY"
    echo "mutmut: $MUTMUT_VERSION"
    echo "runner: $runner"
    echo "scratch: $REPO"
    echo
} > "$report"

echo "mutation_check: app/$module.py with ${test_modules[*]}" >&2
echo "mutation_check: scratch copy at $REPO (checkout is not modified)" >&2

start=$(date +%s)
# Job control puts mutmut in its own process group (PGID == $child), so
# stop_child can terminate the spawn workers too. set +m immediately restores
# the non-interactive default.
set -m
set +e
( cd "$REPO/app" && exec "$PY" -m mutmut run \
    --paths-to-mutate "$module.py" \
    --tests-dir tests \
    --runner "$runner" \
    --simple-output --no-progress ) > "$log" 2>&1 &
child=$!
set +m
wait "$child"
rc=$?
child=
set -e
elapsed=$(( $(date +%s) - start ))

# mutmut 2 exit codes are a bit-OR of 2 (survived), 4 (timeout) and
# 8 (suspicious), so 6 and 10-14 are normal completed runs too; 1 is fatal
# (most often the baseline test run failed).
case "$rc" in
    0|2|4|6|8|10|12|14) ;;
    *) echo "mutation_check: mutmut failed (exit $rc); last 40 log lines:" >&2
       tail -40 "$log" >&2
       publish_report
       echo "mutation_check: full log: $report_dir/run.log" >&2
       exit 1 ;;
esac

# --- Results. Counts and survivor line numbers come from mutmut's cache; the
#     full diffs come from `mutmut show <id>`. ---
counts=$("$PY" - "$REPO/app/.mutmut-cache" <<'PY'
import sqlite3, sys
db = sqlite3.connect(sys.argv[1])
rows = db.execute('select status, count(*) from Mutant group by status').fetchall()
total = sum(n for _, n in rows)
labels = {
    'ok_killed': 'killed',
    'bad_survived': 'survived',
    'bad_timeout': 'timeout',
    'ok_suspicious': 'suspicious',
    'skipped': 'skipped',
}
counts = {labels.get(k, k): n for k, n in rows}
survivors = [r[0] for r in db.execute(
    "select id from Mutant where status in ('bad_survived','bad_timeout') order by id")]
print('total', total)
for k in ('killed', 'survived', 'timeout', 'suspicious', 'skipped'):
    print(k, counts.get(k, 0))
print('survivor_ids', ','.join(map(str, survivors)))
PY
)
survivor_ids=$(echo "$counts" | awk '$1=="survivor_ids"{print $2}')
{
    echo "elapsed: ${elapsed}s"
    echo "$counts" | grep -v '^survivor_ids' | sed 's/^/  /'
    echo
} >> "$report"

{
    echo "=== survivors (mutmut show) ==="
    if [ -n "$survivor_ids" ]; then
        for id in ${survivor_ids//,/ }; do
            echo
            echo "--- mutant $id ---"
            ( cd "$REPO/app" && "$PY" -m mutmut show "$id" ) 2>&1
        done
    else
        echo "none"
    fi
} > "$diff_file"

( cd "$REPO/app" && "$PY" -m mutmut results ) > "$WORK/report/results.txt" 2>&1 || true

# Print the summary and the first diffs; the full set is in survivors.diff.
cat "$report"
if [ -n "$survivor_ids" ]; then
    echo "survivors: $survivor_ids"
    echo
    echo "first surviving mutants:"
    head -120 "$diff_file"
    echo
else
    echo "no surviving mutants"
fi

publish_report
echo "report copied to: $report_dir"

# 0 = the run completed (survivors are a finding, not a failure), 1 = fatal.
exit 0
