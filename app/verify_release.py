#!/usr/bin/env python3
"""Run the required local release gates with explicit skip accounting."""

import argparse
from collections import Counter
from contextlib import ExitStack
import io
import json
import math
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
from subprocess_ownership import (get_owned_exit_result,
                                 start_owned_subprocess, stop_owned_subprocess,
                                 run_owned_capture)

GIT_PROBE_TIMEOUT_SECONDS = 20
# Below the 45-minute CI `unit` job timeout so a hung shard fails loudly with
# its shard named and its owned subprocess tree stopped, rather than GitHub
# cancelling silently.
CI_UNIT_SHARD_TIMEOUT_SECONDS = 40 * 60


def stop_process_group(process, interrupt=False, timeout=5):
    """Retain grace, escalating headless Windows workers through their Job."""
    try:
        return stop_owned_subprocess(process, interrupt=interrupt, timeout=timeout,
                                     force_after_grace=sys.platform == 'win32')
    except BrokenPipeError:
        # The owner can finish between the liveness probe and signal send.
        # Its exit proves descendant cleanup completed; retain the same bound.
        process.wait(timeout=timeout)


def run_command(label, command, cwd, reject_unittest_skips=False, capture_output=True, timeout=None):
    if timeout is not None and (not isinstance(timeout, (int, float)) or isinstance(timeout, bool)
                                or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError(f"{label} timeout must be finite and positive")
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
        deadline = None if timeout is None else time.monotonic() + timeout
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
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError(f"{label} timed out after {timeout}s")
                root_result = get_owned_exit_result(notice)
                if root_result is not None and root_result != 0 and not failure_stopped:
                    stop_process_group(process)
                    failure_stopped = True
                wait = .05 if deadline is None else max(0.0, min(.05, deadline - time.monotonic()))
                try:
                    kind, value = events.get(timeout=wait)
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
            if deadline is None:
                return_code = process.wait()
            else:
                try:
                    return_code = process.wait(timeout=max(0.0, deadline - time.monotonic()))
                except subprocess.TimeoutExpired as exc:
                    raise TimeoutError(f"{label} timed out after {timeout}s") from exc
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


def run_unit_test_report(label, command, app_dir, *, workers=None, report_dir=None,
                         timeout=None, serial_on_low_disk=False):
    """Record executed identities alongside the independently checked count."""
    if workers is not None:
        from local_unit_shards import run_local_unit_tests
        result = run_local_unit_tests(Path(app_dir).parent, workers=workers, report_dir=report_dir,
                                      timeout=timeout, serial_on_low_disk=serial_on_low_disk,
                                      minimum_tests=MINIMUM_UNIT_TESTS)
        if result['status'] != 'passed':
            error = RuntimeError(f"Local unit discovery failed: Ran {result['tests_ran']}; "
                                 f"failures={result['failures']}, errors={result['errors']}, "
                                 f"skips={result['skips']}; artifacts: {result['artifact_dir']}")
            error.result = result
            raise error
        return result
    with tempfile.TemporaryDirectory(prefix="alexandria-unit-receipt-") as directory:
        receipt = Path(directory) / "tests.json"
        report_kwargs = {"reject_unittest_skips": True}
        if timeout is not None:
            report_kwargs["timeout"] = timeout
        count = run_report_command(label, command + ["--test-report", str(receipt)],
                                   app_dir, **report_kwargs)
        ids = json.loads(receipt.read_text(encoding="utf-8")).get("test_ids")
        if (not isinstance(ids, list) or len(ids) != count
                or any(not isinstance(identifier, str) or not identifier for identifier in ids)
                or len(set(ids)) != len(ids)):
            raise ValueError("Unit-test receipt identities do not match the executed count")
        return {"tests_ran": count, "test_ids": ids}


def get_python_paths(repo_dir):
    """Return tracked and non-ignored untracked Python files deterministically."""
    result = run_owned_capture(
        ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard", "--", "*.py"],
        cwd=repo_dir, timeout=GIT_PROBE_TIMEOUT_SECONDS,
    )
    if result.returncode:
        raise RuntimeError("Could not enumerate Python files")
    return [Path(repo_dir) / os.fsdecode(name)
            for name in sorted(set(result.stdout.split(b"\0"))) if name]


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
    if type(summary.get("schema_version")) is not int or summary.get("schema_version") != 1 or summary.get("mode") != expected_mode:
        raise ValueError(f"Invalid API summary schema or mode for {expected_mode} verification")
    tests = summary.get("tests")
    counts = summary.get("counts")
    if not isinstance(tests, list) or not isinstance(counts, dict):
        raise ValueError("API summary is missing tests or counts")
    if any(type(value) is not int or value < 0 for value in counts.values()):
        raise ValueError("API summary counts must be nonnegative integers")
    if not tests:
        raise ValueError("API summary contains no tests")
    names = [test.get("name") for test in tests if isinstance(test, dict)]
    if len(names) != len(tests) or any(not isinstance(name, str) or not name for name in names) or len(set(names)) != len(names):
        raise ValueError("API summary test names must be non-empty and unique strings")
    if any(type(test.get("requires_full")) is not bool for test in tests):
        raise ValueError("API summary tests must declare requires_full")
    status_values = [test.get("status") for test in tests]
    if any(not isinstance(status, str) or status not in {"passed", "failed", "skipped"} for status in status_values):
        raise ValueError("API summary contains an invalid test status")
    statuses = Counter(status_values)
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
        if getattr(exc, 'result', None) is not None:
            gate['result'] = exc.result
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
    parser.add_argument('--local-units', action='store_true',
                        help='local unit receipts and isolated shards (ready.sh default); explicit --shard keeps CI behavior')
    parser.add_argument('--unit-workers', type=int, metavar='N',
                        help='isolated local unit workers; native Windows default is 2; 0 selects serial; higher counts raise memory, driver and IO pressure')
    parser.add_argument('--unit-report-dir', metavar='PATH',
                        help='new or empty directory below the tool temp root alexandria-local-units for retained logs and counts')
    parser.add_argument('--unit-shard-timeout', type=float, metavar='SECONDS',
                        help='local unit wall bound (default 3600 per parallel shard, 7200 serial); no production deadlines change')
    parser.add_argument('--serial-on-low-disk', action='store_true',
                        help='explicitly allow serial discovery when isolated snapshot disk admission refuses')
    parser.add_argument(
        "--shard", metavar="I/N",
        help="run only shard I of N of the unit tests (see unit_test_sharding.py); "
             "the other gates run on shard 1 only, so N parallel runs cover everything once",
    )
    parser.add_argument(
        "--check-evidence", action="store_true",
        help="check all derived evidence indexes before units, including on later shards",
    )
    parser.add_argument(
        "--skip-evidence-recheck", action="store_true",
        help="mark the derived-evidence gates skipped because the caller just regenerated "
             "every derived file; ignored when --check-evidence is explicit; other "
             "unsharded gates still run",
    )
    args = parser.parse_args(argv)
    try:
        shard = parse_shard_spec(args.shard) if args.shard is not None else None
    except ValueError as error:
        parser.error(str(error))
    explicit_local_options = (args.unit_workers is not None or args.unit_report_dir is not None
                              or args.serial_on_low_disk or args.unit_shard_timeout is not None)
    if args.unit_workers is not None and args.unit_workers < 0:
        parser.error('--unit-workers must be nonnegative')
    if shard is not None and explicit_local_options:
        parser.error('Local unit worker/report/timeout options cannot be combined with --shard')
    if args.unit_shard_timeout is not None and not 0 < args.unit_shard_timeout < float('inf'):
        parser.error('--unit-shard-timeout must be finite and positive')
    unit_options = {}
    if shard is None and (args.local_units or explicit_local_options):
        from local_unit_shards import get_default_workers
        workers = (args.unit_workers if args.unit_workers is not None
                   else get_default_workers() if sys.platform == 'win32' else 0)
        unit_options = dict(workers=workers, report_dir=args.unit_report_dir,
                            timeout=args.unit_shard_timeout, serial_on_low_disk=args.serial_on_low_disk)
    elif shard is not None:
        unit_options = dict(timeout=CI_UNIT_SHARD_TIMEOUT_SECONDS)
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
        # One owner for the checks formerly repeated by ready.sh and CI.
        # ready.sh marks them skipped after regeneration; an explicit
        # --check-evidence still forces them on every shard.
        evidence_gates = (("evidence_index", "tools/audit/audit_experiment_artifacts.py"),
                          ("legacy_audit", "tools/audit/audit_legacy_attribution.py"),
                          ("results_index", "tools/audit/collect_results.py"),
                          ("goal_evidence", "app/experiments/goal_evidence_audit.py"))
        if args.check_evidence or (runs_other_gates and not args.skip_evidence_recheck):
            for gate, script in evidence_gates:
                run_report_gate(
                    report, gate, lambda script=script: run_report_command(
                        "Evidence index (%s)" % script,
                        [sys.executable, script, "--check"], repo_dir,
                    ),
                )
        elif runs_other_gates and args.skip_evidence_recheck:
            report["gates"].extend({
                "name": gate,
                "status": "skipped",
                "reason": "ready.sh regenerated derived evidence before the verifier",
                "duration_seconds": 0.0,
            } for gate, _script in evidence_gates)
        unit_command = [sys.executable, "-m", "ci_env", "discover", "-s", ".", "-p", "test_*.py", "-v"]
        if args.shard is not None:
            unit_command += ["--shard", args.shard]
        run_report_gate(
            report, "unit_tests", lambda: run_unit_test_report(
                # ci_env keeps local import exclusions aligned with CI.
                # CI supplies CPU Torch/PEFT for structural artifact checks.
                "Unit test discovery (CI-equivalent env)"
                + (f", shard {args.shard}" if args.shard is not None else ""),
                unit_command, app_dir, **unit_options,
            ),
        )
        if runs_other_gates:
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
                atomic_json_write(report, args.json_report, allow_nonatomic_fallback=False)
            except OSError as exc:
                failure = exc
                print(f"\nRELEASE VERIFICATION FAILED: could not write JSON report: {exc}",
                      file=sys.stderr)
    if failure is not None:
        if isinstance(failure, KeyboardInterrupt):
            return 130
        if args.check_evidence and report.get("failure", {}).get("gate") in {
                "evidence_index", "legacy_audit", "results_index", "goal_evidence"}:
            return 2  # Preserve ready.sh's unsettled-index exit status.
        return 1
    print(f"\nRELEASE VERIFICATION PASSED ({report['mode']}).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
