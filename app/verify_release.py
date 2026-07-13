#!/usr/bin/env python3
"""Run the required local release gates with explicit skip accounting."""

import argparse
import py_compile
import re
import subprocess
import sys
from pathlib import Path


RESULTS_RE = re.compile(
    r"RESULTS:\s+(\d+) passed,\s+(\d+) failed,\s+(\d+) skipped\s+\(total:\s*(\d+)\)"
)


def run_command(label, command, cwd, reject_unittest_skips=False):
    print(f"\n== {label} ==", flush=True)
    process = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )
    output = []
    for line in process.stdout:
        print(line, end="")
        output.append(line)
    return_code = process.wait()
    if return_code:
        raise RuntimeError(f"{label} failed with exit status {return_code}")
    combined = "".join(output)
    if reject_unittest_skips:
        validate_unittest_output(combined)
    return combined


def compile_tracked_python(repo_dir):
    print("\n== Compile tracked Python ==", flush=True)
    result = subprocess.run(
        ["git", "ls-files", "*.py"], cwd=repo_dir, capture_output=True, text=True
    )
    if result.returncode:
        raise RuntimeError("Could not enumerate tracked Python files")
    paths = [repo_dir / line for line in result.stdout.splitlines() if line]
    for path in paths:
        py_compile.compile(str(path), doraise=True)
    print(f"Compiled {len(paths)} tracked Python files.")


def validate_api_summary(output, full):
    match = RESULTS_RE.search(output)
    if not match:
        raise ValueError("API suite did not print a parseable RESULTS summary")
    result = tuple(int(value) for value in match.groups())
    expected = (83, 0, 0, 83) if full else (71, 0, 12, 83)
    if result != expected:
        mode = "full" if full else "quick"
        raise ValueError(f"Unexpected {mode} API result {result}; expected {expected}")
    return result


def validate_unittest_output(output):
    skipped = re.search(r"\bskipped=(\d+)", output)
    if skipped and int(skipped.group(1)):
        raise ValueError(f"Unit tests reported {skipped.group(1)} skipped test(s)")
    if not re.search(r"Ran \d+ tests? in ", output) or not re.search(r"^OK", output, re.MULTILINE):
        raise ValueError("Unit tests did not print a successful summary")


def run_api_suite(app_dir, full):
    label = "Full isolated API suite" if full else "Quick isolated API suite"
    command = [sys.executable, "run_isolated_api_tests.py"]
    if full:
        command.append("--full")
    print(f"\n== {label} ==", flush=True)
    process = subprocess.Popen(
        command, cwd=app_dir, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
    )
    output = []
    for line in process.stdout:
        print(line, end="")
        output.append(line)
    return_code = process.wait()
    if return_code:
        raise RuntimeError(f"{label} failed with exit status {return_code}")
    validate_api_summary("".join(output), full)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full", action="store_true",
        help="require all 83 GPU/LLM/TTS checks with zero skips",
    )
    args = parser.parse_args()
    app_dir = Path(__file__).resolve().parent
    repo_dir = app_dir.parent
    try:
        compile_tracked_python(repo_dir)
        run_command(
            "Unit test discovery",
            [sys.executable, "-m", "unittest", "discover", "-s", ".", "-p", "test_*.py", "-v"],
            app_dir,
            reject_unittest_skips=True,
        )
        run_command(
            "API contract snapshots",
            [sys.executable, "update_api_contract_snapshots.py", "--check"],
            app_dir,
        )
        run_api_suite(app_dir, args.full)
    except (OSError, py_compile.PyCompileError, RuntimeError, ValueError) as exc:
        print(f"\nRELEASE VERIFICATION FAILED: {exc}", file=sys.stderr)
        return 1
    mode = "full" if args.full else "quick"
    print(f"\nRELEASE VERIFICATION PASSED ({mode}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
