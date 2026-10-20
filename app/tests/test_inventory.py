import importlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
import unittest


INVENTORY_PATH = Path(__file__).with_name("unit_test_inventory.json")
EXCLUDED_TEST_MODULES = {"test_api"}  # Script-style live API suite, not unittest.


def get_test_directory(test_directory=None):
    """Return the discovery root, allowing private fixture trees."""
    return Path(test_directory) if test_directory is not None else Path(__file__).parent


def _tracked_test_modules(test_directory=None):
    """Module stems for test files git tracks, or None if git cannot answer.

    This working tree is shared between concurrent sessions on different
    branches, so an untracked test file belonging to someone else's branch sits
    beside ours. Discovering from the filesystem swept those into the checked-in
    inventory, and CI - which only has the committed files - then reported every
    one of them as "no longer discovered". That broke the build three times in
    one day. Only files under version control can be in a checked-in inventory.
    """
    try:
        result = subprocess.run(
            ["git", "ls-files", "-z", "--", "test*.py"],
            cwd=get_test_directory(test_directory), capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return {Path(name).stem
            for name in result.stdout.decode("utf-8").split("\0") if name}


def _iter_tests(suite):
    for test in suite:
        if isinstance(test, unittest.TestSuite):
            yield from _iter_tests(test)
        else:
            yield test


def get_unit_test_inventory(test_directory=None):
    """Return every discoverable unittest module and its stable test IDs."""
    directory = get_test_directory(test_directory)
    tracked = _tracked_test_modules(directory)
    modules = sorted(
        path.stem for path in directory.glob("test*.py")
        if path.stem not in EXCLUDED_TEST_MODULES
        # Fall back to the filesystem when git cannot answer (a source export,
        # a container without git) rather than reporting an empty inventory.
        and (tracked is None or path.stem in tracked)
    )
    inventory = {}
    for module_name in modules:
        module = importlib.import_module(f"{__package__}.{module_name}")
        suite = unittest.defaultTestLoader.loadTestsFromModule(module)
        inventory[module_name] = sorted(test.id() for test in _iter_tests(suite))
    return inventory


class TestInventoryTests(unittest.TestCase):
    def test_unit_test_inventory_is_stable(self):
        from update_test_inventory import get_inventory_differences

        expected = json.loads(INVENTORY_PATH.read_text(encoding="utf-8"))
        actual = get_unit_test_inventory()

        self.assertNotIn("test_api", actual)
        differences = get_inventory_differences(expected, actual)
        self.assertEqual([], differences, "Unit test inventory drift:\n" + "\n".join(differences))

    def test_inventory_differences_are_compact_and_actionable(self):
        from update_test_inventory import get_inventory_differences

        differences = get_inventory_differences(
            {"test_example": ["test_example.Example.test_removed"]},
            {"test_example": ["test_example.Example.test_added"]},
        )
        self.assertEqual([
            "Missing from inventory: test_example.Example.test_added",
            "No longer discovered: test_example.Example.test_removed",
        ], differences)

    def test_inventory_writer_is_deterministic_and_check_is_read_only(self):
        import tempfile
        from unittest.mock import patch
        import update_test_inventory as updater

        inventory = {"test_z": ["z"], "test_a": ["a"]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "inventory.json")
            with patch.object(updater, "get_unit_test_inventory", return_value=inventory):
                updater.write_inventory(path)
                first = path.read_bytes()
                self.assertLess(first.index(b'"test_a"'), first.index(b'"test_z"'))
                updater.write_inventory(path)
                self.assertEqual(first, path.read_bytes())
                self.assertEqual([], updater.check_inventory(path))
                self.assertEqual(first, path.read_bytes())


class InventoryFixtureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.directory = self.root / "tests"
        self.directory.mkdir()
        subprocess.run(["git", "init", "-q", str(self.root)], check=True)
        (self.directory / "test_fixture.py").write_text(
            "import unittest\nclass Fixture(unittest.TestCase):\n"
            "    def test_tracked(self):\n        pass\n", encoding="utf-8")
        (self.directory / "test_api.py").write_text("raise RuntimeError('excluded')\n", encoding="utf-8")
        subprocess.run(["git", "-C", str(self.root), "add", "tests/test_fixture.py", "tests/test_api.py"], check=True)

    def start_fixture_reader(self, directory=None, *, wait_file=None):
        directory = self.directory if directory is None else directory
        code = (
            "import json,os,sys,time\nfrom pathlib import Path\n"
            "import tests.test_inventory as inventory\nimport update_test_inventory as updater\n"
            "import tests\ntests.__path__=[sys.argv[1]]\nroot=Path(sys.argv[1])\n"
        )
        if wait_file is not None:
            code += "(root/'ready').touch()\nwhile not Path(sys.argv[2]).exists():time.sleep(0.01)\n"
        code += "print(json.dumps({'inventory':inventory.get_unit_test_inventory(root),'untracked':updater.find_untracked_test_modules(root)}))\n"
        environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
        environment["PYTHONPATH"] = str(Path(__file__).parent.parent)
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        return subprocess.Popen([sys.executable, "-c", code, str(directory), str(wait_file)],
                                cwd=self.root, env=environment, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True)

    def read_fixture(self, directory=None):
        child = self.start_fixture_reader(directory)
        return self.read_child(child)

    def read_child(self, child):
        try:
            stdout, stderr = child.communicate(timeout=10)
        finally:
            if child.poll() is None:
                child.kill()
                child.communicate()
        self.assertEqual(0, child.returncode, stderr)
        return json.loads(stdout)


class TrackedOnlyDiscoveryTest(InventoryFixtureTests):
    """The inventory is checked in, so it can only contain tracked files.

    This tree is shared between concurrent sessions on different branches. An
    untracked test file from another branch used to be swept into the inventory
    here and then reported missing by CI, which only sees committed files.
    """

    def test_an_untracked_test_file_is_not_inventoried(self):
        untracked = self.directory / "test_zz_untracked_probe.py"
        untracked.write_text(
            "import unittest\n\n\n"
            "class Probe(unittest.TestCase):\n"
            "    def test_probe(self):\n        pass\n",
            encoding="utf-8")
        self.assertNotIn("test_zz_untracked_probe", self.read_fixture()["inventory"])

    def test_tracked_files_are_still_discovered(self):
        self.assertEqual({"test_fixture": ["tests.test_fixture.Fixture.test_tracked"]},
                         self.read_fixture()["inventory"])

    def test_discovery_falls_back_when_git_is_unavailable(self):
        # A source export without git must still produce an inventory rather
        # than an empty one.
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp)
            for name in ("test_fixture.py", "test_api.py"):
                (directory / name).write_bytes((self.directory / name).read_bytes())
            self.assertEqual({"test_fixture": ["tests.test_fixture.Fixture.test_tracked"]},
                             self.read_fixture(directory)["inventory"])
            self.assertEqual([], self.read_fixture(directory)["untracked"])


class UntrackedTestWarningTest(InventoryFixtureTests):
    """Excluding untracked files from the inventory created an ordering trap.

    Write a new test file, regenerate the inventory, then git add and commit:
    the inventory was generated while the file was still untracked, so CI
    discovers tests it does not list and the build goes red. This happened on
    PR #233, one PR after the exclusion was introduced.
    """

    def test_a_new_untracked_test_file_is_reported(self):
        probe = self.directory / "test_zz_untracked_warning_probe.py"
        probe.write_text("import unittest\n", encoding="utf-8")
        self.assertEqual([probe.name], self.read_fixture()["untracked"])

    def test_a_tracked_file_is_not_reported(self):
        # Asserting the whole tree is clean would fail during normal work -
        # a new test file is untracked exactly when you are writing it, which
        # is when you run the suite most. Assert the behaviour instead.
        self.assertEqual([], self.read_fixture()["untracked"])


class InventoryFixtureIsolationTests(InventoryFixtureTests):
    def get_checkout_snapshot(self):
        directory = Path(__file__).parent
        index = subprocess.run(["git", "rev-parse", "--git-path", "index"],
                               cwd=directory, capture_output=True, text=True, check=True).stdout.strip()
        index = Path(index)
        if not index.is_absolute():
            index = directory / index
        return ({path.name: path.read_bytes() for path in directory.glob("test*.py")},
                index.read_bytes())

    def test_hard_exit_after_each_actual_probe_write_leaves_only_private_residue(self):
        before = self.get_checkout_snapshot()
        for class_name, method_name, probe_name in (
            ("TrackedOnlyDiscoveryTest", "test_an_untracked_test_file_is_not_inventoried", "test_zz_untracked_probe.py"),
            ("UntrackedTestWarningTest", "test_a_new_untracked_test_file_is_reported", "test_zz_untracked_warning_probe.py")):
            with self.subTest(class_name=class_name):
                state = self.root / "child-root"
                code = ("import os\nfrom pathlib import Path\nimport tests.test_inventory as module\n"
                        f"case=module.{class_name}({method_name!r})\ncase.setUp()\n"
                        f"Path({str(state)!r}).write_text(str(case.directory))\n"
                        "case.read_fixture=lambda:os._exit(23)\n"
                        f"case.{method_name}()\n")
                environment = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
                environment.update(PYTHONPATH=str(Path(__file__).parent.parent),
                                   PYTHONDONTWRITEBYTECODE="1", TMPDIR=str(self.root))
                result = subprocess.run([sys.executable, "-c", code], cwd=self.root, env=environment,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(23, result.returncode, result.stderr)
                directory = Path(state.read_text())
                self.assertTrue(directory.is_relative_to(self.root))
                self.assertTrue((directory / probe_name).is_file())
                self.assertEqual(before, self.get_checkout_snapshot())

    def test_overlapping_private_readers_keep_fixed_probe_names_and_indexes_independent(self):
        before = self.get_checkout_snapshot()
        other = InventoryFixtureTests()
        other.setUp()
        self.addCleanup(other.doCleanups)
        probe_name = "test_zz_untracked_probe.py"
        (self.directory / probe_name).write_text("first private probe\n")
        (other.directory / probe_name).write_text("second private probe\n")
        indexes = [(case.root / ".git/index").read_bytes() for case in (self, other)]
        release = self.root / "release"
        children = [case.start_fixture_reader(wait_file=release) for case in (self, other)]
        try:
            deadline = time.monotonic() + 5
            while not all((case.directory / "ready").exists() for case in (self, other)):
                self.assertLess(time.monotonic(), deadline, "private readers did not reach barrier")
                time.sleep(0.01)
            release.touch()
            for child in children:
                result = self.read_child(child)
                self.assertEqual({"test_fixture": ["tests.test_fixture.Fixture.test_tracked"]}, result["inventory"])
                self.assertEqual([probe_name], result["untracked"])
            self.assertEqual("first private probe\n", (self.directory / probe_name).read_text())
            self.assertEqual(indexes[1], (other.root / ".git/index").read_bytes())
            other.doCleanups()
            self.assertTrue((self.directory / probe_name).exists())
            self.assertEqual(indexes[0], (self.root / ".git/index").read_bytes())
            self.assertEqual(before, self.get_checkout_snapshot())
        finally:
            for child in children:
                if child.poll() is None:
                    child.kill()
                child.communicate()




class InventoryCheckWarningTests(unittest.TestCase):
    def test_check_warns_about_actual_untracked_tests_without_writing_inventory(self):
        import contextlib
        import io
        import tempfile
        import tests.test_inventory as inventory_module
        import update_test_inventory as updater
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            tests=root/'app/tests'
            tests.mkdir(parents=True)
            subprocess.run(['git','init','-q'],cwd=root,check=True)
            (tests/'test_tracked.py').write_text('# tracked fixture\n',encoding='utf-8')
            (tests/'test_new.py').write_text('# new local fixture\n',encoding='utf-8')
            (tests/'test_api.py').write_text('# explicitly excluded\n',encoding='utf-8')
            subprocess.run(['git','add','app/tests/test_tracked.py'],cwd=root,check=True)
            path=tests/'unit_test_inventory.json'
            expected={'test_tracked':['tests.test_tracked.Example.test_one']}
            path.write_text(updater.format_inventory(expected),encoding='utf-8')
            before=path.read_bytes()
            actual_check=updater.check_inventory
            for current,code in ((expected,0),({'test_tracked':[]},1)):
                with self.subTest(code=code), \
                     patch.object(updater,'__file__',str(root/'app/update_test_inventory.py')), \
                     patch.object(inventory_module,'__file__',str(tests/'test_inventory.py')), \
                     patch.object(updater,'get_unit_test_inventory',return_value=current), \
                     patch.object(updater,'check_inventory',side_effect=lambda:actual_check(path)), \
                     patch.object(updater,'write_inventory') as write:
                    output=io.StringIO()
                    with contextlib.redirect_stdout(output):
                        result=updater.main(['--check'])
                    self.assertEqual(code,result)
                    self.assertIn('WARNING:',output.getvalue())
                    self.assertIn('test_new.py',output.getvalue())
                    self.assertNotIn('  test_api.py',output.getvalue())
                    self.assertNotIn('  test_tracked.py',output.getvalue())
                    self.assertEqual(before,path.read_bytes())
                    write.assert_not_called()


class AtomicInventoryWriterTests(unittest.TestCase):
    def test_open_reader_keeps_prior_json_while_new_reader_gets_complete_inventory(self):
        import tempfile
        from unittest.mock import patch
        import update_test_inventory as updater
        inventory = {"test_z": ["unicode_é"], "test_a": ["first"]}
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "inventory.json")
            prior = b'{"prior": ["retained"]}'
            path.write_bytes(prior)
            with path.open("rb") as old_reader, \
                 patch.object(updater, "get_unit_test_inventory", return_value=inventory):
                self.assertEqual(path, updater.write_inventory(path))
                actual = path.read_bytes()
                self.assertEqual(inventory, json.loads(actual))
                self.assertLess(actual.index(b'"test_a"'), actual.index(b'"test_z"'))
                self.assertIn("é".encode(), actual)
                self.assertEqual(prior, old_reader.read())
                self.assertEqual([], updater.check_inventory(path))
                self.assertEqual(actual, path.read_bytes())
            self.assertEqual([path], list(Path(tmp).iterdir()))

    def test_failed_serialization_or_replace_preserves_prior_inventory_and_cleans_temporary_files(self):
        import tempfile
        from unittest.mock import patch
        import update_test_inventory as updater
        import utils
        def broken_dump(data, handle, **kwargs):
            handle.write('{"partial":')
            raise ValueError("serialization interrupted")
        for operation, error in (("serialize", ValueError), ("replace", OSError)):
            with self.subTest(operation=operation), tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp, "inventory.json")
                prior = b'{"prior": ["retained"]}'
                path.write_bytes(prior)
                fault = (patch.object(utils.json, "dump", side_effect=broken_dump)
                         if operation == "serialize" else
                         patch.object(utils.os, "replace", side_effect=OSError("disk failure")))
                with patch.object(updater, "get_unit_test_inventory", return_value={"new": ["complete"]}), fault:
                    with self.assertRaises(error):
                        updater.write_inventory(path)
                self.assertEqual(prior, path.read_bytes())
                self.assertEqual({"prior": ["retained"]}, json.loads(path.read_bytes()))
                self.assertEqual([path], list(Path(tmp).iterdir()))


if __name__ == "__main__":
    unittest.main()
