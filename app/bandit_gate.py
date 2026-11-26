"""Run Bandit over the app and the repo-root scripts, then gate on its report.

Bandit's INI ``targets`` option takes comma-separated paths, not globs, so the
target set is derived here instead: the whole ``app`` directory plus every
``*.py`` in the repository root. New and renamed modules are scanned without
editing ``bandit.ini``, and no baseline file is used.

The JSON report is the gate, not bandit's exit code: ``--exit-zero`` keeps
bandit from failing on findings (so the report is always written), and this
script exits 1 when the report still contains a medium-or-higher finding or
when bandit reports any file it could not parse or read. LOW findings stay in
the report but do not gate.

Exit codes: 0 clean, 1 findings or scan errors, 2 bandit itself did not run.
"""

import json
from pathlib import Path
import subprocess
import sys

APP_DIR = Path(__file__).resolve().parent
ROOT = APP_DIR.parent
INI_PATH = APP_DIR / "bandit.ini"
REPORT_PATH = APP_DIR / "bandit-report.json"
SEVERITY_LEVEL = "medium"


def get_targets(root=ROOT):
    """Every app module plus every repo-root script, relative to the root."""
    return ["app", *sorted(path.name for path in Path(root).glob("*.py"))]


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


def main(argv=None):
    targets = get_targets()
    REPORT_PATH.unlink(missing_ok=True)
    command = [
        sys.executable, "-m", "bandit",
        "--ini", str(INI_PATH),
        "-r",
        "-f", "json",
        "-o", str(REPORT_PATH),
        "--severity-level", SEVERITY_LEVEL,
        "--exit-zero",
        *targets,
    ]
    completed = subprocess.run(command, cwd=str(ROOT))
    if completed.returncode != 0:
        print("bandit did not run (exit %d)" % completed.returncode,
              file=sys.stderr)
        return 2
    try:
        report = json.loads(REPORT_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        print("could not read %s: %s" % (REPORT_PATH, error), file=sys.stderr)
        return 2
    problems = get_problems(report)
    if problems:
        print("bandit gate failed with %d problem(s):" % len(problems),
              file=sys.stderr)
        for problem in problems:
            print("  " + problem, file=sys.stderr)
        return 1
    print("bandit clean: no medium-or-higher findings in %d targets"
          % len(targets))
    return 0


if __name__ == "__main__":
    sys.exit(main())
