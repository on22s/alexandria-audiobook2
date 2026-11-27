"""Run Bandit over non-excluded app modules and repo-root scripts, then gate.

Bandit's INI ``targets`` option takes comma-separated paths, not globs, so the
target set is derived here from ``source_sweep.app_python_files`` (which skips
any virtualenv inside ``app/``) plus every ``*.py`` in the repository root,
pre-filtered by the exclusions in ``app/bandit.ini``. Pre-filtering keeps the
argv well below Windows' 32,767-character ``CreateProcess`` limit while
preserving exact exclusion semantics, and batching splits the run if the target
set ever grows past ``MAX_COMMAND_LINE_CHARS``. New and renamed modules are
scanned automatically, and no baseline file is used.

The JSON report is the gate, not bandit's exit code: ``--exit-zero`` keeps
bandit from failing on findings (so the report is always written), and this
script exits 1 when the report contains a medium-or-higher finding, any file
bandit could not parse or read, any blanket ``nosec`` suppression
(``metrics._totals["nosec"] != 0``), any target missing from the report, or any
``nosec`` comment that does not name exactly one known Bandit test id. LOW
findings are counted in the report metrics only and do not gate.

Exit codes: 0 clean, 1 findings/errors/invalid nosec, 2 bandit did not run.
"""

import configparser
import fnmatch
import json
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import tokenize

from source_sweep import app_python_files

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
INI_PATH = APP_DIR / "bandit.ini"
REPORT_PATH = APP_DIR / "bandit-report.json"
SEVERITY_LEVEL = "medium"

# Windows CreateProcess caps a command line at 32,767 UTF-16 characters.
# Keeping every Bandit invocation under 24,000 leaves wide headroom for long
# Python executable and report paths.
WINDOWS_CREATEPROCESS_LIMIT = 32767
MAX_COMMAND_LINE_CHARS = 24000

# Bandit parses every token after "nosec", and any token that names a test id
# or plugin suppresses that test too; an unknown test id produces an empty id
# set, which Bandit treats as a BLANKET suppression hiding every finding on the
# line. The convention is an id-only nosec comment with one known test id and
# the reason on the line above.
#
# The prefix mirrors bandit.core.manager.NOSEC_COMMENT
# (r"#\s*nosec:?\s*(?P<tests>[^#]+)?#?"): there is NO word boundary after
# "nosec", so a comment spelling it with no separator (nosecB314) is a per-id
# suppression, and the last id in the comment wins (nosecB314B602 suppresses
# only B602). A \b here previously let those spellings skip both the id check
# and the reason rule.
NOSEC_LINE = re.compile(r"#\s*nosec[^#\n]*")
# Bandit's own regex is case-sensitive, so an upper-case spelling suppresses
# nothing. It is still rejected as a likely typo rather than silently ignored.
NOSEC_ANY_CASE = re.compile(r"#\s*nosec[^#\n]*", re.IGNORECASE)
VALID_NOSEC_LINE = re.compile(r"^# nosec (?P<id>B\d+)\s*$")

# Pinned to bandit==1.8.6 (app/requirements-security.txt) as fallback when
# Bandit is not installed (e.g. the Bandit-free unit test job). Whenever Bandit
# is installed, get_known_test_ids() queries Bandit's own extension registry.
KNOWN_BANDIT_IDS = frozenset({
    "B001",
    "B101", "B102", "B103", "B104", "B105", "B106", "B107", "B108", "B110",
    "B112", "B113", "B201", "B202", "B301", "B302", "B303", "B304", "B305",
    "B306", "B307", "B308", "B310", "B311", "B312", "B313", "B314", "B315",
    "B316", "B317", "B318", "B319", "B321", "B323", "B324", "B401", "B402",
    "B403", "B404", "B405", "B406", "B407", "B408", "B409", "B411", "B412",
    "B413", "B415", "B501", "B502", "B503", "B504", "B505", "B506", "B507",
    "B508", "B509", "B601", "B602", "B603", "B604", "B605", "B606", "B607",
    "B608", "B609", "B610", "B611", "B612", "B613", "B614", "B615", "B701",
    "B702", "B703", "B704",
})


def get_known_test_ids(require_bandit=False):
    """Return every known Bandit test id from Bandit's own registry.

    The registry is Bandit's plugin, blacklist, and builtin ids (``B001`` is
    accepted by ``Manager.check_id`` but suppresses no test result). Falls back
    to ``KNOWN_BANDIT_IDS`` only when Bandit is not installed and
    ``require_bandit`` is False (for Bandit-free unit tests).
    """
    try:
        from bandit.core import extension_loader
    except ImportError:
        if require_bandit:
            raise
        return KNOWN_BANDIT_IDS
    manager = extension_loader.MANAGER
    return (frozenset(manager.plugins_by_id)
            | frozenset(manager.blacklist_by_id)
            | frozenset(manager.builtin))


def get_exclude_patterns(ini_path=INI_PATH):
    """Read the comma-separated ``[bandit] exclude`` patterns from the INI.

    A missing section or option is a hard error, not an empty exclusion list:
    silently scanning the tests and experiments would make the gate slower
    without anyone noticing the edit.
    """
    parser = configparser.ConfigParser(strict=True)
    with Path(ini_path).open("r", encoding="utf-8") as handle:
        parser.read_file(handle)
    raw = parser.get("bandit", "exclude")
    return [pattern.strip() for pattern in raw.split(",") if pattern.strip()]


def is_excluded(path, patterns):
    """Mirror Bandit's path exclusion: glob match or literal substring."""
    normalised = str(path).replace("\\", "/")
    return (any(fnmatch.fnmatch(normalised, pattern) for pattern in patterns)
            or any(pattern in normalised for pattern in patterns))


def collect_targets(root=ROOT, ini_path=INI_PATH):
    """Return ``(included, excluded)`` relative POSIX paths for Bandit.

    Uses ``source_sweep.app_python_files`` to skip virtualenvs, then splits the
    discovered files by ``app/bandit.ini``'s ``exclude`` patterns so excluded
    trees (tests, experiments, harnesses) never bloat the argv.
    """
    root = Path(root)
    patterns = get_exclude_patterns(ini_path)
    files = [*app_python_files(root / "app"), *root.glob("*.py")]
    all_paths = sorted(path.relative_to(root).as_posix() for path in files)
    included = []
    excluded = []
    for path in all_paths:
        if is_excluded(path, patterns):
            excluded.append(path)
        else:
            included.append(path)
    return included, excluded


def get_targets(root=ROOT, ini_path=INI_PATH):
    """Non-excluded app modules plus repo-root scripts, as relative POSIX paths."""
    included, _ = collect_targets(root=root, ini_path=ini_path)
    return included


def _relative_report_path(name):
    """Bandit prefixes root scripts with './' and may use backslashes."""
    path = str(name).replace("\\", "/")
    while path.startswith("./"):
        path = path[2:]
    return path


def get_scanned_counts(report):
    """(total, app files, root scripts) from the report's per-file metrics."""
    scanned = [_relative_report_path(name)
               for name in (report.get("metrics") or {}) if name != "_totals"]
    app_files = sum(1 for name in scanned if name.startswith("app/"))
    return len(scanned), app_files, len(scanned) - app_files


def get_nosec_problems(targets, root=ROOT, valid_ids=None):
    """Reject any nosec comment that is not exactly ``# nosec <known_id>``."""
    if valid_ids is None:
        valid_ids = get_known_test_ids()
    problems = []
    root = Path(root)
    for target in targets:
        try:
            with tokenize.open(root / target) as handle:
                comments = [token for token in
                            tokenize.generate_tokens(handle.readline)
                            if token.type == tokenize.COMMENT]
        except (OSError, UnicodeDecodeError, LookupError, SyntaxError,
                tokenize.TokenError):
            # Unreadable/unparsable files are Bandit's errors array, not ours.
            continue
        for token in comments:
            comment = token.string
            if not NOSEC_ANY_CASE.search(comment):
                continue
            match = VALID_NOSEC_LINE.match(comment)
            if not match:
                problems.append(
                    "{target}:{line} invalid nosec comment; use '# nosec BXXX' "
                    "with the reason on the line above: {comment}".format(
                        target=target, line=token.start[0], comment=comment))
                continue
            test_id = match.group("id")
            if test_id not in valid_ids:
                problems.append(
                    "{target}:{line} unknown bandit test id '{test_id}' in "
                    "nosec comment (Bandit treats unknown ids as a blanket "
                    "nosec): {comment}".format(
                        target=target, line=token.start[0],
                        test_id=test_id, comment=comment))
    return problems


def get_problems(report, expected_targets=None):
    """One message per scan error, finding, blanket nosec, or dropped target."""
    problems = []
    errors = report.get("errors") or []
    for error in errors:
        problems.append(
            "could not scan {filename}: {reason}".format(
                filename=_relative_report_path(error.get("filename") or ""),
                reason=error.get("reason")))
    for result in report.get("results") or []:
        text = " ".join(str(result.get("issue_text") or "").split())
        problems.append(
            "{filename}:{line_number} {test_id} {severity}: {text}".format(
                filename=_relative_report_path(result.get("filename") or ""),
                line_number=result.get("line_number"),
                test_id=result.get("test_id"),
                severity=result.get("issue_severity"),
                text=text))
    metrics = report.get("metrics") or {}
    totals = metrics.get("_totals") or {}
    blanket_nosec = totals.get("nosec", 0) or 0
    if blanket_nosec:
        problems.append(
            "bandit reported %d blanket nosec suppression(s) "
            "(metrics._totals.nosec=%d); every nosec comment must name a "
            "known test id" % (blanket_nosec, blanket_nosec))
    if expected_targets is not None:
        if not expected_targets:
            problems.append("bandit target list is empty")
        else:
            scanned = {_relative_report_path(name)
                       for name in metrics if name != "_totals"}
            errored = {_relative_report_path(error.get("filename") or "")
                       for error in errors}
            if not scanned and not errored:
                problems.append("bandit scanned 0 files")
            else:
                for missing in sorted(set(expected_targets) - scanned - errored):
                    problems.append(
                        "target not reported by bandit: %s" % missing)
    return problems


def get_command(targets=None, report_path=REPORT_PATH, ini_path=INI_PATH):
    """The exact Bandit argv the gate runs for one batch of file targets."""
    if targets is None:
        targets = get_targets(ini_path=ini_path)
    return [
        sys.executable, "-m", "bandit",
        "--ini", str(ini_path),
        "-f", "json",
        "-o", str(report_path),
        "--severity-level", SEVERITY_LEVEL,
        "--exit-zero",
        *targets,
    ]


def batch_targets(targets, max_chars=MAX_COMMAND_LINE_CHARS,
                  report_path=REPORT_PATH, ini_path=INI_PATH):
    """Split targets into batches whose Windows command line fits ``max_chars``."""
    batches = []
    current = []
    for target in targets:
        candidate = [*current, target]
        cmd_len = len(subprocess.list2cmdline(
            get_command(candidate, report_path=report_path, ini_path=ini_path)))
        if current and cmd_len > max_chars:
            batches.append(current)
            current = [target]
        else:
            current = candidate
    if current:
        batches.append(current)
    return batches


def merge_reports(reports):
    """Combine per-batch Bandit JSON reports into a single report dict."""
    if len(reports) == 1:
        return reports[0]
    merged = {
        "errors": [],
        "generated_at": reports[-1].get("generated_at") if reports else None,
        "metrics": {"_totals": {}},
        "results": [],
    }
    totals = merged["metrics"]["_totals"]
    for report in reports:
        merged["errors"].extend(report.get("errors") or [])
        merged["results"].extend(report.get("results") or [])
        for key, value in (report.get("metrics") or {}).items():
            if key == "_totals":
                for metric_name, count in (value or {}).items():
                    if isinstance(count, (int, float)):
                        totals[metric_name] = totals.get(metric_name, 0) + count
            else:
                merged["metrics"][key] = value
    return merged


def _run_bandit_batches(batches):
    """Execute Bandit over ``batches`` and return ``(report, exit_code)``."""
    if len(batches) == 1:
        REPORT_PATH.unlink(missing_ok=True)
        try:
            completed = subprocess.run(
                get_command(batches[0], report_path=REPORT_PATH),
                cwd=str(ROOT))
        except OSError as error:
            print("bandit did not run: %s" % error, file=sys.stderr)
            return None, 2
        if completed.returncode != 0:
            print("bandit did not run (exit %d)" % completed.returncode,
                  file=sys.stderr)
            return None, 2
        try:
            report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as error:
            print("could not read %s: %s" % (REPORT_PATH, error),
                  file=sys.stderr)
            return None, 2
        return report, 0

    reports = []
    REPORT_PATH.unlink(missing_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        for index, batch in enumerate(batches):
            batch_report = Path(temporary) / ("bandit-batch-%d.json" % index)
            try:
                completed = subprocess.run(
                    get_command(batch, report_path=batch_report),
                    cwd=str(ROOT))
            except OSError as error:
                print("bandit did not run: %s" % error, file=sys.stderr)
                return None, 2
            if completed.returncode != 0:
                print("bandit did not run (exit %d)" % completed.returncode,
                      file=sys.stderr)
                return None, 2
            try:
                reports.append(
                    json.loads(batch_report.read_text(encoding="utf-8")))
            except (OSError, ValueError) as error:
                print("could not read %s: %s" % (batch_report, error),
                      file=sys.stderr)
                return None, 2
    merged = merge_reports(reports)
    try:
        REPORT_PATH.write_text(json.dumps(merged, indent=2), encoding="utf-8")
    except OSError as error:
        print("could not write %s: %s" % (REPORT_PATH, error), file=sys.stderr)
        return None, 2
    return merged, 0


def main():
    try:
        targets, excluded = collect_targets()
    except (OSError, configparser.Error) as error:
        print("bandit did not run: invalid %s: %s" % (INI_PATH, error),
              file=sys.stderr)
        return 2
    if not targets:
        print("bandit did not run: no target files found", file=sys.stderr)
        return 2

    batches = batch_targets(targets)
    report, run_code = _run_bandit_batches(batches)
    if run_code != 0:
        return run_code

    scanned, app_files, root_scripts = get_scanned_counts(report)
    total_discovered = len(targets) + len(excluded)
    print(
        "bandit scanned %d of %d python files: %d app files, %d root scripts "
        "(%d excluded)"
        % (scanned, total_discovered, app_files, root_scripts, len(excluded)))

    scanned_targets = [_relative_report_path(name)
                       for name in (report.get("metrics") or {})
                       if name != "_totals"]
    problems = [
        *get_problems(report, expected_targets=targets),
        *get_nosec_problems(scanned_targets),
    ]
    if problems:
        print("bandit gate failed with %d problem(s):" % len(problems),
              file=sys.stderr)
        for problem in problems:
            print("  " + problem, file=sys.stderr)
        return 1
    print("bandit clean: no medium-or-higher findings")
    return 0


if __name__ == "__main__":
    sys.exit(main())
