#!/usr/bin/env python3
"""Run the required local release gates with explicit skip accounting."""

import argparse
from collections import Counter
from contextlib import ExitStack
import io
import json
import os
import py_compile
import queue
import threading
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from utils import atomic_json_write
from diagnostics import get_redacted_credentials
from unit_test_sharding import parse_shard_spec
from subprocess_ownership import (get_owned_exit_result, is_subprocess_tree_running,
                                 start_owned_subprocess, stop_owned_subprocess)


def is_process_group_running(process):
    return is_subprocess_tree_running(process)


def stop_process_group(process, interrupt=False, timeout=5):
    """Retain the verifier grace while using shared descendant ownership."""
    return stop_owned_subprocess(process, interrupt=interrupt, timeout=timeout)


def run_command(label, command, cwd, reject_unittest_skips=False, capture_output=True):
    print(f"\n== {label} ==", flush=True)
    # errors="backslashreplace": this stream is displayed and searched for
    # text markers, never parsed as data, so a stray non-UTF-8 byte from a
    # child must show up as \xNN in the log rather than abort the whole
    # verifier with a UnicodeDecodeError at the reader (PR #586, 2026-09-18:
    # the unit-test gate died at "byte 0xa1 in position 3793" with every
    # test passing, and the strict decoder left no trace of which child
    # wrote it).
    with tempfile.TemporaryDirectory(prefix='alexandria-verifier-owner-') as directory, ExitStack() as stack:
        notice = Path(directory) / 'root-exit.json'
        process = start_owned_subprocess(
            command, cwd=cwd, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, errors="backslashreplace",
            start_new_session=(os.name == "posix"),
            creationflags=(subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0),
            exit_notice_path=notice, termination_grace=5,
        )
        output = [] if capture_output else None
        summary_stream = (stack.enter_context(tempfile.TemporaryFile(mode="w+", encoding="utf-8"))
                          if reject_unittest_skips and not capture_output else None)
        events = queue.Queue()
        def read_output():
            try:
                for line in process.stdout:
                    events.put(('line', line))
            except BaseException as error:
                events.put(('error', error))
            finally:
                events.put(('done', None))
        reader = threading.Thread(target=read_output, daemon=True)
        try:
            reader.start()
            failure_stopped = False
            while True:
                root_result = get_owned_exit_result(notice)
                if root_result is not None and root_result != 0 and not failure_stopped:
                    stop_process_group(process)
                    failure_stopped = True
                try:
                    kind, value = events.get(timeout=.05)
                except queue.Empty:
                    continue
                if kind == 'done':
                    break
                if kind == 'error':
                    raise value
                print(value, end="")
                if output is not None:
                    output.append(value)
                if summary_stream is not None:
                    summary_stream.write(value)
            return_code = process.wait()
            if return_code and not failure_stopped:
                stop_process_group(process)
            root_result = get_owned_exit_result(notice)
            if root_result is not None and root_result != 0:
                return_code = root_result
        except KeyboardInterrupt:
            stop_process_group(process, interrupt=True)
            raise
        except BaseException:
            stop_process_group(process)
            raise
        finally:
            control = getattr(process, '_alexandria_control', None)
            if control is not None:
                control.close()
            if reader.ident is not None:
                reader.join(timeout=5)
            process.stdout.close()

        if return_code:
            raise RuntimeError(f"{label} failed with exit status {return_code}")
        combined = "".join(output) if output is not None else None
        if reject_unittest_skips:
            if summary_stream is not None:
                summary_stream.seek(0)
                return validate_unittest_output(summary_stream)
            return validate_unittest_output(combined)
        return combined


def run_report_command(*args, **kwargs):
    """Run a streamed command without retaining its console output in reports.

    Returns the unit-test count for the unit gate and None for every other gate.
    """
    return run_command(*args, **kwargs, capture_output=False)


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
    with tempfile.TemporaryDirectory(prefix="alexandria-compile-") as directory:
        for index, path in enumerate(paths):
            py_compile.compile(str(path), cfile=str(Path(directory) / f"{index}.pyc"),
                               doraise=True)
    print(f"Compiled {len(paths)} tracked or non-ignored untracked Python files.")


def validate_api_summary(summary, full):
    """Validate API results using the suite-owned inventory and full-only flags."""
    expected_mode = "full" if full else "quick"
    if not isinstance(summary, dict):
        raise ValueError("API summary must be an object")
    if summary.get("schema_version") != 1 or summary.get("mode") != expected_mode:
        raise ValueError(f"Invalid API summary schema or mode for {expected_mode} verification")
    tests = summary.get("tests")
    counts = summary.get("counts")
    if not isinstance(tests, list) or not isinstance(counts, dict):
        raise ValueError("API summary is missing tests or counts")
    if not tests:
        raise ValueError("API summary contains no tests")
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


# A discovery accident collapses the suite silently. `unittest discover` walks
# past any directory that is not an importable package, so deleting
# `tests/__init__.py` makes it print "Ran 0 tests ... OK" - which every check
# below this line accepted as a pass. Measured, not supposed: the probe was run
# before the suite moved into `tests/`.
#
# The floor is deliberately far under the real count (1592 at the time of the
# move). It is here to catch the suite vanishing, not to be a second inventory -
# `update_test_inventory.py --check` is what notices a single test going
# missing, and it runs as its own gate.
MINIMUM_UNIT_TESTS = 500


def validate_unittest_output(output):
    lines = io.StringIO(output) if isinstance(output, str) else output
    skipped = ran = None
    successful = False
    for line in lines:
        current = re.match(r"^Ran (\d+) tests? in ", line)
        if current is not None:
            # Fixtures can print nested unittest summaries. Only the final
            # count and its verdict describe this discovery process.
            ran = current
            skipped = None
            successful = False
        elif ran is not None and re.fullmatch(r"OK(?: \(.*\))?\s*", line):
            skipped = re.search(r"\bskipped=(\d+)", line)
            successful = True
        elif re.match(r"^FAILED\b", line):
            skipped = re.search(r"\bskipped=(\d+)", line)
            successful = False
    if skipped and int(skipped.group(1)):
        raise ValueError(f"Unit tests reported {skipped.group(1)} skipped test(s)")
    if not ran or not successful:
        raise ValueError("Unit tests did not print a successful summary")
    if int(ran.group(1)) < MINIMUM_UNIT_TESTS:
        raise ValueError(
            f"Unit discovery ran only {ran.group(1)} tests, under the "
            f"{MINIMUM_UNIT_TESTS} floor - the suite is not being found. "
            "Check that app/tests/__init__.py still exists.")
    return int(ran.group(1))


def run_api_suite(app_dir, full):
    label = "Full isolated API suite" if full else "Quick isolated API suite"
    with tempfile.TemporaryDirectory(prefix="alexandria-release-") as tmp:
        summary_path = Path(tmp) / "api-summary.json"
        command = [
            sys.executable, "run_isolated_api_tests.py", "--json-summary", str(summary_path),
        ]
        if full:
            command.append("--full")
        run_report_command(label, command, app_dir)
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
    lines = get_redacted_credentials(str(exc)).splitlines()
    message = (lines[0] if lines else type(exc).__name__)[:500]
    return message


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
    parser.add_argument(
        "--shard", metavar="I/N",
        help="run only shard I of N of the unit tests (see unit_test_sharding.py); "
             "the other gates run on shard 1 only, so N parallel runs cover everything once",
    )
    args = parser.parse_args(argv)
    try:
        shard = parse_shard_spec(args.shard) if args.shard is not None else None
    except ValueError as error:
        parser.error(str(error))
    runs_other_gates = shard is None or shard[0] == 1
    app_dir = Path(__file__).resolve().parent
    repo_dir = app_dir.parent
    started = time.monotonic()
    report = {
        "schema_version": 1,
        "mode": "full" if args.full else "quick",
        "status": "running",
        "gates": [],
    }
    if args.shard is not None:
        report["shard"] = args.shard
    failure = None
    try:
        if runs_other_gates:
            run_report_gate(report, "compile_python", lambda: compile_python_files(repo_dir))
            run_report_gate(
                report, "test_inventory", lambda: run_report_command(
                    "Unit test inventory",
                    [sys.executable, "update_test_inventory.py", "--check"], app_dir,
                ),
            )
        unit_command = [sys.executable, "-m", "ci_env", "discover", "-s", ".", "-p", "test_*.py", "-v"]
        if args.shard is not None:
            unit_command += ["--shard", args.shard]
        run_report_gate(
            report, "unit_tests", lambda: {"tests_ran": run_report_command(
                # ci_env keeps local import exclusions aligned with CI.
                # CI supplies CPU Torch/PEFT for structural artifact checks.
                "Unit test discovery (CI-equivalent env)"
                + (f", shard {args.shard}" if args.shard is not None else ""),
                unit_command, app_dir, reject_unittest_skips=True,
            )},
        )
        # THE THREE CHECKS CI RUNS AND THIS DID NOT. "verifier green" was
        # followed by a red CI three times on 2026-08-19/20, every time because
        # a regenerated index was not committed - and each fix surfaced only
        # the next one, because the CI step runs them in sequence and stops at
        # the first. They belong here, where they cost two seconds, rather than
        # in a four-minute round trip. Run from the repo root, not app/.
        if runs_other_gates:
            for gate, script in (("evidence_index", "tools/audit/audit_experiment_artifacts.py"),
                                 ("legacy_audit", "tools/audit/audit_legacy_attribution.py"),
                                 ("results_index", "tools/audit/collect_results.py")):
                run_report_gate(
                    report, gate, lambda script=script: run_report_command(
                        "Evidence index (%s)" % script,
                        [sys.executable, script, "--check"], repo_dir,
                    ),
                )
            run_report_gate(
                report, "api_contract", lambda: run_report_command(
                    "API contract snapshots",
                    [sys.executable, "update_api_contract_snapshots.py", "--check"], app_dir,
                ),
            )
            run_report_gate(report, "api_tests", lambda: run_api_suite(app_dir, args.full))
    except BaseException as exc:
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
