"""Canonical sweep for application Python modules under ``app/``.

``app/env`` is a virtualenv inside the directory these sweeps walk, so a bare
``rglob("*.py")`` returns tens of thousands of third-party site-packages
modules. CI and fresh worktrees have no venv, so a bare sweep passes there and
fails only in the live checkout — the tree the GPU queue runs from.

This module is the single source of truth used by both runtime tooling
(``app/bandit_gate.py``) and test sweeps (``app/tests/test_support.py``).
"""

from pathlib import Path


def _venv_roots(app_dir):
    """Directories under ``app_dir`` that are virtualenvs (hold ``pyvenv.cfg``)."""
    return {path.parent for path in app_dir.rglob("pyvenv.cfg")}


def app_python_files(app_dir):
    """Return our own ``.py`` files under ``app_dir``, never a virtualenv's."""
    app_dir = Path(app_dir)
    roots = _venv_roots(app_dir)
    files = []
    for path in sorted(app_dir.rglob("*.py")):
        if "site-packages" in path.parts or "dist-packages" in path.parts:
            continue
        if any(root in path.parents for root in roots):
            continue
        files.append(path)
    return files
