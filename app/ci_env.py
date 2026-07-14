"""Make a local run see the same imports CI sees.

CI installs `requirements.txt` minus transformers/peft, and torch is not in
requirements.txt at all (production gets it from torch.js, which install.js runs
via the "ai" bundle). A dev machine therefore has torch/transformers/peft while
CI does not, so a test that touches one of them passes locally and fails in CI.

Blocking those imports here lets verify_release.py predict CI instead of just
re-testing the developer's own machine.

Keep BLOCKED_MODULES in step with .github/workflows/tests.yml — the drift test
in test_release_verifier.py fails if they disagree.
"""

import sys

# torch: never in requirements.txt (torch.js ships it in production).
# transformers/peft: explicitly filtered out by the CI workflow's pip install.
BLOCKED_MODULES = ("torch", "transformers", "peft")


class _BlockedImportFinder:
    """Raise ImportError for BLOCKED_MODULES and anything under them."""

    def __init__(self, blocked):
        self._blocked = tuple(blocked)

    def find_spec(self, fullname, path=None, target=None):
        root = fullname.split(".", 1)[0]
        if root in self._blocked:
            raise ImportError(
                f"No module named {fullname!r} (blocked: CI installs without "
                f"{', '.join(self._blocked)})")
        return None  # not ours; let the normal finders handle it


def block_ml_imports(blocked=BLOCKED_MODULES):
    """Install the finder. Also drop already-imported modules so a module
    imported before this call cannot mask the block."""
    for name in list(sys.modules):
        if name.split(".", 1)[0] in blocked:
            del sys.modules[name]
    sys.meta_path.insert(0, _BlockedImportFinder(blocked))


def main(argv=None):
    """Run unittest with the ML libraries blocked.

    Usage: python -m ci_env discover -s . -p "test_*.py"
    """
    import unittest

    argv = list(sys.argv[1:] if argv is None else argv)
    block_ml_imports()
    runner = unittest.main(module=None, argv=["python -m ci_env"] + argv, exit=False)
    return 0 if runner.result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
