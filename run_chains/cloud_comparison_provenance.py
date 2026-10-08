"""Capture immutable model identity before the comparison loaders consume it."""
from importlib import metadata
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "app"))
from subprocess_ownership import run_owned_capture

GIT_PROBE_TIMEOUT_SECONDS = 20


def ensure_comparison_model_snapshot(repo_id, revision=None, allow_patterns=None):
    from huggingface_hub import HfApi, snapshot_download
    requested = revision or "main"
    if re.fullmatch(r"[0-9a-f]{40}", requested):
        commit = requested
    else:
        commit = HfApi().model_info(repo_id, revision=requested, token=os.environ.get("HF_TOKEN")).sha
    if not isinstance(commit, str) or not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError(f"model revision did not resolve to an immutable commit: {repo_id}")
    path = Path(snapshot_download(repo_id=repo_id, repo_type="model", revision=commit,
                                  allow_patterns=allow_patterns, token=os.environ.get("HF_TOKEN")))
    if not path.is_dir() or path.name != commit or path.parent.name != "snapshots":
        raise ValueError(f"model snapshot does not match resolved revision: {path}")
    return {"repo_id": repo_id, "requested_revision": requested,
            "revision": commit, "snapshot_path": str(path)}


def get_comparison_package_versions(names, expected=None):
    versions = {name: metadata.version(name) for name in names}
    if any(not isinstance(version, str) or not version.strip() for version in versions.values()):
        raise ValueError("comparison package has no recorded version")
    for name, wanted in (expected or {}).items():
        if wanted is not None and versions.get(name) != wanted:
            raise ValueError(f"comparison requires {name}=={wanted}, installed {versions.get(name)}")
    return versions


def get_comparison_source_commit(source):
    result = run_owned_capture(["git", "-C", str(source), "rev-parse", "HEAD"],
                               text=True, timeout=GIT_PROBE_TIMEOUT_SECONDS)
    result.check_returncode()
    commit = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise ValueError("comparison source requires a committed revision")
    result = run_owned_capture(["git", "-C", str(source), "status", "--porcelain", "--untracked-files=all"],
                               text=True, timeout=GIT_PROBE_TIMEOUT_SECONDS)
    result.check_returncode()
    dirty = result.stdout
    if dirty:
        raise ValueError("comparison source has changes not represented by its commit")
    return commit
