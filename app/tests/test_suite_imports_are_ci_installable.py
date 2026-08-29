"""No test module may import, at module scope, a package CI declines to install.

`verify_release.py` runs `update_test_inventory.py`, which imports EVERY test
module by name. So one `import pytest` at module scope does not fail one test -
it aborts the inventory step and the whole verifier with ModuleNotFoundError,
before a single test runs.

That is what happened on 2026-08-29: `test_attribution_hybrid.py` and
`test_ctc_text_boundary.py` were written with pytest, passed locally in an
`app/env` that happens to have it, and felled CI at the inventory step.

Three modules already carried a comment explaining the rule. A comment is not a
check, and it had also gone stale - `test_third_party_findings.py` still says
"neither pytest nor openai", but `openai==2.16.0` has since been added to
requirements.txt and IS installed in CI.

SCOPE, stated honestly. This checks the packages the repository EXPLICITLY
declares unavailable: the two the workflow greps out of its install line, and
the ones requirements-test.txt carries commented out. Both are parsed from
those files, so neither can rot the way the comment did. It deliberately does
NOT try to decide availability in general - `httpx` and `starlette` reach CI as
transitive dependencies of fastapi without appearing in any requirements file,
so a whitelist derived from requirements.txt alone reports eight false
positives. Catching a novel uninstalled package is left to CI itself; this
catches the one that has actually happened, and pins it.
"""
import ast
import os
import re
import tempfile
import unittest

TESTS = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(TESTS))

IMPORT_NAME = {"llama-cpp-python": "llama_cpp", "pytest-asyncio": "pytest_asyncio"}


def declared_unavailable():
    """Packages this repository states CI does not install, read from source."""
    names = set()

    # 1. The workflow's own exclusion: grep -vE '^(transformers|peft)==' ...
    workflows = os.path.join(REPO, ".github", "workflows")
    for entry in sorted(os.listdir(workflows)):
        with open(os.path.join(workflows, entry), encoding="utf-8") as handle:
            text = handle.read()
        for group in re.findall(r"grep -vE\s+'\^\(([^)]+)\)", text):
            names.update(part.strip() for part in group.split("|") if part.strip())

    # 2. requirements-test.txt's commented-out entries, e.g. "# pytest>=7.0.0".
    path = os.path.join(REPO, "requirements-test.txt")
    with open(path, encoding="utf-8") as handle:
        for line in handle:
            match = re.match(r"#\s*([A-Za-z][A-Za-z0-9._-]+)\s*[=<>~]", line.strip())
            if match:
                dist = match.group(1).lower()
                names.add(IMPORT_NAME.get(dist, dist.replace("-", "_")))
    return names


def module_level_imports(path):
    """Return root package names imported at module scope in one file."""
    with open(path, encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=path)
    names = set()
    for node in tree.body:                     # module scope only, by design
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            names.add(node.module.split(".")[0])
    return names


class SuiteImportsAreCIInstallableTest(unittest.TestCase):

    def test_no_test_module_imports_a_declared_unavailable_package(self):
        unavailable, offenders = declared_unavailable(), []
        for name in sorted(os.listdir(TESTS)):
            if not (name.startswith("test_") and name.endswith(".py")):
                continue
            for package in sorted(module_level_imports(
                    os.path.join(TESTS, name)) & unavailable):
                offenders.append(f"{name}: import {package}")
        self.assertEqual(
            [], offenders,
            "these module-scope imports abort update_test_inventory in CI, "
            "which imports every test module by name. Move the import inside "
            "the test and skip when it is missing:\n  " + "\n  ".join(offenders))

    def test_the_set_is_read_from_the_files_that_define_it(self):
        """Guards the derivation, so a changed workflow changes the check."""
        unavailable = declared_unavailable()
        self.assertIn("pytest", unavailable)        # commented out in reqs-test
        self.assertIn("transformers", unavailable)  # greped out by the workflow
        self.assertIn("peft", unavailable)          # greped out by the workflow
        self.assertNotIn("soundfile", unavailable)  # installed; 8 modules use it
        self.assertNotIn("openai", unavailable)     # what the stale comment got wrong

    def test_the_detector_fires_on_the_import_that_broke_ci(self):
        """The 2026-08-29 failure, kept so this cannot stop discriminating."""
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test_regression.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("import math\n\nimport pytest\n\n"
                             "def test_x():\n    assert pytest\n")
            self.assertEqual(
                {"pytest"},
                module_level_imports(path) & declared_unavailable())

    def test_an_import_inside_a_test_is_allowed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "test_lazy.py")
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("import unittest\n\n\nclass T(unittest.TestCase):\n"
                             "    def test_x(self):\n"
                             "        import transformers\n"
                             "        self.assertTrue(transformers)\n")
            self.assertEqual(
                set(),
                module_level_imports(path) & declared_unavailable())


if __name__ == "__main__":
    unittest.main()
