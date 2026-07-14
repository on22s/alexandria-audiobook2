"""Verify the current pull request and mark it ready for review."""

import argparse
import json
import re
import subprocess
import sys
import tempfile
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
REPO_DIR = APP_DIR.parent


def get_origin_repo(origin_url):
    """Extract owner/repository from a GitHub origin URL."""
    match = re.search(r"github\.com[/:]([^/]+)/([^/]+?)(?:\.git)?$", origin_url)
    if not match:
        raise ValueError("origin is not a recognizable GitHub repository URL")
    return f"{match.group(1)}/{match.group(2)}"


def get_readiness_errors(pr, head_sha):
    """Return reasons a pull request must not be marked ready."""
    errors = []
    if not pr.get("isDraft"):
        errors.append("pull request is already ready for review")
    if pr.get("headRefOid") != head_sha:
        errors.append("local HEAD does not match the pull request head")
    if pr.get("mergeable") != "MERGEABLE":
        errors.append("pull request is not mergeable")
    if pr.get("mergeStateStatus") != "CLEAN":
        errors.append(f"merge state is {pr.get('mergeStateStatus') or 'unknown'}, not CLEAN")
    checks = pr.get("statusCheckRollup") or []
    if not checks:
        errors.append("pull request has no reported checks")
    for check in checks:
        name = check.get("name") or check.get("context") or "unnamed check"
        status = str(check.get("status") or "").upper()
        conclusion = str(check.get("conclusion") or "").upper()
        if status != "COMPLETED" or conclusion != "SUCCESS":
            errors.append(f"check {name!r} has not completed successfully")
    return errors


def run(command, cwd=REPO_DIR):
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", help="GitHub owner/repository; defaults to origin")
    args = parser.parse_args(argv)

    dirty = run(["git", "status", "--porcelain", "--untracked-files=no"])
    if dirty.returncode or dirty.stdout.strip():
        print("Refusing: tracked worktree changes are present.", file=sys.stderr)
        return 1

    with tempfile.TemporaryDirectory(prefix="alexandria-ready-") as tmp:
        verifier = subprocess.run(
            [sys.executable, "verify_release.py", "--json-report", str(Path(tmp) / "report.json")],
            cwd=APP_DIR,
        )
        if verifier.returncode:
            print("Refusing: local release verification failed.", file=sys.stderr)
            return 1

    head = run(["git", "rev-parse", "HEAD"])
    branch = run(["git", "branch", "--show-current"])
    origin = run(["git", "remote", "get-url", "origin"])
    if head.returncode or branch.returncode or not branch.stdout.strip() or origin.returncode:
        print("Refusing: could not resolve Git state.", file=sys.stderr)
        return 1
    try:
        repository = args.repo or get_origin_repo(origin.stdout.strip())
    except ValueError as exc:
        print(f"Refusing: {exc}.", file=sys.stderr)
        return 1

    view = run([
        "gh", "pr", "view", branch.stdout.strip(), "--repo", repository, "--json",
        "url,isDraft,headRefOid,mergeable,mergeStateStatus",
    ])
    if view.returncode:
        print(f"Refusing: could not inspect pull request: {view.stderr.strip()}", file=sys.stderr)
        return 1
    pr = json.loads(view.stdout)
    checks = run([
        "gh", "api", f"repos/{repository}/commits/{pr['headRefOid']}/check-runs",
        "--jq", ".check_runs",
    ])
    if checks.returncode:
        print(f"Refusing: could not inspect head checks: {checks.stderr.strip()}", file=sys.stderr)
        return 1
    pr["statusCheckRollup"] = json.loads(checks.stdout)
    errors = get_readiness_errors(pr, head.stdout.strip())
    if errors:
        for error in errors:
            print(f"Refusing: {error}.", file=sys.stderr)
        return 1

    ready = run(["gh", "pr", "ready", pr["url"], "--repo", repository])
    if ready.returncode:
        print(f"Could not mark pull request ready: {ready.stderr.strip()}", file=sys.stderr)
        return 1
    print(ready.stdout.strip() or f"Marked {pr['url']} ready for review.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
