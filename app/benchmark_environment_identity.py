"""Standard-library-only build identities for benchmark worker probes."""
from concurrent.futures import ThreadPoolExecutor

import hashlib
import platform
import subprocess
from pathlib import Path
from runtime_info import get_runtime_info

def _get_local_worktree_identity(root_dir):
    """Fingerprint tracked edits and untracked app source used by a run."""
    with ThreadPoolExecutor(max_workers=3) as probes:
        status_probe = probes.submit(subprocess.run,
            ["git", "-C", str(root_dir), "status", "--porcelain=v1", "--", "app"],
            capture_output=True, text=True, timeout=20, check=False)
        diff_probe = probes.submit(subprocess.run,
            ["git", "-C", str(root_dir), "diff", "--binary", "HEAD", "--", "app"],
            capture_output=True, timeout=20, check=False)
        untracked_probe = probes.submit(subprocess.run,
            ["git", "-C", str(root_dir), "ls-files", "--others", "--exclude-standard", "--", "app"],
            capture_output=True, text=True, timeout=20, check=False)
        status = status_probe.result()
        diff = diff_probe.result()
        untracked = untracked_probe.result()
    if status.returncode or diff.returncode or untracked.returncode:
        raise ValueError("local git worktree could not be identified")
    digest = hashlib.sha256()
    def add_field(value):
        digest.update(len(value).to_bytes(8, "big"))
        digest.update(value)

    add_field(status.stdout.encode("utf-8"))
    add_field(diff.stdout)
    for relative_path in sorted(line for line in untracked.stdout.splitlines() if line):
        path = Path(root_dir, relative_path)
        if not path.is_file():
            continue
        add_field(relative_path.encode("utf-8"))
        add_field(path.read_bytes())
    return {"dirty": bool(status.stdout.strip()), "sha256": digest.hexdigest()}


def get_benchmark_runtime_observations(root_dir):
    """Observe this interpreter and actual Git checkout without importing ML."""
    runtime = get_runtime_info(str(root_dir))
    revision = subprocess.run(["git", "-C", str(root_dir), "rev-parse", "HEAD"],
        capture_output=True, text=True, timeout=20, check=False)
    if revision.returncode:
        raise ValueError("benchmark worker git revision could not be identified")
    return {"hostname": platform.node(), "python_version": runtime["python"],
            "platform": runtime["platform"], "packages": runtime["packages"],
            "git_commit": revision.stdout.strip(),
            "worktree": _get_local_worktree_identity(root_dir)}
