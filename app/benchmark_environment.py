"""Verified local and Thunder observations for benchmark fingerprints."""

import json
import hashlib
import os
import platform
import shlex
import subprocess
import sys
import time
from pathlib import Path

from benchmark_core import build_environment_fingerprint
from lmstudio_settings import (_ssh_run, get_gpu_name_and_backend,
                               get_lmstudio_status,
                               get_remote_gpu_name_and_backend,
                               get_remote_lmstudio_status)
from runtime_info import get_runtime_info
from utils import atomic_json_write, safe_load_json

BASELINE_STALE_SECONDS = 24 * 60 * 60


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
    if not status.get("loaded"):
        raise ValueError(f"LM Studio model is not loaded: {model_name}")
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


def _verify_remote_checkout(root_dir, ssh_alias, remote_root):
    """Raise if remote_root's git checkout isn't clean and at the exact
    local HEAD commit. Needed by anything that runs code ON the remote host
    - collect_thunder_tts_environment already has its own inline version of
    this check (combined with a torch/qwen_tts probe in one SSH round trip);
    this is the standalone version for callers that don't need that probe,
    e.g. collect_thunder_environment now that LLM-stage benchmarks can also
    run their worker on the remote host (see llm_benchmark_worker.py)."""
    command = (f"git -C {shlex.quote(remote_root)} rev-parse HEAD && "
               f"git -C {shlex.quote(remote_root)} status --porcelain=v1 -- app")
    result = _ssh_run(ssh_alias, command, timeout=20, connect_timeout=10)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode or not lines:
        raise ValueError(result.stderr.strip() or "remote checkout probe failed")
    commit_index = next((index for index, line in enumerate(lines)
                         if len(line) == 40 and all(c in "0123456789abcdef" for c in line)), None)
    if commit_index is None:
        raise ValueError("remote git revision is unavailable")
    dirty_lines = lines[commit_index + 1:]
    runtime = get_runtime_info(root_dir)
    if lines[commit_index] != runtime["revision"] or dirty_lines:
        raise ValueError("remote checkout must be clean and match the local git revision")
    return lines[commit_index]


def collect_thunder_environment(root_dir, ssh_alias, model_name, remote_root=None):
    """Fingerprint local orchestration plus the Thunder inference host.

    remote_root is optional and only relevant to stages that dispatch a
    worker script onto the remote host (script_generation, script_review,
    persona_generation, nickname_detection via llm_benchmark_worker.py) -
    when given, verifies that checkout is clean and matches local HEAD
    before returning, so a stale/dirty remote checkout fails the preflight
    instead of silently running old code (see PR #186's version-endpoint
    gap for why this class of bug is worth guarding against directly).
    """
    if not ssh_alias:
        raise ValueError("Thunder SSH alias is required")
    remote = _get_remote_runtime_observations(ssh_alias)
    runtime = get_runtime_info(root_dir)
    if not runtime.get("revision"):
        raise ValueError("local git revision could not be identified")
    gpu_name, backend = get_remote_gpu_name_and_backend(ssh_alias)
    if not gpu_name or not backend:
        raise ValueError("Thunder GPU/backend could not be identified")
    remote_commit = (_verify_remote_checkout(root_dir, ssh_alias, remote_root)
                     if remote_root else None)
    observations = {"hostname": remote["hostname"], "gpu_name": gpu_name,
                    "backend": backend, "python_version": runtime["python"],
                    "git_commit": runtime["revision"],
                    "worktree": _get_local_worktree_identity(root_dir),
                    "orchestrator_platform": runtime["platform"],
                    "packages": runtime["packages"], "remote_platform": remote["platform"],
                    "lmstudio": _get_lmstudio_observations(
                        get_remote_lmstudio_status(ssh_alias, model_name), model_name)}
    if remote_commit is not None:
        observations["remote_checkout_commit"] = remote_commit
    return build_environment_fingerprint("thunder", observations)


def collect_local_tts_environment(root_dir, python_executable=None):
    """Fingerprint the Python environment that will execute local TTS."""
    runtime = get_runtime_info(root_dir)
    gpu_name, backend = get_gpu_name_and_backend()
    python_executable = python_executable or sys.executable
    probe = subprocess.run([python_executable, "-c",
                            "import json,platform,torch,qwen_tts; print(json.dumps({'python':platform.python_version(),'torch':torch.__version__,'qwen_tts':getattr(qwen_tts,'__version__','unknown')}))"],
                           capture_output=True, text=True, timeout=20, check=False)
    if probe.returncode or not gpu_name or not backend:
        raise ValueError(probe.stderr.strip() or "local TTS environment is unavailable")
    packages = json.loads(probe.stdout.splitlines()[-1])
    return build_environment_fingerprint("local", {
        "hostname": platform.node(), "gpu_name": gpu_name, "backend": backend,
        "python_version": packages.pop("python"), "git_commit": runtime["revision"],
        "worktree": _get_local_worktree_identity(root_dir), "packages": packages,
        "python_executable": python_executable})


def collect_thunder_tts_environment(root_dir, ssh_alias, remote_root, remote_python):
    """Fingerprint the exact remote checkout and Python used by the TTS worker."""
    if not ssh_alias or not remote_root or not remote_python:
        raise ValueError("Thunder TTS preflight requires SSH alias, remote_root, and remote_python")
    code = ("import json,platform,torch,qwen_tts; print(json.dumps({"
            "'hostname':platform.node(),'python_version':platform.python_version(),"
            "'torch':torch.__version__,'qwen_tts':getattr(qwen_tts,'__version__','unknown')}))")
    command = (f"{shlex.quote(remote_python)} -c {shlex.quote(code)} && "
               f"git -C {shlex.quote(remote_root)} rev-parse HEAD && "
               f"git -C {shlex.quote(remote_root)} status --porcelain=v1 -- app")
    result = _ssh_run(ssh_alias, command, timeout=30, connect_timeout=10)
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if result.returncode or len(lines) < 2:
        raise ValueError(result.stderr.strip() or "remote TTS environment probe failed")
    json_line = next((line for line in lines if line.startswith("{")), None)
    if not json_line:
        raise ValueError("remote TTS Python probe returned invalid JSON")
    details = json.loads(json_line)
    commit_index = next((index for index, line in enumerate(lines)
                         if len(line) == 40 and all(c in "0123456789abcdef" for c in line)), None)
    if commit_index is None:
        raise ValueError("remote TTS git revision is unavailable")
    dirty_lines = lines[commit_index + 1:]
    gpu_name, backend = get_remote_gpu_name_and_backend(ssh_alias)
    if not gpu_name or not backend:
        raise ValueError("Thunder GPU/backend could not be identified")
    runtime = get_runtime_info(root_dir)
    if lines[commit_index] != runtime["revision"] or dirty_lines:
        raise ValueError("remote TTS checkout must be clean and match the local git revision")
    observations = {"hostname": details["hostname"], "gpu_name": gpu_name,
                    "backend": backend, "python_version": details["python_version"],
                    "git_commit": lines[commit_index], "remote_worktree_dirty": bool(dirty_lines),
                    "orchestrator_git_commit": runtime["revision"],
                    "orchestrator_worktree": _get_local_worktree_identity(root_dir),
                    "packages": {"torch": details["torch"], "qwen_tts": details["qwen_tts"]},
                    "python_executable": remote_python, "remote_root": remote_root}
    return build_environment_fingerprint("thunder", observations)


def _torch_major_minor(version_string):
    """Return "MAJOR.MINOR" from a Torch version string, stripping any
    build suffix (e.g. "2.10.0+rocm7.0" -> "2.10"), or None if unparseable."""
    if not version_string:
        return None
    parts = str(version_string).split("+")[0].split(".")
    if len(parts) < 2:
        return None
    return f"{parts[0]}.{parts[1]}"


def verify_comparable_environments(local_env, thunder_env):
    """Raise if local and Thunder fingerprints ran different Torch builds.

    Comparative TTS/LoRA/VoiceLab timings are only meaningful if both sides
    ran the same Torch minor version; a ROCm vs. CUDA build mismatch (seen in
    the 2026-07-18 campaign: local Torch 2.10.0+rocm7.0 vs. Thunder Torch
    2.7.0+cu126) silently turns a hardware comparison into a software-stack
    comparison instead. Fingerprints that don't carry a Torch version (the
    LLM-only stages, which don't run this app's Torch in-process) are skipped.
    """
    local_torch = (local_env.get("details", {}).get("packages", {}) or {}).get("torch")
    thunder_torch = (thunder_env.get("details", {}).get("packages", {}) or {}).get("torch")
    if not local_torch or not thunder_torch:
        return
    if _torch_major_minor(local_torch) != _torch_major_minor(thunder_torch):
        raise ValueError(
            f"local Torch {local_torch} and Thunder Torch {thunder_torch} are "
            "different builds; a comparative benchmark would measure the "
            "software stack, not the GPU. Align both environments before running.")


def save_environment_baseline(target, environment, path):
    """Persist the most recently collected fingerprint for `target`.

    A later single-target preflight for the OTHER target can then check
    comparability against this instead of only against a sibling collected
    in the same call - real callers never request both targets in one
    preflight, so that same-call check never actually fired in practice.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    baselines = safe_load_json(path, default={}) or {}
    baselines[target] = {"environment": environment, "collected_at": time.time()}
    atomic_json_write(baselines, path)


def load_environment_baseline(target, path):
    """Return {"environment": ..., "collected_at": ...} for target, or None
    if nothing has been saved yet (first run ever, or a CPU/LLM-only stage
    that never collects a Torch-carrying fingerprint)."""
    baselines = safe_load_json(path, default={}) or {}
    return baselines.get(target)


def is_baseline_stale(baseline_entry, now=None):
    """True if a baseline is old enough that it might not reflect the
    machine's current state (e.g. a Torch upgrade since it was collected)."""
    now = now if now is not None else time.time()
    return (now - baseline_entry.get("collected_at", 0)) > BASELINE_STALE_SECONDS


def collect_cpu_environment(root_dir, target, ssh_alias=None):
    """Fingerprint a standard-library-only local or remote benchmark runtime."""
    runtime = get_runtime_info(root_dir)
    if target == "local":
        hostname = platform.node()
        python_version = platform.python_version()
        platform_details = runtime["platform"]
    elif target == "thunder":
        if not ssh_alias:
            raise ValueError("Thunder SSH alias is required")
        remote = _get_remote_runtime_observations(ssh_alias)
        hostname = remote["hostname"]
        python_version = remote["python_version"]
        platform_details = remote["platform"]
    else:
        raise ValueError("CPU benchmark target must be local or thunder")
    return build_environment_fingerprint(target, {
        "hostname": hostname, "gpu_name": "none", "backend": "cpu",
        "python_version": python_version, "git_commit": runtime["revision"],
        "worktree": _get_local_worktree_identity(root_dir),
        "platform": platform_details})
