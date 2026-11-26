"""Run Bandit over the app and the repo-root scripts, then gate on its report.

Bandit's INI ``targets`` option takes comma-separated paths, not globs, so the
target set is derived here instead: every ``*.py`` under ``app/`` (never a
virtualenv's) plus every ``*.py`` in the repository root. New and renamed
modules are scanned without editing ``bandit.ini``, and no baseline file is
used.

The JSON report is the gate, not bandit's exit code: ``--exit-zero`` keeps
bandit from failing on findings (so the report is always written), and this
script exits 1 when the report still contains a medium-or-higher finding,
when bandit reports any file it could not parse or read, or when a ``nosec``
comment does not follow the id-only convention. LOW findings are counted in
the report metrics only and do not gate.

Exit codes: 0 clean, 1 findings or scan errors, 2 bandit itself did not run.
"""

import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tokenize

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
INI_PATH = APP_DIR / "bandit.ini"
REPORT_PATH = APP_DIR / "bandit-report.json"
SEVERITY_LEVEL = "medium"

# Bandit parses every token after "nosec", and any token that names a test id
# or plugin suppresses that test too. Appending a reason that names B602 would
# therefore hide a HIGH B602 finding. The convention is one id, nothing else,
# with the reason on the line above.
NOSEC_LINE = re.compile(r"#\s*nosec\b[^#\n]*")
VALID_NOSEC_LINE = re.compile(r"#\s*nosec\s+B\d+\s*$")


def _venv_roots(app_dir):
    """Directories under app_dir that are virtualenvs (pyvenv.cfg lives there)."""
    return {path.parent for path in app_dir.rglob("pyvenv.cfg")}


def get_app_python_files(app_dir=APP_DIR):
    """Our own app modules, never a virtualenv's.

    ``app/env`` is a virtualenv inside the directory being walked, so a bare
    rglob returns tens of thousands of site-packages modules (and Bandit scans
    them, fails on their code, and takes minutes). CI and fresh worktrees have
    no venv, so that failure only appears in the live checkout - the tree the
    GPU queue actually runs from. This is the same sweep as
    tests.test_support.app_python_files: skip anything under a directory that
    holds pyvenv.cfg, and anything with a site-packages/dist-packages part.
    """
    app_dir = Path(app_dir)
    roots = _venv_roots(app_dir)
    files = []
    for path in sorted(app_dir.rglob("*.py")):
        if "site-packages" in path.parts or "dist-packages" in path.parts:
            continue
        if any(root in path.parents for root in roots):
            continue
        files.append(path)
    return files


def get_targets(root=ROOT):
    """Every app module plus every repo-root script, as relative posix paths.

    Explicit files (rather than the ``app`` directory) are what excludes the
    virtualenvs. Posix separators keep Bandit's ``app/tests/*``-style exclude
    globs matching on Windows as well as Linux.
    """
    root = Path(root)
    files = [*get_app_python_files(root / "app"), *root.glob("*.py")]
    return sorted(path.relative_to(root).as_posix() for path in files)


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


def get_nosec_problems(targets, root=ROOT):
    """Reject a nosec comment that names anything beyond one test id."""
    problems = []
    root = Path(root)
    for target in targets:
        try:
            text = (root / target).read_text(encoding="utf-8")
            comments = [token for token in
                        tokenize.generate_tokens(io.StringIO(text).readline)
                        if token.type == tokenize.COMMENT]
        except (OSError, UnicodeDecodeError, SyntaxError,
                tokenize.TokenError):
            # Unreadable/unparsable files are Bandit's errors array, not ours.
            continue
        for token in comments:
            comment = token.string
            if not NOSEC_LINE.search(comment) or VALID_NOSEC_LINE.search(comment):
                continue
            problems.append(
                "{target}:{line} invalid nosec comment; use one test id with "
                "the reason on the line above: {comment}".format(
                    target=target, line=token.start[0], comment=comment))
    return problems


def get_problems(report):
    """One message per unscannable file and per remaining finding."""
    problems = []
    for error in report.get("errors") or []:
        problems.append(
            "could not scan {filename}: {reason}".format(
                filename=error.get("filename"), reason=error.get("reason")))
    for result in report.get("results") or []:
        text = " ".join(str(result.get("issue_text") or "").split())
        problems.append(
            "{filename}:{line_number} {test_id} {severity}: {text}".format(
                filename=result.get("filename"),
                line_number=result.get("line_number"),
                test_id=result.get("test_id"),
                severity=result.get("issue_severity"),
                text=text))
    return problems


def get_command(targets=None):
    """The exact Bandit argv the gate runs."""
    if targets is None:
        targets = get_targets()
    return [
        sys.executable, "-m", "bandit",
        "--ini", str(INI_PATH),
        "-r",
        "-f", "json",
        "-o", str(REPORT_PATH),
        "--severity-level", SEVERITY_LEVEL,
        "--exit-zero",
        *targets,
    ]


def main():
    targets = get_targets()
    REPORT_PATH.unlink(missing_ok=True)
    try:
        completed = subprocess.run(get_command(targets), cwd=str(ROOT))
    except OSError as error:
        print("bandit did not run: %s" % error, file=sys.stderr)
        return 2
    if completed.returncode != 0:
        print("bandit did not run (exit %d)" % completed.returncode,
              file=sys.stderr)
        return 2
    try:
        report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print("could not read %s: %s" % (REPORT_PATH, error), file=sys.stderr)
        return 2

    scanned, app_files, root_scripts = get_scanned_counts(report)
    print("bandit scanned %d files: %d app files, %d root scripts"
          % (scanned, app_files, root_scripts))

    scanned_targets = [_relative_report_path(name)
                       for name in (report.get("metrics") or {})
                       if name != "_totals"]
    problems = [*get_problems(report), *get_nosec_problems(scanned_targets)]
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
