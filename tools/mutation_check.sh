#!/usr/bin/env bash
# On-demand mutation testing (mutmut) for one app/ module and its test modules.
#
#   tools/mutation_check.sh app/dialogue_spans.py test_dialogue_spans test_pass_quality
#   tools/mutation_check.sh dialogue_spans tests.test_dialogue_spans
#
# WHY A SCRATCH COPY. mutmut writes each mutant into the source file and restores
# it afterwards. The GPU queue runs jobs from the live checkout (CLAUDE.md Rule
# 24), so mutating that tree in place - even for a second - is not acceptable.
# This script copies app/, the repo-root modules (utils.py puts the repo root on
# sys.path for gpu_stats.py and friends) and builtin_lora/ into a scratch tree
# and runs mutmut there. The checkout is only read.
#
# NOT PART OF THE REQUIRED CI PATH. Mutation testing is minutes-to-hours per
# module; .github/workflows/mutation.yml runs it manually (workflow_dispatch).
#
# Python: mutmut must be installed in the interpreter named by
# AA2_MUTATION_PYTHON, or in app/env (local dev), or in python3 on PATH:
#   python -m pip install -r app/requirements-mutation.txt
# See app/requirements-mutation.txt for why mutmut 2.x is pinned.
#
# Environment:
#   AA2_MUTATION_PYTHON    interpreter with mutmut installed (default: app/env
#                          python, else python3)
#   AA2_MUTATION_TMP       parent for the scratch tree (default: $TMPDIR or /tmp)
#   AA2_MUTATION_REPORT_DIR  where to copy report.txt, results.txt and
#                          survivors.diff (default: $AA2_MUTATION_TMP/
#                          aa2-mutation-reports/<module>)
#   AA2_MUTATION_KEEP=1    keep the scratch tree (and .mutmut-cache) for debugging
set -euo pipefail

ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)

die() { echo "mutation_check: $*" >&2; exit 2; }

usage() {
    sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
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
# A bare command name (CI sets AA2_MUTATION_PYTHON=python) must become the
# absolute path mutmut's runner will use; shlex.split does not expand PATH.
if [ -n "${PY:-}" ] && [ "${PY#*/}" = "$PY" ]; then
    PY=$(command -v "$PY" || true)
fi
[ -n "${PY:-}" ] && [ -x "$PY" ] || die "no interpreter; set AA2_MUTATION_PYTHON"
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
    if [ "${AA2_MUTATION_KEEP:-0}" = 1 ]; then
        echo "mutation_check: scratch kept at $WORK" >&2
    else
        rm -rf "$WORK"
    fi
}
trap cleanup EXIT

REPO=$WORK/repo
mkdir -p "$REPO"
# app/ is the working tree (so a local edit under test is what runs), minus
# caches that would only confuse mutmut.
cp -a "$ROOT/app" "$REPO/app"
find "$REPO/app" -name __pycache__ -type d -prune -exec rm -rf {} + 2>/dev/null || true
find "$REPO/app" \( -name '*.pyc' -o -name '.mutmut-cache' -o -name '.pytest_cache' \) -delete 2>/dev/null || true
# Root-level modules (gpu_stats.py, alexandria_file_lock.py, ...) and the one
# small package the app imports from the repo root.
if git -C "$ROOT" rev-parse --git-dir >/dev/null 2>&1; then
    while IFS= read -r -d '' f; do
        case "$f" in */*) continue ;; esac
        [ -f "$ROOT/$f" ] && cp -p "$ROOT/$f" "$REPO/$f"
    done < <(git -C "$ROOT" ls-files -z)
else
    for f in "$ROOT"/*.py; do [ -f "$f" ] && cp -p "$f" "$REPO/"; done
fi
if [ -d "$ROOT/builtin_lora" ]; then
    cp -a "$ROOT/builtin_lora" "$REPO/builtin_lora"
fi

# --- mutmut config. The runner is the same invocation GUIDE.md uses: from app/,
#     `python -m unittest tests.test_x ...`, so tests keep their package import
#     context. setup.cfg is written into the scratch copy only. ---
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
set +e
( cd "$REPO/app" && "$PY" -m mutmut run --simple-output --no-progress ) > "$log" 2>&1
rc=$?
set -e
elapsed=$(( $(date +%s) - start ))

# mutmut 2 exit codes are a bit-OR of 2 (survived), 4 (timeout) and
# 8 (suspicious), so 6 and 10-14 are normal completed runs too; 1 is fatal
# (most often the baseline test run failed).
case "$rc" in
    0|2|4|6|8|10|12|14) ;;
    *) echo "mutation_check: mutmut failed (exit $rc); last 40 log lines:" >&2
       tail -40 "$log" >&2
       mkdir -p "$report_dir"
       cp -a "$WORK/report/." "$report_dir/"
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

mkdir -p "$report_dir"
cp -a "$WORK/report/." "$report_dir/"
echo "report copied to: $report_dir"

# 0 = the run completed (survivors are a finding, not a failure), 1 = fatal.
exit 0
