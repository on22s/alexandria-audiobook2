#!/usr/bin/env python3
"""List the unit tests a PR's CI failed, and which of them also fail on main.

Reads each failing *job's* own log (`gh run view --job <id> --log-failed`), not
the run's: the `test` job only reports that a shard failed, so a run-level log
shows that message instead of the failing test. Every gh call names the repo,
because this checkout's `upstream` remote makes gh default to the wrong one.

Usage:
  ci_failures.py <PR number>          # the PR's current head
  ci_failures.py --run <run id>       # one specific run (e.g. an older head)
  ci_failures.py --run <id> --attempt 1   # an earlier attempt of a re-run
  add --json for machine-readable output, --no-baseline to skip main.

Stdlib only. Read-only: it never reruns, comments or pushes.
"""
import argparse
import json
import re
import subprocess
import sys

TEST_HEADER = re.compile(r"^(FAIL|ERROR): (\w+) \(([\w.]+)\)(?: \((.*)\))?\s*$")
SUMMARY = re.compile(r"^(Ran \d+ tests? in [\d.]+s|FAILED \(.*\)|OK(?: \(.*\))?)\s*$")
DETAIL = re.compile(r"^\w*(Error|Exception|Failure)\b|^AssertionError")
TIMESTAMP = re.compile(r"^\d{4}-\d\d-\d\dT[\d:.]+Z ?")
FAILED_CONCLUSIONS = {"FAILURE", "TIMED_OUT", "STARTUP_FAILURE", "failure", "timed_out"}


def gh(*args):
    result = subprocess.run(["gh", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"gh {' '.join(args)}: {result.stderr.strip()[:300]}")
    return result.stdout


def get_repo():
    url = subprocess.run(["git", "remote", "get-url", "origin"], capture_output=True,
                         text=True, check=True).stdout.strip()
    match = re.search(r"github\.com[:/]([^/]+/[^/]+?)(?:\.git)?$", url)
    if not match:
        raise RuntimeError(f"origin is not a GitHub remote: {url}")
    return match.group(1)


def get_log_text(raw):
    """One log line without gh's `job<TAB>step<TAB>timestamp ` prefix."""
    text = raw.split("\t", 2)[-1] if raw.count("\t") >= 2 else raw
    return TIMESTAMP.sub("", text)


def get_log_lines(repo, job_id):
    """The job's failed-step log, prefixes removed."""
    return [get_log_text(raw) for raw in
            gh("run", "view", "-R", repo, "--job", str(job_id), "--log-failed").splitlines()]


def get_error_lines(lines, limit=3):
    """The job's last `##[error]` annotations, which name a non-test failure."""
    errors = [line[len("##[error]"):].strip() for line in lines if line.startswith("##[error]")]
    return [line[:240] for line in errors if line and not line.startswith(("raise ", "File "))][-limit:]


def get_job_failures(lines):
    """-> ({(test, class): {"subtests": [...], "detail": str}}, [summary lines])."""
    failures, summary, current = {}, [], None
    for index, line in enumerate(lines):
        header = TEST_HEADER.match(line)
        if header:
            key = (header.group(2), header.group(3))
            entry = failures.setdefault(key, {"kind": header.group(1), "subtests": [], "detail": ""})
            if header.group(4):
                entry["subtests"].append(header.group(4))
            current = entry
            continue
        if line.startswith("=" * 20):  # unittest separates each failure's block with ====
            current = None
            continue
        if SUMMARY.match(line):  # the run summary ends the last failure's block
            summary.append(line.strip())
            current = None
            continue
        if current is not None and not current["detail"] and DETAIL.search(line):
            current["detail"] = line.strip()[:240]
    return failures, summary


def get_failed_jobs_of_run(repo, run_id, attempt=None):
    extra = ("--attempt", str(attempt)) if attempt else ()
    data = json.loads(gh("run", "view", str(run_id), "-R", repo, *extra, "--json",
                         "jobs,headSha,headBranch,conclusion,url,attempt"))
    jobs = [job for job in data["jobs"] if job.get("conclusion") in FAILED_CONCLUSIONS]
    return data, jobs


def get_failures_of_run(repo, run_id, attempt=None):
    meta, jobs = get_failed_jobs_of_run(repo, run_id, attempt)
    results = []
    for job in jobs:
        lines = get_log_lines(repo, job["databaseId"])
        failures, summary = get_job_failures(lines)
        steps = [step["name"] for step in job.get("steps", ())
                 if step.get("conclusion") in FAILED_CONCLUSIONS]
        results.append({"job": job["name"], "job_id": job["databaseId"], "url": job.get("url"),
                        "failures": failures, "summary": summary, "failed_steps": steps,
                        "errors": get_error_lines(lines)})
    return meta, results


def get_earlier_failed_attempts(repo, run_id, latest):
    """Earlier attempts of a re-run that failed: a pass after one is a flake signal."""
    failed = []
    for attempt in range(1, latest):
        _meta, jobs = get_failed_jobs_of_run(repo, run_id, attempt)
        if jobs:
            failed.append((attempt, [job["name"] for job in jobs]))
    return failed


def get_pr_runs(repo, pr):
    data = json.loads(gh("pr", "view", str(pr), "-R", repo, "--json",
                         "headRefOid,headRefName,statusCheckRollup,title"))
    runs = set()
    for check in data["statusCheckRollup"]:
        if check.get("conclusion") in FAILED_CONCLUSIONS:
            match = re.search(r"/actions/runs/(\d+)", check.get("detailsUrl") or "")
            if match:
                runs.add(int(match.group(1)))
    return data, sorted(runs)


def get_main_baseline(repo):
    """Failing test keys of the latest completed tests.yml run on main (empty if it passed)."""
    runs = json.loads(gh("run", "list", "-R", repo, "--workflow", "tests.yml", "--branch", "main",
                         "--status", "completed", "--limit", "1",
                         "--json", "databaseId,headSha,conclusion,createdAt"))
    if not runs:
        return None, None  # no main run to compare with: claim nothing
    run = runs[0]
    keys = set()
    if run["conclusion"] != "success":
        _meta, results = get_failures_of_run(repo, run["databaseId"])
        for result in results:
            keys.update(result["failures"])
    return run, keys


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("pr", nargs="?", type=int, help="PR number (its current head)")
    target.add_argument("--run", type=int, help="a specific Actions run id")
    parser.add_argument("--attempt", type=int, help="with --run: an earlier attempt of a re-run")
    parser.add_argument("--repo", help="owner/name (default: from the origin remote)")
    parser.add_argument("--no-baseline", action="store_true", help="skip the main comparison")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    repo = args.repo or get_repo()

    if args.run:
        run_ids, header = [args.run], f"run {args.run}"
    else:
        pr, run_ids = get_pr_runs(repo, args.pr)
        header = f"PR #{args.pr} head {pr['headRefOid'][:10]} ({pr['headRefName']})"
    results, retried = [], []
    for run_id in run_ids:
        meta, jobs = get_failures_of_run(repo, run_id, args.attempt)
        results.extend(dict(job, run_id=run_id, head=meta["headSha"][:10]) for job in jobs)
        if not args.attempt and meta.get("attempt", 1) > 1:
            for attempt, names in get_earlier_failed_attempts(repo, run_id, meta["attempt"]):
                retried.append((run_id, attempt, meta["attempt"], names))
    # None, not an empty set, when skipped: nothing was compared, so claim nothing.
    baseline_run, baseline = (None, None) if args.no_baseline else get_main_baseline(repo)

    if args.json:
        print(json.dumps({"repo": repo, "target": header, "baseline_run": baseline_run,
                          "earlier_failed_attempts": retried, "jobs": [
            {**{k: v for k, v in job.items() if k != "failures"}, "failures": [
                {"test": t, "class": c, **entry,
                 "on_main": None if baseline is None else (t, c) in baseline}
                for (t, c), entry in job["failures"].items()]} for job in results]}, indent=1))
        return 1 if results else 0

    print(f"{repo}: {header}")
    if baseline_run:
        state = "passed" if not baseline else f"failed {len(baseline)} tests"
        print(f"main baseline: run {baseline_run['databaseId']} at {baseline_run['headSha'][:10]} "
              f"({baseline_run['createdAt'][:16]}Z) {state}")
    for run_id, attempt, latest, names in retried:
        print(f"note: run {run_id} attempt {attempt} of {latest} failed ({', '.join(names)}); "
              f"a later attempt is shown. Inspect it with --run {run_id} --attempt {attempt}.")
    if not results:
        print("No failing jobs" + (" in the latest attempt." if retried else "."))
        return 0
    for job in results:
        print(f"\n== {job['job']} (run {job['run_id']}, job {job['job_id']}, head {job['head']})")
        if job["summary"]:
            print("   " + " | ".join(job["summary"][-2:]))
        if job["failed_steps"]:
            print(f"   failed step: {', '.join(job['failed_steps'])}")
        if not job["failures"]:
            print("   no unittest failure in this job: the failure is the step above")
            for error in job["errors"]:
                print(f"   ##[error] {error}")
            print(f"   {job['url']}")
        for (test, cls), entry in sorted(job["failures"].items(), key=lambda item: item[0][1]):
            label = ("not compared" if baseline is None
                     else "also on main" if (test, cls) in baseline else "NEW vs main")
            subtests = f" [{len(entry['subtests'])} subtests]" if entry["subtests"] else ""
            print(f"   {entry['kind']:5} {label:12} {cls}.{test}{subtests}")
            if entry["detail"]:
                print(f"         {entry['detail']}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
