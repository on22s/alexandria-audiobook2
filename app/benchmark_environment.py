"""Verified local and Thunder observations for benchmark fingerprints."""

import json
import hashlib
import platform
import shlex
import subprocess
from pathlib import Path

from benchmark_core import build_environment_fingerprint
from lmstudio_settings import (_ssh_run, get_gpu_name_and_backend,
                               get_lmstudio_status,
                               get_remote_gpu_name_and_backend,
                               get_remote_lmstudio_status)
from runtime_info import get_runtime_info


def _get_local_worktree_identity(root_dir):
    """Fingerprint tracked edits and untracked app source used by a run."""
    status = subprocess.run(
        ["git", "-C", str(root_dir), "status", "--porcelain=v1", "--", "app"],
        capture_output=True, text=True, timeout=20, check=False)
    diff = subprocess.run(
        ["git", "-C", str(root_dir), "diff", "--binary", "HEAD", "--", "app"],
        capture_output=True, timeout=20, check=False)
    untracked = subprocess.run(
        ["git", "-C", str(root_dir), "ls-files", "--others", "--exclude-standard", "--", "app"],
        capture_output=True, text=True, timeout=20, check=False)
    if status.returncode or diff.returncode or untracked.returncode:
        raise ValueError("local git worktree could not be identified")
    digest = hashlib.sha256()
    digest.update(status.stdout.encode("utf-8"))
    digest.update(diff.stdout)
    for relative_path in sorted(line for line in untracked.stdout.splitlines() if line):
        path = Path(root_dir, relative_path)
        if not path.is_file():
            continue
        digest.update(relative_path.encode("utf-8"))
        digest.update(path.read_bytes())
    return {"dirty": bool(status.stdout.strip()), "sha256": digest.hexdigest()}


def _get_lmstudio_observations(status, model_name):
    if not status.get("available"):
        raise ValueError("LM Studio status is unavailable")
    return {"model_name": model_name, "model_loaded": status.get("loaded", False),
            "context_length": status.get("context_length"),
            "parallel": status.get("parallel")}


def collect_local_environment(root_dir, model_name):
    """Collect the local cohort identity without changing model settings."""
    runtime = get_runtime_info(root_dir)
    gpu_name, backend = get_gpu_name_and_backend()
    if not gpu_name or not backend:
        raise ValueError("local GPU/backend could not be identified")
    if not runtime.get("revision"):
        raise ValueError("local git revision could not be identified")
    observations = {
        "hostname": platform.node(), "gpu_name": gpu_name, "backend": backend,
        "python_version": runtime["python"], "git_commit": runtime["revision"],
        "worktree": _get_local_worktree_identity(root_dir),
        "platform": runtime["platform"], "packages": runtime["packages"],
        "lmstudio": _get_lmstudio_observations(
            get_lmstudio_status(model_name), model_name),
    }
    return build_environment_fingerprint("local", observations)


def _get_remote_runtime_observations(ssh_alias):
    """Read one JSON line from the inference host after any login banner."""
    probe = (
        "print(json.dumps({'hostname':platform.node(),'python_version':platform.python_version(),"
        "'platform':{'system':platform.system(),'release':platform.release(),'machine':platform.machine()}}))"
    )
    probe = "import json,platform; " + probe
    command = f"python3 -c {shlex.quote(probe)}"
    result = _ssh_run(ssh_alias, command, timeout=20, connect_timeout=10)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode or not lines:
        raise ValueError(result.stderr.strip() or "remote runtime probe failed")
    try:
        observations = json.loads(lines[-1])
    except json.JSONDecodeError as exc:
        raise ValueError("remote runtime probe returned invalid JSON") from exc
    return observations


def collect_thunder_environment(root_dir, ssh_alias, model_name):
    """Fingerprint local orchestration plus the Thunder inference host."""
    if not ssh_alias:
        raise ValueError("Thunder SSH alias is required")
    remote = _get_remote_runtime_observations(ssh_alias)
    runtime = get_runtime_info(root_dir)
    if not runtime.get("revision"):
        raise ValueError("local git revision could not be identified")
    gpu_name, backend = get_remote_gpu_name_and_backend(ssh_alias)
    if not gpu_name or not backend:
        raise ValueError("Thunder GPU/backend could not be identified")
    observations = {"hostname": remote["hostname"], "gpu_name": gpu_name,
                    "backend": backend, "python_version": runtime["python"],
                    "git_commit": runtime["revision"],
                    "worktree": _get_local_worktree_identity(root_dir),
                    "orchestrator_platform": runtime["platform"],
                    "packages": runtime["packages"], "remote_platform": remote["platform"],
                    "lmstudio": _get_lmstudio_observations(
                        get_remote_lmstudio_status(ssh_alias, model_name), model_name)}
    return build_environment_fingerprint("thunder", observations)
