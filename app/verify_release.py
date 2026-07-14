#!/usr/bin/env python3
"""Run the required local release gates with explicit skip accounting."""

import argparse
from collections import Counter
import json
import os
import py_compile
import re
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from utils import atomic_json_write


def is_process_group_running(process):
    if os.name != "posix":
        return process.poll() is None
    try:
        os.killpg(process.pid, 0)
        return True
    except ProcessLookupError:
        return False


def stop_process_group(process, interrupt=False, timeout=5):
    """Stop a verifier child and all descendants, escalating after a bounded wait."""
    graceful_signal = signal.SIGINT if interrupt else signal.SIGTERM
    try:
        if os.name == "posix":
            os.killpg(process.pid, graceful_signal)
        elif process.poll() is None:
            process.terminate()
    except ProcessLookupError:
        return
    deadline = time.monotonic() + timeout
    while is_process_group_running(process) and time.monotonic() < deadline:
        time.sleep(0.05)
    if not is_process_group_running(process):
        return
    if os.name == "posix":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            return
    else:
        subprocess.run(
            ["taskkill", "/PID", str(process.pid), "/T", "/F"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
    if process.poll() is None:
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            pass


def run_command(label, command, cwd, reject_unittest_skips=False):
    print(f"\n== {label} ==", flush=True)
    process = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True,
        start_new_session=(os.name == "posix"),
        creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
    )
    output = []
    try:
        for line in process.stdout:
            print(line, end="")
            output.append(line)
        return_code = process.wait()
    except KeyboardInterrupt:
        stop_process_group(process, interrupt=True)
        raise
    except BaseException:
        stop_process_group(process)
        raise
    finally:
        process.stdout.close()
    if return_code:
        stop_process_group(process)
        raise RuntimeError(f"{label} failed with exit status {return_code}")
    combined = "".join(output)
    if reject_unittest_skips:
        validate_unittest_output(combined)
    return combined


def run_report_command(*args, **kwargs):
    """Run a streamed command without retaining its console output in reports."""
    run_command(*args, **kwargs)


def get_python_paths(repo_dir):
    """Return tracked and non-ignored untracked Python files deterministically."""
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "--", "*.py"],
        cwd=repo_dir, capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError("Could not enumerate Python files")
    return [Path(repo_dir) / line for line in sorted(set(result.stdout.splitlines())) if line]


def compile_python_files(repo_dir):
    print("\n== Compile Python files ==", flush=True)
    paths = get_python_paths(repo_dir)
    for path in paths:
        py_compile.compile(str(path), doraise=True)
    print(f"Compiled {len(paths)} tracked or non-ignored untracked Python files.")


def validate_api_summary(summary, full):
    """Validate API results using the suite-owned inventory and full-only flags."""
    expected_mode = "full" if full else "quick"
    if summary.get("schema_version") != 1 or summary.get("mode") != expected_mode:
        raise ValueError(f"Invalid API summary schema or mode for {expected_mode} verification")
    tests = summary.get("tests")
    counts = summary.get("counts")
    if not isinstance(tests, list) or not isinstance(counts, dict):
        raise ValueError("API summary is missing tests or counts")
    names = [test.get("name") for test in tests if isinstance(test, dict)]
    if len(names) != len(tests) or any(not name for name in names) or len(set(names)) != len(names):
        raise ValueError("API summary test names must be non-empty and unique")
    if any(type(test.get("requires_full")) is not bool for test in tests):
        raise ValueError("API summary tests must declare requires_full")
    statuses = Counter(test.get("status") for test in tests)
    if set(statuses) - {"passed", "failed", "skipped"}:
        raise ValueError("API summary contains an invalid test status")
    actual_counts = {
        "passed": statuses["passed"], "failed": statuses["failed"],
        "skipped": statuses["skipped"], "total": len(tests),
    }
    if counts != actual_counts:
        raise ValueError(f"API summary counts {counts} do not match test records {actual_counts}")
    expected_skips = {test["name"] for test in tests if test["requires_full"] and not full}
    actual_skips = {test["name"] for test in tests if test["status"] == "skipped"}
    failed = [test["name"] for test in tests if test["status"] == "failed"]
    if failed:
        raise ValueError(f"API suite reported failed tests: {', '.join(failed)}")
    if actual_skips != expected_skips:
        raise ValueError(
            f"Unexpected {expected_mode} API skips: expected {sorted(expected_skips)}, "
            f"got {sorted(actual_skips)}"
        )
    return actual_counts


def validate_unittest_output(output):
    skipped = re.search(r"\bskipped=(\d+)", output)
    if skipped and int(skipped.group(1)):
        raise ValueError(f"Unit tests reported {skipped.group(1)} skipped test(s)")
    if not re.search(r"Ran \d+ tests? in ", output) or not re.search(r"^OK", output, re.MULTILINE):
        raise ValueError("Unit tests did not print a successful summary")


def run_api_suite(app_dir, full):
    label = "Full isolated API suite" if full else "Quick isolated API suite"
    with tempfile.TemporaryDirectory(prefix="alexandria-release-") as tmp:
        summary_path = Path(tmp) / "api-summary.json"
        command = [
            sys.executable, "run_isolated_api_tests.py", "--json-summary", str(summary_path),
        ]
        if full:
            command.append("--full")
        run_command(label, command, app_dir)
        try:
            summary = json.loads(summary_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Could not read API JSON summary: {exc}") from exc
        counts = validate_api_summary(summary, full)
        return {
            "counts": counts,
            "skips": [
                {"name": test["name"], "reason": test.get("reason", "")}
                for test in summary["tests"] if test["status"] == "skipped"
            ],
        }


def get_concise_error(exc):
    """Return a bounded one-line error with common credential values redacted."""
    lines = str(exc).splitlines()
    message = (lines[0] if lines else type(exc).__name__)[:500]
    message = re.sub(
        r"(?i)\b(api[_-]?key|token|password|secret)(\s*[=:]\s*)\S+",
        r"\1\2[REDACTED]", message,
    )
    return re.sub(r"(https?://)[^/@\s:]+:[^/@\s]+@", r"\1[REDACTED]@", message)


def run_report_gate(report, name, callback):
    """Run one release gate and append its timed status to the report."""
    started = time.monotonic()
    gate = {"name": name}
    try:
        result = callback()
        gate["status"] = "passed"
        if result is not None:
            gate["result"] = result
        return result
    except BaseException as exc:
        gate.update({
            "status": "failed",
            "failure": {"type": type(exc).__name__, "message": get_concise_error(exc)},
        })
        raise
    finally:
        gate["duration_seconds"] = round(time.monotonic() - started, 3)
        report["gates"].append(gate)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--full", action="store_true",
        help="require every API check, including GPU/LLM/TTS tests, with zero skips",
    )
    parser.add_argument("--json-report", metavar="PATH",
                        help="atomically write a machine-readable release report")
    args = parser.parse_args(argv)
    app_dir = Path(__file__).resolve().parent
    repo_dir = app_dir.parent
    started = time.monotonic()
    report = {
        "schema_version": 1,
        "mode": "full" if args.full else "quick",
        "status": "running",
        "gates": [],
    }
    failure = None
    try:
        run_report_gate(report, "compile_python", lambda: compile_python_files(repo_dir))
        run_report_gate(
            report, "test_inventory", lambda: run_report_command(
                "Unit test inventory",
                [sys.executable, "update_test_inventory.py", "--check"], app_dir,
            ),
        )
        run_report_gate(
            report, "unit_tests", lambda: run_report_command(
                # Run through ci_env so the ML libraries CI lacks are hidden
                # here too. Without this the gate only tests the developer's
                # machine, and a test touching torch passes locally then fails
                # in CI (see ci_env.BLOCKED_MODULES).
                "Unit test discovery (CI-equivalent env)",
                [sys.executable, "-m", "ci_env", "discover", "-s", ".", "-p", "test_*.py", "-v"],
                app_dir, reject_unittest_skips=True,
            ),
        )
        run_report_gate(
            report, "api_contract", lambda: run_report_command(
                "API contract snapshots",
                [sys.executable, "update_api_contract_snapshots.py", "--check"], app_dir,
            ),
        )
        run_report_gate(report, "api_tests", lambda: run_api_suite(app_dir, args.full))
    except (OSError, py_compile.PyCompileError, RuntimeError, ValueError, KeyboardInterrupt) as exc:
        failure = exc
        report["status"] = "failed"
        report["failure"] = {
            "gate": report["gates"][-1]["name"],
            "type": type(exc).__name__,
            "message": get_concise_error(exc),
        }
        print(f"\nRELEASE VERIFICATION FAILED: {get_concise_error(exc)}", file=sys.stderr)
    else:
        report["status"] = "passed"
    finally:
        report["duration_seconds"] = round(time.monotonic() - started, 3)
        if args.json_report:
            try:
                atomic_json_write(report, args.json_report)
            except OSError as exc:
                failure = exc
                print(f"\nRELEASE VERIFICATION FAILED: could not write JSON report: {exc}",
                      file=sys.stderr)
    if failure is not None:
        return 130 if isinstance(failure, KeyboardInterrupt) else 1
    print(f"\nRELEASE VERIFICATION PASSED ({report['mode']}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
