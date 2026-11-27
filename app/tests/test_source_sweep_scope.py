"""A sweep over app/*.py must not walk into the virtualenv.

`app/env` is a venv INSIDE the directory these sweeps walk. CI and worktrees
have no venv, so a bare `rglob("*.py")` passes there and fails only in the live
checkout - the one tree the GPU queue runs from, and therefore the tree where
being unable to run the suite costs the most. On 2026-08-21
`test_speech_fact_preferred` reported `cookies.py`, `cparser.py` and
`_dotenv.py` as modules deciding speech from punctuation.

Two other sweeps already excluded the venv, each with its own hand-rolled
spelling. That is the drift Rule 15 describes: one question, three answers, and
the third was wrong. `app_python_files` is now the only answer.

The inventory below covers every app/ module plus every repo-root script (the
same files the Bandit gate scans); tools/ and app/experiments/ are excluded
from that gate and from this rule.
"""
import pathlib
import re
import tempfile
import unittest

from tests.test_support import app_python_files
import source_sweep

APP_DIR = pathlib.Path(__file__).resolve().parent.parent
BARE_RGLOB = re.compile(r'rglob\(\s*["\']\*\.py["\']\s*\)')


def inventory_candidates(repo_root):
    """Every app/ module plus every repo-root script, as the rule walks them."""
    return sorted(set(app_python_files(repo_root / "app"))
                  | set(repo_root.glob("*.py")))


class AppPythonFilesTest(unittest.TestCase):
    def _tree(self):
        root = pathlib.Path(tempfile.mkdtemp())
        (root / "tts.py").write_text("ours\n", encoding="utf-8")
        venv = root / "env"
        (venv / "lib" / "python3.10" / "site-packages" / "requests").mkdir(
            parents=True)
        (venv / "pyvenv.cfg").write_text("home = /usr\n", encoding="utf-8")
        (venv / "lib" / "python3.10" / "site-packages" / "requests"
         / "cookies.py").write_text("theirs\n", encoding="utf-8")
        # A venv module that is NOT under site-packages: the marker file is
        # what makes it excludable, not the directory name.
        (venv / "lib" / "python3.10" / "sitecustomize.py").write_text(
            "theirs\n", encoding="utf-8")
        return root

    def test_the_venv_is_excluded_and_our_modules_are_not(self):
        root = self._tree()
        names = {p.name for p in app_python_files(root)}
        self.assertEqual({"tts.py"}, names)

    def test_a_bare_rglob_would_have_returned_the_venv(self):
        """The fixture must keep discriminating: prove the old walk fails it."""
        root = self._tree()
        names = {p.name for p in root.rglob("*.py")}
        self.assertIn("cookies.py", names)
        self.assertIn("sitecustomize.py", names)

    def test_a_tree_with_no_venv_is_unaffected(self):
        root = pathlib.Path(tempfile.mkdtemp())
        (root / "a.py").write_text("x\n", encoding="utf-8")
        (root / "sub").mkdir()
        (root / "sub" / "b.py").write_text("x\n", encoding="utf-8")
        self.assertEqual({"a.py", "b.py"},
                         {p.name for p in app_python_files(root)})


class SweepScopeInventoryTest(unittest.TestCase):
    """No production module or test may walk app/ without the shared helper."""

    ALLOWED = {
        # Walks a temp fixture, and asserts the bare form still fails it.
        "app/tests/test_source_sweep_scope.py",
        # Defines the helper; its rglob is the one this rule points everyone at.
        "app/source_sweep.py",
    }

    def test_test_support_delegates_to_shared_helper(self):
        from unittest import mock
        sentinel = [pathlib.Path("sentinel.py")]
        with mock.patch.object(source_sweep, "app_python_files",
                               return_value=sentinel) as shared:
            self.assertEqual(sentinel, app_python_files(APP_DIR))
        shared.assert_called_once_with(APP_DIR)

    def test_inventory_includes_repo_root_scripts(self):
        root = pathlib.Path(tempfile.mkdtemp())
        (root / "app").mkdir()
        (root / "app" / "module.py").write_text("x = 1\n", encoding="utf-8")
        (root / "script.py").write_text(
            'X = list(Path(".").rglob("*.py"))\n', encoding="utf-8")
        self.assertEqual(
            {"module.py", "script.py"},
            {path.name for path in inventory_candidates(root)})

    def test_no_module_rglobs_python_files_by_hand(self):
        repo_root = APP_DIR.parent
        candidates = inventory_candidates(repo_root)
        offenders = []
        for path in candidates:
            rel = path.relative_to(repo_root).as_posix()
            if rel in self.ALLOWED:
                continue
            source = path.read_text(encoding="utf-8", errors="replace")
            if BARE_RGLOB.search(source):
                offenders.append(rel)
        self.assertEqual(
            [], offenders,
            "these walk for .py by hand and will pick up app/env in the live "
            "checkout; call source_sweep.app_python_files instead")
