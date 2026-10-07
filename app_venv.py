"""Standard-library application virtualenv executable selection."""
import os
import sys


def get_app_python(repo):
    """Return the application venv executable for the current platform."""
    if sys.platform == "win32":
        return os.path.join(repo, "app", "env", "Scripts", "python.exe")
    return os.path.join(repo, "app", "env", "bin", "python")
