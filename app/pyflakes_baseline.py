"""Fail CI only on NEW pyflakes findings in app/*.py and app/routers/*.py.

The checked-in baseline in tests/pyflakes_baseline.json records the findings
that already exist. A finding is normalised to ``"path: message"`` with no line
or column, and counted, so an unrelated edit above it is invisible while a
second copy of the same finding is still caught.

Usage (from app/):

    python pyflakes_baseline.py --check   # gate, non-zero on new findings
    python pyflakes_baseline.py --update  # rewrite the baseline deliberately

pyflakes is imported lazily in collect_findings(): importing this module (for
example during test discovery) must not require the linter to be installed.
An environment that runs the check without pyflakes fails loudly instead.
"""

import argparse
import json
import re
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
BASELINE_PATH = APP_DIR / "tests" / "pyflakes_baseline.json"
LINE_NUMBER = re.compile(r"\bline \d+\b")


class _Collector:
    """Receive pyflakes findings through its reporter API, not CLI text.

    All three callbacks are required: a syntax error or an unexpected parse
    failure is a real problem, never a finding to silently baseline.
    """

    def __init__(self):
        self.messages = []

    def flake(self, message):
        self.messages.append(message)

    def syntaxError(self, filename, msg, lineno, offset, text):
        raise RuntimeError(f"{filename}: syntax error: {msg}")

    def unexpectedError(self, filename, msg):
        raise RuntimeError(f"{filename}: could not be checked: {msg}")


def get_source_files(app_dir):
    """Only the app modules and routers: not tests, experiments or tools."""
    app_dir = Path(app_dir)
    files = sorted(app_dir.glob("*.py"))
    routers = app_dir / "routers"
    if routers.is_dir():
        files.extend(sorted(routers.glob("*.py")))
    return files


def message_key(relative_path, message):
    """Stable key: forward-slash path and message text, no line or column.

    Some pyflakes messages embed a source line in their text (for example
    "redefinition of unused 'x' from line 3"); normalise that too, otherwise
    an unrelated edit above the finding would rename its key.
    """
    text = message.message % message.message_args
    text = LINE_NUMBER.sub("line N", text)
    return f"{relative_path}: {text}"


def collect_findings(app_dir):
    """Return ``{normalised finding: count}`` for one app tree."""
    from pyflakes import api  # lazy: discovery must not need the linter

    app_dir = Path(app_dir)
    counts = {}
    for path in get_source_files(app_dir):
        relative_path = path.relative_to(app_dir.parent).as_posix()
        collector = _Collector()
        api.check(path.read_text(encoding="utf-8"), str(path), collector)
        for message in collector.messages:
            key = message_key(relative_path, message)
            counts[key] = counts.get(key, 0) + 1
    return counts


def load_baseline(path):
    """Read a baseline, refusing anything that is not path-to-count JSON."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict) or any(
        not isinstance(key, str) or not isinstance(value, int)
        or isinstance(value, bool) or value < 1
        for key, value in data.items()
    ):
        raise ValueError(f"{path} is not a mapping of findings to positive counts")
    return data


def write_baseline(path, findings):
    """Write a deterministic, LF-terminated baseline and return its path."""
    path = Path(path)
    text = json.dumps(findings, indent=2, sort_keys=True, ensure_ascii=False)
    path.write_text(text + "\n", encoding="utf-8", newline="\n")
    return path


def compare_findings(actual, baseline):
    """Return ``(new, stale)`` counts; only ``new`` makes the check fail."""
    new = {}
    for key, count in actual.items():
        extra = count - baseline.get(key, 0)
        if extra > 0:
            new[key] = extra
    stale = {}
    for key, count in baseline.items():
        missing = count - actual.get(key, 0)
        if missing > 0:
            stale[key] = missing
    return new, stale


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true",
                      help="fail if any finding is absent from the baseline")
    mode.add_argument("--update", action="store_true",
                      help="rewrite the baseline from the current tree")
    parser.add_argument("--app-dir", type=Path, default=APP_DIR,
                        help="app tree to scan (test fixtures override this)")
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH,
                        help="baseline JSON (test fixtures override this)")
    args = parser.parse_args(argv)

    try:
        actual = collect_findings(args.app_dir)
    except ImportError as error:
        print(f"pyflakes baseline could not be checked: {error}")
        print("Install app/requirements-test.txt before running this gate.")
        return 2
    except (OSError, RuntimeError, SyntaxError) as error:
        print(f"pyflakes baseline could not be checked: {error}")
        return 2

    if args.update:
        path = write_baseline(args.baseline, actual)
        print(f"Updated {path}: {sum(actual.values())} findings "
              f"across {len(actual)} keys")
        return 0

    try:
        baseline = load_baseline(args.baseline)
    except (OSError, ValueError) as error:
        print(f"pyflakes baseline could not be checked: {error}")
        return 2

    new, stale = compare_findings(actual, baseline)
    if stale:
        print("Stale baseline entries (findings that were fixed; "
              "run 'python pyflakes_baseline.py --update' to drop them):")
        for key in sorted(stale):
            print(f"- {key} (x{stale[key]})")
    if new:
        print("NEW pyflakes findings, not in the baseline:")
        for key in sorted(new):
            print(f"- {key} (x{new[key]})")
        print("Fix the code, or run 'python pyflakes_baseline.py --update' "
              "if the finding is deliberate.")
        return 1
    print(f"pyflakes baseline matches ({sum(actual.values())} findings)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
