"""Print targeted snapshot-maintenance hints for pull-request changes."""

import argparse
import subprocess
from pathlib import Path


def get_changed_files(repo_dir, base):
    """Return repository-relative files changed from base to HEAD."""
    result = subprocess.run(
        ["git", "diff", "--name-only", f"{base}...HEAD"], cwd=repo_dir,
        capture_output=True, text=True,
    )
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or "could not inspect pull-request changes")
    return set(result.stdout.splitlines())


def get_snapshot_hints(changed_files):
    """Return advisory messages for likely omitted generated files."""
    hints = []
    test_sources_changed = any(
        path.startswith("app/test") and path.endswith(".py")
        for path in changed_files
    )
    if test_sources_changed and "app/unit_test_inventory.json" not in changed_files:
        hints.append(
            "Tests changed without unit_test_inventory.json; run "
            "`python app/update_test_inventory.py` if discovery changed."
        )
    schema_sources_changed = any(
        path == "app/app.py" or path.startswith("app/routers/")
        for path in changed_files
    )
    if schema_sources_changed and "app/api_contract/openapi.json" not in changed_files:
        hints.append(
            "API route code changed without the OpenAPI snapshot; run "
            "`python app/update_api_contract_snapshots.py` if the schema changed."
        )
    return hints


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True, help="base commit SHA")
    args = parser.parse_args(argv)
    repo_dir = Path(__file__).resolve().parent.parent
    for hint in get_snapshot_hints(get_changed_files(repo_dir, args.base)):
        print(f"::notice::{hint}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
