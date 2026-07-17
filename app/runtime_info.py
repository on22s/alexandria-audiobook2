"""Lightweight runtime/build identity without importing ML packages."""

from functools import lru_cache
from importlib import metadata
import os
from pathlib import Path
import platform


RUNTIME_PACKAGES = ("torch", "qwen-tts", "transformers", "peft", "fastapi")


def _get_git_dir(root_dir: str) -> Path | None:
    dot_git = Path(root_dir, ".git")
    if dot_git.is_dir():
        return dot_git
    try:
        marker = dot_git.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not marker.startswith("gitdir:"):
        return None
    path = Path(marker[7:].strip())
    return path if path.is_absolute() else (dot_git.parent / path).resolve()


def _get_git_revision(root_dir: str) -> tuple[str | None, str | None]:
    git_dir = _get_git_dir(root_dir)
    if git_dir is None:
        return None, None
    try:
        head = Path(git_dir, "HEAD").read_text(encoding="utf-8").strip()
    except OSError:
        return None, None
    if not head.startswith("ref:"):
        return (head if len(head) >= 7 else None), None
    ref = head[4:].strip()
    branch = ref.rsplit("/", 1)[-1]
    ref_dirs = [git_dir]
    try:
        common_marker = Path(git_dir, "commondir").read_text(encoding="utf-8").strip()
        common_dir = Path(common_marker)
        ref_dirs.append(common_dir if common_dir.is_absolute()
                        else (git_dir / common_dir).resolve())
    except OSError:
        pass
    for ref_dir in ref_dirs:
        try:
            return Path(ref_dir, ref).read_text(encoding="utf-8").strip(), branch
        except OSError:
            pass
        try:
            lines = Path(ref_dir, "packed-refs").read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.startswith(("#", "^")) and line.endswith(f" {ref}"):
                return line.split(" ", 1)[0], branch
    return None, branch


@lru_cache(maxsize=4)
def get_runtime_info(root_dir: str) -> dict:
    configured_revision = os.environ.get("ALEXANDRIA_BUILD_COMMIT", "").strip()
    revision, branch = _get_git_revision(root_dir)
    if configured_revision:
        revision = configured_revision
        branch = None
        source = "environment"
    elif revision:
        source = "git"
    else:
        source = "unavailable"
    packages = {}
    for package in RUNTIME_PACKAGES:
        try:
            packages[package] = metadata.version(package)
        except metadata.PackageNotFoundError:
            packages[package] = None
    return {
        "revision": revision,
        "short_revision": revision[:8] if revision else None,
        "revision_source": source,
        "branch": branch,
        "python": platform.python_version(),
        "platform": {
            "system": platform.system(), "release": platform.release(),
            "machine": platform.machine(),
        },
        "packages": packages,
    }
